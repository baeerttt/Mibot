"""
Stream de spot de Binance (bookTicker) — la referencia de resolucion.

bookTicker entrega el mejor bid/ask en tiempo real por simbolo. El mid es el
proxy del precio que usa Polymarket para resolver (close vs open del candle).
Reconecta solo ante cualquier corte.
"""
import asyncio
import json

import websockets

import config
from src.storage import Storage, now_ms
from src.state import LiveState


async def run_binance_ws(storage: Storage, state: LiveState, symbols: list[str], stop: asyncio.Event):
    streams = "/".join(f"{s.lower()}@bookTicker" for s in symbols)
    url = config.BINANCE_WS_BASE + streams
    backoff = 1
    while not stop.is_set():
        try:
            async with websockets.connect(url, ping_interval=15, ping_timeout=10) as ws:
                backoff = 1
                if not config.QUIET:
                    print(f"[binance] conectado: {len(symbols)} simbolos")
                while not stop.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    msg = json.loads(raw)
                    d = msg.get("data", msg)
                    sym = d.get("s")
                    if not sym:
                        continue
                    bid = float(d["b"]); ask = float(d["a"])
                    ts = now_ms()
                    storage.put("spot", (ts, sym, bid, ask, (bid + ask) / 2))
                    state.update_spot(sym, bid, ask, ts)
        except asyncio.TimeoutError:
            continue
        except Exception as e:  # noqa: BLE001
            if stop.is_set():
                break
            if not config.QUIET:
                print(f"[binance] reconectando tras error: {e} (backoff {backoff}s)")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
