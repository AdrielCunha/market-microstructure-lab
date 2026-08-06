"""Testes da camada de análise: custo, coleta de topo de livro e negative-risk.

O alvo aqui é a aritmética que sustenta o veredito do Gate 0. Um erro de sinal
ou de normalização nesses cálculos não quebra nada visivelmente — só produz uma
conclusão errada com aparência de rigor.
"""

from __future__ import annotations

import duckdb
import polars as pl
import pytest

from analysis.fees import CostModel, fee_multiplier, taker_fee
from analysis.negrisk import custo_da_cesta, episodios, serie_do_evento
from collector.books import BookCollector
from core.db import SCHEMA, now_ms, Store


@pytest.fixture
def store() -> Store:
    con = duckdb.connect(":memory:")
    con.execute(SCHEMA)
    return Store(con, auto_flush=False)


class TestModeloDeCusto:
    def test_taxa_e_simetrica_em_torno_de_50c(self):
        """A curva colapsa nos extremos: quem compra a 5c e quem compra a 95c
        paga a mesma taxa POR COTA."""
        for shape in ("product", "min"):
            assert taker_fee(0.05, 1.0, 0.05, shape=shape) == \
                   pytest.approx(taker_fee(0.95, 1.0, 0.05, shape=shape))

    def test_taxa_maxima_no_meio(self):
        meio = fee_multiplier(0.5)
        assert meio > fee_multiplier(0.1)
        assert meio > fee_multiplier(0.9)

    def test_taxa_zera_nos_extremos(self):
        assert fee_multiplier(0.0) == 0.0
        assert fee_multiplier(1.0) == 0.0

    def test_pior_caso_e_o_padrao(self):
        """Sem `shape` explicito o modelo tem que devolver a leitura mais cara —
        subestimar custo e como o projeto se enganaria sozinho."""
        p = 0.5
        assert taker_fee(p, 1.0, 0.05) == pytest.approx(
            max(taker_fee(p, 1.0, 0.05, shape="product"),
                taker_fee(p, 1.0, 0.05, shape="min")))

    def test_azarao_paga_mais_caro_em_percentual_do_capital(self):
        """Em centavos por cota a taxa e simetrica, mas como fracao do capital
        aplicado ela explode nos azaroes. E o que mata comprar longshot."""
        pct_5c = taker_fee(0.05, 1.0, 0.05) / 0.05
        pct_95c = taker_fee(0.95, 1.0, 0.05) / 0.95
        assert pct_5c > 10 * pct_95c

    def test_carrego_escala_com_prazo(self):
        m = CostModel()
        assert m.carry_cost(1000, 0) == 0
        assert m.carry_cost(1000, 60) == pytest.approx(2 * m.carry_cost(1000, 30))

    def test_custo_da_cesta_nao_deixa_gas_dominar(self):
        """Gas e fixo por transacao. Normalizar por UMA cota faria $0,02 virar
        2% do payoff e mataria qualquer oportunidade no papel."""
        m = CostModel()
        pequena = custo_da_cesta(1.0, 3, 0.05, 1.0, m, cotas=1.0)
        grande = custo_da_cesta(1.0, 3, 0.05, 1.0, m, cotas=1000.0)
        assert pequena > grande
        assert grande < 0.10, "com tamanho realista o gas vira ruido"


class TestColetorDeLivro:
    def test_price_change_usa_best_bid_ask_do_payload(self, store, price_change_real):
        c = BookCollector(store, [])
        c.handle_event(price_change_real, ts_local=1785344843100)
        linhas = store._buf["book_top"]
        assert len(linhas) == 2
        # (token, ts_ex, ts_local, bid, bid_sz, ask, ask_sz, mid, spread, source)
        assert linhas[0][3] == 0.52 and linhas[0][5] == 0.53
        assert linhas[0][8] == pytest.approx(0.01), "spread = ask - bid"

    def test_tamanho_so_e_registrado_quando_o_nivel_e_o_topo(self, store,
                                                             price_change_real):
        """O payload so informa o tamanho do nivel que mudou. Se ele nao for o
        topo, o campo tem que ficar NULL em vez de mentir."""
        c = BookCollector(store, [])
        c.handle_event(price_change_real, ts_local=0)
        primeira = store._buf["book_top"][0]
        # price=0.51 nao e nem best_bid (0.52) nem best_ask (0.53)
        assert primeira[4] is None and primeira[6] is None

    def test_topo_repetido_e_descartado(self, store, price_change_real):
        c = BookCollector(store, [])
        c.handle_event(price_change_real, ts_local=1)
        c.handle_event(price_change_real, ts_local=2)
        assert c.rows_kept == 2, "so o primeiro par de tokens conta"
        assert c.rows_deduped == 2, "a repeticao e descartada"

    def test_mudanca_real_do_topo_e_gravada(self, store, price_change_real):
        import copy
        c = BookCollector(store, [])
        c.handle_event(price_change_real, ts_local=1)
        mudou = copy.deepcopy(price_change_real)
        mudou["price_changes"][0]["best_ask"] = "0.54"
        c.handle_event(mudou, ts_local=2)
        assert c.rows_kept == 3

    def test_auditoria_rest_nunca_e_descartada(self, store):
        """Os snapshots REST sao a referencia para conferir a serie do
        WebSocket — deduplicar justamente eles destruiria a verificacao."""
        c = BookCollector(store, [])
        for _ in range(3):
            c._emit_top("tok", 1, 1, 0.4, 10.0, 0.42, 10.0, "rest_audit")
        assert c.rows_kept == 3 and c.rows_deduped == 0

    def test_book_completo_calcula_o_topo(self, store, book_real):
        c = BookCollector(store, [])
        c.handle_event(book_real, ts_local=1785343564100)
        linha = store._buf["book_top"][0]
        assert linha[3] == 0.94 and linha[5] == 0.96


class TestNegativeRisk:
    def _serie(self, valores: list[float]) -> pl.DataFrame:
        return pl.DataFrame({
            "ts_local": [1000 * i for i in range(len(valores))],
            "soma_asks": valores,
            "n_pernas": [3] * len(valores),
        })

    def test_agrupa_observacoes_consecutivas_num_episodio(self):
        eps = episodios(self._serie([1.02, 0.94, 0.93, 0.95, 1.01]),
                        "soma_asks", "<", 0.99)
        assert len(eps) == 1
        assert eps["ticks"][0] == 3
        assert eps["duracao_s"][0] == pytest.approx(2.0)

    def test_episodios_separados_nao_se_fundem(self):
        eps = episodios(self._serie([0.9, 1.05, 0.9]), "soma_asks", "<", 0.99)
        assert len(eps) == 2

    def test_sem_oportunidade_devolve_vazio(self):
        eps = episodios(self._serie([1.02, 1.03, 1.01]), "soma_asks", "<", 0.99)
        assert len(eps) == 0

    def test_serie_alinha_tokens_com_forward_fill(self):
        """Cada perna atualiza em instantes diferentes; somar precos exige
        arrastar o ultimo valor conhecido de cada uma."""
        con = duckdb.connect(":memory:")
        con.execute(SCHEMA)
        # Dentro da janela de analise: `serie_do_evento` passou a limitar o
        # periodo (core/janela.py) porque varrer a tabela inteira materializava
        # o resultado em polars e estourava a memoria do container.
        t = now_ms() - 3_600_000
        con.executemany(
            "INSERT INTO book_top VALUES (?,?,?,?,?,?,?,?,?,?)",
            [("A", t, t, 0.30, 1.0, 0.32, 1.0, 0.31, 0.02, "ws"),
             ("B", t, t, 0.60, 1.0, 0.62, 1.0, 0.61, 0.02, "ws"),
             ("A", t + 1, t + 1, 0.35, 1.0, 0.37, 1.0, 0.36, 0.02, "ws")],
        )
        serie = serie_do_evento(con, ["A", "B"])
        con.close()
        assert len(serie) == 2
        # No instante 2 so A mudou; B tem que continuar valendo 0.62.
        assert float(serie["soma_asks"][1]) == pytest.approx(0.37 + 0.62)

    def test_evento_com_uma_perna_so_nao_produz_serie(self):
        """Somar um subconjunto dos resultados fabrica arbitragem que nao
        existe. Menos de duas pernas tem que devolver vazio."""
        con = duckdb.connect(":memory:")
        con.execute(SCHEMA)
        con.executemany("INSERT INTO book_top VALUES (?,?,?,?,?,?,?,?,?,?)",
                        [("A", 1, 1, 0.30, 1.0, 0.32, 1.0, 0.31, 0.02, "ws")])
        assert serie_do_evento(con, ["A"]).is_empty()
        con.close()
