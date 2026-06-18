"""
Calibrador online + mutacion genetica.

Calibrador de vol: ajusta UN parametro k (multiplicador de sigma) minimizando
el Brier sobre una ventana movil de predicciones ya resueltas (OOS).

Mutacion genetica (inspirada en CerberoMemory de Antigravity): cada
MUTATION_THRESHOLD apuestas liquidadas, evalua el win_rate de las ultimas
MUTATION_WINDOW apuestas y ajusta kelly_fraction y min_edge.
  - win_rate < 45%  -> mas conservador (sube edge minimo, baja Kelly)
  - win_rate > 65%  -> mas agresivo    (baja edge minimo, sube Kelly)
El ajuste es acotado para no volverse erratico. La estrategia usa
cal.kelly_fraction y cal.min_edge en lugar de los valores fijos de config.
"""
import json
import os
import time
from collections import deque, defaultdict

from src.fair_value import prob_up_m
import config

STATE_PATH = "data/calibrator_state.json"

K_GRID = [round(0.5 + 0.05 * i, 2) for i in range(31)]  # 0.50 .. 2.00

MUTATION_WINDOW    = 20    # ultimas N apuestas para medir win_rate
MUTATION_THRESHOLD = 10    # recalcular cada N apuestas liquidadas
KELLY_MIN, KELLY_MAX = 0.10, 0.50
EDGE_MIN,  EDGE_MAX  = 0.02, 0.15


class Calibrator:
    def __init__(self, min_samples=60, max_per_market=5):
        self.k = 1.0
        self.buffer = deque(maxlen=3000)          # (m, tau, sigma, y)
        self.pending = defaultdict(list)          # slug -> [(m, tau, sigma)]
        self.min_samples = min_samples
        self.max_per_market = max_per_market
        self.last_brier = None
        self.last_recalib_ts = 0.0
        self.n_recalibs = 0
        self.k_prev = 1.0
        # parametros geneticos — parten de config y mutan con performance
        self.kelly_fraction = config.KELLY_FRACTION
        self.min_edge = config.MIN_EDGE
        # estado de mutacion
        self._recent_outcomes: deque = deque(maxlen=MUTATION_WINDOW)
        self._n_since_mutation: int = 0
        self.generation = 1
        self.last_mutation_ts = 0.0
        self.last_wr_at_mutation: float | None = None

    # --- calibracion de vol (k) ---

    def observe(self, slug, m, tau, sigma):
        """Registra una prediccion pendiente de etiqueta."""
        lst = self.pending[slug]
        if len(lst) < self.max_per_market and tau > 5 and sigma:
            lst.append((m, tau, sigma))

    def label(self, slug, outcome_up: bool):
        """Etiqueta con el resultado real las predicciones pendientes del mercado."""
        y = 1.0 if outcome_up else 0.0
        for (m, tau, sigma) in self.pending.pop(slug, []):
            self.buffer.append((m, tau, sigma, y))

    def _brier_at(self, k) -> float | None:
        n, s = 0, 0.0
        for (m, tau, sigma, y) in self.buffer:
            p = prob_up_m(m, tau, sigma * k)
            if p is None:
                continue
            s += (p - y) ** 2
            n += 1
        return s / n if n else None

    def recalibrate(self):
        if len(self.buffer) < self.min_samples:
            return
        best_k, best_b = self.k, None
        for k in K_GRID:
            b = self._brier_at(k)
            if b is not None and (best_b is None or b < best_b):
                best_b, best_k = b, k
        self.k_prev = self.k
        self.k = 0.7 * self.k + 0.3 * best_k    # cambio suavizado
        self.last_brier = self._brier_at(self.k)
        self.last_recalib_ts = time.time()
        self.n_recalibs += 1
        self.save_state()

    # --- mutacion genetica ---

    def record_bet_outcome(self, won: bool):
        """Llamado por settle_loop tras liquidar cada apuesta. Dispara mutacion si corresponde."""
        self._recent_outcomes.append(won)
        self._n_since_mutation += 1
        if (self._n_since_mutation >= MUTATION_THRESHOLD and
                len(self._recent_outcomes) >= MUTATION_THRESHOLD):
            self._mutate()

    def _mutate(self):
        wins = sum(self._recent_outcomes)
        wr = wins / len(self._recent_outcomes)
        self.last_wr_at_mutation = wr

        if wr < 0.45:
            self.kelly_fraction = max(KELLY_MIN, self.kelly_fraction - 0.025)
            self.min_edge = min(EDGE_MAX, self.min_edge + 0.005)
        elif wr > 0.65:
            self.kelly_fraction = min(KELLY_MAX, self.kelly_fraction + 0.025)
            self.min_edge = max(EDGE_MIN, self.min_edge - 0.003)

        self.generation += 1
        self._n_since_mutation = 0
        self.last_mutation_ts = time.time()
        self.save_state()

    # --- reconstruccion del cerebro desde la DB ---

    def warm_start_from_db(self, db_path: str) -> int:
        """Rellena el buffer con TODAS las predicciones ya etiquetadas de la DB
        y recalibra. Asi el calibrador arranca con todo lo aprendido en vez de
        desde cero (necesitaba ~60 muestras nuevas antes de poder calibrar)."""
        import math
        import sqlite3
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            return 0
        try:
            rows = conn.execute(
                "SELECT spot_open, spot_now, tau, sigma, outcome FROM predictions "
                "WHERE outcome IS NOT NULL AND spot_open IS NOT NULL "
                "AND spot_now IS NOT NULL AND tau > 5 AND sigma IS NOT NULL "
                "ORDER BY ts DESC LIMIT ?", (self.buffer.maxlen,)).fetchall()
        except sqlite3.OperationalError:
            conn.close()
            return 0
        conn.close()
        n = 0
        for spot_open, spot_now, tau, sigma, outcome in reversed(rows):
            if not spot_open or not spot_now or spot_open <= 0 or spot_now <= 0:
                continue
            m = math.log(spot_now / spot_open)
            y = 1.0 if outcome == "up" else 0.0
            self.buffer.append((m, tau, sigma, y))
            n += 1
        if len(self.buffer) >= self.min_samples:
            self.recalibrate()
        return n

    # --- persistencia ---

    def save_state(self):
        state = {
            "k": self.k, "k_prev": self.k_prev,
            "n_recalibs": self.n_recalibs,
            "last_recalib_ts": self.last_recalib_ts,
            "last_brier": self.last_brier,
            "kelly_fraction": self.kelly_fraction,
            "min_edge": self.min_edge,
            "generation": self.generation,
            "last_mutation_ts": self.last_mutation_ts,
            "last_wr_at_mutation": self.last_wr_at_mutation,
            "n_since_mutation": self._n_since_mutation,
            "recent_outcomes": list(self._recent_outcomes),
        }
        try:
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, STATE_PATH)
        except Exception:
            pass

    def load_state(self):
        try:
            with open(STATE_PATH) as f:
                s = json.load(f)
            self.k = s.get("k", 1.0)
            self.k_prev = s.get("k_prev", 1.0)
            self.n_recalibs = s.get("n_recalibs", 0)
            self.last_recalib_ts = s.get("last_recalib_ts", 0.0)
            self.last_brier = s.get("last_brier")
            self.kelly_fraction = s.get("kelly_fraction", config.KELLY_FRACTION)
            self.min_edge = s.get("min_edge", config.MIN_EDGE)
            self.generation = s.get("generation", 1)
            self.last_mutation_ts = s.get("last_mutation_ts", 0.0)
            self.last_wr_at_mutation = s.get("last_wr_at_mutation")
            self._n_since_mutation = s.get("n_since_mutation", 0)
            self._recent_outcomes = deque(s.get("recent_outcomes", []),
                                          maxlen=MUTATION_WINDOW)
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass  # primera vez: usa defaults

    # --- stats ---

    def stats(self):
        return {
            "k": self.k, "k_prev": self.k_prev, "model_brier": self.last_brier,
            "n": len(self.buffer), "n_recalibs": self.n_recalibs,
            "last_recalib_ts": self.last_recalib_ts,
            "generation": self.generation,
            "kelly_fraction": self.kelly_fraction,
            "min_edge": self.min_edge,
            "last_mutation_ts": self.last_mutation_ts,
            "last_wr_at_mutation": self.last_wr_at_mutation,
        }
