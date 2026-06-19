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
        # capa de calibracion isotonica (PAV): mapea fair_p crudo -> prob real.
        # El modelo de fair value esta S-comprimido (dice 65% cuando la realidad es
        # 77%). Ajustar solo k (un parametro) no arregla la forma; esta capa si.
        # Se re-ajusta sobre el buffer movil (rolling) -> se adapta al regimen.
        self._calib_x: list[float] | None = None   # knots: fair_p crudo
        self._calib_y: list[float] | None = None   # knots: prob calibrada
        self.last_brier_cal: float | None = None
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
        self._fit_calibration()                 # re-ajusta la capa isotonica con el k nuevo
        self.last_recalib_ts = time.time()
        self.n_recalibs += 1
        self.save_state()

    # --- capa de calibracion isotonica (PAV) ---

    @staticmethod
    def _pav(pairs):
        """Pool Adjacent Violators: ajuste isotonico (monotono creciente).
        pairs = [(x, y)] ordenado por x. Devuelve bloques [x0, x1, valor]."""
        blocks = []  # cada uno: [peso, suma_y, x0, x1]
        for x, y in pairs:
            blocks.append([1.0, y, x, x])
            while len(blocks) >= 2 and blocks[-2][1] / blocks[-2][0] >= blocks[-1][1] / blocks[-1][0]:
                w2, s2, a2, b2 = blocks.pop()
                w1, s1, a1, b1 = blocks.pop()
                blocks.append([w1 + w2, s1 + s2, a1, b2])
        return [(a, b, s / w) for (w, s, a, b) in blocks]

    def _fit_calibration(self):
        """Ajusta fair_p_crudo -> frecuencia real sobre el buffer movil."""
        pts = []
        for (m, tau, sigma, y) in self.buffer:
            p = prob_up_m(m, tau, sigma * self.k)
            if p is not None:
                pts.append((p, y))
        if len(pts) < self.min_samples:
            self._calib_x = self._calib_y = None
            return
        pts.sort(key=lambda t: t[0])
        blocks = self._pav(pts)
        # knots = centro de cada bloque -> su valor calibrado
        self._calib_x = [(a + b) / 2 for (a, b, _) in blocks]
        self._calib_y = [v for (_, _, v) in blocks]
        # Brier del modelo YA calibrado, para comparar contra el crudo
        s, n = 0.0, 0
        for (p, y) in pts:
            pc = self.calibrate_prob(p)
            s += (pc - y) ** 2
            n += 1
        self.last_brier_cal = s / n if n else None

    def calibrate_prob(self, p: float) -> float:
        """Mapea una P(Up) cruda a la calibrada (interpolacion lineal entre knots)."""
        xs, ys = self._calib_x, self._calib_y
        if not xs or p is None:
            return p
        if p <= xs[0]:
            return ys[0]
        if p >= xs[-1]:
            return ys[-1]
        import bisect
        j = bisect.bisect_right(xs, p) - 1
        j = max(0, min(len(xs) - 2, j))
        x0, x1 = xs[j], xs[j + 1]
        y0, y1 = ys[j], ys[j + 1]
        t = (p - x0) / (x1 - x0) if x1 > x0 else 0.0
        return min(0.999, max(0.001, y0 + t * (y1 - y0)))

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
            "calib_x": self._calib_x, "calib_y": self._calib_y,
            "last_brier_cal": self.last_brier_cal,
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
            self._calib_x = s.get("calib_x")
            self._calib_y = s.get("calib_y")
            self.last_brier_cal = s.get("last_brier_cal")
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass  # primera vez: usa defaults

    # --- stats ---

    def stats(self):
        return {
            "k": self.k, "k_prev": self.k_prev, "model_brier": self.last_brier,
            "model_brier_cal": self.last_brier_cal,
            "calibrated": bool(self._calib_x),
            "n": len(self.buffer), "n_recalibs": self.n_recalibs,
            "last_recalib_ts": self.last_recalib_ts,
            "generation": self.generation,
            "kelly_fraction": self.kelly_fraction,
            "min_edge": self.min_edge,
            "last_mutation_ts": self.last_mutation_ts,
            "last_wr_at_mutation": self.last_wr_at_mutation,
        }
