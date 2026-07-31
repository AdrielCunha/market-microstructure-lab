"""Onde um operador LENTO consegue fazer mercado?

A pergunta não é "onde o spread é largo". É onde o spread é largo **e** a
competição é fraca o bastante para uma máquina a 230ms conseguir participar.

A métrica que decide isso é a **vida do topo de livro**: quanto tempo o melhor
preço sobrevive antes de alguém repricar. Se o topo troca a cada 150ms, nossa
cotação nasce defasada e só é executada quando o preço já virou contra nós —
seleção adversa pura. Se sobrevive 30 segundos, dá para ficar parado no livro.

Comparação de escala: as corridas de latência entre firmas grandes são decididas
em 5–10 microssegundos. Nós estamos a ~230.000 microssegundos. Não competimos
por velocidade em lugar nenhum — só podemos existir onde velocidade não decide.

    python -m analysis.nichos
"""

from __future__ import annotations

import polars as pl

from analysis.fees import taker_fee
from core.db import connect

# Latência medida desta máquina até o CLOB (ida e volta).
LATENCIA_MS = 230

BASE = """
SELECT b.token_id, b.ts_local, b.spread, b.mid,
       m.category, m.liquidity_usd, m.fee_rate, m.fee_rebate_rate,
       date_diff('hour', now(), m.end_date) AS horas_ate_resolver
FROM book_top b
JOIN markets m USING (token_id)
WHERE b.source = 'ws' AND b.spread IS NOT NULL AND b.mid IS NOT NULL
  AND b.spread >= 0 AND b.mid BETWEEN 0.02 AND 0.98
ORDER BY b.token_id, b.ts_local
"""


def faixa_de_preco(col: str = "mid") -> pl.Expr:
    return (pl.when(pl.col(col) < 0.10).then(pl.lit("a) 2-10c"))
              .when(pl.col(col) < 0.25).then(pl.lit("b) 10-25c"))
              .when(pl.col(col) < 0.50).then(pl.lit("c) 25-50c"))
              .when(pl.col(col) < 0.75).then(pl.lit("d) 50-75c"))
              .when(pl.col(col) < 0.90).then(pl.lit("e) 75-90c"))
              .otherwise(pl.lit("f) 90-98c")).alias("faixa"))


def preparar(con=None) -> pl.DataFrame:
    """Carrega o livro e calcula a vida de cada topo.

    Vida do topo = tempo até a PRÓXIMA mudança daquele token. É por isso que a
    consulta vem ordenada por token e tempo: o `shift` só faz sentido dentro do
    mesmo instrumento.
    """
    own = con is None
    con = con or connect(read_only=True)
    df = con.execute(BASE).pl()
    if own:
        con.close()
    if df.is_empty():
        return df

    return (df.with_columns(
                (pl.col("ts_local").shift(-1).over("token_id")
                 - pl.col("ts_local")).alias("vida_ms"))
              .drop_nulls("vida_ms")
              # Cortes de saneamento: vida negativa é impossível, e acima de
              # 10 min quase sempre é buraco entre sessões de coleta, não
              # mercado parado.
              .filter((pl.col("vida_ms") > 0) & (pl.col("vida_ms") < 600_000))
              .with_columns(faixa_de_preco()))


def por_nicho(df: pl.DataFrame, minimo_obs: int = 200) -> pl.DataFrame:
    """Uma linha por (liga × faixa de preço), com o que decide a viabilidade."""
    return (df.group_by("category", "faixa")
              .agg(
                  pl.len().alias("obs"),
                  pl.col("token_id").n_unique().alias("tokens"),
                  (pl.col("spread").median() * 100).round(2).alias("spread_c"),
                  pl.col("vida_ms").median().round(0).alias("vida_topo_ms"),
                  # Fração das cotações que sobreviveriam ao nosso trânsito.
                  (pl.col("vida_ms") > LATENCIA_MS).mean().alias("sobrevive_lat"),
                  (pl.col("vida_ms") > 5 * LATENCIA_MS).mean().alias("sobrevive_5x"),
                  pl.col("mid").median().round(3).alias("mid"),
                  pl.col("liquidity_usd").median().round(0).alias("liq"),
                  pl.col("fee_rate").max().alias("fee_rate"),
                  pl.col("fee_rebate_rate").max().alias("rebate"),
              )
              .filter(pl.col("obs") >= minimo_obs)
              .with_columns(
                  (pl.col("sobrevive_lat") * 100).round(1).alias("sobrevive_pct"),
                  (pl.col("sobrevive_5x") * 100).round(1).alias("folga_pct"),
              ))


def receita_e_score(df: pl.DataFrame, horas: float) -> pl.DataFrame:
    """Receita bruta do maker e a taxa de oportunidade para operador lento.

    São TRÊS dimensões, e faltar qualquer uma zera o nicho:

    1. **Receita** — spread + rebate. Sem isso não há o que capturar.
    2. **Sobrevivência** — o topo precisa durar mais que os nossos 230ms, senão
       a cotação nasce defasada e só é executada quando o preço virou contra.
    3. **Fluxo** — precisa haver movimento. Um livro parado com spread largo
       não paga nada: ninguém vem negociar contra a nossa ordem.

    A primeira versão desta análise só media (1) e (2), e por isso coroou um
    nicho com 3 tokens e outro cujo topo vivia 2,4 minutos porque simplesmente
    não acontecia nada ali. Spread largo em mercado morto é miragem.
    """
    return (df.with_columns(
                pl.struct("mid", "fee_rate", "rebate").map_elements(
                    lambda s: 2 * s["rebate"] * taker_fee(
                        s["mid"], 1.0, s["fee_rate"] or 0.05),
                    return_dtype=pl.Float64).alias("rebate_cota"))
              .with_columns(
                  ((pl.col("spread_c") / 100 + pl.col("rebate_cota"))
                   / pl.col("mid") * 100).round(2).alias("bruto_pct"),
                  # Mudanças de topo por token por hora: proxy de atividade.
                  (pl.col("obs") / pl.col("tokens") / horas).round(1)
                      .alias("fluxo_h"))
              .with_columns(
                  # Taxa de oportunidade: receita x chance de estar no livro a
                  # tempo x quantidade de vezes que a chance aparece.
                  (pl.col("bruto_pct") * pl.col("sobrevive_lat")
                   * pl.col("fluxo_h")).round(1).alias("oportunidade"))
              .sort("oportunidade", descending=True))


def main(con=None) -> None:
    df = preparar(con)
    if df.is_empty():
        print("Sem dados de livro. Rode o coletor antes.")
        return

    horas = (df["ts_local"].max() - df["ts_local"].min()) / 3_600_000
    print("=" * 78)
    print("ONDE UM OPERADOR LENTO (230ms) CONSEGUE FAZER MERCADO")
    print("=" * 78)
    print(f"Base: {len(df):,} mudancas de topo, {df['token_id'].n_unique()} tokens, "
          f"{horas:.1f}h de janela\n")

    print("--- VIDA DO TOPO DE LIVRO (o numero que decide) ---")
    q = df["vida_ms"]
    print(f"  mediana geral      : {q.median():,.0f} ms")
    print(f"  p25 / p75          : {q.quantile(0.25):,.0f} / {q.quantile(0.75):,.0f} ms")
    print(f"  sobrevive aos 230ms: {(df['vida_ms'] > LATENCIA_MS).mean()*100:.1f}% das cotacoes")
    print(f"  sobrevive a 1,15s  : {(df['vida_ms'] > 5*LATENCIA_MS).mean()*100:.1f}%")
    print("\n  Leitura: a fracao que NAO sobrevive e onde a nossa cotacao nasce")
    print("  defasada. Ali so somos executados quando o preco ja virou contra.")

    nichos = receita_e_score(por_nicho(df), horas)
    if nichos.is_empty():
        print("\nNenhum nicho com observacoes suficientes ainda.")
        return

    print("\n--- MELHORES NICHOS (receita x sobrevivencia x fluxo) ---")
    print(nichos.select(
        "category", "faixa", "tokens", "spread_c", "mid", "vida_topo_ms",
        "sobrevive_pct", "fluxo_h", "bruto_pct", "oportunidade").head(12))

    print("\n--- A ARMADILHA: spread largo em mercado parado ---")
    print(nichos.filter(pl.col("fluxo_h") < 20)
                .sort("bruto_pct", descending=True)
                .select("category", "faixa", "tokens", "spread_c",
                        "vida_topo_ms", "fluxo_h", "bruto_pct",
                        "oportunidade").head(8))

    print("\n--- POR LIGA (agregado) ---")
    por_liga = (df.group_by("category")
                  .agg(pl.len().alias("obs"),
                       pl.col("token_id").n_unique().alias("tokens"),
                       (pl.col("spread").median()*100).round(2).alias("spread_c"),
                       pl.col("vida_ms").median().round(0).alias("vida_ms"),
                       (pl.col("vida_ms") > LATENCIA_MS).mean().round(3).alias("sobrev"))
                  .filter(pl.col("obs") >= 500)
                  .sort("sobrev", descending=True))
    print(por_liga)

    melhor = nichos.head(1)
    print("\n" + "=" * 78)
    print("CONCLUSAO")
    print("=" * 78)
    print(f"  Nicho com melhor combinacao: {melhor['category'][0]} / {melhor['faixa'][0]}")
    print(f"    spread {melhor['spread_c'][0]:.2f}c · topo vive {melhor['vida_topo_ms'][0]:,.0f}ms · "
          f"{melhor['sobrevive_pct'][0]:.0f}% sobrevive ao nosso transito")
    print(f"    receita bruta {melhor['bruto_pct'][0]:.2f}% · "
          f"fluxo {melhor['fluxo_h'][0]:.0f} mudancas/token/hora · "
          f"{melhor['tokens'][0]} tokens")
    print("""
  Ressalvas que precisam acompanhar qualquer decisao aqui:

  1. Isto mede OPORTUNIDADE, nao LUCRO. Receita bruta ignora selecao adversa,
     que so aparece nos fills. O markout continua sendo o juiz.
  2. 'Sobrevive ao transito' e condicao necessaria, nao suficiente: sobreviver
     nao significa ser executado, e ser executado nao significa lucrar.
  3. Nicho quieto costuma ser quieto por um motivo — pouco fluxo tambem
     significa poucas execucoes. Spread largo com zero volume nao paga nada.
  4. Janela curta. Liga que so jogou uma noite aparece distorcida.
""")


if __name__ == "__main__":
    main()
