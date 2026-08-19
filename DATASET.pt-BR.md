# DATASET — livro de ofertas do Polymarket, 18,4 dias

**Português** · [English](DATASET.md)


Série contínua de topo de livro coletada por WebSocket entre **19/07 e
06/08/2026**, de uma droplet em Londres (~15ms do exchange).

Dado granular de prediction market é escasso: as APIs públicas dão preço
agregado, não a evolução do livro tick a tick. Este dataset tem **cada mudança
do melhor bid/ask** de 77.985 tokens.

| | |
|---|---|
| Janela | 441,9 horas (18,4 dias) contínuas |
| Tokens | 77.985 |
| Mudanças de topo | 15.997.072 |
| Formato | Parquet, compressão ZSTD |
| Tamanho | 421 MB (de 3,88 GB em DuckDB) |
| Licença | código: MIT. O dado vem de endpoints públicos do Polymarket — redistribuído como observado, sem reivindicação de propriedade sobre ele |

## Baixar

[Release `v1.0-dataset`](https://github.com/AdrielCunha/market-microstructure-lab/releases/tag/v1.0-dataset)

```bash
curl -LO https://github.com/AdrielCunha/market-microstructure-lab/releases/download/v1.0-dataset/book_top.parquet        # 123 MB — o livro de ofertas
curl -LO https://github.com/AdrielCunha/market-microstructure-lab/releases/download/v1.0-dataset/wallet_trades.parquet   #  41 MB — trades das carteiras vigiadas
curl -LO https://github.com/AdrielCunha/market-microstructure-lab/releases/download/v1.0-dataset/markets.parquet         # 1,2 MB — catalogo, taxas, resolucao
```

## Como usar

```python
import duckdb
con = duckdb.connect()

# vida do topo de livro: quanto tempo o melhor preço sobrevive
con.execute("""
    SELECT median(vida) AS mediana_ms,
           avg(vida > 15)::DOUBLE AS sobrevive_15ms
    FROM (SELECT lead(ts_local) OVER (PARTITION BY token_id ORDER BY ts_local)
                 - ts_local AS vida
          FROM 'book_top.parquet' WHERE source = 'ws')
    WHERE vida BETWEEN 1 AND 600000
""").pl()
```

Também funciona com `pandas.read_parquet`, `polars.read_parquet`, Spark, etc.

## Detalhes que importam

- **`ts_local` é o relógio de quem coletou**, `ts_exchange` é o do exchange.
  A diferença entre os dois é o atraso de rede, e foi ela que decidiu o projeto.
- **`source`** distingue `ws` (stream), `rest_audit` (snapshot de conferência) e
  `copy_probe` (sondagem sob demanda). Filtre `source = 'ws'` para a série
  principal.
- **O topo repetido é descartado**: ~96% dos eventos de `price_change` mexem em
  níveis fundos e deixam o topo igual. Uma linha em `book_top` é uma mudança de
  verdade.
- **`wallet_trades.ts_seen - ts_trade`** é o atraso até um trade aparecer na API
  pública. Mediana de 335s — o número que matou a tese de copiar carteira.
- **Há lacunas** entre sessões do coletor (reinícios e um período de instabilidade
  por falta de memória). `collector_log` registra cada arranque.
- **`paper_fills` são execuções SIMULADAS.** Nenhuma ordem real foi enviada.

## Esquema

### `book_top.parquet` — 15,997,072 linhas

| coluna | tipo |
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

### `book_events.parquet` — 1,811,141 linhas

| coluna | tipo |
|---|---|
| `seq` | BIGINT |
| `token_id` | VARCHAR |
| `event_type` | VARCHAR |
| `ts_exchange` | BIGINT |
| `ts_local` | BIGINT |
| `payload` | JSON |

### `wallet_trades.parquet` — 453,466 linhas

| coluna | tipo |
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

### `markets.parquet` — 14,778 linhas

| coluna | tipo |
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

### `paper_fills.parquet` — 29,826 linhas

| coluna | tipo |
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

### `collector_log.parquet` — 34,118 linhas

| coluna | tipo |
|---|---|
| `ts_local` | BIGINT |
| `component` | VARCHAR |
| `level` | VARCHAR |
| `message` | VARCHAR |
| `detail` | JSON |
