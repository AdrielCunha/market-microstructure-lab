# DATASET — Polymarket order book, 18.4 days

**English** · [Português](DATASET.pt-BR.md)

Continuous top-of-book series collected over WebSocket between **2026-07-19 and
2026-08-06**, from a droplet in London (~15ms from the exchange).

Granular prediction-market data is scarce: public APIs expose aggregated prices,
not how the book moves. This dataset holds **every change to the best bid/ask**
across 77,985 tokens.

| | |
|---|---|
| Window | 441.9 hours (18.4 days), continuous |
| Tokens | 77,985 |
| Top-of-book updates | 15,997,072 |
| Format | Parquet, ZSTD compression |
| Size | 421 MB (from 3.88 GB in DuckDB) |
| Licence | code: MIT. Data derived from Polymarket's public endpoints — redistributed as observed, with no claim of ownership over it |

## Download

[Release `v1.0-dataset`](https://github.com/AdrielCunha/market-microstructure-lab/releases/tag/v1.0-dataset)

```bash
B=https://github.com/AdrielCunha/market-microstructure-lab/releases/download/v1.0-dataset
curl -LO $B/book_top.parquet        # 123 MB — the order book
curl -LO $B/wallet_trades.parquet   #  41 MB — trades of the watched wallets
curl -LO $B/markets.parquet         # 1.2 MB — catalogue, fees, resolution
```

## Usage

```python
import duckdb
con = duckdb.connect()

# top-of-book lifetime: how long the best price survives before someone reprices
con.execute("""
    SELECT median(life) AS median_ms,
           avg(life > 15)::DOUBLE AS survives_15ms
    FROM (SELECT lead(ts_local) OVER (PARTITION BY token_id ORDER BY ts_local)
                 - ts_local AS life
          FROM 'book_top.parquet' WHERE source = 'ws')
    WHERE life BETWEEN 1 AND 600000
""").pl()
```

Also readable with `pandas.read_parquet`, `polars.read_parquet`, Spark, etc.

## Things that matter when interpreting it

- **`ts_local` is the collector's clock**, `ts_exchange` is the exchange's. The
  gap between them is network delay — and it is what decided this project.
- **`source`** separates `ws` (stream), `rest_audit` (reconciliation snapshot)
  and `copy_probe` (on-demand probe). Filter `source = 'ws'` for the main series.
- **Unchanged tops are dropped**: ~96% of `price_change` events touch deeper
  levels and leave the top alone. A row in `book_top` is a real change.
- **`wallet_trades.ts_seen - ts_trade`** is the delay before a trade appears on
  the public API. Median 335s — the number that killed the copy-trading thesis.
- **There are gaps** between collector sessions (restarts, plus a period of
  instability caused by memory exhaustion). `collector_log` records every start.
- **`paper_fills` are SIMULATED fills.** No real order was ever sent.

## Schema


### `book_top.parquet` — 15,997,072 rows



| column | type |

|---|---|

| `token_id` | VARCHAR |

| `ts_exchange` | BIGINT |

| `ts_local` | BIGINT |

| `best_bid` | DOUBLE |

| `best_bid_size` | DOUBLE |

| `best_ask` | DOUBLE |

| `best_ask_size` | DOUBLE |

| `mid` | DOUBLE |

| `spread` | DOUBLE |

| `source` | VARCHAR |



### `book_events.parquet` — 1,811,141 rows



| column | type |

|---|---|

| `seq` | BIGINT |

| `token_id` | VARCHAR |

| `event_type` | VARCHAR |

| `ts_exchange` | BIGINT |

| `ts_local` | BIGINT |

| `payload` | JSON |



### `wallet_trades.parquet` — 453,466 rows



| column | type |

|---|---|

| `trade_uid` | VARCHAR |

| `wallet` | VARCHAR |

| `name` | VARCHAR |

| `token_id` | VARCHAR |

| `condition_id` | VARCHAR |

| `side` | VARCHAR |

| `size` | DOUBLE |

| `price` | DOUBLE |

| `notional_usd` | DOUBLE |

| `ts_trade` | BIGINT |

| `ts_seen` | BIGINT |

| `is_backfill` | BOOLEAN |

| `title` | VARCHAR |

| `outcome` | VARCHAR |

| `slug` | VARCHAR |

| `tx_hash` | VARCHAR |



### `markets.parquet` — 14,778 rows



| column | type |

|---|---|

| `token_id` | VARCHAR |

| `condition_id` | VARCHAR |

| `event_id` | VARCHAR |

| `event_slug` | VARCHAR |

| `event_title` | VARCHAR |

| `question` | VARCHAR |

| `outcome` | VARCHAR |

| `outcome_index` | INTEGER |

| `neg_risk` | BOOLEAN |

| `neg_risk_market` | VARCHAR |

| `event_n_outcomes` | INTEGER |

| `category` | VARCHAR |

| `end_date` | TIMESTAMP |

| `liquidity_usd` | DOUBLE |

| `volume_usd` | DOUBLE |

| `min_tick_size` | DOUBLE |

| `order_min_size` | DOUBLE |

| `fee_type` | VARCHAR |

| `fee_rate` | DOUBLE |

| `fee_exponent` | DOUBLE |

| `fee_rebate_rate` | DOUBLE |

| `fee_taker_only` | BOOLEAN |

| `fees_enabled` | BOOLEAN |

| `active` | BOOLEAN |

| `closed` | BOOLEAN |

| `accepting_orders` | BOOLEAN |

| `selected` | BOOLEAN |

| `seen_at` | TIMESTAMP |

| `updated_at` | TIMESTAMP |



### `paper_fills.parquet` — 29,826 rows



| column | type |

|---|---|

| `ts_local` | BIGINT |

| `strategy` | VARCHAR |

| `regra` | VARCHAR |

| `token_id` | VARCHAR |

| `side` | VARCHAR |

| `price` | DOUBLE |

| `size` | DOUBLE |

| `notional_usd` | DOUBLE |

| `mid_at_fill` | DOUBLE |

| `spread_at_fill` | DOUBLE |

| `rebate` | DOUBLE |

| `posicao_depois` | DOUBLE |

| `taxa` | DOUBLE |

| `agressiva` | BOOLEAN |



### `collector_log.parquet` — 34,118 rows



| column | type |

|---|---|

| `ts_local` | BIGINT |

| `component` | VARCHAR |

| `level` | VARCHAR |

| `message` | VARCHAR |

| `detail` | JSON |



