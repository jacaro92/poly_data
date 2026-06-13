"""
Join raw order-fill events with market metadata to produce labeled trades.

Input:  data/fills/YYYYMMDD_BLOCKSTART.parquet  (written by update_chain.py)
Output: processed/trades/YYYYMMDD.parquet       (one file per UTC day)

Incremental: tracks the last processed fill file in processed/fill_cursor.json.
Only new fill files (newer than the cursor) are processed each run.

Rolling retention: trade files older than FILLS_RETAIN_DAYS are deleted to
match the fill-file retention window.
"""

import json
import os
import warnings
from datetime import datetime, timedelta, timezone

warnings.filterwarnings("ignore")

import polars as pl

from poly_utils.utils import (
    MARKETS_LEAN_PARQUET,
    build_lean_markets_parquet,
    update_missing_tokens,
)

FILLS_DIR = "data/fills"
TRADES_DIR = "processed/trades"
FILL_CURSOR_FILE = "processed/fill_cursor.json"
FILLS_RETAIN_DAYS = int(os.environ.get("FILLS_RETAIN_DAYS", "14"))


# ── helpers ──────────────────────────────────────────────────────────────────

def _load_fill_cursor() -> str | None:
    if os.path.isfile(FILL_CURSOR_FILE):
        try:
            with open(FILL_CURSOR_FILE) as f:
                return json.load(f).get("last")
        except Exception:
            pass
    return None


def _save_fill_cursor(fname: str) -> None:
    os.makedirs(os.path.dirname(FILL_CURSOR_FILE), exist_ok=True)
    with open(FILL_CURSOR_FILE, "w") as f:
        json.dump({"last": fname}, f)


def _list_fill_files() -> list[str]:
    """Sorted list of absolute paths to fill parquet files."""
    if not os.path.isdir(FILLS_DIR):
        return []
    return sorted(
        os.path.join(FILLS_DIR, f)
        for f in os.listdir(FILLS_DIR)
        if f.endswith(".parquet")
    )


def _cleanup_old_trades() -> None:
    if not os.path.isdir(TRADES_DIR):
        return
    cutoff = (
        datetime.now(tz=timezone.utc) - timedelta(days=FILLS_RETAIN_DAYS)
    ).strftime("%Y%m%d")
    removed = 0
    for fname in os.listdir(TRADES_DIR):
        if fname.endswith(".parquet") and fname.replace(".parquet", "") < cutoff:
            try:
                os.remove(os.path.join(TRADES_DIR, fname))
                removed += 1
            except OSError:
                pass
    if removed:
        print(f"  [cleanup] Removed {removed} old trade file(s)")


def _collect_unique_tokens(fill_files: list[str]) -> set[str]:
    """Collect unique non-USDC token IDs by reading fill files one at a time.

    Peak RAM: O(unique tokens) ≈ 10 MB, regardless of how many fill files exist.
    Never materialises the full fills dataset.
    """
    tokens: set[str] = set()
    for fpath in fill_files:
        try:
            df = pl.read_parquet(fpath, columns=["makerAssetId", "takerAssetId"])
            tokens.update(v for v in df["makerAssetId"].drop_nulls().to_list() if v != "0")
            tokens.update(v for v in df["takerAssetId"].drop_nulls().to_list() if v != "0")
        except Exception:
            pass
    return tokens


def _get_markets_for_tokens(token_ids: set[str]) -> pl.DataFrame:
    """Load only markets whose tokens appear in token_ids.

    Scans markets_lean.parquet one row-group at a time and filters immediately,
    so peak RAM is ~one row-group (~88 MB) instead of the full ~250 MB for all
    1.38 M markets.  Returns a small DataFrame (only matched markets).
    """
    empty = pl.DataFrame(schema={"id": pl.Utf8, "token1": pl.Utf8, "token2": pl.Utf8})
    if not token_ids:
        return empty
    if not os.path.exists(MARKETS_LEAN_PARQUET):
        raise FileNotFoundError(f"{MARKETS_LEAN_PARQUET} not found — run update_markets() first")
    token_list = list(token_ids)
    return (
        pl.scan_parquet(MARKETS_LEAN_PARQUET)
        .filter(
            pl.col("token1").is_in(token_list) | pl.col("token2").is_in(token_list)
        )
        .collect()
    )


# ── core join ─────────────────────────────────────────────────────────────────

def _processed_df(fills: pl.DataFrame, markets_df: pl.DataFrame) -> pl.DataFrame:
    """Join fills with market metadata and compute derived trade columns."""
    markets_df = markets_df.rename({"id": "market_id"})
    markets_long = markets_df.select(["market_id", "token1", "token2"]).melt(
        id_vars="market_id",
        value_vars=["token1", "token2"],
        variable_name="side",
        value_name="asset_id",
    )

    # Convert unix timestamp to datetime
    fills = fills.with_columns(
        pl.from_epoch(pl.col("timestamp"), time_unit="s").alias("timestamp")
    )

    # Scale amounts from raw (6 decimals) to USD/token units
    fills = fills.with_columns(
        [
            (pl.col("makerAmountFilled") / 10**6).alias("makerAmountFilled"),
            (pl.col("takerAmountFilled") / 10**6).alias("takerAmountFilled"),
        ]
    )

    fills = fills.with_columns(
        pl.when(pl.col("makerAssetId") != "0")
        .then(pl.col("makerAssetId"))
        .otherwise(pl.col("takerAssetId"))
        .alias("nonusdc_asset_id")
    )

    fills = fills.join(markets_long, left_on="nonusdc_asset_id", right_on="asset_id", how="left")

    fills = fills.with_columns(
        [
            pl.when(pl.col("makerAssetId") == "0")
            .then(pl.lit("USDC"))
            .otherwise(pl.col("side"))
            .alias("makerAsset"),
            pl.when(pl.col("takerAssetId") == "0")
            .then(pl.lit("USDC"))
            .otherwise(pl.col("side"))
            .alias("takerAsset"),
            pl.col("market_id"),
        ]
    )

    fills = fills.with_columns(
        [
            pl.when(pl.col("takerAsset") == "USDC")
            .then(pl.lit("BUY"))
            .otherwise(pl.lit("SELL"))
            .alias("taker_direction"),
            pl.when(pl.col("takerAsset") == "USDC")
            .then(pl.lit("SELL"))
            .otherwise(pl.lit("BUY"))
            .alias("maker_direction"),
        ]
    )

    fills = fills.with_columns(
        [
            pl.when(pl.col("makerAssetId") == "0")
            .then(pl.col("takerAsset"))
            .otherwise(pl.col("makerAsset"))
            .alias("nonusdc_side"),
            pl.when(pl.col("takerAsset") == "USDC")
            .then(pl.col("takerAmountFilled"))
            .otherwise(pl.col("makerAmountFilled"))
            .alias("usd_amount"),
            pl.when(pl.col("takerAsset") != "USDC")
            .then(pl.col("takerAmountFilled"))
            .otherwise(pl.col("makerAmountFilled"))
            .alias("token_amount"),
            pl.when(pl.col("takerAsset") == "USDC")
            .then(pl.col("takerAmountFilled") / pl.col("makerAmountFilled"))
            .otherwise(pl.col("makerAmountFilled") / pl.col("takerAmountFilled"))
            .cast(pl.Float64)
            .alias("price"),
        ]
    )

    return fills.select(
        [
            "timestamp",
            "market_id",
            "maker",
            "taker",
            "nonusdc_side",
            "maker_direction",
            "taker_direction",
            "price",
            "usd_amount",
            "token_amount",
        ]
    )


# ── main ──────────────────────────────────────────────────────────────────────

def process_live() -> None:
    print("=" * 60)
    print("🔄 Processing trades")
    print("=" * 60)

    all_fill_files = _list_fill_files()
    if not all_fill_files:
        print(f"⚠ No fill parquet files in {FILLS_DIR} — run update_chain() first")
        return

    # Determine which fill files are new (after the cursor)
    cursor_fname = _load_fill_cursor()
    all_fnames = [os.path.basename(p) for p in all_fill_files]
    if cursor_fname and cursor_fname in all_fnames:
        idx = all_fnames.index(cursor_fname)
        new_files = all_fill_files[idx + 1:]
    else:
        new_files = all_fill_files

    if not new_files:
        print("✓ No new fill files to process.")
        return

    print(f"📍 Processing {len(new_files)} new fill file(s) (cursor: {cursor_fname or 'none'})")

    # Collect unique token IDs (one file at a time: ~10 MB peak regardless of fill count)
    print("  Collecting token IDs from fill files...")
    all_token_ids = _collect_unique_tokens(new_files)
    print(f"  {len(all_token_ids):,} unique token IDs found")

    # Load only matching markets (filtered scan: peak ~100 MB instead of ~250 MB)
    markets_df = _get_markets_for_tokens(all_token_ids)
    print(f"  {len(markets_df):,} relevant markets loaded")

    # Detect and fetch any missing markets (cheap: compares small in-memory sets)
    existing_tokens: set[str] = set()
    for _col in ("token1", "token2"):
        if _col in markets_df.columns:
            existing_tokens.update(markets_df[_col].drop_nulls().to_list())
    missing = sorted(all_token_ids - existing_tokens)
    if missing:
        print(f"🔍 {len(missing)} missing markets — fetching from Polymarket API")
        update_missing_tokens(missing)
        build_lean_markets_parquet()
        markets_df = _get_markets_for_tokens(all_token_ids)
    else:
        print("✅ All markets present")

    os.makedirs(TRADES_DIR, exist_ok=True)
    _cleanup_old_trades()

    # Group new fill files by UTC date (first 8 chars of filename = YYYYMMDD)
    by_date: dict[str, list[str]] = {}
    for path in new_files:
        date_str = os.path.basename(path)[:8]
        by_date.setdefault(date_str, []).append(path)

    total_new_trades = 0
    last_processed_fname: str | None = None

    for date_str in sorted(by_date):
        paths = by_date[date_str]
        fills_chunk = pl.concat([pl.read_parquet(p) for p in paths])
        trades_chunk = _processed_df(fills_chunk, markets_df)

        trades_file = os.path.join(TRADES_DIR, f"{date_str}.parquet")
        if os.path.isfile(trades_file):
            existing = pl.read_parquet(trades_file)
            trades_chunk = pl.concat([existing, trades_chunk])

        trades_chunk.write_parquet(trades_file, compression="zstd")
        total_new_trades += len(trades_chunk)
        last_processed_fname = os.path.basename(paths[-1])
        _save_fill_cursor(last_processed_fname)  # incremental — survives OOM kills
        print(
            f"  {date_str}: {len(fills_chunk):,} new fills "
            f"→ {trades_file} ({len(trades_chunk):,} trades total)"
        )

    print(f"✓ Done. Processed {len(new_files)} fill file(s), {total_new_trades:,} trade rows.")
    print("=" * 60)
    print("✅ Done")
    print("=" * 60)


if __name__ == "__main__":
    process_live()
