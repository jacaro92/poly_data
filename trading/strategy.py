"""Estrategia copy-trading sobre Polymarket.

Sigue los wallets más rentables (processed/top_wallets.csv, generado por
wallet_analyzer, o COPY_WALLETS si se fija a mano) consultando sus trades en
el data-api público de Polymarket, y replica sus compras con el sizing propio.

REGLAS DE ENTRADA — se copia una compra del wallet seguido si:
  1. Es nueva (posterior al cursor guardado; el histórico nunca se copia).
  2. Tamaño del trade original >= MIN_COPY_TRADE_USD (filtra micro-ruido).
  3. Precio del token dentro de [ENTRY_PRICE_MIN, ENTRY_PRICE_MAX]
     (ni loterías casi-resueltas ni colas ilíquidas).
  4. No tenemos ya posición en ese token.
  5. Posiciones abiertas < MAX_OPEN_POSITIONS.
  6. Balance disponible >= tamaño a invertir (solo en live).

REGLAS DE SALIDA — se cierra una posición cuando ocurre lo primero de:
  1. STOP_LOSS_PCT  : el token cae ese % desde la entrada.
  2. TAKE_PROFIT_PCT: el token sube ese % desde la entrada.
  3. COPY_EXIT      : el wallet seguido vende ese mismo token.
  4. RESOLVED       : precio >= 0.99 o <= 0.01 (mercado decidido de facto).
  5. TIME_EXIT      : la posición supera MAX_HOLD_HOURS.

MODOS:
  AUTO_EXECUTE=false → paper trading: registra posiciones simuladas en
    data/positions.json y notifica por Telegram con etiqueta [SIM].
  AUTO_EXECUTE=true  → órdenes reales FOK via PolymarketExecutor, misma lógica.

Uso: python -m trading.strategy   (servicio poly-trader en docker-compose)
"""

import csv
import os
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading import config, positions
from trading.telegram_notifier import TelegramNotifier
from trading.wallet_utils import proxy_balance

DATA_API = "https://data-api.polymarket.com"
TOP_WALLETS_CSV = os.path.join("processed", "top_wallets.csv")
TRADES_DIR = os.path.join("processed", "trades")

# Precio extremo = mercado decidido de facto.
RESOLVED_HI = 0.99
RESOLVED_LO = 0.01

_session = requests.Session()


def _to_unix(s: str) -> int:
    """'YYYY-MM-DD HH:MM:SS' (UTC) → epoch segundos. 0 si no parsea."""
    try:
        return int(
            datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except Exception:
        return 0


# ── Fuentes de datos ──────────────────────────────────────────────────────────

def followed_wallets() -> list[str]:
    """COPY_WALLETS manda; si está vacío, top COPY_TOP_N del analizador."""
    if config.COPY_WALLETS:
        return config.COPY_WALLETS
    if not os.path.isfile(TOP_WALLETS_CSV):
        return []
    wallets = []
    with open(TOP_WALLETS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            wallets.append(row["wallet"].lower())
            if len(wallets) >= config.COPY_TOP_N:
                break
    return wallets


def recent_trades(wallet: str, limit: int = 50) -> list[dict]:
    """Últimos fills del wallet via data-api público (más recientes primero)."""
    resp = _session.get(
        f"{DATA_API}/trades", params={"user": wallet, "limit": limit}, timeout=15
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def midpoint(token_id: str) -> float | None:
    """Precio medio actual del token via CLOB público (sin auth)."""
    try:
        resp = _session.get(
            f"{config.CLOB_HOST}/midpoint", params={"token_id": token_id}, timeout=10
        )
        resp.raise_for_status()
        mid = float(resp.json().get("mid", 0))
        return mid if 0 < mid < 1 else None
    except Exception:
        return None


# Caché de fechas de cierre por conditionId (no cambian; evita repetir la
# llamada al CLOB en cada ciclo para el mismo mercado).
_enddate_cache: dict[str, datetime | None] = {}


def market_end_date(condition_id: str) -> datetime | None:
    """Fecha de resolución (end_date_iso) del mercado via CLOB público.

    Devuelve None si no se puede determinar (sin condition_id, fallo de red o
    el mercado no expone fecha). Cachea el resultado por conditionId."""
    if not condition_id:
        return None
    if condition_id in _enddate_cache:
        return _enddate_cache[condition_id]
    end = None
    try:
        resp = _session.get(
            f"{config.CLOB_HOST}/markets/{condition_id}", timeout=10
        )
        resp.raise_for_status()
        iso = resp.json().get("end_date_iso")
        if iso:
            end = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
    except Exception:
        end = None
    _enddate_cache[condition_id] = end
    return end


def best_bid_ask(token_id: str) -> tuple[float | None, float | None]:
    """(mejor_bid, mejor_ask) del libro público del CLOB. (None, None) si falla
    o el libro está vacío de ese lado. Para medir el spread antes de entrar."""
    try:
        resp = _session.get(
            f"{config.CLOB_HOST}/book", params={"token_id": token_id}, timeout=10
        )
        resp.raise_for_status()
        book = resp.json()
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = max((float(b["price"]) for b in bids), default=None)
        best_ask = min((float(a["price"]) for a in asks), default=None)
        return best_bid, best_ask
    except Exception:
        return None, None


def market_url(trade: dict) -> str:
    slug = trade.get("eventSlug") or trade.get("slug") or ""
    return f"https://polymarket.com/event/{slug}" if slug else ""


# ── Estrategia ────────────────────────────────────────────────────────────────

class CopyTrader:
    def __init__(self):
        self.notifier = TelegramNotifier()
        self.executor = None
        if config.AUTO_EXECUTE:
            from trading.executor import PolymarketExecutor
            self.executor = PolymarketExecutor()
        self.state = positions.load_state()
        if self.executor:
            self._close_stale_sim_positions()
            self._revalidate_closed_pnl()
        self._balance_cache: tuple[float, float] = (0.0, 0.0)  # (ts, total)
        self._last_analyze = 0.0

    def _revalidate_closed_pnl(self) -> None:
        """Recalcula entrada/salida/P&L de las operaciones LIVE con los precios
        de ejecución REALES (get_trades), corrigiendo las registradas con
        midpoint (cierres con P&L $0.00, imposible: siempre se paga el spread)
        y las entradas corruptas. Revalida cierres (entrada+salida) y posiciones
        abiertas (entrada, para que su cierre futuro sea exacto). Idempotente:
        marca cada operación con 'revalidated'."""
        pend_closed = [p for p in self.state["closed"]
                       if p.get("is_live") and not p.get("revalidated")]
        pend_open = [p for p in self.state["open"]
                     if p.get("is_live") and not p.get("revalidated")]
        if not pend_closed and not pend_open:
            return
        try:
            from py_clob_client_v2.clob_types import TradeParams
            trades = self.executor.client.get_trades(TradeParams())
        except Exception as e:
            print(f"  revalidación: get_trades falló: {e}")
            return

        by_asset: dict[str, list[dict]] = {}
        for t in trades:
            by_asset.setdefault(str(t.get("asset_id")), []).append({
                "side": t.get("side"),
                "price": float(t.get("price", 0) or 0),
                "size": float(t.get("size", 0) or 0),
                "t": int(t.get("match_time", 0) or 0),
            })

        def vwap(fills, side, around, window=300):
            sel = [f for f in fills
                   if f["side"] == side and around and abs(f["t"] - around) <= window]
            sz = sum(f["size"] for f in sel)
            if sz <= 0:
                return None, 0.0
            return sum(f["price"] * f["size"] for f in sel) / sz, sz

        def fix_entry(p) -> bool:
            fills = by_asset.get(str(p.get("token_id")), [])
            ep, bsz = vwap(fills, "BUY", _to_unix(p.get("opened_at", "")))
            if ep is not None and bsz > 0:
                p["entry_price"] = round(ep, 4)
                p["shares"] = round(bsz, 4)
                p["size_usd"] = round(ep * bsz, 2)
                return True
            return False

        for p in pend_open:
            fix_entry(p)
            p["revalidated"] = True

        n_fixed = 0
        for p in pend_closed:
            fix_entry(p)
            fills = by_asset.get(str(p.get("token_id")), [])
            xp, _ = vwap(fills, "SELL", _to_unix(p.get("closed_at", "")))
            if xp is not None:
                p["exit_price"] = round(xp, 4)
            e, x, s = p.get("entry_price", 0.0), p.get("exit_price", 0.0), p.get("shares", 0.0)
            if e > 0:
                p["pnl_usd"] = round((x - e) * s, 2)
                p["pnl_pct"] = round((x - e) / e, 4)
            p["revalidated"] = True
            n_fixed += 1
        positions.save_state(self.state)
        print(
            f"  🔧 revalidados {n_fixed} cierres + {len(pend_open)} abiertas "
            f"con precios reales (get_trades)"
        )

    def _close_stale_sim_positions(self) -> None:
        """Al arrancar en LIVE, cierra (sin orden real) las posiciones SIM
        (is_live=false) que quedaran abiertas de la fase de paper trading: no
        deben ocupar slots de MAX_OPEN_POSITIONS ni gestionarse como reales."""
        stale = [p for p in self.state["open"] if not p.get("is_live")]
        for pos in stale:
            positions.close_position(
                self.state, pos, pos.get("entry_price", 0.0), "SIM_CLEANUP"
            )
            print(f"  🧹 SIM cerrada (cleanup live): {pos['question'][:50]}")
        if stale:
            print(f"  🧹 {len(stale)} posición(es) SIM cerradas; slots liberados")

    def maybe_refresh_wallets(self) -> None:
        """Regenera el ranking de wallets cada WALLET_REFRESH_HOURS cuando hay
        datos en trades.csv. Así el sistema se auto-arranca al terminar el
        backfill sin ejecutar wallet_analyzer a mano."""
        if config.COPY_WALLETS or config.WALLET_REFRESH_HOURS <= 0:
            return
        if time.time() - self._last_analyze < config.WALLET_REFRESH_HOURS * 3600:
            return
        if not os.path.isdir(TRADES_DIR) or not any(
            f.endswith(".parquet") for f in os.listdir(TRADES_DIR)
        ):
            return  # aún sin datos suficientes; se reintenta el próximo ciclo
        self._last_analyze = time.time()
        try:
            from trading.wallet_analyzer import analyze
            ranking = analyze()
            print(f"  📊 ranking actualizado: {len(ranking)} wallets")
        except SystemExit as e:
            print(f"  analyzer: {e}")
        except Exception as e:
            print(f"  ! analyzer falló: {e}")

    # ── helpers ──

    def balance(self) -> float:
        ts, total = self._balance_cache
        if time.time() - ts < 120:
            return total
        try:
            total = proxy_balance(config.FUNDER_ADDRESS)["total_usd"]
            self._balance_cache = (time.time(), total)
        except Exception as e:
            print(f"  ! balance no disponible: {e}")
        return self._balance_cache[1]

    def check_sl_tp(self, entry: float, current: float) -> str | None:
        if entry <= 0:
            return None
        change = (current - entry) / entry
        if config.STOP_LOSS_PCT > 0 and change <= -config.STOP_LOSS_PCT:
            return "STOP_LOSS"
        if config.TAKE_PROFIT_PCT > 0 and change >= config.TAKE_PROFIT_PCT:
            return "TAKE_PROFIT"
        return None

    # ── entradas ──

    def maybe_enter(self, trade: dict, wallet: str) -> None:
        token_id = str(trade.get("asset", ""))
        price = float(trade.get("price", 0) or 0)
        usd = float(trade.get("size", 0) or 0) * price
        question = trade.get("title", "") or token_id
        event_slug = trade.get("eventSlug") or trade.get("slug") or ""

        # Filtros baratos primero (sin llamadas de red).
        skip = None
        if usd < config.MIN_COPY_TRADE_USD:
            skip = f"trade ${usd:.0f} < umbral ${config.MIN_COPY_TRADE_USD:.0f}"
        elif not (config.ENTRY_PRICE_MIN <= price <= config.ENTRY_PRICE_MAX):
            skip = f"precio {price:.2f} fuera de [{config.ENTRY_PRICE_MIN}, {config.ENTRY_PRICE_MAX}]"
        elif positions.has_open_token(self.state, token_id):
            skip = "ya en posición"
        elif len(self.state["open"]) >= config.MAX_OPEN_POSITIONS:
            skip = f"límite de {config.MAX_OPEN_POSITIONS} posiciones"
        # Anti-correlación: no amontonar la cartera en un solo wallet/evento.
        elif (config.MAX_POS_PER_WALLET > 0
              and positions.count_open_by_wallet(self.state, wallet) >= config.MAX_POS_PER_WALLET):
            skip = f"ya {config.MAX_POS_PER_WALLET} pos del wallet {wallet[:10]}… (diversificar)"
        elif (config.MAX_POS_PER_EVENT > 0 and event_slug
              and positions.count_open_by_event(self.state, event_slug) >= config.MAX_POS_PER_EVENT):
            skip = f"ya {config.MAX_POS_PER_EVENT} pos en evento '{event_slug[:30]}' (correlación)"

        # Precio real de entrada = midpoint actual (el del wallet seguido puede
        # llevar segundos de retraso). Revalidar el rango con el midpoint evita
        # comprar tokens muertos/ilíquidos cuyo precio ya colapsó a ~0 o ~1:
        # no se podrían vender después ('no match', sin bids) y quedarían
        # atascados. Solo se consulta si los filtros baratos pasaron.
        entry = price
        if not skip:
            # Ventana de resolución: solo mercados que resuelven pronto. Evita
            # entrar en colas largas (geopolítica a 45 días) donde el capital
            # queda atrapado y el TIME_EXIT cierra al midpoint con pérdida.
            if config.MAX_RESOLUTION_HOURS > 0:
                end = market_end_date(str(trade.get("conditionId", "")))
                if end is not None:
                    hours_left = (end - datetime.now(timezone.utc)).total_seconds() / 3600
                    if hours_left > config.MAX_RESOLUTION_HOURS:
                        skip = (
                            f"resuelve en {hours_left:.0f}h > "
                            f"{config.MAX_RESOLUTION_HOURS:.0f}h (ventana larga)"
                        )

        if not skip:
            entry = midpoint(token_id) or price
            size_usd = config.compute_size(self.balance())
            if not (config.ENTRY_PRICE_MIN <= entry <= config.ENTRY_PRICE_MAX):
                skip = f"entrada {entry:.3f} fuera de [{config.ENTRY_PRICE_MIN}, {config.ENTRY_PRICE_MAX}]"
            elif config.AUTO_EXECUTE and self.balance() < size_usd:
                skip = f"balance ${self.balance():.2f} < tamaño ${size_usd:.2f}"
            elif config.MAX_SPREAD_PCT > 0:
                # Mercado ilíquido = spread ancho: se paga el peaje al cruzar el
                # libro al entrar (ask) y salir (bid). Sin libro = inservible.
                bid, ask = best_bid_ask(token_id)
                if not bid or not ask:
                    skip = "mercado sin libro (ilíquido)"
                else:
                    rel = (ask - bid) / ((ask + bid) / 2.0)
                    if rel > config.MAX_SPREAD_PCT:
                        skip = f"spread {rel:.0%} > {config.MAX_SPREAD_PCT:.0%} (ilíquido)"

        positions.log_signal(
            self.state,
            {
                "wallet": wallet,
                "side": "BUY",
                "question": question[:80],
                "price": price,
                "usd": round(usd, 2),
                "action": "SKIP: " + skip if skip else "COPIED",
            },
        )
        if skip:
            print(f"  ⏭ {question[:50]} — {skip}")
            return

        size_usd = config.compute_size(self.balance())
        url = market_url(trade)

        if self.executor:
            try:
                fill = self.executor.buy_market(token_id, size_usd, question=question, market_url=url)
                # Contabilizar con el precio y coste REALES de ejecución (se
                # compra al ask; el midpoint subestima el coste). open_position
                # derivará las shares reales = spent / fill_price.
                entry = fill.get("fill_price") or entry
                size_usd = fill.get("spent") or size_usd
                # Descuenta lo realmente gastado del balance cacheado para no
                # seguir intentando compras sin capital (ventana de caché 120s).
                ts, total = self._balance_cache
                self._balance_cache = (ts, max(0.0, total - size_usd))
            except Exception as e:
                print(f"  ✗ orden falló: {e}")
                # 'not enough balance' es un límite de capital esperado, no un
                # fallo del sistema: no satures Telegram con ello.
                if "not enough balance" not in str(e):
                    self.notifier.notify_error(f"buy_market {question[:50]}", str(e))
                return
        else:
            self.notifier.notify_trade_opened(
                question=question, direction="BUY_YES", price=entry,
                size_usd=size_usd, market_url=url, is_live=False,
            )

        pos = positions.open_position(
            self.state,
            token_id=token_id,
            question=question,
            outcome=trade.get("outcome", ""),
            entry_price=entry,
            size_usd=size_usd,
            source_wallet=wallet,
            market_url=url,
            is_live=bool(self.executor),
            event_slug=event_slug,
        )
        mode = "LIVE" if self.executor else "SIM"
        print(f"  🟢 [{mode}] COPIA {question[:50]} @ {entry:.3f}  ${size_usd:.2f}  (de {wallet[:10]}…)")

    # ── salidas ──

    def exit_position(self, pos: dict, exit_price: float, reason: str) -> None:
        if self.executor and pos.get("is_live"):
            try:
                fill = self.executor.sell_market(
                    pos["token_id"], pos["shares"],
                    question=pos["question"], market_url=pos.get("market_url", ""),
                    entry_price=pos["entry_price"], reason=reason,
                )
                # P&L con el precio de venta REAL (se vende al bid; el midpoint
                # lo sobreestima).
                if fill.get("fill_price"):
                    exit_price = fill["fill_price"]
            except Exception as e:
                msg = str(e)
                print(f"  ✗ venta falló ({reason}): {msg}")
                resolved = (
                    reason == "RESOLVED"
                    or exit_price <= RESOLVED_LO
                    or exit_price >= RESOLVED_HI
                )
                if resolved:
                    # Mercado resuelto de facto (precio en extremo) y a menudo
                    # sin liquidez (0 bids → 'no match'): no se puede vender,
                    # pero los outcome tokens se redimen on-chain a su valor
                    # final (0 o 1). Cerramos en libros a ese valor para no
                    # dejar la posición atascada; la redención on-chain realiza
                    # el P&L. (Cubre también STOP_LOSS sobre un token colapsado,
                    # cuyo SL salta antes que el chequeo de RESOLVED.)
                    exit_price = 1.0 if exit_price >= RESOLVED_HI else 0.0
                    reason = "RESOLVED"
                else:
                    # Fallo transitorio en mercado vivo (liquidez/capital
                    # puntual): 'no match' y 'not enough balance' son
                    # condiciones esperadas, no fallos del sistema → sin
                    # Telegram; se reintenta el próximo ciclo.
                    if "no match" not in msg and "not enough balance" not in msg:
                        self.notifier.notify_error(
                            f"sell_market {pos['question'][:50]}", msg
                        )
                    return  # mantener abierta; se reintenta el próximo ciclo
        else:
            self.notifier.notify_trade_closed(
                question=pos["question"], direction="BUY_YES",
                entry_price=pos["entry_price"], exit_price=exit_price,
                size_usd=pos["size_usd"], reason=reason,
                market_url=pos.get("market_url", ""),
            )
        closed = positions.close_position(self.state, pos, exit_price, reason)
        print(
            f"  🔴 CIERRE {reason}: {pos['question'][:50]} "
            f"{pos['entry_price']:.3f} → {exit_price:.3f}  P&L ${closed['pnl_usd']:+.2f}"
        )

    def check_exits(self, sells_by_token: dict[str, str]) -> None:
        now = datetime.now(timezone.utc)
        for pos in list(self.state["open"]):
            mid = midpoint(pos["token_id"])
            if mid is None:
                continue

            reason = self.check_sl_tp(pos["entry_price"], mid)
            if not reason and (mid >= RESOLVED_HI or mid <= RESOLVED_LO):
                reason = "RESOLVED"
            if not reason and config.COPY_EXIT and pos["token_id"] in sells_by_token:
                reason = "COPY_EXIT"
            if not reason and config.MAX_HOLD_HOURS > 0:
                opened = datetime.strptime(pos["opened_at"], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
                if (now - opened).total_seconds() > config.MAX_HOLD_HOURS * 3600:
                    reason = "TIME_EXIT"

            if reason:
                self.exit_position(pos, mid, reason)

    # ── ciclo principal ──

    def cycle(self) -> None:
        self.maybe_refresh_wallets()
        wallets = followed_wallets()
        if not wallets:
            return  # sin ranking aún (backfill en curso); se reintenta

        sells_by_token: dict[str, str] = {}
        for wallet in wallets:
            try:
                trades = recent_trades(wallet)
            except Exception as e:
                print(f"  ! data-api falló para {wallet[:10]}…: {e}")
                continue

            cursor = self.state["cursors"].get(wallet)
            newest = max((int(t.get("timestamp", 0)) for t in trades), default=0)
            if cursor is None:
                # Primera vez que vemos este wallet: no copiar su histórico.
                self.state["cursors"][wallet] = newest
                continue

            new = sorted(
                (t for t in trades if int(t.get("timestamp", 0)) > cursor),
                key=lambda t: int(t.get("timestamp", 0)),
            )
            for t in new:
                side = (t.get("side") or "").upper()
                if side == "BUY":
                    self.maybe_enter(t, wallet)
                elif side == "SELL":
                    sells_by_token[str(t.get("asset", ""))] = wallet
            if newest > cursor:
                self.state["cursors"][wallet] = newest

        self.check_exits(sells_by_token)
        positions.save_state(self.state)

    def run(self) -> None:
        mode = "LIVE — órdenes reales" if self.executor else "SIM — paper trading"
        wallets = followed_wallets()
        print(f"Copy-trader iniciado [{mode}]")
        print(f"  Wallets seguidos : {len(wallets)}")
        print(f"  Sizing           : {'%.0f%% balance' % (config.TRADE_SIZE_PCT * 100) if config.TRADE_SIZE_PCT > 0 else '$%.2f fijo' % config.TRADE_SIZE_USD}")
        print(f"  SL/TP            : {config.STOP_LOSS_PCT:.0%} / {config.TAKE_PROFIT_PCT:.0%}")
        print(f"  Máx posiciones   : {config.MAX_OPEN_POSITIONS}")
        self.notifier.send(
            f"🤖 <b>Copy-trader iniciado [{ 'LIVE' if self.executor else 'SIM' }]</b>\n"
            f"Siguiendo {len(wallets)} wallets · "
            f"SL {config.STOP_LOSS_PCT:.0%} · TP {config.TAKE_PROFIT_PCT:.0%} · "
            f"máx {config.MAX_OPEN_POSITIONS} posiciones"
        )

        while True:
            try:
                self.cycle()
            except Exception as e:
                print(f"! ciclo falló: {e}")
                self.notifier.notify_error("ciclo de estrategia", str(e))
            time.sleep(config.STRATEGY_POLL_SECONDS)


if __name__ == "__main__":
    CopyTrader().run()
