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

from trading import config

TRADES_DIR = os.path.join("processed", "trades")
OUTPUT_CSV = os.path.join("processed", "top_wallets.csv")
MARKETS_CSV = os.path.join("data", "markets.csv")


def _excluded_market_ids() -> set[str]:
    """IDs de mercados degenerados (EXCLUDE_MARKET_PATTERNS, p.ej. cripto
    'Up or Down') para NO puntuar a las wallets por su rendimiento ahí.

    Sin esto el ranking lo dominan los scalpers de velas cripto y de in-play:
    cierran muchos lotes rápido con win_rate altísimo → score top, pero operan
    justo los mercados que la estrategia excluye o que no son copiables con
    retraso → seguimos wallets cuyos trades nunca copiamos (cartera muerta).

    Scan perezoso de markets.csv (solo id+question+slug, streaming) → bajo RAM.
    Fail-open: si markets.csv no existe o falla, devuelve set vacío (no romper
    el ranking por un fallo de lectura)."""
    pats = config.EXCLUDE_MARKET_PATTERNS
    if not pats or not os.path.isfile(MARKETS_CSV):
        return set()
    try:
        lz = pl.scan_csv(MARKETS_CSV, infer_schema_length=0).select(["id", "question", "slug"])
        expr = None
        for p in pats:
            e = (pl.col("question").str.to_lowercase().str.contains(p, literal=True)
                 | pl.col("slug").str.to_lowercase().str.contains(p, literal=True))
            expr = e if expr is None else (expr | e)
        ids = lz.filter(expr).select("id").collect(streaming=True)["id"].to_list()
        return set(ids)
    except Exception as e:
        print(f"  ⚠ no se pudieron excluir mercados degenerados del ranking: {e}")
        return set()


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
    analyze_days = int(os.environ.get("WALLET_ANALYZE_DAYS", "2"))

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

    # Excluir del scoring los mercados degenerados (updown, etc.): el ranking
    # debe reflejar SOLO el rendimiento en mercados que de verdad copiamos.
    excl = _excluded_market_ids()
    if excl:
        lazy = lazy.filter(~pl.col("market_id").is_in(list(excl)))
        print(f"  Excluyendo {len(excl):,} mercados degenerados del ranking (EXCLUDE_MARKET_PATTERNS)")

    print("  Agregando acciones por (wallet, mercado, dirección)…")
    agg = _aggregate_actions(lazy)
    print(f"  {len(agg):,} grupos únicos")

    wallet_stats = _compute_pnl(agg)

    # Filtro de calidad: excluir market-makers/HFT (nº de acciones disparatado)
    # y arbitrajistas de resolución (win_rate ~100% con pocos lotes). No son
    # copiables con retraso. Configurable por entorno.
    min_lots = int(os.environ.get("MIN_WALLET_LOTS", "10"))
    max_trades = int(os.environ.get("MAX_WALLET_TRADES", "20000"))
    max_win_rate = float(os.environ.get("MAX_WALLET_WIN_RATE", "0.99"))
    # Tamaño medio de apuesta (volume_buy/closed_lots): excluye micro-scalpers
    # (p.ej. sharps de tenis in-play que apuestan $0.2-1.5/lote con 97% win y
    # 200% ROI): su edge no se hereda copiando con $2.5/trade y suele ser in-play
    # (al copiar con retraso el punto ya está decidido). 0 = sin filtro.
    min_avg_lot = float(os.environ.get("MIN_AVG_LOT_USD", "0"))

    # Ranking por ÉXITO, no por P&L bruto. El P&L realizado absoluto favorece a
    # wallets de mucho volumen (pueden ganar $1000 con ROI 1% y win_rate 50%);
    # copiarlos con $2.5/trade no hereda su rentabilidad. El score premia:
    #   - roi      : rentabilidad por dólar arriesgado (eficiencia direccional).
    #   - win_rate : consistencia de acertar (no un par de pelotazos).
    #   - log1p(closed_lots): confianza por tamaño de muestra (rendimientos
    #     decrecientes; no premiar volumen sin límite como el P&L bruto).
    # roi<0 ó win_rate bajo → score negativo/bajo → fondo del ranking.
    ranking = (
        wallet_stats
        .with_columns([
            (pl.col("realized_pnl") / pl.col("volume_buy")).alias("roi"),
            (pl.col("wins") / pl.col("closed_lots")).alias("win_rate"),
            (pl.col("volume_buy") / pl.col("closed_lots")).alias("avg_lot"),
        ])
        .filter(
            (pl.col("n_trades") >= min_trades)
            & (pl.col("volume_buy") >= min_volume)
            & (pl.col("closed_lots") >= min_lots)       # track record real
            & (pl.col("n_trades") <= max_trades)        # excluye MM/HFT
            & (pl.col("win_rate") <= max_win_rate)      # excluye arb perfecto
            & (pl.col("avg_lot") >= min_avg_lot)        # excluye micro-scalpers
        )
        .with_columns([
            (pl.col("roi") * pl.col("win_rate")
             * (pl.col("closed_lots") + 1).log()).alias("score"),
        ])
        .with_columns([
            pl.col("realized_pnl").round(2),
            pl.col("volume_buy").round(2),
            pl.col("roi").round(4),
            pl.col("win_rate").round(4),
            pl.col("avg_lot").round(2),
            pl.col("score").round(4),
        ])
        .sort("score", descending=True)
        .select([
            "wallet", "score", "roi", "win_rate", "avg_lot",
            "realized_pnl", "closed_lots", "n_trades", "volume_buy",
        ])
    )
    print(
        f"  Filtro wallets: lots>={min_lots}, trades<={max_trades}, "
        f"win_rate<={max_win_rate}, avg_lot>=${min_avg_lot:.0f} → {len(ranking)} "
        f"copiables (ordenados por score = roi×win_rate×log1p(lots))"
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
