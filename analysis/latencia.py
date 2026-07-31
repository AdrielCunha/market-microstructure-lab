"""Mede a latência desta máquina até o CLOB do Polymarket.

Rodar aqui e na VPS e comparar os dois números é o que decide se market making
é viável — e é a única medição do projeto que precisa ser refeita em cada
máquina. O `latencias_ms` do `config.toml` sai daqui.

    python -m analysis.latencia

## Por que ida-e-volta (RTT), e não metade

Poderia parecer que só a ida importa: a ordem fica viva quando o pedido CHEGA,
não quando a resposta volta. Mas o que mata um formador de mercado é o laço
completo:

    preço muda no exchange  ->  (ida) a informação chega até nós
                            ->  decidimos cancelar
                            ->  (volta) o cancelamento chega ao exchange

Uma perna de rede em cada sentido. Enquanto esse laço não fecha, a nossa ordem
velha continua exposta — e é exatamente ela que é executada quando o preço já
virou. Por isso o RTT é o número honesto, e é ele que alimenta `latencias_ms`.

## O que fazer com o resultado

Compare a mediana com a **vida do topo de livro** (`python -m analysis.nichos`).
Se a cotação morre antes de a nossa chegar, nascemos defasados.
"""

from __future__ import annotations

import statistics
import time

import httpx

CLOB = "https://clob.polymarket.com"
AMOSTRAS = 30

# ARMADILHA, e ela já pegou este script uma vez.
#
# O Polymarket está atrás de Cloudflare. Endpoints de catálogo
# (`/simplified-markets`, `/markets`) são servidos do PoP mais próximo — de São
# Paulo respondiam em 48ms com `cf-cache-status=HIT`. Isso NÃO é a latência até
# o exchange: é a latência até um cache na mesma cidade.
#
# Medido lado a lado, da mesma máquina, no mesmo minuto:
#     /simplified-markets   48ms   HIT       <- borda, mentira
#     /book                164ms   DYNAMIC   <- origem, verdade
#
# Ordem enviada não pode ser servida de cache: ela tem de chegar na origem. Por
# isso medimos SÓ endpoint dinâmico, e o script se recusa a reportar se o
# Cloudflare disser que serviu do cache.
CAMINHO_DINAMICO = "/book?token_id={token}"
# O token de teste vem da Gamma, não do `/simplified-markets` do CLOB. Contado
# na página 1 deste último: 928 mercados fechados, 59 abertos mas não aceitando
# ordem, 13 aceitando ordem porém fechados, e ZERO negociáveis. É um cemitério,
# e medir contra token morto mede o caminho de erro.
GAMMA = "https://gamma-api.polymarket.com/markets"

# Fração das cotações que sobreviveria a cada latência, medida sobre 844.582
# mudanças de topo em 1.024 tokens. Se o banco local existir, recalculamos com
# o dado de verdade; esta tabela é o fallback para uma máquina recém-criada.
ESCADA_MEDIDA = [
    (5, 97.2, "colocation / FPGA"),
    (20, 92.1, "VPS us-east-1"),
    (50, 77.7, "VPS bom"),
    (100, 64.4, "VPS medio"),
    (230, 51.8, "esta maquina, medido em 2026-07"),
    (500, 38.7, "ruim"),
]

# Round-trip medido a partir de cada PoP do Cloudflare, com conexão quente e
# `cf-cache-status: DYNAMIC`. Usado para triangular ONDE fica a origem, que o
# Cloudflare esconde (todos os hosts do Polymarket resolvem para IP dele).
#
# SJC (148ms) ser MUITO pior que EWR (86ms) elimina a costa oeste: a origem
# está a leste de Newark. E 78ms a leste de Newark (tirando os 8ms até a borda)
# é a distância da Europa — Irlanda/Londres batem, us-east-1 não, porque
# Newark→Ashburn seria ~5ms.
#
# CONFIRMADO em Londres: 15ms, dos quais 5ms são até a borda LHR. Sobram ~10ms
# de borda até a origem — a distância Londres→Dublin. O CLOB do Polymarket roda
# na Europa (quase certamente AWS eu-west-1, Irlanda), NÃO nos Estados Unidos.
# Toda a intuição de "colocar o bot em us-east-1" estava errada para este
# exchange, e custou três droplets descobrir. Valeu os centavos.
POPS_MEDIDOS = {
    "GRU": (164, "Sao Paulo, maquina de casa"),
    "EWR": (86, "DigitalOcean NYC3 — melhor dos EUA"),
    "SJC": (148, "DigitalOcean SFO3 — pior, origem NAO e costa oeste"),
    "LHR": (15, "DigitalOcean LON1 — ESCOLHIDO, ~10ms da origem"),
    "AMS": (20, "DigitalOcean AMS3 — 5ms pior que Londres"),
}


def token_de_teste() -> str | None:
    """Um token de mercado VIVO, com livro de verdade.

    Token de mercado resolvido devolve "No orderbook exists" — que percorre a
    rede igual, mas mede o caminho de erro, não o de trabalho. Aqui a diferença
    saiu igual (85ms contra 86ms), o que aliás foi útil: provou que o tempo é
    transporte e não processamento. Ainda assim, medir a coisa certa é barato.
    """
    import json
    try:
        g = httpx.get(GAMMA, timeout=20.0, params={
            "active": "true", "closed": "false", "limit": 20,
            "order": "liquidity", "ascending": "false",
            "liquidity_min": 20000}).json()
    except (httpx.HTTPError, ValueError):
        return None
    with httpx.Client(base_url=CLOB, timeout=20.0) as cli:
        for m in g if isinstance(g, list) else []:
            ids = m.get("clobTokenIds")
            if isinstance(ids, str):
                try:
                    ids = json.loads(ids)
                except ValueError:
                    continue
            for tid in (ids or []):
                try:
                    j = cli.get(f"/book?token_id={tid}").json()
                except (httpx.HTTPError, ValueError):
                    continue
                if j.get("bids") or j.get("asks"):
                    return str(tid)
    return None


def _amostrar(cliente: httpx.Client, caminho: str,
              n: int) -> tuple[list[float], str, str]:
    """Round-trip em ms com a conexão quente, mais o veredito do cache."""
    tempos: list[float] = []
    cache, pop = "?", "?"
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            r = cliente.get(caminho)
            r.read()
        except httpx.HTTPError:
            continue
        tempos.append((time.perf_counter() - t0) * 1000)
        cache = r.headers.get("cf-cache-status", "sem-cloudflare")
        pop = r.headers.get("cf-ray", "?").rsplit("-", 1)[-1]
        time.sleep(0.05)   # não martelar a API
    return tempos, cache, pop


def medir_frio(caminho: str) -> float | None:
    """Primeira requisição de uma conexão nova: inclui DNS + TCP + TLS.

    É o que se paga ao reconectar. Importa porque o WebSocket cai e reconecta:
    medido em produção, 37 reconexões em 30h.
    """
    t0 = time.perf_counter()
    try:
        with httpx.Client(base_url=CLOB, timeout=20.0) as c:
            c.get(caminho).read()
    except httpx.HTTPError:
        return None
    return (time.perf_counter() - t0) * 1000


def sobrevivencia_real(latencia_ms: float) -> tuple[float, int] | str:
    """Recalcula a sobrevivência com o livro coletado, se der para ler o banco.

    Devolve o motivo quando não dá: o caso mais comum não é "não existe" e sim
    "o coletor está rodando", já que o DuckDB tranca o arquivo num único
    escritor. Dizer "sem banco" ali seria diagnóstico errado.
    """
    try:
        from analysis.nichos import preparar
    except ImportError:
        return "modulo de analise indisponivel"
    try:
        df = preparar()
    except FileNotFoundError:
        return "ainda nao ha banco nesta maquina"
    except Exception as exc:
        if "already" in str(exc).lower() or "being used" in str(exc).lower():
            return "o coletor esta rodando e trancou o banco"
        return f"nao consegui ler o banco ({type(exc).__name__})"
    if df.is_empty():
        return "banco ainda sem dado de livro"
    return float((df["vida_ms"] > latencia_ms).mean() * 100), len(df)


def sobrevivencia_estimada(latencia_ms: float) -> float:
    """Interpola a escada medida. Saltar para o degrau seguinte subestimaria:
    166ms não é o mesmo que 230ms."""
    pontos = [(ms, s) for ms, s, _ in ESCADA_MEDIDA]
    if latencia_ms <= pontos[0][0]:
        return pontos[0][1]
    for (x0, y0), (x1, y1) in zip(pontos, pontos[1:]):
        if latencia_ms <= x1:
            fatia = (latencia_ms - x0) / (x1 - x0)
            return y0 + fatia * (y1 - y0)
    return pontos[-1][1]


def main() -> None:
    print("=" * 74)
    print("LATENCIA ATE O CLOB DO POLYMARKET")
    print("=" * 74)

    token = token_de_teste()
    if token is None:
        print("  Nao consegui um token de teste. Sem rede ou API fora do ar.")
        return
    caminho = CAMINHO_DINAMICO.format(token=token)
    print(f"alvo: {CLOB}/book  (dinamico — tem de chegar na origem)\n")

    frio = medir_frio(caminho)
    print(f"  conexao NOVA (DNS+TCP+TLS+resposta): "
          f"{'falhou' if frio is None else f'{frio:,.0f} ms'}")

    with httpx.Client(base_url=CLOB, timeout=20.0) as cliente:
        cliente.get(caminho).read()          # aquece
        tempos, cache, pop = _amostrar(cliente, caminho, AMOSTRAS)

    if not tempos:
        print("\n  Nenhuma amostra respondeu. Sem rede ou API fora do ar.")
        return

    print(f"  borda Cloudflare: {pop}   cache: {cache}")
    if pop in POPS_MEDIDOS:
        ms, nota = POPS_MEDIDOS[pop]
        print(f"    ({pop}: ~{ms}ms ate a origem em medicao anterior — {nota})")
    if cache.upper() == "HIT":
        print("\n  !! RECUSADO: o Cloudflare serviu do CACHE.")
        print("  Este numero e a distancia ate um cache, nao ate o exchange.")
        print("  Uma ordem enviada nao pode vir de cache. Medicao invalida.")
        return

    tempos.sort()
    def pct(p: float) -> float:
        return tempos[min(len(tempos) - 1, int(p * len(tempos)))]

    mediana = statistics.median(tempos)
    print(f"  conexao QUENTE, {len(tempos)} amostras:")
    print(f"    minimo   : {tempos[0]:>7,.0f} ms")
    print(f"    mediana  : {mediana:>7,.0f} ms   <<< use este em latencias_ms")
    print(f"    p90      : {pct(0.90):>7,.0f} ms")
    print(f"    maximo   : {tempos[-1]:>7,.0f} ms")

    print("\n--- O QUE ISSO SIGNIFICA ---")
    real = sobrevivencia_real(mediana)
    if isinstance(real, tuple):
        sobrev, n = real
        print(f"  Com o livro coletado nesta maquina ({n:,} mudancas de topo):")
        print(f"    {sobrev:.1f}% das cotacoes sobreviveriam a {mediana:,.0f} ms")
    else:
        print(f"  ({real} — estimando pela escada medida em 2026-07)")
        print(f"    ~{sobrevivencia_estimada(mediana):.0f}% das cotacoes "
              f"sobreviveriam a {mediana:,.0f} ms")

    print("\n  Escada de referencia (medida, 844.582 mudancas de topo):")
    for ms, sobrev, rotulo in ESCADA_MEDIDA:
        marca = "  <<< VOCE" if abs(ms - mediana) < 30 else ""
        print(f"    {ms:>4} ms -> {sobrev:>5.1f}% sobrevivem   ({rotulo}){marca}")

    print("\n--- PROXIMO PASSO ---")
    print(f"  Em config.toml, secao [paper]:")
    print(f"      latencias_ms = [{round(mediana)}, 230]")
    print("  Rodar os dois lado a lado mede o ganho da mudanca de maquina")
    print("  contra a linha de base antiga, no MESMO fluxo de mercado.")
    print("\n  Aviso: rode isto algumas vezes em horarios diferentes. Latencia")
    print("  de rede varia com carga, e uma amostra so pode enganar.")


if __name__ == "__main__":
    main()
