"""
watch.py — visor TUI del bot Mibot corriendo en modo 24/7.

Lee data/snapshot.json (el bot lo escribe cada segundo) y muestra el mismo
dashboard que el TUI integrado — sin adquirir el lock, sin estrategia, sin
ningún efecto sobre el trading.

Uso:
  venv\Scripts\python.exe watch.py    # Ctrl+C para salir
"""
import asyncio
import json
import os
import sys
import time
from collections import deque

from rich.live import Live
from rich.panel import Panel
from rich.text import Text

SNAP_PATH = "data/snapshot.json"
LOCK_PATH = "data/bot.lock"
STALE_SEC = 10     # si el snapshot tiene más de 10s, bot se considera caído


# ---------- proxy de estado (misma interface que los objetos reales) ----------

class _ObProxy:
    def __init__(self, data):
        self._bids = [tuple(b) for b in data.get("bids", [])]
        self._asks = [tuple(a) for a in data.get("asks", [])]

    def top(self):
        bid_p = self._bids[0][0] if self._bids else None
        ask_p = self._asks[0][0] if self._asks else None
        bid_s = self._bids[0][1] if self._bids else None
        ask_s = self._asks[0][1] if self._asks else None
        mid = (bid_p + ask_p) / 2 if (bid_p and ask_p) else None
        return bid_p, ask_p, bid_s, ask_s, mid

    def depth(self, n):
        return self._bids[:n], self._asks[:n]


class _SnapState:
    def __init__(self, snap):
        self._spot = snap.get("spot", {})
        self._spot_age = snap.get("spot_age_ms", {})
        self._health = snap.get("health", {})
        self.obooks = {
            token: _ObProxy(ob)
            for token, ob in snap.get("obooks", {}).items()
        }

    def spot_mid(self, sym):
        return self._spot.get(sym)

    def spot_age_ms(self, sym):
        return self._spot_age.get(sym)

    def discovery_stale_sec(self):
        return self._health.get("discovery_stale_sec")


class _SnapEngine:
    def __init__(self, snap):
        d = snap.get("engine", {})
        self.ticks = d.get("ticks", 0)
        self.last_tick_ts = d.get("last_tick_ts", 0.0)
        self.started = d.get("started", time.time())
        self.scanning = d.get("scanning", 0)
        self.last_evals = d.get("last_evals", {})
        self.log = deque(d.get("log", []), maxlen=40)


class _SnapCal:
    def __init__(self, snap):
        self._cs = snap.get("cal", {})
        self.min_edge = self._cs.get("min_edge", 0.02)
        self.kelly_fraction = self._cs.get("kelly_fraction", 0.40)

    def stats(self):
        return self._cs


class _PosProxy:
    def __init__(self, d):
        self.slug = d["slug"]
        self.side = d["side"]
        self.price = d["price"]
        self.stake = d["stake"]
        self.fair_p = d["fair_p"]
        self.edge = d["edge"]


class _SnapPf:
    def __init__(self, snap):
        pf = snap.get("pf", {})
        self._stats = {
            k: v for k, v in pf.items()
            if k not in ("equity_hist", "positions", "recent")
        }
        self.equity_hist = pf.get("equity_hist", [])
        self.positions = {
            str(i): _PosProxy(p) for i, p in enumerate(pf.get("positions", []))
        }
        self.recent = deque(pf.get("recent", []), maxlen=20)
        self.wins = pf.get("wins", 0)
        self.losses = pf.get("losses", 0)

    def stats(self):
        return self._stats


class _SnapVol:
    def __init__(self, d):
        self.sigma_per_sec = d.get("sigma_per_sec", 0.0)
        self._ready = d.get("ready", False)

    def ready(self):
        return self._ready


def _build_proxies(snap):
    return (
        _SnapState(snap),
        _SnapPf(snap),
        _SnapEngine(snap),
        _SnapCal(snap),
        {sym: _SnapVol(v) for sym, v in snap.get("vols", {}).items()},
    )


# ---------- helpers ----------

def _load_snap():
    try:
        with open(SNAP_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def _offline_panel(reason=""):
    msg = f"\n  Bot no está corriendo{': ' + reason if reason else ''}.\n\n" \
          f"  Iniciá:  .\\run-forever.ps1\n"
    return Panel(
        Text(msg, style="grey50 italic"),
        title="[bold orange1]M I B O T[/]  ·  VISOR  ·  SIN DATOS",
        border_style="red1",
    )


# ---------- main ----------

async def main():
    from src.tui import render

    snap = _load_snap()
    if snap is None:
        initial = _offline_panel("data/snapshot.json no existe")
    else:
        try:
            state, pf, engine, cal, vols = _build_proxies(snap)
            initial = render(state, pf, engine, cal, vols)
        except Exception:
            initial = _offline_panel()

    with Live(initial, screen=True, refresh_per_second=4) as live:
        while True:
            try:
                snap = _load_snap()
                if snap is None:
                    live.update(_offline_panel("snapshot no encontrado"))
                elif time.time() - snap.get("ts", 0) > STALE_SEC:
                    age = int(time.time() - snap.get("ts", 0))
                    live.update(_offline_panel(f"snapshot tiene {age}s de antigüedad"))
                else:
                    state, pf, engine, cal, vols = _build_proxies(snap)
                    live.update(render(state, pf, engine, cal, vols))
            except Exception:
                pass
            await asyncio.sleep(0.4)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[visor cerrado]")
