"""
Calibrador online — la pieza de "aprender de los errores", deliberadamente ACOTADA.

En vez de 60 perillas, ajusta UN parametro: el multiplicador k de la volatilidad.
Si el modelo es sobreconfiado (probabilidades muy extremas para lo que realmente
pasa), subir k acerca las probabilidades a 0.5 y baja el Brier; si es subconfiado,
baja k. Se re-estima por grid search minimizando el Brier sobre una ventana movil
de predicciones ya resueltas. Out-of-sample: solo usa mercados ya cerrados.
"""
from collections import deque, defaultdict

from src.fair_value import prob_up_m

K_GRID = [round(0.5 + 0.05 * i, 2) for i in range(31)]  # 0.50 .. 2.00


class Calibrator:
    def __init__(self, min_samples=60, max_per_market=5):
        self.k = 1.0
        self.buffer = deque(maxlen=3000)          # (m, tau, sigma, y)  y=1 si Up gano
        self.pending = defaultdict(list)          # slug -> [(m, tau, sigma)]
        self.min_samples = min_samples
        self.max_per_market = max_per_market
        self.last_brier = None
        self.last_recalib_ts = 0.0
        self.n_recalibs = 0
        self.k_prev = 1.0

    def observe(self, slug, m, tau, sigma):
        """Registra una prediccion (aun sin etiqueta) para este mercado."""
        lst = self.pending[slug]
        if len(lst) < self.max_per_market and tau > 5 and sigma:
            lst.append((m, tau, sigma))

    def label(self, slug, outcome_up: bool):
        """Etiqueta las predicciones pendientes del mercado con su resultado real."""
        y = 1.0 if outcome_up else 0.0
        for (m, tau, sigma) in self.pending.pop(slug, []):
            self.buffer.append((m, tau, sigma, y))

    def _brier_at(self, k) -> float | None:
        n = 0
        s = 0.0
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
        import time
        self.k_prev = self.k
        self.k = 0.7 * self.k + 0.3 * best_k   # cambio suavizado
        self.last_brier = self._brier_at(self.k)
        self.last_recalib_ts = time.time()
        self.n_recalibs += 1

    def stats(self):
        return {"k": self.k, "k_prev": self.k_prev, "model_brier": self.last_brier,
                "n": len(self.buffer), "n_recalibs": self.n_recalibs,
                "last_recalib_ts": self.last_recalib_ts}
