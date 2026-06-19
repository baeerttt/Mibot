"""
Estado compartido en memoria — la fuente de verdad en tiempo real.

Los WS (Polymarket + Binance) lo actualizan; la estrategia y el TUI lo leen.
Es single-threaded (asyncio), asi que no hacen falta locks.
"""
import time


def now_ms() -> int:
    return int(time.time() * 1000)


class OrderBook:
    """Libro local: precio -> size, para bids y asks. Persiste entre reconexiones."""
    __slots__ = ("bids", "asks", "last_written")

    def __init__(self):
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last_written = None

    def reset(self, bids, asks):
        self.bids = {float(b["price"]): float(b["size"]) for b in bids}
        self.asks = {float(a["price"]): float(a["size"]) for a in asks}

    def apply(self, price, size, side):
        book = self.bids if str(side).upper() in ("BUY", "BID") else self.asks
        try:
            price, size = float(price), float(size)
        except (TypeError, ValueError):
            return
        if size == 0:
            book.pop(price, None)
        else:
            book[price] = size

    def top(self):
        bb = max(self.bids) if self.bids else None
        ba = min(self.asks) if self.asks else None
        bsz = self.bids.get(bb) if bb is not None else None
        asz = self.asks.get(ba) if ba is not None else None
        mid = (bb + ba) / 2 if (bb is not None and ba is not None) else None
        return bb, ba, bsz, asz, mid

    def depth(self, n=5):
        bids = sorted(((p, s) for p, s in self.bids.items() if s > 0), reverse=True)[:n]
        asks = sorted((p, s) for p, s in self.asks.items() if s > 0)[:n]
        return bids, asks


class LiveState:
    def __init__(self):
        self.markets: dict = {}          # slug -> Market
        self.token_meta: dict = {}       # token -> (slug, asset, side)
        self.obooks: dict = {}           # token -> OrderBook
        self.book_ts: dict = {}          # token -> ultimo update (ms)
        self.spot: dict = {}             # symbol -> {bid, ask, mid, ts}
        self.window_open: dict = {}      # slug -> S_0 (float) | None si se perdio el open
        self.window_close: dict = {}     # slug -> S_1 (float) al cierre de la ventana
        # salud del discovery (Gamma API): detecta el fallo silencioso por el que el
        # bot sigue "vivo" (ticks + spot de Binance) pero no descubre mercados nuevos.
        self.last_discovery_ok: float = 0.0   # epoch seg del ultimo escaneo exitoso
        self.discovery_fails: int = 0         # fallos consecutivos del discovery
        self.last_discovery_err: str | None = None

    # --- gestion de mercados activos ---
    def token_list(self):
        return list(self.token_meta.keys())

    def token_set(self):
        return frozenset(self.token_meta.keys())

    def add(self, mk):
        self.markets[mk.slug] = mk
        self.token_meta[mk.up_token_id] = (mk.slug, mk.asset, "up")
        self.token_meta[mk.down_token_id] = (mk.slug, mk.asset, "down")

    def remove(self, slug):
        mk = self.markets.pop(slug, None)
        if mk:
            for tok in (mk.up_token_id, mk.down_token_id):
                self.token_meta.pop(tok, None)
                self.obooks.pop(tok, None)
                self.book_ts.pop(tok, None)
        self.window_open.pop(slug, None)
        self.window_close.pop(slug, None)
        return mk

    # --- updates desde los WS ---
    def update_spot(self, symbol, bid, ask, ts):
        self.spot[symbol] = {"bid": bid, "ask": ask, "mid": (bid + ask) / 2, "ts": ts}

    def book_of(self, token) -> OrderBook:
        ob = self.obooks.get(token)
        if ob is None:
            ob = self.obooks[token] = OrderBook()
        return ob

    # --- lectura ---
    def spot_mid(self, symbol):
        s = self.spot.get(symbol)
        return s["mid"] if s else None

    def spot_age_ms(self, symbol):
        s = self.spot.get(symbol)
        return (now_ms() - s["ts"]) if s else None

    def book_age_ms(self, token):
        ts = self.book_ts.get(token)
        return (now_ms() - ts) if ts else None

    def discovery_stale_sec(self):
        """Segundos desde el ultimo discovery exitoso (None si nunca corrio aun)."""
        if not self.last_discovery_ok:
            return None
        return time.time() - self.last_discovery_ok
