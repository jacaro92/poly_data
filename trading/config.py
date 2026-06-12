"""Configuración del módulo de trading real (Polymarket CLOB).

Todas las credenciales vienen de variables de entorno (.env en local,
variables del servicio en Railway). El gate AUTO_EXECUTE controla si se
permiten órdenes reales: mientras sea false, cualquier intento de enviar
una orden lanza RuntimeError.
"""

import os

from dotenv import load_dotenv

load_dotenv()

CLOB_HOST = os.environ.get("CLOB_HOST", "https://clob.polymarket.com")
POLYGON_CHAIN_ID = 137

# Clave privada EVM exportada de Phantom (Ajustes → Administrar cuentas →
# cuenta → Mostrar clave privada → red Ethereum/Polygon).
PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "")

# Dirección que CUSTODIA los fondos (USDC.e en Polygon):
#  - Si depositaste vía web de Polymarket conectando Phantom: la dirección
#    proxy que muestra tu perfil de Polymarket (empieza distinta a tu EOA).
#  - Si vas a operar directo con la EOA de Phantom (sin proxy): tu propia
#    dirección de Phantom.
FUNDER_ADDRESS = os.environ.get("POLYMARKET_FUNDER_ADDRESS", "")

# Tipo de firma del CLOB:
#   0 = EOA directa (fondos en tu dirección Phantom, requiere approvals manuales)
#   1 = POLY_PROXY (cuentas creadas con email/Magic Link)
#   2 = POLY_GNOSIS_SAFE (cuentas creadas conectando una wallet de navegador
#       como Phantom/MetaMask en polymarket.com — el caso normal con Phantom)
SIGNATURE_TYPE = int(os.environ.get("SIGNATURE_TYPE", "2"))

# Gate de ejecución real. false => solo lectura/simulación.
AUTO_EXECUTE = os.environ.get("AUTO_EXECUTE", "false").lower() == "true"


def assert_ready_for_live() -> None:
    """Falla con un mensaje claro si falta algo para operar en real."""
    missing = []
    if not PRIVATE_KEY:
        missing.append("POLYMARKET_PRIVATE_KEY")
    if SIGNATURE_TYPE in (1, 2) and not FUNDER_ADDRESS:
        missing.append("POLYMARKET_FUNDER_ADDRESS")
    if missing:
        raise RuntimeError(
            f"Faltan variables para operar: {', '.join(missing)}. "
            "Exporta la clave EVM desde Phantom y copia la dirección de "
            "depósito de tu perfil de Polymarket."
        )
    if not AUTO_EXECUTE:
        raise RuntimeError(
            "AUTO_EXECUTE=false: el envío de órdenes reales está desactivado. "
            "Ponlo a true SOLO cuando las simulaciones convenzan."
        )
