"""
dashboard.py — dashboard público de métricas (genera data/dashboard.html).

El informe de DSN pide un "dashboard público de métricas" para construir confianza
ante inversores y comunidad. Esto arma una página HTML autocontenida (sin librerías
externas, abrible offline o publicable) desde lo que ya producen track_record.py y
audit.py:

  - tarjetas con los KPIs (PnL, ROI, win rate, drawdown, Brier, skill vs mercado)
  - el Brier Composite Score con sus 6 componentes ponderados
  - curva de equity (SVG inline) y breakdown por activo
  - el sello de auditoría (hash de la cadena) — prueba de que el track no se editó

Requiere correr antes:
    venv\\Scripts\\python.exe track_record.py   # genera el summary + csv
    venv\\Scripts\\python.exe audit.py          # sella la cadena (opcional pero recomendado)

Uso:
    venv\\Scripts\\python.exe dashboard.py
    -> abrir data/dashboard.html
"""
import csv
import json
import os

SUMMARY = "data/track_record_summary.json"
CSV_PATH = "data/track_record.csv"
CHAIN = "audit_chain.jsonl"
OUT = "data/dashboard.html"


def _pct(x, signed=True):
    if x is None:
        return "s/d"
    return f"{x*100:+.2f}%" if signed else f"{x*100:.2f}%"


def _money(x, signed=True):
    if x is None:
        return "s/d"
    s = "+" if (signed and x >= 0) else ""
    return f"{s}${x:,.2f}"


def _equity_svg(points, w=720, h=220, pad=8):
    """Polilínea SVG de la curva de equity (y opcional banda de drawdown)."""
    if len(points) < 2:
        return '<p class="muted">sin datos de equity</p>'
    ys = [p for p in points]
    lo, hi = min(ys), max(ys)
    rng = (hi - lo) or 1.0
    n = len(ys)
    def X(i):
        return pad + i * (w - 2 * pad) / (n - 1)
    def Y(v):
        return pad + (h - 2 * pad) * (1 - (v - lo) / rng)
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(ys))
    base = ys[0]
    color = "#1d9e75" if ys[-1] >= base else "#e24b4a"
    zero_y = Y(base)
    return (
        f'<svg viewBox="0 0 {w} {h}" width="100%" preserveAspectRatio="none" '
        f'style="display:block">'
        f'<line x1="{pad}" y1="{zero_y:.1f}" x2="{w-pad}" y2="{zero_y:.1f}" '
        f'stroke="#888" stroke-dasharray="4 4" stroke-width="1" opacity="0.5"/>'
        f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2"/>'
        f'</svg>'
    )


def _card(label, value, color=""):
    style = f' style="color:{color}"' if color else ""
    return (f'<div class="card"><div class="lbl">{label}</div>'
            f'<div class="val"{style}>{value}</div></div>')


def _bar(label, score, weight, value_str):
    pct = max(0, min(100, score * 100))
    col = "#1d9e75" if score >= 0.6 else ("#ba7517" if score >= 0.3 else "#e24b4a")
    return (
        f'<div class="brow">'
        f'<div class="bname">{label} <span class="muted">({weight*100:.0f}%)</span></div>'
        f'<div class="btrack"><div class="bfill" style="width:{pct:.0f}%;background:{col}"></div></div>'
        f'<div class="bval">{value_str} · <b>{score:.2f}</b></div>'
        f'</div>'
    )


def main():
    if not os.path.exists(SUMMARY):
        print(f"Falta {SUMMARY}. Corré primero: venv\\Scripts\\python.exe track_record.py")
        return
    d = json.load(open(SUMMARY, encoding="utf-8"))

    # curva de equity desde el CSV
    equity = []
    if os.path.exists(CSV_PATH):
        with open(CSV_PATH, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    equity.append(float(row["equity"]))
                except (KeyError, ValueError):
                    pass

    # sello de auditoría (última línea de la cadena)
    seal = None
    if os.path.exists(CHAIN):
        lines = [l for l in open(CHAIN, encoding="utf-8") if l.strip()]
        if lines:
            seal = json.loads(lines[-1])

    sc = d["scores"]; wt = d["pesos"]
    comp = d["brier_composite_score"]
    comp_col = "#1d9e75" if comp >= 0.6 else ("#ba7517" if comp >= 0.4 else "#e24b4a")
    roi = d["roi"]; pnl = d["pnl_neto"]
    skill = d.get("skill_vs_mercado")
    skill_txt = (f"{skill:+.4f}" if skill is not None else "s/d")
    skill_col = "#1d9e75" if (skill or 0) > 0 else "#e24b4a"

    cards = "".join([
        _card("PnL neto", _money(pnl), "#1d9e75" if pnl >= 0 else "#e24b4a"),
        _card("ROI", _pct(roi), "#1d9e75" if roi >= 0 else "#e24b4a"),
        _card("Win rate", _pct(d["win_rate"], signed=False)),
        _card("Precio medio pagado", f"{d['precio_medio_pagado']:.3f}"),
        _card("Drawdown máx", _pct(d["drawdown_max_frac"], signed=False),
              "#e24b4a" if d["drawdown_max_frac"] > 0.15 else ""),
        _card("Brier (bot)", f"{d['brier_bot']:.4f}"),
        _card("Skill vs mercado", skill_txt, skill_col),
        _card("Apuestas / Predicciones", f"{d['n_bets']:,} / {d['n_predicciones']:,}"),
    ])

    bars = "".join([
        _bar("Brier Score", sc["brier"], wt["brier"], f"{d['brier_bot']:.4f}"),
        _bar("Rentabilidad neta", sc["rentabilidad"], wt["rentabilidad"], _money(pnl)),
        _bar("Drawdown", sc["drawdown"], wt["drawdown"], _pct(d["drawdown_max_frac"], False)),
        _bar("Cantidad predicciones", sc["predicciones"], wt["predicciones"], f"{d['n_predicciones']:,}"),
        _bar("Liquidez operable", sc["liquidez"], wt["liquidez"],
             f"{d['liquidez_book_mediana']:.0f}" if d.get("liquidez_book_mediana") else "s/d"),
        _bar("Consistencia temporal", sc["consistencia"], wt["consistencia"],
             f"{d['cohortes_positivas']}/{d['n_cohortes']}"),
    ])

    seal_html = ""
    if seal:
        seal_html = (
            f'<div class="seal"><b>Sello de auditoría</b> · checkpoint #{seal["checkpoint"]} · '
            f'{seal["generated_utc"]} UTC · {seal["n_predictions"]:,} predicciones selladas<br>'
            f'<code>chain_hash: {seal["chain_hash"]}</code><br>'
            f'<span class="muted">Verificable con <code>audit.py --verify</code>. '
            f'Cualquier edición de una predicción pasada rompe el hash.</span></div>'
        )

    html = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mibot · Track Record</title>
<style>
:root {{ color-scheme: light dark; }}
* {{ box-sizing: border-box; }}
body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0;
  background: #0f0f0e; color: #eceae3; padding: 24px; line-height: 1.5; }}
.wrap {{ max-width: 820px; margin: 0 auto; }}
h1 {{ font-size: 22px; font-weight: 600; margin: 0 0 2px; }}
.sub {{ color: #9a988f; font-size: 13px; margin-bottom: 20px; }}
.muted {{ color: #9a988f; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px,1fr)); gap: 10px; margin-bottom: 22px; }}
.card {{ background: #1a1a18; border: 1px solid #2c2c2a; border-radius: 10px; padding: 12px 14px; }}
.card .lbl {{ color: #9a988f; font-size: 12px; }}
.card .val {{ font-size: 21px; font-weight: 600; margin-top: 2px; }}
h2 {{ font-size: 15px; font-weight: 600; margin: 24px 0 10px; }}
.composite {{ background:#1a1a18; border:1px solid #2c2c2a; border-radius:10px; padding:16px; }}
.cscore {{ font-size: 34px; font-weight: 700; color: {comp_col}; }}
.brow {{ display:grid; grid-template-columns: 200px 1fr 150px; gap:10px; align-items:center; margin:7px 0; font-size:13px; }}
.btrack {{ background:#2c2c2a; border-radius:6px; height:14px; overflow:hidden; }}
.bfill {{ height:100%; }}
.bval {{ text-align:right; color:#cfcdc5; }}
.eq {{ background:#1a1a18; border:1px solid #2c2c2a; border-radius:10px; padding:14px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th, td {{ text-align:right; padding:6px 8px; border-bottom:1px solid #2c2c2a; }}
th:first-child, td:first-child {{ text-align:left; }}
.seal {{ margin-top:22px; background:#15150f; border:1px solid #3c3409; border-radius:10px; padding:12px 14px; font-size:12px; }}
code {{ font-family: ui-monospace, Consolas, monospace; color:#d0a85a; word-break:break-all; }}
.foot {{ color:#9a988f; font-size:11px; margin-top:18px; }}
</style></head>
<body><div class="wrap">
<h1>Mibot · Track Record</h1>
<div class="sub">Shadow training sobre Polymarket Up/Down (BTC·ETH·SOL·XRP) ·
  período {d['periodo']['desde']} → {d['periodo']['hasta']} UTC · generado {d['generado']} UTC</div>

<div class="grid">{cards}</div>

<h2>Brier Composite Score <span class="muted">(criterio DSN Empresas)</span></h2>
<div class="composite">
  <div class="cscore">{comp:.2f} <span style="font-size:16px;color:#9a988f">/ 1.00</span></div>
  <div style="margin-top:12px">{bars}</div>
</div>

<h2>Curva de equity</h2>
<div class="eq">{_equity_svg(equity)}</div>

<h2>Skill real vs mercado</h2>
<div class="composite" style="font-size:14px">
  Brier bot <b>{d['brier_bot']:.4f}</b> vs Brier mercado <b>{d['brier_mercado']:.4f}</b> ·
  skill <b style="color:{skill_col}">{skill_txt}</b>
  <div class="muted" style="margin-top:6px">
  Positivo = el bot le gana al precio de mercado en las apuestas tomadas.
  Mide habilidad real, no el Brier crudo (que mide al mercado).</div>
</div>

{seal_html}

<div class="foot">Documento generado automáticamente por dashboard.py. Métricas
auditables: track_record.csv (detalle por apuesta) + audit_chain.jsonl (sello).</div>
</div></body></html>"""

    os.makedirs("data", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Dashboard generado: {OUT}")
    print(f"  Composite: {comp:.2f}/1.00 · PnL {_money(pnl)} · ROI {_pct(roi)} · "
          f"skill {skill_txt}")
    print(f"  Abrir: start data\\dashboard.html  (o doble click)")


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
