"""Markout: a medição da seleção adversa.

Este é o número que o Gate 0 não conseguia produzir e que a Fase 1 existe para
obter.

A ideia: para cada execução simulada, olhar onde o preço médio estava um tempo
DEPOIS. Se compramos a 47c e 30 segundos depois o mid está em 45c, perdemos 2
centavos — fomos executados porque alguém sabia mais. Isso é seleção adversa, e
ela aparece aqui e em nenhum outro lugar.

    markout(h) = (mid em t+h − preço de compra) × tamanho       [compra]
    markout(h) = (preço de venda − mid em t+h) × tamanho        [venda]

A leitura que decide a tese:

    spread capturado + rebate  −  markout negativo  =  edge real

Se o markout comer o spread inteiro, market making não dá lucro por mais bem
executado que seja. Horizontes curtos e longos contam histórias diferentes:
perda a 5s costuma ser microestrutura; perda que persiste a 60s é informação.

    python -m analysis.markout
"""

from __future__ import annotations

import polars as pl

from core.db import connect

HORIZONTES_S = (5, 30, 60, 300)


def _consulta(horizonte_s: int) -> str:
    """Markout num horizonte, via ASOF join com o livro observado depois.

    O corte superior evita casar com um livro muito posterior quando não houve
    observação no horizonte pedido — sem ele, um token parado produziria
    markout calculado contra um preço de minutos depois.
    """
    ms = horizonte_s * 1000
    return f"""
    SELECT f.strategy, f.regra, f.side, f.token_id, f.size, f.price,
           f.mid_at_fill, f.spread_at_fill, f.rebate,
           b.mid AS mid_depois,
           CASE WHEN f.side = 'BUY' THEN (b.mid - f.price) * f.size
                ELSE (f.price - b.mid) * f.size END AS markout
    FROM paper_fills f
    ASOF JOIN (SELECT token_id, ts_local, mid FROM book_top
               WHERE mid IS NOT NULL AND source IN ('ws','copy_probe','rest_audit')) b
         ON f.token_id = b.token_id AND b.ts_local >= f.ts_local + {ms}
    WHERE b.ts_local - f.ts_local <= {ms * 3}
      -- Markout mede seleção adversa, que só existe em ordem PARADA no livro.
      -- Liquidação por resolução e saída forçada não são isso: a primeira não
      -- passa pelo livro, a segunda atravessa o spread por decisão nossa.
      -- Misturá-las contamina o único número que julga a tese.
      AND f.regra <> 'liquidacao'
      AND COALESCE(f.agressiva, FALSE) = FALSE
    """


def por_estrategia(con=None) -> pl.DataFrame:
    own = con is None
    con = con or connect(read_only=True)
    linhas = []
    for h in HORIZONTES_S:
        df = con.execute(_consulta(h)).pl()
        if df.is_empty():
            continue
        agg = (df.group_by("strategy")
                 .agg(
                     pl.len().alias("fills"),
                     pl.col("markout").sum().alias("markout_usd"),
                     (pl.col("markout") / (pl.col("price") * pl.col("size")))
                        .median().alias("markout_pct"),
                     pl.col("rebate").sum().alias("rebate_usd"),
                     (pl.col("spread_at_fill") / 2 * pl.col("size"))
                        .sum().alias("meio_spread_usd"),
                 )
                 .with_columns(pl.lit(h).alias("horizonte_s")))
        linhas.append(agg)
    if own:
        con.close()
    if not linhas:
        return pl.DataFrame()
    return (pl.concat(linhas)
              .with_columns(
                  # Edge real: o que capturamos menos o que a informação levou.
                  (pl.col("meio_spread_usd") + pl.col("rebate_usd")
                   + pl.col("markout_usd")).alias("edge_liquido_usd"))
              .sort(["strategy", "horizonte_s"]))


def main(con=None) -> None:
    df = por_estrategia(con)
    print("=" * 74)
    print("MARKOUT — medicao da selecao adversa")
    print("=" * 74)
    if df.is_empty():
        print("\nSem execucoes simuladas ainda, ou livro insuficiente depois delas.")
        print("Rode o coletor com paper trading ligado por mais tempo.")
        return

    print("\nPara cada execucao: onde o mid estava DEPOIS.")
    print("markout negativo = fomos executados e o preco andou contra nos.\n")
    print(df.select("strategy", "horizonte_s", "fills", "meio_spread_usd",
                    "rebate_usd", "markout_usd", "edge_liquido_usd"))

    print("\n" + "=" * 74)
    print("LEITURA")
    print("=" * 74)
    for estrategia in df["strategy"].unique().sort():
        sub = df.filter(pl.col("strategy") == estrategia)
        longo = sub.sort("horizonte_s").tail(1)
        edge = float(longo["edge_liquido_usd"][0])
        mk = float(longo["markout_usd"][0])
        h = int(longo["horizonte_s"][0])
        veredito = "POSITIVO" if edge > 0 else "NEGATIVO"
        print(f"\n  {estrategia} (horizonte {h}s)")
        print(f"    capturado (meio spread + rebate) : "
              f"${float(longo['meio_spread_usd'][0]) + float(longo['rebate_usd'][0]):>10,.2f}")
        print(f"    markout (selecao adversa)        : ${mk:>10,.2f}")
        print(f"    edge liquido                     : ${edge:>10,.2f}  {veredito}")

    print("""
  Como concluir:
    - As duas regras positivas  -> a tese sobrevive e merece dinheiro real.
    - As duas negativas         -> market making tambem morre. Fim da linha.
    - Divergentes               -> o resultado depende da posicao na fila, ou
                                   seja, de infraestrutura. Resposta util.

  Amostra pequena nao conclui nada. Espere centenas de execucoes por regra.
""")


if __name__ == "__main__":
    main()
