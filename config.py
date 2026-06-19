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
# Salud del discovery: si el ultimo escaneo exitoso fue hace mas de esto, el bot
# esta "cerebro-muerto" (vivo pero sin mercados nuevos: tipico bloqueo DNS del ISP
# a Polymarket). Se alarma en la TUI/visor y en el log headless. ~2.5 polls.
DISCOVERY_STALE_WARN_SEC = 90
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
# Modo agresivo: opera los 4 activos liquidos (BTC/ETH/SOL/XRP), no solo BTC.
# Mas mercados = mas muestras = el sistema genetico aprende mas rapido.
TRADE_ASSETS = ("btc", "eth", "sol", "xrp")
TRADE_INTERVALS = ("5m", "15m")

# --- Edge y sizing (agresivo: arriesga mas, junta mas datos) ---
MIN_EDGE = 0.02             # edge minimo para abrir (bajado: mas trades)
# Edge alto = MAXIMA discrepancia con el mercado = donde el modelo MAS se equivoca.
# El diagnostico (diagnose.py/stability.py) mostro que la banda edge>12% PIERDE en
# los 4 cuartos temporales (brecha wr-precio entre -2.6 y -9.6): es el blow-up de
# sigma*sqrt(tau)->0 cerca del cierre + seleccion adversa. Cortado de 0.25 a 0.12.
MAX_EDGE_TRUST = 0.12
KELLY_FRACTION = 0.40        # base de Kelly (la mutacion genetica lo sube/baja 0.10-0.50)
MAX_BET_PCT = 0.05          # tope por apuesta: 5% del bankroll (apuestas mas grandes)
MIN_BET = 5.0               # apuesta minima en $

# --- Capa de calibracion isotonica (PAV) ---
# El modelo de fair value esta S-comprimido: dice 65% cuando la realidad es 77%
# (medido en diagnose.py). El Calibrator aprende un mapa fair_p_crudo -> prob real
# sobre su buffer movil y la estrategia apuesta con la probabilidad CALIBRADA.
# El log de predictions queda crudo (para no contaminar diagnose/backtest/stability).
USE_CALIBRATION = True

# --- Score de confianza 4-factor (edge, liquidez, spread) ---
MIN_CONFIDENCE = 0.20        # score minimo para apostar (bajado: menos filtro, mas accion)

# --- Costo de ejecucion (clave para rentabilidad) ---
# Margen sobre el ask que cubre: spread/slippage real + la sobreconfianza del modelo
# en los casos de edge alto (seleccion adversa). El edge NETO (edge - EXEC_COST) debe
# superar MIN_EDGE para apostar; ademas el fill paga ask+EXEC_COST (no el ask optimista)
# y el sizing de Kelly usa ese precio. Asi no se apuesta cuando el costo se come la ventaja.
EXEC_COST = 0.01            # 1 centavo (spread BTC/ETH ~1c; ajustar con backtest.py)

# --- Comisiones de Polymarket (taker fee, categoria CRIPTO) ---
# Desde 2026-03-23 Polymarket cobra un fee POR TRADE al taker (Mibot siempre es taker).
# Formula: fee = shares * p * RATE * (p*(1-p))**EXP. Cripto = la categoria mas cara
# (pico 1.80% en p=0.50). Lo modela src/fees.py; lo aplican track_record.py y
# walkforward.py (evidencia y decision). En la logica VIVA se activa con APPLY_FEES_LIVE
# (default False durante el stress-test para no cambiar el dataset; True en produccion).
POLYMARKET_FEE_RATE = 0.072
POLYMARKET_FEE_EXPONENT = 1.0
APPLY_FEES_LIVE = False     # produccion: poner True (el bot exige edge neto del fee real)

# --- Barandas de riesgo (hard gates) ---
# Aun en modo agresivo dejamos un circuit-breaker: protege contra un BUG que dispare
# cientos de apuestas malas (eso ensuciaria el track record, no es "aprender").
MAX_CONCURRENT = 10         # posiciones abiertas simultaneas (4 activos x 2 intervalos)
# Filtro de correlacion: BTC/ETH/SOL/XRP se mueven juntos. Apostar el mismo lado en
# varios a la vez = una sola apuesta apalancada a "la cripto sube/baja" (riesgo de
# ruina). Tope de posiciones simultaneas en la MISMA direccion entre activos.
MAX_SAME_DIR_CONCURRENT = 2
DAILY_LOSS_LIMIT_PCT = 0.20 # circuit-breaker: si el dia pierde 20% -> stop hasta manana
SOFT_DRAWDOWN_PCT  = 0.12   # alerta suave antes del hard stop
MAX_SPREAD = 0.05           # no operar si el spread del lado a comprar supera esto
MIN_TIME_LEFT_SEC = 120     # no abrir con <120s: el backtest mostró que cerca del cierre
                            # el modelo da edge enorme pero acierta ~29% (σ√τ→0 lo infla, es basura)
ONE_BET_PER_MARKET = True   # una sola posicion por ventana

# --- Kill-switches por staleness de datos ---
STALE_SPOT_MS = 3000        # si el spot no actualiza en 3s -> no operar
STALE_BOOK_MS = 6000        # si el book no actualiza en 6s -> no operar

# --- Futures Intelligence (recoleccion de features lider, no se opera con esto aun) ---
# OI Delta / Taker Ratio / L/S Ratio de Binance Futures (API gratis). Se loguean en
# futures_tick para validar con diagnose.py si predicen el outcome. Si predicen,
# recien ahi entran a la estrategia. Buckets de 5m -> no hace falta pollear seguido.
COLLECT_FUTURES = True
FUTURES_POLL_SEC = 60

# --- Motor ---
EVAL_INTERVAL_SEC = 1.0     # cada cuanto evalua oportunidades
VOL_HALFLIFE_SEC = 120      # halflife de la EWMA de volatilidad realizada
SETTLE_GRACE_SEC = 3        # margen tras el cierre antes de liquidar paper

# ============================================================================
# PERSISTENCIA / CONTINUIDAD ENTRE REINICIOS
# ============================================================================
# El cerebro (buffer de calibracion, k, params geneticos, vol) se reconstruye
# desde la DB al arrancar. Asi nada de lo aprendido se pierde en un reinicio.
PERSIST_BRAIN = True         # reconstruir calibrador + warm-start de vol desde la DB
RECONCILE_ORPHANS = True     # liquidar apuestas 'open' colgadas usando spot historico
# Continuidad del equity/track-record: True = el PnL se acumula entre reinicios
# (track record real para el vault). False = arranca limpio en $10k cada vez
# (util durante el stress-test agresivo para evaluar la generacion actual).
# Durante el stress-test (hasta 23/6) = False. En produccion = True.
PERSIST_PORTFOLIO = False

QUIET = False               # bot.py lo pone True para no romper el TUI con prints
