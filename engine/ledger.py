"""Contabilidade das posições simuladas, em linguagem de carteira.

A versão anterior expunha "caixa", que é FLUXO DE DINHEIRO e não lucro: vender
mais do que comprou deixa o caixa alto mesmo estando perdendo. Isso enganava.

Agora a conta é a que qualquer um reconhece:

    patrimonio = capital inicial + PnL realizado + rebates - taxas
                 + PnL nao realizado

- **PnL realizado** — lucro de operação FECHADA. Comprou a 47c e vendeu a 49c:
  2c por cota, embolsados. É dinheiro que já é seu.
- **Taxas** — taxa de taker paga. Quem fica parado no livro não paga; só a
  saída forçada de estoque, que atravessa o spread para se desfazer da posição
  antes do mercado resolver.
- **PnL não realizado** — lucro (ou prejuízo) da posição AINDA ABERTA, avaliada
  pelo preço atual. É promessa: pode virar pó se o preço andar.
- **Capital travado** — quanto do capital está preso dentro das posições
  abertas. Não dá para usar em outra coisa enquanto não fechar.

O custo de cada posição usa PREÇO MÉDIO: ao reduzir posição, o lucro sai da
diferença entre o preço de saída e o custo médio do que estava lá.
"""

from __future__ import annotations

from collections import defaultdict


class Ledger:
    def __init__(self, capital_inicial: float = 1000.0) -> None:
        self.capital_inicial = capital_inicial
        self._posicao: dict[str, float] = defaultdict(float)
        self._custo_medio: dict[str, float] = defaultdict(float)
        self.realizado = 0.0
        self.rebates = 0.0
        # Taxa de taker paga. Maker não paga — só a saída forçada de estoque,
        # que atravessa o spread. É o preço de não ficar com o mico na mão.
        self.taxas = 0.0
        # Decomposição do realizado por motivo de fechamento (ver `aplicar`).
        self.realizado_por_motivo: dict[str, float] = {}
        self.fechamentos_por_motivo: dict[str, int] = {}
        self.cotas_por_motivo: dict[str, float] = {}
        self.fechamentos = 0
        self._pico = capital_inicial
        self.drawdown_max = 0.0

    # ---------------- posições ----------------

    def posicao(self, token_id: str) -> float:
        return self._posicao[token_id]

    def custo_medio(self, token_id: str) -> float:
        return self._custo_medio[token_id]

    def posicoes(self) -> dict[str, float]:
        return {k: v for k, v in self._posicao.items() if abs(v) > 1e-9}

    # ---------------- execução ----------------

    def aplicar(self, token_id: str, side: str, price: float, size: float,
                rebate: float = 0.0, motivo: str = "livro",
                taxa: float = 0.0) -> None:
        """`motivo` separa COMO a posição foi fechada, e isso decide tudo.

        - `livro`     — encontramos contraparte e desmontamos a posição. É market
                        making de verdade: entra e sai, captura o spread.
        - `resolucao` — o mercado acabou e a posição virou $1 ou $0. Isto NÃO é
                        market making, é aposta direcional que não conseguimos
                        desfazer a tempo.

        Sem esta separação o PnL realizado mistura as duas coisas e some com a
        única pergunta que importa: estamos capturando spread ou apostando?
        """
        self.rebates += rebate
        self.taxas += taxa
        pos = self._posicao[token_id]
        custo = self._custo_medio[token_id]
        delta = size if side == "BUY" else -size

        mesmo_sentido = pos == 0 or (pos > 0) == (delta > 0)
        if mesmo_sentido:
            # Aumenta a posição: recalcula o custo médio.
            total = abs(pos) + size
            self._custo_medio[token_id] = (custo * abs(pos) + price * size) / total
            self._posicao[token_id] = pos + delta
            return

        # Reduz (ou inverte) a posição: a parte fechada vira PnL realizado.
        fechado = min(size, abs(pos))
        if pos > 0:                      # estava comprado, vendeu
            ganho = (price - custo) * fechado
        else:                            # estava vendido, comprou
            ganho = (custo - price) * fechado
        self.realizado += ganho
        self.fechamentos += 1
        self.realizado_por_motivo[motivo] = \
            self.realizado_por_motivo.get(motivo, 0.0) + ganho
        self.fechamentos_por_motivo[motivo] = \
            self.fechamentos_por_motivo.get(motivo, 0) + 1
        self.cotas_por_motivo[motivo] = \
            self.cotas_por_motivo.get(motivo, 0.0) + fechado

        restante = size - fechado
        nova = pos + delta
        self._posicao[token_id] = nova
        if abs(nova) < 1e-9:
            self._custo_medio[token_id] = 0.0
        elif restante > 0:
            # Inverteu de lado: o que sobrou entra ao preço desta execução.
            self._custo_medio[token_id] = price

    # ---------------- avaliação ----------------

    def capital_travado(self) -> float:
        """Dinheiro preso dentro das posições abertas.

        Comprado e vendido travam capital DIFERENTE. Quem compra a 26c arrisca
        os 26c que pagou. Quem fica vendido a 26c arrisca os 74c que perde se o
        resultado acontecer — o risco de quem vende barato é o lado grande, não
        o pequeno. Tratar os dois por `abs(pos) * custo` subestimava o short em
        quase 3x e deixava a simulação vender muito além do que o caixa aguenta.
        """
        total = 0.0
        for t, p in self._posicao.items():
            if abs(p) < 1e-9:
                continue
            custo = self._custo_medio[t]
            total += p * custo if p > 0 else abs(p) * (1.0 - custo)
        return total

    def nao_realizado(self, mids: dict[str, float]) -> float:
        """Lucro/prejuízo da posição aberta pelo preço atual."""
        total = 0.0
        for token, pos in self._posicao.items():
            if abs(pos) < 1e-9:
                continue
            mid = mids.get(token)
            if mid is None:
                continue   # sem preço atual, não inventa avaliação
            total += (mid - self._custo_medio[token]) * pos
        return total

    def caixa_livre(self, mids: dict[str, float]) -> float:
        return (self.capital_inicial + self.realizado + self.rebates
                - self.taxas - self.capital_travado())

    def patrimonio(self, mids: dict[str, float]) -> float:
        """Quanto teríamos na carteira agora, se tudo fosse avaliado a mercado."""
        return (self.capital_inicial + self.realizado + self.rebates
                - self.taxas + self.nao_realizado(mids))

    def pnl_total(self, mids: dict[str, float]) -> float:
        return self.patrimonio(mids) - self.capital_inicial

    def retorno_pct(self, mids: dict[str, float]) -> float:
        if not self.capital_inicial:
            return 0.0
        return 100 * self.pnl_total(mids) / self.capital_inicial

    def marcar(self, mids: dict[str, float]) -> float:
        pat = self.patrimonio(mids)
        self._pico = max(self._pico, pat)
        self.drawdown_max = min(self.drawdown_max, pat - self._pico)
        return pat

    def resumo(self, mids: dict[str, float]) -> dict:
        abertas = self.posicoes()
        return {
            "capital_inicial": self.capital_inicial,
            "realizado": self.realizado,
            "rebates": self.rebates,
            "taxas": self.taxas,
            "nao_realizado": self.nao_realizado(mids),
            "pnl_total": self.pnl_total(mids),
            "retorno_pct": self.retorno_pct(mids),
            "patrimonio": self.patrimonio(mids),
            "capital_travado": self.capital_travado(),
            "caixa_livre": self.caixa_livre(mids),
            "drawdown_max": self.drawdown_max,
            "posicoes_abertas": len(abertas),
            "fechamentos": self.fechamentos,
            "exposicao_bruta": sum(abs(p) for p in abertas.values()),
            "realizado_livro": self.realizado_por_motivo.get("livro", 0.0),
            "realizado_resolucao": self.realizado_por_motivo.get("resolucao", 0.0),
            "fechamentos_livro": self.fechamentos_por_motivo.get("livro", 0),
            "fechamentos_resolucao": self.fechamentos_por_motivo.get("resolucao", 0),
            "cotas_livro": self.cotas_por_motivo.get("livro", 0.0),
            "cotas_resolucao": self.cotas_por_motivo.get("resolucao", 0.0),
        }
