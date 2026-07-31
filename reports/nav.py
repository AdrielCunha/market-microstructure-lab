"""Barra de navegação única, usada por todas as páginas.

Antes cada tela tinha a sua listinha de links, e cada uma esquecia de alguma:
o `/ordens` não aparecia em lugar nenhum, o `/nichos` só no índice. Menu que
depende de alguém lembrar de atualizar vira menu incompleto.

Os relatórios em texto (`/gate0`, `/verify`, ...) passam por `pagina_texto`,
que os embrulha num HTML mínimo com a mesma barra. Sem isso eles seriam becos
sem saída: para sair, só o botão voltar do navegador.
"""

from __future__ import annotations

import html

# (rota, rótulo curto, o que responde)
PAGINAS = [
    ("/",        "indice",   "todos os paineis"),
    ("/paper",   "carteira", "quanto temos e quanto e de verdade"),
    ("/ordens",  "ordens",   "cada execucao, uma por linha"),
    ("/coleta",  "coleta",   "o instrumento esta funcionando?"),
    ("/markout", "markout",  "quanto custa a selecao adversa"),
    ("/gate0",   "gate 0",   "alguma tese sobreviveu?"),
    ("/nichos",  "nichos",   "onde um operador lento consegue jogar"),
    ("/verify",  "verify",   "da para confiar nos dados?"),
]

ESTILO_BARRA = """
  .nav { display:flex; flex-wrap:wrap; gap:6px; align-items:center;
         padding:0 0 14px; margin-bottom:16px;
         border-bottom:1px solid #1d232c; }
  .nav a { padding:5px 11px; border:1px solid #232a34; border-radius:6px;
           color:#9aa5b1; text-decoration:none; font-size:12px; }
  .nav a:hover { border-color:#3d6a7a; color:#cbd3dc; background:#161b22; }
  .nav a.on { background:#1b2430; color:#8fd0f0; border-color:#33566b; }
  .nav .marca { color:#5f6874; font-size:12px; margin-right:8px; }
"""


def barra(atual: str = "") -> str:
    """Links para todas as páginas, com a atual destacada."""
    itens = "".join(
        f'<a class="{"on" if rota == atual else ""}" href="{rota}" '
        f'title="{html.escape(desc)}">{rotulo}</a>'
        for rota, rotulo, desc in PAGINAS)
    return f'<div class="nav"><span class="marca">pmlab</span>{itens}</div>'


def pagina_texto(titulo: str, texto: str, atual: str = "") -> str:
    """Embrulha um relatório de texto num HTML com a barra de navegação."""
    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<title>pmlab — {html.escape(titulo)}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ font:13px ui-monospace,"Cascadia Code",Consolas,monospace;
         background:#0e1116; color:#d7dde5; margin:0; padding:22px; }}
  pre {{ white-space:pre; overflow-x:auto; line-height:1.45; margin:0;
        color:#c8d1da; }}
  {ESTILO_BARRA}
</style></head><body>
{barra(atual)}
<pre>{html.escape(texto)}</pre>
</body></html>"""
