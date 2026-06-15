"""Persistencia de posiciones del agente en data/positions.json.

Un único escritor (trading/strategy.py); el dashboard solo lee.
La escritura es atómica (tmp + replace) para que un corte a mitad de
escritura no corrompa el estado.

Estructura del JSON:
    {
      "open":    [posición, ...],
      "closed":  [posición cerrada, ...],
      "cursors": {wallet: last_trade_timestamp},   # para no re-copiar señales
      "signals": [últimas señales vistas, ...]     # log para el dashboard
    }
"""

import json
import os
import uuid
from datetime import datetime, timezone

STATE_FILE = os.path.join("data", "positions.json")

_DEFAULT = {"open": [], "closed": [], "cursors": {}, "signals": []}

MAX_SIGNALS = 50
MAX_CLOSED = 500


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def load_state() -> dict:
    if os.path.isfile(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
            for key, default in _DEFAULT.items():
                state.setdefault(key, default if not isinstance(default, (list, dict)) else type(default)())
            return state
        except Exception:
            pass
    return {k: (type(v)() if isinstance(v, (list, dict)) else v) for k, v in _DEFAULT.items()}


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def open_position(
    state: dict,
    token_id: str,
    question: str,
    outcome: str,
    entry_price: float,
    size_usd: float,
    source_wallet: str,
    market_url: str = "",
    is_live: bool = False,
    event_slug: str = "",
) -> dict:
    pos = {
        "id": uuid.uuid4().hex[:12],
        "token_id": token_id,
        "question": question,
        "outcome": outcome,
        "entry_price": round(entry_price, 4),
        "size_usd": round(size_usd, 2),
        "shares": round(size_usd / entry_price, 4) if entry_price > 0 else 0.0,
        "source_wallet": source_wallet,
        "market_url": market_url,
        "event_slug": event_slug or _event_of_url(market_url),
        "is_live": is_live,
        "opened_at": _now_iso(),
    }
    state["open"].append(pos)
    save_state(state)
    return pos


def close_position(state: dict, pos: dict, exit_price: float, reason: str) -> dict:
    entry = pos.get("entry_price", 0.0)
    pnl_pct = (exit_price - entry) / entry if entry > 0 else 0.0
    pos.update(
        {
            "exit_price": round(exit_price, 4),
            "closed_at": _now_iso(),
            "reason": reason,
            "pnl_pct": round(pnl_pct, 4),
            "pnl_usd": round(pos.get("size_usd", 0.0) * pnl_pct, 2),
        }
    )
    state["open"] = [p for p in state["open"] if p["id"] != pos["id"]]
    state["closed"].append(pos)
    state["closed"] = state["closed"][-MAX_CLOSED:]
    save_state(state)
    return pos


def log_signal(state: dict, signal: dict) -> None:
    signal["seen_at"] = _now_iso()
    state["signals"].append(signal)
    state["signals"] = state["signals"][-MAX_SIGNALS:]
    # save_state lo hace el caller al final del ciclo


def has_open_token(state: dict, token_id: str) -> bool:
    return any(p["token_id"] == token_id for p in state["open"])


def _event_of_url(market_url: str) -> str:
    """Slug del evento desde la URL (…/event/<slug>). '' si no aplica."""
    return market_url.rstrip("/").rsplit("/", 1)[-1] if market_url else ""


def _event_of(pos: dict) -> str:
    """Evento de una posición (event_slug guardado, o derivado de market_url
    para posiciones antiguas sin el campo)."""
    return pos.get("event_slug") or _event_of_url(pos.get("market_url", ""))


def count_open_by_wallet(state: dict, wallet: str) -> int:
    """Posiciones abiertas copiadas del mismo wallet fuente."""
    w = (wallet or "").lower()
    return sum(1 for p in state["open"] if (p.get("source_wallet") or "").lower() == w)


def count_open_by_event(state: dict, event_slug: str) -> int:
    """Posiciones abiertas en el mismo evento (mercados correlacionados:
    p.ej. los varios candidatos a 'firmar el acuerdo' son un solo evento)."""
    if not event_slug:
        return 0
    return sum(1 for p in state["open"] if _event_of(p) == event_slug)
