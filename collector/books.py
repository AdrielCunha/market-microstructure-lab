"""Coletor de order books via WebSocket do CLOB.

Formato do canal `market` (verificado em produção):

- `book`             — livro completo. Chega na assinatura e em recomposições.
- `price_change`     — lote de mudanças. **Traz `best_bid`/`best_ask` já
                       calculados por asset**, então o topo de livro é exato sem
                       precisar reconstruir o livro a partir dos deltas.
- `last_trade_price` — execução, com `fee_rate_bps` (insumo do modelo de custo).
- `tick_size_change` — mudança de tick.

Volume medido: ~44 eventos/s com 60 tokens esportivos. Por isso o payload cru de
`price_change` só é gravado se `log_raw_price_change = true` — o resto do tempo
guardamos apenas a linha derivada em `book_top`, que é compacta.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, Iterable, Sequence

import websockets

from core import config
from core.api import PolymarketAPI, best_levels
from core.db import Store, now_ms
from engine.strategy import Topo

# Eventos cujo payload cru sempre vale a pena guardar (baixo volume, alto valor).
ALWAYS_RAW = {"book", "last_trade_price", "tick_size_change"}


def _f(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _top_row(token_id: str, ts_ex: int, ts_local: int, bid: float | None,
             bid_sz: float | None, ask: float | None, ask_sz: float | None,
             source: str) -> Sequence[Any]:
    mid = (bid + ask) / 2 if bid is not None and ask is not None else None
    spread = (ask - bid) if bid is not None and ask is not None else None
    return (token_id, ts_ex, ts_local, bid, bid_sz, ask, ask_sz, mid, spread, source)


class BookCollector:
    def __init__(self, store: Store, token_ids: Iterable[str]) -> None:
        cfg = config.load()
        self.store = store
        self.api_cfg = cfg["api"]
        self.cfg = cfg["books"]
        self.token_ids = list(dict.fromkeys(token_ids))
        self.log_raw_pc = bool(self.cfg.get("log_raw_price_change", False))
        self.dedupe_top = bool(self.cfg.get("dedupe_unchanged_top", True))
        # Último topo conhecido por token, para descartar repetição.
        self._last_top: dict[str, tuple[float | None, float | None]] = {}
        self.events_seen = 0
        self.rows_kept = 0
        self.rows_deduped = 0
        self.last_event_ts = 0.0
        self.geracao = 0
        self._resubscribe = asyncio.Event()
        # Ganchos da Fase 1. O coletor não conhece o motor de paper trading —
        # apenas avisa quem quiser ouvir. Assim a coleta continua funcionando
        # sozinha se o paper trading estiver desligado.
        self.on_top_callbacks: list = []
        self.on_trade_callbacks: list = []

    def _emit_top(self, token_id: str | None, ts_ex: int, ts_local: int,
                  bid: float | None, bid_sz: float | None,
                  ask: float | None, ask_sz: float | None, source: str) -> None:
        """Grava uma linha de topo de livro, pulando repetições.

        A maioria dos `price_change` mexe em níveis fundos e deixa o topo
        intacto. Guardar essas linhas multiplicaria o banco sem acrescentar
        nada: toda a análise da Fase 0 (spread, negative-risk, execução) só olha
        o topo. Snapshots de auditoria nunca são descartados — são justamente a
        referência para conferir a série.
        """
        if token_id is None:
            return
        if self.dedupe_top and source == "ws":
            key = (bid, ask)
            if self._last_top.get(token_id) == key:
                self.rows_deduped += 1
                return
            self._last_top[token_id] = key
        self.store.add("book_top",
                       _top_row(token_id, ts_ex, ts_local, bid, bid_sz, ask, ask_sz, source))
        self.rows_kept += 1

        if self.on_top_callbacks:
            topo = Topo(token_id, ts_local, bid, ask)
            for cb in self.on_top_callbacks:
                try:
                    cb(topo)
                except Exception as exc:
                    # Um erro no paper trading não pode derrubar a coleta: a
                    # série de mercado é o ativo, a simulação é acessório.
                    self.store.log("paper", "warn", "callback on_top falhou",
                                   {"erro": repr(exc)[:200]})

    # ---------- parsing ----------

    def handle_event(self, ev: dict[str, Any], ts_local: int) -> None:
        etype = ev.get("event_type")
        ts_ex = int(_f(ev.get("timestamp")) or ts_local)
        self.events_seen += 1
        self.last_event_ts = time.monotonic()

        if etype == "book":
            token_id = ev.get("asset_id")
            bid, bid_sz, ask, ask_sz = best_levels(ev)
            self._emit_top(token_id, ts_ex, ts_local, bid, bid_sz, ask, ask_sz, "ws")
            self._raw(token_id, etype, ts_ex, ts_local, ev)

        elif etype == "price_change":
            for ch in ev.get("price_changes") or []:
                token_id = ch.get("asset_id")
                bid, ask = _f(ch.get("best_bid")), _f(ch.get("best_ask"))
                # Só conhecemos o tamanho do nível que mudou. Se ele for o topo,
                # aproveitamos; caso contrário fica NULL em vez de mentir.
                price, size = _f(ch.get("price")), _f(ch.get("size"))
                bid_sz = size if price is not None and price == bid else None
                ask_sz = size if price is not None and price == ask else None
                self._emit_top(token_id, ts_ex, ts_local, bid, bid_sz, ask, ask_sz, "ws")
                if self.log_raw_pc:
                    self._raw(token_id, etype, ts_ex, ts_local, ch)

        elif etype in ALWAYS_RAW:
            self._raw(ev.get("asset_id"), etype, ts_ex, ts_local, ev)
            if etype == "last_trade_price" and self.on_trade_callbacks:
                token_id = ev.get("asset_id")
                price, size = _f(ev.get("price")), _f(ev.get("size"))
                side = ev.get("side")
                if token_id and price is not None and size is not None:
                    for cb in self.on_trade_callbacks:
                        try:
                            cb(token_id, ts_local, price, side, size)
                        except Exception as exc:
                            self.store.log("paper", "warn",
                                           "callback on_trade falhou",
                                           {"erro": repr(exc)[:200]})

    def _raw(self, token_id: str | None, etype: str, ts_ex: int, ts_local: int,
             payload: dict[str, Any]) -> None:
        self.store.add("book_events",
                       (self.store.take_seq(), token_id, etype, ts_ex, ts_local,
                        json.dumps(payload, separators=(",", ":"))))

    # ---------- conexão ----------

    async def _run_socket(self, chunk: list[str], name: str) -> None:
        url = self.api_cfg["ws_market"]
        backoff = self.cfg.get("reconnect_min_s", 1.0)
        while True:
            try:
                async with websockets.connect(url, ping_interval=10, ping_timeout=20,
                                              max_size=16 * 1024 * 1024) as ws:
                    await ws.send(json.dumps({"assets_ids": chunk, "type": "market"}))
                    self.store.log("books", "info", f"{name} conectado",
                                   {"tokens": len(chunk)})
                    backoff = self.cfg.get("reconnect_min_s", 1.0)
                    async for raw in ws:
                        ts_local = now_ms()
                        data = json.loads(raw)
                        for ev in (data if isinstance(data, list) else [data]):
                            if isinstance(ev, dict):
                                self.handle_event(ev, ts_local)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # reconectar é o caso normal, não a exceção
                self.store.log("books", "warn", f"{name} caiu, reconectando",
                               {"erro": repr(exc)[:300], "backoff_s": backoff})
                await asyncio.sleep(backoff + random.uniform(0, 1))
                backoff = min(backoff * 2, self.cfg.get("reconnect_max_s", 60.0))

    async def _audit_loop(self) -> None:
        """Snapshot REST periódico gravado com source='rest_audit'.

        Serve para a verificação do plano: a série derivada do WebSocket tem que
        bater com o livro real. Divergência sistemática = parser quebrado.
        """
        interval = self.cfg.get("audit_interval_s", 900)
        sample_size = int(self.cfg.get("audit_sample", 40))
        await asyncio.sleep(30)
        async with PolymarketAPI() as api:
            while True:
                try:
                    sample = random.sample(self.token_ids,
                                           min(sample_size, len(self.token_ids)))
                    books = await api.books(sample)
                    ts_local = now_ms()
                    for bk in books:
                        token_id = bk.get("asset_id")
                        bid, bid_sz, ask, ask_sz = best_levels(bk)
                        ts_ex = int(_f(bk.get("timestamp")) or ts_local)
                        self._emit_top(token_id, ts_ex, ts_local, bid, bid_sz,
                                       ask, ask_sz, "rest_audit")
                    self.store.log("books", "info", "auditoria REST",
                                   {"tokens": len(books)})
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.store.log("books", "warn", "auditoria falhou",
                                   {"erro": repr(exc)[:300]})
                await asyncio.sleep(interval)

    def update_tokens(self, tokens: list[str]) -> bool:
        """Troca o conjunto assinado. Devolve True se houve mudança real.

        Metade do catálogo vence em menos de 24h: numa coleta de dias, sem isto
        o coletor ficaria vigiando mercados já resolvidos a partir do segundo
        dia e a série longa não valeria nada.
        """
        novos = list(dict.fromkeys(tokens))
        if set(novos) == set(self.token_ids):
            return False
        antigos = set(self.token_ids)
        self.token_ids = novos
        self._resubscribe.set()
        self.store.log("books", "info", "conjunto de tokens mudou", {
            "entraram": len(set(novos) - antigos),
            "sairam": len(antigos - set(novos)),
            "total": len(novos),
        })
        return True

    async def run(self) -> None:
        # O loop de auditoria é persistente: ele lê `self.token_ids` a cada
        # rodada, então sobrevive a uma troca de assinatura sem reiniciar.
        auditoria = asyncio.create_task(self._audit_loop(), name="audit")
        try:
            while True:
                # Uma conexão por bloco: 400 assets num socket só aumenta o
                # custo de uma reconexão e o tamanho do frame.
                size = int(self.cfg.get("tokens_per_socket", 150))
                tokens = self.token_ids
                chunks = [tokens[i:i + size] for i in range(0, len(tokens), size)]
                self._resubscribe.clear()
                sockets = [asyncio.create_task(self._run_socket(c, f"ws{i}"),
                                               name=f"ws{i}")
                           for i, c in enumerate(chunks)]
                self.geracao += 1
                try:
                    # Os sockets rodam para sempre; quem encerra esta geração é
                    # o sinal de reassinatura.
                    await self._resubscribe.wait()
                finally:
                    for s in sockets:
                        s.cancel()
                    await asyncio.gather(*sockets, return_exceptions=True)
                # O topo conhecido vale para a assinatura anterior; manter o
                # cache faria o dedupe engolir a primeira leitura dos tokens
                # novos.
                self._last_top.clear()
                self.store.log("books", "info", "reassinando",
                               {"geracao": self.geracao, "tokens": len(self.token_ids)})
        finally:
            auditoria.cancel()
            await asyncio.gather(auditoria, return_exceptions=True)
