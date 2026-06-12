"""On-chain balance reader for the Polymarket proxy wallet.

Reads ERC-20 balances directly from Polygon via JSON-RPC (eth_call),
bypassing the CLOB API which only reports balances after the proxy has
approved the CTF Exchange (i.e., after the first trade).
"""
import os
import requests

# Polymarket collateral tokens on Polygon (Mainnet)
_TOKENS = {
    "USDC":   "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",  # native USDC (nuevas cuentas)
    "USDC.e": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",  # bridged USDC.e (cuentas legacy)
    "pUSD":   "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",  # wrapper Polymarket (cuentas legacy)
}

_SELECTOR = "0x70a08231"  # keccak256("balanceOf(address)")[:4]

_FALLBACK_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic",
]


def _eth_call(token_address: str, wallet_address: str, rpc_url: str) -> float:
    padded = wallet_address.lower().replace("0x", "").zfill(64)
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": token_address, "data": _SELECTOR + padded}, "latest"],
        "id": 1,
    }
    resp = requests.post(rpc_url, json=payload, timeout=10)
    resp.raise_for_status()
    result = resp.json().get("result", "0x0") or "0x0"
    return int(result, 16) / 1_000_000


def proxy_balance(proxy_address: str, rpc_url: str | None = None) -> dict:
    """Return on-chain balances for all Polymarket collateral tokens at proxy_address."""
    if not rpc_url:
        rpc_url = os.getenv("POLYGON_RPC_URL", _FALLBACK_RPCS[0])

    urls_to_try = [rpc_url] + [u for u in _FALLBACK_RPCS if u != rpc_url]
    balances: dict = {}

    for name, token_addr in _TOKENS.items():
        for url in urls_to_try:
            try:
                balances[name] = _eth_call(token_addr, proxy_address, url)
                break
            except Exception:
                continue
        else:
            balances[name] = None  # todos los RPCs fallaron

    total = sum(v for v in balances.values() if isinstance(v, float))
    return {"tokens": balances, "total_usd": total}
