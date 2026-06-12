"""Dashboard web del copy-trader (solo lectura).

Sirve en http://localhost:8050 (DASHBOARD_PORT) una vista del estado:
balance, posiciones abiertas con P&L en vivo, historial de cierres,
wallets seguidos, últimas señales y progreso del pipeline de datos.

Lee data/positions.json (escrito por strategy.py) y processed/top_wallets.csv.
Sin dependencias nuevas: http.server de la stdlib + requests (ya instalado).

Uso: python -m trading.dashboard   (servicio poly-dashboard en docker-compose)
"""

import csv
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading import config, positions
from trading.wallet_utils import proxy_balance

TOP_WALLETS_CSV = os.path.join("processed", "top_wallets.csv")
CURSOR_FILE = os.path.join("data", "cursor_state.json")
ORDERS_CSV = os.path.join("data", "orderFilled.csv")
TRADES_CSV = os.path.join("processed", "trades.csv")

_session = requests.Session()
_cache: dict = {}


def _cached(key: str, ttl: float, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        value = fn()
    except Exception:
        value = hit[1] if hit else None
    _cache[key] = (now, value)
    return value


def _midpoint(token_id: str) -> float | None:
    def fetch():
        resp = _session.get(
            f"{config.CLOB_HOST}/midpoint", params={"token_id": token_id}, timeout=8
        )
        resp.raise_for_status()
        mid = float(resp.json().get("mid", 0))
        return mid if 0 < mid < 1 else None

    return _cached(f"mid:{token_id}", 30, fetch)


def _balance() -> dict | None:
    return _cached("balance", 120, lambda: proxy_balance(config.FUNDER_ADDRESS))


def _top_wallets(n: int = 10) -> list[dict]:
    if not os.path.isfile(TOP_WALLETS_CSV):
        return []
    with open(TOP_WALLETS_CSV, encoding="utf-8") as f:
        return list(csv.DictReader(f))[:n]


def _pipeline_status() -> dict:
    status: dict = {}
    try:
        with open(CURSOR_FILE) as f:
            status["chain_last_block"] = json.load(f).get("last_block")
    except Exception:
        status["chain_last_block"] = None
    for name, path in (("orderfilled_mb", ORDERS_CSV), ("trades_mb", TRADES_CSV)):
        try:
            status[name] = round(os.path.getsize(path) / 1e6, 1)
        except OSError:
            status[name] = 0.0
    return status


def build_state() -> dict:
    state = positions.load_state()

    open_pos = []
    for p in state["open"]:
        mid = _midpoint(p["token_id"])
        entry = p.get("entry_price", 0)
        pnl_pct = (mid - entry) / entry if (mid and entry > 0) else None
        open_pos.append(
            {
                **p,
                "current_price": mid,
                "pnl_pct": round(pnl_pct, 4) if pnl_pct is not None else None,
                "pnl_usd": round(p["size_usd"] * pnl_pct, 2) if pnl_pct is not None else None,
            }
        )

    closed = state["closed"][::-1][:100]
    realized = sum(p.get("pnl_usd", 0) for p in state["closed"])
    wins = sum(1 for p in state["closed"] if p.get("pnl_usd", 0) >= 0)
    n_closed = len(state["closed"])

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "mode": "LIVE" if config.AUTO_EXECUTE else "SIM",
        "config": {
            "size_usd": config.TRADE_SIZE_USD,
            "size_pct": config.TRADE_SIZE_PCT,
            "stop_loss": config.STOP_LOSS_PCT,
            "take_profit": config.TAKE_PROFIT_PCT,
            "max_positions": config.MAX_OPEN_POSITIONS,
            "copy_top_n": config.COPY_TOP_N,
            "min_copy_usd": config.MIN_COPY_TRADE_USD,
        },
        "balance": _balance(),
        "open": open_pos,
        "closed": closed,
        "totals": {
            "realized_pnl": round(realized, 2),
            "n_closed": n_closed,
            "win_rate": round(wins / n_closed, 4) if n_closed else None,
            "open_exposure": round(sum(p["size_usd"] for p in state["open"]), 2),
            "unrealized_pnl": round(
                sum(p["pnl_usd"] for p in open_pos if p["pnl_usd"] is not None), 2
            ),
        },
        "wallets": _top_wallets(),
        "signals": state["signals"][::-1][:20],
        "pipeline": _pipeline_status(),
    }


PAGE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<title>poly-trader</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--dim:#8b949e;
      --green:#3fb950;--red:#f85149;--accent:#58a6ff}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--text);font:14px/1.5 system-ui,sans-serif;padding:20px;max-width:1200px;margin:auto}
h1{font-size:20px;margin-bottom:4px} h2{font-size:14px;color:var(--dim);margin:24px 0 8px;text-transform:uppercase;letter-spacing:.05em}
.sub{color:var(--dim);font-size:12px;margin-bottom:16px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px}
.card .label{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.05em}
.card .value{font-size:22px;font-weight:600;margin-top:4px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--border);border-radius:8px;overflow:hidden;font-size:13px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--border)}
th{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
tr:last-child td{border-bottom:none}
.pos{color:var(--green)} .neg{color:var(--red)}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600}
.badge.live{background:#1f6f2e;color:#fff}.badge.sim{background:#6e5400;color:#fff}
a{color:var(--accent);text-decoration:none}
.empty{color:var(--dim);padding:14px;background:var(--card);border:1px solid var(--border);border-radius:8px}
</style></head><body>
<h1>poly-trader <span id="mode" class="badge sim">…</span></h1>
<div class="sub">Actualizado <span id="ts">…</span> · refresco cada 15 s</div>
<div class="cards" id="cards"></div>
<h2>Posiciones abiertas</h2><div id="open"></div>
<h2>Historial de cierres</h2><div id="closed"></div>
<h2>Wallets seguidos (ranking)</h2><div id="wallets"></div>
<h2>Últimas señales</h2><div id="signals"></div>
<h2>Pipeline de datos</h2><div id="pipeline"></div>
<script>
const fmt$=v=>v==null?"—":(v<0?"-$":"$")+Math.abs(v).toFixed(2);
const fmtP=v=>v==null?"—":(v*100).toFixed(1)+"%";
const cls=v=>v==null?"":(v>=0?"pos":"neg");
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function table(rows,cols){
  if(!rows||!rows.length)return '<div class="empty">— sin datos —</div>';
  let h="<table><tr>"+cols.map(c=>`<th>${c.h}</th>`).join("")+"</tr>";
  for(const r of rows)h+="<tr>"+cols.map(c=>`<td class="${c.c?c.c(r):""}">${c.f(r)}</td>`).join("")+"</tr>";
  return h+"</table>";
}
async function refresh(){
  const s=await (await fetch("/api/state")).json();
  document.getElementById("ts").textContent=s.generated_at;
  const m=document.getElementById("mode");
  m.textContent=s.mode; m.className="badge "+s.mode.toLowerCase();
  const bal=s.balance?s.balance.total_usd:null;
  document.getElementById("cards").innerHTML=[
    ["Balance",fmt$(bal),""],
    ["P&L realizado",fmt$(s.totals.realized_pnl),cls(s.totals.realized_pnl)],
    ["P&L abierto",fmt$(s.totals.unrealized_pnl),cls(s.totals.unrealized_pnl)],
    ["Exposición",fmt$(s.totals.open_exposure),""],
    ["Win rate",fmtP(s.totals.win_rate),""],
    ["Cierres",s.totals.n_closed,""],
  ].map(([l,v,c])=>`<div class="card"><div class="label">${l}</div><div class="value ${c}">${v}</div></div>`).join("");
  document.getElementById("open").innerHTML=table(s.open,[
    {h:"Mercado",f:r=>r.market_url?`<a href="${esc(r.market_url)}" target="_blank">${esc(r.question.slice(0,60))}</a>`:esc(r.question.slice(0,60))},
    {h:"Outcome",f:r=>esc(r.outcome)},
    {h:"Entrada",f:r=>r.entry_price.toFixed(3)},
    {h:"Actual",f:r=>r.current_price?r.current_price.toFixed(3):"—"},
    {h:"Tamaño",f:r=>fmt$(r.size_usd)},
    {h:"P&L",f:r=>`${fmt$(r.pnl_usd)} (${fmtP(r.pnl_pct)})`,c:r=>cls(r.pnl_usd)},
    {h:"Fuente",f:r=>esc(r.source_wallet.slice(0,10))+"…"},
    {h:"Abierta",f:r=>esc(r.opened_at)},
    {h:"Modo",f:r=>r.is_live?"LIVE":"SIM"},
  ]);
  document.getElementById("closed").innerHTML=table(s.closed,[
    {h:"Mercado",f:r=>esc(r.question.slice(0,60))},
    {h:"Entrada",f:r=>r.entry_price.toFixed(3)},
    {h:"Salida",f:r=>r.exit_price.toFixed(3)},
    {h:"P&L",f:r=>`${fmt$(r.pnl_usd)} (${fmtP(r.pnl_pct)})`,c:r=>cls(r.pnl_usd)},
    {h:"Motivo",f:r=>esc(r.reason)},
    {h:"Cerrada",f:r=>esc(r.closed_at)},
  ]);
  document.getElementById("wallets").innerHTML=table(s.wallets,[
    {h:"Wallet",f:r=>`<a href="https://polymarket.com/profile/${esc(r.wallet)}" target="_blank">${esc(r.wallet.slice(0,14))}…</a>`},
    {h:"PnL realizado",f:r=>fmt$(+r.realized_pnl),c:r=>cls(+r.realized_pnl)},
    {h:"ROI",f:r=>fmtP(+r.roi)},
    {h:"Win rate",f:r=>fmtP(+r.win_rate)},
    {h:"Trades",f:r=>r.n_trades},
    {h:"Volumen",f:r=>fmt$(+r.volume_buy)},
  ]);
  document.getElementById("signals").innerHTML=table(s.signals,[
    {h:"Hora",f:r=>esc(r.seen_at)},
    {h:"Wallet",f:r=>esc((r.wallet||"").slice(0,10))+"…"},
    {h:"Mercado",f:r=>esc(r.question)},
    {h:"Precio",f:r=>(+r.price).toFixed(3)},
    {h:"USD",f:r=>fmt$(r.usd)},
    {h:"Acción",f:r=>esc(r.action),c:r=>r.action==="COPIED"?"pos":""},
  ]);
  const p=s.pipeline;
  document.getElementById("pipeline").innerHTML=
    `<div class="cards">
      <div class="card"><div class="label">Último bloque</div><div class="value">${p.chain_last_block?p.chain_last_block.toLocaleString():"—"}</div></div>
      <div class="card"><div class="label">orderFilled.csv</div><div class="value">${p.orderfilled_mb} MB</div></div>
      <div class="card"><div class="label">trades.csv</div><div class="value">${p.trades_mb} MB</div></div>
    </div>`;
}
refresh(); setInterval(refresh,15000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/state"):
            body = json.dumps(build_state()).encode()
            ctype = "application/json"
        elif self.path == "/" or self.path.startswith("/index"):
            body = PAGE.encode()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # silenciar access log
        pass


def main() -> None:
    port = config.DASHBOARD_PORT
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Dashboard en http://localhost:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
