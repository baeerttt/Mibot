"""
recalib_test.py — el test decisivo de Fase 0.

Pregunta: el modelo pierde porque esta MAL CALIBRADO (arreglable) o porque el
mercado de Polymarket es EFICIENTE (no hay edge que explotar)?

Metodo honesto (out-of-sample):
  1. Parte los datos por tiempo: 60% viejo (train) / 40% nuevo (test).
  2. En TRAIN aprende el mapa de calibracion empirico: cuando el modelo dijo X%,
     en la realidad UP paso Y%. (isotonic-ish, por bins.)
  3. En TEST aplica ese mapa: cal_p = recalibrar(fair_p), recalcula el edge contra
     el MISMO ask que cotizaba el mercado, y mide si AHORA hay una banda rentable.

Si tras recalibrar OOS aparece una banda con (win_rate - precio) > costo:
   -> el edge existe, era un problema de calibracion. SE ARREGLA.
Si ninguna banda supera el break-even ni recalibrada:
   -> el modelo recalibrado = el mercado. NO hay edge. Mercado eficiente.
"""
import sqlite3
import bisect
import config

conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
rows = conn.execute(
    "SELECT ts, fair_p, edge_up, edge_down, tau, outcome FROM predictions "
    "WHERE outcome IS NOT NULL AND fair_p IS NOT NULL "
    "AND edge_up IS NOT NULL AND edge_down IS NOT NULL "
    "ORDER BY ts ASC"
).fetchall()
conn.close()

n = len(rows)
split = int(n * 0.60)
train, test = rows[:split], rows[split:]
print(f"Predicciones: {n}  ·  train={len(train)}  test(OOS)={len(test)}")
print("=" * 78)

# ---- 1) mapa de calibracion empirico sobre TRAIN ----
# bins finos de fair_p -> frecuencia real de UP
NB = 20
bin_w = sum([0])  # noop
counts = [[0, 0] for _ in range(NB)]   # [n, ups]
for ts, fair_p, eu, ed, tau, outcome in train:
    b = min(NB - 1, int(fair_p * NB))
    counts[b][0] += 1
    counts[b][1] += 1 if outcome == "up" else 0

# centro de cada bin -> prob real (con suavizado: si bin vacio, usa el centro)
xs, ys = [], []
for i in range(NB):
    c, u = counts[i]
    center = (i + 0.5) / NB
    real = u / c if c >= 30 else center
    xs.append(center)
    ys.append(real)

# fuerza monotonia creciente (pool-adjacent-violators simplificado)
for i in range(1, NB):
    if ys[i] < ys[i - 1]:
        ys[i] = ys[i - 1]

def recalibrate(p):
    """interpola el p crudo al p real aprendido en train."""
    if p <= xs[0]:
        return ys[0]
    if p >= xs[-1]:
        return ys[-1]
    j = bisect.bisect_right(xs, p) - 1
    j = max(0, min(NB - 2, j))
    x0, x1 = xs[j], xs[j + 1]
    y0, y1 = ys[j], ys[j + 1]
    t = (p - x0) / (x1 - x0) if x1 > x0 else 0
    return y0 + t * (y1 - y0)

print("Mapa de calibracion aprendido (train):  modelo -> real")
for i in range(0, NB, 2):
    if counts[i][0] >= 30:
        print(f"  {xs[i]*100:5.1f}% -> {ys[i]*100:5.1f}%  (n={counts[i][0]})")

cost = config.EXEC_COST

def band_analysis(data, use_recalib, lo, hi, min_tau=120):
    nb = wb = 0
    psum = pnl = 0.0
    for ts, fair_p, eu, ed, tau, outcome in data:
        if tau < min_tau:
            continue
        ask_up = fair_p - eu
        ask_dn = (1 - fair_p) - ed
        p_up = recalibrate(fair_p) if use_recalib else fair_p
        e_up = p_up - ask_up
        e_dn = (1 - p_up) - ask_dn
        if e_up >= e_dn:
            side, p_side, edge, ask = "up", p_up, e_up, ask_up
        else:
            side, p_side, edge, ask = "down", 1 - p_up, e_dn, ask_dn
        net = edge - cost
        if not (lo <= net < hi):
            continue
        if edge > config.MAX_EDGE_TRUST:
            continue
        ask_eff = ask + cost
        if ask_eff <= 0.01 or ask_eff >= 0.99:
            continue
        win = (side == outcome)
        nb += 1
        wb += 1 if win else 0
        psum += ask_eff
        shares = 100 / ask_eff
        pnl += shares * (1 - ask_eff) if win else -100
    if nb == 0:
        return None
    return nb, wb / nb * 100, psum / nb * 100, pnl

def report(data, use_recalib, tag):
    print(f"\n{tag}")
    print("-" * 78)
    bands = [(0.02, 0.04), (0.04, 0.06), (0.06, 0.09), (0.09, 0.12), (0.12, 0.25)]
    any_pos = False
    for lo, hi in bands:
        r = band_analysis(data, use_recalib, lo, hi)
        if r is None:
            print(f"  edge {lo*100:4.0f}-{hi*100:4.0f}%:  sin candidatos")
            continue
        nb, wr, price, pnl = r
        brecha = wr - price
        mark = "  <<< RENTABLE" if brecha > 0.5 else ""
        if brecha > 0.5:
            any_pos = True
        print(f"  edge {lo*100:4.0f}-{hi*100:4.0f}%:  n={nb:5d}  wr={wr:5.1f}%  "
              f"precio={price:4.1f}c  brecha={brecha:+5.1f}  pnl=${pnl:+8.0f}{mark}")
    return any_pos

print("\n" + "=" * 78)
print("TEST OOS  (mapa aprendido en train, aplicado al 40% NUEVO)")
print("=" * 78)
base = report(test, False, ">>> SIN recalibrar (modelo actual) en OOS:")
reca = report(test, True, ">>> RECALIBRADO (modelo corregido) en OOS:")

print("\n" + "=" * 78)
print("VEREDICTO")
print("-" * 78)
if reca:
    print("  Recalibrar ABRE una banda rentable OOS -> el edge existe, era calibracion.")
    print("  Accion: meter la capa de recalibracion en el modelo y operar esa banda.")
elif base:
    print("  El modelo crudo ya tiene una banda rentable OOS (raro dado el diag).")
else:
    print("  NINGUNA banda supera el break-even, ni cruda ni recalibrada, OOS.")
    print("  => El modelo recalibrado converge al mercado. Polymarket es EFICIENTE")
    print("     en estos mercados. El lag spot<->implicito NO es edge tras costos.")
    print("  => Verdadero gate de Fase 0: este edge candidato esta MUERTO.")
    print("     Caminos reales: (a) otro edge, (b) proveer liquidez en vez de cruzar")
    print("     el spread, (c) usar el bot como motor de research/calibracion.")
