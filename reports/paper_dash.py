"""Dashboard do paper trading — separado do de coleta de propósito.

O de coleta responde "o instrumento está funcionando?".
Este responde "estamos ganhando ou perdendo dinheiro?".

Cada número aparece com a explicação do lado, na própria tela. Um painel de
resultado financeiro que exige manual é um painel que vai ser mal interpretado.
"""

from __future__ import annotations

import html
import time

import duckdb

from analysis.markout import HORIZONTES_S, _consulta
from reports import carteira, nav

REFRESH_S = 15

# Motor que representa a realidade: regra honesta (`negocio`, so conta negocio
# impresso) na latencia que de fato temos depois da mudanca para Londres.
REFERENCIA = "maker_negocio_lat15"

# Explicação de cada métrica, mostrada junto do valor. O texto é parte do
# produto: sem ele, "caixa alto" já foi confundido com "lucro" uma vez.
GLOSSARIO = {
    "capital_inicial": ("Capital inicial",
        "Dinheiro de partida da simulacao. Nao e dinheiro real."),
    "patrimonio": ("Quanto teriamos agora",
        "Capital inicial + lucro realizado + rebates + lucro nao realizado. "
        "E o que estaria na carteira se tudo fosse avaliado a preco de agora."),
    "pnl_total": ("Resultado total",
        "Patrimonio menos capital inicial. Positivo = lucro, negativo = prejuizo."),
    "realizado": ("Lucro REALIZADO",
        "De operacao FECHADA: comprou e vendeu. Este dinheiro ja e seu e nao "
        "some se o preco andar."),
    "nao_realizado": ("Lucro NAO realizado",
        "Da posicao AINDA ABERTA, avaliada pelo preco atual. E promessa: vira "
        "po se o preco andar contra."),
    "rebates": ("Rebates recebidos",
        "O Polymarket devolve parte da taxa a quem deixa ordem parada no livro. "
        "Maker nao paga taxa; recebe."),
    "capital_travado": ("Capital travado",
        "Quanto do capital esta preso dentro das posicoes abertas. Nao da para "
        "usar em outra coisa ate fechar."),
    "caixa_livre": ("Caixa livre",
        "O que sobra do capital para abrir posicao nova."),
    "drawdown_max": ("Pior queda",
        "Maior tombo do patrimonio desde o topo. Mede o susto, nao o resultado."),
    "fechamentos": ("Operacoes fechadas",
        "Quantas vezes uma posicao foi zerada. Poucos fechamentos com muitas "
        "execucoes = estoque acumulando, nao market making."),
    "realizado_livro": ("Realizado NO LIVRO",
        "Lucro de posicao desmontada achando contraparte: comprou e vendeu. "
        "ISTO e market making de verdade — o spread capturado."),
    "taxas": ("Taxas pagas (saida forcada)",
        "Taxa de taker paga para atravessar o spread e zerar estoque antes do "
        "mercado resolver. Maker nao paga taxa — este numero e o PRECO de nao "
        "ficar com o mico na mao. Comparar com 'Realizado NA RESOLUCAO': se a "
        "taxa for muito menor, a saida forcada valeu a pena."),
    "realizado_resolucao": ("Realizado NA RESOLUCAO",
        "Lucro/prejuizo de posicao que NAO conseguimos desfazer e virou $1 ou "
        "$0 quando o mercado acabou. Isto nao e market making: e aposta "
        "direcional. Se este numero domina o outro, a estrategia esta apostando."),
}


def _n(v, casas=2) -> str:
    return "—" if v is None else f"{v:,.{casas}f}"


def _sinal(v: float) -> str:
    return "verde" if v > 0 else ("vermelho" if v < 0 else "")


def coletar(con: duckdb.DuckDBPyConnection, motores: dict) -> dict:
    markout = {}
    for h in HORIZONTES_S:
        try:
            for est, n, mk, meio, reb in con.execute(f"""
                SELECT strategy, count(*), sum(markout),
                       sum(spread_at_fill/2*size), sum(rebate)
                FROM ({_consulta(h)}) GROUP BY 1
            """).fetchall():
                markout.setdefault(est, {})[h] = {
                    "n": n, "markout": mk or 0.0,
                    "meio_spread": meio or 0.0, "rebate": reb or 0.0}
        except Exception:
            pass

    # Valor de saída por motor. Precisa do livro atual, então roda aqui, com a
    # conexão em mãos — e nunca derruba o painel se um motor falhar.
    avaliacoes = {}
    for nome, m in motores.items():
        led = m.get("ledger_obj")
        if led is None:
            continue
        try:
            avaliacoes[nome] = carteira.avaliar(con, led)
        except Exception:
            pass
    return {"markout": markout, "motores": motores, "avaliacoes": avaliacoes}


def _card(chave: str, valor: str, cor: str = "", extra: str = "") -> str:
    rotulo, explica = GLOSSARIO.get(chave, (chave, ""))
    return (f'<div class="card {cor}">'
            f'<div class="rotulo">{html.escape(rotulo)}</div>'
            f'<div class="valor">{valor}</div>'
            + (f'<div class="extra">{html.escape(extra)}</div>' if extra else "")
            + f'<div class="explica">{html.escape(explica)}</div></div>')


def _bloco_motor(nome: str, mot: dict, mk: dict) -> str:
    led = mot["ledger"]
    m = mot["metricas"]
    pnl = led["pnl_total"]
    ret = led["retorno_pct"]

    if pnl > 0:
        veredito, cor_v = f"EM LUCRO: +${_n(pnl)} ({ret:+.2f}%)", "verde"
    elif pnl < 0:
        veredito, cor_v = f"EM PREJUIZO: ${_n(pnl)} ({ret:+.2f}%)", "vermelho"
    else:
        veredito, cor_v = "NO ZERO — nenhuma operacao ainda", ""

    taxa_fill = (100 * m["fills"] / m["quotes"]) if m["quotes"] else 0
    cards = "".join([
        _card("capital_inicial", f"${_n(led['capital_inicial'], 0)}"),
        _card("patrimonio", f"${_n(led['patrimonio'])}", _sinal(pnl)),
        _card("pnl_total", f"${_n(pnl)}", _sinal(pnl), f"{ret:+.2f}% sobre o capital"),
        _card("realizado", f"${_n(led['realizado'])}", _sinal(led['realizado']),
              f"{led['fechamentos']} operacoes fechadas"),
        _card("nao_realizado", f"${_n(led['nao_realizado'])}",
              _sinal(led['nao_realizado']),
              f"{led['posicoes_abertas']} posicoes abertas"),
        _card("rebates", f"${_n(led['rebates'])}"),
        _card("capital_travado", f"${_n(led['capital_travado'])}",
              "alerta" if led["capital_travado"] > led["capital_inicial"] * 0.8 else ""),
        _card("caixa_livre", f"${_n(led['caixa_livre'])}",
              "alerta" if led["caixa_livre"] < 0 else ""),
        _card("drawdown_max", f"${_n(led['drawdown_max'])}"),
        _card("fechamentos", f"{led['fechamentos']:,}",
              "alerta" if m["fills"] > 20 and led["fechamentos"] == 0 else "",
              f"{m['fills']:,} execucoes · taxa {taxa_fill:.1f}%"),
        _card("realizado_livro", f"${_n(led.get('realizado_livro', 0.0))}",
              _sinal(led.get("realizado_livro", 0.0)),
              f"{led.get('fechamentos_livro', 0)} fechadas no livro"),
        _card("realizado_resolucao", f"${_n(led.get('realizado_resolucao', 0.0))}",
              _sinal(led.get("realizado_resolucao", 0.0)),
              f"{led.get('fechamentos_resolucao', 0)} liquidadas na resolucao"),
        _card("taxas", f"${_n(led.get('taxas', 0.0))}", "",
              f"{m.get('saidas_forcadas', 0)} saidas forcadas"),
    ])

    # nome = "maker_<regra>_lat<N>". O ultimo campo e a latencia, nao a regra:
    # pegar [-1] rotulava TODO painel com o texto de `negocio`.
    regra = nome.split("_")[1] if "_" in nome else nome
    explica_regra = (
        "conta DEMAIS — dispara quando o topo passa por cima da cotacao, mas o "
        "livro tambem se move por CANCELAMENTO, e cancelamento nao executa "
        "ninguem" if regra == "cruzamento" else
        "conta de MENOS — so aceita negocio impresso, e o feed publica poucos "
        "prints (~9 para cada ~1.300 mudancas de preco); em compensacao, cada "
        "execucao aqui e real")

    # Avisos que impedem o painel de ser lido como boa notícia sem ressalva.
    alertas = []
    if m["fills"] > 20 and led["fechamentos"] == 0:
        alertas.append(
            "Muitas execucoes e NENHUMA operacao fechada. O resultado e todo "
            "nao realizado — estoque acumulado, nao spread capturado. Isso e "
            "aposta direcional disfarcada de market making.")
    if pnl > 0 and led["rebates"] > 0.5 * pnl:
        pct = 100 * led["rebates"] / pnl
        alertas.append(
            f"{pct:.0f}% do lucro vem de REBATE, nao de spread capturado. "
            "A formula da taxa NAO foi verificada (o campo fee_rate_bps veio 0 "
            "em 957 de 957 execucoes observadas), entao esse pedaco do "
            "resultado repousa sobre uma premissa, nao sobre medicao.")
    if led["caixa_livre"] < 0:
        alertas.append(
            f"Caixa livre NEGATIVO (${_n(led['caixa_livre'])}): a simulacao "
            "abriu posicao com dinheiro que nao tem. Resultado nao executavel.")
    # O teste que separa market making de aposta: de onde veio o realizado.
    livro = led.get("realizado_livro", 0.0)
    resol = led.get("realizado_resolucao", 0.0)
    if abs(resol) > abs(livro) and abs(resol) > 1.0:
        alertas.append(
            f"O realizado e dominado pela RESOLUCAO (${_n(resol)}) e nao pelo "
            f"livro (${_n(livro)}). Ou seja: o dinheiro nao vem de capturar "
            "spread, vem de segurar posicao ate o mercado virar $1 ou $0. "
            "Isso e aposta direcional, e o resultado dela nao escala com "
            "volume nem melhora com latencia menor.")
    alerta = "".join(f'<div class="perigo">ATENCAO: {html.escape(a)}</div>'
                     for a in alertas)

    return (f'<h2>{html.escape(nome)}</h2>'
            f'<div class="sub">regra <b>{regra}</b>: {html.escape(explica_regra)}</div>'
            f'<div class="veredito {cor_v}">{html.escape(veredito)}</div>'
            f'{alerta}'
            f'<div class="grid">{cards}</div>')


def _bloco_valor_real(avaliacoes: dict) -> str:
    """O número que responde 'quanto eu teria de verdade'.

    Destaca o motor de referência — a regra honesta na latência que realmente
    temos — porque somar os motores seria errado: são a MESMA estratégia
    simulada de seis jeitos, cada uma com os seus $1.000 de mentira, e não seis
    carteiras que se somam.
    """
    if not avaliacoes:
        return ""
    nome = REFERENCIA if REFERENCIA in avaliacoes else sorted(avaliacoes)[0]
    a = avaliacoes[nome]
    lucro = a["valor_de_saida"] - a["capital_inicial"]
    pct = 100 * lucro / a["capital_inicial"] if a["capital_inicial"] else 0.0
    sem_reb = a["sem_rebate"] - a["capital_inicial"]

    aviso_preco = ""
    if a["sem_preco"]:
        aviso_preco = (f'<div class="explica">{a["sem_preco"]} posicao(oes) sem '
                       'cotacao no livro agora: avaliadas pelo custo, nunca por '
                       'preco inventado.</div>')

    return f"""
<h2>Quanto teriamos DE VERDADE</h2>
<div class="sub">Motor de referencia: <b>{html.escape(nome)}</b> — a regra
  honesta, na latencia que realmente temos.</div>
<div class="veredito {_sinal(lucro)}">
  ${_n(a["valor_de_saida"])} &nbsp;·&nbsp; {"lucro" if lucro > 0 else "prejuizo"}
  de ${_n(lucro)} ({pct:+.2f}%)</div>
<div class="grid">
  <div class="card"><div class="rotulo">Se desmontasse tudo agora</div>
    <div class="valor {_sinal(lucro)}">${_n(a["valor_de_saida"])}</div>
    <div class="explica">Realizado + rebates &minus; taxas + posicao aberta
      avaliada ao preco de SAIR (bid se comprado, ask se vendido), ja descontada
      a taxa de taker para sair. <b>E o dinheiro que entraria na conta.</b></div>
    {aviso_preco}</div>
  <div class="card"><div class="rotulo">Patrimonio contabil</div>
    <div class="valor">${_n(a["patrimonio"])}</div>
    <div class="explica">A mesma carteira avaliada pelo mid. E a convencao
      correta, mas otimista: ninguem sai no mid.</div></div>
  <div class="card alerta"><div class="rotulo">Custo de otimismo</div>
    <div class="valor">${_n(a["otimismo"])}</div>
    <div class="explica">A diferenca entre as duas contas acima: o spread que
      se paga para sair, mais a taxa de taker (~3,4% do notional, medido).</div></div>
  <div class="card"><div class="rotulo">Sem contar rebate</div>
    <div class="valor {_sinal(sem_reb)}">${_n(sem_reb)}</div>
    <div class="explica">O mesmo resultado jogando fora TODO o rebate. A formula
      da taxa nunca foi confirmada (fee_rate_bps veio 0 em 957 de 957 execucoes),
      entao este e o piso do piso.</div></div>
  <div class="card"><div class="rotulo">Taxas ja pagas</div>
    <div class="valor">${_n(a["taxas_pagas"])}</div>
    <div class="explica">Taxa de taker das saidas forcadas. Ja esta descontada
      nos numeros acima.</div></div>
  <div class="card"><div class="rotulo">Custo para zerar</div>
    <div class="valor">${_n(a["custo_para_sair"])}</div>
    <div class="explica">Taxa que ainda pagariamos para desmontar as
      {a["posicoes"]} posicao(oes) abertas.</div></div>
</div>
<div class="aviso">
  <b>Por que nao somar os seis motores.</b> Eles nao sao seis carteiras: sao a
  mesma estrategia simulada com regras e latencias diferentes, cada uma com os
  seus $1.000 de mentira, sobre o mesmo fluxo de mercado. Somar daria um numero
  sem significado nenhum. O que vale e o motor de referencia acima; os outros
  servem para comparar.
</div>"""


def render(s: dict) -> str:
    bloco_real = _bloco_valor_real(s.get("avaliacoes", {}))
    blocos = "".join(_bloco_motor(n, m, s["markout"].get(n, {}))
                     for n, m in sorted(s["motores"].items()))
    if not blocos:
        blocos = "<p>Nenhum motor de paper trading ativo.</p>"

    linhas_mk = ""
    for nome, hs in sorted(s["markout"].items()):
        for h in sorted(hs):
            d = hs[h]
            edge = d["meio_spread"] + d["rebate"] + d["markout"]
            linhas_mk += (f"<tr><td>{html.escape(nome)}</td><td>{h}s</td>"
                          f"<td>{d['n']}</td><td>${_n(d['meio_spread'])}</td>"
                          f"<td>${_n(d['rebate'])}</td>"
                          f"<td class='{_sinal(d['markout'])}'>${_n(d['markout'])}</td>"
                          f"<td class='{_sinal(edge)}'>${_n(edge)}</td></tr>")
    if not linhas_mk:
        linhas_mk = ('<tr><td colspan="7">aguardando execucoes com livro '
                     'observado depois delas</td></tr>')

    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="{REFRESH_S}">
<title>pmlab — paper trading</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ font:14px ui-monospace,"Cascadia Code",Consolas,monospace;
         background:#0e1116; color:#d7dde5; margin:0; padding:24px; }}
  h1 {{ font-size:19px; margin:0 0 4px; }}
  h2 {{ font-size:16px; margin:34px 0 2px; color:#e2e8ef; }}
  h3 {{ font-size:13px; margin:30px 0 8px; color:#9aa5b1; }}
  .sub {{ color:#7d8794; font-size:12px; margin-bottom:12px; line-height:1.5; }}
  .aviso {{ background:#1a1f27; border-left:3px solid #3d6a7a; padding:12px 16px;
           margin:16px 0 24px; font-size:12px; color:#9aa5b1; line-height:1.6;
           max-width:860px; }}
  .perigo {{ background:#231518; border-left:3px solid #8b3a3a; padding:11px 15px;
            margin:10px 0 14px; font-size:12px; color:#e0a0a0; line-height:1.6;
            max-width:860px; }}
  .veredito {{ font-size:20px; padding:12px 16px; border-radius:8px;
              background:#161b22; border:1px solid #232a34; margin:10px 0 14px;
              display:inline-block; }}
  .veredito.verde {{ color:#6ecf9a; border-color:#2f6b4a; }}
  .veredito.vermelho {{ color:#e07a7a; border-color:#8b3a3a; }}
  .grid {{ display:grid; gap:12px;
          grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); }}
  .card {{ background:#161b22; border:1px solid #232a34; border-radius:8px;
          padding:13px 14px; }}
  .card.verde {{ border-color:#2f6b4a; }}
  .card.vermelho {{ border-color:#8b3a3a; }}
  .card.alerta {{ border-color:#7a6320; }}
  .rotulo {{ color:#8b95a1; font-size:11px; text-transform:uppercase;
            letter-spacing:.5px; }}
  .valor {{ font-size:23px; margin:5px 0 1px; font-variant-numeric:tabular-nums; }}
  .extra {{ color:#8b95a1; font-size:11px; margin-bottom:5px; }}
  .explica {{ color:#646d79; font-size:11px; line-height:1.45; margin-top:6px;
             border-top:1px solid #1d232c; padding-top:6px; }}
  table {{ border-collapse:collapse; width:100%; max-width:880px; }}
  td, th {{ padding:5px 10px; border-bottom:1px solid #1d232c; text-align:left; }}
  th {{ color:#8b95a1; font-weight:normal; }}
  td.verde {{ color:#6ecf9a; }} td.vermelho {{ color:#e07a7a; }}
  a {{ color:#6fa8c7; }}
  {nav.ESTILO_BARRA}
</style></head><body>
{nav.barra("/paper")}

<h1>pmlab — paper trading (Fase 1)</h1>
<div class="sub">atualiza a cada {REFRESH_S}s</div>

{bloco_real}

<div class="aviso">
  <b>Dinheiro de mentira.</b> Nenhuma ordem real e enviada; nao existe chave
  privada no projeto. O capital inicial e um numero de configuracao.<br><br>
  Duas regras rodam sobre o MESMO fluxo e <b>emparedam o numero de
  execucoes</b>: <code>cruzamento</code> conta demais, <code>negocio</code>
  conta de menos.
  <b>Nao compare o lucro absoluto entre elas</b> — executam quantidades muito
  diferentes. Para comparar, use o markout, que independe de quantas vezes
  fomos executados.
</div>

{blocos}

<h3>Markout por horizonte — quanto custa a selecao adversa</h3>
<div class="sub">
  Onde o preco medio estava DEPOIS de cada execucao.<br>
  <b>Markout negativo</b> = fomos executados e o preco andou contra nos: alguem
  sabia mais. <b>Edge liquido</b> = meio spread + rebate + markout; e o que
  sobra de verdade por execucao.<br>
  Perda que some rapido e microestrutura; perda que persiste a 300s e informacao.
</div>
<table>
<tr><th>estrategia</th><th>horizonte</th><th>execucoes</th><th>meio spread</th>
    <th>rebate</th><th>markout</th><th>edge liquido</th></tr>
{linhas_mk}
</table>

<div class="sub" style="margin-top:18px">gerado em {time.strftime('%H:%M:%S')}</div>
</body></html>"""


def pagina(con: duckdb.DuckDBPyConnection, motores: dict) -> str:
    return render(coletar(con, motores))
