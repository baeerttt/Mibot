"""
Portfolio paper (shadow training). Capital ficticio, sin riesgo real.

Modelo de una apuesta binaria a precio `a` (el ask que pagamos):
  - comprar `shares` cuesta  shares*a
  - si el lado apostado gana -> cada share vale 1   -> pnl = shares*(1-a)
  - si pierde                -> vale 0               -> pnl = -shares*a (= -stake)

Trackea bankroll, P&L realizado, win rate, Brier de las apuestas, y el limite de
perdida diaria. Persiste cada apuesta y la curva de capital a SQLite.
"""
import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

import config
from src.state import now_ms


def _ro_conn(db_path):
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        c.execute("PRAGMA busy_timeout=5000")
        return c
    except sqlite3.OperationalError:
        return None


def _rw_conn(db_path):
    try:
        c = sqlite3.connect(db_path)
        c.execute("PRAGMA busy_timeout=5000")
        return c
    except sqlite3.OperationalError:
        return None


@dataclass
class Position:
    uid: str
    slug: str
    asset: str
    interval: str
    side: str          # 'up' | 'down'
    price: float       # ask pagado
    shares: float
    stake: float
    fair_p: float      # prob estimada del lado apostado, al abrir
    edge: float
    ts_open: int


class PaperPortfolio:
    def __init__(self, storage, start=config.PAPER_BANKROLL):
        self.storage = storage
        self.start = start
        self.realized = 0.0
        self.positions: dict[str, Position] = {}
        self.n_bets = 0
        self.n_settled = 0
        self.wins = 0
        self.brier_sum = 0.0
        self.streak = 0                    # >0 racha ganadora, <0 perdedora
        self.up_bets = 0                   # cuantas apuestas a UP vs DOWN
        self.down_bets = 0
        self.recent = deque(maxlen=20)     # ultimas apuestas liquidadas (para TUI)
        self.equity_hist = deque(maxlen=80)  # para el sparkline del dashboard
        self.day = datetime.now(timezone.utc).date()
        self.day_start_realized = 0.0
        # drawdown en vivo: pico de equity y maxima caida desde el pico
        self.peak_equity = start
        self.max_dd_abs = 0.0
        self.max_dd_frac = 0.0

    # --- contabilidad ---
    @property
    def open_exposure(self):
        return sum(p.stake for p in self.positions.values())

    @property
    def equity(self):
        # capital realizado (no marca a mercado las abiertas, conservador)
        return self.start + self.realized

    @property
    def cash(self):
        return self.equity - self.open_exposure

    @property
    def n_open(self):
        return len(self.positions)

    @property
    def losses(self):
        return self.n_settled - self.wins

    @property
    def win_rate(self):
        return self.wins / self.n_settled if self.n_settled else None

    @property
    def brier(self):
        return self.brier_sum / self.n_settled if self.n_settled else None

    @property
    def roi(self):
        return self.realized / self.start

    def _roll_day(self):
        today = datetime.now(timezone.utc).date()
        if today != self.day:
            self.day = today
            self.day_start_realized = self.realized

    @property
    def day_pnl(self):
        self._roll_day()
        return self.realized - self.day_start_realized

    def daily_limit_hit(self) -> bool:
        return self.day_pnl <= -config.DAILY_LOSS_LIMIT_PCT * self.start

    def soft_drawdown_hit(self) -> bool:
        return self.day_pnl <= -config.SOFT_DRAWDOWN_PCT * self.start

    def has_position(self, slug: str) -> bool:
        return any(p.slug == slug for p in self.positions.values())

    def _update_drawdown(self):
        """Actualiza pico de equity y maximo drawdown (llamar tras cada cambio de PnL)."""
        eq = self.equity
        if eq > self.peak_equity:
            self.peak_equity = eq
        dd = self.peak_equity - eq
        if dd > self.max_dd_abs:
            self.max_dd_abs = dd
        if self.peak_equity > 0:
            self.max_dd_frac = max(self.max_dd_frac, dd / self.peak_equity)

    @property
    def recent_win_rate(self):
        """Win rate de las ultimas apuestas liquidadas (forma reciente; None si vacio)."""
        if not self.recent:
            return None
        return sum(1 for r in self.recent if r["win"]) / len(self.recent)

    # --- operaciones ---
    def open_bet(self, mk, side, price, shares, fair_p, edge) -> Position | None:
        stake = shares * price
        if stake < config.MIN_BET or stake > self.cash:
            return None
        uid = f"{mk.slug}|{side}|{now_ms()}"
        pos = Position(uid, mk.slug, mk.asset, mk.interval, side, price,
                       shares, stake, fair_p, edge, now_ms())
        self.positions[uid] = pos
        self.n_bets += 1
        if side == "up":
            self.up_bets += 1
        else:
            self.down_bets += 1
        self.storage.put("bet", (uid, pos.ts_open, mk.slug, mk.asset, mk.interval,
                                 side, price, shares, stake, fair_p, edge,
                                 "open", None, None, None))
        return pos

    def settle_market(self, slug: str, outcome: str) -> list[bool]:
        """Liquida todas las posiciones abiertas de un mercado. outcome: 'up'|'down'.
        Retorna lista de bool (True=win) por cada posicion liquidada."""
        results = []
        for uid, pos in list(self.positions.items()):
            if pos.slug != slug:
                continue
            win = (pos.side == outcome)
            pnl = pos.shares * (1 - pos.price) if win else -pos.stake
            self.realized += pnl
            self.n_settled += 1
            self.wins += 1 if win else 0
            self.brier_sum += (pos.fair_p - (1.0 if win else 0.0)) ** 2
            self.streak = (self.streak + 1) if (win and self.streak >= 0) else \
                          (self.streak - 1) if (not win and self.streak <= 0) else \
                          (1 if win else -1)
            self.equity_hist.append(self.equity)
            self._update_drawdown()
            self.recent.appendleft({
                "slug": slug, "side": pos.side, "outcome": outcome,
                "price": pos.price, "stake": pos.stake, "pnl": pnl, "win": win,
            })
            self.storage.execute(
                "UPDATE bets SET status='settled', ts_settle=?, outcome=?, pnl=? WHERE bet_uid=?",
                (now_ms(), outcome, pnl, uid))
            self.positions.pop(uid, None)
            results.append(win)
        return results

    # --- reconciliacion y reconstruccion al arrancar ---

    def reconcile_orphans(self, db_path: str) -> int:
        """Liquida apuestas que quedaron 'open' en la DB porque el bot se apago
        antes de que su ventana cerrara. Usa el spot historico (spot_tick) para
        derivar el outcome. Asi ninguna apuesta queda colgada sin resolver."""
        rw = _rw_conn(db_path)
        if rw is None:
            return 0
        settled = 0
        try:
            orphans = rw.execute(
                "SELECT bet_uid, slug, asset, side, price, shares, stake, fair_p "
                "FROM bets WHERE status='open'").fetchall()
            for uid, slug, asset, side, price, shares, stake, fair_p in orphans:
                mk = rw.execute(
                    "SELECT window_start_ts, window_end_ts FROM markets WHERE slug=?",
                    (slug,)).fetchone()
                if not mk:
                    continue
                w_start, w_end = mk
                sym = config.ASSETS.get(asset)
                if not sym:
                    continue
                o = rw.execute(
                    "SELECT mid FROM spot_tick WHERE symbol=? AND ts>=? "
                    "ORDER BY ts ASC LIMIT 1", (sym, int(w_start * 1000))).fetchone()
                c = rw.execute(
                    "SELECT mid FROM spot_tick WHERE symbol=? AND ts<=? "
                    "ORDER BY ts DESC LIMIT 1", (sym, int(w_end * 1000))).fetchone()
                if not o or not c or not o[0] or not c[0]:
                    continue
                outcome = "up" if c[0] > o[0] else "down"
                win = (side == outcome)
                pnl = shares * (1 - price) if win else -stake
                rw.execute(
                    "UPDATE bets SET status='settled', ts_settle=?, outcome=?, pnl=? "
                    "WHERE bet_uid=?", (now_ms(), outcome, pnl, uid))
                rw.execute(
                    "INSERT OR REPLACE INTO resolutions(slug,asset,window_start_ts,"
                    "window_end_ts,outcome,resolved_at,source) VALUES(?,?,?,?,?,?,?)",
                    (slug, asset, w_start, w_end, outcome.capitalize(), now_ms(),
                     "spot_backfill"))
                settled += 1
            rw.commit()
        finally:
            rw.close()
        return settled

    def restore_from_db(self, db_path: str) -> int:
        """Reconstruye el estado del portfolio (PnL, W/L, racha, Brier, recientes)
        replayando TODAS las apuestas liquidadas de la DB. El track record es
        continuo entre reinicios en vez de volver a $10k cada vez."""
        ro = _ro_conn(db_path)
        if ro is None:
            return 0
        try:
            all_bets = ro.execute(
                "SELECT side FROM bets").fetchall()
            settled = ro.execute(
                "SELECT slug, side, price, shares, stake, fair_p, outcome, pnl, ts_settle "
                "FROM bets WHERE status='settled' ORDER BY ts_settle ASC").fetchall()
        finally:
            ro.close()

        self.n_bets = len(all_bets)
        self.up_bets = sum(1 for (s,) in all_bets if s == "up")
        self.down_bets = self.n_bets - self.up_bets

        # medianoche UTC de hoy en ms -> para separar el PnL del dia
        now = datetime.now(timezone.utc)
        midnight_ms = int(datetime.combine(
            now.date(), datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)

        self.realized = 0.0
        self.n_settled = 0
        self.wins = 0
        self.brier_sum = 0.0
        self.streak = 0
        self.day_start_realized = 0.0
        self.peak_equity = self.start
        self.max_dd_abs = 0.0
        self.max_dd_frac = 0.0
        self.recent.clear()
        for slug, side, price, shares, stake, fair_p, outcome, pnl, ts_settle in settled:
            win = (side == outcome)
            self.realized += (pnl or 0.0)
            self._update_drawdown()
            self.n_settled += 1
            self.wins += 1 if win else 0
            if fair_p is not None:
                self.brier_sum += (fair_p - (1.0 if win else 0.0)) ** 2
            self.streak = (self.streak + 1) if (win and self.streak >= 0) else \
                          (self.streak - 1) if (not win and self.streak <= 0) else \
                          (1 if win else -1)
            if ts_settle and ts_settle < midnight_ms:
                self.day_start_realized = self.realized
            self.recent.appendleft({
                "slug": slug, "side": side, "outcome": outcome,
                "price": price, "stake": stake, "pnl": pnl, "win": win,
            })
        self.day = now.date()
        self.equity_hist.append(self.equity)
        return self.n_settled

    def snapshot_equity(self):
        if not self.equity_hist:
            self.equity_hist.append(self.equity)
        self.storage.put("equity", (now_ms(), self.equity, self.realized,
                                    self.open_exposure, self.n_open))

    def stats(self):
        return {
            "equity": self.equity, "cash": self.cash, "realized": self.realized,
            "open_exposure": self.open_exposure, "n_open": self.n_open,
            "n_bets": self.n_bets, "n_settled": self.n_settled,
            "wins": self.wins, "losses": self.losses, "streak": self.streak,
            "up_bets": self.up_bets, "down_bets": self.down_bets,
            "win_rate": self.win_rate, "brier": self.brier, "roi": self.roi,
            "day_pnl": self.day_pnl, "halted": self.daily_limit_hit(),
            "soft_warn": self.soft_drawdown_hit(),
            "max_dd_abs": self.max_dd_abs, "max_dd_frac": self.max_dd_frac,
            "recent_win_rate": self.recent_win_rate,
        }
