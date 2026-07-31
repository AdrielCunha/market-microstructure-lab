"""Copiar as carteiras do topo do leaderboard dá lucro ou prejuízo?

A tese ingênua é: "vejo o que o trader lucrativo fez e faço igual". Este módulo
mede as duas coisas que a derrubam ou a sustentam, com dados, não com opinião:

  1. ATRASO DE VISÃO — quanto tempo passa entre o trade acontecer e ele aparecer
     na API pública. Não é o intervalo do meu poll: é o atraso do próprio
     indexador do Polymarket, e nenhuma engenharia minha reduz isso.

  2. CUSTO DE ENTRAR DEPOIS — o trader lucrativo em geral é MAKER (deixa ordem
     parada e embolsa o spread). Quem copia chega depois e só consegue executar
     como TAKER, cruzando o spread — ou seja, comprando dele. O "slippage" aqui
     não é imperfeição de execução: é a receita dele saindo do meu bolso.

Limite conhecido: só temos livro dos tokens selecionados no catálogo, enquanto
as carteiras negociam o mercado inteiro. A cobertura é reportada junto.

    python -m analysis.copyability
"""

from __future__ import annotations

import polars as pl

from analysis.fees import CostModel, taker_fee
from core.db import connect

ATRASO = """
SELECT name, wallet, count(*) AS n,
       round(min((ts_seen - ts_trade) / 1000.0), 1)                AS min_s,
       round(quantile_cont((ts_seen - ts_trade) / 1000.0, 0.25), 1) AS p25_s,
       round(median((ts_seen - ts_trade) / 1000.0), 1)             AS mediana_s,
       round(quantile_cont((ts_seen - ts_trade) / 1000.0, 0.95), 1) AS p95_s,
       round(median(notional_usd), 2)                              AS notional_mediano
FROM wallet_trades
WHERE NOT is_backfill
GROUP BY 1, 2
ORDER BY n DESC
"""

# ASOF JOIN na direção causal da cópia: eu enxergo o trade em `ts_seen` e SÓ
# ENTÃO consulto o livro. O preço relevante é portanto o do primeiro livro em ou
# DEPOIS de ts_seen — não o anterior. Usar o livro anterior daria um preço que
# eu nunca teria conseguido pegar e faria a cópia parecer melhor do que é.
# `b.ts_local >= t.ts_seen` faz o ASOF escolher a observação mais próxima à
# frente. O corte de 60s evita casar com um livro de muito depois, quando o
# mercado já é outro.
COPIA = """
SELECT t.name, t.side, t.price AS preco_deles, t.size, t.notional_usd,
       t.ts_trade, t.ts_seen,
       (t.ts_seen - t.ts_trade) / 1000.0 AS atraso_s,
       (b.ts_local - t.ts_seen) / 1000.0 AS defasagem_livro_s,
       b.best_bid, b.best_ask, b.mid, b.spread,
       m.fee_rate, COALESCE(m.category, '(fora do catalogo)') AS category
FROM wallet_trades t
ASOF JOIN book_top b
     ON t.token_id = b.token_id AND b.ts_local >= t.ts_seen
-- LEFT: a sondagem sob demanda alcança justamente tokens FORA do catálogo, e um
-- join interno com `markets` descartaria exatamente os casos que ela existe
-- para capturar.
LEFT JOIN markets m ON m.token_id = t.token_id
WHERE NOT t.is_backfill AND b.best_ask IS NOT NULL AND b.best_bid IS NOT NULL
  AND b.source IN ('ws', 'copy_probe')
  AND b.ts_local - t.ts_seen <= 60000
"""

COBERTURA = """
SELECT
    (SELECT count(*) FROM wallet_trades WHERE NOT is_backfill) AS trades_novos,
    (SELECT count(DISTINCT t.trade_uid)
     FROM wallet_trades t JOIN book_top b USING (token_id)
     WHERE NOT t.is_backfill
       AND b.ts_local >= t.ts_seen AND b.ts_local - t.ts_seen <= 60000) AS com_livro
"""


def simular(df: pl.DataFrame, modelo: CostModel) -> pl.DataFrame:
    """Preço que eu pagaria copiando, versus o preço que eles conseguiram.

    Copiar um BUY = comprar ao melhor ASK. Copiar um SELL = vender ao melhor
    BID. Em ambos os casos eu cruzo o spread e pago taxa de taker.
    """
    return (df.with_columns(
                pl.when(pl.col("side") == "BUY").then(pl.col("best_ask"))
                  .otherwise(pl.col("best_bid")).alias("meu_preco"))
              .with_columns(
                  # Diferença assinada no sentido "pior para mim".
                  pl.when(pl.col("side") == "BUY")
                    .then(pl.col("meu_preco") - pl.col("preco_deles"))
                    .otherwise(pl.col("preco_deles") - pl.col("meu_preco"))
                    .alias("desvantagem"))
              .with_columns(
                  (pl.col("desvantagem") * 100).alias("desvantagem_c"),
                  pl.struct("meu_preco", "fee_rate").map_elements(
                      lambda s: taker_fee(s["meu_preco"], 1.0, s["fee_rate"] or 0.05),
                      return_dtype=pl.Float64).alias("taxa_por_cota"))
              .with_columns(
                  ((pl.col("desvantagem") + pl.col("taxa_por_cota")) / pl.col("meu_preco"))
                      .alias("custo_total_pct")))


def main() -> None:
    con = connect(read_only=True)

    print("=" * 68)
    print("1) ATRASO DE VISAO — quando o trade aparece na API publica")
    print("=" * 68)
    atraso = con.execute(ATRASO).pl()
    if atraso.is_empty():
        print("Sem trades novos ainda. Rode `python -m collector.run` por mais tempo.")
        con.close()
        return
    print(atraso.drop("wallet"))

    geral = con.execute("""
        SELECT count(*), round(min((ts_seen-ts_trade)/1000.0),1),
               round(median((ts_seen-ts_trade)/1000.0),1),
               round(quantile_cont((ts_seen-ts_trade)/1000.0,0.95),1)
        FROM wallet_trades WHERE NOT is_backfill
    """).fetchone()
    print(f"\n  Consolidado: n={geral[0]}  MINIMO={geral[1]}s  "
          f"mediana={geral[2]}s  p95={geral[3]}s")
    print(f"  O poll roda a cada 3s, entao o atraso NAO e meu: e do indexador")
    print(f"  do Polymarket. Nem colocar servidor nos EUA muda esse numero.")

    cob = con.execute(COBERTURA).fetchone()
    print(f"\n  Cobertura de livro: {cob[1]}/{cob[0]} trades novos em tokens que")
    print(f"  monitoramos ({100*cob[1]/cob[0] if cob[0] else 0:.0f}%). O resto")
    print(f"  aconteceu fora do catalogo selecionado.")

    print("\n" + "=" * 68)
    print("2) CUSTO DE COPIAR — preco que eu pagaria vs. preco que eles pegaram")
    print("=" * 68)
    df = con.execute(COPIA).pl()
    con.close()

    if df.is_empty():
        print("Sem interseccao entre trades novos e livro coletado.")
        print("Rode o coletor por mais tempo para acumular casos.")
        return

    sim = simular(df, CostModel())
    print(f"Casos com livro no instante da observacao: {len(sim)}\n")
    print(sim.select(
        pl.len().alias("n"),
        (pl.col("desvantagem_c").median()).round(2).alias("desvantagem_mediana_c"),
        (pl.col("desvantagem_c").quantile(0.25)).round(2).alias("p25_c"),
        (pl.col("desvantagem_c").quantile(0.75)).round(2).alias("p75_c"),
        (pl.col("taxa_por_cota") * 100).median().round(3).alias("taxa_c"),
        (pl.col("custo_total_pct") * 100).median().round(2).alias("custo_total_pct"),
    ))

    piores = sim.filter(pl.col("desvantagem") > 0)
    print(f"\n  Trades onde eu entraria PIOR que eles: {len(piores)}/{len(sim)} "
          f"({100*len(piores)/len(sim):.0f}%)")
    custo_mediano = float(sim["custo_total_pct"].median()) * 100
    print(f"  Custo mediano de copiar (spread cruzado + taxa): {custo_mediano:.2f}% do notional")
    print(f"\n  Para a copia dar lucro, o edge do trader copiado teria que")
    print(f"  superar {custo_mediano:.2f}% POR TRADE — e ainda sobrar algo.")
    print(f"  Referencia medida no perfil do swisstony: margem de ~1,28% sobre")
    print(f"  volume. Se o custo de copiar for maior que isso, copiar destroi")
    print(f"  exatamente o que se queria capturar.")


if __name__ == "__main__":
    main()
