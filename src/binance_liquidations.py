"""
Liquidaciones de Binance Futures — el combustible de los movimientos bruscos.

No hay endpoint REST publico para liquidaciones (el viejo allForceOrders se
discontinuo). Van por WebSocket: !forceOrder@arr empuja TODAS las liquidaciones
forzadas del mercado en un solo stream. Cada evento:
    side SELL = se liquido un LONG  (venta forzada)  -> presiona el precio ABAJO
    side BUY  = se liquido un SHORT (compra forzada)  -> presiona el precio ARRIBA

Acumulamos USD liquidado por (simbolo, bucket 5m) y direccion. Las cascadas de
liquidaciones mueven el spot dentro de la ventana de 5/15 min -> feature LIDER
candidata. No se opera con esto: se recolecta y se valida con diagnose_futures.py.

Diseno: AISLADO y resiliente. Un solo productor (el WS) y un solo consumidor
(futures_loop), ambos en el event loop -> sin locks. Si el WS se cae, el
acumulador devuelve ceros y el bot sigue operando sin enterarse.
"""
import asyncio
import json
import time

import websockets

import config

BUCKET_MS = 300_000
FSTREAM = "wss://fstream.binance.com/ws/!forceOrder@arr"


class LiquidationAccumulator:
    """USD liquidado por (symbol, bucket 5m) y direccion (buy/sell)."""

    def __init__(self, symbols):
        self.symbols = set(symbols)
        # symbol -> bucket_ts -> [buy_usd, sell_usd, count]
        self.data: dict[str, dict[int, list]] = {s: {} for s in self.symbols}

    def add(self, symbol: str, ts_ms: int, side: str, usd: float):
        if symbol not in self.data or not ts_ms:
            return
        b = (ts_ms // BUCKET_MS) * BUCKET_MS
        cell = self.data[symbol].setdefault(b, [0.0, 0.0, 0])
        if side == "BUY":      # short liquidado -> presion compradora (precio arriba)
            cell[0] += usd
        else:                  # SELL: long liquidado -> presion vendedora (precio abajo)
            cell[1] += usd
        cell[2] += 1

    def snapshot(self, symbol: str, bucket_ts: int):
        """(buy_usd, sell_usd, count) del bucket 5m que arranca en bucket_ts."""
        b = (bucket_ts // BUCKET_MS) * BUCKET_MS
        cell = self.data.get(symbol, {}).get(b)
        return (cell[0], cell[1], cell[2]) if cell else (0.0, 0.0, 0)

    def prune(self, keep_ms: int = 1_800_000):
        """Descarta buckets de mas de 30 min para no crecer sin limite."""
        cutoff = int(time.time() * 1000) - keep_ms
        for buckets in self.data.values():
            for b in [b for b in buckets if b < cutoff]:
                del buckets[b]


async def run_liquidation_ws(acc: LiquidationAccumulator, stop: asyncio.Event,
                             quiet: bool = False):
    """Escucha forceOrder y alimenta el acumulador. Reconecta solo ante cortes."""
    if not config.COLLECT_FUTURES:
        return
    backoff = 1
    while not stop.is_set():
        try:
            async with websockets.connect(FSTREAM, ping_interval=15, ping_timeout=10) as ws:
                backoff = 1
                if not quiet:
                    print("[liquidations] conectado a forceOrder")
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        continue   # 30s sin liquidaciones es normal; el ping mantiene viva la conexion
                    m = json.loads(raw)
                    o = m.get("o", {})
                    sym = o.get("s")
                    if sym not in acc.symbols:
                        continue
                    try:
                        usd = float(o["ap"]) * float(o["z"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    acc.add(sym, int(o.get("T") or m.get("E") or 0), o.get("S"), usd)
        except asyncio.CancelledError:
            break
        except Exception as e:  # noqa: BLE001
            if not quiet:
                print(f"[liquidations] reconectando: {e!r}")
            await asyncio.sleep(min(backoff, 30))
            backoff = min(backoff * 2, 30)
