"""
Polymarket CTF Exchange V2 OrderFilled event poller.

Reads order-fill events directly from Polygon via JSON-RPC.
No Goldsky, no subgraph, no API key beyond an optional RPC URL.

Writes data/fills/YYYYMMDD_BLOCKSTART.parquet (one file per 500-block chunk,
no txHash column — reduces storage ~2x vs keeping it).

Cursor (last block scanned) is persisted in data/cursor_state.json.

Rolling retention: fill files older than FILLS_RETAIN_DAYS are deleted at
startup. If the saved cursor is older than the retention window, it is
automatically advanced so we never scan history we would immediately discard.

Timestamp optimisation: instead of one eth_getBlockByNumber per event-bearing
block (~500 calls per chunk), we fetch only the first and last block of each
chunk and linearly interpolate. Polygon blocks are ~2 s apart, so the error
is ±1 block (±2 s) — acceptable for P&L analytics.
"""

import os
import time
from datetime import datetime, timedelta, timezone

import polars as pl
from dotenv import load_dotenv
from eth_abi import decode as abi_decode
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

load_dotenv()

CTF_EXCHANGE_V2 = Web3.to_checksum_address("0xE111180000d2663C0091e4f400237545B87B996B")
V2_GENESIS_BLOCK = 84_902_353

ORDERFILLED_TOPIC = "0x" + Web3.keccak(
    text=(
        "OrderFilled(bytes32,address,address,uint8,uint256,"
        "uint256,uint256,uint256,bytes32,bytes32)"
    )
).hex().lstrip("0x")
_DATA_TYPES = ["uint8", "uint256", "uint256", "uint256", "uint256", "bytes32", "bytes32"]

# Output — one Parquet file per getLogs chunk, no txHash column.
FILLS_DIR = os.path.join("data", "fills")
CURSOR_FILE = os.path.join("data", "cursor_state.json")

# Keep fill files for this many days. Cursor is advanced if it is older
# than the retention window so we never scan history we would discard.
FILLS_RETAIN_DAYS = int(os.getenv("FILLS_RETAIN_DAYS", "14"))
# Approximate Polygon block rate used only for the cursor-advance heuristic.
_BLOCKS_PER_DAY = 43_200

BLOCK_RANGE = int(os.environ.get("POLYGON_MAX_BLOCK_RANGE", "5"))
CONFIRMATIONS = 20
RATE_LIMIT_BACKOFF_MAX = 60.0
CONNECT_MAX_RETRIES = 5
PRUNED_MAX_RETRIES = 12

DEFAULT_RPC = "https://polygon-bor-rpc.publicnode.com"


def _rpc_url() -> str:
    return os.environ.get("POLYGON_RPC_URL", DEFAULT_RPC)


def _load_cursor() -> int:
    if os.path.isfile(CURSOR_FILE):
        try:
            import json
            with open(CURSOR_FILE) as f:
                last = json.load(f).get("last_block")
            if isinstance(last, int) and last >= V2_GENESIS_BLOCK:
                return last
        except Exception:
            pass
    return V2_GENESIS_BLOCK


def _save_cursor(next_block: int) -> None:
    import json
    with open(CURSOR_FILE, "w") as f:
        json.dump({"last_block": next_block}, f)


def _enforce_retention(cursor: int, safe_latest: int) -> int:
    """Advance cursor if it is older than FILLS_RETAIN_DAYS to avoid scanning
    history we will delete immediately."""
    min_block = safe_latest - FILLS_RETAIN_DAYS * _BLOCKS_PER_DAY
    if cursor < min_block:
        print(
            f"  Cursor {cursor:,} is older than {FILLS_RETAIN_DAYS} days; "
            f"advancing to {min_block:,} (skipping {min_block - cursor:,} blocks)"
        )
        _save_cursor(min_block)
        return min_block
    return cursor


def _cleanup_old_fills() -> None:
    """Delete fill Parquet files whose date prefix is older than FILLS_RETAIN_DAYS."""
    if not os.path.isdir(FILLS_DIR):
        return
    cutoff = (
        datetime.now(tz=timezone.utc) - timedelta(days=FILLS_RETAIN_DAYS)
    ).strftime("%Y%m%d")
    removed = 0
    for fname in os.listdir(FILLS_DIR):
        if fname.endswith(".parquet") and fname[:8] < cutoff:
            try:
                os.remove(os.path.join(FILLS_DIR, fname))
                removed += 1
            except OSError:
                pass
    if removed:
        print(f"  [cleanup] Removed {removed} fill file(s) older than {FILLS_RETAIN_DAYS} days")


def _decode_log(log) -> dict:
    topics = log["topics"]
    maker = "0x" + topics[2].hex()[-40:]
    taker = "0x" + topics[3].hex()[-40:]

    data = log["data"]
    if isinstance(data, str):
        data = bytes.fromhex(data[2:] if data.startswith("0x") else data)

    side, token_id, maker_amt, taker_amt, _fee, _builder, _metadata = abi_decode(
        _DATA_TYPES, data
    )

    if side == 0:
        maker_asset_id = "0"
        taker_asset_id = str(token_id)
    else:
        maker_asset_id = str(token_id)
        taker_asset_id = "0"

    return {
        "maker": maker.lower(),
        "taker": taker.lower(),
        "makerAssetId": maker_asset_id,
        "takerAssetId": taker_asset_id,
        "makerAmountFilled": int(maker_amt),
        "takerAmountFilled": int(taker_amt),
        "_block_number": log["blockNumber"],
    }


def _is_rate_limited(msg: str) -> bool:
    return "429" in msg or "too many requests" in msg


def _call_with_rate_retry(fn, what: str):
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as e:
            if not _is_rate_limited(str(e).lower()):
                raise
            delay = min(2 ** attempt, RATE_LIMIT_BACKOFF_MAX)
            print(f"  ! rate limited (429) on {what}; waiting {delay:.0f}s then retrying")
            time.sleep(delay)
            attempt += 1


def _connect(w3, rpc: str) -> int:
    for attempt in range(CONNECT_MAX_RETRIES):
        try:
            return _call_with_rate_retry(lambda: w3.eth.block_number, "block_number")
        except Exception as e:
            if attempt + 1 >= CONNECT_MAX_RETRIES:
                raise RuntimeError(
                    f"Cannot reach Polygon RPC after {CONNECT_MAX_RETRIES} attempts: {rpc}\n"
                    f"        Check POLYGON_RPC_URL and that your plan is active. Last error: {e}"
                ) from e
            delay = min(2 ** attempt, RATE_LIMIT_BACKOFF_MAX)
            print(f"  ! RPC connect failed ({e}); retrying in {delay:.0f}s ({attempt + 1}/{CONNECT_MAX_RETRIES})")
            time.sleep(delay)


def _fetch_logs(w3, start: int, end: int):
    pruned_attempt = 0
    while True:
        try:
            return _call_with_rate_retry(
                lambda: w3.eth.get_logs(
                    {
                        "fromBlock": start,
                        "toBlock": end,
                        "address": CTF_EXCHANGE_V2,
                        "topics": [ORDERFILLED_TOPIC],
                    }
                ),
                f"get_logs {start}-{end}",
            )
        except Exception as e:
            msg = str(e).lower()
            err_body = getattr(getattr(e, "response", None), "text", "")[:300]
            if err_body:
                print(f"  ! RPC error body: {err_body}")
            if "pruned" in msg and pruned_attempt < PRUNED_MAX_RETRIES:
                delay = min(2 ** pruned_attempt, 30)
                print(
                    f"  ! pruned-history backend on get_logs {start}-{end}; "
                    f"retrying in {delay:.0f}s ({pruned_attempt + 1}/{PRUNED_MAX_RETRIES})"
                )
                time.sleep(delay)
                pruned_attempt += 1
                continue
            range_err = any(
                s in msg
                for s in (
                    "range", "too many", "limit", "result",
                    "413", "too large", "entity too large",
                    "400", "bad request", "query returned more",
                )
            )
            if range_err:
                raise RuntimeError(
                    f"RPC rejected blocks {start}-{end} ({end - start + 1} blocks): {e}\n"
                    f"        POLYGON_MAX_BLOCK_RANGE={BLOCK_RANGE} may be too high for this RPC — "
                    f"lower it and re-run (it resumes from the saved cursor).\n"
                    f"        RPC error body: {err_body}"
                ) from e
            raise


def _get_block_ts(w3, block_num: int) -> int:
    return _call_with_rate_retry(
        lambda b=block_num: w3.eth.get_block(b)["timestamp"],
        f"get_block {block_num}",
    )


def update_chain() -> None:
    os.makedirs(FILLS_DIR, exist_ok=True)
    _cleanup_old_fills()

    rpc = _rpc_url()
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    print(f"RPC: {rpc}")
    latest = _connect(w3, rpc)
    safe_latest = latest - CONFIRMATIONS
    raw_cursor = _load_cursor()
    start_block = _enforce_retention(raw_cursor, safe_latest)

    print(f"Latest block: {latest:,}  (safe: {safe_latest:,} after {CONFIRMATIONS} confs)")
    print(f"Scanning from block {start_block:,}  (retention: {FILLS_RETAIN_DAYS} days)")

    if start_block > safe_latest:
        print("Already up to date.")
        return

    cur = start_block
    total_new = 0

    while cur <= safe_latest:
        end = min(cur + BLOCK_RANGE - 1, safe_latest)
        logs = _fetch_logs(w3, cur, end)

        if logs:
            # Fetch only the start and end block timestamps; interpolate linearly
            # for all blocks in between. Reduces get_block calls from ~500 to 2
            # per chunk — critical for rate-limited public RPCs.
            ts_start = _get_block_ts(w3, cur)
            ts_end = _get_block_ts(w3, end) if end > cur else ts_start

            rows = []
            for log in logs:
                row = _decode_log(log)
                bn = row["_block_number"]
                frac = (bn - cur) / (end - cur) if end > cur else 0.0
                ts = int(ts_start + frac * (ts_end - ts_start))
                rows.append({
                    "timestamp": ts,
                    "maker": row["maker"],
                    "makerAssetId": row["makerAssetId"],
                    "makerAmountFilled": row["makerAmountFilled"],
                    "taker": row["taker"],
                    "takerAssetId": row["takerAssetId"],
                    "takerAmountFilled": row["takerAmountFilled"],
                })

            chunk_date = datetime.fromtimestamp(ts_start, tz=timezone.utc).strftime("%Y%m%d")
            fname = f"{chunk_date}_{cur:012d}.parquet"
            pl.DataFrame(
                rows,
                schema={
                    "timestamp": pl.Int64,
                    "maker": pl.Utf8,
                    "makerAssetId": pl.Utf8,
                    "makerAmountFilled": pl.Int64,
                    "taker": pl.Utf8,
                    "takerAssetId": pl.Utf8,
                    "takerAmountFilled": pl.Int64,
                },
            ).write_parquet(os.path.join(FILLS_DIR, fname), compression="zstd")
            total_new += len(rows)

            readable = datetime.fromtimestamp(ts_start, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            print(
                f"  Blocks {cur:>12,} → {end:<12,} ({readable})  "
                f"events: {len(logs):>5}  total: {total_new:,}  → {fname}"
            )
        else:
            print(f"  Blocks {cur:>12,} → {end:<12,}  events:     0  total: {total_new:,}")

        cur = end + 1
        _save_cursor(cur)
        time.sleep(2.0)

    print(f"Done. Wrote {total_new:,} new rows to {FILLS_DIR}/.")


if __name__ == "__main__":
    update_chain()
