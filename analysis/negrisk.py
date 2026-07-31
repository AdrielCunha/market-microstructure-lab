"""Arbitragem em eventos negative-risk: existe, e dura quanto tempo?

Num evento negative-risk os resultados são mutuamente exclusivos e exaustivos —
exatamente um acontece. Logo a soma dos preços dos "Yes" tem que valer $1:

    soma dos melhores ASKS < $1  → compro todos, recebo $1 na resolução
    soma dos melhores BIDS > $1  → vendo todos, pago $1 na resolução

O desvio bruto não é lucro. Só vira lucro se sobreviver a:
  - taxa de taker em cada perna;
  - gás;
  - capital travado até a resolução;
  - e ao tempo: se a janela fecha antes dos ~230ms de latência desta máquina,
    a oportunidade não é executável daqui.

É por isso que este módulo mede DURAÇÃO, não só existência.

    python -m analysis.negrisk
"""

from __future__ import annotations

import polars as pl

from analysis.fees import CostModel, taker_fee
from core import config
from core.db import connect

# Só o token "Yes" de cada mercado entra na soma — é a perna que representa
# "este resultado acontece".
# HAVING count(*) = max(event_n_outcomes) é a trava de completude: se faltar
# uma perna, a soma dos preços não tem que dar $1 e qualquer desvio medido é
# artefato da coleta, não arbitragem.
EVENTOS = """
SELECT event_id, event_title, count(*) AS n_resultados,
       max(fee_rate) AS fee_rate, max(fee_rebate_rate) AS rebate,
       min(end_date) AS end_date, sum(liquidity_usd) AS liquidez
FROM markets
WHERE selected AND neg_risk AND outcome_index = 0 AND event_id IS NOT NULL
GROUP BY 1, 2
HAVING count(*) >= 2 AND count(*) = max(event_n_outcomes)
ORDER BY liquidez DESC
"""

EVENTOS_INCOMPLETOS = """
SELECT count(*) FROM (
    SELECT event_id FROM markets
    WHERE selected AND neg_risk AND outcome_index = 0 AND event_id IS NOT NULL
    GROUP BY 1 HAVING count(*) <> max(event_n_outcomes)
)
"""

TOKENS_DO_EVENTO = """
SELECT token_id, question FROM markets
WHERE event_id = ? AND neg_risk AND outcome_index = 0
"""

SERIE = """
SELECT token_id, ts_local, best_ask, best_bid
FROM book_top
WHERE token_id IN ({}) AND source = 'ws'
  AND best_ask IS NOT NULL AND best_bid IS NOT NULL
ORDER BY ts_local
"""


def serie_do_evento(con, token_ids: list[str]) -> pl.DataFrame:
    """Alinha os livros dos N resultados numa única linha do tempo.

    Cada token atualiza em momentos diferentes; para somar preços num instante
    é preciso arrastar o último valor conhecido de cada um (forward fill). Antes
    do primeiro tick de algum token a soma é indefinida — essas linhas caem.
    """
    marks = ", ".join("?" * len(token_ids))
    df = con.execute(SERIE.format(marks), token_ids).pl()
    if df.is_empty():
        return df

    wide = (df.pivot(on="token_id", index="ts_local",
                     values=["best_ask", "best_bid"], aggregate_function="last")
              .sort("ts_local")
              .fill_null(strategy="forward"))

    ask_cols = [c for c in wide.columns if c.startswith("best_ask")]
    bid_cols = [c for c in wide.columns if c.startswith("best_bid")]
    if len(ask_cols) < 2:
        return pl.DataFrame()

    return (wide.drop_nulls()
                .with_columns(
                    pl.sum_horizontal(ask_cols).alias("soma_asks"),
                    pl.sum_horizontal(bid_cols).alias("soma_bids"),
                    pl.lit(len(ask_cols)).alias("n_pernas"))
                .select("ts_local", "soma_asks", "soma_bids", "n_pernas"))


def episodios(serie: pl.DataFrame, coluna: str, comparador: str,
              limiar: float) -> pl.DataFrame:
    """Agrupa observações consecutivas fora do limiar em episódios contínuos.

    Um episódio começa quando a soma cruza o limiar e termina quando volta. A
    duração é o que decide se a oportunidade é alcançável daqui.
    """
    if serie.is_empty():
        return pl.DataFrame()
    cond = (pl.col(coluna) < limiar) if comparador == "<" else (pl.col(coluna) > limiar)
    marc = (serie.with_columns(cond.alias("dentro"))
                 .with_columns((pl.col("dentro") != pl.col("dentro").shift(1))
                               .fill_null(True).cum_sum().alias("grupo")))
    return (marc.filter(pl.col("dentro"))
                .group_by("grupo")
                .agg(
                    pl.col("ts_local").min().alias("inicio"),
                    pl.col("ts_local").max().alias("fim"),
                    pl.len().alias("ticks"),
                    pl.col(coluna).min().alias("min"),
                    pl.col(coluna).max().alias("max"),
                    pl.col("n_pernas").first().alias("n_pernas"))
                .with_columns(((pl.col("fim") - pl.col("inicio")) / 1000.0)
                              .alias("duracao_s"))
                .sort("inicio"))


def custo_da_cesta(soma_precos: float, n_pernas: int, fee_rate: float,
                   dias: float, modelo: CostModel, cotas: float = 1000.0) -> float:
    """Custo de montar a cesta inteira, expresso por $1 de payoff.

    Uma cota de cada perna paga exatamente $1 na resolução, então a unidade
    natural é "por cota". Cuidado: taxa e carrego escalam com o tamanho, mas o
    GÁS é fixo por transação — dividi-lo por uma cota faria um custo de $0,02
    virar 2% do payoff e mataria qualquer oportunidade no papel. Por isso o
    cálculo é feito para uma cesta de tamanho realista e só então normalizado.
    """
    preco_medio = soma_precos / n_pernas if n_pernas else 0.5
    taxa = n_pernas * taker_fee(preco_medio, cotas, fee_rate)
    gas = modelo.gas_usd * n_pernas          # fixo, independe do tamanho
    carrego = modelo.carry_cost(soma_precos * cotas, dias)
    return (taxa + gas + carrego) / cotas


def analisar(limite_eventos: int = 40, con=None) -> dict:
    """Analisa os eventos negative-risk.

    `con` permite injetar a conexão do coletor: com a coleta rodando, o DuckDB
    trava o arquivo e abrir uma segunda conexão de fora é impossível.
    """
    own = con is None
    con = con or connect(read_only=True)
    modelo = CostModel()
    gate = config.load()["gate0"]
    eventos = con.execute(EVENTOS).fetchall()
    incompletos = con.execute(EVENTOS_INCOMPLETOS).fetchone()[0]

    achados: list[dict] = []
    total_ticks = 0
    eventos_com_serie = 0

    for event_id, titulo, n_res, fee_rate, rebate, end_date, liq in eventos[:limite_eventos]:
        tokens = [r[0] for r in con.execute(TOKENS_DO_EVENTO, [event_id]).fetchall()]
        if len(tokens) < 2:
            continue
        serie = serie_do_evento(con, tokens)
        if serie.is_empty():
            continue
        eventos_com_serie += 1
        total_ticks += len(serie)

        dias = 1.0
        n_pernas = int(serie["n_pernas"][0])

        # Compra da cesta: preciso que a soma dos asks + custo fique abaixo de $1.
        custo_compra = custo_da_cesta(float(serie["soma_asks"].median()), n_pernas,
                                      fee_rate or 0.05, dias, modelo)
        eps_compra = episodios(serie, "soma_asks", "<", 1.0 - custo_compra)
        # Venda da cesta: soma dos bids tem que superar $1 + custo.
        custo_venda = custo_da_cesta(float(serie["soma_bids"].median()), n_pernas,
                                     fee_rate or 0.05, dias, modelo)
        eps_venda = episodios(serie, "soma_bids", ">", 1.0 + custo_venda)

        achados.append({
            "event_id": event_id,
            "titulo": (titulo or "")[:48],
            "n_pernas": n_pernas,
            "ticks": len(serie),
            "soma_asks_min": round(float(serie["soma_asks"].min()), 4),
            "soma_asks_mediana": round(float(serie["soma_asks"].median()), 4),
            "soma_bids_max": round(float(serie["soma_bids"].max()), 4),
            "custo_compra": round(custo_compra, 4),
            "eps_compra": len(eps_compra),
            "eps_venda": len(eps_venda),
            "dur_max_s": round(float(
                max(eps_compra["duracao_s"].max() if len(eps_compra) else 0,
                    eps_venda["duracao_s"].max() if len(eps_venda) else 0) or 0), 2),
        })

    if own:
        con.close()
    return {
        "eventos_analisados": eventos_com_serie,
        "eventos_incompletos_ignorados": incompletos,
        "ticks": total_ticks,
        "achados": achados,
        "min_survival_s": float(gate.get("min_survival_seconds", 2.0)),
    }


def main(con=None) -> None:
    r = analisar(con=con)
    if not r["achados"]:
        print("Sem serie suficiente. Rode `python -m collector.run` por mais tempo.")
        return

    df = pl.DataFrame(r["achados"]).sort("ticks", descending=True)
    print(f"Eventos negative-risk COMPLETOS com serie: {r['eventos_analisados']}  "
          f"({r['ticks']:,} instantes alinhados)")
    print(f"Eventos incompletos ignorados: {r['eventos_incompletos_ignorados']} "
          f"(faltava perna; somar subconjunto fabrica arb falsa)\n")

    print("=== SOMA DOS RESULTADOS (deveria valer exatamente $1) ===")
    print(df.select("titulo", "n_pernas", "ticks", "soma_asks_mediana",
                    "soma_asks_min", "soma_bids_max").head(12))

    total_compra = int(df["eps_compra"].sum())
    total_venda = int(df["eps_venda"].sum())
    print(f"\n=== OPORTUNIDADES LIQUIDAS DE CUSTO ===")
    print(f"  episodios de compra de cesta (< $1 - custo) : {total_compra}")
    print(f"  episodios de venda de cesta  (> $1 + custo) : {total_venda}")

    if total_compra + total_venda == 0:
        mediana = float(df["soma_asks_mediana"].median())
        menor = float(df["soma_asks_min"].min())
        custo = float(df["custo_compra"].median())
        print("\n  NENHUMA. A leitura correta disso:")
        print(f"    - soma mediana dos asks : ${mediana:.4f}  (deveria ser $1,0000)")
        print(f"    - melhor caso observado : ${menor:.4f}")
        print(f"    - custo de montar cesta : ${custo:.4f} por $1 de payoff "
              f"(cesta de 1.000 cotas)")
        print(f"    - limiar para ser arb   : soma dos asks < ${1-custo:.4f}")
        print(f"    O desvio existe, mas e MENOR que o custo de captura-lo.")
        print(f"    Arb aparente < custo = nao e arb.")
    else:
        sobrev = df.filter((pl.col("eps_compra") + pl.col("eps_venda")) > 0)
        print("\n  Eventos com oportunidade:")
        print(sobrev.select("titulo", "eps_compra", "eps_venda", "dur_max_s"))
        print(f"\n  Criterio do Gate 0: sobreviver >= {r['min_survival_s']}s")
        print(f"  Duracao maxima observada: {float(df['dur_max_s'].max()):.2f}s")


if __name__ == "__main__":
    main()
