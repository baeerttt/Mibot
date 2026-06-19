"""
stability.py — la banda 6-12% es edge REAL o una racha?

Parte la historia en 4 cuartos temporales y mide la banda rentable en cada uno.
Si la brecha (wr - precio) es positiva en LOS 4 -> edge robusto.
Si solo en el ultimo -> racha/no-estacionariedad, NO confiar.
Tambien muestra el base-rate de UP por cuarto (detecta drift direccional del cripto).
"""
import sqlite3
from datetime import datetime, timezone
import config

conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
rows = conn.execute(
    "SELECT ts, fair_p, edge_up, edge_down, tau, outcome FROM predictions "
    "WHERE outcome IS NOT NULL AND fair_p IS NOT NULL "
    "AND edge_up IS NOT NULL AND edge_down IS NOT NULL "
    "ORDER BY ts ASC"
).fetchall()
conn.close()

cost = config.EXEC_COST
n = len(rows)
q = n // 4
quarters = [rows[0:q], rows[q:2*q], rows[2*q:3*q], rows[3*q:]]

def band(data, lo, hi, min_tau=120):
    nb = wb = 0
    psum = pnl = 0.0
    for ts, fair_p, eu, ed, tau, outcome in data:
        if tau < min_tau:
            continue
        if eu >= ed:
            side, p_side, edge = "up", fair_p, eu
        else:
            side, p_side, edge = "down", 1 - fair_p, ed
        net = edge - cost
        if not (lo <= net < hi) or edge > config.MAX_EDGE_TRUST:
            continue
        ask_eff = (p_side - edge) + cost
        if ask_eff <= 0.01 or ask_eff >= 0.99:
            continue
        win = side == outcome
        nb += 1; wb += 1 if win else 0; psum += ask_eff
        shares = 100 / ask_eff
        pnl += shares * (1 - ask_eff) if win else -100
    if nb == 0:
        return None
    return nb, wb/nb*100, psum/nb*100, pnl

def up_rate(data):
    return sum(1 for r in data if r[5] == "up") / len(data) * 100

print(f"Total: {n} predicciones, 4 cuartos de ~{q} c/u")
print("=" * 86)
for i, data in enumerate(quarters):
    t0 = datetime.fromtimestamp(data[0][0]/1000, timezone.utc).strftime("%d/%m %H:%M")
    t1 = datetime.fromtimestamp(data[-1][0]/1000, timezone.utc).strftime("%d/%m %H:%M")
    print(f"\nCUARTO {i+1}  ({t0} -> {t1})   base-rate UP={up_rate(data):.1f}%")
    print("-" * 86)
    for lo, hi in [(0.02, 0.05), (0.05, 0.09), (0.09, 0.12), (0.12, 0.25)]:
        r = band(data, lo, hi)
        if r is None:
            print(f"  edge {lo*100:4.0f}-{hi*100:4.0f}%:  sin candidatos")
            continue
        nb, wr, price, pnl = r
        brecha = wr - price
        mark = "  RENTABLE" if brecha > 0.5 else ("  ~par" if brecha > -0.5 else "")
        print(f"  edge {lo*100:4.0f}-{hi*100:4.0f}%:  n={nb:4d}  wr={wr:5.1f}%  "
              f"precio={price:4.1f}c  brecha={brecha:+5.1f}{mark}")

print("\n" + "=" * 86)
print("Si la banda 5-12% es RENTABLE en los 4 cuartos -> edge robusto, implementar.")
print("Si salta de signo entre cuartos -> no-estacionario, es ruido. Esperar mas datos.")
