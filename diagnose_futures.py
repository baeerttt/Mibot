"""
diagnose_futures.py — ¿la Futures Intelligence predice el outcome?

Antes de meter OI Delta / Taker Ratio / L/S Ratio en la estrategia hay que probar
que predicen la direccion del spot en la ventana de los mercados Up/Down. Esto NO
opera nada: mide poder predictivo crudo de cada feature contra el outcome real.

Para cada fila de futures_tick (bucket 5m de Binance Futures):
  - open  = spot al inicio del bucket (bucket_ts)
  - out5  = sign(spot(bucket_ts + 5m)  - open)   -> outcome del mercado 5m
  - out15 = sign(spot(bucket_ts + 15m) - open)   -> outcome del mercado 15m

Metricas por feature (oi_delta, taker_ratio, ls_ratio):
  - AUC   : P(feature en casos UP > feature en casos DOWN). 0.5 = sin info, >0.55 sirve.
  - win rate de una regla direccional simple (ej taker_ratio>1 -> apostar UP).
  - up-freq por tercil de la feature (monotonico = senal real).

Uso:  venv\\Scripts\\python.exe diagnose_futures.py
"""
import sqlite3
import config

DB = config.DB_PATH
HOR_5M = 300_000     # ms
HOR_15M = 900_000    # ms
TOL = 120_000        # tolerancia para encontrar el spot cerca del borde (ms)


def ro():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.execute("PRAGMA busy_timeout=8000")
    return c


def hr(t=""):
    print("\n" + "=" * 78)
    if t:
        print(t)
        print("-" * 78)


def spot_at(conn, symbol, ts):
    """mid mas cercano a ts (dentro de TOL): primero el ultimo <=ts, sino el primero >=ts."""
    r = conn.execute(
        "SELECT mid, ts FROM spot_tick WHERE symbol=? AND ts<=? AND ts>=? "
        "ORDER BY ts DESC LIMIT 1", (symbol, ts, ts - TOL)).fetchone()
    if r:
        return r[0]
    r = conn.execute(
        "SELECT mid, ts FROM spot_tick WHERE symbol=? AND ts>=? AND ts<=? "
        "ORDER BY ts ASC LIMIT 1", (symbol, ts, ts + TOL)).fetchone()
    return r[0] if r else None


def build():
    conn = ro()
    fut = conn.execute(
        "SELECT symbol, bucket_ts, oi_delta, taker_ratio, ls_ratio, funding_rate, basis, "
        "liq_buy_usd, liq_sell_usd, liq_count "
        "FROM futures_tick WHERE bucket_ts IS NOT NULL ORDER BY bucket_ts ASC").fetchall()
    recs = []
    for symbol, b, oid, tk, ls, fr, basis, lbuy, lsell, lcnt in fut:
        if b is None:
            continue
        o = spot_at(conn, symbol, b)
        c5 = spot_at(conn, symbol, b + HOR_5M)
        c15 = spot_at(conn, symbol, b + HOR_15M)
        pre = spot_at(conn, symbol, b - HOR_5M)   # spot 5m ANTES: direccion de entrada
        if o is None:
            continue
        out5 = ("up" if c5 > o else "down") if (c5 is not None and c5 != o) else None
        out15 = ("up" if c15 > o else "down") if (c15 is not None and c15 != o) else None
        recent = ("up" if o > pre else "down") if (pre is not None and o != pre) else None
        # imbalance de liquidaciones: buy(shorts liquidados) - sell(longs liquidados)
        liq_imb = None
        if lbuy is not None or lsell is not None:
            liq_imb = (lbuy or 0.0) - (lsell or 0.0)
        recs.append({"symbol": symbol, "oi_delta": oid, "taker_ratio": tk,
                     "ls_ratio": ls, "funding_rate": fr, "basis": basis,
                     "liq_imbalance": liq_imb, "out5": out5, "out15": out15,
                     "recent_dir": recent})
    conn.close()
    return recs


def auc(values_outcomes):
    """AUC = P(valor en UP > valor en DOWN). Rank-sum (Mann-Whitney)."""
    pairs = [(v, o) for v, o in values_outcomes if v is not None and o is not None]
    if len(pairs) < 20:
        return None, 0, 0
    pos = [v for v, o in pairs if o == "up"]
    neg = [v for v, o in pairs if o == "down"]
    if not pos or not neg:
        return None, len(pos), len(neg)
    sv = sorted(pairs, key=lambda x: x[0])
    # ranks promedio (maneja empates)
    ranks = {}
    i = 0
    while i < len(sv):
        j = i
        while j + 1 < len(sv) and sv[j + 1][0] == sv[i][0]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    rank_pos = sum(ranks[k] for k, (v, o) in enumerate(sv) if o == "up")
    n_p, n_n = len(pos), len(neg)
    a = (rank_pos - n_p * (n_p + 1) / 2) / (n_p * n_n)
    return a, n_p, n_n


def rule_winrate(recs, feat, thr, horizon, predict_high="up"):
    """Win rate de: si feature>thr -> apostar predict_high, sino el contrario."""
    n = w = 0
    other = "down" if predict_high == "up" else "up"
    for r in recs:
        v, o = r[feat], r[horizon]
        if v is None or o is None:
            continue
        bet = predict_high if v > thr else other
        n += 1
        w += 1 if bet == o else 0
    return (w, n, w / n * 100 if n else None)


def terciles(recs, feat, horizon):
    vals = sorted(r[feat] for r in recs if r[feat] is not None and r[horizon] is not None)
    if len(vals) < 30:
        return None
    lo_c = vals[len(vals) // 3]
    hi_c = vals[2 * len(vals) // 3]
    buckets = {"bajo": [0, 0], "medio": [0, 0], "alto": [0, 0]}
    for r in recs:
        v, o = r[feat], r[horizon]
        if v is None or o is None:
            continue
        b = "bajo" if v <= lo_c else ("alto" if v > hi_c else "medio")
        buckets[b][0] += 1
        buckets[b][1] += 1 if o == "up" else 0
    return lo_c, hi_c, buckets


def base_rate(recs, horizon):
    vals = [r[horizon] for r in recs if r[horizon] is not None]
    if not vals:
        return None, 0
    return sum(1 for v in vals if v == "up") / len(vals), len(vals)


FEATS = [
    ("taker_ratio", 1.0, "up", "Taker Ratio (>1 = compra agresiva -> UP)"),
    ("oi_delta", 0.0, "up", "OI Delta (>0 = OI sube -> conviccion)"),
    ("ls_ratio", None, "down", "L/S Ratio (alto = muchos long -> contrarian DOWN)"),
    ("funding_rate", None, "down", "Funding Rate (alto = longs apalancados -> contrarian DOWN)"),
    ("basis", 0.0, "up", "Basis perp-spot (>0 = premio del perp -> presion UP)"),
    ("liq_imbalance", 0.0, "up", "Liq imbalance buy-sell (>0 = shorts liquidados -> UP)"),
]


def report(recs, horizon, label):
    hr(f"HORIZONTE {label}")
    br, n = base_rate(recs, horizon)
    if br is None:
        print("  sin outcomes."); return
    print(f"  muestra: {n} buckets   ·   base rate UP: {br*100:.1f}%")
    if n < 200:
        print("  ⚠  MUESTRA CHICA (<200): esto es una PISTA, no un veredicto.")
    print()
    for feat, thr, hi, desc in FEATS:
        a, np_, nn_ = auc([(r[feat], r[horizon]) for r in recs])
        # umbral: si es None usar la mediana (para L/S contrarian)
        t = thr
        if t is None:
            vv = sorted(r[feat] for r in recs if r[feat] is not None)
            t = vv[len(vv) // 2] if vv else 0.0
        w, wn, wr = rule_winrate(recs, feat, t, horizon, predict_high=hi)
        a_str = f"{a:.3f}" if a is not None else "  -  "
        # AUC se reporta SIEMPRE como P(valor mayor en UP); para contrarian, <0.5 = predice
        flag = ""
        if a is not None:
            edge = abs(a - 0.5)
            flag = "  <<< SENAL" if edge >= 0.05 else ("  (debil)" if edge >= 0.03 else "")
        print(f"  {desc}")
        print(f"     AUC={a_str}  (0.5=nada)   regla wr={wr:.1f}% ({w}/{wn}) vs base {br*100:.1f}%{flag}")
        ter = terciles(recs, feat, horizon)
        if ter:
            lo_c, hi_c, bk = ter
            parts = []
            for name in ("bajo", "medio", "alto"):
                cnt, up = bk[name]
                parts.append(f"{name}={up/cnt*100:4.1f}%(n{cnt})" if cnt else f"{name}=-")
            print(f"     up-freq por tercil:  {'  '.join(parts)}   (monotonico = senal)")
        print()


def per_symbol(recs, horizon):
    hr(f"POR ACTIVO (horizonte {horizon})  —  AUC de cada feature")
    syms = sorted(set(r["symbol"] for r in recs))
    print(f"  {'activo':10} {'n':>4}  {'taker_ratio':>11} {'oi_delta':>9} {'ls_ratio':>9}")
    for s in syms:
        sub = [r for r in recs if r["symbol"] == s]
        br, n = base_rate(sub, horizon)
        a_tk = auc([(r["taker_ratio"], r[horizon]) for r in sub])[0]
        a_oi = auc([(r["oi_delta"], r[horizon]) for r in sub])[0]
        a_ls = auc([(r["ls_ratio"], r[horizon]) for r in sub])[0]
        f = lambda a: f"{a:.3f}" if a is not None else "  -  "
        print(f"  {s:10} {n:>4}  {f(a_tk):>11} {f(a_oi):>9} {f(a_ls):>9}")
    print("  (AUC>0.55 o <0.45 = la feature separa UP de DOWN; ~0.50 = ruido)")


def interaction(recs, horizon, label, oi_min=0.0):
    """OI Delta x direccion del precio (marco clasico de order flow):
        precio sube + OI sube  -> nuevos longs        -> CONTINUACION (up)
        precio sube + OI baja  -> cobertura de shorts -> REVERSION   (down)
        precio baja + OI sube  -> nuevos shorts       -> CONTINUACION (down)
        precio baja + OI baja  -> liquidacion longs   -> REVERSION   (up)
    La tesis es de INTERACCION, no del signo de OI solo. oi_min filtra |oi_delta|
    chico (ruido): la conviccion/squeeze deberia ser mas nitida con OI moviendose fuerte.
    """
    quad = {
        ("up", "up"):   ("CONTINUA", "up"),    # precio↑ OI↑  nuevos longs
        ("up", "down"): ("REVIERTE", "down"),  # precio↑ OI↓  short covering
        ("down", "up"): ("CONTINUA", "down"),  # precio↓ OI↑  nuevos shorts
        ("down", "down"): ("REVIERTE", "up"),  # precio↓ OI↓  liquidacion longs
    }
    hr(f"INTERACCION OI Delta x precio  —  horizonte {label}" +
       (f"  (|oi_delta| > {oi_min:.4f})" if oi_min else ""))
    tally = {}
    tot_n = tot_w = 0
    for r in recs:
        oid, rd, o = r["oi_delta"], r["recent_dir"], r[horizon]
        if oid is None or rd is None or o is None or abs(oid) <= oi_min:
            continue
        oi_dir = "up" if oid > 0 else "down"
        regime, pred = quad[(rd, oi_dir)]
        key = (rd, oi_dir, regime, pred)
        t = tally.setdefault(key, [0, 0, 0])   # [n, up_real, aciertos_prediccion]
        t[0] += 1
        t[1] += 1 if o == "up" else 0
        t[2] += 1 if pred == o else 0
        tot_n += 1
        tot_w += 1 if pred == o else 0
    if tot_n == 0:
        print("  sin datos."); return None
    print(f"  {'precio':6} {'OI':5} {'regimen':9} {'predice':8} {'n':>4} {'up_real':>8} {'acierto':>8}")
    for (rd, oid, regime, pred), (n, up, acc) in sorted(tally.items(), key=lambda x: -x[1][0]):
        print(f"  {rd:6} {oid:5} {regime:9} {pred:8} {n:>4} {up/n*100:7.1f}% {acc/n*100:7.1f}%")
    wr = tot_w / tot_n * 100
    br, _ = base_rate(recs, horizon)
    edge = wr - 50.0
    flag = "  <<< SENAL" if abs(edge) >= 3 and tot_n >= 200 else ("  (pista)" if abs(edge) >= 2 else "")
    print(f"  ---")
    print(f"  win rate de la regla de interaccion: {wr:.1f}%  ({tot_w}/{tot_n})  vs 50%{flag}")
    print(f"  (mira la columna 'acierto' por cuadrante: si un cuadrante supera 55% consistente,")
    print(f"   ESE es el sub-edge; la conviccion (CONTINUA) suele ser mas fiable que la reversion)")
    return wr, tot_n


def main():
    recs = build()
    print(f"Futures Intelligence — diagnostico predictivo")
    print(f"Buckets futuros con spot alineado: {len(recs)}")
    if not recs:
        print("Sin datos alineables."); return
    report(recs, "out5", "5m")
    report(recs, "out15", "15m")
    per_symbol(recs, "out5")
    per_symbol(recs, "out15")

    # Test de INTERACCION (la tesis real de OI Delta): signo(oi_delta) x direccion previa.
    # Se corre sobre todos los buckets y tambien filtrando |oi_delta| al tercil alto
    # (donde la conviccion/squeeze deberia ser mas nitida).
    oi_abs = sorted(abs(r["oi_delta"]) for r in recs if r["oi_delta"] is not None)
    oi_hi = oi_abs[2 * len(oi_abs) // 3] if oi_abs else 0.0
    interaction(recs, "out5", "5m")
    interaction(recs, "out15", "15m")
    interaction(recs, "out5", "5m", oi_min=oi_hi)
    interaction(recs, "out15", "15m", oi_min=oi_hi)

    hr("LECTURA")
    print("  AUC lejos de 0.50 (>0.55 directo, <0.45 contrarian) = la feature TIENE info.")
    print("  Si todas dan ~0.50 -> los futuros NO predicen el outcome en esta muestra:")
    print("  no meterlos a la estrategia todavia; juntar mas dias y re-correr.")
    print("  Si alguna da senal consistente por activo -> candidata real a feature.")


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
