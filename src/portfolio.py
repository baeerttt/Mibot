"""
Portfolio paper (shadow training). Capital ficticio, sin riesgo real.

Modelo de una apuesta binaria a precio `a` (el ask que pagamos):
  - comprar `shares` cuesta  shares*a
  - si el lado apostado gana -> cada share vale 1   -> pnl = shares*(1-a)
  - si pierde                -> vale 0               -> pnl = -shares*a (= -stake)

Trackea bankroll, P&L realizado, win rate, Brier de las apuestas, y el limite de
perdida diaria. Persiste cada apuesta y la curva de capital a SQLite.
"""
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

import config
from src.state import now_ms


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
        }
