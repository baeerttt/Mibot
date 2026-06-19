"""
backtest.py — simula la estrategia sobre las predicciones historicas ya guardadas.

En vez de esperar dias operando en vivo, replaya las ~21k predicciones etiquetadas
de la DB y simula que habria hecho la estrategia bajo una config dada. Permite
probar umbrales, sizing, costos de ejecucion, seleccion de activos/intervalos y
filtro de correlacion en SEGUNDOS, no dias.

Clave del replay:
  - cada fila de predictions tiene fair_p, edge_up/down, spot, tau, outcome.
  - el ask realmente cotizado se recupera:  ask = p_lado - edge_lado.
  - se apuesta UNA vez por mercado (la primera vez que pasa el gate), igual que
    el bot en vivo (ONE_BET_PER_MARKET).
  - el outcome real ya esta etiquetado -> PnL deterministico.

Costo de ejecucion: 'cost' se suma al ask (en unidades de precio, ej 0.005 = medio
centavo) para modelar spread/slippage que el fill optimista de hoy ignora. Es el
parametro que revela si el edge sobrevive a la ejecucion real.

Uso:
  venv\\Scripts\\python.exe backtest.py
"""
import sqlite3
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import config
from src import fees as fees_mod

DB_PATH = config.DB_PATH


@dataclass
class BTConfig:
    name: str
    min_edge: float = 0.04
    max_edge_trust: float = 0.25
    kelly: float = 0.25
    max_bet_pct: float = 0.02
    min_bet: float = 5.0
    min_time_left: float = 30.0
    cost: float = 0.0                      # slippage/costo sumado al ask (precio)
    real_fee: bool = False                 # True: suma el taker fee real de Polymarket (cripto)
    dead_zone: float = 0.0                 # distancia a 0.50 a EVITAR (0.05 = no operar ask en [0.45,0.55])
    assets: tuple = ("btc", "eth", "sol", "xrp")
    intervals: tuple = ("5m", "15m")
    max_concurrent: int = 10
    daily_loss_pct: float = 0.20
    max_corr_per_dir: int = 99             # tope de apuestas mismo sentido a la vez
    bankroll: float = 10_000.0


@dataclass
class BTResult:
    name: str
    equity: float
    pnl: float
    n_bets: int
    wins: int
    brier_sum: float = 0.0
    peak: float = 0.0
    max_dd: float = 0.0
    fees_paid: float = 0.0
    by_asset: dict = field(default_factory=dict)
    by_interval: dict = field(default_factory=dict)

    @property
    def win_rate(self):
        return self.wins / self.n_bets if self.n_bets else None

    @property
    def brier(self):
        return self.brier_sum / self.n_bets if self.n_bets else None

    @property
    def roi(self):
        return self.pnl / (self.equity - self.pnl) if (self.equity - self.pnl) else 0.0


def load_predictions(db_path=DB_PATH):
    """Trae las predicciones etiquetadas, ordenadas por tiempo."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT ts, slug, asset, fair_p, edge_up, edge_down, tau, outcome "
        "FROM predictions WHERE outcome IS NOT NULL AND fair_p IS NOT NULL "
        "AND edge_up IS NOT NULL AND edge_down IS NOT NULL "
        "ORDER BY ts ASC").fetchall()
    conn.close()
    return rows


def _utc_day(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).date()


def simulate(rows, cfg: BTConfig) -> BTResult:
    res = BTResult(cfg.name, cfg.bankroll, 0.0, 0, 0, peak=cfg.bankroll)
    equity = cfg.bankroll
    realized = 0.0
    bet_slugs = set()
    open_bets = []          # (settle_ts, pnl)
    open_dir = {"up": 0, "down": 0}
    day = None
    day_start_realized = 0.0

    def settle_due(now_ts):
        nonlocal realized, equity
        still = []
        for settle_ts, pnl, side in open_bets:
            if settle_ts <= now_ts:
                realized += pnl
                equity = cfg.bankroll + realized
                open_dir[side] -= 1
                res.peak = max(res.peak, equity)
                res.max_dd = max(res.max_dd, res.peak - equity)
            else:
                still.append((settle_ts, pnl, side))
        open_bets[:] = still

    for ts, slug, asset, fair_p, edge_up, edge_dn, tau, outcome in rows:
        t = ts / 1000.0
        settle_due(t)

        # reset diario del circuit-breaker
        d = _utc_day(ts)
        if d != day:
            day = d
            day_start_realized = realized
        day_pnl = realized - day_start_realized
        if day_pnl <= -cfg.daily_loss_pct * cfg.bankroll:
            continue
        if len(open_bets) >= cfg.max_concurrent:
            continue

        interval = slug.split("-")[2] if len(slug.split("-")) > 2 else "?"
        if asset not in cfg.assets or interval not in cfg.intervals:
            continue
        if slug in bet_slugs:
            continue
        if tau < cfg.min_time_left:
            continue

        # elegir el mejor lado
        if edge_up >= edge_dn:
            side, p_side, edge = "up", fair_p, edge_up
        else:
            side, p_side, edge = "down", 1 - fair_p, edge_dn

        ask = p_side - edge                 # ask realmente cotizado
        # dead-zone: evitar apostar cerca de 0.50 (ahi el fee real es maximo)
        if cfg.dead_zone > 0 and abs(ask - 0.5) < cfg.dead_zone:
            continue
        fee_ps = fees_mod.fee_per_share(ask) if cfg.real_fee else 0.0
        # edge NETO (despues de costo+fee) es el que tiene que superar el umbral
        net_edge = edge - cfg.cost - fee_ps
        if net_edge < cfg.min_edge or edge > cfg.max_edge_trust:
            continue
        if open_dir[side] >= cfg.max_corr_per_dir:
            continue

        ask_eff = ask + cfg.cost + fee_ps   # fill conservador (paga ask+costo+fee)
        if ask_eff <= 0.01 or ask_eff >= 0.99:
            continue
        f_star = (p_side - ask_eff) / (1 - ask_eff)
        if f_star <= 0:                     # el costo se comio el edge
            continue
        frac = max(0.0, min(cfg.kelly * f_star, cfg.max_bet_pct))
        stake = equity * frac
        if stake < cfg.min_bet:
            continue
        shares = stake / ask_eff
        fee_paid = fees_mod.taker_fee(shares, ask) if cfg.real_fee else 0.0

        win = (side == outcome)
        pnl = (shares * (1 - ask_eff) if win else -stake)

        bet_slugs.add(slug)
        open_dir[side] += 1
        open_bets.append((t + tau, pnl, side))

        res.n_bets += 1
        res.wins += 1 if win else 0
        res.fees_paid += fee_paid
        res.brier_sum += (p_side - (1.0 if win else 0.0)) ** 2
        a = res.by_asset.setdefault(asset, [0, 0, 0.0])
        a[0] += 1; a[1] += 1 if win else 0; a[2] += pnl
        iv = res.by_interval.setdefault(interval, [0, 0, 0.0])
        iv[0] += 1; iv[1] += 1 if win else 0; iv[2] += pnl

    # liquidar lo que quede
    settle_due(float("inf"))
    res.equity = cfg.bankroll + realized
    res.pnl = realized
    return res


def _fmt(res: BTResult):
    wr = f"{res.win_rate*100:4.1f}%" if res.win_rate is not None else "  -  "
    br = f"{res.brier:.3f}" if res.brier is not None else "  -  "
    fee_str = f"  fee={res.fees_paid:6.0f}" if res.fees_paid else ""
    return (f"{res.name:<30} pnl={res.pnl:+9.2f}  eq={res.equity:9.2f}  "
            f"bets={res.n_bets:4d}  wr={wr}  brier={br}  maxDD={res.max_dd:7.0f}{fee_str}")


def main():
    rows = load_predictions()
    distinct = len(set(r[1] for r in rows))
    horas = (rows[-1][0] - rows[0][0]) / 1000 / 3600 if rows else 0
    print(f"Predicciones: {len(rows)}  ·  mercados distintos: {distinct}  ·  {horas:.1f}h de datos")
    if distinct < 500:
        print("⚠  MUESTRA CHICA: con < 500 mercados los resultados son RUIDO, no señal.")
        print("   No saques conclusiones; re-corré esto cuando haya más días de datos.")
    print("=" * 100)

    presets = [
        BTConfig("Agresivo actual (cost 0)", min_edge=0.02, kelly=0.40, max_bet_pct=0.05),
        BTConfig("Produccion BTC/ETH c0",   min_edge=0.04, kelly=0.25, max_bet_pct=0.02,
                 assets=("btc", "eth"), max_concurrent=4, daily_loss_pct=0.08),
        BTConfig("Produccion + cost 0.5c",  min_edge=0.04, kelly=0.25, max_bet_pct=0.02,
                 assets=("btc", "eth"), max_concurrent=4, daily_loss_pct=0.08, cost=0.005),
        BTConfig("Produccion + cost 1c",    min_edge=0.04, kelly=0.25, max_bet_pct=0.02,
                 assets=("btc", "eth"), max_concurrent=4, daily_loss_pct=0.08, cost=0.010),
        BTConfig("Solo 15m BTC/ETH c1",     min_edge=0.04, kelly=0.25, max_bet_pct=0.02,
                 assets=("btc", "eth"), intervals=("15m",), max_concurrent=4,
                 daily_loss_pct=0.08, cost=0.010),
        BTConfig("Solo BTC 15m c1",         min_edge=0.04, kelly=0.25, max_bet_pct=0.02,
                 assets=("btc",), intervals=("15m",), max_concurrent=4,
                 daily_loss_pct=0.08, cost=0.010),
        BTConfig("Solo BTC 15m edge8 c1",   min_edge=0.08, kelly=0.25, max_bet_pct=0.02,
                 assets=("btc",), intervals=("15m",), max_concurrent=4,
                 daily_loss_pct=0.08, cost=0.010),
    ]
    results = [simulate(rows, c) for c in presets]
    for r in results:
        print(_fmt(r))

    print("\n" + "=" * 100)
    print("SWEEP: min_edge con costo realista (1c), BTC/ETH, 15m\n")
    for me in (0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10):
        r = simulate(rows, BTConfig(f"  min_edge={me:.2f}", min_edge=me, kelly=0.25,
                     max_bet_pct=0.02, assets=("btc", "eth"), intervals=("15m",),
                     max_concurrent=4, daily_loss_pct=0.08, cost=0.010))
        print(_fmt(r))

    print("\n" + "=" * 100)
    print("Mejor preset — desglose por activo e intervalo:\n")
    best = max(results, key=lambda r: r.pnl)
    print(f">>> {best.name}  (pnl {best.pnl:+.2f})")
    for asset, (n, w, pnl) in sorted(best.by_asset.items(), key=lambda x: -x[1][2]):
        print(f"   {asset:4} {n:4d} bets  {w:3d}W  wr={w/n*100:4.1f}%  pnl={pnl:+9.2f}")
    for iv, (n, w, pnl) in sorted(best.by_interval.items(), key=lambda x: -x[1][2]):
        print(f"   {iv:4} {n:4d} bets  {w:3d}W  wr={w/n*100:4.1f}%  pnl={pnl:+9.2f}")

    print("\n" + "=" * 100)
    print("#1 FILTRO DE CORRELACION: mismo preset, tope de apuestas misma direccion\n")
    base = dict(min_edge=0.04, kelly=0.25, max_bet_pct=0.02, assets=("btc", "eth"),
                max_concurrent=4, daily_loss_pct=0.08, cost=0.010)
    for cap in (1, 2, 3, 99):
        cap_name = "sin tope" if cap == 99 else f"max {cap}/dir"
        r = simulate(rows, BTConfig(f"  corr {cap_name}", max_corr_per_dir=cap, **base))
        print(_fmt(r))
    print("  (mirá maxDD: el filtro baja la caida maxima aunque el PnL sea ruido)")

    print("\n" + "=" * 100)
    print("#3 TIME-DECAY: win rate del lado elegido por franja de tau (candidatos edge>=4c)\n")
    tau_analysis(rows, min_edge=0.04, cost=0.010)

    print("\n" + "=" * 100)
    print("#4 RENTABILIDAD CON FEE REAL (taker fee Polymarket cripto, max 1.80% en p=0.50)\n")
    fee_sweeps(rows)


def fee_sweeps(rows):
    """Barrido para encontrar la config mas rentable DESPUES del fee real:
    por activo, por banda de edge, y por dead-zone alrededor de 0.50."""
    base = dict(kelly=0.25, max_bet_pct=0.02, intervals=("15m",), max_concurrent=4,
                daily_loss_pct=0.08, cost=0.010, real_fee=True)

    print("Por activo (solo, min_edge=0.04, 15m, fee real):\n")
    by_asset_results = []
    for a in ("btc", "eth", "sol", "xrp"):
        r = simulate(rows, BTConfig(f"  solo {a}", min_edge=0.04, assets=(a,), **base))
        by_asset_results.append((a, r))
        print(_fmt(r))

    print("\nSweep de banda de edge (min_edge), BTC+ETH, fee real:\n")
    band_results = []
    for me in (0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.12):
        r = simulate(rows, BTConfig(f"  min_edge={me:.2f}", min_edge=me,
                     assets=("btc", "eth"), **base))
        band_results.append((me, r))
        print(_fmt(r))

    print("\nSweep de dead-zone alrededor de 0.50 (BTC+ETH, min_edge=0.04, fee real):\n")
    dz_results = []
    for dz in (0.0, 0.02, 0.05, 0.08, 0.10, 0.15):
        r = simulate(rows, BTConfig(f"  dead_zone={dz:.2f}", min_edge=0.04,
                     dead_zone=dz, assets=("btc", "eth"), **base))
        dz_results.append((dz, r))
        print(_fmt(r))

    print("\n" + "=" * 100)
    print("MEJOR COMBINACION ENCONTRADA (greedy: activo -> banda -> dead-zone):\n")
    best_asset, best_r = max(by_asset_results, key=lambda x: x[1].pnl)
    best_band, _ = max(band_results, key=lambda x: x[1].pnl)
    best_dz, _ = max(dz_results, key=lambda x: x[1].pnl)
    final = simulate(rows, BTConfig(f"FINAL {best_asset} edge{best_band} dz{best_dz}",
                     min_edge=best_band, dead_zone=best_dz, assets=(best_asset,), **base))
    print(_fmt(final))
    if final.n_bets < 100:
        print("\n  OJO: muestra chica en la combinacion ganadora — no es una conclusion firme,")
        print("  es una pista. Re-correr esto el 23/6 con 5 dias de datos.")


def tau_analysis(rows, min_edge=0.04, cost=0.010):
    """Win rate del lado que elige el modelo, segmentado por tiempo al cierre.
    Responde: ¿el edge funciona mejor cerca del cierre o lejos? (datos, no opinion)"""
    buckets = [(0, 60), (60, 120), (120, 300), (300, 600), (600, 900)]
    stats = {b: [0, 0, 0.0] for b in buckets}   # [candidatos, aciertos, suma_edge]
    for ts, slug, asset, fair_p, edge_up, edge_dn, tau, outcome in rows:
        if edge_up >= edge_dn:
            side, edge = "up", edge_up
        else:
            side, edge = "down", edge_dn
        if (edge - cost) < min_edge:
            continue
        for lo, hi in buckets:
            if lo < tau <= hi:
                s = stats[(lo, hi)]
                s[0] += 1
                s[1] += 1 if side == outcome else 0
                s[2] += edge
                break
    for (lo, hi), (n, w, esum) in stats.items():
        if n == 0:
            print(f"  tau {lo:3d}-{hi:3d}s:  sin candidatos")
            continue
        wr = w / n * 100
        print(f"  tau {lo:3d}-{hi:3d}s:  {n:4d} candidatos  win_rate={wr:5.1f}%  edge_prom={esum/n*100:4.1f}%")
    print("  Lectura: si el win_rate SUBE cuando tau baja -> conviene ser agresivo al cierre.")
    print("           si BAJA -> la cautela actual (MAX_EDGE_TRUST, MIN_TIME_LEFT) acierta.")


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
