"""Relatório do Gate 0 — o veredito que decide se o projeto continua.

Critério definido no plano:

    Existe pelo menos UM nicho onde o edge bruto medido supera o custo total
    (taxa + spread + gás + capital travado) por margem de >= 2x, e a
    oportunidade sobrevive >= 2 segundos.

Se falhar, o projeto para. Isso é sucesso, não fracasso: custou tempo em vez de
dinheiro, e a resposta é definitiva.

    python -m reports.gate0
"""

from __future__ import annotations

import polars as pl

from analysis import copyability, negrisk, spreads
from analysis.fees import CostModel, taker_fee
from core import config
from core.db import connect

LINHA = "=" * 72


def _sec(titulo: str) -> None:
    print(f"\n{LINHA}\n{titulo}\n{LINHA}")


def cobertura(con) -> dict:
    b = con.execute("""
        SELECT count(*), count(DISTINCT token_id),
               (max(ts_local) - min(ts_local)) / 3600000.0
        FROM book_top WHERE source = 'ws'
    """).fetchone()
    t = con.execute("""
        SELECT count(*) FILTER (WHERE NOT is_backfill), count(DISTINCT wallet)
        FROM wallet_trades
    """).fetchone()
    return {"obs_livro": b[0], "tokens": b[1], "horas": b[2] or 0.0,
            "trades_novos": t[0], "carteiras": t[1]}


def criterio_arbitragem(min_surv: float, con=None) -> tuple[bool, str, dict]:
    r = negrisk.analisar(con=con)
    if not r["achados"]:
        return False, "sem dados", {}
    df = pl.DataFrame(r["achados"])
    eps = int(df["eps_compra"].sum() + df["eps_venda"].sum())
    dur_max = float(df["dur_max_s"].max() or 0)
    dados = {
        "eventos_completos": r["eventos_analisados"],
        "eventos_incompletos_ignorados": r["eventos_incompletos_ignorados"],
        "soma_asks_mediana": float(df["soma_asks_mediana"].median()),
        "soma_asks_melhor": float(df["soma_asks_min"].min()),
        "custo_cesta": float(df["custo_compra"].median()),
        "episodios": eps,
        "duracao_max_s": dur_max,
    }
    passou = eps > 0 and dur_max >= min_surv
    razao = (f"{eps} episodios, duracao maxima {dur_max:.1f}s"
             if eps else "nenhum desvio maior que o custo de captura")
    return passou, razao, dados


def criterio_market_making(ratio_min: float, con=None) -> tuple[str, str, dict]:
    """Meio spread + rebate cobre gás e carrego com folga?

    Este critério NUNCA devolve PASS, e isso é deliberado.

    O que dá para medir só com o livro é a receita BRUTA do maker. O custo que
    realmente decide se fazer mercado dá lucro — seleção adversa (ser executado
    justamente quando o preço vai contra) e risco de estoque — não aparece em
    lugar nenhum no livro: ele só se manifesta nos fills, que só existem quando
    há ordem de verdade postada.

    Declarar PASS aqui seria produzir o falso positivo clássico: uma folga de
    centenas de vezes sobre um custo que exclui o custo dominante. O veredito
    honesto é INCONCLUSIVO, resolvido na Fase 1 com paper trading.
    """
    df = spreads.load(con)
    if df.is_empty():
        return "FAIL", "sem dados", {}
    modelo = CostModel()
    linhas = spreads.receita_liquida_do_maker(df)
    melhor = linhas.sort("bruto_pct_notional", descending=True).head(1)
    faixa = melhor["faixa"][0]
    bruto_pct = float(melhor["bruto_pct_notional"][0]) / 100

    # Custo do maker: não paga taxa de taker, mas paga gás e carrego.
    preco = 0.5
    cotas = 1000.0
    custo = (modelo.gas_usd * 2 + modelo.carry_cost(preco * cotas, 1.0)) / (preco * cotas)
    ratio = bruto_pct / custo if custo else float("inf")
    dados = {"melhor_faixa": faixa, "bruto_pct": bruto_pct * 100,
             "custo_pct": custo * 100, "ratio": ratio,
             "spread_mediano_c": float(df["spread"].median()) * 100}
    if ratio < ratio_min:
        # Se nem a receita bruta cobre gás e carrego, não há o que investigar.
        return "FAIL", f"{faixa}: bruto {bruto_pct*100:.2f}% nao cobre nem o custo mecanico", dados
    return ("INCONCLUSIVO",
            f"{faixa}: receita bruta {bruto_pct*100:.2f}% do notional — falta medir "
            f"selecao adversa, que so aparece com ordem postada", dados)


def criterio_copia(con) -> tuple[bool, str, dict]:
    atraso = con.execute("""
        SELECT count(*), min((ts_seen-ts_trade)/1000.0), median((ts_seen-ts_trade)/1000.0)
        FROM wallet_trades WHERE NOT is_backfill
    """).fetchone()
    if not atraso[0]:
        return False, "sem trades novos", {}
    df = con.execute(copyability.COPIA).pl()
    dados = {"n_trades": atraso[0], "atraso_min_s": atraso[1],
             "atraso_mediano_s": atraso[2], "n_simulados": len(df)}
    if df.is_empty():
        return False, "sem livro casado com os trades", dados
    sim = copyability.simular(df, CostModel())
    custo = float(sim["custo_total_pct"].median()) * 100
    pior = float((sim["desvantagem"] > 0).mean()) * 100
    # Margem sobre volume medida no perfil do swisstony (ver plano).
    margem_alvo = 1.28
    dados.update({"custo_copia_pct": custo, "pct_pior": pior,
                  "margem_alvo_pct": margem_alvo,
                  "desvantagem_mediana_c": float(sim["desvantagem_c"].median())})
    return custo < margem_alvo, f"copiar custa {custo:.2f}% vs margem alvo {margem_alvo}%", dados


def main(con=None) -> None:
    cfg = config.load()["gate0"]
    ratio_min = float(cfg.get("min_edge_over_cost_ratio", 2.0))
    min_surv = float(cfg.get("min_survival_seconds", 2.0))
    # Conexao injetada = coletor rodando; senao abre a nossa.
    own = con is None
    con = con or connect(read_only=True)

    cov = cobertura(con)
    print(LINHA)
    print("RELATORIO GATE 0 — Polymarket")
    print(LINHA)
    print(f"Base coletada: {cov['obs_livro']:,} observacoes de livro em "
          f"{cov['tokens']} tokens")
    print(f"               {cov['horas']:.1f} horas de janela")
    print(f"               {cov['trades_novos']} trades novos de {cov['carteiras']} carteiras")
    if cov["horas"] < 24:
        print("\n  AVISO: menos de 24h de coleta. Os numeros abaixo indicam direcao,")
        print("  nao servem como veredito final. O criterio do plano pressupoe")
        print("  semanas de serie contínua.")

    resultados: list[tuple[str, str]] = []

    _sec("CRITERIO 1 — Arbitragem negative-risk")
    ok, razao, d = criterio_arbitragem(min_surv, con)
    resultados.append(("Arbitragem negative-risk", "PASS" if ok else "FAIL"))
    if d:
        print(f"  eventos completos analisados   : {d['eventos_completos']}")
        print(f"  eventos incompletos ignorados  : {d['eventos_incompletos_ignorados']}")
        print(f"  soma dos asks (mediana)        : ${d['soma_asks_mediana']:.4f}  "
              f"(o justo e $1,0000)")
        print(f"  melhor caso observado          : ${d['soma_asks_melhor']:.4f}")
        print(f"  custo de montar a cesta        : ${d['custo_cesta']:.4f} por $1 de payoff")
        print(f"  limiar para virar arbitragem   : soma < ${1-d['custo_cesta']:.4f}")
        print(f"  episodios encontrados          : {d['episodios']}")
    print(f"  -> {'PASSOU' if ok else 'FALHOU'}: {razao}")

    _sec("CRITERIO 2 — Market making (meio spread + rebate)")
    veredito2, razao2, d2 = criterio_market_making(ratio_min, con)
    resultados.append(("Market making", veredito2))
    if d2:
        print(f"  spread mediano do livro        : {d2['spread_mediano_c']:.2f} centavos")
        print(f"  melhor faixa de preco          : {d2['melhor_faixa']}")
        print(f"  receita BRUTA por rodada       : {d2['bruto_pct']:.2f}% do notional")
        print(f"  custo mecanico (gas + carrego) : {d2['custo_pct']:.3f}% do notional")
        print()
        print("  Por que isso NAO vira PASS mesmo com folga enorme:")
        print("  o custo que decide market making e a SELECAO ADVERSA — sua ordem")
        print("  parada e executada preferencialmente quando o preco esta prestes")
        print("  a andar contra voce. Esse custo nao existe no livro; ele so")
        print("  aparece nos fills, e fill so existe com ordem postada de verdade.")
        print("  Medir isso e exatamente o trabalho da Fase 1 (paper trading).")
    print(f"  -> {veredito2}: {razao2}")

    _sec("CRITERIO 3 — Copiar carteiras do leaderboard")
    ok3, razao3, d3 = criterio_copia(con)
    resultados.append(("Copy trading", "PASS" if ok3 else "FAIL"))
    if d3:
        print(f"  trades novos observados        : {d3['n_trades']}")
        print(f"  atraso ATE O TRADE APARECER    : minimo {d3['atraso_min_s']:.0f}s, "
              f"mediana {d3['atraso_mediano_s']:.0f}s")
        print("  (o poll roda a cada 3s — o atraso e do indexador do Polymarket,")
        print("   nao da minha infraestrutura, e nenhum servidor nos EUA o reduz)")
        if "custo_copia_pct" in d3:
            print(f"  casos simulados com livro      : {d3['n_simulados']}")
            print(f"  desvantagem mediana de entrada : {d3['desvantagem_mediana_c']:.2f} centavos/cota")
            print(f"  entrada pior que a deles em    : {d3['pct_pior']:.0f}% dos casos")
            print(f"  custo total de copiar          : {d3['custo_copia_pct']:.2f}% do notional")
            print(f"  margem do trader copiado       : {d3['margem_alvo_pct']:.2f}% sobre volume")
    print(f"  -> {'PASSOU' if ok3 else 'FALHOU'}: {razao3}")

    if own:
        con.close()

    _sec("VEREDITO")
    for nome, v in resultados:
        print(f"  [{v:^13}] {nome}")
    print()
    passes = [n for n, v in resultados if v == "PASS"]
    inconclusivos = [n for n, v in resultados if v == "INCONCLUSIVO"]

    if passes:
        print(f"  GATE 0: PASSOU — edge acima do custo em: {', '.join(passes)}.")
        print("  Proximo passo: Fase 1, paper trading do nicho aprovado.")
    elif inconclusivos:
        print("  GATE 0: PARCIAL — nenhuma tese passou so com dados de livro,")
        print(f"  mas segue viva para teste: {', '.join(inconclusivos)}.")
        print("  As teses que dependem apenas de preco publico (arbitragem e")
        print("  copia) FALHARAM. A unica sobrevivente exige postar ordem para")
        print("  ser avaliada — ou seja, so a Fase 1 responde.")
        print("  Proximo passo: Fase 1 restrita a essa tese, sem dinheiro real.")
    else:
        print("  GATE 0: NAO PASSOU com os dados atuais.")
        print("  Nenhuma das tres teses sobreviveu ao custo real de executa-la.")

    if cov["horas"] < 24:
        print()
        print("  Lembrete: a janela coletada ainda e curta. Nenhuma destas")
        print("  conclusoes e definitiva antes de semanas de serie continua.")


if __name__ == "__main__":
    main()
