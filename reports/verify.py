"""Verificação de integridade dos dados coletados.

Roda ANTES de acreditar em qualquer número do Gate 0. Um coletor com parser
errado produz análises perfeitamente formatadas e completamente falsas — estas
checagens existem para pegar isso.

    python -m reports.verify
"""

from __future__ import annotations

from core.db import connect

TOL_CENTAVOS = 1.0        # divergência aceitável entre WS e REST, em centavos
MAX_AVISOS_POR_HORA = 4.0  # reconexão é normal; o sintoma é a frequência


def check(nome: str, ok: bool, detalhe: str) -> bool:
    print(f"  [{'OK  ' if ok else 'FALHA'}] {nome}")
    if detalhe:
        print(f"          {detalhe}")
    return ok


def main(con=None) -> None:
    own = con is None
    con = con or connect(read_only=True)
    print("=" * 70)
    print("VERIFICACAO DE INTEGRIDADE DA COLETA")
    print("=" * 70)
    resultados = []

    # 1. A série derivada do WebSocket bate com o livro real?
    # Para cada snapshot REST de auditoria, comparamos com a última observação
    # do WS logo antes. Divergência sistemática = parser quebrado.
    print("\n1) WebSocket x snapshot REST (o parser esta correto?)")
    cmp = con.execute("""
        SELECT count(*) AS n,
               round(median(abs(r.best_ask - w.best_ask)) * 100, 3) AS dif_ask_c,
               round(median(abs(r.best_bid - w.best_bid)) * 100, 3) AS dif_bid_c,
               round(max(abs(r.best_ask - w.best_ask)) * 100, 3)    AS pior_c
        FROM (SELECT * FROM book_top WHERE source = 'rest_audit') r
        ASOF JOIN (SELECT * FROM book_top WHERE source = 'ws') w
             ON r.token_id = w.token_id AND r.ts_local >= w.ts_local
        WHERE r.best_ask IS NOT NULL AND w.best_ask IS NOT NULL
    """).fetchone()
    if not cmp or not cmp[0]:
        resultados.append(check("comparacao WS x REST", False,
                                "sem pares comparaveis — rode o coletor por mais de 15min"))
    else:
        ok = (cmp[1] or 0) <= TOL_CENTAVOS
        resultados.append(check(
            "comparacao WS x REST", ok,
            f"n={cmp[0]}  dif mediana ask={cmp[1]}c  bid={cmp[2]}c  pior={cmp[3]}c"))

    # 2. Spread negativo é impossível num livro válido.
    print("\n2) Sanidade do topo de livro")
    neg = con.execute("SELECT count(*) FROM book_top WHERE spread < 0").fetchone()[0]
    resultados.append(check("nenhum spread negativo", neg == 0, f"{neg} violacoes"))

    fora = con.execute("""
        SELECT count(*) FROM book_top
        WHERE best_bid < 0 OR best_bid > 1 OR best_ask < 0 OR best_ask > 1
    """).fetchone()[0]
    resultados.append(check("precos dentro de [0,1]", fora == 0, f"{fora} violacoes"))

    # 3. Continuidade da série.
    #
    # A versão anterior contava lacunas com `lag()` sobre timestamps distintos e
    # devolvia números impossíveis (3.198 lacunas de 2min numa janela de 225min
    # — não cabem). Contar MINUTOS SEM DADO é robusto, direto de interpretar e
    # imune a sutileza de window function.
    #
    # E lacuna nem sempre é defeito: toda vez que o coletor é desligado e
    # religado sobra um buraco legítimo. Por isso comparamos as lacunas com o
    # número de reinícios registrados no log.
    print("\n3) Continuidade da serie")
    cont = con.execute("""
        WITH minutos AS (
            -- CAST explícito: em DuckDB `/` é divisão real e produziria
            -- minutos fracionários, que poluem a contagem e a mensagem.
            SELECT DISTINCT CAST(ts_local / 60000 AS BIGINT) AS m
            FROM book_top WHERE source = 'ws'
        ), faixa AS (
            SELECT min(m) AS a, max(m) AS b, count(*) AS com_dado FROM minutos
        )
        SELECT (b - a + 1) AS total, com_dado, (b - a + 1) - com_dado AS sem_dado
        FROM faixa
    """).fetchone()
    reinicios = con.execute("""
        SELECT count(*) FROM collector_log WHERE message = 'dashboard no ar'
    """).fetchone()[0]
    janela = float(cont[0] or 0)
    sem_dado = int(cont[2] or 0)
    # Cada reinício explica uma lacuna. Sem reinício, qualquer minuto vazio é
    # queda de conexão não detectada.
    inexplicado = max(0, sem_dado - max(reinicios - 1, 0) * 60)
    resultados.append(check(
        "cobertura temporal", sem_dado == 0 or inexplicado == 0,
        f"{cont[1]} de {cont[0]} minutos com dado; {sem_dado} vazios; "
        f"{reinicios} inicios do coletor no log"))
    if sem_dado:
        print(f"          (lacuna e esperada entre sessoes — o coletor foi "
              f"religado {reinicios}x)")

    # 4. Completude dos eventos negative-risk — a trava que impede medir
    # "arbitragem" que na verdade e perna faltando.
    print("\n4) Completude dos eventos negative-risk")
    inc = con.execute("""
        SELECT count(*) FROM (
            SELECT event_id FROM markets
            WHERE selected AND neg_risk AND outcome_index = 0
            GROUP BY 1 HAVING count(*) <> max(event_n_outcomes)
        )
    """).fetchone()[0]
    resultados.append(check("todo evento negRisk esta completo", inc == 0,
                            f"{inc} eventos com perna faltando"))

    # 5. Erros registrados pelo coletor.
    # Reconexão de WebSocket é operação normal, não defeito — o que importa é a
    # frequência. Exigir zero produzia alarme falso a cada queda isolada.
    print("\n5) Log do coletor")
    warns = con.execute(
        "SELECT count(*) FROM collector_log WHERE level <> 'info'").fetchone()[0]
    horas = (janela or 0) / 60.0
    por_hora = warns / horas if horas else float(warns)
    resultados.append(check(
        "avisos em ritmo normal", por_hora <= MAX_AVISOS_POR_HORA,
        f"{warns} avisos em {horas:.1f}h = {por_hora:.1f}/h "
        f"(limite {MAX_AVISOS_POR_HORA}/h)"))
    if warns:
        for msg, n in con.execute("""
            SELECT message, count(*) FROM collector_log WHERE level <> 'info'
            GROUP BY 1 ORDER BY 2 DESC LIMIT 5
        """).fetchall():
            print(f"            - {msg}: {n}x")

    if own:
        con.close()
    print("\n" + "=" * 70)
    falhas = [r for r in resultados if not r]
    if falhas:
        print(f"{len(falhas)} verificacao(oes) FALHARAM.")
        print("Corrija antes de tratar os numeros do Gate 0 como validos.")
    else:
        print("Todas as verificacoes passaram. Os dados podem ser usados.")


if __name__ == "__main__":
    main()
