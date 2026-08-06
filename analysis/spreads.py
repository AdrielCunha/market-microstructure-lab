"""Onde o spread é largo o bastante para pagar market making?

O spread é a receita bruta de quem faz mercado e o custo de entrada de quem
cruza. Esta análise responde: em qual nicho ele compensa o custo, e com que
frequência.

    python -m analysis.spreads
"""

from __future__ import annotations

import polars as pl

from analysis.fees import CostModel, taker_fee
from core import janela
from core.db import connect

def _query() -> str:
    return f"""
SELECT m.category, m.neg_risk, m.fee_rate, m.fee_rebate_rate,
       m.liquidity_usd, m.end_date,
       b.token_id, b.best_bid, b.best_ask, b.mid, b.spread, b.ts_local,
       date_diff('minute', now(), m.end_date) / 1440.0 AS dias_ate_resolver
FROM book_top b
JOIN markets m USING (token_id)
WHERE b.source = 'ws' AND b.spread IS NOT NULL AND b.mid IS NOT NULL
  AND b.spread >= 0
  {janela.clausula('b.ts_local')}
"""


def load(con=None) -> pl.DataFrame:
    own = con is None
    con = con or connect(read_only=True)
    df = con.execute(_query()).pl()
    if own:
        con.close()
    return df


def por_categoria(df: pl.DataFrame) -> pl.DataFrame:
    return (df.group_by("category")
              .agg(
                  pl.len().alias("obs"),
                  pl.col("token_id").n_unique().alias("tokens"),
                  (pl.col("spread").median() * 100).round(2).alias("spread_mediano_c"),
                  (pl.col("spread").quantile(0.25) * 100).round(2).alias("p25_c"),
                  (pl.col("spread").quantile(0.75) * 100).round(2).alias("p75_c"),
                  (pl.col("mid").median()).round(3).alias("mid_mediano"),
              )
              .sort("obs", descending=True))


def por_faixa_de_preco(df: pl.DataFrame) -> pl.DataFrame:
    """O spread varia muito com o preço: perto de 0 ou 1 o livro é fino em
    centavos mas caríssimo em % do capital."""
    return (df.with_columns(
                pl.when(pl.col("mid") < 0.10).then(pl.lit("a) 0-10c"))
                  .when(pl.col("mid") < 0.25).then(pl.lit("b) 10-25c"))
                  .when(pl.col("mid") < 0.50).then(pl.lit("c) 25-50c"))
                  .when(pl.col("mid") < 0.75).then(pl.lit("d) 50-75c"))
                  .when(pl.col("mid") < 0.90).then(pl.lit("e) 75-90c"))
                  .otherwise(pl.lit("f) 90-100c")).alias("faixa"))
              .group_by("faixa")
              .agg(
                  pl.len().alias("obs"),
                  (pl.col("spread").median() * 100).round(2).alias("spread_c"),
                  # Spread relativo: é isso que o capital sente.
                  ((pl.col("spread") / pl.col("mid")).median() * 100).round(2)
                      .alias("spread_pct_do_mid"),
              )
              .sort("faixa"))


def receita_liquida_do_maker(df: pl.DataFrame, cfg_rate: float = 0.05,
                             rebate: float = 0.15) -> pl.DataFrame:
    """Quanto sobra para quem faz mercado, por faixa de preço.

    O maker ganha metade do spread por perna, mais o rebate, e não paga taxa.
    Isto é receita BRUTA por rodada — não desconta risco de estoque nem seleção
    adversa (ser executado justamente quando o preço vai contra), que são o
    verdadeiro custo do market making e não dá para medir só com o livro.
    """
    return (df.filter(pl.col("mid").is_between(0.02, 0.98))
              .with_columns(
                  (pl.col("spread") / 2).alias("meio_spread"),
                  pl.col("mid").map_elements(
                      lambda p: taker_fee(p, 1.0, cfg_rate) * rebate,
                      return_dtype=pl.Float64).alias("rebate_por_cota"))
              .with_columns(
                  (pl.col("meio_spread") + pl.col("rebate_por_cota")).alias("bruto_por_cota"))
              .with_columns(
                  pl.when(pl.col("mid") < 0.25).then(pl.lit("a) <25c"))
                    .when(pl.col("mid") < 0.75).then(pl.lit("b) 25-75c"))
                    .otherwise(pl.lit("c) >75c")).alias("faixa"))
              .group_by("faixa")
              .agg(
                  pl.len().alias("obs"),
                  (pl.col("meio_spread").median() * 100).round(3).alias("meio_spread_c"),
                  (pl.col("rebate_por_cota").median() * 100).round(3).alias("rebate_c"),
                  (pl.col("bruto_por_cota").median() * 100).round(3).alias("bruto_c"),
                  ((pl.col("bruto_por_cota") / pl.col("mid")).median() * 100).round(3)
                      .alias("bruto_pct_notional"),
              )
              .sort("faixa"))


def main() -> None:
    df = load()
    if df.is_empty():
        print("Sem dados de livro. Rode `python -m collector.run` antes.")
        return

    janela_min = (df["ts_local"].max() - df["ts_local"].min()) / 60000
    print(f"Base: {len(df):,} observacoes de topo de livro, "
          f"{df['token_id'].n_unique()} tokens, janela de {janela_min:.1f} min\n")

    print("=== SPREAD POR CATEGORIA ===")
    print(por_categoria(df))
    print("\n=== SPREAD POR FAIXA DE PRECO ===")
    print(por_faixa_de_preco(df))
    print("\n=== RECEITA BRUTA DO MAKER (meio spread + rebate) ===")
    print(receita_liquida_do_maker(df))

    m = CostModel()
    custo_rt = m.roundtrip_cost_pct(0.5, 1000, fee_rate=0.05, days_to_resolution=1)
    spread_mediano = float(df["spread"].median())
    print(f"\nReferencia: spread mediano = {spread_mediano*100:.2f}c")
    print(f"            custo ida-e-volta de taker a 50c = {custo_rt*100:.2f}% do notional")
    print("            => cruzar o spread duas vezes custa mais que o spread inteiro.")


if __name__ == "__main__":
    main()
