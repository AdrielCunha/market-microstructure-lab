"""Projeção de resultado do market making, por escala de capital.

Isto NÃO é previsão. É aritmética de cenário: dado o spread que já medimos no
livro, quanto sobraria sob diferentes graus de seleção adversa e de giro de
capital. Serve para responder "vale a pena o esforço nessa escala?" ANTES de
gastar semanas construindo o simulador da Fase 1.

Os dois parâmetros que decidem tudo — quanto do spread sobrevive à seleção
adversa, e quantas vezes o capital gira por dia — são exatamente os que ainda
NÃO foram medidos. Por isso aparecem como cenário, e não como número único.

    python -m reports.projecao
"""

from __future__ import annotations

from dataclasses import dataclass

from analysis.fees import taker_fee
from core.db import connect

# --------------------------------------------------------------------------
# Premissas. Medidas onde deu; explicitamente arbitradas onde não deu.
# --------------------------------------------------------------------------

# MEDIDO no livro (analysis/spreads.py): spread mediano 1 a 2 centavos,
# preço mediano ~50c. Usamos o caso conservador de 1 centavo.
SPREAD_C = 1.0
PRECO_MEDIO = 0.50

# MEDIDO no catálogo (Gamma API): esportes cobram taker 5% e devolvem 15% ao
# maker. O maker não paga taxa; ele recebe.
FEE_RATE = 0.05
REBATE_RATE = 0.15

# NÃO MEDIDO — e é o parâmetro que decide tudo.
#
# Seleção adversa é o custo de ser executado justamente quando o preço vai
# contra: tua compra a 47c fecha porque alguém acabou de saber que o time
# tomou gol. Expressa como fração da receita bruta que ela consome.
#
# ATENÇÃO: acima de 100% o resultado é PREJUÍZO. Esse cenário precisa estar na
# tabela. Uma projeção cujo pior caso ainda é lucro não é projeção, é folheto
# de venda.
CENARIOS_SELECAO_ADVERSA = {
    "Leve (60%)":   0.60,   # captura +40% do bruto
    "Media (95%)":  0.95,   # captura  +5% — quase empate
    "Pesada (130%)": 1.30,  # captura -30% — PREJUIZO
}

# NÃO MEDIDO. Voltas COMPLETAS (comprou e vendeu) por dia. É onde a projeção
# mais engana: postar ordem não é ser executado. A maior parte das ordens
# paradas nunca fecha, e quando só uma perna fecha, sobra estoque — que é
# posição direcional, não market making.
GIROS_POR_DIA = (0.5, 2.0, 5.0)

# Âncora de sanidade: margem realizada pelo #1 do leaderboard, sobre volume.
# Uma projeção que supera isso está com premissa generosa demais.
MARGEM_SWISSTONY = 0.0128

# Fração do capital efetivamente exposta. O resto é folga para não travar.
UTILIZACAO = 0.60

DIAS_POR_MES = 30
VPS_MENSAL_USD = 20.0   # servidor nos EUA, necessário a partir de certa escala


@dataclass(frozen=True)
class Premissas:
    spread_c: float = SPREAD_C
    preco: float = PRECO_MEDIO
    fee_rate: float = FEE_RATE
    rebate_rate: float = REBATE_RATE
    utilizacao: float = UTILIZACAO

    @property
    def bruto_por_giro(self) -> float:
        """Receita bruta de uma volta completa, como fração do notional.

        Comprar no bid e vender no ask captura o spread inteiro. Somamos o
        rebate das duas pernas — o maker não paga taxa, ele recebe.
        """
        spread = self.spread_c / 100.0
        rebate_cota = 2 * self.rebate_rate * taker_fee(self.preco, 1.0, self.fee_rate)
        return (spread + rebate_cota) / self.preco


def resultado_mensal(capital: float, giros_dia: float, selecao_adversa: float,
                     p: Premissas, com_vps: bool) -> dict:
    """Resultado de um mês. `selecao_adversa` acima de 1.0 produz prejuízo."""
    exposto = capital * p.utilizacao
    notional_mes = exposto * giros_dia * DIAS_POR_MES
    # Cada volta completa gera duas pernas de volume (compra + venda).
    volume_mes = notional_mes * 2
    captura = 1.0 - selecao_adversa
    bruto = notional_mes * p.bruto_por_giro * captura
    custo_fixo = VPS_MENSAL_USD if com_vps else 0.0
    liquido = bruto - custo_fixo
    return {
        "notional_mes": notional_mes,
        "volume_mes": volume_mes,
        "captura": captura,
        "liquido": liquido,
        "pct_mes": 100 * liquido / capital if capital else 0.0,
        # Margem sobre volume: a unidade em que dá para comparar com o
        # swisstony e detectar premissa fantasiosa.
        "margem_volume": liquido / volume_mes if volume_mes else 0.0,
    }


def spread_medido() -> tuple[float, float, int] | None:
    """Puxa o spread real do banco, se já houver coleta."""
    try:
        con = connect(read_only=True)
        r = con.execute("""
            SELECT median(spread)*100, median(mid), count(*)
            FROM book_top WHERE source='ws' AND spread IS NOT NULL AND mid IS NOT NULL
        """).fetchone()
        con.close()
        return r if r and r[2] else None
    except Exception:
        return None


def main() -> None:
    p = Premissas()
    medido = spread_medido()

    print("=" * 74)
    print("PROJECAO DE MARKET MAKING — cenarios, nao previsao")
    print("=" * 74)
    print("\nPREMISSAS")
    if medido:
        print(f"  spread mediano medido no livro : {medido[0]:.2f} centavos "
              f"({medido[2]:,} observacoes)")
        print(f"  preco mediano medido           : {medido[1]:.2f}")
    print(f"  spread usado no calculo        : {p.spread_c:.2f}c (conservador)")
    print(f"  rebate de maker                : {p.rebate_rate*100:.0f}% da taxa de taker")
    print(f"  receita bruta por giro         : {p.bruto_por_giro*100:.2f}% do notional")
    print(f"  capital exposto                : {p.utilizacao*100:.0f}%")
    print("\n  NAO MEDIDOS (sao o cenario):")
    print("    - captura: quanto do spread sobrevive a selecao adversa")
    print("    - giro: quantas voltas o capital da por dia")

    alertas: list[str] = []

    for capital in (500.0, 5_000.0, 50_000.0):
        com_vps = capital >= 5_000.0
        print("\n" + "=" * 74)
        print(f"CAPITAL: US$ {capital:,.0f}" +
              (f"   (+ VPS US$ {VPS_MENSAL_USD:.0f}/mes)" if com_vps else
               "   (sem VPS — nessa escala ele nao se paga)"))
        print("=" * 74)
        print(f"{'voltas/dia':>10} |" +
              "|".join(f"{nome:^20}" for nome in CENARIOS_SELECAO_ADVERSA))
        print(f"{'':>10} |" +
              "|".join(f"{'US$/mes':>11}{'%/mes':>9}"
                       for _ in CENARIOS_SELECAO_ADVERSA))
        print("-" * 74)
        for giros in GIROS_POR_DIA:
            celulas = []
            for sa in CENARIOS_SELECAO_ADVERSA.values():
                r = resultado_mensal(capital, giros, sa, p, com_vps)
                celulas.append(f"{r['liquido']:>11,.0f}{r['pct_mes']:>8.1f}%")
                if r["margem_volume"] > MARGEM_SWISSTONY:
                    alertas.append(
                        f"US$ {capital:,.0f} / {giros:.1f} voltas / {sa*100:.0f}% SA "
                        f"=> margem {r['margem_volume']*100:.2f}% sobre volume")
            print(f"{giros:>10.1f} |" + "|".join(celulas))

        meio = resultado_mensal(capital, GIROS_POR_DIA[1], 0.95, p, com_vps)
        print(f"\n  Cenario do meio (2 voltas/dia, selecao adversa media): "
              f"US$ {meio['liquido']:,.0f}/mes")
        if not com_vps:
            print(f"  Um VPS de US$ {VPS_MENSAL_USD:.0f}/mes consumiria "
                  f"{100*VPS_MENSAL_USD/meio['liquido']:.0f}% disso."
                  if meio["liquido"] > 0 else
                  f"  E ja e prejuizo antes de pagar qualquer infraestrutura.")

    print("\n" + "=" * 74)
    print("ANCORA DE SANIDADE — comparacao com o #1 do leaderboard")
    print("=" * 74)
    melhor = resultado_mensal(50_000.0, max(GIROS_POR_DIA), 0.60, p, True)
    print(f"  Margem sobre volume no cenario MAIS otimista desta tabela: "
          f"{melhor['margem_volume']*100:.2f}%")
    print(f"  Margem realizada pelo swisstony (#1 do leaderboard)      : "
          f"{MARGEM_SWISSTONY*100:.2f}%")
    if alertas:
        print("\n  ALERTA: as combinacoes abaixo superam o melhor operador da")
        print("  plataforma. Quando um modelo diz isso, o errado e o modelo.\n")
        for a in alertas[:6]:
            print(f"    - {a}")
        if len(alertas) > 6:
            print(f"    ... e mais {len(alertas) - 6} combinacoes")
    else:
        print("\n  Nenhum cenario supera o #1. Isso valida a economia POR OPERACAO:")
        print("  a margem por unidade de volume aqui e conservadora.")
        print("\n  Mas cuidado com a leitura inversa: os percentuais mensais altos")
        print("  NAO vem de margem gorda, vem de GIRO. Sao o mesmo centavo ganho")
        print("  muitas vezes. Logo, a pergunta que decide o resultado nao e")
        print("  'quanto ganho por operacao?' — e 'quantas operacoes completas")
        print("  eu realmente consigo fechar por dia?'. Essa e a incognita.")

    print("\n" + "=" * 74)
    print("COMO LER ISTO")
    print("=" * 74)
    print("""
  1. A coluna "Pesada" existe porque ela e um resultado possivel, nao um
     exercicio. Comecar perdendo dinheiro e o caso base de quem entra
     competindo contra operadores estabelecidos.

  2. "Volta completa" pressupoe comprar E vender. Postar ordem nao e ser
     executado. Quando so uma perna fecha, o que sobra e estoque — ou seja,
     aposta direcional, que e exatamente o que market making deveria evitar.
     A tabela e otimista nesse ponto por construcao.

  3. O resultado e proporcional ao giro, e o giro e a premissa mais fragil.
     Trate as linhas de baixo com muito mais desconfianca que as de cima.

  4. Nenhuma celula desconta imposto (Brasil), nem o tempo de quem opera.

  5. Escala importa mais que esperteza: a mesma captura rende o mesmo
     percentual em qualquer capital. O que muda e se o valor absoluto paga o
     esforco. Em US$ 500, mesmo um cenario bom nao paga o tempo investido —
     nessa escala o retorno e aprendizado, nao renda.
""")


if __name__ == "__main__":
    main()
