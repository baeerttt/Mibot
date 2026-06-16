"""
Modelo de fair value para los mercados Up/Down.

Idea: en una ventana [t0, t1], "Up" gana si el precio al cierre > precio de apertura.
Modelamos el log-retorno restante como un random walk sin drift con volatilidad sigma.
Entonces, con m = log(precio_actual / precio_apertura) y tau = segundos restantes:

    P(Up) = Phi( m / (sigma * sqrt(tau)) )

Cuando el precio ya esta arriba del open (m>0), P(Up) > 0.5, y tiende a 1 a medida
que tau -> 0. La volatilidad sigma se estima en vivo del spot (EWMA de retornos).

Este es el "valor justo" contra el cual comparamos la probabilidad implicita de
Polymarket. El edge es la diferencia, y aparece cuando el implicito reprecia tarde.
"""
import math


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


class VolEstimator:
    """EWMA de retornos log ~1s del spot -> sigma por sqrt-segundo."""

    def __init__(self, halflife_sec: float = 120.0, sample_sec: float = 1.0):
        self.sample_sec = sample_sec
        self.alpha = 1.0 - 0.5 ** (sample_sec / halflife_sec)
        self.var: float | None = None
        self.last_price: float | None = None
        self.last_ts: float | None = None
        self.samples = 0

    def update(self, price: float, ts_ms: int):
        ts = ts_ms / 1000.0
        if self.last_price is None or price <= 0:
            self.last_price, self.last_ts = price, ts
            return
        dt = ts - self.last_ts
        if dt < self.sample_sec:
            return  # no sobre-muestrear
        r = math.log(price / self.last_price)
        # normalizar la magnitud a una ventana de sample_sec exacta
        r_norm = r * math.sqrt(self.sample_sec / dt)
        r2 = r_norm * r_norm
        self.var = r2 if self.var is None else (1 - self.alpha) * self.var + self.alpha * r2
        self.last_price, self.last_ts = price, ts
        self.samples += 1

    @property
    def sigma_per_sec(self) -> float | None:
        if not self.var or self.var <= 0:
            return None
        return math.sqrt(self.var / self.sample_sec)

    def ready(self) -> bool:
        return self.samples >= 20 and self.sigma_per_sec is not None


def prob_up_m(m: float, tau_sec: float, sigma_per_sec: float) -> float | None:
    """P(Up) directo desde el log-move m = log(precio_actual/precio_apertura)."""
    if not (sigma_per_sec and sigma_per_sec > 0):
        return None
    if tau_sec <= 0:
        return 0.999 if m > 0 else 0.001
    s = sigma_per_sec * math.sqrt(tau_sec)
    if s <= 0:
        return 0.999 if m > 0 else 0.001
    return min(0.999, max(0.001, _norm_cdf(m / s)))


def prob_up(spot_open: float, spot_now: float, tau_sec: float, sigma_per_sec: float) -> float | None:
    """Probabilidad de que 'Up' resuelva, dado el movimiento desde el open."""
    if not (spot_open and spot_now and sigma_per_sec and sigma_per_sec > 0):
        return None
    return prob_up_m(math.log(spot_now / spot_open), tau_sec, sigma_per_sec)
