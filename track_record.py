"""
track_record.py — el "expediente de evidencia" de Mibot.

Lee la DB y arma el track record auditable que pide la evaluacion de DSN Empresas
(la metrica unica Brier NO alcanza): calcula los 6 componentes del Brier Composite
Score recomendado y exporta el registro limpio para inversores.

Componentes (peso sugerido por DSN):
    Brier Score          35%   calidad predictiva (calibracion)
    Rentabilidad neta    20%   ¿el edge genera dinero tras costos?
    Drawdown maximo      15%   riesgo para los LPs
    Cantidad de preds    10%   tamaño de muestra (DSN: 300-1000 minimo)
    Liquidez operable    10%   capital ejecutable sin distorsionar precio
    Consistencia temporal 10%  performance estable entre periodos (no azar)

Ademas calcula el SKILL real = Brier_mercado - Brier_bot sobre las apuestas
tomadas (positivo = el bot le gana al precio de mercado; ver hallazgo en memoria:
el Brier on-chain mide al mercado, no al bot).

Salidas:
    - reporte por pantalla (dashboard + breakdown por activo/intervalo/cohorte)
    - data/track_record.csv          (una fila por apuesta liquidada, con equity y drawdown)
    - data/track_record_summary.json (todos los componentes + composite + metadata)

Uso:
    venv\\Scripts\\python.exe track_record.py
    venv\\Scripts\\python.exe track_record.py --since 2026-06-19   # solo desde una fecha
    venv\\Scripts\\python.exe track_record.py --asset btc --interval 5m
"""
import argparse
import csv
import json
import sqlite3
import time
from datetime import datetime, timezone

import config

# --- Pesos del Brier Composite Score (recomendacion DSN Empresas, 19/6/2026) ---
W_BRIER, W_RENT, W_DD, W_PREDS, W_LIQ, W_CONS = 0.35, 0.20, 0.15, 0.10, 0.10, 0.10

# --- Escalas de normalizacion (PRIMER PASO; DSN da pesos, no escalas: ajustar) ---
BRIER_CHANCE = 0.25     # Brier del azar -> score 0; Brier 0 -> score 1
RENT_TARGET  = 0.10     # ROI 10% -> score 1
DD_FLOOR     = 0.20     # drawdown 20% -> score 0
PREDS_TARGET = 1000     # 1000 predicciones etiquetadas -> score 1 (DSN: 300-1000)
LIQ_TARGET   = 500.0    # 500 contratos de profundidad media -> score 1
N_COHORTS    = 6        # cohortes temporales para medir consistencia


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _load_bets(conn, since_ms, asset, interval):
    q = ("SELECT ts_open, ts_settle, slug, asset, interval, side, price, shares, "
         "stake, fair_p, edge, outcome, pnl FROM bets WHERE status='settled'")
    p = []
    if since_ms:
        q += " AND ts_settle >= ?"; p.append(since_ms)
    if asset:
        q += " AND asset = ?"; p.append(asset)
    if interval:
        q += " AND interval = ?"; p.append(interval)
    q += " ORDER BY ts_settle ASC"
    rows = conn.execute(q, p).fetchall()
    out = []
    for (ts_open, ts_settle, slug, asset_, interval_, side, price, shares,
         stake, fair_p, edge, outcome, pnl) in rows:
        if outcome is None or pnl is None:
            continue
        win = 1 if side == outcome else 0
        out.append({
            "ts_open": ts_open, "ts_settle": ts_settle, "slug": slug,
            "asset": asset_, "interval": interval_, "side": side,
            "price": price, "shares": shares, "stake": stake,
            "fair_p": fair_p, "edge": edge, "outcome": outcome,
            "win": win, "pnl": pnl,
        })
    return out


def _brier(bets):
    """Brier del bot y del mercado sobre las apuestas tomadas (mismo subconjunto)."""
    nb = ns = 0
    sb = sm = 0.0
    for b in bets:
        if b["fair_p"] is not None:
            sb += (b["fair_p"] - b["win"]) ** 2; nb += 1
        if b["price"] is not None:
            sm += (b["price"] - b["win"]) ** 2; ns += 1
    brier_bot = sb / nb if nb else None
    brier_mkt = sm / ns if ns else None
    skill = (brier_mkt - brier_bot) if (brier_bot is not None and brier_mkt is not None) else None
    return brier_bot, brier_mkt, skill


def _drawdown(bets, start):
    """Curva de equity (start + PnL acumulado) y maximo drawdown (abs y %)."""
    equity = start
    peak = start
    max_dd_abs = 0.0
    max_dd_frac = 0.0
    curve = []
    for b in bets:
        equity += b["pnl"]
        peak = max(peak, equity)
        dd = peak - equity
        max_dd_abs = max(max_dd_abs, dd)
        if peak > 0:
            max_dd_frac = max(max_dd_frac, dd / peak)
        curve.append((equity, peak - equity))
    return curve, max_dd_abs, max_dd_frac


def _cohorts(bets, n):
    """Parte las apuestas en n cohortes cronologicas iguales; metricas por cohorte."""
    if not bets:
        return []
    size = max(1, len(bets) // n)
    chunks = [bets[i:i + size] for i in range(0, len(bets), size)]
    # si quedo una cola chica, fusionarla con la anterior
    if len(chunks) > n and len(chunks[-1]) < size / 2:
        chunks[-2].extend(chunks[-1]); chunks.pop()
    res = []
    for ch in chunks:
        w = sum(b["win"] for b in ch)
        pnl = sum(b["pnl"] for b in ch)
        res.append({
            "n": len(ch), "wins": w, "win_rate": w / len(ch),
            "pnl": pnl, "t0": ch[0]["ts_settle"], "t1": ch[-1]["ts_settle"],
        })
    return res


def _by_day(bets):
    days = {}
    for b in bets:
        d = datetime.fromtimestamp(b["ts_settle"] / 1000, tz=timezone.utc).date().isoformat()
        x = days.setdefault(d, {"n": 0, "wins": 0, "pnl": 0.0})
        x["n"] += 1; x["wins"] += b["win"]; x["pnl"] += b["pnl"]
    return dict(sorted(days.items()))


def _by_key(bets, key):
    g = {}
    for b in bets:
        x = g.setdefault(b[key], {"n": 0, "wins": 0, "pnl": 0.0, "sb": 0.0})
        x["n"] += 1; x["wins"] += b["win"]; x["pnl"] += b["pnl"]
        if b["fair_p"] is not None:
            x["sb"] += (b["fair_p"] - b["win"]) ** 2
    for k, x in g.items():
        x["win_rate"] = x["wins"] / x["n"]
        x["brier"] = x["sb"] / x["n"]
        x["roi"] = x["pnl"] / config.PAPER_BANKROLL
    return dict(sorted(g.items(), key=lambda kv: -kv[1]["pnl"]))


def _liquidity(conn, bets):
    """Profundidad media del book (best_ask size) en los mercados apostados.
    Mide cuanto capital se podia ejecutar realmente, no solo lo que apostamos."""
    sizes = []
    for b in bets:
        row = conn.execute(
            "SELECT AVG(ask_size) FROM pm_book WHERE slug=? AND side=? "
            "AND ts BETWEEN ? AND ?",
            (b["slug"], b["side"], b["ts_open"] - 60000, b["ts_open"] + 60000)).fetchone()
        if row and row[0]:
            sizes.append(row[0])
    if not sizes:
        return None, None
    sizes.sort()
    median = sizes[len(sizes) // 2]
    avg = sum(sizes) / len(sizes)
    return median, avg


def _n_predictions(conn, since_ms, asset, interval):
    q = "SELECT COUNT(*) FROM predictions WHERE outcome IS NOT NULL"
    p = []
    if since_ms:
        q += " AND ts >= ?"; p.append(since_ms)
    if asset:
        q += " AND asset = ?"; p.append(asset)
    # predictions no guarda interval; el filtro de intervalo no aplica aca
    return conn.execute(q, p).fetchone()[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="fecha YYYY-MM-DD (filtra por ts_settle)")
    ap.add_argument("--asset", help="btc/eth/sol/xrp")
    ap.add_argument("--interval", help="5m/15m")
    ap.add_argument("--cohorts", type=int, default=N_COHORTS)
    args = ap.parse_args()

    since_ms = None
    if args.since:
        since_ms = int(datetime.strptime(args.since, "%Y-%m-%d")
                       .replace(tzinfo=timezone.utc).timestamp() * 1000)

    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    bets = _load_bets(conn, since_ms, args.asset, args.interval)
    start = config.PAPER_BANKROLL

    if not bets:
        print("Sin apuestas liquidadas para ese filtro.")
        return

    # --- metricas crudas ---
    n = len(bets)
    wins = sum(b["win"] for b in bets)
    win_rate = wins / n
    pnl = sum(b["pnl"] for b in bets)
    roi = pnl / start
    brier_bot, brier_mkt, skill = _brier(bets)
    curve, dd_abs, dd_frac = _drawdown(bets, start)
    n_preds = _n_predictions(conn, since_ms, args.asset, args.interval)
    liq_med, liq_avg = _liquidity(conn, bets)
    cohorts = _cohorts(bets, args.cohorts)
    coh_pos = sum(1 for c in cohorts if c["pnl"] > 0)
    consistency = coh_pos / len(cohorts) if cohorts else 0.0
    avg_stake = sum(b["stake"] for b in bets) / n
    t0, t1 = bets[0]["ts_settle"], bets[-1]["ts_settle"]

    # --- normalizacion a score 0-1 ---
    s_brier = _clamp((BRIER_CHANCE - brier_bot) / BRIER_CHANCE) if brier_bot is not None else 0.0
    s_rent  = _clamp(roi / RENT_TARGET)
    s_dd    = _clamp(1 - dd_frac / DD_FLOOR)
    s_preds = _clamp(n_preds / PREDS_TARGET)
    s_liq   = _clamp((liq_med or 0) / LIQ_TARGET)
    s_cons  = _clamp(consistency)
    composite = (W_BRIER * s_brier + W_RENT * s_rent + W_DD * s_dd +
                 W_PREDS * s_preds + W_LIQ * s_liq + W_CONS * s_cons)

    # --- dashboard ---
    print("=" * 68)
    print("  MIBOT — TRACK RECORD  ·  Brier Composite Score (criterio DSN)")
    print("=" * 68)
    flt = []
    if args.since: flt.append(f"desde {args.since}")
    if args.asset: flt.append(args.asset)
    if args.interval: flt.append(args.interval)
    print(f"  Periodo: {_iso(t0)}  ->  {_iso(t1)} UTC" + (f"   [{', '.join(flt)}]" if flt else ""))
    print(f"  Apuestas liquidadas: {n}   ·   Predicciones etiquetadas: {n_preds:,}")
    print("-" * 68)
    print(f"  {'COMPONENTE':<24}{'VALOR':>16}{'SCORE':>10}{'PESO':>8}")
    print(f"  {'Brier Score (bot)':<24}{brier_bot:>16.4f}{s_brier:>10.2f}{W_BRIER:>8.0%}")
    print(f"  {'Rentabilidad neta':<24}{('$%+.2f' % pnl):>16}{s_rent:>10.2f}{W_RENT:>8.0%}")
    print(f"  {'  (ROI)':<24}{(_pct(roi)):>16}")
    print(f"  {'Drawdown maximo':<24}{(_pct(dd_frac)):>16}{s_dd:>10.2f}{W_DD:>8.0%}")
    print(f"  {'  (en $)':<24}{('$%.2f' % dd_abs):>16}")
    print(f"  {'Cantidad predicciones':<24}{n_preds:>16,}{s_preds:>10.2f}{W_PREDS:>8.0%}")
    liq_str = f"{liq_med:.0f}" if liq_med is not None else "s/d"
    print(f"  {'Liquidez (book med.)':<24}{liq_str:>16}{s_liq:>10.2f}{W_LIQ:>8.0%}")
    print(f"  {'Consistencia (cohortes)':<24}{(f'{coh_pos}/{len(cohorts)}+'):>16}{s_cons:>10.2f}{W_CONS:>8.0%}")
    print("-" * 68)
    print(f"  {'BRIER COMPOSITE SCORE':<24}{'':<16}{composite:>10.2f}{'/1.00':>8}")
    print("=" * 68)

    # --- skill real vs mercado ---
    print("\n  SKILL vs MERCADO (sobre las apuestas tomadas):")
    print(f"    Brier bot    = {brier_bot:.4f}")
    print(f"    Brier mercado= {brier_mkt:.4f}  (precio pagado vs outcome)")
    if skill is not None:
        verdict = "el bot LE GANA al mercado" if skill > 0 else "el mercado le gana al bot"
        print(f"    Skill        = {skill:+.4f}   -> {verdict}")
    print(f"    Win rate     = {_pct(win_rate)}   ·   precio medio pagado = {_avg_price(bets):.3f}")
    print(f"    (break-even: win_rate debe superar el precio medio pagado)")

    # --- breakdown por activo / intervalo ---
    print("\n  POR ACTIVO:")
    _print_group(_by_key(bets, "asset"))
    print("\n  POR INTERVALO:")
    _print_group(_by_key(bets, "interval"))

    # --- consistencia: cohortes y dias ---
    print("\n  COHORTES TEMPORALES (consistencia):")
    for i, c in enumerate(cohorts, 1):
        bar = "+" if c["pnl"] > 0 else "-"
        print(f"    cohorte {i}  n={c['n']:>4}  wr={_pct(c['win_rate']):>6}  "
              f"pnl=${c['pnl']:>+9.2f}  [{bar}]")
    print("\n  POR DIA (UTC):")
    for d, x in _by_day(bets).items():
        wr = x["wins"] / x["n"]
        print(f"    {d}  n={x['n']:>4}  wr={_pct(wr):>6}  pnl=${x['pnl']:>+9.2f}")

    # --- exports ---
    _write_csv(bets, curve, start)
    _write_summary({
        "generado": _iso(int(time.time() * 1000)),
        "periodo": {"desde": _iso(t0), "hasta": _iso(t1)},
        "filtros": {"since": args.since, "asset": args.asset, "interval": args.interval},
        "n_bets": n, "n_predicciones": n_preds,
        "win_rate": win_rate, "precio_medio_pagado": _avg_price(bets),
        "pnl_neto": pnl, "roi": roi, "equity_final": start + pnl,
        "brier_bot": brier_bot, "brier_mercado": brier_mkt, "skill_vs_mercado": skill,
        "drawdown_max_frac": dd_frac, "drawdown_max_abs": dd_abs,
        "liquidez_book_mediana": liq_med, "liquidez_book_media": liq_avg,
        "stake_medio": avg_stake,
        "consistencia_cohortes": consistency, "cohortes_positivas": coh_pos,
        "n_cohortes": len(cohorts),
        "scores": {"brier": s_brier, "rentabilidad": s_rent, "drawdown": s_dd,
                   "predicciones": s_preds, "liquidez": s_liq, "consistencia": s_cons},
        "pesos": {"brier": W_BRIER, "rentabilidad": W_RENT, "drawdown": W_DD,
                  "predicciones": W_PREDS, "liquidez": W_LIQ, "consistencia": W_CONS},
        "brier_composite_score": composite,
    })
    print("\n  Export: data/track_record.csv  +  data/track_record_summary.json")
    print("  NOTA: las escalas de normalizacion son un primer paso (DSN da pesos,")
    print("  no escalas). Ajustar BRIER_CHANCE/RENT_TARGET/etc. al consensuar el gate.")


def _pct(x):
    return f"{x*100:+.2f}%" if x is not None else "s/d"


def _avg_price(bets):
    ps = [b["price"] for b in bets if b["price"] is not None]
    return sum(ps) / len(ps) if ps else 0.0


def _print_group(g):
    print(f"    {'':<8}{'n':>6}{'win_rate':>10}{'brier':>9}{'pnl':>12}{'roi':>9}")
    for k, x in g.items():
        print(f"    {k:<8}{x['n']:>6}{_pct(x['win_rate']):>10}{x['brier']:>9.4f}"
              f"{('$%+.2f' % x['pnl']):>12}{_pct(x['roi']):>9}")


def _write_csv(bets, curve, start):
    with open("data/track_record.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts_open_utc", "ts_settle_utc", "asset", "interval", "side",
                    "price", "shares", "stake", "fair_p", "edge", "outcome",
                    "win", "pnl", "equity", "drawdown"])
        for b, (equity, dd) in zip(bets, curve):
            w.writerow([_iso(b["ts_open"]), _iso(b["ts_settle"]), b["asset"],
                        b["interval"], b["side"], f"{b['price']:.4f}",
                        f"{b['shares']:.4f}", f"{b['stake']:.4f}",
                        f"{b['fair_p']:.4f}" if b["fair_p"] is not None else "",
                        f"{b['edge']:.4f}" if b["edge"] is not None else "",
                        b["outcome"], b["win"], f"{b['pnl']:.4f}",
                        f"{equity:.2f}", f"{dd:.2f}"])


def _write_summary(d):
    with open("data/track_record_summary.json", "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # evita UnicodeEncodeError en consolas Windows legacy
    except Exception:
        pass
    main()
