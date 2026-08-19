# pmlab — medindo se dá para ganhar dinheiro no Polymarket

**Português** · [English](README.md)


Laboratório de instrumentação para responder, **com dado e não com opinião**, se
existe um negócio sistemático de trading no [Polymarket](https://polymarket.com).

O projeto está **concluído**. A resposta foi *não* — e o valor está em *como* se
chegou nela, e no que sobrou pelo caminho.

> **Nenhuma ordem real foi enviada. Não existe chave privada neste repositório.**
> Todas as fases usaram apenas endpoints públicos de leitura.

---

## O veredito

Três teses foram testadas. As três morreram, cada uma com um número:

| tese | resultado | por quê |
|---|---|---|
| **Arbitragem** negative-risk | morta | 1 episódio em 30h, com duração de **0,0s**. A soma dos preços fica em $1,02 — sobrepreço, não desconto |
| **Copiar carteiras** vencedoras | morta | copiar custa **6,7% do notional**; a margem do trader copiado é **1,28%**. O trade só aparece na API pública **335s** depois de acontecer |
| **Market making** | morta | **−31,4%** em 5 dias, já a 15ms de latência e com 93% das cotações sobrevivendo |

A terceira é a que interessa. Ela não perdeu por falta de velocidade: a
velocidade foi comprada e medida. De 68 ciclos de posição, apenas **2** fecharam
encontrando contraparte no livro — o resto foi pagar para fugir ou segurar até o
mercado resolver.

**Custo total do experimento: algumas dezenas de dólares em servidor, zero em
dinheiro de operação.**

---

## A descoberta que ninguém tinha escrito

A intuição de mercado é *"coloque o bot em `us-east-1`"*. Para este exchange,
**está errada.**

O Polymarket fica atrás de Cloudflare e a origem é invisível. Foi preciso
triangular com cinco droplets descartáveis, medindo só endpoints dinâmicos
(`cf-cache-status: DYNAMIC`) para não medir cache de borda:

| de onde | PoP | round-trip | sobrevivência das cotações |
|---|---|---|---|
| São Paulo | GRU | 164ms | ~57% |
| DigitalOcean SFO3 | SJC | 148ms | ~59% |
| DigitalOcean NYC3 | EWR | 86ms | ~68% |
| DigitalOcean AMS3 | AMS | 20ms | ~92% |
| **DigitalOcean LON1** | **LHR** | **15ms** | **~93%** |

San Jose ser 67ms pior que Newark — a largura dos EUA — prova que a origem está
a leste. E 78ms a leste de Newark não é a Virgínia (~5ms), é o Atlântico.
Amsterdam fecha a conta: **o CLOB do Polymarket roda na Irlanda.**

Custo da descoberta: cerca de US$ 0,20 em droplets cobradas por hora.

---

## O dataset

18,4 dias de livro de ofertas de prediction market, tick a tick. Dado granular
desse tipo é escasso — foi a única coisa deste projeto que nunca deu resultado
negativo.

| tabela | linhas | conteúdo |
|---|---|---|
| `book_top` | **15.997.072** | topo de livro a cada mudança: bid, ask, tamanhos, spread |
| `book_events` | 1.811.141 | payload cru do WebSocket, para reprocessar |
| `wallet_trades` | 453.466 | trades públicos das 30 maiores carteiras, com o atraso de visão medido |
| `markets` | 14.778 | catálogo: taxas, resolução, estrutura negative-risk |
| `paper_fills` | 29.826 | execuções simuladas, com rebate e taxa por linha |

**77.985 tokens · 441,9 horas contínuas · 421 MB em Parquet** (de 3,88 GB em
DuckDB).

### Baixar

Os arquivos estão na [**Release `v1.0-dataset`**](https://github.com/AdrielCunha/market-microstructure-lab/releases/tag/v1.0-dataset) —
separados de propósito, para não obrigar a baixar 421 MB quem só quer o livro:

```bash
# só o livro de ofertas (123 MB) — é o que interessa a quase todo mundo
curl -LO https://github.com/AdrielCunha/market-microstructure-lab/releases/download/v1.0-dataset/book_top.parquet
```

```python
import duckdb
duckdb.sql("SELECT count(*), count(DISTINCT token_id) FROM 'book_top.parquet'")
# 15.997.072 linhas, 77.985 tokens
```

Esquema completo e armadilhas de interpretação em [DATASET.pt-BR.md](DATASET.pt-BR.md).

---

## O que torna isto um instrumento, e não uma planilha otimista

Um simulador que dá lucro em qualquer estratégia não vale nada. A maior parte do
esforço foi impedir o sistema de mentir a favor. **23 defeitos estão
documentados em [CONTEXT.md](CONTEXT.md)**, com o motivo de cada um — e quase
todos são da mesma família: *resultado bom demais escondendo o custo dominante*.

Alguns:

- **Um veredito de aprovação recusado.** Market making ia passar no Gate 0 com
  200x de folga sobre o custo — folga que ignorava seleção adversa. Foi criado
  um terceiro estado, `INCONCLUSIVO`, que por construção nunca devolve `PASS`.
- **Latência medida em endpoint cacheado** deu 50ms; o caminho real dava 164ms.
  O script agora **se recusa a reportar** se o Cloudflare disser `HIT`.
- **A própria verificação mentiu**: reportou *3.198 lacunas de 2 minutos numa
  janela de 225 minutos* — aritmeticamente impossível. Alarme falso é pior que
  nenhuma verificação: treina quem lê a ignorar o vermelho.
- **Vender a descoberto era de graça** na trava de capital: a simulação saiu com
  6.995 cotas vendidas contra 917 compradas, uma carteira impossível de montar.
- **Lucro de papel virava poder de fogo**, numa realimentação que produziu
  "$35.309 de lucro sobre $1.000" em 3 horas.

**142 testes** travam essas classes de erro para que não voltem.

---

## Arquitetura

```
collector/   WebSocket do CLOB (400 tokens simultâneos), catálogo, carteiras
engine/      simulador de market making: latência, fila, estoque, liquidação
analysis/    spreads, negative-risk, copyability, nichos, markout, latência
reports/     painéis HTTP servidos pelo próprio coletor
```

Decisões não óbvias, todas motivadas por falha real em produção:

- **Processo único.** O DuckDB trava o arquivo num escritor só — os painéis
  rodam dentro do coletor, sobre uma conexão de leitura com lock explícito.
- **Latência como matriz.** `latencias_ms = [0, 15, 170]` roda seis motores
  sobre o **mesmo tick de mercado**. Comparação pareada: o regime de mercado
  cancela, sobra só o efeito da distância.
- **Duas regras de execução em paralelo.** `cruzamento` conta execuções demais
  (o livro também se move por cancelamento), `negocio` conta de menos (o feed
  publica ~9 negócios para cada ~1.300 mudanças de preço). A verdade fica no
  meio, e a de baixo é a que decide.
- **Watchdog + `restart: unless-stopped`.** Congelar em silêncio numa coleta de
  dias é pior que cair. O watchdog transforma travamento em saída visível; o
  Docker reergue; o livro-caixa é reconstruído de `paper_fills`.
- **Janela de análise.** Com 3,9 GB acumulados, consulta sem limite estourava a
  memória do container e o kernel matava o processo. O DuckDB não enxerga
  cgroup: lê a RAM da máquina e se dá 80% dela.

---

## Rodando

```bash
pip install -r requirements.txt

python -m collector.run          # coleta (Ctrl+C para parar)
python -m reports.verify         # os dados prestam?
python -m reports.gate0          # o veredito
python -m analysis.latencia      # onde esta máquina está na escada
python -m pytest tests -q        # 142 testes
```

Painel em `http://127.0.0.1:8787` — coleta, carteira, ordem a ordem, markout,
nichos.

Com Docker: `docker compose up -d --build`. O painel fica preso em `127.0.0.1`
de propósito — não tem autenticação.

---

## O que eu levo daqui

O plano dizia, antes de qualquer linha de código: *"se não passar, o projeto
para aqui. Isso é sucesso, não fracasso: custou tempo, não dinheiro, e a
resposta é definitiva."*

Foi o que aconteceu. A parte difícil não foi construir o coletor — foi construir
um instrumento disposto a dizer **não**, e depois acreditar nele.
