# pmlab — measuring whether market making on Polymarket actually pays

**English** · [Português](README.pt-BR.md)

Instrumentation built to answer, **with measurements rather than opinions**,
whether a systematic trading business exists on [Polymarket](https://polymarket.com).

The project is **finished**. The answer was *no* — and the value is in *how* that
was established, and in what it left behind.

> **No real orders were ever sent. There is no private key in this repository.**
> Every phase used public read-only endpoints only.

---

## The verdict

Three theses were tested. All three died, each with a number attached:

| thesis | outcome | why |
|---|---|---|
| **Negative-risk arbitrage** | dead | 1 episode in 30h, lasting **0.0s**. Best asks sum to $1.02 — a premium, not a discount |
| **Copying winning wallets** | dead | copying costs **6.7% of notional**; the copied trader's margin is **1.28%**. A trade only surfaces on the public API **335s** after it happens |
| **Market making** | dead | **−31.4%** over 5 days, already at 15ms latency with 93% of quotes surviving transit |

The third one is the interesting one. It did not fail for lack of speed — the
speed was bought and measured. Out of 68 position cycles, only **2** closed by
finding a counterparty in the book. The rest were either paying to escape or
holding until the market resolved.

**Total cost: a few dozen dollars of server time, zero trading capital.**

---

## The finding nobody had written down

Conventional wisdom says *"put the bot in `us-east-1`."* For this exchange,
**that is wrong.**

Polymarket sits behind Cloudflare and the origin is invisible. Locating it took
triangulation from five disposable droplets, measuring only dynamic endpoints
(`cf-cache-status: DYNAMIC`) so as not to measure an edge cache instead:

| measured from | PoP | round-trip | quotes surviving transit |
|---|---|---|---|
| São Paulo | GRU | 164ms | ~57% |
| DigitalOcean SFO3 | SJC | 148ms | ~59% |
| DigitalOcean NYC3 | EWR | 86ms | ~68% |
| DigitalOcean AMS3 | AMS | 20ms | ~92% |
| **DigitalOcean LON1** | **LHR** | **15ms** | **~93%** |

San Jose being 67ms worse than Newark — the width of the United States — proves
the origin lies east. And 78ms east of Newark is not Virginia (~5ms), it is the
Atlantic. Amsterdam closes the argument: **Polymarket's CLOB runs in Ireland.**

Cost of the discovery: roughly US$ 0.20 in hourly-billed droplets.

---

## The dataset

18.4 days of prediction-market order book, tick by tick. Granular data of this
kind is scarce — public APIs expose aggregated prices, not how the book evolves.

| table | rows | contents |
|---|---|---|
| `book_top` | **15,997,072** | top of book on every change: bid, ask, sizes, spread |
| `book_events` | 1,811,141 | raw WebSocket payload, for reprocessing from scratch |
| `wallet_trades` | 453,466 | public trades of the top 30 wallets, with observation delay measured |
| `markets` | 14,778 | catalogue: fees, resolution dates, negative-risk structure |
| `paper_fills` | 29,826 | simulated fills, with rebate and fee per row |

**77,985 tokens · 441.9 continuous hours · 421 MB in Parquet** (down from 3.88 GB
in DuckDB).

### Download

Files live in the [**`v1.0-dataset` release**](https://github.com/AdrielCunha/market-microstructure-lab/releases/tag/v1.0-dataset) —
kept separate on purpose, so nobody has to pull 421 MB just to get the book:

```bash
# the order book alone (123 MB) — what most people actually want
curl -LO https://github.com/AdrielCunha/market-microstructure-lab/releases/download/v1.0-dataset/book_top.parquet
```

```python
import duckdb
duckdb.sql("SELECT count(*), count(DISTINCT token_id) FROM 'book_top.parquet'")
# 15,997,072 rows, 77,985 tokens
```

Full schema and interpretation caveats in [DATASET.md](DATASET.md).

---

## What makes this an instrument rather than an optimistic spreadsheet

A simulator that turns a profit on any strategy is worthless. Most of the effort
went into stopping the system from lying in its own favour. **23 defects are
documented in [CONTEXT.md](CONTEXT.md)** with the reasoning behind each — and
nearly all belong to one family: *a result too good, hiding the dominant cost.*

A few:

- **A passing verdict, refused.** Market making was about to clear Gate 0 with
  200x headroom over cost — headroom that ignored adverse selection. A third
  state, `INCONCLUSIVE`, was introduced; by construction it can never return
  `PASS`.
- **Latency measured against a cached endpoint** reported 50ms; the real path was
  164ms. The script now **refuses to report** if Cloudflare says `HIT`.
- **The integrity check itself lied**, reporting *3,198 two-minute gaps inside a
  225-minute window* — arithmetically impossible. A false alarm is worse than no
  check: it trains the reader to ignore red.
- **Selling short was free** in the capital guard: the simulation ended up with
  6,995 shares sold against 917 bought — a book that could never have been
  opened with the stated capital.
- **Paper profit became buying power**, a feedback loop that produced
  "$35,309 of profit on $1,000" in three hours.

**142 tests** lock these classes of error so they cannot return.

---

## Architecture

```
collector/   CLOB WebSocket (400 tokens live), catalogue, wallet polling
engine/      market-making simulator: latency, queue, inventory, settlement
analysis/    spreads, negative-risk, copyability, niches, markout, latency
reports/     HTTP dashboards served by the collector process itself
```

Non-obvious decisions, each driven by an actual production failure:

- **Single process.** DuckDB locks the file to one writer — dashboards run inside
  the collector, over a read cursor guarded by an explicit lock.
- **Latency as a matrix.** `latencias_ms = [0, 15, 170]` runs six engines over
  the **same market tick**. A paired comparison: market regime cancels out and
  only the effect of distance remains.
- **Two fill rules in parallel.** `cruzamento` overcounts fills (the book also
  moves on cancellation), `negocio` undercounts (the feed prints ~9 trades per
  ~1,300 price changes). Truth sits between them, and the conservative one
  decides.
- **Watchdog + `restart: unless-stopped`.** Freezing silently during a multi-day
  collection is worse than crashing. The watchdog turns a freeze into a visible
  exit; Docker brings it back; the ledger is rebuilt from `paper_fills`.
- **Bounded analysis window.** At 3.9 GB, unbounded queries blew past the
  container memory limit and the kernel killed the process. DuckDB does not see
  cgroups: it reads host RAM and grants itself 80% of it.

---

## Running it

```bash
pip install -r requirements.txt

python -m collector.run          # collect (Ctrl+C to stop)
python -m reports.verify         # is the data trustworthy?
python -m reports.gate0          # the verdict
python -m analysis.latencia      # where this machine sits on the ladder
python -m pytest tests -q        # 142 tests
```

Dashboard at `http://127.0.0.1:8787` — collection health, wallet, fill-by-fill,
markout, niches.

With Docker: `docker compose up -d --build`. The dashboard is bound to
`127.0.0.1` deliberately — it has no authentication.

> `CONTEXT.md` is the working log, kept in Portuguese. It holds the full decision
> history, every measurement, and all 23 defects with their reasoning.

---

## What I take away

The plan said this before a single line was written: *"if it doesn't pass, the
project stops here. That is success, not failure: it cost time, not money, and
the answer is definitive."*

That is what happened. The hard part was never building the collector — it was
building an instrument willing to say **no**, and then believing it.
