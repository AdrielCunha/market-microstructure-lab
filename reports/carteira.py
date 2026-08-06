"""Quanto teríamos NA MÃO se parássemos agora.

O `patrimonio` que os painéis mostram avalia a posição aberta pelo preço médio
do livro (o *mid*). Isso é a convenção contábil, e está certa — mas **não é o
dinheiro que entraria na conta**, por dois motivos que não são detalhe:

1. **Ninguém sai no mid.** Para desmontar posição comprada é preciso vender no
   melhor BID, que é mais baixo. Vendida, comprar no melhor ASK, mais alto. A
   diferença é o spread inteiro, não metade.
2. **Sair é ser taker.** Quem atravessa o spread paga taxa. Medido nas saídas
   forçadas: **~3,4% do notional**.

Some-se um terceiro problema, que não é de saída e sim de premissa: boa parte
do resultado vem de **rebate**, e a fórmula da taxa do Polymarket **nunca foi
confirmada** (`fee_rate_bps` veio 0 em 957 de 957 execuções observadas). Por
isso este módulo também devolve o número SEM rebate — o piso do piso.

Ordem dos números, do mais otimista ao mais honesto:

    patrimonio        capital + realizado + rebates - taxas + (posicao no mid)
    valor_de_saida    idem, mas a posicao avaliada ao preco de SAIR, com taxa
    sem_rebate        idem, jogando fora todo o rebate nao verificado
"""

from __future__ import annotations

import duckdb

from analysis.fees import taker_fee
from core import janela

# Regime de taxa do Polymarket quando o catálogo não diz. Esportes cobram
# `rate=0.05`; é o caso da maioria dos mercados que coletamos.
TAXA_PADRAO = 0.05


def _precos_de_saida(con: duckdb.DuckDBPyConnection,
                     tokens: list[str]) -> dict[str, tuple]:
    """Melhor bid/ask atual e a taxa de cada token."""
    if not tokens:
        return {}
    marcas = ", ".join("?" * len(tokens))
    linhas = con.execute(f"""
        SELECT b.token_id,
               last(b.best_bid ORDER BY b.ts_local),
               last(b.best_ask ORDER BY b.ts_local),
               max(m.fee_rate)
        FROM book_top b
        LEFT JOIN markets m USING (token_id)
        WHERE b.token_id IN ({marcas})
          {janela.clausula('b.ts_local')}
        GROUP BY b.token_id
    """, list(tokens)).fetchall()
    return {r[0]: (r[1], r[2], r[3] or TAXA_PADRAO) for r in linhas}


def avaliar(con: duckdb.DuckDBPyConnection, ledger) -> dict:
    """Avalia a carteira de um motor pelos três critérios.

    Posição sem preço no livro entra em `sem_preco` e é avaliada pelo custo
    médio — nunca inventamos cotação, mas também não podemos sumir com ela.
    """
    posicoes = ledger.posicoes()
    precos = _precos_de_saida(con, list(posicoes))

    valor_saida = 0.0        # o que a posicao renderia ao ser desmontada
    custo_saida = 0.0        # taxa de taker para sair de tudo
    valor_mid = 0.0          # a mesma posicao avaliada pela convencao contabil
    sem_preco = 0

    for token, qtd in posicoes.items():
        custo = ledger.custo_medio(token)
        bid, ask, rate = precos.get(token, (None, None, TAXA_PADRAO))
        mid = None if bid is None or ask is None else (bid + ask) / 2

        if qtd > 0:
            saida = bid          # comprado desmonta VENDENDO no bid
        else:
            saida = ask          # vendido desmonta COMPRANDO no ask

        if saida is None:
            sem_preco += 1
            saida = custo        # sem livro, o melhor palpite honesto e o custo
            mid = custo

        valor_saida += (saida - custo) * qtd
        valor_mid += ((mid if mid is not None else custo) - custo) * qtd
        custo_saida += taker_fee(saida, abs(qtd), rate)

    base = ledger.capital_inicial + ledger.realizado + ledger.rebates - ledger.taxas
    patrimonio = base + valor_mid
    liquido = base + valor_saida - custo_saida

    return {
        "capital_inicial": ledger.capital_inicial,
        "realizado": ledger.realizado,
        "rebates": ledger.rebates,
        "taxas_pagas": ledger.taxas,
        "posicoes": len(posicoes),
        "sem_preco": sem_preco,

        # avaliação da posição aberta, pelos dois critérios
        "nao_realizado_mid": valor_mid,
        "nao_realizado_saida": valor_saida,
        "custo_para_sair": custo_saida,

        # os três totais
        "patrimonio": patrimonio,
        "valor_de_saida": liquido,
        "sem_rebate": liquido - ledger.rebates,

        # o que separa a conta contábil da conta de bolso
        "otimismo": patrimonio - liquido,
    }
