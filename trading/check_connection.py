"""Verificación SOLO LECTURA de las credenciales de trading.

No envía ninguna orden. Ejecutar tras configurar el .env con la clave de
Phantom para confirmar que la firma y la cuenta funcionan:

    python -m trading.check_connection
"""

from trading import config


def main() -> None:
    print(f"CLOB host       : {config.CLOB_HOST}")
    print(f"Signature type  : {config.SIGNATURE_TYPE}")
    print(f"Funder          : {config.FUNDER_ADDRESS or '(no definido)'}")
    print(f"AUTO_EXECUTE    : {config.AUTO_EXECUTE}")

    if not config.PRIVATE_KEY:
        print("\n✗ POLYMARKET_PRIVATE_KEY vacía — exporta la clave EVM de Phantom.")
        return

    from trading.executor import PolymarketExecutor

    ex = PolymarketExecutor()
    print(f"\n✓ Firma OK — EOA del signer: {ex.signer_address()}")
    bal = ex.usdc_balance()
    print(f"✓ Balance USDC  : {bal}")

    open_orders = ex.open_orders()
    print(f"✓ Órdenes abiertas: {len(open_orders)}")
    print("\nTodo listo. Las órdenes reales siguen bloqueadas hasta AUTO_EXECUTE=true.")


if __name__ == "__main__":
    main()
