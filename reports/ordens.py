"""Painel ordem a ordem: o que o robô fez, uma execução por linha.

Os outros painéis respondem "quanto estamos ganhando". Este responde **o quê
exatamente aconteceu**: qual mercado, que preço, quantas cotas, quanto sobrou de
posição e quanto AQUELA execução colocou ou tirou do bolso.

O resultado por execução é reconstruído reprocessando as execuções gravadas
pelo próprio `Ledger` — o mesmo objeto que faz a contabilidade de verdade. Isso
não é detalhe: uma segunda implementação da conta acabaria divergindo da
primeira, e aí duas telas do mesmo sistema mostrariam números diferentes.

O corte de sessão é o mesmo que o motor usa (`paper_sessao`), senão a tela
mostraria execuções de rodadas antigas, algumas com bugs já corrigidos.
"""

from __future__ import annotations

import html
import time

import duckdb

from engine.ledger import Ledger
from reports import nav

REFRESH_S = 20
MAX_EXECUCOES = 120       # a tabela precisa caber na tela e continuar legível

# Qual motor abrir por padrão: a regra honesta, na latência que realmente temos.
PADRAO = "maker_negocio_lat15"


def _n(v, casas=2) -> str:
    return "—" if v is None else f"{v:,.{casas}f}"


def _sinal(v: float) -> str:
    return "verde" if v > 1e-9 else ("vermelho" if v < -1e-9 else "")


def _quando(ts_ms: int) -> str:
    return time.strftime("%d/%m %H:%M:%S", time.localtime(ts_ms / 1000))


def _duracao(ms: float) -> str:
    s = ms / 1000
    if s < 90:
        return f"{s:.0f}s"
    if s < 5400:
        return f"{s/60:.0f}min"
    return f"{s/3600:.1f}h"


def motores_disponiveis(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT DISTINCT strategy FROM paper_fills ORDER BY 1").fetchall()]


def carregar(con: duckdb.DuckDBPyConnection, estrategia: str) -> dict:
    """Reprocessa as execuções da sessão e devolve tudo que a tela precisa."""
    # Banco anterior a esta tabela (analise de arquivo antigo, ou modo leitura
    # sem o coletor ter criado o schema) mostra a serie inteira. Preferivel a
    # quebrar a tela.
    try:
        corte = con.execute(
            "SELECT desde_ts FROM paper_sessao WHERE strategy = ?",
            [estrategia]).fetchone()
        desde = int(corte[0]) if corte else 0
    except duckdb.Error:
        desde = 0

    fills = con.execute("""
        SELECT f.ts_local, f.token_id, f.side, f.price, f.size, f.notional_usd,
               f.rebate, f.taxa, f.regra, COALESCE(f.agressiva, FALSE),
               m.question, m.outcome, m.category, m.end_date
        FROM paper_fills f
        LEFT JOIN markets m USING (token_id)
        WHERE f.strategy = ? AND f.ts_local >= ?
        ORDER BY f.ts_local
    """, [estrategia, desde]).fetchall()

    led = Ledger()
    execucoes: list[dict] = []
    ciclos: list[dict] = []
    # Ciclo aberto por token: de quando a posição saiu de zero até voltar.
    aberto: dict[str, dict] = {}

    for (ts, token, side, price, size, notional, rebate, taxa, regra,
         agressiva, question, outcome, categoria, fim) in fills:
        antes_pos = led.posicao(token)
        antes_real = led.realizado
        led.aplicar(token, side, float(price), float(size),
                    rebate=float(rebate or 0.0), taxa=float(taxa or 0.0),
                    motivo="resolucao" if regra == "liquidacao" else "livro")
        delta = led.realizado - antes_real
        liquido = delta + float(rebate or 0.0) - float(taxa or 0.0)
        depois_pos = led.posicao(token)

        rotulo = question or f"token {str(token)[:10]}…"
        if outcome:
            rotulo = f"{rotulo} — {outcome}"

        execucoes.append({
            "ts": ts, "token": token, "rotulo": rotulo, "side": side,
            "price": float(price), "size": float(size),
            "notional": float(notional or 0.0), "rebate": float(rebate or 0.0),
            "taxa": float(taxa or 0.0), "pos": depois_pos,
            "delta": delta, "liquido": liquido, "regra": regra,
            "agressiva": bool(agressiva), "categoria": categoria or "?",
        })

        # --- ciclos: de posição zero até voltar a zero ---
        if abs(antes_pos) < 1e-9 and abs(depois_pos) > 1e-9:
            aberto[token] = {"rotulo": rotulo, "inicio": ts, "entrada": price,
                             "lado": "comprado" if depois_pos > 0 else "vendido",
                             "cotas": abs(depois_pos), "resultado": liquido,
                             "execucoes": 1, "categoria": categoria or "?"}
        elif token in aberto:
            c = aberto[token]
            c["resultado"] += liquido
            c["execucoes"] += 1
            c["cotas"] = max(c["cotas"], abs(depois_pos))
            if abs(depois_pos) < 1e-9:
                c.update(fim=ts, saida=price, duracao=ts - c["inicio"],
                         encerrou="resolucao" if regra == "liquidacao"
                                  else ("saida forcada" if agressiva else "livro"))
                ciclos.append(c)
                del aberto[token]

    # Preço atual de cada posição ainda aberta, para marcar a mercado.
    abertas = []
    posicoes = led.posicoes()
    if posicoes:
        marcas = ", ".join("?" * len(posicoes))
        mids = dict(con.execute(f"""
            SELECT token_id, last(mid ORDER BY ts_local) FROM book_top
            WHERE token_id IN ({marcas}) AND mid IS NOT NULL
            GROUP BY token_id
        """, list(posicoes)).fetchall())
        infos = dict((r[0], r) for r in con.execute(f"""
            SELECT token_id, question, outcome, end_date
            FROM markets WHERE token_id IN ({marcas})
        """, list(posicoes)).fetchall())
        agora = time.time() * 1000
        for token, qtd in posicoes.items():
            custo = led.custo_medio(token)
            mid = mids.get(token)
            info = infos.get(token)
            rotulo = (info[1] if info and info[1] else f"token {str(token)[:10]}…")
            if info and info[2]:
                rotulo = f"{rotulo} — {info[2]}"
            fim_ms = None
            if info and info[3] is not None:
                fim_ms = info[3].timestamp() * 1000
            abertas.append({
                "rotulo": rotulo, "qtd": qtd, "custo": custo, "mid": mid,
                "nao_realizado": None if mid is None else (mid - custo) * qtd,
                "falta": None if fim_ms is None else fim_ms - agora,
            })
        abertas.sort(key=lambda a: (a["falta"] is None, a["falta"] or 0))

    return {"execucoes": execucoes, "ciclos": ciclos, "abertas": abertas,
            "ledger": led, "desde": desde}


ESTILO = """
  :root { color-scheme: dark; }
  body { font:13px ui-monospace,"Cascadia Code",Consolas,monospace;
         background:#0e1116; color:#d7dde5; margin:0; padding:22px; }
  h1 { font-size:18px; margin:0 0 4px; }
  h2 { font-size:15px; margin:30px 0 4px; color:#e2e8ef; }
  .sub { color:#7d8794; font-size:12px; margin-bottom:10px; line-height:1.55;
         max-width:900px; }
  .aviso { background:#1a1f27; border-left:3px solid #3d6a7a; padding:10px 14px;
           margin:12px 0 18px; font-size:12px; color:#9aa5b1; line-height:1.6;
           max-width:900px; }
  .abas { margin:10px 0 6px; }
  .abas a { display:inline-block; padding:4px 10px; margin:2px 4px 2px 0;
            border:1px solid #232a34; border-radius:6px; color:#9aa5b1;
            text-decoration:none; font-size:12px; }
  .abas a.on { background:#1b2430; color:#8fd0f0; border-color:#33566b; }
  .grid { display:grid; gap:10px; margin:10px 0 4px;
          grid-template-columns:repeat(auto-fill,minmax(165px,1fr)); }
  .card { background:#161b22; border:1px solid #232a34; border-radius:7px;
          padding:10px 12px; }
  .rotulo { color:#8b95a1; font-size:10px; text-transform:uppercase;
            letter-spacing:.5px; }
  .valor { font-size:19px; margin-top:4px; font-variant-numeric:tabular-nums; }
  table { border-collapse:collapse; width:100%; margin-top:6px; }
  td, th { padding:4px 8px; border-bottom:1px solid #1b212a; text-align:left;
           white-space:nowrap; font-variant-numeric:tabular-nums; }
  th { color:#8b95a1; font-weight:normal; font-size:11px;
       text-transform:uppercase; letter-spacing:.4px; }
  td.mercado { white-space:normal; max-width:330px; color:#b9c2cc; }
  .verde { color:#6ecf9a; } .vermelho { color:#e07a7a; }
  .compra { color:#7fb5e8; } .venda { color:#e8b57f; }
  .etiqueta { font-size:10px; padding:1px 6px; border-radius:9px;
              border:1px solid #33404f; color:#93a0ae; }
  .vazio { color:#646d79; padding:10px 0; }
  a { color:#6fa8c7; }
""" + nav.ESTILO_BARRA


def _cabecalho(estrategia: str, motores: list[str]) -> str:
    abas = "".join(
        f'<a class="{"on" if m == estrategia else ""}" href="/ordens?e={m}">'
        f'{html.escape(m.replace("maker_", ""))}</a>' for m in motores)
    return f"""{nav.barra("/ordens")}
<h1>pmlab — ordem a ordem</h1>
<div class="sub">atualiza a cada {REFRESH_S}s · uma aba por motor</div>
<div class="abas">{abas}</div>"""


def pagina(con: duckdb.DuckDBPyConnection, estrategia: str | None = None) -> str:
    motores = motores_disponiveis(con)
    if not motores:
        return ("<!doctype html><meta charset=utf-8>"
                "<body style='background:#0e1116;color:#d7dde5;font-family:monospace;"
                "padding:24px'>Nenhuma execucao ainda. Deixe o coletor rodar.</body>")
    if estrategia not in motores:
        estrategia = PADRAO if PADRAO in motores else motores[0]

    d = carregar(con, estrategia)
    led = d["ledger"]
    nao_realizado = sum(a["nao_realizado"] or 0.0 for a in d["abertas"])
    patrimonio = (led.capital_inicial + led.realizado + led.rebates
                  - led.taxas + nao_realizado)

    cards = f"""<div class="grid">
      <div class="card"><div class="rotulo">Patrimonio</div>
        <div class="valor {_sinal(patrimonio - led.capital_inicial)}">
        ${_n(patrimonio)}</div></div>
      <div class="card"><div class="rotulo">Realizado</div>
        <div class="valor {_sinal(led.realizado)}">${_n(led.realizado)}</div></div>
      <div class="card"><div class="rotulo">Nao realizado</div>
        <div class="valor {_sinal(nao_realizado)}">${_n(nao_realizado)}</div></div>
      <div class="card"><div class="rotulo">Rebates</div>
        <div class="valor">${_n(led.rebates)}</div></div>
      <div class="card"><div class="rotulo">Taxas pagas</div>
        <div class="valor">${_n(led.taxas)}</div></div>
      <div class="card"><div class="rotulo">Execucoes</div>
        <div class="valor">{len(d["execucoes"]):,}</div></div>
      <div class="card"><div class="rotulo">Ciclos fechados</div>
        <div class="valor">{len(d["ciclos"]):,}</div></div>
      <div class="card"><div class="rotulo">Posicoes abertas</div>
        <div class="valor">{len(d["abertas"]):,}</div></div>
    </div>"""

    # ---------------- posições abertas ----------------
    if d["abertas"]:
        linhas = "".join(f"""<tr>
          <td class="mercado">{html.escape(a["rotulo"][:90])}</td>
          <td class="{"compra" if a["qtd"] > 0 else "venda"}">
            {"comprado" if a["qtd"] > 0 else "vendido"}</td>
          <td>{_n(abs(a["qtd"]), 0)}</td>
          <td>{_n(a["custo"], 3)}</td>
          <td>{_n(a["mid"], 3)}</td>
          <td class="{_sinal(a["nao_realizado"] or 0)}">
            ${_n(a["nao_realizado"])}</td>
          <td>{"—" if a["falta"] is None else _duracao(a["falta"])}</td>
        </tr>""" for a in d["abertas"])
        tabela_abertas = f"""<table>
          <tr><th>mercado</th><th>lado</th><th>cotas</th><th>custo medio</th>
              <th>preco agora</th><th>nao realizado</th><th>resolve em</th></tr>
          {linhas}</table>"""
    else:
        tabela_abertas = '<div class="vazio">Nenhuma posicao aberta agora.</div>'

    # ---------------- ciclos fechados ----------------
    if d["ciclos"]:
        linhas = "".join(f"""<tr>
          <td class="mercado">{html.escape(c["rotulo"][:90])}</td>
          <td class="{"compra" if c["lado"] == "comprado" else "venda"}">
            {c["lado"]}</td>
          <td>{_n(c["cotas"], 0)}</td>
          <td>{_n(c["entrada"], 3)} &rarr; {_n(c["saida"], 3)}</td>
          <td class="{_sinal(c["resultado"])}">${_n(c["resultado"])}</td>
          <td>{_duracao(c["duracao"])}</td>
          <td><span class="etiqueta">{c["encerrou"]}</span></td>
          <td>{_quando(c["fim"])}</td>
        </tr>""" for c in reversed(d["ciclos"][-60:]))
        tabela_ciclos = f"""<table>
          <tr><th>mercado</th><th>lado</th><th>cotas</th><th>entrada &rarr; saida</th>
              <th>resultado</th><th>durou</th><th>fechou por</th><th>quando</th></tr>
          {linhas}</table>"""
    else:
        tabela_ciclos = ('<div class="vazio">Nenhum ciclo completo ainda — '
                         'nenhuma posicao voltou a zero.</div>')

    # ---------------- execuções ----------------
    if d["execucoes"]:
        linhas = "".join(f"""<tr>
          <td>{_quando(e["ts"])}</td>
          <td class="mercado">{html.escape(e["rotulo"][:80])}</td>
          <td class="{"compra" if e["side"] == "BUY" else "venda"}">
            {"COMPRA" if e["side"] == "BUY" else "VENDA"}
            {'<span class="etiqueta">atravessou</span>' if e["agressiva"] else ""}
            {'<span class="etiqueta">resolucao</span>' if e["regra"] == "liquidacao" else ""}</td>
          <td>{_n(e["price"], 3)}</td>
          <td>{_n(e["size"], 0)}</td>
          <td>${_n(e["notional"])}</td>
          <td class="{_sinal(e["rebate"] - e["taxa"])}">
            ${_n(e["rebate"] - e["taxa"])}</td>
          <td class="{_sinal(e["delta"])}">{"—" if abs(e["delta"]) < 1e-9 else "$" + _n(e["delta"])}</td>
          <td class="{_sinal(e["liquido"])}">${_n(e["liquido"])}</td>
          <td>{_n(e["pos"], 0)}</td>
        </tr>""" for e in reversed(d["execucoes"][-MAX_EXECUCOES:]))
        tabela_exec = f"""<table>
          <tr><th>quando</th><th>mercado</th><th>lado</th><th>preco</th>
              <th>cotas</th><th>notional</th><th>rebate&minus;taxa</th>
              <th>fechou</th><th>total desta</th><th>posicao depois</th></tr>
          {linhas}</table>"""
    else:
        tabela_exec = '<div class="vazio">Nenhuma execucao ainda.</div>'

    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="{REFRESH_S}">
<title>pmlab — ordens</title><style>{ESTILO}</style></head><body>
{_cabecalho(estrategia, motores)}

<div class="aviso">
  <b>Dinheiro de mentira.</b> Nenhuma ordem real e enviada.<br><br>
  <b>fechou</b> = quanto esta execucao realizou ao REDUZIR posicao. Comprar nao
  fecha nada, entao aparece tracinho: o resultado da compra so existe quando ela
  e desfeita.<br>
  <b>total desta</b> = o que fechou, mais rebate, menos taxa.<br>
  <b>atravessou</b> = saida forcada perto da resolucao; pagamos o spread para
  sair em vez de esperar contraparte.
</div>

{cards}

<h2>Posicoes abertas</h2>
<div class="sub">O que esta na mao agora, marcado a preco de mercado. Enquanto
  nao fecha, o resultado e promessa — vira po se o preco andar contra.</div>
{tabela_abertas}

<h2>Ciclos fechados</h2>
<div class="sub">Cada linha e uma posicao que saiu do zero e voltou ao zero.
  <b>E aqui que market making aparece de verdade</b>: comprou e vendeu de volta,
  embolsou a diferenca. Ciclo que fecha por <i>resolucao</i> nao e market
  making, e aposta que deu certo ou errado.</div>
{tabela_ciclos}

<h2>Execucoes ({len(d["execucoes"]):,})</h2>
<div class="sub">Ultimas {MAX_EXECUCOES}, da mais recente para a mais antiga.</div>
{tabela_exec}

<div class="sub" style="margin-top:20px">gerado em {time.strftime("%H:%M:%S")}</div>
</body></html>"""
