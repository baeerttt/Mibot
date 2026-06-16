"""
Colector Fase 0 — punto de entrada.

Corre 4 tareas en paralelo:
  1. discovery_loop  : descubre mercados 5/15m abiertos y mantiene el set activo.
  2. resolution_loop : registra el outcome oficial al cerrar cada ventana.
  3. binance_ws      : graba el spot de referencia.
  4. polymarket_ws   : graba el order book de cada mercado activo.

Uso:
  python collector.py --probe            # corrida corta, imprime formato de mensajes
  python collector.py                     # captura continua (Ctrl+C para frenar)
  python collector.py --duration 3600     # captura 1 hora y termina
"""
import argparse
import asyncio
import time

import aiohttp

import config
from src.storage import Storage, now_ms
from src.state import LiveState
from src.binance_ws import run_binance_ws
from src.polymarket_ws import run_polymarket_ws
from src.polymarket_api import discover_markets, fetch_resolution


async def sleep_or_stop(stop: asyncio.Event, secs: float):
    try:
        await asyncio.wait_for(stop.wait(), timeout=secs)
    except asyncio.TimeoutError:
        pass


async def discovery_loop(session, storage, state, stop, pending):
    while not stop.is_set():
        try:
            markets = await discover_markets(session)
            for mk in markets:
                if mk.slug not in state.markets:
                    state.add(mk)
                    storage.put("market", mk.db_row())
                    print(f"[discovery] + {mk.slug} ({mk.asset} {mk.interval}, liq {mk.liquidity:.0f})")
        except Exception as e:  # noqa: BLE001
            print(f"[discovery] error: {e}")
        # expirar mercados cuya ventana ya cerro -> pasan a resolucion
        now = time.time()
        for slug, mk in list(state.markets.items()):
            if now > mk.window_end_ts + 15:
                state.remove(slug)
                pending[slug] = {"mk": mk, "attempts": 0}
        await sleep_or_stop(stop, config.DISCOVERY_POLL_SEC)


async def resolution_loop(session, storage, state, stop, pending):
    while not stop.is_set():
        now = time.time()
        for slug, info in list(pending.items()):
            mk = info["mk"]
            if now < mk.window_end_ts + 20:
                continue  # darle tiempo a UMA a resolver
            try:
                outcome = await fetch_resolution(session, slug)
            except Exception:  # noqa: BLE001
                outcome = None
            info["attempts"] += 1
            if outcome:
                storage.put("resolution", (slug, mk.asset, mk.window_start_ts,
                                           mk.window_end_ts, outcome, now_ms(), "gamma"))
                print(f"[resolution] {slug} -> {outcome}")
                pending.pop(slug, None)
            elif info["attempts"] > 30:  # ~5 min de reintentos -> abandonar (label se deriva offline)
                storage.put("resolution", (slug, mk.asset, mk.window_start_ts,
                                           mk.window_end_ts, None, now_ms(), "timeout"))
                pending.pop(slug, None)
        await sleep_or_stop(stop, 10)


async def stats_loop(storage, state, stop):
    while not stop.is_set():
        await sleep_or_stop(stop, 30)
        c = storage.counts
        print(f"[stats] activos={len(state.markets)} | book={c['book']} raw={c['raw']} "
              f"spot={c['spot']} resol={c['resolution']}")


async def main(probe=False, duration=None):
    state = LiveState()
    pending: dict[str, dict] = {}
    storage = Storage(config.DB_PATH, config.DB_COMMIT_EVERY, config.DB_COMMIT_MAX_SEC)
    await storage.start()
    stop = asyncio.Event()
    symbols = sorted(set(config.ASSETS.values()))

    async with aiohttp.ClientSession() as session:
        tasks = [
            asyncio.create_task(discovery_loop(session, storage, state, stop, pending)),
            asyncio.create_task(resolution_loop(session, storage, state, stop, pending)),
            asyncio.create_task(run_binance_ws(storage, state, symbols, stop)),
            asyncio.create_task(run_polymarket_ws(storage, state, stop, probe)),
            asyncio.create_task(stats_loop(storage, state, stop)),
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
            print(f"[done] guardado en {config.DB_PATH} | counts={storage.counts}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="imprime formato de mensajes y termina rapido")
    ap.add_argument("--duration", type=int, default=None, help="segundos de captura, luego termina")
    args = ap.parse_args()
    dur = 60 if args.probe and not args.duration else args.duration
    try:
        asyncio.run(main(probe=args.probe, duration=dur))
    except KeyboardInterrupt:
        print("\n[interrumpido]")
