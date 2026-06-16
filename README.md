# Mibot — Quant bot para Polymarket (mercados 5/15 min)

Bot cuantitativo enfocado en los mercados **"Up or Down" de 5 y 15 minutos** de
Polymarket (BTC/ETH/SOL/XRP), que resuelven contra el candle de Binance. El
objetivo final es un track record de **shadow training** verificable (Brier +
win rate, con mejora demostrable out-of-sample) sobre el cual abrir un vault.

## Principio rector

> El ML no *crea* el edge, lo *explota*. Primero hay que probar que existe un edge
> medible; recién después se construye ejecución y autonomía encima.

Por eso el proyecto avanza por **fases con gate**: no se pasa de fase hasta validar.

| Fase | Qué construye | Gate para avanzar |
|------|---------------|-------------------|
| **0. Edge discovery** *(actual)* | Colector de order book de Polymarket + spot de Binance, timestampeado a SQLite. Análisis offline del lag spot↔implícito. | Edge positivo y medible out-of-sample, después de fees+spread |
| 1. Ejecución determinista | Señal sub-segundo (sin LLM), `py-clob-client`, barandas de riesgo duras | Paper trading con latencia real, P&L > costos |
| 2. Autonomía controlada | Sizing con tope, daily loss limit, kill-switch, capital chico real | Sobrevive semanas sin tocar los límites |
| 3. Auto-mejora (acotada) | Recalibración online de modelo chico, gating por régimen, promoción gateada por walk-forward | Mejora sostenida OOS |
| 4. Vault | Estructura de cuenta gestionada (Polymarket no entra en vaults estándar) | Confirmación shadow ≈ vivo con capital propio |

### El edge candidato
**Lag de latencia spot ↔ probabilidad implícita.** El order book de Polymarket
reprecia más lento que el spot de Binance. Cuando el spot ya se movió pero el
implícito sigue stale, hay una apuesta contra un precio viejo — filtrada siempre
por spread + fees + slippage.

## Fase 0 — Colector (lo que hay ahora)

Captura, sin riesgo y sin API keys (datos públicos):

- **`pm_book`** — top-of-book de cada token Up/Down, en cada cambio.
- **`pm_raw`** — mensaje crudo del WS (seguro ante errores de parsing).
- **`spot_tick`** — mejor bid/ask de Binance por símbolo.
- **`markets`** / **`resolutions`** — metadata y outcome oficial de cada ventana.

### Setup

```powershell
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
```

### Correr

```powershell
# Validar formato de mensajes del WS (corrida corta)
venv\Scripts\python.exe collector.py --probe

# Captura continua (Ctrl+C para frenar)
venv\Scripts\python.exe collector.py

# Capturar N segundos y terminar
venv\Scripts\python.exe collector.py --duration 3600
```

Los datos quedan en `data/mibot.db` (SQLite).

## Bot de paper trading + TUI (`bot.py`)

Bot autónomo que apuesta en **paper** (capital ficticio $10k) sobre Bitcoin Up/Down,
con dashboard de terminal en vivo. Es el motor de *shadow training*: cada ventana
genera una predicción y, si hay edge, una apuesta; al cerrar se liquida y se actualizan
Brier / win rate / equity. El modelo se auto-calibra (un solo parámetro: la volatilidad).

```powershell
venv\Scripts\python.exe bot.py            # con TUI (Ctrl+C para frenar)
venv\Scripts\python.exe bot.py --no-tui   # sin TUI, logs a stdout (para dejar 24/7)
```

El dashboard es estilo Bloomberg: badges **EN VIVO / SHADOW / ANALIZANDO / APRENDIENDO**,
ticker de precios, panel de **escaneo en vivo** (señal ▲UP/▼DOWN por mercado), cuenta con
sparkline de equity, **performance + aprendizaje** (win rate, W-L, racha, Brier de apuestas
y de modelo, k de calibración), order book Up/Down, posiciones e **historial W/L**.

### Correr 24/7 (siempre entrenando)

```powershell
.\run-forever.ps1     # watchdog: corre headless y se reinicia solo si se cae
```

Dejalo en una máquina que quede prendida (tu PC sin suspender, o un VPS). Mirá los logs con
`Get-Content data\watchdog.log -Wait -Tail 20`. **Una sola instancia a la vez** — un lock
(`data/bot.lock`) impide que dos bots corran juntos y dupliquen trades (eso rompería el track record).

**Estrategia (sin LLM, determinista):**
- `fair_p = P(Up)` del modelo = Φ(m / (σ·√τ)), con m = log(spot/open), σ vol EWMA calibrada.
- `edge_up = fair_p − ask_up`, `edge_down = (1−fair_p) − ask_down`.
- Apuesta el lado con mejor edge si supera `MIN_EDGE` (4%) y no es absurdo (>25% = error de modelo).
- Sizing: fracción de Kelly (¼) topeada al 2% del bankroll.

**Barandas de riesgo (todas duras, en `config.py`):** máx 3 posiciones, una por ventana,
límite de pérdida diaria 8% (auto-stop), spread máximo, tiempo mínimo al cierre,
kill-switch por staleness de spot/book, guard de edge inflado.

**El dashboard muestra:** equity / P&L / ROI, win rate y Brier (apuestas y modelo),
volatilidad y k de calibración, mercado BTC en foco (spot vs open, fair vs implícito, edge),
order book Up/Down (5 niveles), posiciones abiertas, últimas liquidadas y log de decisiones.

> **Honestidad:** que el bot apueste y suba el Brier en paper **no prueba rentabilidad**.
> El Brier mide calibración; la rentabilidad es edge − costos. El fill se asume al mejor
> ask (optimista; falta modelar latencia/slippage). Liquida con el precio de Binance
> (mismo criterio que Polymarket) derivado de nuestro spot — cerca del borde puede diferir
> de la resolución oficial por un hilo. Antes de cualquier vault: fase chica en vivo que
> confirme shadow ≈ vivo.

## Estado

- [x] Colector Fase 0 (discovery + book + spot + resolución), formato WS validado
- [x] Bot paper + TUI: estrategia de edge, Kelly, barandas, calibración online
- [ ] Acumular track record y medir si el edge sobrevive a costos (gate de Fase 0)
- [ ] Modelar latencia/slippage realista en el fill
- [ ] Reconciliar resolución spot-derivada vs oficial de Polymarket
- [ ] Notebook de research sobre el dataset acumulado
