"""Dashboard web del copy-trader (solo lectura).

Sirve en http://localhost:8050 (DASHBOARD_PORT) una vista del estado:
balance, posiciones abiertas con P&L en vivo, historial de cierres,
wallets seguidos, últimas señales y progreso del pipeline de datos.

Lee data/positions.json (escrito por strategy.py) y processed/top_wallets.csv.
Sin dependencias nuevas: http.server de la stdlib + requests (ya instalado).

Uso: python -m trading.dashboard   (servicio poly-dashboard en docker-compose)
"""

import base64
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
FILLS_DIR = os.path.join("data", "fills")
TRADES_DIR = os.path.join("processed", "trades")

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


# Génesis del CTF Exchange V2 y bloque aproximado donde empiezan los
# OrderFilled (migración de v1, ~2026-04-28). Antes de eso el scan da 0 eventos.
V2_GENESIS_BLOCK = 84_902_353
EVENTS_FROM_BLOCK = 86_100_000


def _network_latest_block() -> int | None:
    def fetch():
        rpc = os.environ.get("POLYGON_RPC_URL", "https://polygon.drpc.org")
        resp = _session.post(
            rpc,
            json={"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1},
            timeout=10,
        )
        resp.raise_for_status()
        return int(resp.json()["result"], 16)

    return _cached("network_latest", 60, fetch)


def _markets_fetched() -> int:
    total = 0
    for part in ("closed", "active"):
        try:
            with open(os.path.join("data", f"markets_{part}_state.json")) as f:
                total += json.load(f).get("fetched", 0) or 0
        except Exception:
            pass
    return total


def _pipeline_status() -> dict:
    status: dict = {}
    try:
        with open(CURSOR_FILE) as f:
            status["chain_last_block"] = json.load(f).get("last_block")
    except Exception:
        status["chain_last_block"] = None

    latest = _network_latest_block()
    status["network_latest_block"] = latest
    cursor = status["chain_last_block"]
    if cursor and latest and latest > V2_GENESIS_BLOCK:
        status["chain_progress"] = round(
            min((cursor - V2_GENESIS_BLOCK) / (latest - V2_GENESIS_BLOCK), 1.0), 4
        )
    else:
        status["chain_progress"] = None
    status["events_from_block"] = EVENTS_FROM_BLOCK
    status["markets_fetched"] = _markets_fetched()

    def _dir_mb(dirpath: str) -> float:
        if not os.path.isdir(dirpath):
            return 0.0
        return round(
            sum(
                os.path.getsize(os.path.join(dirpath, f))
                for f in os.listdir(dirpath)
                if f.endswith(".parquet")
            )
            / 1e6,
            1,
        )

    status["orderfilled_mb"] = _dir_mb(FILLS_DIR)
    status["trades_mb"] = _dir_mb(TRADES_DIR)
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

    closed_all = state["closed"]
    closed = closed_all[::-1][:200]

    def _totals(open_subset: list[dict], closed_subset: list[dict]) -> dict:
        realized = sum(p.get("pnl_usd", 0) or 0 for p in closed_subset)
        n_closed = len(closed_subset)
        wins = sum(1 for p in closed_subset if (p.get("pnl_usd", 0) or 0) >= 0)
        return {
            "realized_pnl": round(realized, 2),
            "n_closed": n_closed,
            "win_rate": round(wins / n_closed, 4) if n_closed else None,
            "n_open": len(open_subset),
            "open_exposure": round(sum(p["size_usd"] for p in open_subset), 2),
            "unrealized_pnl": round(
                sum(p["pnl_usd"] for p in open_subset if p.get("pnl_usd") is not None), 2
            ),
        }

    # Un cierre sin is_live es SIM antiguo (paper trading previo al go-live).
    open_live = [p for p in open_pos if p.get("is_live")]
    open_sim = [p for p in open_pos if not p.get("is_live")]
    closed_live = [p for p in closed_all if p.get("is_live")]
    closed_sim = [p for p in closed_all if not p.get("is_live")]

    def _equity(closed_subset: list[dict]) -> list[dict]:
        """Curva de P&L acumulado: cierres ordenados por fecha con suma corrida."""
        pts, cum = [], 0.0
        for p in sorted(closed_subset, key=lambda x: x.get("closed_at", "")):
            cum += p.get("pnl_usd", 0) or 0
            pts.append({"t": p.get("closed_at", ""), "cum": round(cum, 2)})
        return pts

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
        "totals": _totals(open_pos, closed_all),          # Todos (SIM+LIVE)
        "totals_live": _totals(open_live, closed_live),   # solo órdenes reales
        "totals_sim": _totals(open_sim, closed_sim),      # solo paper trading
        "equity_live": _equity(closed_live),
        "equity_sim": _equity(closed_sim),
        "equity_all": _equity(closed_all),
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
.toggle{display:inline-flex;gap:0;border:1px solid var(--border);border-radius:8px;overflow:hidden;margin-bottom:16px}
.toggle button{background:var(--card);color:var(--dim);border:none;padding:7px 16px;font:600 12px system-ui,sans-serif;cursor:pointer;border-left:1px solid var(--border)}
.toggle button:first-child{border-left:none}
.toggle button.on{background:var(--accent);color:#0d1117}
a{color:var(--accent);text-decoration:none}
.empty{color:var(--dim);padding:14px;background:var(--card);border:1px solid var(--border);border-radius:8px}
</style></head><body>
<h1>poly-trader <span id="mode" class="badge sim">…</span></h1>
<div class="sub">Actualizado <span id="ts">…</span> · refresco cada 15 s</div>
<div class="toggle" id="toggle">
  <button data-m="live" class="on">LIVE (real)</button>
  <button data-m="sim">SIM (paper)</button>
  <button data-m="all">Todos</button>
</div>
<div class="cards" id="cards"></div>
<h2>Curva de P&amp;L acumulado <span id="equityScope" class="sub" style="text-transform:none"></span></h2>
<div id="equity" class="card" style="padding:16px"></div>
<h2>Posiciones abiertas <span id="openScope" class="sub" style="text-transform:none"></span></h2><div id="open"></div>
<h2>Historial de cierres <span id="closedScope" class="sub" style="text-transform:none"></span></h2><div id="closed"></div>
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
function drawEquity(pts){
  if(!pts||!pts.length)return '<div class="empty">— sin cierres todavía —</div>';
  const W=800,H=220,pad=34;
  const ys=pts.map(p=>p.cum).concat([0]);
  let mn=Math.min(...ys),mx=Math.max(...ys); if(mn===mx){mn-=1;mx+=1;}
  const n=pts.length;
  const X=i=>n===1?W/2:pad+i*(W-2*pad)/(n-1);
  const Y=v=>H-pad-(v-mn)*(H-2*pad)/(mx-mn);
  const pl=pts.map((p,i)=>`${X(i).toFixed(1)},${Y(p.cum).toFixed(1)}`).join(" ");
  const last=pts[n-1].cum, color=last>=0?"var(--green)":"var(--red)", zy=Y(0).toFixed(1);
  const dots=pts.map((p,i)=>`<circle cx="${X(i).toFixed(1)}" cy="${Y(p.cum).toFixed(1)}" r="2.5" fill="${color}"><title>${esc(p.t)}: ${fmt$(p.cum)}</title></circle>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="xMidYMid meet" style="font:11px system-ui">
    <line x1="${pad}" y1="${zy}" x2="${W-pad}" y2="${zy}" stroke="var(--border)" stroke-dasharray="4 4"/>
    <text x="${pad-4}" y="${(+zy+4)}" fill="var(--dim)" text-anchor="end">0</text>
    <polyline fill="none" stroke="${color}" stroke-width="2" points="${pl}"/>${dots}
    <text x="${W-pad}" y="${Math.max(14,Y(last)-8).toFixed(1)}" fill="${color}" text-anchor="end" font-weight="600">${fmt$(last)}</text>
    <text x="${pad}" y="14" fill="var(--dim)">máx ${fmt$(mx)} · mín ${fmt$(mn)} · ${n} cierre${n!==1?"s":""}</text>
  </svg>`;
}
let MODE="live", LAST=null;
async function refresh(){ LAST=await (await fetch("/api/state")).json(); render(); }
function render(){
  const s=LAST; if(!s)return;
  document.getElementById("ts").textContent=s.generated_at;
  const m=document.getElementById("mode");
  m.textContent=s.mode; m.className="badge "+s.mode.toLowerCase();
  const g = MODE==="all" ? s.totals : s["totals_"+MODE];
  const bal=s.balance?s.balance.total_usd:null;
  const onlyLive=MODE==="live", onlySim=MODE==="sim";
  const flt=arr=>MODE==="all"?arr:arr.filter(r=>onlyLive?r.is_live:!r.is_live);
  const openRows=flt(s.open), closedRows=flt(s.closed);
  const scope=MODE==="all"?"(SIM + LIVE)":(onlyLive?"(solo órdenes reales)":"(solo paper trading)");
  document.getElementById("openScope").textContent=`${scope} · ${g.n_open} abiertas`;
  document.getElementById("closedScope").textContent=`${scope} · ${g.n_closed} cerradas`;
  document.getElementById("cards").innerHTML=[
    ["Balance (wallet)",fmt$(bal),""],
    ["P&L realizado",fmt$(g.realized_pnl),cls(g.realized_pnl)],
    ["P&L abierto",fmt$(g.unrealized_pnl),cls(g.unrealized_pnl)],
    ["Exposición",fmt$(g.open_exposure),""],
    ["Win rate",fmtP(g.win_rate),""],
    ["Cierres",g.n_closed,""],
  ].map(([l,v,c])=>`<div class="card"><div class="label">${l}</div><div class="value ${c}">${v}</div></div>`).join("");
  const eq = MODE==="all" ? s.equity_all : s["equity_"+MODE];
  document.getElementById("equity").innerHTML=drawEquity(eq);
  document.getElementById("equityScope").textContent=scope;
  document.getElementById("open").innerHTML=table(openRows,[
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
  document.getElementById("closed").innerHTML=table(closedRows,[
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
  const prog=p.chain_progress!=null?(p.chain_progress*100).toFixed(1)+"%":"—";
  const preEvents=p.chain_last_block&&p.chain_last_block<p.events_from_block;
  document.getElementById("pipeline").innerHTML=
    `<div class="cards">
      <div class="card"><div class="label">Backfill</div><div class="value">${prog}</div></div>
      <div class="card"><div class="label">Bloque escaneado</div><div class="value">${p.chain_last_block?p.chain_last_block.toLocaleString():"—"}</div></div>
      <div class="card"><div class="label">Bloque red</div><div class="value">${p.network_latest_block?p.network_latest_block.toLocaleString():"—"}</div></div>
      <div class="card"><div class="label">Markets</div><div class="value">${p.markets_fetched.toLocaleString()}</div></div>
      <div class="card"><div class="label">fills/ (parquet)</div><div class="value">${p.orderfilled_mb} MB</div></div>
      <div class="card"><div class="label">trades/ (parquet)</div><div class="value">${p.trades_mb} MB</div></div>
    </div>`+
    (preEvents?`<div class="sub" style="margin-top:8px">ℹ️ Los eventos OrderFilled empiezan en el bloque ~${p.events_from_block.toLocaleString()} (28-abr-2026). Hasta que el scan llegue ahí, trades/wallets/señales estarán vacíos — es lo esperado.</div>`:"");
}
document.getElementById("toggle").addEventListener("click",e=>{
  const b=e.target.closest("button"); if(!b)return;
  MODE=b.dataset.m;
  [...document.querySelectorAll("#toggle button")].forEach(x=>x.classList.toggle("on",x===b));
  render();
});
refresh(); setInterval(refresh,15000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _authorized(self) -> bool:
        """HTTP Basic Auth (usuario 'admin') si DASHBOARD_PASSWORD está fijada."""
        if not config.DASHBOARD_PASSWORD:
            return True
        expected = base64.b64encode(
            f"admin:{config.DASHBOARD_PASSWORD}".encode()
        ).decode()
        return self.headers.get("Authorization", "") == f"Basic {expected}"

    def do_GET(self):
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="poly-trader"')
            self.end_headers()
            return
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
