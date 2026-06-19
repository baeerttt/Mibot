"""
Futures Intelligence — indicadores LIDER de Binance Futures (API gratis, sin key).

El order flow de futuros suele anticipar al spot 1-5 min, justo la ventana de los
mercados Up/Down de 5/15 min. Recolectamos 3 senales por activo:

  - OI Delta     : cambio del open interest. Sube + precio sube -> tendencia con
                   conviccion; cae + precio sube -> short squeeze/liquidacion.
  - Taker Ratio  : volumen de takers compra/venta. > 1 = compra agresiva.
  - L/S Ratio    : cuentas long vs short (global). Extremos = contrarian.

NO se opera con esto todavia. Se LOGUEA como feature candidata para validar con
diagnose.py si predice el outcome. Si predice -> recien ahi entra a la estrategia.

Endpoints publicos (https://fapi.binance.com/futures/data/...), periodo 5m.
Resiliente: cualquier fallo (geo-block, corte) -> devuelve lo que pudo, sin romper.
"""
import aiohttp

FUT_BASE = "https://fapi.binance.com/futures/data"


async def _get(session, path, params):
    async with session.get(f"{FUT_BASE}/{path}", params=params, timeout=10) as r:
        if r.status != 200:
            return None
        return await r.json()


async def fetch_futures(session: aiohttp.ClientSession, symbol: str) -> dict:
    """Devuelve {oi, oi_delta, taker_ratio, ls_ratio, bucket_ts} (parcial si algo falla)."""
    out: dict = {}
    try:
        data = await _get(session, "openInterestHist",
                          {"symbol": symbol, "period": "5m", "limit": 2})
        if data and len(data) >= 2:
            oi_prev = float(data[0]["sumOpenInterest"])
            oi_now = float(data[1]["sumOpenInterest"])
            out["oi"] = oi_now
            out["oi_delta"] = (oi_now - oi_prev) / oi_prev if oi_prev else 0.0
            out["bucket_ts"] = int(data[1]["timestamp"])
    except Exception:  # noqa: BLE001
        pass
    try:
        data = await _get(session, "takerlongshortRatio",
                          {"symbol": symbol, "period": "5m", "limit": 1})
        if data:
            out["taker_ratio"] = float(data[0]["buySellRatio"])
    except Exception:  # noqa: BLE001
        pass
    try:
        data = await _get(session, "globalLongShortAccountRatio",
                          {"symbol": symbol, "period": "5m", "limit": 1})
        if data:
            out["ls_ratio"] = float(data[0]["longShortRatio"])
    except Exception:  # noqa: BLE001
        pass
    return out
