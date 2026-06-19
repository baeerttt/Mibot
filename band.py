"""Banda de edge: win rate y break-even por franja, en la muestra grande."""
import sqlite3
import config

conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
rows = conn.execute(
    "SELECT fair_p, edge_up, edge_down, tau, outcome FROM predictions "
    "WHERE outcome IS NOT NULL AND fair_p IS NOT NULL "
    "AND edge_up IS NOT NULL AND edge_down IS NOT NULL"
).fetchall()
conn.close()

cost = config.EXEC_COST

def band(lo, hi, min_tau=120):
    n = w = 0
    psum = 0.0
    pnl = 0.0
    for fair_p, eu, ed, tau, outcome in rows:
        if tau < min_tau:
            continue
        if eu >= ed:
            side, p_side, edge = "up", fair_p, eu
        else:
            side, p_side, edge = "down", 1 - fair_p, ed
        net = edge - cost
        if not (lo <= net < hi):
            continue
        if edge > config.MAX_EDGE_TRUST:
            continue
        ask_eff = (p_side - edge) + cost
        if ask_eff <= 0.01 or ask_eff >= 0.99:
            continue
        win = (side == outcome)
        n += 1
        w += 1 if win else 0
        psum += ask_eff
        # apuesta de stake fijo $100 para comparar bandas en igualdad
        shares = 100 / ask_eff
        pnl += shares * (1 - ask_eff) if win else -100
    if n == 0:
        return f"  edge neto {lo*100:4.0f}-{hi*100:4.0f}%:  sin candidatos"
    wr = w / n * 100
    price = psum / n * 100
    return (f"  edge neto {lo*100:4.0f}-{hi*100:4.0f}%:  n={n:5d}  wr={wr:5.1f}%  "
            f"precio={price:4.1f}c  brecha={wr-price:+5.1f}  pnl(stake$100)={pnl:+9.0f}")

print("BANDAS DE EDGE  (muestra grande, tau>=120s, stake fijo $100 por apuesta)")
print("=" * 78)
for lo, hi in [(0.00, 0.02), (0.02, 0.04), (0.04, 0.05), (0.05, 0.07),
               (0.07, 0.09), (0.09, 0.12), (0.12, 0.16), (0.16, 0.25)]:
    print(band(lo, hi))

print("\nBANDAS ACUMULADAS (lo que pasaria si filtraramos a esa franja):")
print("=" * 78)
for lo, hi in [(0.05, 0.12), (0.05, 0.10), (0.06, 0.12), (0.07, 0.12), (0.04, 0.12)]:
    print(band(lo, hi))
