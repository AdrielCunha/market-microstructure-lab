"""Modelo de custo de uma operação no Polymarket.

Componentes: taxa de taker, gás, e custo de oportunidade do capital travado até
a resolução — esse último é o que a maioria das análises de prediction market
ignora e é justamente o que derruba o retorno anualizado.

ATENÇÃO — a fórmula exata da taxa NÃO está verificada empiricamente.
O campo `fee_rate_bps` do WebSocket veio 0 em 957/957 execuções observadas, ou
seja, não serve para calibrar. O que sabemos do catálogo (Gamma API) é o
`feeSchedule` por mercado:

    esportes    : rate=0.05  exponent=1  rebateRate=0.15  takerOnly=true
    nao-esporte : rate=0.07  exponent=1  rebateRate=0.25  takerOnly=true

O formato conhecido é uma curva que colapsa nos extremos de preço, mas há duas
leituras plausíveis do multiplicador — `p*(1-p)` e `min(p,1-p)`. Implementamos
as duas e usamos por padrão a MAIS CARA, para não construir uma tese em cima de
um custo subestimado. Isso vira medição real na Fase 2, comparando com trades
on-chain (ver `analysis/slippage.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from core import config

FeeShape = Literal["product", "min"]


def fee_multiplier(price: float, shape: FeeShape = "product",
                   exponent: float = 1.0) -> float:
    """Parte da taxa que depende do preço. Zero nos extremos, máxima em 0.5."""
    p = min(max(price, 0.0), 1.0)
    base = p * (1.0 - p) if shape == "product" else min(p, 1.0 - p)
    return base ** exponent if exponent != 1.0 else base


def taker_fee(price: float, shares: float, rate: float, *,
              shape: FeeShape | None = None, exponent: float = 1.0) -> float:
    """Taxa em USD paga por quem cruza o spread.

    Com `shape=None` devolve o pior caso entre as duas leituras da fórmula.
    """
    if shape is not None:
        return rate * fee_multiplier(price, shape, exponent) * shares
    pior = max(fee_multiplier(price, "product", exponent),
               fee_multiplier(price, "min", exponent))
    return rate * pior * shares


def maker_rebate(price: float, shares: float, rate: float, rebate_rate: float,
                 *, shape: FeeShape | None = None, exponent: float = 1.0) -> float:
    """Rebate creditado a quem deixa a ordem parada no livro.

    É o motor econômico do market making no Polymarket: o maker não paga taxa e
    ainda recebe uma fração do que o taker pagou. Modelamos como uma fração da
    taxa de taker; a proporção exata precisa ser confirmada na Fase 2.
    """
    return rebate_rate * taker_fee(price, shares, rate, shape=shape, exponent=exponent)


@dataclass(frozen=True)
class CostBreakdown:
    """Custo total de montar (e carregar) uma posição, em USD."""
    notional: float
    fee: float
    gas: float
    carry: float
    rebate: float

    @property
    def total(self) -> float:
        return self.fee + self.gas + self.carry - self.rebate

    @property
    def total_pct(self) -> float:
        """Custo como fração do notional — a unidade em que comparamos com edge."""
        return self.total / self.notional if self.notional else 0.0

    def __str__(self) -> str:
        return (f"notional=${self.notional:,.2f} taxa=${self.fee:.4f} gas=${self.gas:.4f} "
                f"carrego=${self.carry:.4f} rebate=${self.rebate:.4f} "
                f"-> total=${self.total:.4f} ({self.total_pct*100:.3f}%)")


class CostModel:
    def __init__(self, cfg: dict | None = None) -> None:
        c = cfg or config.load()["fees"]
        self.default_rate = float(c.get("taker_fee_rate", 0.02))
        self.gas_usd = float(c.get("gas_usd_per_tx", 0.02))
        self.annual_rate = float(c.get("capital_annual_rate", 0.14))
        shape = c.get("fee_shape")
        self.shape: FeeShape | None = shape if shape in ("product", "min") else None

    def carry_cost(self, notional: float, days_to_resolution: float) -> float:
        """Custo de oportunidade do capital preso até a resolução.

        No Polymarket o dinheiro fica travado na posição — um trade de 3% de
        lucro que só liquida em 60 dias rende menos que parece.
        """
        return notional * self.annual_rate * max(days_to_resolution, 0.0) / 365.0

    def cost(self, price: float, shares: float, *, fee_rate: float | None = None,
             exponent: float = 1.0, days_to_resolution: float = 0.0,
             is_maker: bool = False, rebate_rate: float = 0.0,
             n_tx: int = 1) -> CostBreakdown:
        notional = price * shares
        rate = self.default_rate if fee_rate is None else fee_rate
        fee = 0.0 if is_maker else taker_fee(price, shares, rate,
                                             shape=self.shape, exponent=exponent)
        rebate = (maker_rebate(price, shares, rate, rebate_rate,
                               shape=self.shape, exponent=exponent)
                  if is_maker else 0.0)
        return CostBreakdown(
            notional=notional,
            fee=fee,
            gas=self.gas_usd * n_tx,
            carry=self.carry_cost(notional, days_to_resolution),
            rebate=rebate,
        )

    def roundtrip_cost_pct(self, price: float, shares: float, *,
                           fee_rate: float | None = None, exponent: float = 1.0,
                           days_to_resolution: float = 0.0) -> float:
        """Custo de entrar E sair como taker, em fração do notional.

        É a barra que qualquer edge precisa superar para valer a pena.
        """
        entrada = self.cost(price, shares, fee_rate=fee_rate, exponent=exponent,
                            days_to_resolution=days_to_resolution)
        saida = self.cost(price, shares, fee_rate=fee_rate, exponent=exponent,
                          days_to_resolution=0.0)
        notional = price * shares
        return (entrada.total + saida.total) / notional if notional else 0.0


def _demo() -> None:
    """Mostra as duas leituras da fórmula lado a lado.

    A diferença entre elas é grande demais para ser escondida atrás de um
    "pior caso": é a maior incerteza do modelo de custo e precisa aparecer.
    """
    rate = 0.05  # esportes
    print("TAXA DE TAKER, esportes (rate=0.05) — duas leituras da formula")
    print("A verdadeira ainda NAO foi verificada: fee_rate_bps veio 0 em 957/957 execucoes.\n")
    print(f"{'preco':>6} | {'p*(1-p)':>17} | {'min(p,1-p)':>17}")
    print(f"{'':>6} | {'c/cota':>8} {'% notional':>8} | {'c/cota':>8} {'% notional':>8}")
    print("-" * 52)
    for p in (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95, 0.99):
        fp = taker_fee(p, 1.0, rate, shape="product")
        fm = taker_fee(p, 1.0, rate, shape="min")
        print(f"{p:>6.2f} | {fp*100:>7.3f}c {fp/p*100:>7.2f}% | "
              f"{fm*100:>7.3f}c {fm/p*100:>7.2f}%")

    print("\nDuas leituras do mesmo dado que importam para a estrategia:")
    print("  1) A taxa em centavos por cota e simetrica, mas como % do capital")
    print("     aplicado ela explode nos azaroes: a 5c, a taxa come 5% da aposta")
    print("     na leitura mais cara. Comprar azarao paga caro so para entrar.")
    print("  2) Comparar com o spread mediano medido no livro: 1.9 centavos.")
    print("     Edge so existe se superar taxa + spread + gas + carrego.")

    m = CostModel()
    print("\nCusto do capital travado (carrego), por notional de $1.000:")
    for d in (1, 7, 30, 90):
        print(f"  {d:>3} dias ate resolver: ${m.carry_cost(1000, d):>6.2f}"
              f"  ({m.carry_cost(1000, d)/10:.2f}% do notional)")


if __name__ == "__main__":
    _demo()
