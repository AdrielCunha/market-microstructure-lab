"""Testes do valor de saída e da barra de navegação.

O valor de saída existe porque `patrimonio` responde a pergunta contábil e nao
a pergunta do bolso. Quem le "estou com $1.000,30" e decide colocar dinheiro
real precisa saber que, desmontando agora, sairiam $996,40 — a diferenca e o
spread inteiro mais a taxa de taker.
"""

from __future__ import annotations

import duckdb
import pytest

from core.db import SCHEMA, now_ms
from engine.ledger import Ledger
from reports import carteira, index, nav, paper_dash


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute(SCHEMA)
    c.execute("INSERT INTO markets (token_id, fee_rate) VALUES ('t1', 0.05)")
    # Dentro da janela de análise: as consultas pesadas passaram a olhar só as
    # últimas horas (`core/janela.py`), porque varrer o banco inteiro segurava o
    # lock e travava o coletor. Um timestamp fixo em 1970 cai fora e a posição
    # apareceria como "sem cotacao".
    t = now_ms() - 3_600_000
    c.execute("INSERT INTO book_top VALUES (?,?,?,?,?,?,?,?,?,'ws')",
              ["t1", t, t, 0.38, 10, 0.42, 10, 0.40, 0.04])
    return c


class TestValorDeSaida:
    def test_sair_custa_o_spread_inteiro_mais_taxa(self, con):
        led = Ledger(capital_inicial=1000.0)
        led.aplicar("t1", "BUY", 0.40, 100, rebate=0.30)

        a = carteira.avaliar(con, led)
        # Pelo mid (0.40) a posicao esta no zero a zero...
        assert a["nao_realizado_mid"] == pytest.approx(0.0)
        # ...mas para sair e preciso VENDER no bid (0.38): -2c x 100 cotas.
        assert a["nao_realizado_saida"] == pytest.approx(-2.0)
        assert a["custo_para_sair"] > 0, "sair e ser taker, e taker paga taxa"
        assert a["valor_de_saida"] < a["patrimonio"]
        assert a["otimismo"] == pytest.approx(
            a["patrimonio"] - a["valor_de_saida"])

    def test_vendido_sai_pelo_ask(self, con):
        """Comprado desmonta no bid; vendido desmonta no ask. Errar o lado
        inverteria o sinal do numero que mais importa."""
        led = Ledger(capital_inicial=1000.0)
        led.aplicar("t1", "SELL", 0.40, 100)
        a = carteira.avaliar(con, led)
        # Recomprar no ask 0.42 custa 2c a mais por cota.
        assert a["nao_realizado_saida"] == pytest.approx(-2.0)

    def test_sem_rebate_e_o_piso_do_piso(self, con):
        """A formula da taxa nunca foi confirmada e o rebate chegou a ser 60%
        do lucro. O painel precisa mostrar o resultado sem ele."""
        led = Ledger(capital_inicial=1000.0)
        led.aplicar("t1", "BUY", 0.40, 100, rebate=5.0)
        a = carteira.avaliar(con, led)
        assert a["sem_rebate"] == pytest.approx(a["valor_de_saida"] - 5.0)

    def test_posicao_sem_livro_nao_inventa_preco(self, con):
        led = Ledger(capital_inicial=1000.0)
        led.aplicar("fantasma", "BUY", 0.50, 100)
        a = carteira.avaliar(con, led)
        assert a["sem_preco"] == 1
        assert a["nao_realizado_saida"] == pytest.approx(0.0), \
            "sem cotacao, avalia pelo custo — nunca por preco inventado"

    def test_cotacao_velha_demais_conta_como_sem_preco(self, con):
        """Preco de dias atras nao serve para dizer quanto sai hoje. Fora da
        janela, a posicao e avaliada pelo custo e sinalizada."""
        antigo = now_ms() - 30 * 24 * 3_600_000
        con.execute("INSERT INTO markets (token_id, fee_rate) VALUES ('velho', 0.05)")
        con.execute("INSERT INTO book_top VALUES (?,?,?,?,?,?,?,?,?,'ws')",
                    ["velho", antigo, antigo, 0.10, 10, 0.90, 10, 0.50, 0.80])
        led = Ledger(capital_inicial=1000.0)
        led.aplicar("velho", "BUY", 0.50, 100)
        a = carteira.avaliar(con, led)
        assert a["sem_preco"] == 1
        assert a["nao_realizado_saida"] == pytest.approx(0.0)

    def test_carteira_vazia_e_o_capital_inicial(self, con):
        a = carteira.avaliar(con, Ledger(capital_inicial=1000.0))
        assert a["valor_de_saida"] == pytest.approx(1000.0)
        assert a["custo_para_sair"] == pytest.approx(0.0)
        assert a["otimismo"] == pytest.approx(0.0)

    def test_taxas_ja_pagas_entram_nas_duas_contas(self, con):
        led = Ledger(capital_inicial=1000.0)
        led.aplicar("t1", "BUY", 0.40, 100)
        led.aplicar("t1", "SELL", 0.42, 100, taxa=1.90)
        a = carteira.avaliar(con, led)
        assert a["taxas_pagas"] == pytest.approx(1.90)
        assert a["valor_de_saida"] == pytest.approx(1000.0 + 2.0 - 1.90)


class TestBarraDeNavegacao:
    def test_toda_rota_do_menu_aparece(self):
        barra = nav.barra("/paper")
        for rota, _, _ in nav.PAGINAS:
            assert f'href="{rota}"' in barra, f"{rota} sumiu da barra"

    def test_marca_a_pagina_atual(self):
        assert 'class="on" href="/ordens"' in nav.barra("/ordens")
        assert 'class="on"' not in nav.barra("/rota-que-nao-existe")

    def test_relatorio_de_texto_vai_escapado(self):
        """O relatorio e texto do sistema, mas embrulhar sem escapar seria
        transformar qualquer `<` num bug de renderizacao."""
        t = nav.pagina_texto("gate0", "a < b & c > d", "/gate0")
        assert "&lt;" in t and "&amp;" in t and "&gt;" in t
        assert 'href="/paper"' in t

    def test_paginas_html_trazem_a_barra(self, con):
        assert 'href="/ordens"' in index.pagina()
        assert 'href="/ordens"' in paper_dash.render(
            {"markout": {}, "motores": {}, "avaliacoes": {}})


class TestPainelDoValorReal:
    def _estado(self, led):
        return {"maker_negocio_lat15": {
            "ledger_obj": led, "ledger": led.resumo({}),
            "metricas": {"fills": 1, "compras": 1, "vendas": 0, "volume": 40.0,
                         "rebates": 0.3, "quotes": 5, "saidas_forcadas": 0,
                         "liquidacoes": 0}}}

    def test_mostra_o_valor_real_e_avisa_para_nao_somar(self, con):
        led = Ledger(capital_inicial=1000.0)
        led.aplicar("t1", "BUY", 0.40, 100, rebate=0.30)
        h = paper_dash.pagina(con, self._estado(led))
        assert "Quanto teriamos DE VERDADE" in h
        assert "Por que nao somar os seis motores" in h
        assert paper_dash.REFERENCIA in h

    def test_sem_avaliacao_nao_quebra(self):
        assert paper_dash._bloco_valor_real({}) == ""
