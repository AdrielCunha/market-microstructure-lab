"""Interface de estratégia e a estratégia de market making usada na Fase 1.

Uma estratégia recebe o topo de livro e devolve as cotações que quer manter
paradas. Ela não executa nada: quem decide se a cotação virou execução é o
`PaperEngine`, e é ali que mora a honestidade do experimento.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Topo:
    """Estado do topo de livro num instante."""
    token_id: str
    ts_local: int
    best_bid: float | None
    best_ask: float | None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


@dataclass(frozen=True)
class Cotacao:
    """Par de ordens passivas que a estratégia quer manter no livro."""
    bid: float | None
    ask: float | None
    size: float


class Estrategia:
    nome = "base"

    def cotar(self, topo: Topo, posicao: float) -> Cotacao | None:
        raise NotImplementedError


class MakerSimples(Estrategia):
    """Fica parado nos dois lados do livro e ADMINISTRA O ESTOQUE que acumula.

    Não prevê nada. A tese é embolsar o spread e o rebate, repetidamente, e
    voltar para zero. A segunda metade dessa frase é o que faltava.

    ## Por que a gestão de estoque existe

    A versão anterior fazia só a primeira metade: comprava (ou vendia) e ficava
    sentada. Medido em 30h de simulação: de ~140 execuções, **6 a 9** viraram
    ida-e-volta completa. O resto empilhou e ficou na mão por **309 minutos de
    mediana**, até o mercado resolver e virar $1 ou $0.

    O resultado disso não é market making, é aposta — e a conta apareceu
    inteira: `Realizado NO LIVRO` de **+$1,90** contra `Realizado NA RESOLUÇÃO`
    de **−$238,29**. O spread capturado era irrelevante perto da moeda no ar.

    ## As quatro travas

    1. **Desvio assimétrico** — com estoque, o lado da SAÍDA fica agressivo e o
       lado da ENTRADA se afasta. A versão anterior deslocava os dois lados
       igualmente, o que reposiciona a cotação mas não desmonta nada.
    2. **Janela sem abrir** — perto da resolução, só cota o lado que REDUZ
       posição. Não adianta desmontar de um lado e montar do outro.
    3. **Saída forçada** — mais perto ainda, atravessa o spread e zera. Custa o
       spread; é um custo conhecido e pequeno no lugar de um risco de 68c.
    4. **Piso para vender** — não abre posição vendida em azarão barato. Vender
       a 26c é arriscar 74c para ganhar 26c: precisa acertar 74% das vezes só
       para empatar, e o medido a 230ms foi **50%**.

    As duas restrições antigas continuam valendo:

    - **spread mínimo**: abaixo de um certo spread não há o que capturar.
    - **faixa de preço**: perto de 0 ou 1 o custo como fração do capital explode.
    """

    nome = "maker"

    def __init__(self, size: float = 100.0, spread_minimo_c: float = 1.0,
                 preco_min: float = 0.10, preco_max: float = 0.90,
                 posicao_maxima: float = 500.0, skew_maximo_c: float = 2.0,
                 tick: float = 0.01, minutos_sem_abrir: float = 30.0,
                 minutos_saida_forcada: float = 10.0,
                 preco_min_venda: float = 0.15) -> None:
        self.size = size
        self.spread_minimo = spread_minimo_c / 100.0
        self.preco_min = preco_min
        self.preco_max = preco_max
        self.posicao_maxima = posicao_maxima
        self.skew_maximo = skew_maximo_c / 100.0
        self.tick = tick
        self.minutos_sem_abrir = minutos_sem_abrir
        self.minutos_saida_forcada = minutos_saida_forcada
        self.preco_min_venda = preco_min_venda

    # ------------------------------------------------------------------
    # Estoque
    # ------------------------------------------------------------------

    def _razao_estoque(self, posicao: float) -> float:
        """Quão cheio está o estoque, de -1 (vendido no limite) a +1."""
        if not self.posicao_maxima:
            return 0.0
        return max(-1.0, min(1.0, posicao / self.posicao_maxima))

    def _arredondar(self, preco: float) -> float:
        """Ao tick, com o segundo `round` limpando o resíduo binário: sem ele um
        bid de 47c sai como 0.47000000000000003, que não é preço postável."""
        return round(round(preco / self.tick) * self.tick, 4)

    def _saida_forcada(self, topo: Topo, posicao: float) -> Cotacao | None:
        """Atravessa o spread para zerar antes do mercado resolver.

        Devolve uma cotação MARKETABLE de propósito: preço no toque do outro
        lado. O motor detecta isso e executa na hora, como taker — pagando taxa
        e sem rebate. É assim que tem de ser: sair correndo custa dinheiro, e
        esse custo precisa aparecer no resultado, não sumir.
        """
        if posicao > 0:
            if topo.best_bid is None:
                return None
            return Cotacao(bid=None, ask=topo.best_bid, size=abs(posicao))
        if topo.best_ask is None:
            return None
        return Cotacao(bid=topo.best_ask, ask=None, size=abs(posicao))

    # ------------------------------------------------------------------

    def cotar(self, topo: Topo, posicao: float,
              minutos_ate_fim: float | None = None) -> Cotacao | None:
        tem_estoque = abs(posicao) > 1e-9

        # 1) Perto demais da resolução com estoque na mão: sai atravessando.
        if (minutos_ate_fim is not None and tem_estoque
                and minutos_ate_fim <= self.minutos_saida_forcada):
            return self._saida_forcada(topo, posicao)

        if topo.best_bid is None or topo.best_ask is None:
            return None
        mid, spread = topo.mid, topo.spread
        if mid is None or spread is None:
            return None
        if spread < self.spread_minimo:
            return None
        if not (self.preco_min <= mid <= self.preco_max):
            return None

        # 2) Desvio assimétrico: puxa a saída para perto, empurra a entrada.
        razao = self._razao_estoque(posicao)
        desvio = -razao * self.skew_maximo          # negativo quando comprado
        afasta = abs(razao) * self.skew_maximo      # alarga o lado da entrada
        if posicao > 0:      # comprado: saída é vender, entrada é comprar
            bid = topo.best_bid + desvio - afasta
            ask = topo.best_ask + desvio
        elif posicao < 0:    # vendido: saída é comprar, entrada é vender
            bid = topo.best_bid + desvio
            ask = topo.best_ask + desvio + afasta
        else:
            bid, ask = topo.best_bid, topo.best_ask

        bid = self._arredondar(bid)
        ask = self._arredondar(ask)
        if bid >= ask:
            return None
        # Nunca postar preço fora do intervalo válido de uma probabilidade.
        bid = max(self.tick, min(bid, 1 - self.tick))
        ask = max(self.tick, min(ask, 1 - self.tick))

        # 3) Estoque no limite: para de cotar o lado que aumentaria a posição.
        if posicao >= self.posicao_maxima:
            bid = None
        if posicao <= -self.posicao_maxima:
            ask = None

        # 4) Não abrir venda em azarão barato — risco 74c para ganhar 26c.
        if mid < self.preco_min_venda and posicao <= 0:
            ask = None

        # 5) Janela de encerramento: só o lado que REDUZ estoque. Desmontar de
        #    um lado e montar do outro deixa a posição igual no fim.
        if (minutos_ate_fim is not None
                and minutos_ate_fim <= self.minutos_sem_abrir):
            if posicao > 0:
                bid = None
            elif posicao < 0:
                ask = None
            else:
                return None   # sem estoque e perto do fim: não abre nada

        if bid is None and ask is None:
            return None
        return Cotacao(bid=bid, ask=ask, size=self.size)
