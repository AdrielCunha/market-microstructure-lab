"""Motor de paper trading: transforma cotação em execução simulada.

Aqui mora a honestidade do experimento inteiro. Um simulador que erra a favor
produz curva de lucro linda e some com o dinheiro depois. Por isso ele roda com
DUAS regras ao mesmo tempo, que emparedam o número de execuções:

  CRUZAMENTO — executa quando o topo de livro passa por cima da nossa cotação
      (o melhor bid cai abaixo do nosso bid). Conta DEMAIS: o livro também se
      move quando um maker simplesmente CANCELA a ordem dele, e cancelamento
      não executa ninguém. Aqui isso vira execução.

  NEGOCIO — executa apenas quando sai um negócio impresso no nosso preço ou
      melhor. Cada execução dessas é REAL: houve troca de fato. Mas conta de
      MENOS, porque o feed do Polymarket publica `last_trade_price` de forma
      esparsa — medimos ~9 prints para cada ~1.300 mudanças de preço. A maior
      parte dos negócios simplesmente não aparece.

Na prática, com o feed real, CRUZAMENTO executa muito mais que NEGOCIO (medido:
210 contra 6 em 4 minutos). Isso é consequência da esparsidade dos prints, NÃO
uma propriedade matemática das regras: com um print a cada tick, a relação se
inverteria. Não há dominância garantida entre elas — o que existe são dois
estimadores enviesados em direções opostas, e o valor real entre os dois.

ATENÇÃO ao interpretar: como as duas regras produzem quantidades de execução
muito diferentes, comparar o LUCRO ABSOLUTO entre elas não diz nada. O que se
compara é o edge POR EXECUÇÃO — e principalmente o markout, que é a medida de
seleção adversa e independe de quantas vezes fomos executados.

Nada aqui envia ordem. Não existe chave privada no projeto.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from analysis.fees import maker_rebate, taker_fee
from core.db import now_ms
from engine.ledger import Ledger
from engine.strategy import Cotacao, Estrategia, Topo

REGRAS = ("cruzamento", "negocio")


@dataclass
class EstadoToken:
    # Cotação que está VIVA no livro agora e pode ser executada.
    cotacao: Cotacao | None = None
    # Cotação decidida mas que ainda não chegou ao exchange, com o instante em
    # que ela passa a valer. É isto que modela a latência.
    pendente: tuple[int, Cotacao | None] | None = None
    ultimo_topo: Topo | None = None
    ticks_viva: int = 0


@dataclass
class Metricas:
    quotes_postadas: int = 0
    fills: int = 0
    fills_compra: int = 0
    fills_venda: int = 0
    volume: float = 0.0
    rebates: float = 0.0
    tokens_ativos: set[str] = field(default_factory=set)


class PaperEngine:
    """Uma instância por regra de execução."""

    def __init__(self, store, estrategia: Estrategia, regra: str,
                 fee_lookup: Callable[[str], tuple[float, float]] | None = None,
                 exposicao_maxima_usd: float = 5000.0,
                 capital_inicial: float = 1000.0,
                 latencia_ms: int = 230,
                 fim_lookup: Callable[[str], int | None] | None = None) -> None:
        if regra not in REGRAS:
            raise ValueError(f"regra desconhecida: {regra}")
        self.store = store
        self.estrategia = estrategia
        self.regra = regra
        self.nome = f"{estrategia.nome}_{regra}_lat{latencia_ms}"
        self.fee_lookup = fee_lookup or (lambda _t: (0.05, 0.15))
        # Quando o mercado deste token resolve, em epoch ms. É o que permite à
        # estratégia parar de abrir e sair antes do apito. Sem isso ela cota até
        # o último instante e come a resolução na cara.
        self.fim_lookup = fim_lookup or (lambda _t: None)
        self.saidas_forcadas = 0
        self.exposicao_maxima = exposicao_maxima_usd
        # Tempo entre DECIDIR uma cotação e ela estar viva no exchange.
        #
        # Sem isto o simulador modela um operador de latência ZERO — fisicamente
        # impossível, e favorável exatamente onde importa: ele recotaria sempre
        # no melhor preço do instante e nunca ficaria com ordem defasada no
        # livro. Ordem defasada sendo executada É a seleção adversa. Medido
        # nesta máquina: ~230ms de ida-e-volta até o CLOB.
        self.latencia_ms = latencia_ms
        self.estado: dict[str, EstadoToken] = {}
        self.ledger = Ledger(capital_inicial)
        self.m = Metricas()
        self.liquidacoes = 0
        # Notional das cotações de COMPRA vivas no livro, por token.
        #
        # Sem isto o capital estoura: barrar só na hora de postar não adianta,
        # porque as cotações já postadas em centenas de tokens continuam sendo
        # executadas. Observado antes da correção: capital travado de $2.556
        # sobre capital inicial de $1.000. Um market maker de verdade dimensiona
        # a cotação contra o capital total, contando o que já está exposto no
        # livro — não apenas o que já virou posição.
        self._comprometido: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Continuidade entre reinícios
    # ------------------------------------------------------------------

    def restaurar(self) -> int:
        """Reconstrói o livro-caixa relendo as execuções já gravadas.

        O ledger vive em memória; o banco guarda cada execução. Sem isto,
        reiniciar o processo zerava dias de experimento — e com deploy
        automático isso vira um push destruindo a medição.

        O corte vem de `paper_sessao`. Na primeira vez a linha não existe:
        gravamos o instante atual e NÃO reprocessamos nada. Isso é deliberado —
        `paper_fills` tem sessões antigas rodadas com bugs já corrigidos (venda
        a descoberto de graça, entre outros), e ressuscitar aquilo produziria
        uma carteira que nunca poderia ter existido.

        Devolve quantas execuções foram reaplicadas.
        """
        linha = self.store.execute(
            "SELECT desde_ts FROM paper_sessao WHERE strategy = ?",
            [self.nome]).fetchone()
        if linha is None:
            agora = now_ms()
            self.store.execute(
                "INSERT INTO paper_sessao VALUES (?, ?, ?)",
                [self.nome, agora, agora])
            return 0

        desde = int(linha[0])
        fills = self.store.execute("""
            SELECT token_id, side, price, size, rebate, taxa, regra,
                   COALESCE(agressiva, FALSE)
            FROM paper_fills
            WHERE strategy = ? AND ts_local >= ?
            ORDER BY ts_local
        """, [self.nome, desde]).fetchall()

        for token_id, side, price, size, rebate, taxa, regra, agressiva in fills:
            self.ledger.aplicar(
                token_id, side, float(price), float(size),
                rebate=float(rebate or 0.0), taxa=float(taxa or 0.0),
                motivo="resolucao" if regra == "liquidacao" else "livro")
            self.m.fills += 1
            self.m.volume += float(price) * float(size)
            self.m.rebates += float(rebate or 0.0)
            if side == "BUY":
                self.m.fills_compra += 1
            else:
                self.m.fills_venda += 1
            if regra == "liquidacao":
                self.liquidacoes += 1
            elif agressiva:
                self.saidas_forcadas += 1
        return len(fills)

    # ------------------------------------------------------------------
    # Entradas
    # ------------------------------------------------------------------

    def _capital_da_cotacao(self, token_id: str, c: Cotacao | None) -> float:
        """Capital que esta cotação consome, contando OS DOIS lados.

        A versão anterior só cobrava capital do lado da compra. Vender era de
        graça — e vender sem ter a cota é ficar VENDIDO, que no Polymarket
        significa carregar a perna contrária: quem fica vendido em YES a 26c
        precisa bancar os 74c que perde se o resultado acontecer. Isso não é
        detalhe: com a compra esbarrando no limite de caixa e a venda passando
        livre, a simulação virou uma máquina de vender a descoberto — 6.995
        cotas vendidas contra 917 compradas, uma carteira que não poderia ter
        sido montada com $1.000.

        Cobramos o lado mais caro, não a soma: uma cotação de dois lados que
        executa nos dois volta a ficar zerada.
        """
        if c is None:
            return 0.0
        pos = self.ledger.posicao(token_id)
        custo_compra = 0.0
        if c.bid is not None and pos >= 0:
            # Comprar estando comprado ou zerado aumenta a posição: gasta caixa.
            custo_compra = c.size * c.bid
        custo_venda = 0.0
        if c.ask is not None and pos <= 0:
            # Vender estando vendido ou zerado abre/aumenta o short: exige
            # bancar o que se perde se o resultado acontecer.
            custo_venda = c.size * (1.0 - c.ask)
        return max(custo_compra, custo_venda)

    def _atualizar_compromisso(self, st: EstadoToken, token_id: str) -> None:
        """Capital comprometido por este token: a cotação em trânsito, se
        houver, senão a que já está viva.

        A ordem enviada compromete capital NO ENVIO, não quando chega. Contar
        só a partir da chegada abre uma janela em que centenas de cotações
        viajam ao mesmo tempo sem consumir limite — e o capital estoura.
        Observado: $2.080 travados sobre $1.000 de capital, só com latência.
        """
        c = st.pendente[1] if st.pendente is not None else st.cotacao
        custo = self._capital_da_cotacao(token_id, c)
        if custo > 0:
            self._comprometido[token_id] = custo
        else:
            self._comprometido.pop(token_id, None)

    def _promover(self, st: EstadoToken, topo: Topo) -> None:
        """Torna viva a cotação pendente cujo tempo de trânsito já passou.

        Se ela CHEGA atravessando o livro, executa na hora como taker. A
        checagem é feita só aqui, na chegada, e nunca de novo: maker ou taker se
        decide no instante em que a ordem entra no livro, não depois. Ordem
        parada que o mercado atravessa continua sendo maker — é exatamente esse
        o mecanismo da seleção adversa, e tratá-la como taker apagaria o
        fenômeno que o projeto inteiro existe para medir.
        """
        if st.pendente is None or topo.ts_local < st.pendente[0]:
            return
        st.cotacao = st.pendente[1]
        st.pendente = None
        self._atualizar_compromisso(st, topo.token_id)
        self._executar_se_marketable(st, topo)

    def on_top(self, topo: Topo) -> None:
        st = self.estado.setdefault(topo.token_id, EstadoToken())

        # 1) A cotação decidida antes já chegou ao exchange? Se chegou
        #    atravessando o livro, executa na hora como taker.
        self._promover(st, topo)

        # 2) A cotação VIVA (possivelmente já defasada) virou execução?
        #    Esta é a ordem que importa: enquanto a nova cotação não chega, é a
        #    antiga que está exposta no livro — e é ela que é executada.
        if st.cotacao is not None and self.regra == "cruzamento":
            self._checar_atravessou(st, topo)

        # 3) Decidir a nova cotação e agendá-la para daqui a `latencia_ms`.
        posicao = self.ledger.posicao(topo.token_id)
        nova = self.estrategia.cotar(topo, posicao,
                                     self._minutos_ate_fim(topo.token_id,
                                                           topo.ts_local))
        if nova is not None and not self._dentro_do_risco(topo, nova):
            nova = None
        atual = (st.cotacao.bid, st.cotacao.ask) if st.cotacao else None
        desejada = (nova.bid, nova.ask) if nova else None
        if desejada != atual:
            st.pendente = (topo.ts_local + self.latencia_ms, nova)
            self._atualizar_compromisso(st, topo.token_id)
            if nova is not None:
                self.m.quotes_postadas += 1
            st.ticks_viva = 0
        else:
            st.ticks_viva += 1
        if nova is not None:
            self.m.tokens_ativos.add(topo.token_id)
        # Com latência zero a cotação já nasce viva. Sem isto ela só valeria na
        # próxima atualização, embutindo um atraso que não foi pedido.
        self._promover(st, topo)
        st.ultimo_topo = topo

    def _minutos_ate_fim(self, token_id: str, ts_local: int) -> float | None:
        fim = self.fim_lookup(token_id)
        return None if fim is None else (fim - ts_local) / 60_000.0

    def _executar_se_marketable(self, st: EstadoToken, topo: Topo) -> None:
        """A cotação chegou já atravessando o livro: executa como TAKER.

        É o caminho da saída forçada de estoque, que não espera contraparte —
        paga para sair. Modelar isso como execução passiva daria rebate a quem
        cruzou o spread, o contrário do que acontece, e faria a saída parecer
        de graça.

        Se o preço fugiu durante o trânsito e a ordem chega SEM atravessar, ela
        simplesmente descansa no livro e não executa. Isso é fiel: ordem limite
        agressiva que chega atrasada vira ordem parada, e a posição continua na
        mão. A estratégia recota no tick seguinte e persegue — cada perseguição
        custa mais uma latência.
        """
        c = st.cotacao
        if c is None:
            return
        if (c.bid is not None and topo.best_ask is not None
                and c.bid >= topo.best_ask):
            self._executar(topo.token_id, topo.ts_local, "BUY", topo.best_ask,
                           c.size, topo, agressiva=True)
            st.cotacao = None
            self._comprometido.pop(topo.token_id, None)
            return
        if (c.ask is not None and topo.best_bid is not None
                and c.ask <= topo.best_bid):
            self._executar(topo.token_id, topo.ts_local, "SELL", topo.best_bid,
                           c.size, topo, agressiva=True)
            st.cotacao = None
            self._comprometido.pop(topo.token_id, None)

    def on_trade(self, token_id: str, ts_local: int, price: float,
                 side: str, size: float) -> None:
        """Negócio impresso no mercado. Só a regra `negocio` reage."""
        if self.regra != "negocio":
            return
        st = self.estado.get(token_id)
        if st is None or st.cotacao is None or st.ultimo_topo is None:
            return
        c = st.cotacao
        # Um negócio de VENDA a preço <= nosso bid poderia ter sido executado
        # contra a nossa ordem, se estivéssemos na frente da fila.
        if c.bid is not None and side == "SELL" and price <= c.bid:
            self._executar(token_id, ts_local, "BUY", c.bid,
                           min(size, c.size), st.ultimo_topo)
            st.cotacao = None
        elif c.ask is not None and side == "BUY" and price >= c.ask:
            self._executar(token_id, ts_local, "SELL", c.ask,
                           min(size, c.size), st.ultimo_topo)
            st.cotacao = None

    def liquidar(self, token_id: str, preco_final: float, ts_local: int) -> None:
        """Fecha a posição ao preço de resolução do mercado ($1 ou $0).

        É o fechamento que não depende de encontrar contraparte: o jogo acabou.
        Sem isto, toda posição fica pendurada como "não realizado" para sempre.
        """
        pos = self.ledger.posicao(token_id)
        if abs(pos) < 1e-9:
            return
        side = "SELL" if pos > 0 else "BUY"
        size = abs(pos)
        # Liquidação não é execução no livro: não há spread nem rebate.
        self.ledger.aplicar(token_id, side, preco_final, size, rebate=0.0,
                            motivo="resolucao")
        self.m.fills += 1
        self.liquidacoes += 1
        st = self.estado.get(token_id)
        if st is not None:
            st.cotacao = None
            st.pendente = None
        self._comprometido.pop(token_id, None)
        self.store.add("paper_fills", (
            ts_local, self.nome, "liquidacao", token_id, side, preco_final,
            size, preco_final * size, preco_final, 0.0, 0.0,
            self.ledger.posicao(token_id), 0.0, False,
        ))

    # ------------------------------------------------------------------
    # Regras de execução
    # ------------------------------------------------------------------

    def _checar_atravessou(self, st: EstadoToken, topo: Topo) -> None:
        """Regra `cruzamento`: o topo passou por cima da nossa ordem.

        Cuidado: isto tambem dispara quando o maker da frente apenas CANCELA.
        Por isso e limite superior, nao estimativa.
        """
        c = st.cotacao
        if c is None:
            return
        if (c.bid is not None and topo.best_bid is not None
                and topo.best_bid < c.bid):
            self._executar(topo.token_id, topo.ts_local, "BUY", c.bid,
                           c.size, topo)
            st.cotacao = None
            return
        if (c.ask is not None and topo.best_ask is not None
                and topo.best_ask > c.ask):
            self._executar(topo.token_id, topo.ts_local, "SELL", c.ask,
                           c.size, topo)
            st.cotacao = None

    def _dentro_do_risco(self, topo: Topo, c: Cotacao) -> bool:
        """Duas travas: exposição por token E capital total comprometido.

        A segunda conta o que já está posicionado MAIS o que está cotado no
        livro. Sem isso a simulação abre posição com dinheiro que não tem, e o
        resultado não descreve nada executável.
        """
        preco = topo.mid or 0.5
        # Pior posição que esta cotação pode produzir, considerando cada lado.
        # A versão anterior só somava `+size`, então uma cotação que SÓ VENDE
        # para desmontar posição comprada era contada como se aumentasse a
        # exposição — e a saída podia ser barrada justamente quando é urgente.
        pos = self.ledger.posicao(topo.token_id)
        piores = [abs(pos)]
        if c.bid is not None:
            piores.append(abs(pos + c.size))
        if c.ask is not None:
            piores.append(abs(pos - c.size))
        if max(piores) * preco > self.exposicao_maxima:
            return False
        novo = self._capital_da_cotacao(topo.token_id, c)
        if novo <= 0:
            return True   # só reduz posição: devolve capital, não consome
        return self.capital_disponivel(excluir=topo.token_id) >= novo

    def capital_disponivel(self, excluir: str | None = None) -> float:
        """Caixa livre menos o que já está comprometido em cotações vivas.

        `excluir` tira do cálculo o compromisso do próprio token que está sendo
        recotado: a cotação nova SUBSTITUI a antiga, não se soma a ela. Sem
        isso, recotar o mesmo token disputa capital consigo mesmo e a estratégia
        trava sozinha depois da primeira cotação.

        ## Por que o teto no capital inicial

        Lucro de papel NÃO vira poder de fogo. Sem esse teto existe uma
        realimentação: resultado positivo aumenta o caixa livre, caixa livre
        maior deixa cotar mais tokens, mais tokens produzem mais resultado. Cada
        volta amplifica a anterior.

        Observado em produção com a regra `cruzamento`, que já conta execuções
        demais: **$35.309 de "lucro" sobre $1.000 de capital em 3 horas**, com
        12.944 execuções em 400 tokens. A trava de capital tinha parado de
        travar, e o motor cotava em todo lugar ao mesmo tempo.

        O teto é assimétrico de propósito: **prejuízo reduz o que dá para
        operar, lucro não aumenta.** É o comportamento certo para um aparelho de
        medição — queremos saber quanto de vantagem existe por dólar de capital,
        e não simular capitalização composta, que introduz realimentação e
        torna a medida instável.
        """
        comprometido = sum(v for k, v in self._comprometido.items() if k != excluir)
        livre = min(self.ledger.caixa_livre({}),
                    self.ledger.capital_inicial - self.ledger.capital_travado())
        return livre - comprometido

    def _executar(self, token_id: str, ts_local: int, side: str, price: float,
                  size: float, topo: Topo, agressiva: bool = False) -> None:
        if size <= 0:
            return
        rate, rebate_rate = self.fee_lookup(token_id)
        if agressiva:
            # Quem atravessa o spread é taker: paga taxa e não recebe rebate.
            rebate, taxa = 0.0, taker_fee(price, size, rate)
            self.saidas_forcadas += 1
        else:
            # Como maker não pagamos taxa de taker; recebemos rebate.
            rebate, taxa = maker_rebate(price, size, rate, rebate_rate), 0.0

        # A cotação virou posição: sai do compromisso e entra no capital travado.
        self._comprometido.pop(token_id, None)
        self.ledger.aplicar(token_id, side, price, size, rebate, taxa=taxa)
        self.m.fills += 1
        self.m.volume += price * size
        self.m.rebates += rebate
        if side == "BUY":
            self.m.fills_compra += 1
        else:
            self.m.fills_venda += 1

        self.store.add("paper_fills", (
            ts_local, self.nome, self.regra, token_id, side, price, size,
            price * size, topo.mid, topo.spread, rebate,
            self.ledger.posicao(token_id), taxa, agressiva,
        ))
