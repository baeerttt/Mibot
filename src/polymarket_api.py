"""
Descubrimiento de mercados via Gamma API (REST).

Encuentra los mercados 'Up or Down' de 5m/15m que estan abiertos AHORA y extrae
los token IDs de Up/Down para suscribirse al order book por WebSocket.
Tambien resuelve el outcome oficial una vez cerrado el mercado.
"""
import json
import re
import time
from dataclasses import dataclass

import aiohttp

import config

# btc-updown-5m-1781646600  ->  ('btc', '5m', '1781646600')
_SLUG_RE = re.compile(r"^([a-z0-9]+)-updown-(5m|15m)-(\d+)$")


@dataclass
class Market:
    slug: str
    asset: str          # clave de config.ASSETS (ej 'btc')
    symbol: str         # simbolo Binance (ej 'BTCUSDT')
    interval: str       # '5m' | '15m'
    window_start_ts: int
    window_end_ts: int
    up_token_id: str
    down_token_id: str
    liquidity: float
    condition_id: str

    def db_row(self):
        return (
            self.slug, self.condition_id, self.asset, self.interval,
            self.window_start_ts, self.window_end_ts, self.up_token_id,
            self.down_token_id, self.liquidity, int(time.time() * 1000),
        )


def _parse_market(ev: dict) -> Market | None:
    slug = ev.get("slug") or ""
    m = _SLUG_RE.match(slug)
    if not m:
        return None
    prefix, interval, start_ts = m.group(1), m.group(2), int(m.group(3))
    if prefix not in config.ASSETS or interval not in config.INTERVALS:
        return None

    mkt = (ev.get("markets") or [{}])[0]
    try:
        outcomes = json.loads(mkt.get("outcomes") or "[]")
        token_ids = json.loads(mkt.get("clobTokenIds") or "[]")
    except (json.JSONDecodeError, TypeError):
        return None
    if len(outcomes) != 2 or len(token_ids) != 2:
        return None

    # Mapear outcome -> token (no asumir orden ciegamente).
    tok = {o.lower(): t for o, t in zip(outcomes, token_ids)}
    if "up" not in tok or "down" not in tok:
        return None

    liq = float(mkt.get("liquidity") or ev.get("liquidity") or 0)
    if liq < config.MIN_LIQUIDITY:
        return None

    end_iso = ev.get("endDate")
    end_ts = _iso_to_epoch(end_iso) if end_iso else start_ts

    return Market(
        slug=slug,
        asset=prefix,
        symbol=config.ASSETS[prefix],
        interval=interval,
        window_start_ts=start_ts,
        window_end_ts=end_ts,
        up_token_id=str(tok["up"]),
        down_token_id=str(tok["down"]),
        liquidity=liq,
        condition_id=str(mkt.get("conditionId") or ""),
    )


def _iso_to_epoch(iso: str) -> int:
    # '2026-06-16T21:55:00Z' -> epoch segundos (UTC)
    from datetime import datetime
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


async def discover_markets(session: aiohttp.ClientSession) -> list[Market]:
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    params = {
        "closed": "false",
        "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_date_max": (now + timedelta(minutes=config.DISCOVERY_HORIZON_MIN)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "order": "endDate",
        "ascending": "true",
        "limit": "300",
    }
    url = f"{config.GAMMA_BASE}/events"
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
        events = await r.json()
    out = []
    for ev in events if isinstance(events, list) else []:
        mk = _parse_market(ev)
        if mk:
            out.append(mk)
    return out


async def fetch_resolution(session: aiohttp.ClientSession, slug: str) -> str | None:
    """Devuelve 'Up' / 'Down' si el mercado ya resolvio, si no None."""
    url = f"{config.GAMMA_BASE}/markets"
    async with session.get(url, params={"slug": slug}, timeout=aiohttp.ClientTimeout(total=15)) as r:
        rows = await r.json()
    if not rows:
        return None
    mkt = rows[0] if isinstance(rows, list) else rows
    if not mkt.get("closed"):
        return None
    try:
        outcomes = json.loads(mkt.get("outcomes") or "[]")
        prices = json.loads(mkt.get("outcomePrices") or "[]")
    except (json.JSONDecodeError, TypeError):
        return None
    for o, p in zip(outcomes, prices):
        if float(p) > 0.5:
            return o  # 'Up' o 'Down'
    return None
