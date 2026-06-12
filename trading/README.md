# Trading real con billetera Phantom

Este módulo deja TODO preparado para operar en real en Polymarket firmando con
la clave EVM de Phantom. **Queda bloqueado** (`AUTO_EXECUTE=false`) hasta que
terminemos de recoger datos, simular y decidir ir a real.

## Cómo funciona

Polymarket corre en **Polygon (EVM)**. Phantom soporta EVM además de Solana,
así que sirve para firmar: se exporta la clave privada de la red
Ethereum/Polygon y `py-clob-client` (cliente oficial del CLOB) firma las
órdenes con ella. Las órdenes en el CLOB son *gasless* (no pagas gas por orden).

## Preparación de la cuenta nueva (una sola vez)

1. **Crear/elegir cuenta en Phantom** (separada de la actual para no mezclar).
2. Entrar en [polymarket.com](https://polymarket.com) → conectar con Phantom.
   Polymarket crea tu **dirección de depósito (proxy)** — visible en tu perfil.
3. **Depositar USDC** en esa dirección proxy (red Polygon). El depósito desde
   la web ya configura los approvals necesarios.
4. **Exportar la clave EVM de Phantom**: Ajustes → Administrar cuentas →
   (tu cuenta) → Mostrar clave privada → **Ethereum** (sirve para Polygon).
5. Rellenar en `.env` (local) o variables de Railway:

```bash
POLYMARKET_PRIVATE_KEY=<clave EVM exportada de Phantom>
POLYMARKET_FUNDER_ADDRESS=<dirección proxy de tu perfil Polymarket>
SIGNATURE_TYPE=2          # wallet de navegador conectada (caso Phantom)
AUTO_EXECUTE=false        # ¡sigue en false hasta decidir ir a real!
```

> Si prefieres operar sin proxy (fondos directamente en la EOA de Phantom),
> usa `SIGNATURE_TYPE=0` y `POLYMARKET_FUNDER_ADDRESS` vacío — pero entonces
> los approvals de USDC/CTF hay que hacerlos a mano. Lo normal y recomendado
> es el flujo proxy (tipo 2).

## Verificar credenciales (sin enviar órdenes)

```bash
# Local (dentro del contenedor)
docker exec poly-data python -m trading.check_connection
```

Imprime la dirección del signer, el balance USDC y las órdenes abiertas.
No envía ninguna orden.

## Activar el modo real (cuando lo decidamos)

1. Validar simulaciones con los datos de `processed/trades.csv`.
2. Poner `AUTO_EXECUTE=true` en el entorno.
3. Usar `trading.executor.PolymarketExecutor` desde la estrategia:

```python
from trading.executor import PolymarketExecutor

ex = PolymarketExecutor()
ex.buy_limit(token_id, price=0.55, size=10)   # 10 shares a $0.55
ex.cancel_all()
```

Mientras `AUTO_EXECUTE=false`, cualquier intento de orden lanza un error con
mensaje claro — imposible operar por accidente.

## Seguridad

- La clave privada **nunca** va al repositorio (`.env` está gitignored); en
  Railway se define como variable del servicio.
- Capital inicial recomendado al activar: **$100–150 USDC**.
