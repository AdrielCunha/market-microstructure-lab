"""Dashboard de acompanhamento da coleta, servido pelo próprio coletor.

Por que dentro do processo: o DuckDB trava o arquivo para um único escritor —
enquanto `collector.run` grava, nenhum outro processo consegue abrir o banco,
nem em modo leitura. Um dashboard separado simplesmente não conectaria. Então
ele reaproveita a conexão viva do coletor.

A página é HTML puro com auto-refresh, sem dependência externa: o objetivo é
ver se a coleta está saudável, não fazer análise bonita.
"""

from __future__ import annotations

import html
import time
from typing import Any

import duckdb

from reports import nav

REFRESH_S = 10


def _fmt(v: Any, casas: int = 0) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.{casas}f}"
    if isinstance(v, int):
        return f"{v:,}"
    return html.escape(str(v))


def _q1(con: duckdb.DuckDBPyConnection, sql: str, default: Any = None) -> Any:
    try:
        r = con.execute(sql).fetchone()
        return r if r else default
    except Exception:
        return default


def coletar_stats(con: duckdb.DuckDBPyConnection, runtime: dict) -> dict:
    livro = _q1(con, """
        SELECT count(*), count(DISTINCT token_id),
               (max(ts_local) - min(ts_local)) / 3600000.0,
               max(ts_local)
        FROM book_top WHERE source = 'ws'
    """, (0, 0, 0.0, 0))
    trades = _q1(con, """
        SELECT count(*) FILTER (WHERE NOT is_backfill),
               count(DISTINCT wallet),
               max(ts_seen)
        FROM wallet_trades
    """, (0, 0, 0))
    atraso = _q1(con, """
        SELECT count(*), min((ts_seen-ts_trade)/1000.0),
               median((ts_seen-ts_trade)/1000.0),
               quantile_cont((ts_seen-ts_trade)/1000.0, 0.95)
        FROM wallet_trades WHERE NOT is_backfill
    """, (0, None, None, None))
    spread = _q1(con, """
        SELECT median(spread)*100, quantile_cont(spread,0.25)*100,
               quantile_cont(spread,0.75)*100
        FROM book_top WHERE source='ws' AND spread IS NOT NULL
    """, (None, None, None))
    warns = _q1(con, "SELECT count(*) FROM collector_log WHERE level<>'info'", (0,))
    neg = _q1(con, "SELECT count(*) FROM book_top WHERE spread < 0", (0,))

    por_categoria = []
    try:
        por_categoria = con.execute("""
            SELECT m.category, count(*) AS obs, round(median(b.spread)*100, 2) AS spread_c
            FROM book_top b JOIN markets m USING (token_id)
            WHERE b.source='ws' AND b.spread IS NOT NULL AND m.selected
            GROUP BY 1 ORDER BY 2 DESC LIMIT 10
        """).fetchall()
    except Exception:
        pass

    ultimos_logs = []
    try:
        ultimos_logs = con.execute("""
            SELECT ts_local, component, level, message
            FROM collector_log ORDER BY ts_local DESC LIMIT 12
        """).fetchall()
    except Exception:
        pass

    agora_ms = int(time.time() * 1000)
    return {
        "runtime": runtime,
        "obs_livro": livro[0], "tokens": livro[1], "horas": livro[2] or 0.0,
        "silencio_livro_s": (agora_ms - livro[3]) / 1000 if livro[3] else None,
        "trades_novos": trades[0], "carteiras": trades[1],
        "silencio_trades_s": (agora_ms - trades[2]) / 1000 if trades[2] else None,
        "atraso_n": atraso[0], "atraso_min": atraso[1],
        "atraso_med": atraso[2], "atraso_p95": atraso[3],
        "spread_med": spread[0], "spread_p25": spread[1], "spread_p75": spread[2],
        "spreads_negativos": neg[0], "avisos": warns[0],
        "por_categoria": por_categoria, "logs": ultimos_logs,
    }


def _card(rotulo: str, valor: str, nota: str = "", alerta: bool = False) -> str:
    cls = "card alerta" if alerta else "card"
    return (f'<div class="{cls}"><div class="rotulo">{rotulo}</div>'
            f'<div class="valor">{valor}</div>'
            f'<div class="nota">{html.escape(nota)}</div></div>')


def render(s: dict) -> str:
    rt = s["runtime"]
    horas_up = rt["uptime_s"] / 3600

    # Silêncio longo demais é o sintoma de conexão caída sem reconexão.
    silencio = s["silencio_livro_s"]
    livro_mudo = silencio is not None and silencio > 120

    progresso = min(100, 100 * s["horas"] / (7 * 24))

    cards = "".join([
        _card("Tempo no ar", f"{horas_up:.1f}h",
              f"geracao de assinatura #{rt['geracao']}"),
        _card("Eventos de livro", _fmt(rt["eventos"]),
              f"{rt['taxa']:.0f}/s agora"),
        # `tokens` conta o histórico do banco, não a assinatura atual: inclui
        # tokens que já venceram e os sondados sob demanda fora do catálogo.
        _card("Topos gravados", _fmt(s["obs_livro"]),
              f"{s['tokens']} tokens ja vistos · {rt['dedupe_pct']:.1f}% descartado"),
        _card("Ultimo evento ha", f"{silencio:.0f}s" if silencio is not None else "—",
              "conexao caiu?" if livro_mudo else "fluxo normal", alerta=livro_mudo),
        _card("Trades novos", _fmt(s["trades_novos"]),
              f"{s['carteiras']} carteiras vigiadas"),
        _card("Atraso da API", f"{s['atraso_med']:.0f}s" if s["atraso_med"] else "—",
              f"min {s['atraso_min']:.0f}s · p95 {s['atraso_p95']:.0f}s"
              if s["atraso_min"] else "aguardando trades"),
        _card("Spread mediano", f"{s['spread_med']:.2f}c" if s["spread_med"] else "—",
              f"p25 {s['spread_p25']:.2f}c · p75 {s['spread_p75']:.2f}c"
              if s["spread_p25"] else ""),
        _card("Buffer de escrita", _fmt(rt["buffer"]),
              f"{_fmt(rt['gravadas'])} linhas no banco",
              alerta=rt["buffer"] > 20000),
        _card("Spreads negativos", _fmt(s["spreads_negativos"]),
              "impossivel num livro valido", alerta=s["spreads_negativos"] > 0),
        _card("Avisos no log", _fmt(s["avisos"]), "reconexoes e falhas",
              alerta=s["avisos"] > 20),
    ])

    linhas_cat = "".join(
        f"<tr><td>{html.escape(str(c))}</td><td>{_fmt(n)}</td><td>{sp}c</td></tr>"
        for c, n, sp in s["por_categoria"])

    linhas_log = "".join(
        f'<tr class="{ "warn" if lvl != "info" else "" }">'
        f"<td>{time.strftime('%H:%M:%S', time.localtime(ts/1000))}</td>"
        f"<td>{html.escape(comp)}</td><td>{html.escape(msg)}</td></tr>"
        for ts, comp, lvl, msg in s["logs"])

    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="{REFRESH_S}">
<title>pmlab — coleta ao vivo</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ font: 14px ui-monospace, "Cascadia Code", Consolas, monospace;
         background:#0e1116; color:#d7dde5; margin:0; padding:24px; }}
  h1 {{ font-size:18px; margin:0 0 4px; font-weight:600; }}
  .sub {{ color:#7d8794; margin-bottom:20px; font-size:12px; }}
  .grid {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fill,minmax(215px,1fr)); }}
  .card {{ background:#161b22; border:1px solid #232a34; border-radius:8px; padding:14px; }}
  .card.alerta {{ border-color:#8b3a3a; background:#1d1416; }}
  .rotulo {{ color:#7d8794; font-size:11px; text-transform:uppercase; letter-spacing:.5px; }}
  .valor {{ font-size:26px; margin:6px 0 2px; font-variant-numeric:tabular-nums; }}
  .nota {{ color:#6f7986; font-size:11px; }}
  .barra {{ height:6px; background:#1d232c; border-radius:3px; margin:20px 0 6px; overflow:hidden; }}
  .barra > div {{ height:100%; background:#3d7a5a; width:{progresso:.1f}%; }}
  h2 {{ font-size:13px; color:#9aa5b1; margin:28px 0 8px; font-weight:600; }}
  table {{ border-collapse:collapse; width:100%; max-width:620px; }}
  td, th {{ padding:5px 10px; border-bottom:1px solid #1d232c; text-align:left; }}
  tr.warn td {{ color:#e0a458; }}
  .cols {{ display:flex; gap:40px; flex-wrap:wrap; }}
  {nav.ESTILO_BARRA}
</style></head><body>
{nav.barra("/coleta")}
<h1>pmlab — coleta ao vivo</h1>
<div class="sub">atualiza sozinho a cada {REFRESH_S}s · Fase 0, nenhuma ordem enviada</div>

<div class="grid">{cards}</div>

<div class="barra"><div></div></div>
<div class="nota">{s['horas']:.1f}h de {7*24}h — meta de 7 dias de serie continua ({progresso:.1f}%)</div>

<div class="cols">
<div><h2>Spread por categoria</h2>
<table><tr><th>categoria</th><th>obs</th><th>mediana</th></tr>{linhas_cat}</table></div>
<div><h2>Log recente</h2>
<table><tr><th>hora</th><th>componente</th><th>mensagem</th></tr>{linhas_log}</table></div>
</div>
</body></html>"""


def pagina(con: duckdb.DuckDBPyConnection, runtime: dict) -> str:
    return render(coletar_stats(con, runtime))
