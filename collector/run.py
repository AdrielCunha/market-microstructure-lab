"""Processo único que roda todos os coletores.

DuckDB não aceita dois processos escrevendo o mesmo arquivo, então books,
wallets e o refresh do catálogo compartilham um Store aqui. O flush vai para
uma thread separada — gravar no meio de uma rajada do WebSocket travaria o
event loop e criaria buracos na série.

    python -m collector.run
    python -m collector.run --minutes 30
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import os
import signal
import threading
import time

from collector import catalog
from collector.books import BookCollector
from collector.wallets import WalletCollector
from core import config
from core.api import PolymarketAPI
from core.db import Store, connect
from analysis import markout, nichos
from engine.paper import REGRAS, PaperEngine
from engine.settlement import liquidar_resolvidos
from engine.strategy import MakerSimples
from reports import dashboard, gate0, index, nav, ordens, paper_dash, verify

# Relatórios que produzem texto puro. Todos passam pelo mesmo embrulho HTML.
TEXTUAIS = {
    "/gate0": gate0.main,
    "/verify": verify.main,
    "/nichos": nichos.main,
    "/markout": markout.main,
}


async def servidor_dashboard(store: Store, books: BookCollector,
                             wallets: WalletCollector, started: float,
                             porta: int, motores: list | None = None) -> None:
    """Serve o dashboard em localhost.

    Roda aqui dentro porque o DuckDB trava o arquivo num único escritor: com o
    coletor gravando, nenhum outro processo consegue abrir o banco nem para
    leitura. `con.cursor()` dá uma conexão independente sobre o MESMO banco em
    memória do processo, então a consulta do dashboard não disputa a conexão de
    escrita.
    """
    leitura = store.con.cursor()

    def runtime() -> dict:
        elapsed = time.monotonic() - started
        total_top = books.rows_kept + books.rows_deduped
        return {
            "uptime_s": elapsed,
            "eventos": books.events_seen,
            "taxa": books.events_seen / elapsed if elapsed else 0,
            "dedupe_pct": 100 * books.rows_deduped / total_top if total_top else 0,
            "buffer": store.pending(),
            "gravadas": store.rows_written,
            "geracao": books.geracao,
            "probes": wallets.probes,
        }

    def estado_paper() -> dict:
        """Snapshot em memória dos motores de paper trading.

        O ledger vive no processo; o banco só guarda as execuções. Por isso o
        painel lê o estado daqui e o histórico de lá.
        """
        out = {}
        for mot in (motores or []):
            mids = {t: st.ultimo_topo.mid
                    for t, st in mot.estado.items()
                    if st.ultimo_topo is not None and st.ultimo_topo.mid is not None}
            mot.ledger.marcar(mids)
            out[mot.nome] = {
                # O objeto vivo, para o painel calcular o valor de SAÍDA — o que
                # entraria na conta se desmontássemos tudo agora. Isso precisa
                # dos preços do livro, então não dá para vir do `resumo`.
                "ledger_obj": mot.ledger,
                "ledger": mot.ledger.resumo(mids),
                "metricas": {
                    "fills": mot.m.fills, "compras": mot.m.fills_compra,
                    "vendas": mot.m.fills_venda, "volume": mot.m.volume,
                    "rebates": mot.m.rebates, "quotes": mot.m.quotes_postadas,
                    "saidas_forcadas": mot.saidas_forcadas,
                    "liquidacoes": mot.liquidacoes,
                },
            }
        return out

    def _com_lock(func, *args):
        with store.db_lock:
            return func(*args)

    def _relatorio(func) -> str:
        """Roda um relatório capturando o stdout.

        As análises normalmente abrem a própria conexão, mas com a coleta
        rodando o DuckDB trava o arquivo — nenhum processo de fora consegue
        abrir o banco. Injetamos a conexão de leitura para que o veredito possa
        ser consultado SEM parar a coleta.
        """
        buf = io.StringIO()
        # Relatorios sao consultas pesadas na MESMA base que o coletor grava.
        # Sem o lock, duas threads usam o DuckDB ao mesmo tempo e ele congela.
        with store.db_lock, contextlib.redirect_stdout(buf):
            func(con=leitura)
        return buf.getvalue()

    # Relatórios prontos, recalculados em segundo plano.
    #
    # Antes, cada carregamento de página disparava a consulta na hora, segurando
    # o `db_lock` por 20 a 40 segundos — e nesse tempo o coletor PARAVA de
    # processar evento. Uma consulta passou de 300s e o watchdog matou o
    # processo: `309s sem processar evento algum`. Ou seja, abrir o painel
    # derrubava a coleta, e piorava conforme o banco crescia.
    #
    # Agora o cálculo acontece num relógio próprio, e a página só entrega o que
    # já está pronto. Carregar o painel virou custo zero para o banco.
    cache: dict[str, tuple[float, str]] = {}

    def _servir(rota: str) -> str:
        pronto = cache.get(rota)
        if pronto is None:
            return nav.pagina_texto(
                rota.strip("/"),
                "Relatorio ainda nao calculado.\n\n"
                "Ele e recalculado em segundo plano de tempos em tempos, para\n"
                "que abrir esta pagina nunca dispute o banco com o coletor.\n"
                "Aguarde alguns minutos e recarregue.", rota)
        idade = time.monotonic() - pronto[0]
        return pronto[1].replace(
            "</pre>", f"</pre><div style='color:#5f6874;font-size:12px;"
                      f"padding-top:10px'>calculado ha {idade/60:.0f} min</div>")

    async def precomputador(minutos: float) -> None:
        """Recalcula os relatórios pesados fora do caminho da requisição."""
        while True:
            for rota, fn in TEXTUAIS.items():
                try:
                    texto = await asyncio.to_thread(_relatorio, fn)
                    cache[rota] = (time.monotonic(),
                                   nav.pagina_texto(rota.strip("/"), texto, rota))
                except Exception as exc:
                    store.log("run", "warn", "precompute falhou",
                              {"rota": rota, "erro": repr(exc)[:300]})
                # Respira entre relatórios: soltar o lock deixa o coletor
                # drenar o buffer antes da próxima consulta pesada.
                await asyncio.sleep(5)
            await asyncio.sleep(minutos * 60)

    async def handler(reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            pedido = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError):
            writer.close()
            return
        try:
            alvo = pedido.split(b" ")[1].decode()
            rota, _, consulta = alvo.partition("?")
        except (IndexError, UnicodeDecodeError):
            rota, consulta = "/", ""
        # Só um parâmetro é usado (`e`, qual motor mostrar). Um dicionário
        # completo de query string aqui seria mais superfície do que o painel
        # precisa.
        escolhido = None
        for par in consulta.split("&"):
            chave, _, valor = par.partition("=")
            if chave == "e" and valor:
                escolhido = valor[:80]

        tipo = "text/html; charset=utf-8"
        try:
            # Relatórios de texto vão embrulhados em HTML com a barra de
            # navegação: em texto puro eram becos sem saída — para sair, só o
            # botão voltar do navegador.
            if rota in TEXTUAIS:
                corpo = _servir(rota)
            elif rota == "/paper":
                estado = estado_paper()
                corpo = await asyncio.to_thread(_com_lock, paper_dash.pagina,
                                                leitura, estado)
            elif rota == "/ordens":
                corpo = await asyncio.to_thread(_com_lock, ordens.pagina,
                                                leitura, escolhido)
            elif rota == "/coleta":
                rt = runtime()
                corpo = await asyncio.to_thread(_com_lock, dashboard.pagina,
                                                leitura, rt)
            else:
                corpo = index.pagina()
        except Exception as exc:
            corpo = f"erro em {rota}: {exc!r}"
            tipo = "text/plain; charset=utf-8"
        dados = corpo.encode("utf-8")
        writer.write(b"HTTP/1.1 200 OK\r\n"
                     + f"Content-Type: {tipo}\r\n".encode()
                     + b"Cache-Control: no-store\r\n"
                     + f"Content-Length: {len(dados)}\r\n\r\n".encode()
                     + dados)
        try:
            await writer.drain()
        finally:
            writer.close()

    # Fora de container, 127.0.0.1: o painel não tem autenticação nenhuma e não
    # pode aparecer na rede local.
    #
    # DENTRO do container é obrigatório escutar em 0.0.0.0 — e isso NÃO expõe
    # nada: o Docker encaminha para o IP do container, e um servidor preso ao
    # loopback interno simplesmente não recebe (foi o que aconteceu: `healthy`
    # por dentro, conexão recusada por fora). Quem limita o alcance é a
    # publicação da porta em `127.0.0.1:8787` no compose, mais o firewall.
    endereco = os.environ.get("PMLAB_DASHBOARD_HOST", "127.0.0.1")
    servidor = await asyncio.start_server(handler, endereco, porta)
    store.log("run", "info", "dashboard no ar",
              {"porta": porta, "endereco": endereco})
    print(f"\n  PAINEL: http://127.0.0.1:{porta}")
    print("    /coleta   saude do coletor      /paper    paper trading")
    print("    /gate0    veredito              /verify   integridade")
    print("    /markout  selecao adversa       /ordens   ordem a ordem\n")

    from core import config as _cfg
    minutos = float(_cfg.load()["analise"].get("precompute_minutos", 10))
    tarefa = asyncio.create_task(precomputador(minutos), name="precompute")
    try:
        async with servidor:
            await servidor.serve_forever()
    finally:
        tarefa.cancel()


def iniciar_watchdog(books: BookCollector, limite_s: float = 300.0) -> None:
    """Mata o processo se o event loop parar de progredir.

    Aconteceu em producao: depois de 2,7h o coletor congelou com o processo
    vivo e a porta escutando, sem processar nada. Travar em silencio numa
    coleta de dias e pior que cair — ninguem percebe. O watchdog roda numa
    thread propria (nao depende do loop) e transforma o congelamento numa
    saida visivel, com codigo de erro.
    """

    def vigiar() -> None:
        while True:
            time.sleep(30)
            ocioso = time.monotonic() - books.last_event_ts if books.last_event_ts else 0
            if books.last_event_ts and ocioso > limite_s:
                print(f"\n!! WATCHDOG: {ocioso:.0f}s sem processar evento algum.",
                      flush=True)
                print("!! O event loop travou. Encerrando para que a falha seja "
                      "visivel.", flush=True)
                os._exit(1)

    threading.Thread(target=vigiar, daemon=True, name="watchdog").start()


async def liquidador(store: Store, motores: list, minutos: float) -> None:
    """Fecha posicoes cujo mercado ja resolveu.

    Sem isto a simulacao nunca fecha a conta: o jogo acaba e a posicao fica
    pendurada como 'nao realizado' para sempre.
    """
    while True:
        await asyncio.sleep(minutos * 60)
        try:
            n = await liquidar_resolvidos(store, motores)
            if n:
                print(f"  [liquidacao] {n} posicoes fechadas por resolucao")
        except Exception as exc:
            store.log("paper", "warn", "liquidacao falhou", {"erro": repr(exc)[:300]})


async def flusher(store: Store, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        await asyncio.to_thread(store.flush)


async def catalog_refresher(store: Store, books: BookCollector, minutes: float,
                            taxas: dict | None = None,
                            fins: dict | None = None) -> None:
    """Mantém o catálogo atualizado e reassina o WebSocket em voo.

    Mercados que vencem saem, novos entram. Sem isto, uma coleta de dias
    passaria a maior parte do tempo vigiando mercados já resolvidos.

    `taxas` e `fins` são os mesmos dicionários que os motores de paper trading
    consultam. Precisam ser atualizados aqui: token novo que entra sem data de
    resolução conhecida faz a estratégia achar que o mercado nunca acaba, e ela
    volta a cotar até o apito — que é justamente o defeito que a gestão de
    estoque existe para consertar.
    """
    while True:
        await asyncio.sleep(minutes * 60)
        try:
            tokens = await catalog.refresh(store)
            mudou = books.update_tokens(tokens)
            if taxas is not None or fins is not None:
                for tid, rate, fim in store.execute(
                        "SELECT token_id, fee_rate, epoch_ms(end_date) "
                        "FROM markets").fetchall():
                    if taxas is not None and rate is not None:
                        taxas[tid] = rate
                    if fins is not None and fim is not None:
                        fins[tid] = fim
            print(f"  [catalogo] {len(tokens)} tokens"
                  f"{' — reassinando' if mudou else ' — sem mudanca'}")
        except Exception as exc:
            store.log("run", "warn", "refresh de catalogo falhou", {"erro": repr(exc)[:300]})


async def heartbeat(store: Store, books: BookCollector, wallets: WalletCollector,
                    started: float) -> None:
    last_events = 0
    while True:
        await asyncio.sleep(60)
        elapsed = time.monotonic() - started
        rate = (books.events_seen - last_events) / 60
        last_events = books.events_seen
        silence = time.monotonic() - books.last_event_ts if books.last_event_ts else -1
        total_top = books.rows_kept + books.rows_deduped
        dedupe_pct = 100 * books.rows_deduped / total_top if total_top else 0
        store.log("run", "info", "heartbeat", {
            "uptime_s": round(elapsed),
            "book_events": books.events_seen,
            "eventos_por_s": round(rate, 1),
            "silencio_s": round(silence, 1),
            "topo_gravado": books.rows_kept,
            "topo_descartado_pct": round(dedupe_pct, 1),
            "trades_novos": wallets.trades_novos,
            "trades_lidos": wallets.trades_seen,
            "linhas_gravadas": store.rows_written,
            "buffer": store.pending(),
        })
        print(f"[{round(elapsed):>6}s] eventos={books.events_seen:>9,} ({rate:>6.1f}/s)  "
              f"topo={books.rows_kept:>8,} (dedup {dedupe_pct:>4.1f}%)  "
              f"trades={wallets.trades_novos:>6,}  "
              f"gravadas={store.rows_written:>8,}  buffer={store.pending():>6,}")


async def main(minutes: float | None) -> None:
    cfg = config.load()
    con = connect()
    store = Store(con,
                  flush_rows=cfg["books"].get("flush_rows", 2000),
                  flush_interval_s=cfg["books"].get("flush_interval_s", 3.0),
                  auto_flush=False)

    print("Atualizando catalogo...")
    tokens = await catalog.refresh(store)
    print(f"  {len(tokens)} tokens selecionados")

    books = BookCollector(store, tokens)
    wallets = WalletCollector(store)

    # Fase 1: um motor por regra de execucao, sobre o MESMO fluxo de mercado.
    # As duas emparedam o resultado real de um market maker.
    pcfg = cfg["paper"]
    motores: list[PaperEngine] = []
    # Fora do `if` porque o refresh de catálogo os atualiza incondicionalmente.
    taxas: dict[str, float] = {}
    fins: dict[str, int] = {}
    if pcfg.get("ligado", True):
        taxas.update(con.execute(
            "SELECT token_id, fee_rate FROM markets "
            "WHERE fee_rate IS NOT NULL").fetchall())

        def fee_lookup(token_id: str) -> tuple[float, float]:
            rate = taxas.get(token_id) or 0.05
            # rebate segue o regime: 15% em esportes (0.05), 25% fora (0.07)
            return (rate, 0.25 if rate > 0.06 else 0.15)

        # Quando cada mercado resolve, em epoch ms. É o que deixa a estratégia
        # parar de abrir e sair antes do apito. Em epoch de propósito: comparar
        # datetime com e sem fuso é fonte silenciosa de erro, e `ts_local` já é
        # epoch ms. O dicionário é atualizado junto com o catálogo (mercados
        # vencem e novos entram), então precisa ser mutável e compartilhado.
        fins.update(con.execute(
            "SELECT token_id, epoch_ms(end_date) FROM markets "
            "WHERE end_date IS NOT NULL").fetchall())

        def fim_lookup(token_id: str) -> int | None:
            return fins.get(token_id)

        # Uma instancia por (regra x latencia). Rodar a MESMA estrategia com
        # latencia 0 e com a latencia real mede quanto a latencia custa: a
        # diferenca entre as duas colunas e o preco de estar a 230ms do
        # exchange, e nao uma estimativa.
        latencias = [int(x) for x in pcfg.get("latencias_ms", [0, 230])]
        for regra in REGRAS:
            for lat in latencias:
                estrategia = MakerSimples(
                    size=float(pcfg.get("size_cotas", 100)),
                    spread_minimo_c=float(pcfg.get("spread_minimo_c", 1.0)),
                    preco_min=float(pcfg.get("preco_min", 0.10)),
                    preco_max=float(pcfg.get("preco_max", 0.90)),
                    posicao_maxima=float(pcfg.get("posicao_maxima_cotas", 500)),
                    skew_maximo_c=float(pcfg.get("skew_maximo_c", 2.0)),
                    minutos_sem_abrir=float(pcfg.get("minutos_sem_abrir", 30)),
                    minutos_saida_forcada=float(
                        pcfg.get("minutos_saida_forcada", 10)),
                    preco_min_venda=float(pcfg.get("preco_min_venda", 0.15)))
                mot = PaperEngine(store, estrategia, regra, fee_lookup,
                                  float(pcfg.get("exposicao_maxima_usd", 5000)),
                                  float(pcfg.get("capital_inicial_usd", 1000)),
                                  lat, fim_lookup)
                # Reconstroi o livro-caixa das execucoes ja gravadas. Sem isto
                # todo reinicio zerava o experimento — e com deploy automatico
                # um push destruiria dias de medicao.
                try:
                    n = mot.restaurar()
                    if n:
                        print(f"  [{mot.nome}] {n:,} execucoes restauradas")
                except Exception as exc:
                    store.log("paper", "warn", "restauracao falhou",
                              {"motor": mot.nome, "erro": repr(exc)[:300]})
                motores.append(mot)
                books.on_top_callbacks.append(mot.on_top)
                books.on_trade_callbacks.append(mot.on_trade)
        print(f"  Paper trading ligado: {len(motores)} motores "
              f"({', '.join(m.nome for m in motores)})")
    # Descobre as carteiras do leaderboard antes de subir as tasks, para o
    # resumo abaixo mostrar o número real.
    async with PolymarketAPI() as api:
        await wallets.discover(api)
    started = time.monotonic()
    iniciar_watchdog(books, float(cfg["books"].get("watchdog_s", 300)))

    tasks = [
        asyncio.create_task(books.run(), name="books"),
        asyncio.create_task(wallets.run(), name="wallets"),
        asyncio.create_task(flusher(store, store.flush_interval_s), name="flusher"),
        asyncio.create_task(heartbeat(store, books, wallets, started), name="heartbeat"),
        asyncio.create_task(
            catalog_refresher(store, books,
                              cfg["catalog"].get("refresh_minutes", 30),
                              taxas, fins),
            name="catalog"),
        asyncio.create_task(
            liquidador(store, motores,
                       float(cfg["paper"].get("liquidacao_minutos", 10))),
            name="liquidador"),
        asyncio.create_task(
            servidor_dashboard(store, books, wallets, started,
                               int(cfg["dashboard"].get("porta", 8787)),
                               motores),
            name="dashboard"),
    ]

    stop = asyncio.Event()

    def _stop(*_: object) -> None:
        stop.set()

    with contextlib.suppress(NotImplementedError, ValueError):
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

    print(f"Coletando de {len(wallets.wallets)} carteiras e {len(tokens)} tokens. Ctrl+C para parar.\n")
    try:
        if minutes:
            await asyncio.wait_for(stop.wait(), timeout=minutes * 60)
        else:
            await stop.wait()
    except (asyncio.TimeoutError, KeyboardInterrupt):
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(store.flush)
        elapsed = time.monotonic() - started
        total_top = books.rows_kept + books.rows_deduped
        print(f"\nEncerrado apos {elapsed:.0f}s")
        print(f"  eventos processados   : {books.events_seen:,} ({books.events_seen/elapsed:.0f}/s)")
        print(f"  topos gravados        : {books.rows_kept:,}")
        print(f"  topos descartados     : {books.rows_deduped:,} "
              f"({100*books.rows_deduped/total_top if total_top else 0:.1f}% sem mudanca)")
        print(f"  trades de carteiras   : {wallets.trades_seen:,}")
        print(f"  linhas gravadas       : {store.rows_written:,}")
        for mot in motores:
            r = mot.ledger.resumo({})
            print(f"  [paper] {mot.nome:<28} fills={mot.m.fills:>6,}  "
                  f"fechadas={r['fechamentos']:>4}  "
                  f"realizado=${r['realizado']:>8,.2f}  "
                  f"travado=${r['capital_travado']:>9,.2f}")
        store.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Coletor Fase 0")
    ap.add_argument("--minutes", type=float, default=None,
                    help="parar automaticamente apos N minutos (padrao: roda ate Ctrl+C)")
    args = ap.parse_args()
    asyncio.run(main(args.minutes))
