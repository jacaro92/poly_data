"""
Telegram notifications for Polymarket trade events.
No-op when TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID are not configured.
"""
import logging

import requests

from trading import config

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    def __init__(self) -> None:
        self._token = config.TELEGRAM_BOT_TOKEN
        self._chat_id = config.TELEGRAM_CHAT_ID
        self._enabled = bool(self._token and self._chat_id)
        if self._enabled:
            logger.info("Telegram notifier enabled (chat_id=%s)", self._chat_id)
        else:
            logger.info(
                "Telegram notifier disabled — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to enable"
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def send(self, text: str) -> None:
        """Fire-and-forget; never raises."""
        if not self._enabled:
            return
        try:
            url = _TELEGRAM_API.format(token=self._token)
            resp = requests.post(
                url,
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if not resp.ok:
                logger.warning("Telegram send failed: %s %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.warning("Telegram notification error: %s", exc)

    def notify_trade_opened(
        self,
        question: str,
        direction: str,
        price: float,
        size_usd: float,
        market_url: str = "",
        is_live: bool = True,
    ) -> None:
        mode = "LIVE" if is_live else "SIM"
        icon = "🟢" if direction == "BUY_YES" else "🔴"
        side = "YES" if direction == "BUY_YES" else "NO"
        token_cost = price if direction == "BUY_YES" else (1.0 - price)
        url_line = f'\n<a href="{market_url}">Ver mercado</a>' if market_url else ""
        self.send(
            f"{icon} <b>ORDEN ABIERTA [{mode}]</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>{question[:80]}</b>\n\n"
            f"Lado   : BUY {side}\n"
            f"Precio : {token_cost:.4f} ({token_cost:.1%})\n"
            f"Tamaño : ${size_usd:.2f}"
            f"{url_line}"
        )

    def notify_trade_closed(
        self,
        question: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        size_usd: float,
        reason: str,
        market_url: str = "",
    ) -> None:
        entry_cost = entry_price if direction == "BUY_YES" else (1.0 - entry_price)
        exit_cost = exit_price if direction == "BUY_YES" else (1.0 - exit_price)
        pnl_pct = (exit_cost - entry_cost) / entry_cost if entry_cost > 0 else 0.0
        pnl = size_usd * pnl_pct
        icon = "✅" if pnl >= 0 else "❌"
        side = "YES" if direction == "BUY_YES" else "NO"
        url_line = f'\n<a href="{market_url}">Ver mercado</a>' if market_url else ""
        self.send(
            f"{icon} <b>ORDEN CERRADA — {reason}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"BUY {side} | {question[:60]}\n\n"
            f"Entrada : {entry_cost:.4f}\n"
            f"Salida  : {exit_cost:.4f}\n"
            f"P&L     : ${pnl:+.2f} ({pnl_pct:+.1%})"
            f"{url_line}"
        )

    def notify_balance(self, balance_usdc: float) -> None:
        self.send(
            f"💰 <b>Balance proxy</b>\n"
            f"${balance_usdc:.2f} USDC disponibles"
        )

    def notify_error(self, context: str, error: str) -> None:
        self.send(
            f"🚨 <b>ERROR EN AGENTE</b>\n"
            f"{context}\n\n"
            f"<code>{error[:400]}</code>"
        )
