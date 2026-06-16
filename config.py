"""
Configuracion central de Mibot — colector de datos Fase 0 (Polymarket 5/15 min).

Sin secretos aca: la Fase 0 solo usa datos publicos (Gamma API + Binance spot).
Las API keys de ejecucion entran recien en la Fase 1.
"""

# --- Endpoints publicos ---
GAMMA_BASE = "https://gamma-api.polymarket.com"
PM_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
# Stream combinado de Binance spot (bookTicker = mejor bid/ask en tiempo real)
BINANCE_WS_BASE = "wss://stream.binance.com:9443/stream?streams="

# --- Activos a seguir ---
# Mapeo: prefijo de slug de Polymarket -> simbolo de Binance.
# Solo los liquidos y de spread fino. Agregar mas es cambiar este dict.
ASSETS = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
}

# Intervalos de mercado a capturar (aparecen en el slug: ...-updown-5m-...).
INTERVALS = ("5m", "15m")

# --- Parametros del colector ---
DISCOVERY_POLL_SEC = 30      # cada cuanto re-escanea Gamma por mercados nuevos
DISCOVERY_HORIZON_MIN = 60   # ventana hacia adelante para buscar mercados que cierran
MIN_LIQUIDITY = 500.0        # filtra mercados sin liquidez util
PM_RECONNECT_CHECK_SEC = 10  # cada cuanto el WS revisa si cambio el set de tokens

# --- Almacenamiento ---
DB_PATH = "data/mibot.db"
DB_COMMIT_EVERY = 50         # batch de filas antes de commit (rendimiento)
DB_COMMIT_MAX_SEC = 2.0      # o commit forzado cada N segundos
CAPTURE_RAW = False          # guardar mensajes crudos: True solo para depurar (cientos de GB/dia en 24/7)

# ============================================================================
# TRADING (PAPER / shadow training) — bankroll ficticio, sin riesgo real
# ============================================================================
PAPER_BANKROLL = 10_000.0    # capital ficticio inicial
TRADE_ASSETS = ("btc",)      # por ahora SOLO Bitcoin Up/Down
TRADE_INTERVALS = ("5m", "15m")

# --- Edge y sizing ---
MIN_EDGE = 0.04              # edge minimo (fair_p - ask) para abrir una apuesta
MAX_EDGE_TRUST = 0.25        # edges mayores = probable error de modelo cerca del cierre -> ignorar
KELLY_FRACTION = 0.25        # quarter-Kelly (conservador)
MAX_BET_PCT = 0.02          # tope por apuesta: 2% del bankroll
MIN_BET = 5.0               # apuesta minima en $

# --- Barandas de riesgo (hard gates) ---
MAX_CONCURRENT = 3          # posiciones abiertas simultaneas
DAILY_LOSS_LIMIT_PCT = 0.08 # si el dia pierde 8% del bankroll -> stop hasta manana
MAX_SPREAD = 0.05           # no operar si el spread del lado a comprar supera esto
MIN_TIME_LEFT_SEC = 30      # no abrir con menos de 30s para el cierre
ONE_BET_PER_MARKET = True   # una sola posicion por ventana

# --- Kill-switches por staleness de datos ---
STALE_SPOT_MS = 3000        # si el spot no actualiza en 3s -> no operar
STALE_BOOK_MS = 6000        # si el book no actualiza en 6s -> no operar

# --- Motor ---
EVAL_INTERVAL_SEC = 1.0     # cada cuanto evalua oportunidades
VOL_HALFLIFE_SEC = 120      # halflife de la EWMA de volatilidad realizada
SETTLE_GRACE_SEC = 3        # margen tras el cierre antes de liquidar paper

QUIET = False               # bot.py lo pone True para no romper el TUI con prints
