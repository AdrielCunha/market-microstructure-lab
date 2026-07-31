"""Página inicial: explica o que cada painel responde.

Existem painéis separados porque são perguntas separadas. Misturar "o coletor
está saudável?" com "a estratégia está ganhando?" num único painel faz o
segundo parecer confiável quando o primeiro está quebrado.
"""

from __future__ import annotations

from reports import nav

PAINEIS = [
    ("/coleta", "Coleta", "O instrumento esta funcionando?",
     "Saude do coletor: fluxo do WebSocket, buffer de escrita, silencio de "
     "conexao, spreads invalidos, reconexoes. Cartao vermelho aqui invalida "
     "tudo o que os outros paineis mostram — dado quebrado gera analise linda "
     "e falsa.", "atualiza sozinho a cada 10s"),
    ("/paper", "Paper trading", "A estrategia esta ganhando ou perdendo?",
     "Resultado das execucoes SIMULADAS, com as duas regras rodando em "
     "paralelo. O numero que decide e o EDGE LIQUIDO: meio spread + rebate "
     "menos o markout. Se ele for negativo nas duas regras, market making "
     "morreu.", "atualiza sozinho a cada 15s"),
    ("/ordens", "Ordem a ordem", "O que exatamente o robo fez?",
     "A visao detalhada: cada execucao com hora, mercado, preco, cotas e quanto "
     "AQUELA operacao colocou ou tirou do bolso. Mais as posicoes abertas "
     "marcadas a preco de agora, e os ciclos que voltaram ao zero — que e onde "
     "market making aparece de verdade. Uma aba por motor.",
     "atualiza sozinho a cada 20s"),
    ("/gate0", "Veredito Gate 0", "Alguma tese sobreviveu ao custo real?",
     "Relatorio em texto com os tres criterios: arbitragem negative-risk, "
     "market making e copy trading. Cada um com PASS, FAIL ou INCONCLUSIVO e "
     "os numeros que sustentam o veredito.", "gerado sob demanda"),
    ("/verify", "Integridade", "Da para confiar nos dados?",
     "Compara a serie do WebSocket com snapshots REST independentes, procura "
     "spread negativo, buraco na serie e evento negative-risk incompleto. "
     "Rode isto ANTES de acreditar em qualquer numero.", "gerado sob demanda"),
    ("/nichos", "Nichos", "Onde um operador LENTO consegue jogar?",
     "Cruza liga x faixa de preco e mede a VIDA DO TOPO DE LIVRO: quanto tempo "
     "o melhor preco sobrevive antes de alguem repricar. Se o topo troca mais "
     "rapido que os nossos 230ms, a cotacao nasce defasada e so e executada "
     "quando o preco ja virou contra. E o filtro que separa nicho jogavel de "
     "corrida perdida.", "gerado sob demanda"),
    ("/markout", "Markout", "Quanto custa a selecao adversa?",
     "Detalhe por horizonte (5s, 30s, 60s, 300s) de onde o preco foi DEPOIS "
     "de cada execucao. Perda que some rapido e microestrutura; perda que "
     "persiste a 300s e informacao — alguem sabia mais que nos.",
     "gerado sob demanda"),
]


def pagina() -> str:
    cards = ""
    for rota, titulo, pergunta, desc, nota in PAINEIS:
        cards += f"""
        <a class="card" href="{rota}">
          <div class="rota">{rota}</div>
          <div class="titulo">{titulo}</div>
          <div class="pergunta">{pergunta}</div>
          <div class="desc">{desc}</div>
          <div class="nota">{nota}</div>
        </a>"""

    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<title>pmlab — painel</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ font:14px ui-monospace,"Cascadia Code",Consolas,monospace;
         background:#0e1116; color:#d7dde5; margin:0; padding:32px; }}
  h1 {{ font-size:20px; margin:0 0 6px; }}
  .sub {{ color:#7d8794; font-size:12px; margin-bottom:6px; }}
  .aviso {{ background:#1a1f27; border-left:3px solid #3d6a7a; padding:12px 16px;
           margin:20px 0 26px; font-size:12px; color:#9aa5b1; max-width:760px; }}
  .grid {{ display:grid; gap:14px; grid-template-columns:repeat(auto-fill,minmax(330px,1fr));
          max-width:1100px; }}
  .card {{ display:block; background:#161b22; border:1px solid #232a34;
          border-radius:8px; padding:16px; text-decoration:none; color:inherit; }}
  .card:hover {{ border-color:#3d6a7a; background:#1a212a; }}
  .rota {{ color:#6fa8c7; font-size:11px; }}
  .titulo {{ font-size:17px; margin:4px 0 2px; }}
  .pergunta {{ color:#cbd3dc; font-size:12px; margin-bottom:8px; font-style:italic; }}
  .desc {{ color:#8b95a1; font-size:12px; line-height:1.5; }}
  .nota {{ color:#5f6874; font-size:11px; margin-top:10px; }}
  {nav.ESTILO_BARRA}
</style></head><body>
{nav.barra("/")}
<h1>pmlab — laboratorio Polymarket</h1>
<div class="sub">Fase 0 (coleta) + Fase 1 (paper trading)</div>

<div class="aviso">
  <b>Nenhuma ordem real e enviada por este sistema.</b> Nao existe chave privada
  no projeto. Tudo aqui e medicao e simulacao.<br><br>
  Ordem de leitura recomendada: <b>Integridade</b> (os dados prestam?) →
  <b>Coleta</b> (o instrumento esta vivo?) → <b>Paper trading</b> (a estrategia
  ganha?) → <b>Gate 0</b> (o veredito).
</div>

<div class="grid">{cards}</div>
</body></html>"""
