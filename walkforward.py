"""
walkforward.py — Purged Walk-Forward CV (Lopez de Prado), version honesta.

stability.py parte la historia en cuartos pero NO entrena nada: solo mira win rate.
Esto es el test de verdad para decidir si hay edge: simula que en cada momento
SOLO conocemos el pasado, calibramos con el pasado, elegimos la banda con el pasado,
y apostamos en el futuro inmediato. Si gana fold tras fold -> edge real y operable.

Mecanica (anchored / ventana expansiva):
  - fold i: train = [inicio .. corte_i],  test = [corte_i .. corte_{i+1}]
  - PURGE: se descartan filas de train cuyo outcome resuelve DENTRO del test
    (cada prediccion resuelve en ts+tau, tau<=900s) -> sin fuga de informacion.
  - EMBARGO: gap extra tras el corte para no pegar train y test.
  - En train: se ajusta la calibracion (PAV) y se ELIGE la banda de edge con mejor
    brecha (sin mirar el test). En test: se aplica todo y se mide OOS.

Veredicto:
  - brecha (wr - precio) > 0 en la MAYORIA de los folds -> edge robusto, implementar.
  - signo que salta entre folds -> no-estacionario, no hay edge confiable.

Uso:  venv\\Scripts\\python.exe walkforward.py
"""
import sqlite3
import bisect
import config
from src.fair_value import prob_up_m  # no usado directo, pero marca la dependencia conceptual

DB = config.DB_PATH
PURGE_SEC = 900       # tau maximo: una prediccion de train puede resolver hasta 900s despues
EMBARGO_SEC = 300     # gap extra tras el corte
N_FOLDS = 6
COST = config.EXEC_COST
CAND_BANDS = [(0.03, 0.06), (0.05, 0.09), (0.05, 0.12), (0.06, 0.12), (0.09, 0.12)]


def load():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT ts, fair_p, edge_up, edge_down, tau, outcome FROM predictions "
        "WHERE outcome IS NOT NULL AND fair_p IS NOT NULL "
        "AND edge_up IS NOT NULL AND edge_down IS NOT NULL "
        "ORDER BY ts ASC").fetchall()
    conn.close()
    return rows


def pav(pairs):
    blocks = []
    for x, y in pairs:
        blocks.append([1.0, y, x, x])
        while len(blocks) >= 2 and blocks[-2][1]/blocks[-2][0] >= blocks[-1][1]/blocks[-1][0]:
            w2, s2, a2, b2 = blocks.pop()
            w1, s1, a1, b1 = blocks.pop()
            blocks.append([w1+w2, s1+s2, a1, b2])
    xs = [(a+b)/2 for (w, s, a, b) in blocks]
    ys = [s/w for (w, s, a, b) in blocks]
    return xs, ys


def fit_calib(train):
    pts = []
    for ts, fair_p, eu, ed, tau, outcome in train:
        pts.append((fair_p, 1.0 if outcome == "up" else 0.0))
    if len(pts) < 200:
        return None, None
    pts.sort()
    return pav(pts)


def calibrate(p, xs, ys):
    if not xs:
        return p
    if p <= xs[0]:
        return ys[0]
    if p >= xs[-1]:
        return ys[-1]
    j = max(0, min(len(xs)-2, bisect.bisect_right(xs, p) - 1))
    x0, x1 = xs[j], xs[j+1]
    t = (p - x0) / (x1 - x0) if x1 > x0 else 0.0
    return ys[j] + t * (ys[j+1] - ys[j])


def eval_band(data, xs, ys, lo, hi, use_calib, min_tau=120):
    n = w = 0
    psum = pnl = 0.0
    for ts, fair_p, eu, ed, tau, outcome in data:
        if tau < min_tau:
            continue
        ask_up = fair_p - eu
        ask_dn = (1 - fair_p) - ed
        p = calibrate(fair_p, xs, ys) if use_calib else fair_p
        e_up, e_dn = p - ask_up, (1 - p) - ask_dn
        if e_up >= e_dn:
            side, p_side, edge, ask = "up", p, e_up, ask_up
        else:
            side, p_side, edge, ask = "down", 1 - p, e_dn, ask_dn
        net = edge - COST
        if not (lo <= net < hi) or edge > config.MAX_EDGE_TRUST:
            continue
        ask_eff = ask + COST
        if ask_eff <= 0.01 or ask_eff >= 0.99:
            continue
        win = side == outcome
        n += 1; w += 1 if win else 0; psum += ask_eff
        shares = 100 / ask_eff
        pnl += shares * (1 - ask_eff) if win else -100
    if n == 0:
        return None
    return n, w/n*100, psum/n*100, pnl


def main():
    rows = load()
    if not rows:
        print("Sin datos.")
        return
    t0, t1 = rows[0][0], rows[-1][0]
    span_h = (t1 - t0) / 1000 / 3600
    print(f"Predicciones: {len(rows)}  ·  {span_h:.1f}h  ·  folds={N_FOLDS}  "
          f"purge={PURGE_SEC}s embargo={EMBARGO_SEC}s  ·  USE_CALIBRATION test")
    print("=" * 90)
    if span_h < 48:
        print("⚠  Con < 2 dias de datos esto sigue siendo ruido. El veredicto firme es con")
        print("   los 5 dias (23/6). Por ahora mide la METODOLOGIA, no saques conclusiones.\n")

    cuts = [t0 + (t1 - t0) * (i + 1) / (N_FOLDS + 1) for i in range(N_FOLDS)]
    cuts.append(t1 + 1)

    pos_raw = pos_cal = 0
    for i in range(N_FOLDS):
        cut = cuts[i]
        test_end = cuts[i + 1]
        purge_cut = cut - PURGE_SEC * 1000
        train = [r for r in rows if r[0] <= purge_cut]
        test = [r for r in rows if (cut + EMBARGO_SEC * 1000) <= r[0] < test_end]
        if len(train) < 500 or len(test) < 100:
            print(f"fold {i+1}: muestra insuficiente (train={len(train)} test={len(test)})")
            continue
        xs, ys = fit_calib(train)

        # elegir la banda con MEJOR brecha en TRAIN (sin mirar test) — para calib y crudo
        def best_band(use_calib):
            best = None
            for lo, hi in CAND_BANDS:
                r = eval_band(train, xs, ys, lo, hi, use_calib)
                if r and r[0] >= 100:
                    brecha = r[1] - r[2]
                    if best is None or brecha > best[1]:
                        best = ((lo, hi), brecha)
            return best[0] if best else (0.05, 0.12)

        band_raw = best_band(False)
        band_cal = best_band(True)
        r_raw = eval_band(test, xs, ys, band_raw[0], band_raw[1], False)
        r_cal = eval_band(test, xs, ys, band_cal[0], band_cal[1], True)

        def fmt(r, band):
            if not r:
                return "sin apuestas"
            n, wr, price, pnl = r
            brecha = wr - price
            return (f"banda{band[0]*100:.0f}-{band[1]*100:.0f}  n={n:4d}  wr={wr:5.1f}%  "
                    f"precio={price:4.1f}c  brecha={brecha:+5.1f}  pnl=${pnl:+7.0f}")

        if r_raw and (r_raw[1] - r_raw[2]) > 0.5:
            pos_raw += 1
        if r_cal and (r_cal[1] - r_cal[2]) > 0.5:
            pos_cal += 1
        print(f"fold {i+1}  (train={len(train):5d} test={len(test):4d})")
        print(f"    CRUDO       {fmt(r_raw, band_raw)}")
        print(f"    CALIBRADO   {fmt(r_cal, band_cal)}")

    print("=" * 90)
    print(f"Folds rentables OOS  —  CRUDO: {pos_raw}/{N_FOLDS}   CALIBRADO: {pos_cal}/{N_FOLDS}")
    print("VEREDICTO:")
    if pos_cal >= N_FOLDS - 1:
        print("  Edge ROBUSTO con calibracion (rentable en casi todos los folds OOS). Implementar y producir.")
    elif pos_cal > pos_raw:
        print("  La calibracion AYUDA pero el edge aun no es robusto fold-a-fold. Mas datos.")
    else:
        print("  Sin edge robusto OOS: el signo salta entre folds (no-estacionariedad).")
        print("  No pasar a produccion con esta estrategia. Re-correr con los 5 dias.")


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
