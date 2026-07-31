"""Testes do painel ordem a ordem.

A propriedade que importa: o detalhe TEM de somar o total. Uma tela que mostra
execucao por execucao e nao bate com o resumo e pior que nao ter tela — duas
telas do mesmo sistema discordando fazem quem olha parar de confiar nas duas.
"""

from __future__ import annotations

import duckdb
import pytest

from core.db import SCHEMA
from reports import ordens

EST = "maker_negocio_lat15"


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute(SCHEMA)
    return c


def grava(con, ts, side, price, size, rebate=0.0, taxa=0.0,
          regra="negocio", agressiva=False, token="t1"):
    con.execute(
        "INSERT INTO paper_fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [ts, EST, regra, token, side, price, size, price * size,
         price, 0.02, rebate, 0.0, taxa, agressiva])


class TestReconciliacao:
    def test_soma_das_linhas_bate_com_o_total(self, con):
        grava(con, 1000, "BUY", 0.40, 100, rebate=0.30)
        grava(con, 2000, "SELL", 0.45, 100, rebate=0.30)
        grava(con, 3000, "BUY", 0.30, 100, rebate=0.30, token="t2")
        grava(con, 4000, "SELL", 0.28, 100, taxa=1.40, agressiva=True, token="t2")

        d = ordens.carregar(con, EST)
        led = d["ledger"]

        assert sum(e["delta"] for e in d["execucoes"]) == pytest.approx(led.realizado)
        assert sum(e["liquido"] for e in d["execucoes"]) == pytest.approx(
            led.realizado + led.rebates - led.taxas)

    def test_compra_nao_realiza_nada_venda_realiza(self, con):
        grava(con, 1000, "BUY", 0.40, 100)
        grava(con, 2000, "SELL", 0.45, 100)
        e = ordens.carregar(con, EST)["execucoes"]
        assert e[0]["delta"] == pytest.approx(0.0), "abrir posicao nao realiza"
        assert e[1]["delta"] == pytest.approx(5.0), "5c x 100 cotas"

    def test_corte_de_sessao_ignora_historico_antigo(self, con):
        """Sem isto a tela mostraria rodadas antigas, algumas com bugs ja
        corrigidos, e o painel discordaria do motor."""
        grava(con, 1000, "BUY", 0.90, 100)      # sessao velha
        grava(con, 2000, "SELL", 0.10, 100)     # prejuizo enorme, ficou para tras
        con.execute("INSERT INTO paper_sessao VALUES (?, ?, ?)", [EST, 5000, 5000])
        grava(con, 6000, "BUY", 0.40, 100)
        grava(con, 7000, "SELL", 0.45, 100)

        d = ordens.carregar(con, EST)
        assert len(d["execucoes"]) == 2
        assert d["ledger"].realizado == pytest.approx(5.0)


class TestCiclos:
    def test_ciclo_vai_de_zero_a_zero(self, con):
        grava(con, 1000, "BUY", 0.40, 100, rebate=0.30)
        grava(con, 1500, "BUY", 0.38, 100, rebate=0.30)   # aumenta, nao fecha
        grava(con, 9000, "SELL", 0.42, 200, rebate=0.60)  # zera

        ciclos = ordens.carregar(con, EST)["ciclos"]
        assert len(ciclos) == 1
        c = ciclos[0]
        assert c["lado"] == "comprado"
        assert c["cotas"] == pytest.approx(200)
        assert c["execucoes"] == 3
        assert c["duracao"] == 8000
        # (0.42-0.39)*200 de spread + 1.20 de rebates
        assert c["resultado"] == pytest.approx(6.0 + 1.20)

    def test_posicao_aberta_nao_vira_ciclo(self, con):
        grava(con, 1000, "BUY", 0.40, 100)
        d = ordens.carregar(con, EST)
        assert d["ciclos"] == []
        assert len(d["abertas"]) == 1
        assert d["abertas"][0]["qtd"] == pytest.approx(100)

    def test_ciclo_registra_como_terminou(self, con):
        grava(con, 1000, "BUY", 0.40, 100)
        grava(con, 2000, "SELL", 0.0, 100, regra="liquidacao", token="t1")
        c = ordens.carregar(con, EST)["ciclos"][0]
        assert c["encerrou"] == "resolucao", \
            "fechar por resolucao nao e market making e precisa ficar visivel"

    def test_saida_forcada_aparece_rotulada(self, con):
        grava(con, 1000, "BUY", 0.40, 100)
        grava(con, 2000, "SELL", 0.38, 100, taxa=1.90, agressiva=True)
        c = ordens.carregar(con, EST)["ciclos"][0]
        assert c["encerrou"] == "saida forcada"


class TestListaDeMotores:
    """A aba do motor que importa nao pode sumir por falta de execucao.

    A regra `negocio` so conta negocio impresso, e o feed publica poucos. Nas
    primeiras horas ela fica com zero execucoes. Listar apenas quem tem linha em
    `paper_fills` a escondia — e a pagina caia calada no `cruzamento`, o motor
    cujo resultado nao vale.
    """

    def test_motor_ligado_sem_execucao_aparece(self, con):
        con.execute("INSERT INTO paper_sessao VALUES (?, 0, 0)", [EST])
        con.execute("INSERT INTO paper_sessao VALUES "
                    "('maker_cruzamento_lat15', 0, 0)")
        grava(con, 1000, "BUY", 0.40, 100, token="t1")  # so o cruzamento executou
        con.execute("UPDATE paper_fills SET strategy = 'maker_cruzamento_lat15'")

        motores = ordens.motores_disponiveis(con)
        assert EST in motores, "motor sem execucao sumiu da lista"
        assert "maker_cruzamento_lat15" in motores

    def test_padrao_e_respeitado_mesmo_sem_execucao(self, con):
        con.execute("INSERT INTO paper_sessao VALUES (?, 0, 0)", [EST])
        con.execute("INSERT INTO paper_sessao VALUES "
                    "('maker_cruzamento_lat0', 0, 0)")
        html = ordens.pagina(con)
        assert f'class="on" href="/ordens?e={EST}"' in html, \
            "a pagina caiu em outro motor em vez de mostrar o padrao vazio"

    def test_aba_vazia_explica_em_vez_de_parecer_quebrada(self, con):
        con.execute("INSERT INTO paper_sessao VALUES (?, 0, 0)", [EST])
        html = ordens.pagina(con, EST)
        assert "negocio IMPRESSO" in html or "negocio</b> so conta" in html


class TestPaginaNaoQuebra:
    def test_sem_motor_nenhum(self, con):
        """Banco virgem: nenhum motor registrado ainda. E diferente de 'motor
        ligado sem execucao', que agora tem tela propria."""
        assert "Nenhum motor ligado" in ordens.pagina(con)

    def test_banco_sem_paper_sessao(self):
        """Analise de arquivo antigo nao pode derrubar a tela."""
        c = duckdb.connect(":memory:")
        c.execute(SCHEMA)
        c.execute("DROP TABLE paper_sessao")
        grava(c, 1000, "BUY", 0.40, 100)
        assert "<!doctype html>" in ordens.pagina(c, EST)

    def test_motor_inexistente_cai_no_padrao(self, con):
        grava(con, 1000, "BUY", 0.40, 100)
        html = ordens.pagina(con, "motor_que_nao_existe")
        assert "<!doctype html>" in html
        assert EST in html
