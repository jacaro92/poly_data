"""Ranking de wallets rentables a partir de processed/trades.csv.

Reconstruye el ledger de cada wallet (compras acumulan posición a coste medio,
ventas realizan PnL contra ese coste medio) y produce processed/top_wallets.csv
ordenado por PnL realizado. La estrategia copy-trading lee ese CSV para decidir
a quién seguir.

Uso:
    python -m trading.wallet_analyzer
    python -m trading.wallet_analyzer --min-trades 20 --min-volume 500

Limitaciones (aceptables para ranking):
  - Ventas sin compra previa registrada (posición anterior al backfill) se
    ignoran en la parte no cubierta — no inventamos coste.
  - No calcula PnL no realizado ni resoluciones de mercado (un wallet que
    compra y deja resolver no suma realized; el ROI lo penaliza, no lo premia).
"""

import argparse
import os
import sys

import polars as pl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TRADES_CSV = os.path.join("processed", "trades.csv")
OUTPUT_CSV = os.path.join("processed", "top_wallets.csv")


def _wallet_actions(trades: pl.DataFrame) -> pl.DataFrame:
    """Aplana cada fill en dos acciones (maker y taker) con dirección propia."""
    base_cols = ["timestamp", "market_id", "nonusdc_side", "price", "usd_amount", "token_amount"]
    maker = trades.select(
        base_cols
        + [
            pl.col("maker").alias("wallet"),
            pl.col("maker_direction").alias("direction"),
        ]
    )
    taker = trades.select(
        base_cols
        + [
            pl.col("taker").alias("wallet"),
            pl.col("taker_direction").alias("direction"),
        ]
    )
    return pl.concat([maker, taker]).sort("timestamp")


def analyze(min_trades: int = 10, min_volume: float = 100.0) -> pl.DataFrame:
    if not os.path.isfile(TRADES_CSV):
        raise SystemExit(f"No existe {TRADES_CSV} — espera a que el pipeline lo genere.")

    trades = pl.read_csv(
        TRADES_CSV,
        schema_overrides={"price": pl.Float64, "usd_amount": pl.Float64, "token_amount": pl.Float64},
    ).drop_nulls(subset=["market_id", "nonusdc_side"])

    if trades.is_empty():
        raise SystemExit(f"{TRADES_CSV} está vacío — aún no hay fills procesados.")

    actions = _wallet_actions(trades)
    print(f"Fills: {len(trades):,}  →  acciones de wallet: {len(actions):,}")

    # Ledger por (wallet, mercado+lado) a coste medio.
    ledger: dict = {}   # key -> [shares, cost_usd]
    stats: dict = {}    # wallet -> métricas acumuladas

    for ts, market_id, side, price, usd, tokens, wallet, direction in actions.iter_rows():
        if not usd or not tokens or usd <= 0 or tokens <= 0:
            continue
        key = (wallet, market_id, side)
        st = stats.setdefault(
            wallet,
            {"realized_pnl": 0.0, "volume_buy": 0.0, "n_trades": 0, "wins": 0, "losses": 0,
             "first_ts": ts, "last_ts": ts},
        )
        st["n_trades"] += 1
        st["last_ts"] = ts

        if direction == "BUY":
            shares, cost = ledger.get(key, (0.0, 0.0))
            ledger[key] = (shares + tokens, cost + usd)
            st["volume_buy"] += usd
        else:  # SELL
            shares, cost = ledger.get(key, (0.0, 0.0))
            matched = min(tokens, shares)
            if matched <= 0:
                continue  # venta de posición anterior al backfill
            avg = cost / shares
            pnl = usd * (matched / tokens) - avg * matched
            st["realized_pnl"] += pnl
            if pnl >= 0:
                st["wins"] += 1
            else:
                st["losses"] += 1
            ledger[key] = (shares - matched, cost - avg * matched)

    rows = []
    for wallet, st in stats.items():
        closed = st["wins"] + st["losses"]
        if st["n_trades"] < min_trades or st["volume_buy"] < min_volume or closed == 0:
            continue
        rows.append(
            {
                "wallet": wallet,
                "realized_pnl": round(st["realized_pnl"], 2),
                "roi": round(st["realized_pnl"] / st["volume_buy"], 4) if st["volume_buy"] else 0.0,
                "win_rate": round(st["wins"] / closed, 4),
                "closed_lots": closed,
                "n_trades": st["n_trades"],
                "volume_buy": round(st["volume_buy"], 2),
                "first_trade": str(st["first_ts"]),
                "last_trade": str(st["last_ts"]),
            }
        )

    if not rows:
        raise SystemExit(
            "Ningún wallet pasa los filtros — baja --min-trades/--min-volume "
            "o espera a que el backfill acumule más historia."
        )

    ranking = pl.DataFrame(rows).sort("realized_pnl", descending=True)
    os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)
    ranking.write_csv(OUTPUT_CSV)
    print(f"\n{len(ranking)} wallets → {OUTPUT_CSV}\n")
    print(ranking.head(20))
    return ranking


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ranking de wallets por PnL realizado")
    parser.add_argument("--min-trades", type=int, default=10)
    parser.add_argument("--min-volume", type=float, default=100.0)
    args = parser.parse_args()
    analyze(min_trades=args.min_trades, min_volume=args.min_volume)
