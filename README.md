# pmlab — Fase 0: medir antes de arriscar

> **Chegando agora ou voltando depois de um tempo? Leia [CONTEXT.md](CONTEXT.md)
> primeiro** — ele tem o histórico da decisão, todos os números já medidos, os
> bugs que não devem ser reintroduzidos e o estado atual.

Laboratório de instrumentação do Polymarket. **Fase 0 não envia nenhuma ordem e
não usa chave privada** — só endpoints públicos de leitura. O objetivo é
produzir os números que decidem se existe negócio aqui, antes de qualquer
dinheiro entrar.

## Instalação

```bash
python -m pip install duckdb polars pyarrow httpx websockets pytest
```

## Uso

```bash
# 1. Coletar. Roda até Ctrl+C; --minutes para parar sozinho.
python -m collector.run
python -m collector.run --minutes 60

# 2. Conferir que os dados prestam ANTES de acreditar em qualquer análise.
python -m reports.verify

# 3. O veredito.
python -m reports.gate0

# Análises individuais
python -m analysis.spreads       # onde o spread paga market making
python -m analysis.negrisk       # arbitragem existe? dura quanto?
python -m analysis.copyability   # copiar carteira dá lucro ou prejuízo?
python -m analysis.fees          # modelo de custo
python -m collector.catalog      # só atualizar o catálogo

python -m pytest tests/ -q
```

Tudo é configurado em `config.toml`.

## Como está organizado

```
core/       config, DuckDB (schema + escrita em lote), clientes HTTP
collector/  catalog (o que monitorar), books (WebSocket), wallets (carteiras),
            run (processo único que roda todos)
analysis/   fees (custo), spreads, negrisk, copyability
reports/    verify (integridade), gate0 (veredito)
```

Um processo só escreve no banco (`collector.run`) — DuckDB não aceita dois
escritores no mesmo arquivo. As análises abrem em modo leitura.

## Decisões que não são óbvias

**O catálogo seleciona eventos inteiros, não mercados.** Filtrar mercado a
mercado por liquidez descarta pernas fracas de um evento negative-risk, e somar
um subconjunto dos resultados fabrica "arbitragem" que não existe. Foi um bug
real: eventos incompletos apareciam somando $0,85 em vez de $1,00.

**98% das atualizações de livro são descartadas.** A maioria dos `price_change`
mexe em níveis fundos e deixa o topo igual. Toda a análise da Fase 0 olha só o
topo, então repetição não é gravada. Sem isso o banco cresceria alguns GB/dia.
Snapshots REST de auditoria nunca são descartados — são a referência de
conferência.

**A escrita usa DataFrame, não `executemany`.** O WebSocket entrega ~800
eventos/s; inserção linha a linha não acompanha e o buffer cresce sem parar.

**O ASOF join da análise de cópia olha para a FRENTE no tempo.** Quem copia vê
o trade e só então consulta o livro — usar o livro anterior daria um preço que
nunca teria sido alcançável e faria a cópia parecer melhor do que é.

**A fórmula da taxa não está verificada.** O campo `fee_rate_bps` do WebSocket
veio `0` em 957/957 execuções observadas. Sabemos o `feeSchedule` do catálogo
(esportes: rate 0.05, rebate 0.15; não-esportes: 0.07 e 0.25), mas há duas
leituras plausíveis do multiplicador de preço. O modelo implementa as duas e usa
a mais cara por padrão. Isso só vira medição real na Fase 2, comparando com
trades on-chain.

## O que já foi medido

Coleta ainda curta (minutos, não semanas) — direção, não veredito final.

| Medida | Valor |
|---|---|
| Latência desta máquina → CLOB | ~230ms TTFB |
| Throughput do WebSocket | ~800 eventos/s em 400 tokens |
| Spread mediano do livro | 1–2 centavos |
| Soma dos resultados em eventos negRisk | ~$1,02 (mediana) |
| Custo de montar cesta de arbitragem | ~$0,05 por $1 de payoff |
| **Atraso até um trade aparecer na API** | **mínimo 56s, mediana ~5min** |
| Desvantagem de entrada ao copiar | ~2 centavos/cota, pior em 78% dos casos |
| Custo total de copiar | ~8,9% do notional |

O atraso da API não é do poll (que roda a cada 3s) nem da rede: é do indexador
do Polymarket. Nenhum servidor nos EUA reduz esse número.

## Estado dos critérios do Gate 0

- **Arbitragem negative-risk — FALHA.** O desvio existe (~2% de sobre-preço),
  mas é menor que o custo de capturá-lo. Arb aparente < custo não é arb.
- **Copy trading — FALHA.** Custa ~8,9% do notional para copiar, contra margem
  de ~1,28% do trader copiado.
- **Market making — INCONCLUSIVO, por construção.** O que dá para medir só com
  o livro é a receita bruta. O custo que decide — seleção adversa, ser executado
  justamente quando o preço vai contra — não aparece no livro: só nos fills, e
  fill só existe com ordem postada. Declarar PASS aqui seria o falso positivo
  clássico. Resolve-se na Fase 1, com paper trading.
