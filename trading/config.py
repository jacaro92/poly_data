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
#   1 = POLY_PROXY (cuentas creadas con email/Magic Link — legacy V1)
#   2 = POLY_GNOSIS_SAFE (proxy Gnosis Safe — legacy V1)
#   3 = POLY_1271 (EIP-1271, "deposit wallet flow" de CLOB V2). Tras la
#       migración a V2 (28-abr-2026) las cuentas de wallet de navegador
#       (Phantom/MetaMask) DEBEN usar 3: con 1/2 el CLOB devuelve balance 0 y
#       rechaza las órdenes con "maker address not allowed, please use the
#       deposit wallet flow". Verificado: con 3 el balance real aparece y los
#       allowances están a max uint. El funder sigue siendo la dirección proxy.
SIGNATURE_TYPE = int(os.environ.get("SIGNATURE_TYPE", "3"))

# Gate de ejecución real. false => solo lectura/simulación.
AUTO_EXECUTE = os.environ.get("AUTO_EXECUTE", "false").lower() == "true"

# Capital al pasar a LIVE. Sirve para el P&L REAL de cuenta del dashboard:
# pnl_total = (caja + valor de posiciones abiertas) - INITIAL_CAPITAL_USD.
# Esta métrica cruza con la "Cartera" de Polymarket (mark-to-market), a
# diferencia del P&L realizado por libros (solo cierres). Ajustar por entorno
# si se deposita/retira.
INITIAL_CAPITAL_USD = float(os.environ.get("INITIAL_CAPITAL_USD", "14.96"))

# ── Telegram notifications ──────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Sizing por trade ────────────────────────────────────────────────────────
# TRADE_SIZE_PCT > 0 tiene prioridad sobre TRADE_SIZE_USD.
# Ejemplo: 0.10 = 10% del balance disponible por trade.
TRADE_SIZE_USD = float(os.environ.get("TRADE_SIZE_USD", "5.0"))
TRADE_SIZE_PCT = float(os.environ.get("TRADE_SIZE_PCT", "0.0"))

# ── Gestión de riesgo ───────────────────────────────────────────────────────
# 0.0 = deshabilitado. Valores en fracción del valor del token desde entrada.
# Ejemplo: STOP_LOSS_PCT=0.30 → cierra si el token pierde 30% desde entrada.
# Ejemplo: TAKE_PROFIT_PCT=0.40 → cierra si el token sube 40% desde entrada.
# NOTA: Polymarket no tiene SL/TP nativos; requiere un loop de monitoreo
# que llame a executor.check_sl_tp() y ejecute la venta si se dispara.
STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PCT", "0.0"))
TAKE_PROFIT_PCT = float(os.environ.get("TAKE_PROFIT_PCT", "0.0"))

# ── Estrategia copy-trading (trading/strategy.py) ───────────────────────────
# Wallets a seguir, separadas por comas. Si está vacío se leen las top
# COPY_TOP_N de processed/top_wallets.csv (generado por wallet_analyzer).
COPY_WALLETS = [
    w.strip().lower()
    for w in os.environ.get("COPY_WALLETS", "").split(",")
    if w.strip()
]
COPY_TOP_N = int(os.environ.get("COPY_TOP_N", "5"))

# Solo se copian trades del wallet seguido con tamaño >= este umbral (USD).
# Filtra el ruido de micro-trades y rebalanceos.
MIN_COPY_TRADE_USD = float(os.environ.get("MIN_COPY_TRADE_USD", "50.0"))

# ── Filtro de calidad de wallets (wallet_analyzer) ──────────────────────────
# Excluye del ranking wallets NO copiables con retraso:
#   - Market-makers / bots HFT: nº de acciones disparatado (ganan por spread y
#     rewards, no por dirección; copiarlos paga el peaje que ellos cobran).
#   - Arbitrajistas de resolución: win_rate ~100% con muy pocos lotes cerrados
#     (compran a 0.98 justo antes de resolver; sin margen copiable).
MIN_WALLET_LOTS = int(os.environ.get("MIN_WALLET_LOTS", "10"))       # track record mínimo
MAX_WALLET_TRADES = int(os.environ.get("MAX_WALLET_TRADES", "20000"))  # excluye MM/HFT
MAX_WALLET_WIN_RATE = float(os.environ.get("MAX_WALLET_WIN_RATE", "0.99"))  # excluye arb perfecto

# ── Filtro de spread en la entrada ──────────────────────────────────────────
# No copiar en mercados ilíquidos: con spread ancho se paga el peaje al cruzar
# el libro en compra (ask) y venta (bid). Spread relativo = (ask-bid)/mid.
# 0 = desactivado. Sin libro (sin bids/asks) también se descarta.
MAX_SPREAD_PCT = float(os.environ.get("MAX_SPREAD_PCT", "0.05"))

# Límites de cartera y de entrada.
MAX_OPEN_POSITIONS = int(os.environ.get("MAX_OPEN_POSITIONS", "3"))
ENTRY_PRICE_MIN = float(os.environ.get("ENTRY_PRICE_MIN", "0.10"))
ENTRY_PRICE_MAX = float(os.environ.get("ENTRY_PRICE_MAX", "0.90"))

# ── Diversificación / anti-correlación ──────────────────────────────────────
# Evita sobre-exponer la cartera a una sola apuesta. El daño de junio fue tener
# 9/10 posiciones del MISMO wallet y del MISMO evento (los varios candidatos a
# "firmar el acuerdo Irán" son un único evento desglosado → correlación total):
# si la narrativa se mueve en contra, caen todas juntas y el ranking de wallets
# no sirve de nada.
#   MAX_POS_PER_WALLET: máx posiciones abiertas copiadas del mismo source_wallet.
#   MAX_POS_PER_EVENT : máx posiciones abiertas en el mismo evento (eventSlug).
# 0 = sin límite.
MAX_POS_PER_WALLET = int(os.environ.get("MAX_POS_PER_WALLET", "2"))
MAX_POS_PER_EVENT = int(os.environ.get("MAX_POS_PER_EVENT", "2"))

# Salidas adicionales a SL/TP:
#   COPY_EXIT: cerrar cuando el wallet seguido vende el mismo token.
#   MAX_HOLD_HOURS: cierre por tiempo (0 = sin límite).
COPY_EXIT = os.environ.get("COPY_EXIT", "true").lower() == "true"
MAX_HOLD_HOURS = float(os.environ.get("MAX_HOLD_HOURS", "72"))

# ── Filtro de ventana de resolución del mercado (entrada) ───────────────────
# Solo copiar mercados que RESUELVEN pronto (end_date_iso dentro de esta
# ventana desde ahora). Distinto de MAX_HOLD_HOURS (que limita cuánto aguanta
# el bot la posición): esto evita ENTRAR en mercados de cola larga (p.ej.
# geopolítica con cierre a 45 días) donde el capital queda atrapado y el
# TIME_EXIT acaba cerrando al midpoint con pérdida. 0 = sin filtro.
MAX_RESOLUTION_HOURS = float(os.environ.get("MAX_RESOLUTION_HOURS", "72"))

# Cadencia del loop de la estrategia (segundos).
STRATEGY_POLL_SECONDS = int(os.environ.get("STRATEGY_POLL_SECONDS", "30"))

# Cada cuántas horas la estrategia regenera processed/top_wallets.csv a partir
# de trades.csv (0 = nunca; solo aplica si COPY_WALLETS está vacío).
WALLET_REFRESH_HOURS = float(os.environ.get("WALLET_REFRESH_HOURS", "6"))

# Puerto del dashboard web (trading/dashboard.py). Railway inyecta PORT al
# generar un dominio público; tiene prioridad sobre DASHBOARD_PORT.
DASHBOARD_PORT = int(os.environ.get("PORT", os.environ.get("DASHBOARD_PORT", "8050")))

# Contraseña del dashboard (HTTP Basic Auth, usuario "admin"). OBLIGATORIA
# si el dashboard es público en internet; vacía = sin auth (solo local).
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")


def compute_size(balance_usdc: float) -> float:
    """Devuelve el tamaño a invertir en USD según la configuración."""
    if TRADE_SIZE_PCT > 0:
        return round(balance_usdc * TRADE_SIZE_PCT, 2)
    return TRADE_SIZE_USD


def assert_ready_for_live() -> None:
    """Falla con un mensaje claro si falta algo para operar en real."""
    missing = []
    if not PRIVATE_KEY:
        missing.append("POLYMARKET_PRIVATE_KEY")
    if SIGNATURE_TYPE in (1, 2, 3) and not FUNDER_ADDRESS:
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
