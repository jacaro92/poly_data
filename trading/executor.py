"""Ejecutor de órdenes en el CLOB de Polymarket firmando con la clave EVM
exportada de Phantom.

Uso típico (cuando AUTO_EXECUTE=true):

    from trading.executor import PolymarketExecutor

    ex = PolymarketExecutor()
    print(ex.usdc_balance())
    print(ex.midpoint(token_id))
    ex.buy_limit(token_id, price=0.55, size=10)   # 10 shares a 0.55
    ex.sell_limit(token_id, price=0.80, size=10)
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
        # Con proxy (signature_type 1/2) los fondos viven en FUNDER_ADDRESS y
        # la EOA de Phantom solo firma.
        if config.SIGNATURE_TYPE in (1, 2):
            kwargs["signature_type"] = config.SIGNATURE_TYPE
            kwargs["funder"] = config.FUNDER_ADDRESS
        self.client = ClobClient(**kwargs)
        self.client.set_api_creds(self.client.create_or_derive_api_creds())

    # ── Solo lectura ──────────────────────────────────────────────────────────

    def signer_address(self) -> str:
        return self.client.get_address()

    def usdc_balance(self) -> dict:
        from trading.wallet_utils import proxy_balance
        return proxy_balance(config.FUNDER_ADDRESS)

    def midpoint(self, token_id: str) -> dict:
        return self.client.get_midpoint(token_id)

    def order_book(self, token_id: str):
        return self.client.get_order_book(token_id)

    def open_orders(self):
        return self.client.get_orders()

    # ── Escritura (requiere AUTO_EXECUTE=true) ────────────────────────────────

    def _guard(self) -> None:
        config.assert_ready_for_live()

    def buy_limit(self, token_id: str, price: float, size: float):
        """Orden límite de compra: `size` shares a `price` (0-1)."""
        self._guard()
        order = self.client.create_order(
            OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
        )
        return self.client.post_order(order, OrderType.GTC)

    def sell_limit(self, token_id: str, price: float, size: float):
        self._guard()
        order = self.client.create_order(
            OrderArgs(token_id=token_id, price=price, size=size, side=SELL)
        )
        return self.client.post_order(order, OrderType.GTC)

    def buy_market(self, token_id: str, usd_amount: float):
        """Orden a mercado: gasta `usd_amount` USDC en el token (FOK)."""
        self._guard()
        order = self.client.create_market_order(
            MarketOrderArgs(token_id=token_id, amount=usd_amount, side=BUY)
        )
        return self.client.post_order(order, OrderType.FOK)

    def sell_market(self, token_id: str, shares: float):
        self._guard()
        order = self.client.create_market_order(
            MarketOrderArgs(token_id=token_id, amount=shares, side=SELL)
        )
        return self.client.post_order(order, OrderType.FOK)

    def cancel_all(self):
        self._guard()
        return self.client.cancel_all()
