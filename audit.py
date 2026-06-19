"""
audit.py — registro inalterable y verificable de las predicciones (trazabilidad).

El informe de DSN pide "registro historico inalterable + trazabilidad" para que un
inversor confie en que el track record no se editó a posteriori. Esto lo resuelve
con una cadena de hashes (tipo blockchain liviana): cada checkpoint sella TODAS las
predicciones hasta ese momento con un SHA-256 encadenado al checkpoint anterior.

Clave: se hashea SOLO la parte INMUTABLE de cada predicción (ts, slug, fair_p,
spot_open, spot_now, tau, sigma) — NO el outcome, que se rellena legítimamente al
cerrar la ventana. Así se prueba "qué predijo el bot y cuándo", que es lo que
importa: nadie cambió una predicción después de ver el resultado.

La cadena vive en `audit_chain.jsonl` (en la raíz, NO en data/ que está gitignored):
commiteándola a GitHub cada checkpoint queda con timestamp público e inmutable (la
historia de git es a prueba de manipulación). Si alguien edita una predicción vieja
en la DB, `--verify` recomputa el hash y NO coincide -> manipulación detectada.

Uso:
    venv\\Scripts\\python.exe audit.py            # sella un checkpoint nuevo
    venv\\Scripts\\python.exe audit.py --verify   # verifica toda la cadena contra la DB
"""
import argparse
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

import config

CHAIN_PATH = "audit_chain.jsonl"
GENESIS = "GENESIS"


def _canon(row) -> str:
    """Serializacion canonica e inmutable de una prediccion (sin outcome)."""
    ts, slug, asset, fair_p, spot_open, spot_now, tau, sigma = row
    def f(x):
        return "" if x is None else f"{x:.10g}"
    return f"{ts}|{slug}|{asset}|{f(fair_p)}|{f(spot_open)}|{f(spot_now)}|{f(tau)}|{f(sigma)}"


def _content_hash(conn, up_to_ts=None):
    """SHA-256 incremental sobre las predicciones (orden determinista ts,slug)."""
    q = ("SELECT ts, slug, asset, fair_p, spot_open, spot_now, tau, sigma "
         "FROM predictions")
    p = []
    if up_to_ts is not None:
        q += " WHERE ts <= ?"; p.append(up_to_ts)
    q += " ORDER BY ts ASC, slug ASC"
    h = hashlib.sha256()
    n = 0
    last_ts = 0
    for row in conn.execute(q, p):
        h.update(_canon(row).encode("utf-8"))
        h.update(b"\n")
        n += 1
        last_ts = row[0]
    return h.hexdigest(), n, last_ts


def _read_chain():
    if not os.path.exists(CHAIN_PATH):
        return []
    out = []
    with open(CHAIN_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _iso(ms=None):
    t = time.time() if ms is None else ms / 1000
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def seal():
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    content_hash, n, last_ts = _content_hash(conn)
    conn.close()
    chain = _read_chain()
    prev = chain[-1]["chain_hash"] if chain else GENESIS
    chain_hash = hashlib.sha256((prev + content_hash).encode("utf-8")).hexdigest()
    entry = {
        "checkpoint": len(chain) + 1,
        "generated_utc": _iso(),
        "n_predictions": n,
        "last_pred_ts": last_ts,
        "last_pred_utc": _iso(last_ts) if last_ts else None,
        "content_hash": content_hash,
        "prev_hash": prev,
        "chain_hash": chain_hash,
    }
    with open(CHAIN_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"Checkpoint #{entry['checkpoint']} sellado.")
    print(f"  predicciones: {n:,}   hasta: {entry['last_pred_utc']} UTC")
    print(f"  content_hash: {content_hash[:16]}…")
    print(f"  chain_hash:   {chain_hash[:16]}…  (prev: {prev[:16] if prev != GENESIS else GENESIS}…)")
    print(f"\n  -> commiteá {CHAIN_PATH} para dejar el sello con timestamp publico en GitHub.")


def verify():
    chain = _read_chain()
    if not chain:
        print("No hay cadena todavia. Corré 'audit.py' para sellar el primer checkpoint.")
        return
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    prev = GENESIS
    ok = True
    for e in chain:
        # 1) la cadena enlaza bien
        expect_chain = hashlib.sha256((prev + e["content_hash"]).encode("utf-8")).hexdigest()
        link_ok = (e["prev_hash"] == prev and e["chain_hash"] == expect_chain)
        # 2) el contenido de la DB hasta ese ts sigue dando el mismo hash
        recomputed, n, _ = _content_hash(conn, e["last_pred_ts"])
        content_ok = (recomputed == e["content_hash"])
        status = "OK" if (link_ok and content_ok) else "FALLA"
        if not (link_ok and content_ok):
            ok = False
        extra = ""
        if not content_ok:
            extra = f"  <-- CONTENIDO ALTERADO (DB n={n}, sellado n={e['n_predictions']})"
        if not link_ok:
            extra += "  <-- CADENA ROTA"
        print(f"  checkpoint #{e['checkpoint']:>3}  {e['generated_utc']}  "
              f"n={e['n_predictions']:>7,}  [{status}]{extra}")
        prev = e["chain_hash"]
    conn.close()
    print("\n  " + ("CADENA INTEGRA — ninguna prediccion fue alterada."
                    if ok else "ATENCION: la cadena NO verifica (ver lineas con FALLA)."))


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="verifica la cadena contra la DB")
    args = ap.parse_args()
    if args.verify:
        verify()
    else:
        seal()
