"""
WebSocket del market channel de Polymarket.

Mantiene el order book local por token en LiveState (persiste entre reconexiones)
a partir de 'book' (snapshot) y 'price_change' (deltas), escribe top-of-book
deduplicado a SQLite, y marca el timestamp de update para los kill-switches.

El set de tokens activos rota cada pocos minutos -> reconecta con la lista nueva.
"""
import asyncio
import json

import websockets

import config
from src.storage import Storage
from src.state import LiveState, now_ms


def _extract(msg, *keys):
    for k in keys:
        if k in msg and msg[k] is not None:
            return msg[k]
    return None


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def run_polymarket_ws(storage: Storage, state: LiveState, stop: asyncio.Event, probe=False):
    probe_seen = set()
    capture_raw = probe or config.CAPTURE_RAW
    while not stop.is_set():
        tokens = state.token_list()
        if not tokens:
            await asyncio.sleep(2)
            continue
        current = frozenset(tokens)
        try:
            async with websockets.connect(config.PM_WS_URL, ping_interval=15, ping_timeout=10) as ws:
                await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
                if not config.QUIET:
                    print(f"[polymarket] suscrito a {len(tokens)} tokens")
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=config.PM_RECONNECT_CHECK_SEC)
                    except asyncio.TimeoutError:
                        if state.token_set() != current:
                            break
                        continue
                    _handle(raw, storage, state, probe, probe_seen, capture_raw)
                    if state.token_set() != current:
                        break
        except Exception as e:  # noqa: BLE001
            if stop.is_set():
                break
            if not config.QUIET:
                print(f"[polymarket] reconectando tras error: {e}")
            await asyncio.sleep(2)


def _snapshot(storage, state, token, etype, bb=None, ba=None, ts=None):
    meta = state.token_meta.get(token)
    if not meta:
        return
    slug, asset, side = meta
    ob = state.book_of(token)
    cb, ca, _, _, _ = ob.top()
    bbf = _to_float(bb) if bb not in (None, "") else cb
    baf = _to_float(ba) if ba not in (None, "") else ca
    bsz = ob.bids.get(bbf) if bbf is not None else None
    asz = ob.asks.get(baf) if baf is not None else None
    mid = (bbf + baf) / 2 if (bbf is not None and baf is not None) else None
    ts = ts or now_ms()
    state.book_ts[token] = ts
    top = (bbf, baf, bsz, asz)
    if top == ob.last_written:
        return
    ob.last_written = top
    storage.put("book", (ts, slug, asset, token, side, bbf, baf, bsz, asz, mid, etype))


def _handle(raw, storage, state, probe, probe_seen, capture_raw=False):
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return
    events = data if isinstance(data, list) else [data]
    for msg in events:
        if not isinstance(msg, dict):
            continue
        etype = _extract(msg, "event_type", "type") or "unknown"

        if probe and etype not in probe_seen:
            probe_seen.add(etype)
            print(f"[probe] event_type={etype} keys={list(msg.keys())}")

        if etype == "price_change":
            if capture_raw:
                storage.put("raw", (now_ms(), "multi", etype, raw[:8000]))
            changes = _extract(msg, "price_changes", "changes") or []
            affected = {}
            for c in changes:
                tok = str(c.get("asset_id"))
                state.book_of(tok).apply(c.get("price"), c.get("size"), c.get("side", "BUY"))
                affected[tok] = (c.get("best_bid"), c.get("best_ask"))
            ts = now_ms()
            for tok, (bb, ba) in affected.items():
                _snapshot(storage, state, tok, etype, bb, ba, ts)
            continue

        token = str(_extract(msg, "asset_id", "token_id"))
        if capture_raw:
            storage.put("raw", (now_ms(), token, etype, raw[:8000]))
        if etype == "book":
            bids = _extract(msg, "bids", "buys") or []
            asks = _extract(msg, "asks", "sells") or []
            state.book_of(token).reset(bids, asks)
            _snapshot(storage, state, token, etype)
        # last_trade_price / tick_size_change -> ignorados (no afectan el top-of-book)
