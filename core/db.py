"""Camada DuckDB: schema + escrita com buffer.

DuckDB não aceita dois processos escrevendo o mesmo arquivo. Por isso todos os
coletores rodam num único processo (`collector/run.py`) compartilhando um Store.
As análises abrem o arquivo em modo leitura e podem rodar em paralelo.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

import duckdb
import polars as pl

from core import config

SCHEMA = """
CREATE SEQUENCE IF NOT EXISTS seq_book_events START 1;

-- Catálogo de tokens negociáveis (cada mercado binário tem 2 tokens: Yes/No).
CREATE TABLE IF NOT EXISTS markets (
    token_id         VARCHAR PRIMARY KEY,
    condition_id     VARCHAR,
    event_id         VARCHAR,
    event_slug       VARCHAR,
    event_title      VARCHAR,
    question         VARCHAR,
    outcome          VARCHAR,
    outcome_index    INTEGER,
    neg_risk         BOOLEAN,
    neg_risk_market  VARCHAR,
    -- Quantos resultados o evento tem no total. A análise de negative-risk só
    -- pode somar preços se tivermos TODAS as pernas; sem isso o desvio medido
    -- é perna faltando, não arbitragem.
    event_n_outcomes INTEGER,
    category         VARCHAR,
    end_date         TIMESTAMP,
    liquidity_usd    DOUBLE,
    volume_usd       DOUBLE,
    min_tick_size    DOUBLE,
    order_min_size   DOUBLE,
    -- Economia da taxa. Em esportes o Polymarket cobra taker numa curva
    -- rate * p * (1-p) e devolve rebate ao maker: é o motor de quem faz mercado.
    fee_type         VARCHAR,
    fee_rate         DOUBLE,
    fee_exponent     DOUBLE,
    fee_rebate_rate  DOUBLE,
    fee_taker_only   BOOLEAN,
    fees_enabled     BOOLEAN,
    active           BOOLEAN,
    closed           BOOLEAN,
    accepting_orders BOOLEAN,
    -- True = está no conjunto que o coletor assina agora. O catálogo acumula
    -- histórico; a seleção muda a cada refresh.
    selected         BOOLEAN,
    seen_at          TIMESTAMP,
    updated_at       TIMESTAMP
);

-- Log bruto de tudo que chega do WebSocket. Nunca descartar informação: se o
-- schema derivado estiver errado, dá pra reprocessar a partir daqui.
CREATE TABLE IF NOT EXISTS book_events (
    seq          BIGINT PRIMARY KEY,
    token_id     VARCHAR,
    event_type   VARCHAR,   -- book | price_change | last_trade_price | tick_size_change
    ts_exchange  BIGINT,    -- ms, relógio do exchange
    ts_local     BIGINT,    -- ms, relógio local no momento do recebimento
    payload      JSON
);

-- Topo de livro derivado, para análise rápida. Uma linha por atualização.
CREATE TABLE IF NOT EXISTS book_top (
    token_id       VARCHAR,
    ts_exchange    BIGINT,
    ts_local       BIGINT,
    best_bid       DOUBLE,
    best_bid_size  DOUBLE,
    best_ask       DOUBLE,
    best_ask_size  DOUBLE,
    mid            DOUBLE,
    spread         DOUBLE,
    source         VARCHAR  -- ws | rest_audit
);

-- Trades públicos das carteiras vigiadas.
-- ts_seen - ts_trade = quanto tempo eu levo pra enxergar o trade. É o custo
-- mínimo de qualquer estratégia de cópia.
CREATE TABLE IF NOT EXISTS wallet_trades (
    trade_uid     VARCHAR PRIMARY KEY,
    wallet        VARCHAR,
    name          VARCHAR,
    token_id      VARCHAR,
    condition_id  VARCHAR,
    side          VARCHAR,
    size          DOUBLE,
    price         DOUBLE,
    notional_usd  DOUBLE,
    ts_trade      BIGINT,   -- ms
    ts_seen       BIGINT,   -- ms
    -- True = veio no primeiro poll da carteira, então já era histórico quando
    -- começamos. O atraso de visão desses trades não significa nada e eles
    -- precisam ficar de fora da medição de copyability.
    is_backfill   BOOLEAN,
    title         VARCHAR,
    outcome       VARCHAR,
    slug          VARCHAR,
    tx_hash       VARCHAR
);

-- Execuções SIMULADAS da Fase 1. Nenhuma ordem real foi enviada.
-- Uma linha por fill, por regra de execução — as duas regras rodam em paralelo
-- sobre o mesmo fluxo de mercado para emparedar o resultado real.
CREATE TABLE IF NOT EXISTS paper_fills (
    ts_local        BIGINT,
    strategy        VARCHAR,   -- ex.: maker_conservador
    regra           VARCHAR,   -- conservador | otimista
    token_id        VARCHAR,
    side            VARCHAR,
    price           DOUBLE,
    size            DOUBLE,
    notional_usd    DOUBLE,
    mid_at_fill     DOUBLE,
    spread_at_fill  DOUBLE,
    rebate          DOUBLE,
    posicao_depois  DOUBLE,
    -- Taxa de taker paga. Só existe em execução AGRESSIVA: quem fica parado no
    -- livro não paga taxa, recebe rebate. A saída forçada de estoque atravessa
    -- o spread e vira taker — é o custo de não ficar com o mico na mão.
    taxa            DOUBLE DEFAULT 0.0,
    agressiva       BOOLEAN DEFAULT FALSE
);

-- Colunas acrescentadas depois: `CREATE TABLE IF NOT EXISTS` não altera tabela
-- que já existe, e o banco de coleta é longo demais para recriar.
ALTER TABLE paper_fills ADD COLUMN IF NOT EXISTS taxa DOUBLE DEFAULT 0.0;
ALTER TABLE paper_fills ADD COLUMN IF NOT EXISTS agressiva BOOLEAN DEFAULT FALSE;

-- De que instante em diante as execuções de cada estratégia contam.
--
-- Existe por causa do deploy automático: reiniciar o processo zerava o
-- livro-caixa, que vive em memória, e um push no meio da noite destruía dias de
-- experimento. Agora o motor reconstrói o estado relendo `paper_fills`.
--
-- O corte é necessário porque `paper_fills` acumula sessões antigas, algumas
-- rodadas com bugs já corrigidos (venda a descoberto de graça, por exemplo).
-- Reprocessar aquilo ressuscitaria carteira impossível. Sem linha aqui = começa
-- do zero e grava o corte agora.
CREATE TABLE IF NOT EXISTS paper_sessao (
    strategy   VARCHAR PRIMARY KEY,
    desde_ts   BIGINT,
    criada_em  BIGINT
);

-- Registro de saúde do coletor: gaps, reconexões, erros.
CREATE TABLE IF NOT EXISTS collector_log (
    ts_local   BIGINT,
    component  VARCHAR,
    level      VARCHAR,
    message    VARCHAR,
    detail     JSON
);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def _limitar_memoria(con: duckdb.DuckDBPyConnection) -> None:
    """Impede o DuckDB de estourar o teto de memória do container.

    O DuckDB não enxerga cgroup: ele lê a RAM da MÁQUINA e define o próprio
    limite em ~80% disso. Numa droplet de 2 GB isso dá ~1,5 GB — exatamente o
    teto do container. Resultado medido em produção:

        Memory cgroup out of memory: Killed process (python)
        MEM 1.465GiB / 1.465GiB  99.98%

    Cinco mortes por OOM em meia hora, com o Docker reerguendo a cada vez. O
    sintoma na tela era "o painel não responde"; a causa era o banco pedindo
    mais memória do que o container pode dar, numa consulta pesada.

    Com o limite explícito o DuckDB **derrama para disco** em vez de morrer —
    a consulta fica mais lenta, que é o preço certo a pagar. `threads` também
    é fixado: a droplet tem 1 vCPU, e paralelismo demais só multiplica o
    consumo de memória sem acelerar nada.
    """
    cfg = config.load()
    try:
        mem = str(cfg["analise"].get("duckdb_memoria", "512MB"))
        threads = int(cfg["analise"].get("duckdb_threads", 2))
    except Exception:
        mem, threads = "512MB", 2
    for sql in (f"SET memory_limit='{mem}'", f"SET threads={threads}"):
        try:
            con.execute(sql)
        except duckdb.Error:
            pass          # versão antiga do DuckDB: segue sem o ajuste


def connect(read_only: bool = False, path: Path | None = None) -> duckdb.DuckDBPyConnection:
    cfg = config.load()
    p = path or cfg.db_path
    p.parent.mkdir(parents=True, exist_ok=True)
    if read_only and not p.exists():
        raise FileNotFoundError(f"Banco não existe ainda: {p}. Rode o coletor primeiro.")
    con = duckdb.connect(str(p), read_only=read_only)
    _limitar_memoria(con)
    if not read_only:
        con.execute(SCHEMA)
    return con


class Store:
    """Escrita com buffer. Insert linha-a-linha em DuckDB é lento demais para
    o volume de um WebSocket de 400 tokens; acumulamos e damos flush em lote."""

    def __init__(self, con: duckdb.DuckDBPyConnection, flush_rows: int = 500,
                 flush_interval_s: float = 5.0, auto_flush: bool = True) -> None:
        self.con = con
        self.flush_rows = flush_rows
        self.flush_interval_s = flush_interval_s
        # auto_flush=False: `add` só enfileira em memória e quem chama decide
        # quando gravar. É o modo do coletor, que faz o flush numa thread
        # separada para não travar o event loop no meio de uma rajada.
        self.auto_flush = auto_flush
        self._buf: dict[str, list[Sequence[Any]]] = {}
        self._last_flush = time.monotonic()
        self._lock = threading.Lock()
        self._seq = self._next_seq()
        self.rows_written = 0
        # TODO acesso ao banco passa por aqui.
        #
        # A conexão do DuckDB NÃO é thread-safe. O flusher roda numa thread
        # (`asyncio.to_thread`) enquanto o refresh de catálogo, a liquidação e
        # os relatórios do dashboard usam a mesma conexão a partir do event
        # loop. Isso congelou o coletor em produção depois de 2,7h: processo
        # vivo, porta escutando, e nada mais acontecendo.
        self.db_lock = threading.RLock()

    def _next_seq(self) -> int:
        row = self.con.execute("SELECT COALESCE(MAX(seq), 0) FROM book_events").fetchone()
        return int(row[0]) + 1 if row else 1

    def take_seq(self) -> int:
        with self._lock:
            s = self._seq
            self._seq += 1
            return s

    def add(self, table: str, row: Sequence[Any]) -> None:
        with self._lock:
            self._buf.setdefault(table, []).append(row)
            if not self.auto_flush:
                return
            total = sum(len(v) for v in self._buf.values())
            due = (total >= self.flush_rows
                   or time.monotonic() - self._last_flush >= self.flush_interval_s)
        if due:
            self.flush()

    def pending(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._buf.values())

    def add_many(self, table: str, rows: Iterable[Sequence[Any]]) -> None:
        for r in rows:
            self.add(table, r)

    def log(self, component: str, level: str, message: str, detail: Any = None) -> None:
        self.add("collector_log",
                 (now_ms(), component, level, message, json.dumps(detail) if detail else None))

    def execute(self, sql: str, params: Any = None):
        """Consulta protegida pelo lock. Use SEMPRE isto em vez de `store.con`."""
        with self.db_lock:
            return self.con.execute(sql, params) if params is not None \
                else self.con.execute(sql)

    def executemany(self, sql: str, seq_params):
        with self.db_lock:
            return self.con.executemany(sql, seq_params)

    def flush(self) -> None:
        with self._lock:
            buf, self._buf = self._buf, {}
            self._last_flush = time.monotonic()
        with self.db_lock:
            self._flush_locked(buf)

    def _flush_locked(self, buf: dict[str, list[Sequence[Any]]]) -> None:
        for table, rows in buf.items():
            if not rows:
                continue
            verb = "INSERT OR IGNORE INTO" if table in _DEDUPED else "INSERT INTO"
            schema = TABLE_SCHEMAS.get(table)
            if schema is None:
                placeholders = ", ".join("?" * len(rows[0]))
                self.con.executemany(f"{verb} {table} VALUES ({placeholders})", rows)
            else:
                # executemany do DuckDB é prepared-statement linha a linha e não
                # aguenta o volume do WebSocket (medido: ~870 eventos/s). Passar
                # um DataFrame faz a inserção virar um scan vetorizado.
                frame = pl.DataFrame(rows, schema=schema, orient="row")
                self.con.register("_flush_buf", frame)
                try:
                    self.con.execute(f"{verb} {table} SELECT * FROM _flush_buf")
                finally:
                    self.con.unregister("_flush_buf")
            self.rows_written += len(rows)

    def close(self) -> None:
        self.flush()
        self.con.close()


# Tabelas com chave primária onde reinserção é esperada (dedupe silencioso).
_DEDUPED = {"wallet_trades", "book_events"}

# Schemas explícitos para o caminho rápido de escrita. Sem eles o polars
# inferiria o tipo de uma coluna toda-nula como Null e a inserção quebraria.
TABLE_SCHEMAS: dict[str, dict[str, Any]] = {
    "book_top": {
        "token_id": pl.Utf8, "ts_exchange": pl.Int64, "ts_local": pl.Int64,
        "best_bid": pl.Float64, "best_bid_size": pl.Float64,
        "best_ask": pl.Float64, "best_ask_size": pl.Float64,
        "mid": pl.Float64, "spread": pl.Float64, "source": pl.Utf8,
    },
    "book_events": {
        "seq": pl.Int64, "token_id": pl.Utf8, "event_type": pl.Utf8,
        "ts_exchange": pl.Int64, "ts_local": pl.Int64, "payload": pl.Utf8,
    },
    "wallet_trades": {
        "trade_uid": pl.Utf8, "wallet": pl.Utf8, "name": pl.Utf8, "token_id": pl.Utf8,
        "condition_id": pl.Utf8, "side": pl.Utf8, "size": pl.Float64,
        "price": pl.Float64, "notional_usd": pl.Float64, "ts_trade": pl.Int64,
        "ts_seen": pl.Int64, "is_backfill": pl.Boolean, "title": pl.Utf8,
        "outcome": pl.Utf8, "slug": pl.Utf8, "tx_hash": pl.Utf8,
    },
    "collector_log": {
        "ts_local": pl.Int64, "component": pl.Utf8, "level": pl.Utf8,
        "message": pl.Utf8, "detail": pl.Utf8,
    },
    "paper_fills": {
        "ts_local": pl.Int64, "strategy": pl.Utf8, "regra": pl.Utf8,
        "token_id": pl.Utf8, "side": pl.Utf8, "price": pl.Float64,
        "size": pl.Float64, "notional_usd": pl.Float64,
        "mid_at_fill": pl.Float64, "spread_at_fill": pl.Float64,
        "rebate": pl.Float64, "posicao_depois": pl.Float64,
        "taxa": pl.Float64, "agressiva": pl.Boolean,
    },
}
