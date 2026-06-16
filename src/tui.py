"""
TUI estilo Bloomberg para Mibot.

Denso, amber sobre oscuro, multi-panel. Muestra que el bot esta VIVO (escaneando),
ACTIVO en shadow (tomando trades paper) y APRENDIENDO (calibrando), con el ledger
W/L, win rate y Brier calculados en tiempo real.

render(state, pf, engine, cal, vol) -> Layout (se reconstruye cada frame).
"""
import time
from datetime import datetime, timezone

from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

import config

AMBER = "orange1"
BG = "on grey3"
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BARS = "▁▂▃▄▅▆▇█"


# ---------- helpers ----------
def _money(x, signed=False):
    if x is None:
        return "-"
    s = "+" if (signed and x >= 0) else ""
    return f"{s}${x:,.2f}"


def _pct(x, signed=False):
    if x is None:
        return "-"
    s = "+" if (signed and x >= 0) else ""
    return f"{s}{x*100:.1f}%"


def _col(x):
    return "green3" if (x or 0) >= 0 else "red1"


def _mmss(sec):
    if sec is None or sec < 0:
        return "--:--"
    return f"{int(sec)//60:02d}:{int(sec)%60:02d}"


def _ago(ts):
    if not ts:
        return "nunca"
    d = time.time() - ts
    if d < 60:
        return f"hace {int(d)}s"
    return f"hace {int(d)//60}m"


def _spark(values):
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return Text("·" * 10, style="grey50")
    lo, hi = min(vals), max(vals)
    rng = hi - lo or 1
    out = "".join(BARS[min(7, int((v - lo) / rng * 7))] for v in vals[-40:])
    c = _col(vals[-1] - vals[0])
    return Text(out, style=c)


def _kv(label, value, vstyle="bold white"):
    t = Text()
    t.append(f"{label} ", style="grey62")
    t.append(value, style=vstyle)
    return t


# ---------- top status bar ----------
def _topbar(state, engine, cal):
    spin = SPIN[engine.ticks % len(SPIN)]
    fresh = (state.spot_age_ms("BTCUSDT") or 9e9) < 4000
    analyzing = (time.time() - engine.last_tick_ts) < 3 if engine.last_tick_ts else False
    learning = cal.stats()["n"] > 0

    badges = Text()
    badges.append(" ● EN VIVO " if fresh else " ○ SIN DATOS ",
                  style="bold black on green3" if fresh else "bold white on red1")
    badges.append("  ◆ SHADOW ", style="bold black on orange1")
    badges.append(f" {spin} ANALIZANDO " if analyzing else "  PAUSADO ",
                  style="bold black on deep_sky_blue1" if analyzing else "grey50")
    badges.append(" 🧠 APRENDIENDO " if learning else " 🧠 juntando datos ",
                  style="bold black on medium_purple1" if learning else "grey50")

    ticker = Text("   ")
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"):
        mid = state.spot_mid(sym)
        ticker.append(f"{sym[:3]} ", style=AMBER)
        ticker.append(f"{mid:,.1f}  " if mid else "----  ", style="bold white")

    clock = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    up = int(time.time() - engine.started)
    right = Text(f"scan {engine.scanning}  ticks {engine.ticks}  ↑ {up//3600:02d}:{(up%3600)//60:02d}:{up%60:02d}  {clock} ",
                 style="grey70")

    grid = Table.grid(expand=True)
    grid.add_column(justify="left")
    grid.add_column(justify="right")
    grid.add_row(badges, right)
    grid.add_row(ticker, Text(""))
    return Panel(grid, box=box.HEAVY, border_style=AMBER, style=BG,
                 title="[bold orange1]M I B O T[/]  ·  POLYMARKET BTC UP/DOWN  ·  PAPER / SHADOW TRAINING",
                 title_align="left")


# ---------- account ----------
def _account(pf):
    s = pf.stats()
    g = Table.grid(expand=True, padding=(0, 1))
    for _ in range(3):
        g.add_column(justify="left", ratio=1)
    g.add_row(
        _kv("EQUITY", _money(s["equity"]), "bold cyan1"),
        _kv("P&L", _money(s["realized"], True), f"bold {_col(s['realized'])}"),
        _kv("ROI", _pct(s["roi"], True), f"bold {_col(s['roi'])}"),
    )
    g.add_row(
        _kv("cash", _money(s["cash"])),
        _kv("expuesto", _money(s["open_exposure"])),
        _kv("abiertas", str(s["n_open"])),
    )
    g.add_row(
        _kv("P&L día", _money(s["day_pnl"], True), f"bold {_col(s['day_pnl'])}"),
        _kv("límite día", _pct(-config.DAILY_LOSS_LIMIT_PCT)),
        Text(" ⛔ STOP" if s["halted"] else " ● activo",
             style="bold red1" if s["halted"] else "green3"),
    )
    spark = _spark(list(pf.equity_hist))
    body = Table.grid(expand=True)
    body.add_row(g)
    body.add_row(Text("equity ", style="grey62") + spark)
    return Panel(body, title="[bold orange1]CUENTA[/]", title_align="left",
                 border_style="grey35", style=BG, box=box.SQUARE)


# ---------- performance + learning ----------
def _perf(pf, cal, vol):
    s = pf.stats()
    cs = cal.stats()
    sigma = vol.sigma_per_sec
    sig5 = (sigma * (300 ** 0.5) * 100) if sigma else None

    wl = Text()
    wl.append(f"{s['wins']}", style="bold green3")
    wl.append(" W - ", style="grey62")
    wl.append(f"{s['losses']}", style="bold red1")
    wl.append(" L", style="grey62")
    if s["streak"]:
        stk = s["streak"]
        wl.append(f"   racha {'+' if stk>0 else ''}{stk}",
                  style=f"bold {_col(stk)}")

    bet_brier = f"{s['brier']:.4f}" if s["brier"] is not None else "—"
    mdl_brier = f"{cs['model_brier']:.4f}" if cs["model_brier"] is not None else f"n={cs['n']}"
    wr = _pct(s["win_rate"]) if s["win_rate"] is not None else "—"

    g = Table.grid(expand=True, padding=(0, 1))
    for _ in range(2):
        g.add_column(justify="left", ratio=1)
    g.add_row(_kv("WIN RATE", wr, "bold cyan1"), wl)
    g.add_row(_kv("liquidadas", str(s["n_settled"])),
              _kv("apuestas", f"{s['n_bets']}  ↑{s['up_bets']}/↓{s['down_bets']}"))
    g.add_row(_kv("Brier apuestas", bet_brier, "bold white"),
              _kv("Brier modelo", mdl_brier, "bold white"))
    g.add_row(_kv("vol BTC 5m", f"{sig5:.2f}%" if sig5 else "calentando…"),
              _kv("calibración k", f"{cs['k']:.3f}", "bold medium_purple1"))
    g.add_row(_kv("recalibraciones", str(cs["n_recalibs"])),
              _kv("últ. aprendizaje", _ago(cs["last_recalib_ts"])))

    note = Text("Brier: 0=perfecto · 0.25=azar · menor es mejor", style="grey50 italic")
    body = Table.grid(expand=True)
    body.add_row(g)
    body.add_row(note)
    return Panel(body, title="[bold orange1]PERFORMANCE  ·  APRENDIZAJE[/]", title_align="left",
                 border_style="grey35", style=BG, box=box.SQUARE)


# ---------- scan table (lo que esta analizando) ----------
def _scan(engine):
    t = Table(expand=True, box=box.SIMPLE_HEAD, style=BG, header_style=f"bold {AMBER}",
              pad_edge=False)
    t.add_column("MERCADO", justify="left", no_wrap=True)
    t.add_column("cierra", justify="right")
    t.add_column("mov%", justify="right")
    t.add_column("FAIR", justify="right")
    t.add_column("impl ↑", justify="right")
    t.add_column("impl ↓", justify="right")
    t.add_column("edge ↑", justify="right")
    t.add_column("edge ↓", justify="right")
    t.add_column("SEÑAL", justify="center")

    evs = sorted(engine.last_evals.values(), key=lambda e: e["tau"] or 9e9)
    if not evs:
        t.add_row("[grey50]esperando mercados BTC activos + warm-up de volatilidad…[/]",
                  "", "", "", "", "", "", "", "")
    for ev in evs[:9]:
        eu, ed = ev["edge_up"], ev["edge_dn"]
        sig = Text("—", style="grey50")
        if eu is not None and eu >= config.MIN_EDGE and eu <= config.MAX_EDGE_TRUST:
            sig = Text("▲ UP", style="bold black on green3")
        elif ed is not None and ed >= config.MIN_EDGE and ed <= config.MAX_EDGE_TRUST:
            sig = Text("▼ DOWN", style="bold black on red1")
        name = ev["slug"].replace("-updown", "").rsplit("-", 1)[0]
        mov = ev["m"] * 100
        t.add_row(
            f"{name} [grey62]{ev['interval']}[/]",
            _mmss(ev["tau"]),
            Text(f"{mov:+.3f}", style=_col(mov)),
            f"[cyan1]{ev['fair_p']:.3f}[/]",
            f"{ev['mid_up']:.2f}" if ev["mid_up"] else "—",
            f"{ev['mid_dn']:.2f}" if ev["mid_dn"] else "—",
            Text(_pct(eu, True) if eu is not None else "—", style=_col(eu) if eu is not None else "grey50"),
            Text(_pct(ed, True) if ed is not None else "—", style=_col(ed) if ed is not None else "grey50"),
            sig,
        )
    return Panel(t, title="[bold orange1]ANÁLISIS EN VIVO  ·  ESCANEANDO MERCADOS[/]",
                 title_align="left", border_style="grey35", style=BG, box=box.SQUARE)


# ---------- order book ----------
def _book_table(state, token, accent):
    t = Table(box=box.SIMPLE, expand=True, show_edge=False, pad_edge=False,
              style=BG, header_style=f"{accent}")
    t.add_column("bid", justify="right")
    t.add_column("tam", justify="right")
    t.add_column("ask", justify="left")
    t.add_column("tam", justify="left")
    ob = state.obooks.get(token) if token else None
    if not ob:
        t.add_row("—", "—", "—", "—")
        return t
    bids, asks = ob.depth(5)
    for i in range(max(len(bids), len(asks), 1)):
        bp, bs = bids[i] if i < len(bids) else ("", "")
        ap, az = asks[i] if i < len(asks) else ("", "")
        t.add_row(
            f"[green3]{bp:.2f}[/]" if bp != "" else "",
            f"{bs:,.0f}" if bs != "" else "",
            f"[red1]{ap:.2f}[/]" if ap != "" else "",
            f"{az:,.0f}" if az != "" else "",
        )
    return t


def _books(state, engine):
    evs = [e for e in engine.last_evals.values() if e["tau"] and e["tau"] > 0]
    if not evs:
        return Panel(Text("sin mercado en foco", style="grey50"),
                     title="[bold orange1]ORDER BOOK[/]", title_align="left",
                     border_style="grey35", style=BG, box=box.SQUARE)
    ev = min(evs, key=lambda e: e["tau"])
    g = Table.grid(expand=True, padding=(0, 1))
    g.add_column(ratio=1)
    g.add_column(ratio=1)
    up = Panel(_book_table(state, ev["up_token"], "green3"), title="[bold green3]UP[/]",
               border_style="green3", style=BG, box=box.MINIMAL)
    dn = Panel(_book_table(state, ev["dn_token"], "red1"), title="[bold red1]DOWN[/]",
               border_style="red1", style=BG, box=box.MINIMAL)
    g.add_row(up, dn)
    name = ev["slug"].replace("-updown", "")
    return Panel(g, title=f"[bold orange1]ORDER BOOK[/]  [grey62]{name} · cierra {_mmss(ev['tau'])}[/]",
                 title_align="left", border_style="grey35", style=BG, box=box.SQUARE)


# ---------- open positions ----------
def _positions(pf):
    t = Table(expand=True, box=box.SIMPLE_HEAD, style=BG, header_style=f"bold {AMBER}", pad_edge=False)
    t.add_column("mercado", no_wrap=True); t.add_column("lado")
    t.add_column("px", justify="right"); t.add_column("$", justify="right")
    t.add_column("p", justify="right"); t.add_column("edge", justify="right")
    if not pf.positions:
        t.add_row("[grey50]sin posiciones abiertas[/]", "", "", "", "", "")
    for p in list(pf.positions.values())[:7]:
        c = "green3" if p.side == "up" else "red1"
        arrow = "▲up" if p.side == "up" else "▼dn"
        t.add_row(p.slug.replace("-updown", "").rsplit("-", 1)[0],
                  f"[{c}]{arrow}[/]", f"{p.price:.2f}", f"{p.stake:.0f}",
                  f"{p.fair_p:.2f}", Text(_pct(p.edge, True), style=_col(p.edge)))
    return Panel(t, title="[bold orange1]POSICIONES ABIERTAS[/]", title_align="left",
                 border_style="grey35", style=BG, box=box.SQUARE)


# ---------- W/L ledger ----------
def _ledger(pf):
    t = Table(expand=True, box=box.SIMPLE_HEAD, style=BG, header_style=f"bold {AMBER}", pad_edge=False)
    t.add_column("res", justify="center"); t.add_column("mercado", no_wrap=True)
    t.add_column("lado"); t.add_column("→"); t.add_column("P&L", justify="right")
    if not pf.recent:
        t.add_row("—", "[grey50]aún sin liquidar[/]", "", "", "")
    for r in list(pf.recent)[:9]:
        badge = Text(" W ", style="bold black on green3") if r["win"] else Text(" L ", style="bold black on red1")
        t.add_row(badge, r["slug"].split("-")[0],
                  ("▲up" if r["side"] == "up" else "▼dn"),
                  r["outcome"],
                  Text(_money(r["pnl"], True), style=_col(r["pnl"])))
    title = f"[bold orange1]HISTORIAL W/L[/]  [green3]{pf.wins}W[/][grey62]-[/][red1]{pf.losses}L[/]"
    return Panel(t, title=title, title_align="left", border_style="grey35", style=BG, box=box.SQUARE)


# ---------- decision log ----------
def _log(engine):
    lines = []
    for ln in list(engine.log)[:10]:
        style = "green3" if ln.startswith("BET") else ("cyan1" if ln.startswith("RESOLVE") else "grey70")
        lines.append(Text(ln, style=style))
    if not lines:
        lines = [Text("sin decisiones todavía — analizando…", style="grey50 italic")]
    body = Text("\n").join(lines)
    return Panel(body, title="[bold orange1]LOG DE DECISIONES[/]", title_align="left",
                 border_style="grey35", style=BG, box=box.SQUARE)


# ---------- ensamblado ----------
def render(state, pf, engine, cal, vol):
    root = Layout()
    root.split_column(
        Layout(name="top", size=4),
        Layout(name="mid"),
        Layout(name="bottom", size=12),
    )
    root["mid"].split_row(Layout(name="left", ratio=5), Layout(name="right", ratio=4))
    root["left"].split_column(Layout(name="account", size=9), Layout(name="scan"))
    root["right"].split_column(Layout(name="perf", size=12), Layout(name="books"))
    root["bottom"].split_row(
        Layout(name="positions", ratio=1),
        Layout(name="ledger", ratio=1),
        Layout(name="log", ratio=1),
    )

    root["top"].update(_topbar(state, engine, cal))
    root["account"].update(_account(pf))
    root["scan"].update(_scan(engine))
    root["perf"].update(_perf(pf, cal, vol))
    root["books"].update(_books(state, engine))
    root["positions"].update(_positions(pf))
    root["ledger"].update(_ledger(pf))
    root["log"].update(_log(engine))
    return root
