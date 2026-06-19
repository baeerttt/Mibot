"""
diagnose.py — autopsia del ROI negativo.

No mira el win rate en abstracto: mide la MECANICA por la que se pierde plata.
En un mercado binario, comprar a precio p y ganar paga (1-p); perder cuesta p.
=> El break-even NO es 50%: es win_rate > precio_pagado. Si pagas 0.55 promedio,
   necesitas ganar > 55% o perdes SI o SI, tengas el win rate que tengas.

Responde, con numeros:
  1. BREAK-EVEN: cual es el precio promedio pagado vs el win rate real.
  2. CALIBRACION DEL SUBCONJUNTO APOSTADO: cuando el modelo dice 'fair_p', cuanto gana.
  3. SELECCION ADVERSA: a mayor edge declarado, baja o sube el acierto.
  4. FADE: apostar el lado contrario daria positivo.
  5. DESCOMPOSICION DEL PnL: de donde sale la perdida.

Usa la tabla bets (apuestas reales) y predictions (muestra grande).
Uso:  venv\\Scripts\\python.exe diagnose.py
"""
import sqlite3
import math

import config

DB = config.DB_PATH


def ro():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


def hr(t=""):
    print("\n" + "=" * 78)
    if t:
        print(t)
        print("-" * 78)


def diag_bets():
    """Autopsia sobre las apuestas REALES (lo que de verdad paso)."""
    conn = ro()
    rows = conn.execute(
        "SELECT side, price, shares, stake, fair_p, edge, outcome, pnl, asset, interval "
        "FROM bets WHERE status='settled' AND outcome IS NOT NULL"
    ).fetchall()
    conn.close()
    if not rows:
        print("Sin apuestas liquidadas todavia.")
        return

    n = len(rows)
    wins = sum(1 for r in rows if r[0] == r[6])
    wr = wins / n
    avg_price = sum(r[1] for r in rows) / n          # precio pagado promedio
    avg_fair = sum(r[4] for r in rows) / n           # prob que el modelo le asigno al lado
    avg_edge = sum(r[5] for r in rows) / n
    total_pnl = sum(r[7] for r in rows)
    total_stake = sum(r[3] for r in rows)
    pnl_wins = sum(r[7] for r in rows if r[0] == r[6])
    pnl_loss = sum(r[7] for r in rows if r[0] != r[6])

    hr("1. BREAK-EVEN  (la mecanica que importa)")
    print(f"  apuestas liquidadas : {n}   ({wins}W / {n-wins}L)")
    print(f"  win rate REAL       : {wr*100:5.1f}%")
    print(f"  precio pagado prom   : {avg_price*100:5.1f}c  <- ESTE es el break-even necesario")
    print(f"  brecha (wr - precio) : {(wr-avg_price)*100:+5.1f} puntos", end="")
    if wr - avg_price < 0:
        print("   <<< NEGATIVO = perdes por construccion")
    else:
        print("   (positivo = el edge sobrevive)")
    print(f"\n  Lectura: en binario, comprar a {avg_price*100:.0f}c y ganar {wr*100:.0f}% de las")
    print(f"           veces da perdida si {wr*100:.0f} < {avg_price*100:.0f}. El win rate de 50%")
    print(f"           NO es el umbral: el precio que pagas lo es.")

    hr("2. CALIBRACION DEL SUBCONJUNTO APOSTADO")
    print(f"  el modelo le asigno al lado apostado, en promedio : {avg_fair*100:5.1f}%")
    print(f"  ese lado gano en la realidad                      : {wr*100:5.1f}%")
    gap = avg_fair - wr
    print(f"  sobreconfianza del modelo                         : {gap*100:+5.1f} puntos", end="")
    if gap > 0.03:
        print("   <<< el modelo CREE mas de lo que acierta")
    else:
        print()
    print(f"  edge declarado promedio                           : {avg_edge*100:5.1f}%")
    print(f"  edge REAL (wr - precio)                            : {(wr-avg_price)*100:+5.1f}%")
    print("  Si el edge declarado es alto pero el real es <=0: el edge es ILUSORIO.")

    hr("3. SELECCION ADVERSA  (a mayor edge, gana mas o menos?)")
    buckets = [(0.0, 0.04), (0.04, 0.07), (0.07, 0.12), (0.12, 1.0)]
    for lo, hi in buckets:
        sub = [r for r in rows if lo <= r[5] < hi]
        if not sub:
            print(f"  edge {lo*100:4.0f}-{hi*100:4.0f}%:  sin apuestas")
            continue
        w = sum(1 for r in sub if r[0] == r[6])
        p = sum(r[1] for r in sub) / len(sub)
        pl = sum(r[7] for r in sub)
        print(f"  edge {lo*100:4.0f}-{hi*100:4.0f}%:  {len(sub):3d} apuestas  wr={w/len(sub)*100:5.1f}%  "
              f"precio={p*100:4.0f}c  pnl={pl:+8.2f}")
    print("  Si el wr CAE al subir el edge -> seleccion adversa: el modelo apuesta")
    print("  fuerte justo donde se equivoca (el mercado sabe algo que el no).")

    hr("4. FADE  (y si apostaramos el lado CONTRARIO?)")
    fade_wins = n - wins
    fade_wr = fade_wins / n
    # PnL del fade: comprar el lado opuesto al mismo book. Aproximacion: precio
    # del lado contrario ~ (1 - precio_pagado) si el spread fuera 0.
    fade_pnl = 0.0
    for side, price, shares, stake, fair_p, edge, outcome, pnl, asset, iv in rows:
        opp_price = 1 - price                 # aprox del ask contrario (spread 0)
        opp_price = min(0.99, opp_price + config.EXEC_COST)
        opp_win = (("up" if side == "down" else "down") == outcome)
        opp_shares = stake / opp_price        # mismo stake
        fade_pnl += opp_shares * (1 - opp_price) if opp_win else -stake
    print(f"  win rate fadeando : {fade_wr*100:5.1f}%   ({fade_wins}W / {wins}L)")
    print(f"  PnL estimado fade : {fade_pnl:+8.2f}   (aprox: precio contrario = 1 - precio, +costo)")
    print("  OJO: aproximacion. El precio real del lado contrario lo da el book,")
    print("       no es exactamente 1 - precio. Pero da el orden de magnitud.")

    hr("5. DESCOMPOSICION DEL PnL")
    print(f"  PnL total        : {total_pnl:+9.2f}")
    print(f"  ganado en wins   : {pnl_wins:+9.2f}  ({wins} apuestas)")
    print(f"  perdido en losses: {pnl_loss:+9.2f}  ({n-wins} apuestas)")
    print(f"  stake total      : {total_stake:9.2f}")
    print(f"  ROI sobre stake  : {total_pnl/total_stake*100:+5.1f}%")
    if wins:
        print(f"  ganancia media/win : {pnl_wins/wins:+7.2f}")
    if n - wins:
        print(f"  perdida media/loss : {pnl_loss/(n-wins):+7.2f}")

    # por activo
    hr("   PnL por activo / intervalo")
    by = {}
    for side, price, shares, stake, fair_p, edge, outcome, pnl, asset, iv in rows:
        k = asset
        b = by.setdefault(k, [0, 0, 0.0])
        b[0] += 1; b[1] += 1 if side == outcome else 0; b[2] += pnl
    for k, (cnt, w, pl) in sorted(by.items(), key=lambda x: x[1][2]):
        print(f"   {k:5}: {cnt:3d} apuestas  wr={w/cnt*100:5.1f}%  pnl={pl:+8.2f}")


def diag_predictions():
    """Muestra grande: calibracion global del modelo y del subconjunto candidato."""
    conn = ro()
    rows = conn.execute(
        "SELECT fair_p, edge_up, edge_down, tau, outcome FROM predictions "
        "WHERE outcome IS NOT NULL AND fair_p IS NOT NULL "
        "AND edge_up IS NOT NULL AND edge_down IS NOT NULL"
    ).fetchall()
    conn.close()
    if not rows:
        return
    n = len(rows)

    hr(f"6. CALIBRACION GLOBAL DEL MODELO  (muestra grande: {n} predicciones)")
    # bins de fair_p vs frecuencia real de 'up'
    bins = [(i/10, (i+1)/10) for i in range(10)]
    print("  fair_p (modelo) ->  frecuencia real de UP   (perfecto = diagonal)")
    for lo, hi in bins:
        sub = [r for r in rows if lo <= r[0] < hi]
        if len(sub) < 10:
            continue
        up_freq = sum(1 for r in sub if r[4] == "up") / len(sub)
        mid = (lo + hi) / 2
        flag = "  <- mal calibrado" if abs(up_freq - mid) > 0.12 else ""
        print(f"  {lo*100:3.0f}-{hi*100:3.0f}%  (n={len(sub):5d}):  real={up_freq*100:5.1f}%{flag}")

    # brier global
    brier = sum((r[0] - (1.0 if r[4] == "up" else 0.0))**2 for r in rows) / n
    print(f"\n  Brier global del modelo: {brier:.4f}   (azar=0.25, perfecto=0)")

    # subconjunto candidato a apostar (edge neto > min_edge)
    hr("7. EL MODELO EN EL MARGEN DONDE APUESTA  (candidatos edge neto > min_edge)")
    cost = config.EXEC_COST
    me = config.MIN_EDGE
    cand = []
    for fair_p, eu, ed, tau, outcome in rows:
        if tau < config.MIN_TIME_LEFT_SEC:
            continue
        if eu >= ed:
            side, p_side, edge = "up", fair_p, eu
        else:
            side, p_side, edge = "down", 1 - fair_p, ed
        if (edge - cost) < me or edge > config.MAX_EDGE_TRUST:
            continue
        cand.append((side, p_side, edge, outcome))
    if cand:
        cw = sum(1 for s, p, e, o in cand if s == o)
        cwr = cw / len(cand)
        cap = sum(p - e for s, p, e, o in cand) / len(cand)   # precio = p_side - edge
        cfp = sum(p for s, p, e, o in cand) / len(cand)
        cbrier = sum((p - (1.0 if s == o else 0.0))**2 for s, p, e, o in cand) / len(cand)
        print(f"  candidatos: {len(cand)}")
        print(f"  win rate del lado del modelo : {cwr*100:5.1f}%")
        print(f"  precio promedio (break-even) : {cap*100:5.1f}c")
        print(f"  brecha (wr - precio)         : {(cwr-cap)*100:+5.1f} pts", end="")
        print("   <<< si es negativo, NO hay edge" if cwr - cap < 0 else "")
        print(f"  prob que asigna el modelo    : {cfp*100:5.1f}%  (vs {cwr*100:.0f}% real)")
        print(f"  Brier en el margen apostado  : {cbrier:.4f}  (vs {brier:.4f} global)")
        print(f"\n  FADE en muestra grande: el lado contrario gana {(1-cwr)*100:.1f}%")
        print(f"                          break-even contrario ~ {(1-cap)*100:.0f}c")
        edge_fade = (1 - cwr) - (1 - cap)
        print(f"                          edge del fade ~ {edge_fade*100:+.1f} pts", end="")
        print("   <<< FADE TENDRIA EDGE" if edge_fade > 0.02 else "  (no concluyente)")
    else:
        print("  sin candidatos con la config actual.")


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("AUTOPSIA DEL ROI  ·  Mibot")
    diag_bets()
    diag_predictions()
    hr("RESUMEN")
    print("  El ROI negativo se explica por UNA de estas (o varias):")
    print("   A) pagas un precio mayor que tu tasa de acierto (break-even no cubierto)")
    print("   B) el modelo esta sobreconfiado en el margen donde apuesta (Brier sube)")
    print("   C) seleccion adversa: edge alto = donde el modelo mas se equivoca")
    print("  Mira que numeros de arriba estan en rojo y ataca ESE.")
