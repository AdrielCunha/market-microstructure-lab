"""Por que a posição não fecha? Anatomia do estoque preso.

A decomposição livro/resolução mostrou QUE a estratégia acumula estoque em vez
de girar. Este módulo mostra POR QUE, respondendo três coisas que o painel não
responde:

1. Quanto tempo a posição fica na mão antes de virar $1 ou $0.
2. Quanto do fluxo é entrada (abre posição) e quanto é saída (desmonta).
3. Se existe algum nicho onde a posição realmente gira.

    python -m analysis.estoque
"""

from __future__ import annotations

import polars as pl

from core.db import connect

# Uma execução da regra `negocio` é a única que corresponde a negócio impresso.
# `cruzamento` conta cancelamento como execução, então distorce toda contagem.
REGRA_HONESTA = "negocio"

# Só a sessão corrente. `paper_fills` acumula rodadas antigas — algumas com
# bugs já corrigidos, como a venda a descoberto de graça e a realimentação de
# capital. Misturar aquilo com a série nova produz uma anatomia de estoque que
# não descreve estratégia nenhuma. O painel `/ordens` usa o mesmo corte.
FILLS = """
SELECT f.ts_local, f.strategy, f.regra, f.token_id, f.side, f.price, f.size,
       f.notional_usd, f.posicao_depois,
       COALESCE(m.category, 'desconhecido') AS category,
       m.end_date
FROM paper_fills f
LEFT JOIN markets m USING (token_id)
LEFT JOIN paper_sessao s ON s.strategy = f.strategy
WHERE f.ts_local >= COALESCE(s.desde_ts, 0)
ORDER BY f.strategy, f.token_id, f.ts_local
"""


def carregar(con=None) -> pl.DataFrame:
    own = con is None
    con = con or connect(read_only=True)
    try:
        df = con.execute(FILLS).pl()
    except Exception:
        # Banco anterior a `paper_sessao` (arquivo antigo aberto para análise).
        # Mostra a série inteira, que é o comportamento antigo — melhor que
        # recusar a rodar.
        print("  (banco sem paper_sessao: analisando a serie inteira)\n")
        df = con.execute(FILLS.replace(
            "LEFT JOIN paper_sessao s ON s.strategy = f.strategy", ""
        ).replace("WHERE f.ts_local >= COALESCE(s.desde_ts, 0)", "")).pl()
    if own:
        con.close()
    return df


def ciclos(df: pl.DataFrame) -> pl.DataFrame:
    """Tempo entre a PRIMEIRA execução num token e a liquidação dele.

    É o tempo que o capital ficou preso. Market making saudável mede isso em
    segundos ou minutos; se medir em horas, não é market making.
    """
    if df.is_empty():
        return df
    primeiro = (df.filter(pl.col("regra") != "liquidacao")
                  .group_by("strategy", "token_id")
                  .agg(pl.col("ts_local").min().alias("ts_entrada")))
    liq = (df.filter(pl.col("regra") == "liquidacao")
             .group_by("strategy", "token_id")
             .agg(pl.col("ts_local").max().alias("ts_liquidacao"),
                  pl.col("price").last().alias("preco_final"),
                  pl.col("size").sum().alias("cotas_liquidadas"),
                  pl.col("category").last().alias("category")))
    return (primeiro.join(liq, on=["strategy", "token_id"], how="inner")
                    .with_columns(((pl.col("ts_liquidacao") - pl.col("ts_entrada"))
                                   / 60_000).round(1).alias("minutos_preso")))


def fluxo_por_lado(df: pl.DataFrame) -> pl.DataFrame:
    """Entrada x saída. Se BUY e SELL não se equilibram, o estoque só cresce."""
    return (df.filter(pl.col("regra") != "liquidacao")
              .group_by("strategy", "side")
              .agg(pl.len().alias("execucoes"),
                   pl.col("size").sum().alias("cotas"),
                   pl.col("notional_usd").sum().round(2).alias("notional"))
              .sort("strategy", "side"))


def main(con=None) -> None:
    df = carregar(con)
    if df.is_empty():
        print("Sem execucoes de paper trading ainda.")
        return

    print("=" * 78)
    print("ANATOMIA DO ESTOQUE PRESO")
    print("=" * 78)

    n_liq = df.filter(pl.col("regra") == "liquidacao").height
    n_exec = df.height - n_liq
    print(f"Base: {n_exec:,} execucoes + {n_liq:,} liquidacoes por resolucao\n")

    print("--- 1) ENTRADA x SAIDA (o estoque se equilibra?) ---")
    print(fluxo_por_lado(df))
    print("\n  Leitura: market making compra E vende. Se um lado domina, a")
    print("  estrategia esta acumulando direcional, nao girando estoque.")

    c = ciclos(df)
    if c.is_empty():
        print("\n--- 2) Nenhuma posicao liquidada ainda. ---")
    else:
        print("\n--- 2) QUANTO TEMPO O CAPITAL FICOU PRESO ---")
        m = c["minutos_preso"]
        print(f"  posicoes liquidadas : {c.height}")
        print(f"  mediana             : {m.median():,.1f} min")
        print(f"  p25 / p75           : {m.quantile(0.25):,.1f} / {m.quantile(0.75):,.1f} min")
        print(f"  maximo              : {m.max():,.1f} min")
        print("\n  Leitura: market making sadio mede isto em segundos. Em horas,")
        print("  o que existe e uma aposta direcional com passos extras.")

        print("\n--- 3) COMO A LIQUIDACAO TERMINOU ---")
        print(c.group_by("preco_final")
               .agg(pl.len().alias("posicoes"),
                    pl.col("cotas_liquidadas").sum().alias("cotas"))
               .sort("preco_final"))
        print("  preco_final 1.0 = o resultado aconteceu · 0.0 = nao aconteceu.")
        print("  Perto de 50/50 significa que estamos jogando cara ou coroa.")

    print("\n--- 4) GIRO POR TOKEN (regra honesta) ---")
    h = df.filter((pl.col("regra") == REGRA_HONESTA))
    if h.is_empty():
        print("  sem execucoes da regra honesta.")
    else:
        por_token = (h.group_by("strategy", "token_id", "category")
                      .agg(pl.len().alias("execucoes"),
                           (pl.col("side") == "BUY").sum().alias("compras"),
                           (pl.col("side") == "SELL").sum().alias("vendas")))
        print(por_token.group_by("strategy")
                       .agg(pl.len().alias("tokens_tocados"),
                            (pl.col("vendas") > 0).sum().alias("tokens_com_venda"),
                            pl.col("execucoes").sum().alias("execucoes"))
                       .sort("strategy"))
        print("\n  tokens_com_venda / tokens_tocados = fracao onde chegamos a")
        print("  desmontar alguma coisa. Baixo = compramos e ficamos com o mico.")


if __name__ == "__main__":
    main()
