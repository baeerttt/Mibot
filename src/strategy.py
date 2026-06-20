"""
Motor de estrategia — edge de lag spot<->implicito, con todas las barandas.

Por cada mercado BTC Up/Down activo:
  1. fair_p = P(Up) del modelo (spot vs open, vol calibrada).
  2. edge_up   = fair_p     - ask_up      (comprar Up si esta barato)
     edge_down = (1-fair_p) - ask_down    (comprar Down si esta barato)
  3. Score de confianza 4-factor (edge, liquidez, spread).
  4. Si el mejor edge supera cal.min_edge y la confianza supera MIN_CONFIDENCE,
     dimensiona con fraccion de Kelly (cal.kelly_fraction, topeada) y abre la apuesta.

Cada decision pasa por hard-gates: limite diario, max concurrentes, una apuesta
por ventana, frescura de datos (kill-switch), tiempo restante, spread, liquidez.
Los parametros de Kelly y edge minimo son adaptados por la mutacion genetica del
Calibrator segun el win_rate OOS.
"""
import math
import time
from collections import deque

import config
from src import fees
from src.fair_value import prob_up
from src.state import now_ms


class StrategyEngine:
    def __init__(self, state, portfolio, calibrator, vols, storage):
        self.state = state
        self.pf = portfolio
        self.cal = calibrator
        self.vols = vols          # dict symbol -> VolEstimator (uno por activo)
        self.storage = storage
        self.last_evals: dict[str, dict] = {}
        self.log = deque(maxlen=40)
        self._last_pred_log: dict[str, float] = {}
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

    def _confidence(self, edge: float, ask: float, bid, asz) -> float:
        """Score 0-1 combinando edge (50%), liquidez (25%) y spread (25%)."""
        min_e = self.cal.min_edge
        spread = (ask - bid) if bid else config.MAX_SPREAD
        edge_f = min(1.0, edge / (3.0 * min_e)) if (edge > 0 and min_e > 0) else 0.0
        liq_f  = min(1.0, (asz or 0) / 500.0)
        spr_f  = max(0.0, 1.0 - spread / config.MAX_SPREAD)
        return round(0.50 * edge_f + 0.25 * liq_f + 0.25 * spr_f, 3)

    def tick(self):
        self.ticks += 1
        self.last_tick_ts = time.time()
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
        for slug in list(self.last_evals):
            if slug not in active:
                self.last_evals.pop(slug, None)

    def _evaluate(self, mk):
        s = self.state
        vol = self.vols.get(mk.symbol)
        if vol is None:
            return None
        spot_now = s.spot_mid(mk.symbol)
        spot_open = s.window_open.get(mk.slug)
        sigma_base = vol.sigma_per_sec
        if spot_now is None or not spot_open or not vol.ready():
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
        bid_up, ask_up, _, asz_up, mid_up = up
        bid_dn, ask_dn, _, asz_dn, mid_dn = dn

        edge_up = (fair_p - ask_up) if ask_up else None
        edge_dn = ((1 - fair_p) - ask_dn) if ask_dn else None

        conf_up = self._confidence(edge_up, ask_up, bid_up, asz_up) if edge_up is not None else None
        conf_dn = self._confidence(edge_dn, ask_dn, bid_dn, asz_dn) if edge_dn is not None else None

        ev = {
            "slug": mk.slug, "asset": mk.asset, "interval": mk.interval,
            "window_end": mk.window_end_ts, "tau": tau, "m": m, "sigma": sigma_base,
            "fair_p": fair_p, "spot_open": spot_open, "spot_now": spot_now,
            "ask_up": ask_up, "ask_dn": ask_dn, "asz_up": asz_up, "asz_dn": asz_dn,
            "bid_up": bid_up, "bid_dn": bid_dn, "mid_up": mid_up, "mid_dn": mid_dn,
            "edge_up": edge_up, "edge_dn": edge_dn,
            "conf_up": conf_up, "conf_dn": conf_dn,
            "up_token": mk.up_token_id, "dn_token": mk.down_token_id,
        }

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
        spot_age = self.state.spot_age_ms(mk.symbol)
        if spot_age is None or spot_age > config.STALE_SPOT_MS:
            return

        # usar parametros geneticos (adaptativos) del calibrador
        min_edge = self.cal.min_edge
        kelly_frac = self.cal.kelly_fraction

        # CALIBRACION: para DECIDIR apostamos con la P(Up) calibrada (honesta), no
        # la cruda. El log de predictions queda crudo (no contaminar el analisis);
        # solo el edge/confianza con que se apuesta usan la probabilidad corregida.
        fair_pc = self.cal.calibrate_prob(ev["fair_p"]) if config.USE_CALIBRATION else ev["fair_p"]

        cands = []
        if ev["ask_up"]:
            e_up = fair_pc - ev["ask_up"]
            c_up = self._confidence(e_up, ev["ask_up"], ev["bid_up"], ev["asz_up"])
            cands.append(("up", fair_pc, ev["ask_up"], ev["bid_up"],
                          ev["asz_up"], e_up, c_up, mk.up_token_id))
        if ev["ask_dn"]:
            e_dn = (1 - fair_pc) - ev["ask_dn"]
            c_dn = self._confidence(e_dn, ev["ask_dn"], ev["bid_dn"], ev["asz_dn"])
            cands.append(("down", 1 - fair_pc, ev["ask_dn"], ev["bid_dn"],
                          ev["asz_dn"], e_dn, c_dn, mk.down_token_id))
        cands.sort(key=lambda c: c[5], reverse=True)
        if not cands:
            return
        side, p_side, ask, bid, asz, edge, conf, token = cands[0]

        # Dead-zone: cerca de 0.50 el fee de Polymarket es MAXIMO (1.8%) y el modelo no
        # tiene edge (moneda al aire). El barrido fee-real (backtest.py) mostro que
        # evitar |ask-0.50| < DEAD_ZONE da vuelta el signo del PnL. Es el lever #1.
        if config.DEAD_ZONE > 0 and abs(ask - 0.5) < config.DEAD_ZONE:
            return

        # El edge NETO (despues del costo de ejecucion + fee real del taker) es el que
        # tiene que valer. fee_per_share(ask) es maximo en 0.50 -> exige mas edge ahi.
        # Asi no apostamos cuando el spread/slippage/fee se comen la ventaja.
        cost = config.EXEC_COST
        fee_ps = fees.fee_per_share(ask) if config.APPLY_FEES_LIVE else 0.0
        net_edge = edge - cost - fee_ps
        if net_edge < min_edge or edge > config.MAX_EDGE_TRUST:
            return
        if conf is None or conf < config.MIN_CONFIDENCE:
            return
        if not ask or ask >= 0.99 or not asz:
            return
        book_age = self.state.book_age_ms(token)
        if book_age is None or book_age > config.STALE_BOOK_MS:
            return
        if bid and (ask - bid) > config.MAX_SPREAD:
            return

        # Filtro de correlacion: no acumular apuestas en la misma direccion entre
        # activos correlacionados (seria una sola apuesta apalancada a la cripto).
        same_dir = sum(1 for p in self.pf.positions.values() if p.side == side)
        if same_dir >= config.MAX_SAME_DIR_CONCURRENT:
            return

        # Sizing conservador: Kelly sobre el precio EFECTIVO (ask+cost+fee). PERO a la
        # tabla bets va el precio SIN fee (ask+cost): el fee se contabiliza UNA sola vez
        # en el analisis (track_record.py/walkforward.py calculan el fee desde shares y
        # price). Si ademas lo bakearamos en el price guardado, se contaria DOBLE y se
        # contaminaria el brier_market. Convencion: la DB guarda PnL/price BRUTO de fee.
        ask_eff = ask + cost + fee_ps        # efectivo: para dimensionar (conservador)
        price_book = ask + cost              # lo que se registra (bruto de fee)
        if ask_eff >= 0.99:
            return
        f_star = (p_side - ask_eff) / (1 - ask_eff)
        if f_star <= 0:
            return
        frac = max(0.0, min(kelly_frac * f_star, config.MAX_BET_PCT))
        stake = self.pf.equity * frac
        shares = stake / ask_eff
        shares = min(shares, asz)
        stake = shares * price_book
        if stake < config.MIN_BET:
            return

        pos = self.pf.open_bet(mk, side, price_book, shares, p_side, net_edge)
        if pos:
            self.last_bet_ts = time.time()
            self.log.appendleft(
                f"BET {side.upper():4} {mk.interval} {mk.slug.split('-')[0]} @ {ask_eff:.2f} "
                f"${stake:5.0f} edge {net_edge*100:4.1f}% conf {conf:.2f}")
