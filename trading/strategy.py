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
TRADES_CSV = os.path.join("processed", "trades.csv")

# Precio extremo = mercado decidido de facto.
RESOLVED_HI = 0.99
RESOLVED_LO = 0.01

_session = requests.Session()


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
        self._balance_cache: tuple[float, float] = (0.0, 0.0)  # (ts, total)
        self._last_analyze = 0.0

    def maybe_refresh_wallets(self) -> None:
        """Regenera el ranking de wallets cada WALLET_REFRESH_HOURS cuando hay
        datos en trades.csv. Así el sistema se auto-arranca al terminar el
        backfill sin ejecutar wallet_analyzer a mano."""
        if config.COPY_WALLETS or config.WALLET_REFRESH_HOURS <= 0:
            return
        if time.time() - self._last_analyze < config.WALLET_REFRESH_HOURS * 3600:
            return
        if not os.path.isfile(TRADES_CSV) or os.path.getsize(TRADES_CSV) < 10_000:
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

        skip = None
        if usd < config.MIN_COPY_TRADE_USD:
            skip = f"trade ${usd:.0f} < umbral ${config.MIN_COPY_TRADE_USD:.0f}"
        elif not (config.ENTRY_PRICE_MIN <= price <= config.ENTRY_PRICE_MAX):
            skip = f"precio {price:.2f} fuera de [{config.ENTRY_PRICE_MIN}, {config.ENTRY_PRICE_MAX}]"
        elif positions.has_open_token(self.state, token_id):
            skip = "ya en posición"
        elif len(self.state["open"]) >= config.MAX_OPEN_POSITIONS:
            skip = f"límite de {config.MAX_OPEN_POSITIONS} posiciones"

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
        if config.AUTO_EXECUTE and self.balance() < size_usd:
            print(f"  ⏭ balance ${self.balance():.2f} < tamaño ${size_usd:.2f}")
            return

        # Entrada al precio actual (midpoint), no al precio del wallet seguido,
        # que puede tener varios segundos de retraso.
        entry = midpoint(token_id) or price
        url = market_url(trade)

        if self.executor:
            try:
                self.executor.buy_market(token_id, size_usd, question=question, market_url=url)
            except Exception as e:
                print(f"  ✗ orden falló: {e}")
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
        )
        mode = "LIVE" if self.executor else "SIM"
        print(f"  🟢 [{mode}] COPIA {question[:50]} @ {entry:.3f}  ${size_usd:.2f}  (de {wallet[:10]}…)")

    # ── salidas ──

    def exit_position(self, pos: dict, exit_price: float, reason: str) -> None:
        if self.executor and pos.get("is_live"):
            try:
                self.executor.sell_market(
                    pos["token_id"], pos["shares"],
                    question=pos["question"], market_url=pos.get("market_url", ""),
                    entry_price=pos["entry_price"], reason=reason,
                )
            except Exception as e:
                print(f"  ✗ venta falló ({reason}): {e}")
                self.notifier.notify_error(f"sell_market {pos['question'][:50]}", str(e))
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
