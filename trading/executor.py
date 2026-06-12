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

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    MarketOrderArgs,
    OrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY, SELL

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
        if config.SIGNATURE_TYPE in (1, 2):
            kwargs["signature_type"] = config.SIGNATURE_TYPE
            kwargs["funder"] = config.FUNDER_ADDRESS
        self.client = ClobClient(**kwargs)
        self.client.set_api_creds(self.client.create_or_derive_api_creds())
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

    def order_book(self, token_id: str):
        return self.client.get_order_book(token_id)

    def open_orders(self):
        return self.client.get_orders()

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
            OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
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
        order = self.client.create_order(
            OrderArgs(token_id=token_id, price=price, size=size, side=SELL)
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
        """Orden a mercado: gasta `usd_amount` USDC en el token (FOK)."""
        self._guard()
        order = self.client.create_market_order(
            MarketOrderArgs(token_id=token_id, amount=usd_amount, side=BUY)
        )
        result = self.client.post_order(order, OrderType.FOK)
        mid = self.midpoint(token_id).get("mid", 0.0)
        self.notifier.notify_trade_opened(
            question=question or token_id,
            direction="BUY_YES",
            price=float(mid),
            size_usd=usd_amount,
            market_url=market_url,
            is_live=True,
        )
        return result

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
        order = self.client.create_market_order(
            MarketOrderArgs(token_id=token_id, amount=shares, side=SELL)
        )
        result = self.client.post_order(order, OrderType.FOK)
        if question and entry_price > 0:
            mid = self.midpoint(token_id).get("mid", 0.0)
            self.notifier.notify_trade_closed(
                question=question,
                direction="BUY_YES",
                entry_price=entry_price,
                exit_price=float(mid),
                size_usd=shares * entry_price,
                reason=reason,
                market_url=market_url,
            )
        return result

    def cancel_all(self):
        self._guard()
        return self.client.cancel_all()
