"""
Almacenamiento SQLite para el colector Fase 0.

Diseno: un unico consumidor (coroutine) drena una cola y escribe en batches.
Asi no hay concurrencia sobre la conexion sqlite y el event loop nunca se bloquea
en el commit (la escritura real corre en un thread via asyncio.to_thread).

Guardamos crudo (pm_raw) Y parseado (pm_book). El crudo es el seguro: si manana
descubrimos que parseamos mal un campo, el dato original sigue intacto.
"""
import asyncio
import sqlite3
import time
import os

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    slug            TEXT PRIMARY KEY,
    condition_id    TEXT,
    asset           TEXT,
    interval        TEXT,
    window_start_ts INTEGER,
    window_end_ts   INTEGER,
    up_token_id     TEXT,
    down_token_id   TEXT,
    liquidity       REAL,
    discovered_at   INTEGER
);

CREATE TABLE IF NOT EXISTS pm_book (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER,          -- epoch ms (reloj local del colector)
    slug      TEXT,
    asset     TEXT,
    token_id  TEXT,
    side      TEXT,             -- 'up' | 'down'
    best_bid  REAL,
    best_ask  REAL,
    bid_size  REAL,
    ask_size  REAL,
    mid       REAL,
    event_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_book_slug_ts ON pm_book(slug, ts);
CREATE INDEX IF NOT EXISTS idx_book_ts ON pm_book(ts);

CREATE TABLE IF NOT EXISTS pm_raw (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER,
    token_id   TEXT,
    event_type TEXT,
    payload    TEXT
);

CREATE TABLE IF NOT EXISTS spot_tick (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     INTEGER,
    symbol TEXT,
    bid    REAL,
    ask    REAL,
    mid    REAL
);
CREATE INDEX IF NOT EXISTS idx_spot_sym_ts ON spot_tick(symbol, ts);

CREATE TABLE IF NOT EXISTS resolutions (
    slug            TEXT PRIMARY KEY,
    asset           TEXT,
    window_start_ts INTEGER,
    window_end_ts   INTEGER,
    outcome         TEXT,        -- 'Up' | 'Down' | NULL si aun no se sabe
    resolved_at     INTEGER,
    source          TEXT
);

-- Apuestas paper (shadow training)
CREATE TABLE IF NOT EXISTS bets (
    bet_uid   TEXT PRIMARY KEY,
    ts_open   INTEGER,
    slug      TEXT,
    asset     TEXT,
    interval  TEXT,
    side      TEXT,             -- 'up' | 'down'
    price     REAL,             -- ask pagado al abrir
    shares    REAL,
    stake     REAL,
    fair_p    REAL,             -- prob estimada del lado apostado al abrir
    edge      REAL,
    status    TEXT,             -- 'open' | 'settled'
    ts_settle INTEGER,
    outcome   TEXT,             -- 'up' | 'down'
    pnl       REAL
);
CREATE INDEX IF NOT EXISTS idx_bets_status ON bets(status);

-- Curva de capital
CREATE TABLE IF NOT EXISTS equity (
    ts            INTEGER,
    bankroll      REAL,
    realized_pnl  REAL,
    open_exposure REAL,
    n_open        INTEGER
);

-- Predicciones periodicas (para Brier/calibracion, incluso sin apostar)
CREATE TABLE IF NOT EXISTS predictions (
    ts          INTEGER,
    slug        TEXT,
    asset       TEXT,
    fair_p      REAL,
    implied_up  REAL,
    implied_down REAL,
    edge_up     REAL,
    edge_down   REAL,
    spot_open   REAL,
    spot_now    REAL,
    tau         REAL,
    sigma       REAL,
    outcome     TEXT          -- se completa al cerrar (NULL hasta entonces)
);
CREATE INDEX IF NOT EXISTS idx_pred_slug ON predictions(slug);
CREATE INDEX IF NOT EXISTS idx_pred_ts ON predictions(ts);
"""

_INSERTS = {
    "book": (
        "INSERT INTO pm_book(ts,slug,asset,token_id,side,best_bid,best_ask,"
        "bid_size,ask_size,mid,event_type) VALUES(?,?,?,?,?,?,?,?,?,?,?)"
    ),
    "raw": "INSERT INTO pm_raw(ts,token_id,event_type,payload) VALUES(?,?,?,?)",
    "spot": "INSERT INTO spot_tick(ts,symbol,bid,ask,mid) VALUES(?,?,?,?,?)",
    "market": (
        "INSERT OR REPLACE INTO markets(slug,condition_id,asset,interval,"
        "window_start_ts,window_end_ts,up_token_id,down_token_id,liquidity,"
        "discovered_at) VALUES(?,?,?,?,?,?,?,?,?,?)"
    ),
    "resolution": (
        "INSERT OR REPLACE INTO resolutions(slug,asset,window_start_ts,"
        "window_end_ts,outcome,resolved_at,source) VALUES(?,?,?,?,?,?,?)"
    ),
    "bet": (
        "INSERT OR REPLACE INTO bets(bet_uid,ts_open,slug,asset,interval,side,"
        "price,shares,stake,fair_p,edge,status,ts_settle,outcome,pnl) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    ),
    "equity": (
        "INSERT INTO equity(ts,bankroll,realized_pnl,open_exposure,n_open) "
        "VALUES(?,?,?,?,?)"
    ),
    "prediction": (
        "INSERT INTO predictions(ts,slug,asset,fair_p,implied_up,implied_down,"
        "edge_up,edge_down,spot_open,spot_now,tau,sigma,outcome) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
    ),
}


def now_ms() -> int:
    return int(time.time() * 1000)


class Storage:
    def __init__(self, path, commit_every=50, commit_max_sec=2.0):
        self.path = path
        self.commit_every = commit_every
        self.commit_max_sec = commit_max_sec
        self.queue: asyncio.Queue = asyncio.Queue()
        self.conn: sqlite3.Connection | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self.counts = {k: 0 for k in _INSERTS}
        self.counts["sql"] = 0

    async def start(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.conn = await asyncio.to_thread(self._open)
        self._running = True
        self._task = asyncio.create_task(self._consumer())

    def _open(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        conn.commit()
        return conn

    def put(self, kind: str, row: tuple):
        """No-bloqueante: encola una fila para escribir."""
        self.queue.put_nowait((kind, row))

    def execute(self, sql: str, params: tuple):
        """Encola un statement arbitrario (ej UPDATE para liquidar apuestas)."""
        self.queue.put_nowait(("sql", (sql, params)))

    async def _consumer(self):
        batch: dict[str, list] = {k: [] for k in _INSERTS}
        batch["sql"] = []
        pending = 0
        last_flush = time.monotonic()
        while self._running or not self.queue.empty():
            timeout = self.commit_max_sec
            try:
                kind, row = await asyncio.wait_for(self.queue.get(), timeout)
                batch[kind].append(row)
                pending += 1
            except asyncio.TimeoutError:
                pass
            if pending >= self.commit_every or (
                pending and time.monotonic() - last_flush >= self.commit_max_sec
            ):
                await self._flush(batch)
                pending = 0
                last_flush = time.monotonic()
        await self._flush(batch)

    async def _flush(self, batch):
        if not any(batch.values()):
            return
        await asyncio.to_thread(self._write, batch)

    def _write(self, batch):
        for kind, rows in batch.items():
            if not rows:
                continue
            if kind == "sql":
                for sql, params in rows:
                    self.conn.execute(sql, params)
            else:
                self.conn.executemany(_INSERTS[kind], rows)
            self.counts[kind] += len(rows)
            rows.clear()
        self.conn.commit()

    async def close(self):
        self._running = False
        if self._task:
            await self._task
        if self.conn:
            await asyncio.to_thread(self.conn.close)
