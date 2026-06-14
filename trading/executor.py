"""Ejecutor de órdenes en el CLOB de Polymarket firmando con la clave EVM
exportada de Phantom.

Uso típico (cuando AUTO_EXECUTE=true):

    from trading.executor import PolymarketExecutor

    ex = PolymarketExecutor()
    balance = ex.usdc_balance()["total"]
    size = ex.compute_size(balance)         # usa TRADE_SIZE_USD o TRADE_SIZE_PCT
    print(ex.midpoint(token_id))
    ex.buy_limit(token_id, price=0.55, size=size, question="¿Ganará X?")
    ex.sell_limit(token_id, price=0.80, size=size)
    ex.cancel_all()

Las funciones de solo lectura (balance, midpoint, open_orders) funcionan
siempre; las que envían órdenes exigen AUTO_EXECUTE=true.
"""

import time

# CLOB V2 (Polymarket migró el dominio EIP-712 de "1" a "2" el 28-abr-2026;
# el paquete viejo py-clob-client quedó obsoleto → "invalid order version").
from py_clob_client_v2 import ClobClient
from py_clob_client_v2.clob_types import (
    MarketOrderArgsV2,
    OrderArgsV2,
    OrderType,
    TradeParams,
)
from py_clob_client_v2.order_builder.constants import BUY, SELL

from trading import config
from trading.telegram_notifier import TelegramNotifier


class PolymarketExecutor:
    def __init__(self):
        if not config.PRIVATE_KEY:
            raise RuntimeError(
                "POLYMARKET_PRIVATE_KEY vacía: exporta la clave EVM desde "
                "Phantom (Ajustes → Administrar cuentas → Mostrar clave privada)."
            )
        kwargs = dict(
            host=config.CLOB_HOST,
            key=config.PRIVATE_KEY,
            chain_id=config.POLYGON_CHAIN_ID,
        )
        if config.SIGNATURE_TYPE in (1, 2, 3):
            kwargs["signature_type"] = config.SIGNATURE_TYPE
            kwargs["funder"] = config.FUNDER_ADDRESS
        self.client = ClobClient(**kwargs)
        self.client.set_api_creds(self.client.create_or_derive_api_key())
        self.notifier = TelegramNotifier()

    # ── Solo lectura ──────────────────────────────────────────────────────────

    def signer_address(self) -> str:
        return self.client.get_address()

    def usdc_balance(self) -> dict:
        from trading.wallet_utils import proxy_balance
        return proxy_balance(config.FUNDER_ADDRESS)

    def compute_size(self, balance_usdc: float) -> float:
        """Tamaño a invertir según TRADE_SIZE_PCT o TRADE_SIZE_USD."""
        return config.compute_size(balance_usdc)

    def midpoint(self, token_id: str) -> dict:
        return self.client.get_midpoint(token_id)

    def _midpoint_price(self, token_id: str) -> float:
        """Precio medio como float, tolerante a fallos/formatos (solo para
        notificaciones; nunca debe romper una orden ya enviada)."""
        try:
            mid = self.client.get_midpoint(token_id)
            if isinstance(mid, dict):
                mid = mid.get("mid", 0.0)
            return float(mid)
        except Exception:
            return 0.0

    def held_shares(self, token_id: str) -> float:
        """Shares (outcome tokens) que realmente se poseen del token, según el
        CLOB. El market-buy FOK deja algo menos de shares que size/precio
        (fees/redondeo); vender pos['shares'] da 'not enough balance'. Como el
        SDK redondea el size a la baja, vender este saldo real nunca excede lo
        disponible. Devuelve 0.0 si no se puede leer (el caller decide).

        Se reintenta una vez: la PRIMERA llamada CONDITIONAL de cada cliente
        py-clob-client-v2 falla con 'assetId invalid value -1' (bug del SDK);
        la segunda ya resuelve bien el tokenId."""
        from py_clob_client_v2.clob_types import (
            AssetType,
            BalanceAllowanceParams,
        )
        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL,
            token_id=token_id,
            signature_type=config.SIGNATURE_TYPE,
        )
        for attempt in range(2):
            try:
                resp = self.client.get_balance_allowance(params)
                raw = float((resp or {}).get("balance", 0))
                return raw / 1_000_000.0
            except Exception:
                if attempt == 0:
                    continue
                return 0.0
        return 0.0

    def collateral_balance(self) -> float:
        """Saldo de colateral (pUSD/USDC) en el CLOB, en USD. -1.0 si falla.
        Sirve para medir el gasto/ingreso real de una orden por diferencia
        de saldo antes/después (las respuestas del CLOB no traen el importe
        ejecutado)."""
        try:
            from py_clob_client_v2.clob_types import (
                AssetType,
                BalanceAllowanceParams,
            )
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=config.SIGNATURE_TYPE,
            )
            resp = self.client.get_balance_allowance(params)
            return float((resp or {}).get("balance", 0)) / 1_000_000.0
        except Exception:
            return -1.0

    def _last_fill_price(self, token_id: str, side: str, since_ts: int) -> float:
        """Precio de ejecución REAL de nuestra última orden en este token,
        leído de get_trades (fuente autoritativa). El CLOB no devuelve el
        importe ejecutado en la respuesta de la orden, y el saldo de colateral
        no se actualiza síncrono; get_trades sí registra el fill al instante.
        Media ponderada por tamaño si la orden cruzó varios niveles. Devuelve
        0.0 si no se encuentra (el caller cae al midpoint)."""
        for _ in range(4):
            try:
                trades = self.client.get_trades(
                    TradeParams(asset_id=token_id), only_first_page=True
                )
            except Exception:
                trades = []
            ours = [
                t for t in trades
                if t.get("side") == side
                and int(t.get("match_time", 0) or 0) >= since_ts
            ]
            if ours:
                latest = max(int(t.get("match_time", 0) or 0) for t in ours)
                batch = [t for t in ours if int(t.get("match_time", 0) or 0) == latest]
                sz = sum(float(t.get("size", 0) or 0) for t in batch)
                if sz > 0:
                    return sum(
                        float(t.get("price", 0) or 0) * float(t.get("size", 0) or 0)
                        for t in batch
                    ) / sz
            time.sleep(0.7)
        return 0.0

    def order_book(self, token_id: str):
        return self.client.get_order_book(token_id)

    def open_orders(self):
        return self.client.get_open_orders()

    def check_sl_tp(
        self,
        direction: str,
        entry_price: float,
        current_price: float,
    ) -> str | None:
        """Devuelve 'STOP_LOSS', 'TAKE_PROFIT' o None.

        El precio del token en Polymarket va de 0 a 1. La caída/subida se mide
        en términos del valor del token en la dirección comprada:
          - BUY_YES: el valor del token es el precio (YES price)
          - BUY_NO: el valor del token es (1 - price)
        """
        entry_cost = entry_price if direction == "BUY_YES" else (1.0 - entry_price)
        current_cost = current_price if direction == "BUY_YES" else (1.0 - current_price)
        if entry_cost <= 0:
            return None
        change = (current_cost - entry_cost) / entry_cost
        if config.STOP_LOSS_PCT > 0 and change <= -config.STOP_LOSS_PCT:
            return "STOP_LOSS"
        if config.TAKE_PROFIT_PCT > 0 and change >= config.TAKE_PROFIT_PCT:
            return "TAKE_PROFIT"
        return None

    # ── Escritura (requiere AUTO_EXECUTE=true) ────────────────────────────────

    def _guard(self) -> None:
        config.assert_ready_for_live()

    def buy_limit(
        self,
        token_id: str,
        price: float,
        size: float,
        question: str = "",
        market_url: str = "",
    ):
        """Orden límite de compra: `size` shares a `price` (0-1)."""
        self._guard()
        order = self.client.create_order(
            OrderArgsV2(token_id=token_id, price=price, size=size, side=BUY)
        )
        result = self.client.post_order(order, OrderType.GTC)
        self.notifier.notify_trade_opened(
            question=question or token_id,
            direction="BUY_YES",
            price=price,
            size_usd=size * price,
            market_url=market_url,
            is_live=True,
        )
        return result

    def sell_limit(
        self,
        token_id: str,
        price: float,
        size: float,
        question: str = "",
        market_url: str = "",
        entry_price: float = 0.0,
        reason: str = "MANUAL",
    ):
        self._guard()
        held = self.held_shares(token_id)
        if held > 0:
            size = min(size, held)
        order = self.client.create_order(
            OrderArgsV2(token_id=token_id, price=price, size=size, side=SELL)
        )
        result = self.client.post_order(order, OrderType.GTC)
        if question and entry_price > 0:
            self.notifier.notify_trade_closed(
                question=question,
                direction="BUY_YES",
                entry_price=entry_price,
                exit_price=price,
                size_usd=size * entry_price,
                reason=reason,
                market_url=market_url,
            )
        return result

    def buy_market(
        self,
        token_id: str,
        usd_amount: float,
        question: str = "",
        market_url: str = "",
    ):
        """Orden a mercado: gasta `usd_amount` USDC en el token (FOK).

        Devuelve dict con el resultado y el FILL REAL (precio medio y coste
        efectivos, medidos por delta de colateral y shares recibidas), para
        contabilizar el P&L con el precio ejecutado y no con el midpoint
        (que ignora el spread: se compra al ask)."""
        self._guard()
        t0 = int(time.time()) - 2
        order = self.client.create_market_order(
            MarketOrderArgsV2(token_id=token_id, amount=usd_amount, side=BUY)
        )
        result = self.client.post_order(order, OrderType.FOK)
        shares = self.held_shares(token_id)
        fill_price = self._last_fill_price(token_id, BUY, t0) or self._midpoint_price(token_id) or 0.0
        spent = round(fill_price * shares, 2) if (fill_price > 0 and shares > 0) else round(usd_amount, 2)
        self.notifier.notify_trade_opened(
            question=question or token_id,
            direction="BUY_YES",
            price=float(fill_price),
            size_usd=spent,
            market_url=market_url,
            is_live=True,
        )
        return {
            "result": result,
            "fill_price": round(fill_price, 4),
            "shares": round(shares, 4),
            "spent": spent,
        }

    def sell_market(
        self,
        token_id: str,
        shares: float,
        question: str = "",
        market_url: str = "",
        entry_price: float = 0.0,
        reason: str = "MANUAL",
    ):
        self._guard()
        held = self.held_shares(token_id)
        if held > 0:
            shares = min(shares, held)
        t0 = int(time.time()) - 2
        order = self.client.create_market_order(
            MarketOrderArgsV2(token_id=token_id, amount=shares, side=SELL)
        )
        result = self.client.post_order(order, OrderType.FOK)
        # Precio de venta REAL (se vende al bid) leído de get_trades; fallback
        # al midpoint si no se encuentra el fill.
        fill_price = self._last_fill_price(token_id, SELL, t0) or self._midpoint_price(token_id) or 0.0
        proceeds = round(fill_price * shares, 2) if fill_price > 0 else 0.0
        if question and entry_price > 0:
            self.notifier.notify_trade_closed(
                question=question,
                direction="BUY_YES",
                entry_price=entry_price,
                exit_price=float(fill_price),
                size_usd=shares * entry_price,
                reason=reason,
                market_url=market_url,
            )
        return {
            "result": result,
            "fill_price": round(fill_price, 4),
            "proceeds": round(proceeds, 2),
            "shares": round(shares, 4),
        }

    def cancel_all(self):
        self._guard()
        return self.client.cancel_all()
