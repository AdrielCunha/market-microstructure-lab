"""Testes da reassinatura em voo do WebSocket.

Metade do catálogo vence em menos de 24h. Numa coleta de 7 dias, um coletor que
só assina os tokens do momento em que subiu passaria a maior parte do tempo
vigiando mercados já resolvidos — e a série longa, que é o produto inteiro da
Fase 0, não valeria nada. Estes testes travam esse comportamento.
"""

from __future__ import annotations

import asyncio

import duckdb
import pytest

from collector.books import BookCollector
from core.db import SCHEMA, Store


@pytest.fixture
def store() -> Store:
    con = duckdb.connect(":memory:")
    con.execute(SCHEMA)
    return Store(con, auto_flush=False)


class TestUpdateTokens:
    def test_conjunto_igual_nao_reassina(self, store):
        """Reassinar derruba os sockets e abre um buraco na série. Só vale a
        pena quando o conjunto realmente mudou."""
        c = BookCollector(store, ["a", "b"])
        assert c.update_tokens(["b", "a"]) is False, "mesma lista em outra ordem"
        assert not c._resubscribe.is_set()

    def test_conjunto_diferente_sinaliza(self, store):
        c = BookCollector(store, ["a", "b"])
        assert c.update_tokens(["b", "c"]) is True
        assert c._resubscribe.is_set()
        assert c.token_ids == ["b", "c"]

    def test_registra_entradas_e_saidas(self, store):
        c = BookCollector(store, ["a", "b"])
        c.update_tokens(["b", "c", "d"])
        detalhe = store._buf["collector_log"][-1][4]
        assert '"entraram": 2' in detalhe
        assert '"sairam": 1' in detalhe

    def test_remove_duplicatas(self, store):
        c = BookCollector(store, [])
        c.update_tokens(["a", "a", "b"])
        assert c.token_ids == ["a", "b"]


class TestLoopDeReassinatura:
    """Exercita `run()` com o socket substituído, para verificar o ciclo de
    vida das gerações sem depender da rede."""

    def _preparar(self, store, monkeypatch, tokens):
        c = BookCollector(store, tokens)
        abertos: list[list[str]] = []
        cancelados: list[list[str]] = []

        async def socket_falso(chunk, nome):
            abertos.append(list(chunk))
            try:
                await asyncio.Event().wait()   # roda para sempre
            except asyncio.CancelledError:
                cancelados.append(list(chunk))
                raise

        async def auditoria_falsa():
            await asyncio.Event().wait()

        monkeypatch.setattr(c, "_run_socket", socket_falso)
        monkeypatch.setattr(c, "_audit_loop", auditoria_falsa)
        return c, abertos, cancelados

    @pytest.mark.parametrize("tokens_iniciais", [["t1", "t2"]])
    def test_troca_de_tokens_derruba_e_reabre_sockets(self, store, monkeypatch,
                                                      tokens_iniciais):
        c, abertos, cancelados = self._preparar(store, monkeypatch, tokens_iniciais)

        async def cenario():
            tarefa = asyncio.create_task(c.run())
            await asyncio.sleep(0.05)
            assert abertos == [["t1", "t2"]], "primeira geracao assinada"
            assert c.geracao == 1

            c.update_tokens(["t3", "t4", "t5"])
            await asyncio.sleep(0.05)

            assert cancelados == [["t1", "t2"]], "geracao antiga foi derrubada"
            assert abertos[-1] == ["t3", "t4", "t5"], "nova geracao assinada"
            assert c.geracao == 2

            tarefa.cancel()
            await asyncio.gather(tarefa, return_exceptions=True)

        asyncio.run(cenario())

    def test_cache_de_topo_e_limpo_ao_reassinar(self, store, monkeypatch):
        """O último topo conhecido vale para a assinatura anterior. Mantê-lo
        faria o dedupe engolir a primeira leitura dos tokens novos."""
        c, _, _ = self._preparar(store, monkeypatch, ["t1"])

        async def cenario():
            tarefa = asyncio.create_task(c.run())
            await asyncio.sleep(0.05)
            c._last_top["t1"] = (0.4, 0.42)

            c.update_tokens(["t2"])
            await asyncio.sleep(0.05)
            assert c._last_top == {}, "cache tem que zerar entre geracoes"

            tarefa.cancel()
            await asyncio.gather(tarefa, return_exceptions=True)

        asyncio.run(cenario())

    def test_divide_em_varios_sockets(self, store, monkeypatch):
        c, abertos, _ = self._preparar(store, monkeypatch,
                                       [f"t{i}" for i in range(350)])

        async def cenario():
            tarefa = asyncio.create_task(c.run())
            await asyncio.sleep(0.05)
            assert len(abertos) == 3, "350 tokens em blocos de 150 = 3 sockets"
            assert sum(len(x) for x in abertos) == 350
            tarefa.cancel()
            await asyncio.gather(tarefa, return_exceptions=True)

        asyncio.run(cenario())
