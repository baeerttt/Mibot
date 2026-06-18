"""
Mibot — bot de paper trading con TUI para Bitcoin Up/Down de Polymarket.

Junta todo: colector (WS Polymarket + Binance) -> estado en vivo -> estrategia de
edge (lag spot↔implicito) -> portfolio paper $10k -> calibracion online del modelo
-> dashboard de terminal.

NOTA: liquida cada ventana con el precio de Binance (mismo criterio que usa
Polymarket para resolver), derivado de nuestro propio spot. Cerca del borde puede
diferir de la resolucion oficial por un hilo; aceptable para paper.

Uso:
  python bot.py                 # con TUI (Ctrl+C para frenar)
  python bot.py --no-tui        # sin TUI, logs a stdout (para 24/7)
  python bot.py --no-tui --duration 600
"""
import argparse
import asyncio
import os
import time

import aiohttp

import config
from src.storage import Storage, now_ms
from src.state import LiveState
from src.binance_ws import run_binance_ws
from src.polymarket_ws import run_polymarket_ws
from src.polymarket_api import discover_markets
from src.fair_value import VolEstimator
from src.portfolio import PaperPortfolio
from src.calibration import Calibrator
from src.strategy import StrategyEngine


LOCK_PATH = "data/bot.lock"
LOCK_STALE_SEC = 20


def acquire_lock() -> bool:
    """Evita dos instancias a la vez (duplicarian trades y romperian el track record)."""
    if os.path.exists(LOCK_PATH):
        age = time.time() - os.path.getmtime(LOCK_PATH)
        if age < LOCK_STALE_SEC:
            return False
    os.makedirs(os.path.dirname(LOCK_PATH) or ".", exist_ok=True)
    with open(LOCK_PATH, "w") as f:
        f.write(str(os.getpid()))
    return True


def release_lock():
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


async def lock_loop(stop):
    while not stop.is_set():
        try:
            os.utime(LOCK_PATH, None)   # heartbeat
        except OSError:
            pass
        await sleep_or_stop(stop, 5)


async def sleep_or_stop(stop, secs):
    try:
        await asyncio.wait_for(stop.wait(), timeout=secs)
    except asyncio.TimeoutError:
        pass


async def discovery_loop(session, storage, state, stop):
    """Solo agrega mercados nuevos; la expiracion la maneja settle_loop."""
    while not stop.is_set():
        try:
            for mk in await discover_markets(session):
                if mk.slug not in state.markets:
                    state.add(mk)
                    storage.put("market", mk.db_row())
        except Exception:  # noqa: BLE001
            pass
        await sleep_or_stop(stop, config.DISCOVERY_POLL_SEC)


async def price_capture_loop(state, stop):
    """Captura S0 (open) al cruzar window_start y S1 (close) al cruzar window_end."""
    while not stop.is_set():
        now = time.time()
        for slug, mk in list(state.markets.items()):
            if slug not in state.window_open and now >= mk.window_start_ts:
                if now <= mk.window_start_ts + 2:
                    mid = state.spot_mid(mk.symbol)
                    if mid:
                        state.window_open[slug] = mid
                else:
                    state.window_open[slug] = None      # llegamos tarde -> no operar
            if slug not in state.window_close and now >= mk.window_end_ts:
                mid = state.spot_mid(mk.symbol)
                if mid:
                    state.window_close[slug] = mid
        await sleep_or_stop(stop, 0.4)


async def settle_loop(storage, state, pf, cal, engine, stop):
    """Liquida ventanas cerradas (open vs close) y etiqueta el calibrador."""
    while not stop.is_set():
        now = time.time()
        for slug, mk in list(state.markets.items()):
            if now < mk.window_end_ts + config.SETTLE_GRACE_SEC:
                continue
            o = state.window_open.get(slug)
            c = state.window_close.get(slug) or state.spot_mid(mk.symbol)
            if o and c:
                outcome = "up" if c > o else "down"
                had = pf.has_position(slug)
                wins = pf.settle_market(slug, outcome)
                cal.label(slug, outcome == "up")
                for won in wins:
                    cal.record_bet_outcome(won)
                storage.put("resolution", (slug, mk.asset, mk.window_start_ts,
                                           mk.window_end_ts, outcome.capitalize(),
                                           now_ms(), "spot"))
                storage.execute(
                    "UPDATE predictions SET outcome=? WHERE slug=? AND outcome IS NULL",
                    (outcome, slug))
                if had:
                    engine.log.appendleft(
                        f"RESOLVE {mk.slug.split('-')[0]} {mk.interval} -> {outcome.upper()}")
            state.remove(slug)
        await sleep_or_stop(stop, 2)


async def strategy_loop(engine, stop):
    while not stop.is_set():
        try:
            engine.tick()
        except Exception as e:  # noqa: BLE001
            engine.log.appendleft(f"[err] {e}")
        await sleep_or_stop(stop, config.EVAL_INTERVAL_SEC)


async def vol_loop(state, vol, stop):
    while not stop.is_set():
        mid = state.spot_mid("BTCUSDT")
        if mid:
            vol.update(mid, now_ms())
        await sleep_or_stop(stop, 0.5)


async def recalib_loop(cal, stop):
    while not stop.is_set():
        await sleep_or_stop(stop, 30)
        try:
            cal.recalibrate()
        except Exception:  # noqa: BLE001
            pass


async def equity_loop(pf, stop):
    while not stop.is_set():
        await sleep_or_stop(stop, 15)
        pf.snapshot_equity()


async def tui_loop(state, pf, engine, cal, vol, stop):
    from rich.live import Live
    from src.tui import render
    with Live(render(state, pf, engine, cal, vol), screen=True, refresh_per_second=8) as live:
        while not stop.is_set():
            try:
                live.update(render(state, pf, engine, cal, vol))
            except Exception as e:  # noqa: BLE001
                engine.log.appendleft(f"[tui err] {e}")
            await sleep_or_stop(stop, 0.4)


async def headless_loop(pf, engine, stop):
    while not stop.is_set():
        await sleep_or_stop(stop, 15)
        s = pf.stats()
        wr = f"{s['win_rate']*100:.1f}%" if s["win_rate"] is not None else "-"
        print(f"[bot] equity=${s['equity']:.2f} pnl={s['realized']:+.2f} "
              f"bets={s['n_bets']} settled={s['n_settled']} wr={wr} open={s['n_open']}")


async def main(use_tui=True, duration=None):
    config.QUIET = use_tui
    if not acquire_lock():
        print("⛔ Ya hay un bot de Mibot corriendo (data/bot.lock activo).")
        print("   Corré una sola instancia: o el TUI, o el headless 24/7, no ambos.")
        return
    storage = Storage(config.DB_PATH, config.DB_COMMIT_EVERY, config.DB_COMMIT_MAX_SEC)
    await storage.start()
    state = LiveState()
    pf = PaperPortfolio(storage)
    cal = Calibrator()
    vol = VolEstimator(config.VOL_HALFLIFE_SEC)
    engine = StrategyEngine(state, pf, cal, vol, storage)
    stop = asyncio.Event()
    symbols = sorted(set(config.ASSETS.values()))

    async with aiohttp.ClientSession() as session:
        tasks = [
            asyncio.create_task(run_binance_ws(storage, state, symbols, stop)),
            asyncio.create_task(run_polymarket_ws(storage, state, stop)),
            asyncio.create_task(discovery_loop(session, storage, state, stop)),
            asyncio.create_task(price_capture_loop(state, stop)),
            asyncio.create_task(settle_loop(storage, state, pf, cal, engine, stop)),
            asyncio.create_task(strategy_loop(engine, stop)),
            asyncio.create_task(vol_loop(state, vol, stop)),
            asyncio.create_task(recalib_loop(cal, stop)),
            asyncio.create_task(equity_loop(pf, stop)),
            asyncio.create_task(lock_loop(stop)),
            asyncio.create_task(
                tui_loop(state, pf, engine, cal, vol, stop) if use_tui
                else headless_loop(pf, engine, stop)),
        ]
        try:
            if duration:
                await sleep_or_stop(stop, duration)
                stop.set()
            else:
                await asyncio.gather(*tasks)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            stop.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await storage.close()
            release_lock()
            s = pf.stats()
            wr = f"{s['win_rate']*100:.1f}%" if s["win_rate"] is not None else "-"
            print(f"[done] equity=${s['equity']:.2f} pnl={s['realized']:+.2f} "
                  f"bets={s['n_bets']} settled={s['n_settled']} wr={wr}")


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # evita UnicodeEncodeError en consolas Windows legacy
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-tui", action="store_true", help="sin dashboard, logs a stdout")
    ap.add_argument("--duration", type=int, default=None, help="segundos y termina")
    args = ap.parse_args()
    try:
        asyncio.run(main(use_tui=not args.no_tui, duration=args.duration))
    except KeyboardInterrupt:
        print("\n[interrumpido]")
