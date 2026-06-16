"""
Motor de estrategia — edge de lag spot↔implicito, con todas las barandas.

Por cada mercado BTC Up/Down activo:
  1. fair_p = P(Up) del modelo (spot vs open, vol calibrada).
  2. edge_up   = fair_p     - ask_up      (comprar Up si esta barato)
     edge_down = (1-fair_p) - ask_down    (comprar Down si esta barato)
  3. Si el mejor edge supera MIN_EDGE (y no es absurdo), dimensiona con
     fraccion de Kelly topeada y abre la apuesta paper.

Cada decision pasa por hard-gates: limite diario, max concurrentes, una apuesta
por ventana, frescura de datos (kill-switch), tiempo restante, spread, liquidez.
"""
import math
import time
from collections import deque

import config
from src.fair_value import prob_up
from src.state import now_ms


class StrategyEngine:
    def __init__(self, state, portfolio, calibrator, vol, storage):
        self.state = state
        self.pf = portfolio
        self.cal = calibrator
        self.vol = vol
        self.storage = storage
        self.last_evals: dict[str, dict] = {}   # slug -> ultimo eval (para el TUI)
        self.log = deque(maxlen=40)              # eventos recientes (para el TUI)
        self._last_pred_log: dict[str, float] = {}
        # --- metricas de actividad (para el dashboard: probar que esta vivo y aprendiendo) ---
        self.started = time.time()
        self.ticks = 0
        self.n_evals = 0
        self.n_predictions = 0
        self.scanning = 0
        self.last_tick_ts = 0.0
        self.last_bet_ts = 0.0

    def _tradable(self):
        now = time.time()
        out = []
        for slug, mk in self.state.markets.items():
            if mk.asset not in config.TRADE_ASSETS or mk.interval not in config.TRADE_INTERVALS:
                continue
            if not (mk.window_start_ts <= now < mk.window_end_ts):
                continue
            out.append(mk)
        return out

    def tick(self):
        self.ticks += 1
        self.last_tick_ts = time.time()
        # gates globales
        halted = self.pf.daily_limit_hit()
        full = self.pf.n_open >= config.MAX_CONCURRENT
        trad = self._tradable()
        self.scanning = len(trad)
        active = set()
        for mk in trad:
            active.add(mk.slug)
            ev = self._evaluate(mk)
            if ev:
                self.n_evals += 1
                self.last_evals[mk.slug] = ev
                if not halted and not full:
                    self._maybe_bet(mk, ev)
        # limpiar evals de mercados que ya cerraron (que no quede basura en el scan)
        for slug in list(self.last_evals):
            if slug not in active:
                self.last_evals.pop(slug, None)

    def _evaluate(self, mk):
        s = self.state
        spot_now = s.spot_mid(mk.symbol)
        spot_open = s.window_open.get(mk.slug)
        sigma_base = self.vol.sigma_per_sec
        if spot_now is None or not spot_open or not self.vol.ready():
            return None

        tau = mk.window_end_ts - time.time()
        if tau <= 0:
            return None
        sigma_eff = self.cal.k * sigma_base
        m = math.log(spot_now / spot_open)
        fair_p = prob_up(spot_open, spot_now, tau, sigma_eff)
        if fair_p is None:
            return None

        up_ob = s.obooks.get(mk.up_token_id)
        dn_ob = s.obooks.get(mk.down_token_id)
        up = up_ob.top() if up_ob else (None,) * 5
        dn = dn_ob.top() if dn_ob else (None,) * 5
        ask_up, asz_up, mid_up = up[1], up[3], up[4]
        ask_dn, asz_dn, mid_dn = dn[1], dn[3], dn[4]

        edge_up = (fair_p - ask_up) if ask_up else None
        edge_dn = ((1 - fair_p) - ask_dn) if ask_dn else None

        ev = {
            "slug": mk.slug, "asset": mk.asset, "interval": mk.interval,
            "window_end": mk.window_end_ts, "tau": tau, "m": m, "sigma": sigma_base,
            "fair_p": fair_p, "spot_open": spot_open, "spot_now": spot_now,
            "ask_up": ask_up, "ask_dn": ask_dn, "asz_up": asz_up, "asz_dn": asz_dn,
            "bid_up": up[0], "bid_dn": dn[0], "mid_up": mid_up, "mid_dn": mid_dn,
            "edge_up": edge_up, "edge_dn": edge_dn,
            "up_token": mk.up_token_id, "dn_token": mk.down_token_id,
        }

        # log periodico de prediccion + alimentar calibrador (cada ~5s)
        last = self._last_pred_log.get(mk.slug, 0)
        if time.time() - last >= 5:
            self._last_pred_log[mk.slug] = time.time()
            self.storage.put("prediction", (now_ms(), mk.slug, mk.asset, fair_p,
                                            mid_up, mid_dn, edge_up, edge_dn,
                                            spot_open, spot_now, tau, sigma_base, None))
            self.cal.observe(mk.slug, m, tau, sigma_base)
            self.n_predictions += 1
        return ev

    def _maybe_bet(self, mk, ev):
        if config.ONE_BET_PER_MARKET and self.pf.has_position(mk.slug):
            return
        if ev["tau"] < config.MIN_TIME_LEFT_SEC:
            return
        # kill-switch por frescura de datos (None = sin datos = stale; 0ms es fresco)
        spot_age = self.state.spot_age_ms(mk.symbol)
        if spot_age is None or spot_age > config.STALE_SPOT_MS:
            return

        # elegir el lado con mejor edge
        cands = []
        if ev["edge_up"] is not None:
            cands.append(("up", ev["fair_p"], ev["ask_up"], ev["bid_up"], ev["asz_up"], ev["edge_up"], mk.up_token_id))
        if ev["edge_dn"] is not None:
            cands.append(("down", 1 - ev["fair_p"], ev["ask_dn"], ev["bid_dn"], ev["asz_dn"], ev["edge_dn"], mk.down_token_id))
        cands.sort(key=lambda c: c[5], reverse=True)
        if not cands:
            return
        side, p_side, ask, bid, asz, edge, token = cands[0]

        if edge < config.MIN_EDGE or edge > config.MAX_EDGE_TRUST:
            return
        if not ask or ask >= 0.99 or not asz:
            return
        book_age = self.state.book_age_ms(token)
        if book_age is None or book_age > config.STALE_BOOK_MS:
            return
        if bid and (ask - bid) > config.MAX_SPREAD:
            return

        # sizing: fraccion de Kelly topeada
        f_star = (p_side - ask) / (1 - ask)
        frac = max(0.0, min(config.KELLY_FRACTION * f_star, config.MAX_BET_PCT))
        stake = self.pf.equity * frac
        shares = stake / ask
        shares = min(shares, asz)            # no exceder la liquidez en el mejor ask
        stake = shares * ask
        if stake < config.MIN_BET:
            return

        pos = self.pf.open_bet(mk, side, ask, shares, p_side, edge)
        if pos:
            self.last_bet_ts = time.time()
            self.log.appendleft(
                f"BET {side.upper():4} {mk.interval} {mk.slug.split('-')[0]} @ {ask:.2f} "
                f"${stake:5.0f} edge {edge*100:4.1f}% p {p_side:.2f}")
