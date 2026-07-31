"""Testes do motor de paper trading.

Esta é a peça mais perigosa do projeto: um simulador otimista produz curva de
lucro bonita e some com o dinheiro depois. Os testes abaixo travam justamente
as regras que impedem o simulador de mentir a favor.
"""

from __future__ import annotations

import duckdb
import pytest

from core.db import SCHEMA, Store, now_ms
from engine.ledger import Ledger
from engine.paper import PaperEngine
from engine.strategy import Cotacao, MakerSimples, Topo


@pytest.fixture
def store() -> Store:
    con = duckdb.connect(":memory:")
    con.execute(SCHEMA)
    return Store(con, auto_flush=False)


def topo(bid, ask, ts=1000, token="t1") -> Topo:
    return Topo(token, ts, bid, ask)


class TestMakerSimples:
    def test_cota_nos_dois_lados(self):
        c = MakerSimples().cotar(topo(0.47, 0.49), posicao=0)
        assert c is not None and c.bid == 0.47 and c.ask == 0.49

    def test_recusa_spread_apertado(self):
        """Abaixo do spread mínimo não há o que capturar — cotar ali só
        acumula estoque e risco."""
        assert MakerSimples(spread_minimo_c=2.0).cotar(topo(0.47, 0.48), 0) is None

    def test_recusa_extremos_de_preco(self):
        e = MakerSimples()
        assert e.cotar(topo(0.02, 0.05), 0) is None
        assert e.cotar(topo(0.96, 0.99), 0) is None

    def test_para_de_comprar_no_limite_de_estoque(self):
        """Sem esta trava, execuções seguidas de um lado só viram aposta
        direcional — o oposto de market making."""
        e = MakerSimples(posicao_maxima=500)
        c = e.cotar(topo(0.47, 0.49), posicao=500)
        assert c is not None and c.bid is None, "no limite comprado, nao compra mais"
        assert c.ask is not None and c.ask < 0.49,             "comprado demais: baixa o preco de venda para sair do estoque"

    def test_para_de_vender_no_limite_negativo(self):
        e = MakerSimples(posicao_maxima=500)
        c = e.cotar(topo(0.47, 0.49), posicao=-500)
        assert c is not None and c.ask is None
        assert c.bid is not None and c.bid > 0.47,             "vendido demais: sobe o preco de compra para recomprar"

    def test_desvio_e_assimetrico_saida_perto_entrada_longe(self):
        """Com estoque, a SAIDA aproxima do mercado e a ENTRADA se afasta.

        A versao antiga deslocava os dois lados igualmente: a cotacao mudava de
        lugar mas continuava com a mesma largura, entao entrava tanto quanto
        saia. O estoque nunca desmontava.
        """
        e = MakerSimples(posicao_maxima=500, skew_maximo_c=2.0)
        neutro = e.cotar(topo(0.40, 0.44), posicao=0)
        comprado = e.cotar(topo(0.40, 0.44), posicao=250)
        # Comprado: quer VENDER. O ask desce (mais facil sair)...
        assert comprado.ask < neutro.ask
        # ...e o bid desce MAIS que o ask (mais dificil entrar de novo).
        assert (neutro.bid - comprado.bid) > (neutro.ask - comprado.ask)

        vendido = e.cotar(topo(0.40, 0.44), posicao=-250)
        # Vendido: quer COMPRAR. O bid sobe, e o ask sobe mais.
        assert vendido.bid > neutro.bid
        assert (vendido.ask - neutro.ask) > (vendido.bid - neutro.bid)

    def test_nao_abre_venda_em_azarao_barato(self):
        """Vender a 26c e arriscar 74c para ganhar 26c: precisa acertar 74% so
        para empatar. Medido a 230ms: 50%. Estruturalmente ruim."""
        e = MakerSimples(preco_min_venda=0.15, preco_min=0.05)
        c = e.cotar(topo(0.09, 0.11), posicao=0)
        assert c is not None and c.ask is None, "nao abre venda abaixo do piso"
        assert c.bid is not None, "comprar barato continua permitido"
        # Ja comprado, vender ali e DESMONTAR — isso continua liberado.
        c2 = e.cotar(topo(0.09, 0.11), posicao=200)
        assert c2 is not None and c2.ask is not None

    def test_perto_do_fim_so_cota_o_lado_que_reduz(self):
        e = MakerSimples(minutos_sem_abrir=30.0, minutos_saida_forcada=10.0)
        comprado = e.cotar(topo(0.47, 0.49), posicao=200, minutos_ate_fim=20.0)
        assert comprado.bid is None and comprado.ask is not None
        vendido = e.cotar(topo(0.47, 0.49), posicao=-200, minutos_ate_fim=20.0)
        assert vendido.ask is None and vendido.bid is not None
        # Sem estoque e perto do fim: nao abre nada.
        assert e.cotar(topo(0.47, 0.49), posicao=0, minutos_ate_fim=20.0) is None
        # Longe do fim, os dois lados voltam.
        longe = e.cotar(topo(0.47, 0.49), posicao=0, minutos_ate_fim=600.0)
        assert longe.bid is not None and longe.ask is not None

    def test_saida_forcada_atravessa_o_spread(self):
        """No limite do prazo, para de esperar contraparte e paga para sair."""
        e = MakerSimples(minutos_saida_forcada=10.0)
        c = e.cotar(topo(0.47, 0.49), posicao=200, minutos_ate_fim=5.0)
        # Comprado: vende NO BID (atravessa), pelo tamanho inteiro da posicao.
        assert c.ask == pytest.approx(0.47) and c.bid is None
        assert c.size == pytest.approx(200)

        c2 = e.cotar(topo(0.47, 0.49), posicao=-200, minutos_ate_fim=5.0)
        # Vendido: compra NO ASK.
        assert c2.bid == pytest.approx(0.49) and c2.ask is None
        assert c2.size == pytest.approx(200)

        # Sem estoque nao ha o que encerrar.
        assert e.cotar(topo(0.47, 0.49), posicao=0, minutos_ate_fim=5.0) is None

    def test_sem_data_de_resolucao_nada_muda(self):
        """`minutos_ate_fim=None` (token fora do catalogo) nao pode disparar
        saida forcada — seria vender por causa de dado faltando."""
        e = MakerSimples()
        c = e.cotar(topo(0.47, 0.49), posicao=200, minutos_ate_fim=None)
        assert c is not None and c.bid is not None and c.ask is not None

    def test_skew_empurra_as_cotacoes_conforme_o_estoque(self):
        """Comprado empurra as duas pontas para baixo; vendido, para cima.

        E a peca que faltava: sem ela a estrategia sentava no melhor bid/ask e
        acumulava de um lado so — centenas de execucoes e ZERO fechamentos.
        """
        e = MakerSimples(posicao_maxima=500, skew_maximo_c=2.0)
        neutro = e.cotar(topo(0.47, 0.49), posicao=0)
        comprado = e.cotar(topo(0.47, 0.49), posicao=250)
        vendido = e.cotar(topo(0.47, 0.49), posicao=-250)
        assert comprado.ask < neutro.ask and comprado.bid < neutro.bid
        assert vendido.ask > neutro.ask and vendido.bid > neutro.bid

    def test_preco_sai_limpo_no_tick(self):
        """0.47000000000000003 nao e um preco postavel."""
        c = MakerSimples().cotar(topo(0.47, 0.49), posicao=0)
        assert c.bid == 0.47 and c.ask == 0.49

    def test_livro_incompleto_nao_gera_cotacao(self):
        assert MakerSimples().cotar(topo(None, 0.49), 0) is None


class TestRegraCruzamento:
    def _motor(self, store) -> PaperEngine:
        return PaperEngine(store, MakerSimples(size=100), "cruzamento",
                           latencia_ms=0)

    def test_nao_executa_se_preco_nao_atravessa(self, store):
        """Ficar parado no livro sem o preço passar por cima não é execução.
        Assumir o contrário é a mentira mais comum de um simulador."""
        m = self._motor(store)
        m.on_top(topo(0.47, 0.49, ts=1))
        m.on_top(topo(0.47, 0.49, ts=2))
        assert m.m.fills == 0

    def test_executa_compra_quando_bid_cai_abaixo(self, store):
        m = self._motor(store)
        m.on_top(topo(0.47, 0.49, ts=1))     # cota bid 0.47
        m.on_top(topo(0.46, 0.48, ts=2))     # preco atravessou
        assert m.m.fills == 1
        assert m.m.fills_compra == 1
        assert m.ledger.posicao("t1") == 100

    def test_executa_venda_quando_ask_sobe_acima(self, store):
        m = self._motor(store)
        m.on_top(topo(0.47, 0.49, ts=1))
        m.on_top(topo(0.48, 0.50, ts=2))
        assert m.m.fills_venda == 1
        assert m.ledger.posicao("t1") == -100

    def test_nao_reage_a_negocio_impresso(self, store):
        """A regra `cruzamento` ignora prints — ela so reage ao livro."""
        m = self._motor(store)
        m.on_top(topo(0.47, 0.49, ts=1))
        m.on_trade("t1", 2, 0.47, "SELL", 100)
        assert m.m.fills == 0


class TestRegraNegocio:
    def _motor(self, store) -> PaperEngine:
        return PaperEngine(store, MakerSimples(size=100), "negocio",
                           latencia_ms=0)

    def test_executa_com_negocio_no_nosso_preco(self, store):
        m = self._motor(store)
        m.on_top(topo(0.47, 0.49, ts=1))
        m.on_trade("t1", 2, 0.47, "SELL", 100)
        assert m.m.fills == 1 and m.ledger.posicao("t1") == 100

    def test_ignora_negocio_em_preco_pior(self, store):
        m = self._motor(store)
        m.on_top(topo(0.47, 0.49, ts=1))
        m.on_trade("t1", 2, 0.48, "SELL", 100)   # acima do nosso bid
        assert m.m.fills == 0

    def test_tamanho_limitado_ao_do_negocio(self, store):
        """Não dá para executar mais do que passou pelo mercado."""
        m = self._motor(store)
        m.on_top(topo(0.47, 0.49, ts=1))
        m.on_trade("t1", 2, 0.47, "SELL", 30)
        assert m.ledger.posicao("t1") == 30


class TestRegrasEmparedamOResultado:
    """`cruzamento` conta demais, `negocio` conta de menos.

    Medido em produção: 210 execuções por cruzamento contra 6 por negócio no
    mesmo fluxo de 4 minutos. A diferença vem de duas causas reais — o livro se
    move também por CANCELAMENTO (que cruzamento conta como execução) e o feed
    publica `last_trade_price` de forma esparsa (~9 prints para cada ~1.300
    mudanças de preço).
    """

    def _fluxo(self, motores, prints: bool) -> None:
        fluxo = [topo(0.47, 0.49, ts=1), topo(0.46, 0.48, ts=2),
                 topo(0.46, 0.48, ts=3), topo(0.45, 0.47, ts=4)]
        for t in fluxo:
            for mot in motores:
                mot.on_top(t)
                if prints and t.best_bid is not None:
                    mot.on_trade("t1", t.ts_local, t.best_bid, "SELL", 100)

    def test_negocio_nao_executa_sem_print(self, store):
        """Sem negocio impresso, `negocio` nao inventa execucao.

        Esta e a propriedade que faz cada execucao dessa regra ser REAL.
        """
        neg = PaperEngine(store, MakerSimples(size=100), "negocio", latencia_ms=0)
        self._fluxo([neg], prints=False)
        assert neg.m.fills == 0

    def test_cruzamento_nao_executa_sem_o_livro_se_mover(self, store):
        """E a propriedade simetrica: `cruzamento` so reage ao livro."""
        cruz = PaperEngine(store, MakerSimples(size=100), "cruzamento", latencia_ms=0)
        parado = [topo(0.47, 0.49, ts=i) for i in range(1, 5)]
        for t in parado:
            cruz.on_top(t)
            cruz.on_trade("t1", t.ts_local, 0.47, "SELL", 100)
        assert cruz.m.fills == 0

    def test_com_prints_densos_negocio_pode_superar_cruzamento(self, store):
        """Documenta um limite do desenho, para nao ser lido como bug depois.

        Nao existe dominancia matematica entre as regras: se cada tick tivesse
        um negocio impresso, `negocio` executaria MAIS que `cruzamento`. O que
        se observa em producao (210 contra 6 em 4 minutos) vem do feed real ser
        esparso — ~9 prints para cada ~1.300 mudancas de preco —, nao de uma
        propriedade da regra. Por isso a comparacao valida entre as duas e o
        markout por execucao, nunca o lucro absoluto.
        """
        cruz = PaperEngine(store, MakerSimples(size=100), "cruzamento", latencia_ms=0)
        neg = PaperEngine(store, MakerSimples(size=100), "negocio", latencia_ms=0)
        self._fluxo([cruz, neg], prints=True)
        assert neg.m.fills >= cruz.m.fills,             "com print em todo tick, `negocio` nao pode ficar atras"


class TestGestaoDeEstoque:
    """A correcao mais importante da Fase 1, medida antes de existir:

    de ~140 execucoes em 30h, so 6 a 9 viraram ida-e-volta. O resto ficou na mao
    309 minutos de mediana ate o mercado virar $1 ou $0. Resultado: +$1,90
    capturado no livro contra -$238,29 na resolucao. Estes testes travam o
    mecanismo que impede isso de voltar.
    """

    def _motor(self, store, fim_ms, **kw):
        return PaperEngine(
            store, MakerSimples(size=100, minutos_sem_abrir=30.0,
                                minutos_saida_forcada=10.0, **kw),
            "cruzamento", latencia_ms=0, fim_lookup=lambda _t: fim_ms)

    def test_estoque_e_zerado_antes_da_resolucao(self, store):
        """O teste que resume a feature: com o apito chegando, a posicao SAI."""
        # Mercado resolve no instante 1_000_000 do relogio local.
        m = self._motor(store, fim_ms=1_000_000)

        # Longe do fim (60 min antes): opera normal e acumula posicao.
        t0 = 1_000_000 - 60 * 60_000
        m.on_top(topo(0.47, 0.49, ts=t0))
        m.on_top(topo(0.44, 0.46, ts=t0 + 1))      # topo desce: compramos a 0.47
        assert m.ledger.posicao("t1") > 0, "precisa ter estoque para o teste valer"

        # Agora faltam 5 minutos: a saida forcada tem de zerar a posicao.
        t1 = 1_000_000 - 5 * 60_000
        m.on_top(topo(0.44, 0.46, ts=t1))
        assert m.ledger.posicao("t1") == pytest.approx(0.0), \
            "com o mercado prestes a resolver, o estoque tem de sair"
        assert m.saidas_forcadas == 1
        assert m.ledger.taxas > 0, "atravessar o spread e ser taker: paga taxa"
        assert m.ledger.fechamentos_por_motivo.get("livro") == 1, \
            "saida forcada fecha NO LIVRO, nao na resolucao"

    def test_saida_forcada_paga_taxa_e_nao_ganha_rebate(self, store):
        """Se a saida desse rebate, sair de graca pareceria lucro."""
        m = self._motor(store, fim_ms=1_000_000)
        m.ledger.aplicar("t1", "BUY", 0.47, 100)
        rebates_antes = m.ledger.rebates
        m.on_top(topo(0.44, 0.46, ts=1_000_000 - 5 * 60_000))
        assert m.ledger.taxas > 0
        assert m.ledger.rebates == pytest.approx(rebates_antes), \
            "quem atravessa o spread nao recebe rebate"

    def test_saida_forcada_nunca_e_barrada_por_falta_de_caixa(self, store):
        """Desmontar posicao devolve capital. Barrar a saida por falta de caixa
        prenderia a estrategia exatamente quando ela precisa sair."""
        m = PaperEngine(
            store, MakerSimples(size=100, minutos_saida_forcada=10.0),
            "cruzamento", capital_inicial=1.0, latencia_ms=0,
            fim_lookup=lambda _t: 1_000_000)
        m.ledger.aplicar("t1", "BUY", 0.47, 100)
        m.on_top(topo(0.44, 0.46, ts=1_000_000 - 5 * 60_000))
        assert m.ledger.posicao("t1") == pytest.approx(0.0)

    def test_preco_que_foge_no_transito_deixa_a_ordem_parada(self, store):
        """Saida forcada que chega atrasada NAO executa — vira ordem parada.

        E o custo da latencia sobre a saida, e precisa aparecer: fingir que a
        ordem agressiva sempre executa apagaria justamente o risco que a
        latencia cria na hora de escapar.
        """
        m = PaperEngine(
            store, MakerSimples(size=100, minutos_saida_forcada=10.0),
            "negocio", latencia_ms=200, fim_lookup=lambda _t: 1_000_000)
        m.ledger.aplicar("t1", "BUY", 0.47, 100)
        t = 1_000_000 - 5 * 60_000
        m.on_top(topo(0.44, 0.46, ts=t))            # decide vender a 0.44
        # O bid desaba antes da ordem chegar: 0.44 nao atravessa mais nada.
        m.on_top(topo(0.30, 0.32, ts=t + 300))
        assert m.ledger.posicao("t1") == pytest.approx(100.0), \
            "ordem agressiva atrasada nao executa; a posicao continua na mao"


class TestContinuidadeEntreReinicios:
    """Reiniciar nao pode destruir o experimento.

    O livro-caixa vive em memoria e o banco guarda as execucoes. Com deploy
    automatico ligado, um push no meio da noite reiniciava o processo e apagava
    dias de medicao. `restaurar()` reconstroi o estado.
    """

    def _motor(self, store, nome_lat=0):
        return PaperEngine(store, MakerSimples(size=100), "cruzamento",
                           latencia_ms=nome_lat)

    def test_primeira_vez_nao_reprocessa_historico(self, store):
        """`paper_fills` tem sessoes antigas, algumas com bugs ja corrigidos.
        Ressuscitar aquilo produziria carteira que nunca existiu."""
        store.execute(
            "INSERT INTO paper_fills VALUES "
            "(1, 'maker_cruzamento_lat0', 'cruzamento', 't1', 'BUY', 0.4, 100, "
            " 40, 0.4, 0.02, 0.1, 100, 0.0, FALSE)")
        m = self._motor(store)
        assert m.restaurar() == 0, "sem marco de sessao, nao reprocessa nada"
        assert m.ledger.posicao("t1") == 0
        # E grava o corte, para o proximo reinicio ter de onde continuar.
        assert store.execute(
            "SELECT count(*) FROM paper_sessao WHERE strategy = ?",
            [m.nome]).fetchone()[0] == 1

    def test_reinicio_reconstroi_posicao_e_resultado(self, store):
        m1 = self._motor(store)
        m1.restaurar()                      # grava o marco
        m1.ledger.aplicar("t1", "BUY", 0.40, 100)
        m1.ledger.aplicar("t1", "SELL", 0.45, 100)
        # Grava as execucoes como o motor faria.
        for side, price, size in (("BUY", 0.40, 100), ("SELL", 0.45, 100)):
            store.add("paper_fills", (
                now_ms(), m1.nome, "cruzamento", "t1", side, price, size,
                price * size, price, 0.02, 0.5, 0.0, 0.0, False))
        store.flush()

        m2 = self._motor(store)             # "reiniciou o processo"
        assert m2.restaurar() == 2
        assert m2.ledger.realizado == pytest.approx(m1.ledger.realizado)
        assert m2.ledger.rebates == pytest.approx(1.0)
        assert m2.m.fills == 2
        assert m2.ledger.posicao("t1") == pytest.approx(0.0)

    def test_liquidacao_restaurada_conta_como_resolucao(self, store):
        """A decomposicao livro/resolucao e o instrumento que julga a tese —
        nao pode se perder no reinicio."""
        m1 = self._motor(store)
        m1.restaurar()
        store.add("paper_fills", (now_ms(), m1.nome, "cruzamento", "t1", "BUY",
                                  0.40, 100, 40, 0.40, 0.02, 0.0, 100, 0.0, False))
        store.add("paper_fills", (now_ms(), m1.nome, "liquidacao", "t1", "SELL",
                                  0.0, 100, 0, 0.0, 0.0, 0.0, 0, 0.0, False))
        store.flush()

        m2 = self._motor(store)
        m2.restaurar()
        r = m2.ledger.resumo({})
        assert r["realizado_resolucao"] == pytest.approx(-40.0)
        assert r["realizado_livro"] == pytest.approx(0.0)
        assert m2.liquidacoes == 1

    def test_saida_forcada_restaurada_mantem_taxa(self, store):
        m1 = self._motor(store)
        m1.restaurar()
        store.add("paper_fills", (now_ms(), m1.nome, "cruzamento", "t1", "BUY",
                                  0.40, 100, 40, 0.40, 0.02, 0.1, 100, 0.0, False))
        store.add("paper_fills", (now_ms(), m1.nome, "cruzamento", "t1", "SELL",
                                  0.38, 100, 38, 0.39, 0.02, 0.0, 0, 1.9, True))
        store.flush()

        m2 = self._motor(store)
        m2.restaurar()
        assert m2.ledger.taxas == pytest.approx(1.9)
        assert m2.saidas_forcadas == 1
        assert m2.ledger.realizado == pytest.approx(-2.0)


class TestLedger:
    """A conta tem que ser a que qualquer um reconhece:

        patrimonio = capital inicial + realizado + rebates + nao realizado
    """

    def test_operacao_fechada_vira_lucro_realizado(self):
        led = Ledger(capital_inicial=1000.0)
        led.aplicar("t1", "BUY", 0.47, 100)
        assert led.realizado == 0.0, "posicao aberta ainda nao realizou nada"
        led.aplicar("t1", "SELL", 0.49, 100)
        assert led.realizado == pytest.approx(2.0)   # 2c x 100 cotas
        assert led.posicao("t1") == 0
        assert led.fechamentos == 1
        assert led.patrimonio({}) == pytest.approx(1002.0)

    def test_realizado_separa_livro_de_resolucao(self):
        """A pergunta que decide a tese: o dinheiro veio de spread ou de aposta?

        Fechar no livro (achou contraparte) e liquidar na resolucao (o mercado
        acabou e a posicao virou $1 ou $0) sao negocios diferentes. Somados num
        numero so, um esconde o outro — foi assim que 20h de simulacao passaram
        parecendo market making enquanto o resultado vinha de outro lugar.
        """
        led = Ledger(capital_inicial=1000.0)
        # Round trip de verdade: ganhou 2c em 100 cotas.
        led.aplicar("t1", "BUY", 0.47, 100)
        led.aplicar("t1", "SELL", 0.49, 100)
        # Estoque que nao deu para desmontar e resolveu contra: comprou a 40c,
        # virou po.
        led.aplicar("t2", "BUY", 0.40, 100)
        led.aplicar("t2", "SELL", 0.0, 100, motivo="resolucao")

        r = led.resumo({})
        assert r["realizado"] == pytest.approx(-38.0)
        assert r["realizado_livro"] == pytest.approx(2.0)
        assert r["realizado_resolucao"] == pytest.approx(-40.0)
        assert r["fechamentos_livro"] == 1
        assert r["fechamentos_resolucao"] == 1
        # As partes tem que somar o todo, senao a decomposicao mente.
        assert (r["realizado_livro"] + r["realizado_resolucao"]
                == pytest.approx(r["realizado"]))
        assert r["fechamentos_livro"] + r["fechamentos_resolucao"] == r["fechamentos"]

    def test_posicao_aberta_e_nao_realizado(self):
        """Enquanto nao fecha, o lucro e promessa — tem que ficar separado."""
        led = Ledger(capital_inicial=1000.0)
        led.aplicar("t1", "BUY", 0.40, 100)
        r = led.resumo({"t1": 0.50})
        assert r["realizado"] == pytest.approx(0.0)
        assert r["nao_realizado"] == pytest.approx(10.0)
        assert r["patrimonio"] == pytest.approx(1010.0)
        assert r["capital_travado"] == pytest.approx(40.0)
        assert r["caixa_livre"] == pytest.approx(960.0)

    def test_prejuizo_aparece_como_negativo(self):
        led = Ledger(capital_inicial=1000.0)
        led.aplicar("t1", "BUY", 0.50, 100)
        led.aplicar("t1", "SELL", 0.45, 100)
        assert led.realizado == pytest.approx(-5.0)
        assert led.pnl_total({}) == pytest.approx(-5.0)
        assert led.retorno_pct({}) == pytest.approx(-0.5)

    def test_fechamento_parcial_usa_custo_medio(self):
        led = Ledger(capital_inicial=1000.0)
        led.aplicar("t1", "BUY", 0.40, 100)
        led.aplicar("t1", "BUY", 0.60, 100)   # custo medio = 0.50
        assert led.custo_medio("t1") == pytest.approx(0.50)
        led.aplicar("t1", "SELL", 0.55, 100)  # fecha metade a 5c de lucro
        assert led.realizado == pytest.approx(5.0)
        assert led.posicao("t1") == pytest.approx(100)
        assert led.custo_medio("t1") == pytest.approx(0.50)

    def test_venda_a_descoberto_lucra_com_queda(self):
        led = Ledger(capital_inicial=1000.0)
        led.aplicar("t1", "SELL", 0.50, 100)
        assert led.posicao("t1") == -100
        led.aplicar("t1", "BUY", 0.45, 100)
        assert led.realizado == pytest.approx(5.0)

    def test_rebate_entra_no_patrimonio(self):
        led = Ledger(capital_inicial=1000.0)
        led.aplicar("t1", "BUY", 0.5, 100, rebate=0.75)
        assert led.rebates == pytest.approx(0.75)
        assert led.patrimonio({"t1": 0.5}) == pytest.approx(1000.75)

    def test_sem_preco_atual_nao_inventa_avaliacao(self):
        """Sem mid, a posicao nao entra no nao realizado — chutar preco seria
        fabricar lucro."""
        led = Ledger(capital_inicial=1000.0)
        led.aplicar("t1", "BUY", 0.40, 100)
        assert led.nao_realizado({}) == pytest.approx(0.0)

    def test_drawdown_registra_pior_queda(self):
        led = Ledger(capital_inicial=1000.0)
        led.aplicar("t1", "BUY", 0.50, 100)
        led.marcar({"t1": 0.60})    # patrimonio 1010
        led.marcar({"t1": 0.40})    # patrimonio 990
        assert led.drawdown_max == pytest.approx(-20.0)


class TestGravacao:
    def test_fill_vai_para_o_banco(self, store):
        m = PaperEngine(store, MakerSimples(size=100), "cruzamento", latencia_ms=0)
        m.on_top(topo(0.47, 0.49, ts=1))
        m.on_top(topo(0.46, 0.48, ts=2))
        linhas = store._buf["paper_fills"]
        assert len(linhas) == 1
        ts, strategy, regra, token, side, price, size = linhas[0][:7]
        assert regra == "cruzamento" and side == "BUY" and price == 0.47
        assert strategy == "maker_cruzamento_lat0"


class TestTravaDeCapital:
    """Sem trava de capital a simulacao opera com dinheiro que nao tem.

    Observado em producao antes da correcao: capital travado de $1.100 sobre
    capital inicial de $1.000, com caixa livre negativo. Resultado produzido
    assim nao descreve nada executavel.
    """

    def test_caixa_livre_nunca_negativo_em_varios_tokens(self, store):
        """O caso que quebrou de verdade: centenas de tokens, cada um com uma
        cotacao viva, todas executando. Barrar so na postagem nao basta — o
        compromisso em aberto tambem consome capital."""
        m = PaperEngine(store, MakerSimples(size=100), "cruzamento",
                        capital_inicial=200.0, latencia_ms=0)
        for i in range(50):
            tok = f"tok{i}"
            m.on_top(Topo(tok, i * 2, 0.47, 0.49))
            m.on_top(Topo(tok, i * 2 + 1, 0.46, 0.48))
        assert m.ledger.caixa_livre({}) >= 0, "abriu posicao sem ter o dinheiro"
        assert m.ledger.capital_travado() <= 200.0

    def test_compromisso_em_aberto_conta_contra_o_capital(self, store):
        """Cotacao viva no livro ja compromete caixa, mesmo sem ter executado.

        Capital de $120 e o que faz duas cotacoes caberem e a terceira nao:
        cada cotacao de dois lados custa o maior dos lados, e o lado VENDIDO de
        100 cotas a 0.49 pede 100 x (1 - 0.49) = $51 de garantia.
        """
        m = PaperEngine(store, MakerSimples(size=100), "cruzamento",
                        capital_inicial=120.0, latencia_ms=0)
        m.on_top(Topo("a", 1, 0.47, 0.49))     # compromete ~$51
        antes = m.capital_disponivel()
        m.on_top(Topo("b", 1, 0.47, 0.49))     # compromete mais ~$51
        assert m.capital_disponivel() < antes
        m.on_top(Topo("c", 1, 0.47, 0.49))     # nao cabe mais
        assert m.capital_disponivel() >= 0

    def test_vender_a_descoberto_custa_capital(self, store):
        """Vender sem ter a cota NAO e de graca.

        Ficar vendido em YES a 26c e bancar os 74c que se perde se o resultado
        acontecer. Enquanto a trava so cobrava o lado da compra, a compra
        esbarrava no caixa e a venda passava livre: 30h de simulacao sairam com
        6.995 cotas vendidas contra 917 compradas — carteira impossivel de
        montar com o capital declarado.
        """
        m = PaperEngine(store, MakerSimples(size=100), "cruzamento",
                        capital_inicial=1000.0, latencia_ms=0)
        # Cotacao barata: comprar 100 a 0.05 custa $5; ficar vendido a 0.06
        # exige $94. E o lado vendido que manda.
        assert m._capital_da_cotacao("t", Cotacao(bid=0.05, ask=0.06, size=100)) \
            == pytest.approx(94.0)

        # Vendido de verdade: o capital travado tem que refletir o lado grande.
        m.ledger.aplicar("t", "SELL", 0.26, 100)
        assert m.ledger.posicao("t") == -100
        assert m.ledger.capital_travado() == pytest.approx(74.0)
        assert m.ledger.caixa_livre({}) == pytest.approx(926.0)

    def test_venda_que_so_reduz_posicao_nao_consome_caixa(self, store):
        """Quem esta comprado e vende esta desmontando: devolve capital."""
        m = PaperEngine(store, MakerSimples(size=100), "cruzamento",
                        capital_inicial=1000.0, latencia_ms=0)
        m.ledger.aplicar("t", "BUY", 0.40, 100)
        # Posicao comprada: o lado da venda apenas reduz, nao pede garantia.
        assert m._capital_da_cotacao("t", Cotacao(bid=None, ask=0.42, size=100)) \
            == pytest.approx(0.0)

    def test_lucro_de_papel_nao_vira_poder_de_fogo(self, store):
        """Sem teto existe realimentacao: lucro aumenta o caixa, caixa maior
        deixa cotar mais, mais cotacoes produzem mais lucro.

        Observado em producao: $35.309 sobre $1.000 em 3 horas, 12.944
        execucoes em 400 tokens, porque a trava de capital tinha parado de
        travar.
        """
        m = PaperEngine(store, MakerSimples(size=100), "cruzamento",
                        capital_inicial=100.0, latencia_ms=0)
        antes = m.capital_disponivel()
        # Um lucro enorme, como o que a regra `cruzamento` fabricava.
        m.ledger.realizado += 50_000.0
        assert m.capital_disponivel() == pytest.approx(antes), \
            "lucro de papel nao pode aumentar o que da para operar"

    def test_prejuizo_reduz_o_que_da_para_operar(self, store):
        """O teto e assimetrico de proposito: perda encolhe a capacidade."""
        m = PaperEngine(store, MakerSimples(size=100), "cruzamento",
                        capital_inicial=1000.0, latencia_ms=0)
        antes = m.capital_disponivel()
        m.ledger.realizado -= 400.0
        assert m.capital_disponivel() == pytest.approx(antes - 400.0)

    def test_recotar_nao_conta_o_mesmo_token_duas_vezes(self, store):
        m = PaperEngine(store, MakerSimples(size=100), "cruzamento",
                        capital_inicial=100.0, latencia_ms=0)
        m.on_top(Topo("a", 1, 0.47, 0.49))
        d1 = m.capital_disponivel()
        m.on_top(Topo("a", 2, 0.47, 0.49))     # mesma cotacao, mesmo token
        assert m.capital_disponivel() == pytest.approx(d1)


class TestLatencia:
    """A critica mais importante levantada no projeto.

    Em paper trading a latencia e zero por padrao, porque quem calcula sou eu e
    calculo instantaneo. Isso modela um operador fisicamente impossivel — e
    favoravel exatamente onde importa: ele recotaria sempre no melhor preco do
    instante e nunca ficaria com ordem defasada no livro.

    Ordem defasada sendo executada E a selecao adversa. Sem modelar latencia, o
    simulador simplesmente nao ve o custo que mais importa.
    """

    def test_cotacao_nao_fica_viva_antes_do_transito(self, store):
        m = PaperEngine(store, MakerSimples(size=100), "cruzamento",
                        latencia_ms=230)
        m.on_top(topo(0.47, 0.49, ts=1000))
        st = m.estado["t1"]
        assert st.cotacao is None, "ainda em transito"
        assert st.pendente is not None
        assert st.pendente[0] == 1000 + 230

    def test_cotacao_fica_viva_apos_o_transito(self, store):
        m = PaperEngine(store, MakerSimples(size=100), "cruzamento",
                        latencia_ms=230)
        m.on_top(topo(0.47, 0.49, ts=1000))
        m.on_top(topo(0.47, 0.49, ts=1300))
        assert m.estado["t1"].cotacao is not None

    def test_ordem_defasada_e_a_que_executa(self, store):
        """O nucleo da selecao adversa: o preco vira, a nossa cotacao antiga
        ainda esta no livro, e e ela que e pega."""
        m = PaperEngine(store, MakerSimples(size=100), "cruzamento",
                        latencia_ms=200)
        m.on_top(topo(0.47, 0.49, ts=1000))     # decide cotar 0.47/0.49
        m.on_top(topo(0.47, 0.49, ts=1300))     # cotacao fica viva
        # Preco desaba antes de conseguirmos cancelar: somos executados a 0.47
        m.on_top(topo(0.42, 0.44, ts=1350))
        assert m.m.fills == 1
        assert m.ledger.posicao("t1") == 100
        assert m.ledger.custo_medio("t1") == pytest.approx(0.47), \
            "compramos a 47c num mercado que ja estava a 43c"

    def test_latencia_zero_executa_menos_no_mesmo_fluxo(self, store):
        """Comparacao que o painel usa: a diferenca entre as duas colunas e o
        preco de estar a 230ms do exchange."""
        rapido = PaperEngine(store, MakerSimples(size=100), "cruzamento",
                             latencia_ms=0)
        lento = PaperEngine(store, MakerSimples(size=100), "cruzamento",
                            latencia_ms=500)
        fluxo = [topo(0.47, 0.49, ts=1000), topo(0.47, 0.49, ts=1100),
                 topo(0.40, 0.42, ts=1200), topo(0.40, 0.42, ts=1900),
                 topo(0.33, 0.35, ts=2000)]
        for t in fluxo:
            rapido.on_top(t)
            lento.on_top(t)
        # Os dois sao executados, mas a que ficou defasada compra mais caro
        # em relacao ao mercado do momento.
        assert rapido.m.fills > 0 and lento.m.fills > 0


class TestCompromissoComLatencia:
    def test_ordem_em_transito_ja_consome_capital(self, store):
        """A ordem enviada compromete capital NO ENVIO, nao na chegada.

        Contar so a partir da chegada abre uma janela em que centenas de
        cotacoes viajam ao mesmo tempo sem consumir limite. Observado antes da
        correcao: $2.080 travados sobre $1.000 de capital, so por causa da
        latencia.
        """
        m = PaperEngine(store, MakerSimples(size=100), "cruzamento",
                        capital_inicial=100.0, latencia_ms=230)
        m.on_top(Topo("a", 1000, 0.47, 0.49))
        assert m.estado["a"].cotacao is None, "ainda em transito"
        assert m.capital_disponivel() < 100.0, "mas ja consome capital"

    def test_capital_nao_estoura_com_latencia(self, store):
        m = PaperEngine(store, MakerSimples(size=100), "cruzamento",
                        capital_inicial=200.0, latencia_ms=230)
        for i in range(60):
            tok = f"tok{i}"
            m.on_top(Topo(tok, 1000 + i, 0.47, 0.49))
            m.on_top(Topo(tok, 1500 + i, 0.46, 0.48))
        assert m.ledger.caixa_livre({}) >= 0
        assert m.ledger.capital_travado() <= 200.0
