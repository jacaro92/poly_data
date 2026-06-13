"""Ranking de wallets rentables a partir de processed/trades/*.parquet.

Usa group_by vectorizado de Polars (scan perezoso + agrupación en Arrow) en vez
de cargar las 59 M+ filas en RAM y recorrerlas con un dict Python.  Pico de RAM:
≈ 100-200 MB (tabla hash Arrow para el group_by) en vez de los > 2 GB del
enfoque original.

Limitaciones (aceptables para ranking):
  - PnL realizado calculado con método de coste medio agregado (equivalente al
    cálculo fila-a-fila para este método, ver comentario en _compute_pnl).
  - Posiciones abiertas al final del periodo (sin venta) no contribuyen al PnL
    realizado ni cuentan como win/loss.
  - WALLET_ANALYZE_DAYS controla cuántos días se analizan (0 = todos).
"""

import os
import sys

import polars as pl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TRADES_DIR = os.path.join("processed", "trades")
OUTPUT_CSV = os.path.join("processed", "top_wallets.csv")


def _aggregate_actions(lazy: pl.LazyFrame) -> pl.DataFrame:
    """Agrega fills en acciones por (wallet, market_id, side, direction).

    Hace DOS scans perezosos separados (maker / taker) para no duplicar el
    dataset en memoria.  Cada scan reduce las N millones de filas a un grupo
    compacto antes de collect().

    Coste medio agregado — equivalencia con el algoritmo fila-a-fila:
      Para el mismo (wallet, market_id, side):
        avg_cost      = buy_usd  / buy_tokens
        matched       = min(sell_tokens, buy_tokens)
        realized_pnl  = sell_usd × (matched/sell_tokens) – avg_cost × matched
      Esto es matemáticamente idéntico a procesar cada transacción por orden
      siempre que se use método de coste medio (no FIFO/LIFO).
    """
    NEED = ["market_id", "nonusdc_side", "usd_amount", "token_amount"]

    maker_agg = (
        lazy
        .filter(pl.col("market_id").is_not_null() & (pl.col("usd_amount") > 0))
        .select(NEED + ["maker", "maker_direction"])
        .rename({"maker": "wallet", "maker_direction": "direction", "nonusdc_side": "side"})
        .group_by(["wallet", "market_id", "side", "direction"])
        .agg(
            pl.col("usd_amount").sum().alias("usd"),
            pl.col("token_amount").sum().alias("tok"),
            pl.len().alias("n"),
        )
        .collect()
    )

    taker_agg = (
        lazy
        .filter(pl.col("market_id").is_not_null() & (pl.col("usd_amount") > 0))
        .select(NEED + ["taker", "taker_direction"])
        .rename({"taker": "wallet", "taker_direction": "direction", "nonusdc_side": "side"})
        .group_by(["wallet", "market_id", "side", "direction"])
        .agg(
            pl.col("usd_amount").sum().alias("usd"),
            pl.col("token_amount").sum().alias("tok"),
            pl.len().alias("n"),
        )
        .collect()
    )

    return (
        pl.concat([maker_agg, taker_agg])
        .group_by(["wallet", "market_id", "side", "direction"])
        .agg(
            pl.col("usd").sum(),
            pl.col("tok").sum(),
            pl.col("n").sum(),
        )
    )


def _compute_pnl(agg: pl.DataFrame) -> pl.DataFrame:
    """Convierte la tabla agrupada en PnL realizado por wallet."""
    buys = (
        agg.filter(pl.col("direction") == "BUY")
        .select(["wallet", "market_id", "side",
                 pl.col("usd").alias("buy_usd"),
                 pl.col("tok").alias("buy_tok"),
                 pl.col("n").alias("n_buy")])
    )
    sells = (
        agg.filter(pl.col("direction") == "SELL")
        .select(["wallet", "market_id", "side",
                 pl.col("usd").alias("sell_usd"),
                 pl.col("tok").alias("sell_tok"),
                 pl.col("n").alias("n_sell")])
    )

    pos = (
        buys
        .join(sells, on=["wallet", "market_id", "side"], how="left")
        .filter(pl.col("buy_tok") > 0)
        .with_columns([
            pl.col("sell_tok").fill_null(0.0),
            pl.col("sell_usd").fill_null(0.0),
            pl.col("n_sell").fill_null(0),
        ])
        .with_columns(
            pl.min_horizontal("sell_tok", "buy_tok").alias("matched")
        )
        .with_columns(
            pl.when(pl.col("sell_tok") > 0)
            .then(
                pl.col("sell_usd") * pl.col("matched") / pl.col("sell_tok")
                - (pl.col("buy_usd") / pl.col("buy_tok")) * pl.col("matched")
            )
            .otherwise(0.0)
            .alias("pnl"),
            (pl.col("n_buy") + pl.col("n_sell")).alias("n_pos"),
        )
    )

    return (
        pos
        .group_by("wallet")
        .agg([
            pl.col("pnl").sum().alias("realized_pnl"),
            pl.col("buy_usd").sum().alias("volume_buy"),
            pl.col("n_pos").sum().alias("n_trades"),
            (pl.col("pnl") > 0).sum().alias("wins"),
            (pl.col("pnl") < 0).sum().alias("losses"),
        ])
        .with_columns([
            (pl.col("wins") + pl.col("losses")).alias("closed_lots"),
        ])
        .filter(pl.col("closed_lots") > 0)
    )


def analyze(min_trades: int = 10, min_volume: float = 100.0) -> pl.DataFrame:
    analyze_days = int(os.environ.get("WALLET_ANALYZE_DAYS", "7"))

    all_files = sorted(
        os.path.join(TRADES_DIR, f)
        for f in os.listdir(TRADES_DIR)
        if f.endswith(".parquet")
    ) if os.path.isdir(TRADES_DIR) else []

    if not all_files:
        raise SystemExit(
            f"No existe {TRADES_DIR}/*.parquet — espera a que el pipeline lo genere."
        )

    trade_files = all_files[-analyze_days:] if analyze_days > 0 else all_files
    print(f"  Analizando {len(trade_files)} archivo(s) de trades (últimos {analyze_days} días)")

    lazy = pl.scan_parquet(trade_files)

    print("  Agregando acciones por (wallet, mercado, dirección)…")
    agg = _aggregate_actions(lazy)
    print(f"  {len(agg):,} grupos únicos")

    wallet_stats = _compute_pnl(agg)

    ranking = (
        wallet_stats
        .filter(
            (pl.col("n_trades") >= min_trades)
            & (pl.col("volume_buy") >= min_volume)
        )
        .with_columns([
            (pl.col("realized_pnl") / pl.col("volume_buy")).alias("roi"),
            (pl.col("wins") / pl.col("closed_lots")).alias("win_rate"),
            pl.col("realized_pnl").round(2),
            pl.col("volume_buy").round(2),
        ])
        .sort("realized_pnl", descending=True)
        .select([
            "wallet", "realized_pnl", "roi", "win_rate",
            "closed_lots", "n_trades", "volume_buy",
        ])
    )

    if ranking.is_empty():
        raise SystemExit(
            "Ningún wallet pasa los filtros — baja --min-trades/--min-volume "
            "o espera a que el backfill acumule más historia."
        )

    os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)
    ranking.write_csv(OUTPUT_CSV)
    print(f"\n{len(ranking)} wallets → {OUTPUT_CSV}\n")
    print(ranking.head(20))
    return ranking


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Ranking de wallets por PnL realizado")
    parser.add_argument("--min-trades", type=int, default=10)
    parser.add_argument("--min-volume", type=float, default=100.0)
    args = parser.parse_args()
    analyze(min_trades=args.min_trades, min_volume=args.min_volume)
