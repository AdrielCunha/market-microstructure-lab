# CONTEXTO DO PROJETO — leia isto primeiro

Documento de passagem de contexto entre sessões. Se você é uma sessão nova
(humano ou assistente) chegando neste repositório, **leia este arquivo antes de
propor qualquer coisa.** Ele registra o que já foi medido, o que já foi
descartado com dado, e quais suposições ainda estão em aberto.

Última atualização: 2026-07-29.

---

## 1. De onde isto veio

O usuário viu circular no Twitter/X a tese de que dá para ganhar muito dinheiro
no Polymarket com "matemática e fórmulas", e depois o perfil
[`@swisstony`](https://polymarket.com/@swisstony) (+$22M em ~1 ano). A pergunta
original foi:

> É possível desenvolver uma plataforma que usa IA para arbitrar e prever no
> Polymarket, e fazer dinheiro em pouco tempo?

O objetivo declarado, na ordem: **paper trading → dinheiro real pequeno →
escalar se funcionar → possivelmente virar produto/vender para uma empresa.**

A premissa "IA prevendo melhor que o mercado" foi verificada e **está errada**
(seção 3). A pergunta subjacente — "existe algo sistemático e lucrativo aqui?" —
é legítima e é o que este projeto mede.

Plano aprovado em:
`C:\Users\cunha\.claude\plans\um-cara-desenvolveu-algo-delightful-ladybug.md`

---

## 2. O que este projeto É (e o que NÃO é)

**É um aparelho de medição.** Fase 0 não envia ordem, não tem chave privada, só
usa endpoints públicos de leitura. Ele existe para responder "existe vantagem
real aqui?" **antes** de qualquer dinheiro entrar.

**Não é um bot de trading.** Nunca executou uma ordem. O diretório `engine/`
está vazio de propósito — o simulador da Fase 1 só faz sentido depois de dias
de série coletada.

---

## 3. O que já foi MEDIDO (não é opinião)

Todos os números abaixo vieram de consultas reais às APIs do Polymarket a partir
desta máquina, não de estimativa.

### O que o swisstony realmente faz

| Métrica | Valor |
|---|---|
| Lucro total | ~US$ 22,3M |
| Volume negociado | ~US$ 1,743 bilhão |
| **Margem sobre volume** | **~1,28%** |
| Trades | ~153.400 |
| Posições abertas (no momento da consulta) | ~US$ 186k |
| Ranking 30d | **#1**, US$ 8,95M |

Fills reais capturados ao vivo:

```
BUY  2.781607 cotas @ 0.87  → US$ 2,42   (Craiova 1st Half O/U 1.5)
BUY  3.15     cotas @ 0.31  → US$ 0,98   (Craiova vs Levski O/U 1.5)
```

Dois trades **no mesmo segundo**, de US$ 1 e US$ 2, em micro-mercados
esportivos. Isso não é previsão: é **market making** — ordens passivas
espalhadas por milhares de micro-mercados, executadas em farelo. Ele é o
*maker*; quem cruza o spread é a contraparte.

Mediana do notional dos fills observados: **~US$ 11**.

### As três teses testadas

| Tese | Veredito | Evidência |
|---|---|---|
| Arbitragem negative-risk | **FALHOU** | soma dos asks mediana $1,02 (sobre-preço, não desconto); melhor caso $0,99; custo da cesta $0,0515 por $1 de payoff → limiar $0,9485. **Zero episódios.** |
| Copy trading | **FALHOU** | custa 8,85% do notional para copiar, contra margem de 1,28% do copiado |
| Market making | **INCONCLUSIVO por construção** | ver seção 4 |

### Latências e atrasos (o dado mais importante)

- **Latência desta máquina → CLOB**: **~166–172ms** (mediana, conexão quente,
  endpoint dinâmico). Reproduzível: `python -m analysis.latencia`. Bots em
  `us-east-1` operam em <10ms; qualquer corrida de milissegundos está fora de
  alcance daqui.

  **O valor antigo de 230ms estava errado, e o de 50ms também.** O Polymarket
  fica atrás de Cloudflare. Medido lado a lado, da mesma máquina, no mesmo
  minuto:

  | endpoint | mediana | `cf-cache-status` |
  |---|---|---|
  | `/simplified-markets` | 48ms | **HIT** (borda GRU) |
  | `/markets` | 82ms | **HIT** |
  | `/book` | **164ms** | DYNAMIC (origem) |
  | `/price` | 164ms | DYNAMIC |
  | `/midpoint` | 171ms | DYNAMIC |

  Endpoint de catálogo é servido de um cache em São Paulo — 48ms é a distância
  até o cache, não até o exchange. Ordem enviada não pode vir de cache: tem de
  chegar na origem. Por isso `analysis/latencia.py` mede **só endpoint
  dinâmico** e **se recusa a reportar** se o Cloudflare devolver `HIT`.

  `POST /order` sem credencial responde 403 em 33ms — rejeitado na borda, não
  chega na origem. Não serve como medição.

- **ONDE FICA A ORIGEM DO CLOB** — triangulado com 3 droplets descartáveis,
  todos medindo `/book` com conexão quente e `cf-cache-status: DYNAMIC`:

  | de onde | PoP | até a borda | total | sobrevivência das cotações |
  |---|---|---|---|---|
  | São Paulo (casa) | GRU | — | 164–172ms | ~57% |
  | DigitalOcean SFO3 | SJC | 3ms | **148ms** | ~59% |
  | DigitalOcean NYC3 | EWR | 8ms | **86ms** | ~68% |
  | DigitalOcean AMS3 | AMS | 5ms | 20ms | ~92% |
  | **DigitalOcean LON1** | **LHR** | **5ms** | **~15ms** | **~94%** |

  **O CLOB do Polymarket roda na EUROPA, não nos Estados Unidos.** São Jose ser
  67ms pior que Newark (a largura dos EUA) prova que a origem está a leste de
  Newark; e 78ms a leste de Newark não é a Virgínia (~5ms), é o Atlântico. Em
  Londres sobram ~10ms depois da borda — a distância Londres→Dublin, o que
  aponta AWS `eu-west-1` (Irlanda).

  Amsterdam fecha a conta: 20ms totais, ~15ms depois da borda. Londres está a
  ~10ms da origem e Amsterdam a ~15ms — e Dublin fica a ~10ms de Londres e
  ~17ms de Amsterdam. **Irlanda confirmada por triangulação.** Frankfurt não
  foi testada porque seria necessariamente pior: está mais longe de Dublin que
  Amsterdam, que já perdeu.

  **LON1 é o fim da linha.** Dos 15ms, 5ms são só chegar na borda do
  Cloudflare — o piso real é ~10ms, e não existe região de VPS mais perto.

  A intuição de mercado ("põe o bot em `us-east-1`") está **errada para este
  exchange**. Custou três droplets por hora — centavos — descobrir, e nenhuma
  dedução chegou lá: as duas primeiras hipóteses (Virgínia, depois costa oeste)
  foram derrubadas por medição.

  **Consequência: 230ms → 15ms.** A sobrevivência das cotações sai de ~52% para
  ~94%, que é o degrau onde a tese de market making finalmente pode ser testada
  em condição justa.

- **Atraso até um trade aparecer na Data API pública**: medido em **3 coletas
  independentes**, sempre na mesma faixa:

  | coleta | n | mínimo | mediana | p95 |
  |---|---|---|---|---|
  | 1 | 105 | 56,7s | 252,7s | 439,7s |
  | 2 | 173 | 84,2s | 316,6s | 449,0s |
  | 3 | 100 | 174s | 284s | 383s |

  **O poll roda a cada 3 segundos.** O atraso não é da nossa infraestrutura: é
  do indexador do Polymarket. Nenhum servidor nos EUA reduz esse número. É isto
  que mata copy trading de forma definitiva.

### Custo de copiar (n=151 casos simulados contra livro real)

- Desvantagem mediana de entrada: **2,0 centavos/cota**
- Entraria pior que o copiado em **78% dos casos**
- Custo total (spread cruzado + taxa): **8,85% do notional**

### Estrutura de taxas (do catálogo Gamma)

| Categoria | taker rate | rebate de maker | takerOnly |
|---|---|---|---|
| Esportes (`sports_fees_v2`) | 0,05 | 0,15 | sim |
| Não-esportes | 0,07 | 0,25 | sim |

**O maker não paga taxa — ele recebe rebate.** É o motor econômico da coisa.

### Livro de ofertas

- Spread mediano: **1 a 2 centavos** (varia muito: MLB ~1,0c; LOL ~18c)
- Throughput do WebSocket: **~800 eventos/s** com 400 tokens
- **95–98,5%** das atualizações não mudam o topo do livro

---

## 4. Por que market making está "INCONCLUSIVO" e não "APROVADO"

Isto é uma decisão deliberada de engenharia e **não deve ser revertida sem
entender o motivo.**

O que dá para medir só com o livro é a **receita bruta** do maker (meio spread +
rebate). Rodando os números, ela dá folga de ~200x sobre gás e carrego. Seria
fácil carimbar PASS.

Mas o custo que **decide** se market making dá lucro é a **seleção adversa**:
sua ordem parada é executada preferencialmente quando o preço está prestes a
andar contra você (compraram de você porque acabou de sair gol). Esse custo
**não existe no livro de ofertas** — ele só se manifesta nos *fills*, e fill só
existe com ordem postada de verdade.

Declarar PASS com base em receita bruta seria produzir o falso positivo
clássico: uma folga enorme sobre um custo que exclui o custo dominante. Por isso
`reports/gate0.py` tem três estados (PASS / FAIL / INCONCLUSIVO) e o critério 2
**nunca** retorna PASS por construção.

---

## 5. Projeção de resultado (`python -m reports.projecao`)

Cenários, **não previsão**. Os dois parâmetros que decidem tudo — seleção
adversa e giro de capital — são exatamente os que ainda não foram medidos.

Cenário do meio (2 voltas completas/dia, seleção adversa média):

| Capital | Resultado/mês |
|---|---|
| US$ 500 | +US$ 32 (um VPS de $20 comeria 63% disso) |
| US$ 5.000 | +US$ 295 |
| US$ 50.000 | +US$ 3.130 |

Com seleção adversa pesada, **todas as linhas viram prejuízo** (−$189, −$1.910,
−$18.920 respectivamente). Esse cenário está na tabela de propósito: começar
perdendo é o caso base de quem entra competindo com operadores estabelecidos.

**Âncora de sanidade embutida no script**: se algum cenário produzir margem
sobre volume maior que os 1,28% do #1 do leaderboard, o relatório dispara
alerta — quando um modelo diz que você bate o melhor operador da plataforma, o
errado é o modelo. Atualmente o cenário mais otimista dá 0,70%, abaixo do
teto, então a economia *por operação* está conservadora. **Os percentuais
mensais altos vêm de GIRO, não de margem gorda.**

---

## 6. Arquitetura e decisões não-óbvias

```
core/       config, DuckDB (schema + escrita em lote), clientes HTTP
collector/  catalog (o que monitorar), books (WebSocket), wallets (carteiras),
            run (processo único que roda tudo + serve o dashboard)
analysis/   fees, spreads, negrisk, copyability
reports/    verify (integridade), gate0 (veredito), projecao, dashboard
engine/     VAZIO — Fase 1, ainda não construído
```

**Um único processo escreve no banco.** O DuckDB trava o arquivo para um único
escritor: enquanto `collector.run` grava, **nenhum outro processo consegue abrir
o banco, nem em modo leitura** (testado e confirmado). Consequências:
- books, wallets e catálogo compartilham um `Store` no mesmo processo;
- o dashboard é servido **de dentro** do coletor, via `con.cursor()`;
- rodar `reports.gate0` com o coletor ligado dá erro de arquivo em uso.

**O catálogo seleciona EVENTOS INTEIROS, não mercados.** Filtrar mercado a
mercado por liquidez descarta pernas fracas de um evento negative-risk, e somar
um subconjunto dos resultados fabrica arbitragem que não existe.

**98% das atualizações de topo são descartadas.** A maioria dos `price_change`
mexe em níveis fundos. Toda a análise da Fase 0 olha só o topo. Snapshots REST
de auditoria **nunca** são descartados — são a referência de conferência.

**A escrita usa DataFrame polars, não `executemany`.** Inserção linha a linha do
DuckDB não acompanha ~800 eventos/s; o buffer crescia sem parar.

**A fórmula da taxa NÃO está verificada.** `fee_rate_bps` do WebSocket veio `0`
em **957/957** execuções observadas. O modelo em `analysis/fees.py` implementa
as duas leituras plausíveis do multiplicador (`p*(1-p)` e `min(p,1-p)`) e usa a
**mais cara** por padrão. Só vira medição real na Fase 2, contra trades
on-chain.

---

## 7. Bugs já encontrados e corrigidos (não reintroduzir)

Registrados porque cada um produziria uma conclusão **errada com aparência de
rigor**:

1. **Eventos negative-risk incompletos.** O catálogo filtrava por mercado,
   quebrando a completude do evento. Somava 2 de 3 pernas e reportava
   "arbitragem" de 15%. Era perna faltando. Corrigido: seleção por evento
   inteiro + trava `count(*) = max(event_n_outcomes)` na análise.
2. **Gás normalizado por 1 cota** inflava o custo da cesta de 5% para 11%. Gás é
   fixo por transação; o cálculo tem que ser feito para uma cesta de tamanho
   realista e só então normalizado.
3. **ASOF join na direção errada do tempo.** Quem copia vê o trade e *só então*
   consulta o livro. Usar o livro anterior daria um preço nunca alcançável e
   faria a cópia parecer melhor do que é.
4. **`JOIN markets` interno** descartava justamente os tokens sondados sob
   demanda (que estão fora do catálogo). Cobertura da análise de cópia: 2% →
   100%. Tem que ser LEFT JOIN.
5. **Paginação da Gamma**: `limit` é silenciosamente capado em 100, e `offset`
   acima de ~2100 devolve 422. Filtros pesados têm que ir no servidor.
6. **Cache de topo na reassinatura**: precisa ser limpo entre gerações, senão o
   dedupe engole a primeira leitura dos tokens novos.
7. **Console Windows cp1252** quebrava ao imprimir tabelas polars. Corrigido em
   `core/__init__.py`.
8. **Endpoint do leaderboard**: é `https://lb-api.polymarket.com/profit?window=30d`
   (janelas aceitas: `1d`, `7d`, `30d`, `all` — `1m`/`1w` devolvem 400).
9. **A própria verificação mentiu.** A checagem de continuidade contava lacunas
   com `lag()` sobre timestamps distintos e reportou *3.198 lacunas de 2 minutos
   numa janela de 225 minutos* — não cabem no tempo observado. Trocada por
   contagem de MINUTOS SEM DADO, que é robusta e direta de interpretar, e que
   desconta os reinícios do coletor (parar e religar deixa buraco legítimo).
   `tests/test_verify.py` trava a classe do bug: lacunas nunca podem exceder a
   janela. Alarme falso é pior que nenhuma verificação — treina quem lê a
   ignorar o vermelho.

10. **Vender a descoberto era de graça.** `_dentro_do_risco` só cobrava capital
    do lado da COMPRA (`if c.bid is None: return True`). Consequência: com o
    caixa apertado, a compra esbarrava na trava e a venda passava livre. Em 30h
    a simulação saiu com **6.995 cotas vendidas contra 917 compradas** — 93% das
    liquidações eram de posição VENDIDA. Ficar vendido em YES a 26c é bancar os
    74c que se perde se o resultado acontecer; a simulação bancava $0. Corrigido
    em `_capital_da_cotacao` (cobra o maior dos dois lados, e venda que só reduz
    posição não pede garantia) e em `Ledger.capital_travado` (short trava
    `1 - custo`, não `custo` — subestimava em quase 3x). Travado por
    `test_vender_a_descoberto_custa_capital`.

    Efeito colateral do conserto: recotar o mesmo token passou a disputar
    capital consigo mesmo. `capital_disponivel(excluir=token_id)` resolve — a
    cotação nova SUBSTITUI a antiga.

14. **Falha silenciosa escondia o painel.** `paper_dash.coletar` engolia a
    exceção de `carteira.avaliar` com `except Exception: pass`. O bloco do valor
    real simplesmente sumia da tela, e quem olhasse concluiria "não foi
    implementado" em vez de "quebrou". Agora a falha aparece em vermelho, com o
    tipo do erro.

13. **Constante de latência cravada no código.** `analysis/nichos.py` tinha
    `LATENCIA_MS = 230`. A operação mudou para Londres e passou a rodar a 15ms,
    mas o módulo continuou respondendo *"onde um operador de 230ms consegue
    jogar"*. Medido na série histórica (1,47M mudanças de topo, 46,3h): a 230ms
    sobrevivem ~52% das cotações, **a 15ms sobrevivem 92,8%**. O ranking de
    nichos apontava para o lugar errado por um fator de quase dois.

    A primeira tentativa de conserto errou também: deduzir a latência real como
    o **maior** valor de `latencias_ms` devolvia **170** — a máquina antiga do
    Brasil, que fica na lista só para comparação. Por isso existe agora a chave
    explícita `latencia_real_ms`, e `tests/test_config_latencia.py` trava a
    classe inteira: a constante não pode voltar ao código, a latência real tem
    de estar entre as simuladas, e o motor de referência do painel tem de
    corresponder a ela.

12. **Medir latência num endpoint cacheado.** A primeira versão de
    `analysis/latencia.py` usava `/simplified-markets` e reportou **50ms** —
    quatro vezes melhor que a realidade. Era `cf-cache-status=HIT` num PoP do
    Cloudflare em São Paulo. O caminho real (`/book`, DYNAMIC) dá ~166ms.
    Um número bom demais que escondia o custo dominante — exatamente o padrão
    que este projeto já rejeitou antes. Corrigido: só endpoint dinâmico, e o
    script aborta se o cabeçalho disser HIT.

11. **O painel rotulava toda regra como `negocio`.** `nome.split("_")[-1]` pega
    a latência (`lat0`), não a regra. Todo painel de `cruzamento` vinha descrito
    com o texto da regra oposta. Corrigido para `split("_")[1]`.

---

## 7b. Fase 1 — paper trading (construída, resultados AINDA NÃO confiáveis)

`engine/` deixou de estar vazio. Roda dentro do próprio coletor, ligado por
callbacks em `BookCollector` (`on_top_callbacks`, `on_trade_callbacks`), para
que um erro na simulação nunca derrube a coleta — a série de mercado é o ativo,
a simulação é acessório.

**Duas regras de execução, enviesadas em direções opostas:**

- `cruzamento` — executa quando o topo passa por cima da cotação. **Conta
  demais**: o livro também se move por CANCELAMENTO, e cancelamento não executa
  ninguém.
- `negocio` — só executa com negócio impresso. Cada execução é real, mas
  **conta de menos**: o feed publica ~9 prints para cada ~1.300 mudanças de
  preço.

Medido em produção: 210 execuções por cruzamento contra 6 por negócio em 4
minutos. **Isso NÃO é propriedade matemática** — vem da esparsidade dos prints.
Com um print por tick a relação se inverte (há teste documentando isso). Por
isso a comparação válida entre as regras é o markout por execução, **nunca o
lucro absoluto**.

`analysis/markout.py` é a peça que o Gate 0 não conseguia produzir: para cada
execução, onde o mid estava 5s/30s/60s/300s depois. É a única medição possível
de seleção adversa.

### Contabilidade de carteira (feita depois, ao pedido do usuário)

A primeira versão do `Ledger` expunha **"caixa"**, que é fluxo de dinheiro e não
lucro: vender mais do que se comprou deixa o caixa alto mesmo estando perdendo.
Isso enganava. A conta agora é a que qualquer um reconhece:

    patrimonio = capital inicial + PnL realizado + rebates + PnL nao realizado

- **realizado** — de operação FECHADA, por custo médio. Já é seu.
- **não realizado** — da posição ABERTA, ao preço atual. É promessa.
- **capital travado / caixa livre** — quanto está preso e quanto sobra.

`reports/paper_dash.py` mostra cada valor **com a explicação do lado, na própria
tela** (dicionário `GLOSSARIO`), mais um veredito grande "EM LUCRO / EM
PREJUÍZO / NO ZERO" e avisos automáticos em vermelho.

### Dois bugs que só apareceram por causa dessa contabilidade

1. **A simulação operava com dinheiro que não tinha.** Capital travado de
   **$2.556 sobre capital inicial de $1.000**, com caixa livre em **−$1.505**.
   Barrar na hora de postar a cotação não bastava: cotações já vivas em ~400
   tokens continuavam sendo executadas. Corrigido com `_comprometido` — o
   notional das cotações de compra vivas conta contra o capital, igual a um
   market maker de verdade dimensionando contra o capital total.
2. **O "lucro" era quase todo rebate.** ~60% do resultado vinha do rebate de
   maker, cuja fórmula **não está verificada**. O painel agora avisa sozinho
   quando o rebate passa de metade do lucro.

**Efeito da correção**: o resultado caiu de **+$68,84 (+6,88%) para +$2,43
(+0,24%)** na mesma janela. Fator de 28. Todo o excedente vinha de capital
inexistente.

### Modelagem de latência (crítica levantada pelo usuário — estava certa)

O usuário perguntou: *"em paper trading a latência é nada, porque você faz as
contas; numa operação real pode demorar, fazendo com que nada disso funcione"*.
Estava correto e era a falha mais grave do simulador.

O motor recotava **no mesmo instante** em que o livro mudava — um operador de
latência ZERO, fisicamente impossível e favorável exatamente onde importa:
nunca ficaria com ordem defasada no livro. **Ordem defasada sendo executada É a
seleção adversa**; sem latência, o simulador não via o custo principal.

Agora `EstadoToken` separa `cotacao` (viva no livro) de `pendente` (decidida,
ainda em trânsito). A cotação só passa a valer em `ts + latencia_ms`, e enquanto
isso é a ANTIGA que fica exposta e é executada.

`config.toml` roda uma matriz `latencias_ms = [0, 15, 170]` × 2 regras = 6
motores sobre o mesmo fluxo: teto impossível, Londres real, e a máquina antiga
do Brasil mantida para comparação. **A diferença entre as colunas é o preço da
distância** — medido, não estimado, e sobre o MESMO tick de mercado, o que é
mais limpo do que comparar duas máquinas.

`latencia_real_ms = 15` é uma chave separada e explícita: qual das latências
descreve a máquina de verdade **não se adivinha**. Deduzir isso da lista já
produziu resposta errada (ver bug 13).

Isso expôs mais um furo na trava de capital: o compromisso só era contado quando
a cotação ficava viva, então centenas de ordens viajavam ao mesmo tempo sem
consumir limite ($2.080 travados sobre $1.000). Corrigido — a ordem compromete
capital no ENVIO.

**O que ainda NÃO é modelado** (e não dá para modelar com dados públicos):
posição na fila em cada nível de preço, e latência de CANCELAMENTO separada da
de postagem. Um maker real perde dinheiro justamente por não conseguir cancelar
a tempo; aqui isso só aparece indiretamente.

### Gestão de estoque e liquidação por resolução

- **Liquidação** (`engine/settlement.py`): no Polymarket todo mercado termina e
  cada cota vira $1 ou $0. Fonte: `GET clob.polymarket.com/markets/{condition_id}`
  → `closed` + `tokens[].winner/price`. Roda a cada 10 min. Sem isso toda posição
  ficava pendurada como "não realizado" para sempre, mesmo com o jogo encerrado.

#### O diagnóstico que motivou a reescrita (medido em 30,3h)

A decomposição `livro` vs `resolucao` no `Ledger` respondeu a pergunta central:

| motor | Realizado NO LIVRO | fechadas | Realizado NA RESOLUÇÃO | liquidadas |
|---|---|---|---|---|
| `negocio_lat0` | +$4,30 | 9 | −$19,65 | 36 |
| `negocio_lat230` | **+$1,90** | 6 | **−$238,29** | 35 |

Market making capturou **$1,90 em 9h**. Todo o resto foi moeda no ar. E o
capital ficou preso **309 minutos de mediana** (p75 371, máx 1.006).

O mecanismo, medido em `analysis/estoque.py` e nas consultas de acerto:

| | vendeu a | acerto observado | acerto necessário |
|---|---|---|---|
| lat 0ms | 28,1c | **74,4%** | 71,8% |
| lat 230ms | 31,6c | **50,0%** | 68,5% |

Mesma estratégia, mesmos mercados, mesmo período — só muda a latência. A 230ms
a ordem parada é levada justamente quando a notícia já saiu. **É seleção adversa
aparecendo na RESOLUÇÃO, não no markout de 5s.**

Agregado (107 posições): vendemos a 36,9% de chance implícita, aconteceu 42,1%.

#### As quatro travas (`MakerSimples`)

1. **Desvio assimétrico** — o lado da SAÍDA aproxima do mercado, o lado da
   ENTRADA se afasta. A versão anterior deslocava os dois igualmente: a cotação
   mudava de lugar mas mantinha a largura, então entrava tanto quanto saía.
2. **`minutos_sem_abrir` (30)** — perto da resolução, só cota o lado que REDUZ.
3. **`minutos_saida_forcada` (10)** — atravessa o spread e zera.
4. **`preco_min_venda` (0,15)** — não abre venda em azarão barato.

#### Maker ou taker se decide na CHEGADA, nunca depois

`PaperEngine._executar_se_marketable` roda **só** dentro de `_promover`. Ordem
parada que o mercado atravessa continua sendo **maker** — é exatamente o
mecanismo da seleção adversa, e tratá-la como taker apagaria o fenômeno que o
projeto existe para medir. Um teste trava isso
(`test_ordem_defasada_e_a_que_executa`): ele pegou a primeira versão desta
mudança errando justamente aí.

Corolário: saída forçada que chega **depois** do preço fugir não executa — vira
ordem parada e a posição continua na mão. É o custo da latência sobre a fuga, e
precisa aparecer (`test_preco_que_foge_no_transito_deixa_a_ordem_parada`).

#### Sair custa caro — e é o número a vigiar

Medido no smoke test: sair de 100 cotas a 0,44 pagou **$2,20 de taxa sobre $44
de notional — 5%**, contra um meio-spread de captura de ~$0,50. **Cada saída
forçada custa 2 a 4x o que a entrada rendeu.**

Isso usa a leitura PIOR da fórmula de taxa (`min(p, 1-p)`), que é o padrão
deliberado de `analysis/fees.py` — e a fórmula **continua não verificada**
(`fee_rate_bps` veio 0 em 957/957 execuções observadas). Com a leitura
`p*(1-p)` o custo cairia para ~$1,23.

Mesmo assim a troca compensa: $2,20 de custo conhecido no lugar de um risco de
$68 (68c/cota) na resolução. **Mas isso é uma hipótese a medir, não um fato** —
é precisamente o que a próxima rodada responde, comparando `Taxas pagas` com
`Realizado NA RESOLUÇÃO`.

#### Ressalva sobre `end_date`

`fim_lookup` usa `markets.end_date`, que é o fim **programado**. Jogo que atrasa
ou mercado que resolve depois do previsto desloca a janela de saída. Token sem
`end_date` devolve `None` e a estratégia opera normal, sem saída forçada — nunca
vende por causa de dado faltando (`test_sem_data_de_resolucao_nada_muda`).

### Três motivos para NÃO acreditar no P&L de paper ainda

1. **Markout deu POSITIVO** (+$13 a +$18) para `cruzamento`. Um market maker
   ingênuo deveria sofrer markout negativo. Positivo sugere que a regra está
   gerando execuções irreais — provavelmente contando cancelamentos.
2. **As execuções não formam ida-e-volta.** Diagnóstico: compras a preço médio
   0,462 e vendas a 0,544 — 8 centavos de distância, muito acima do spread de
   1–2c. São tokens DIFERENTES, não round trips. O caixa positivo é artefato de
   comprar instrumentos baratos e vender instrumentos caros, não de capturar
   spread.
3. **Amostra minúscula** — poucas centenas de execuções, minutos de janela.

O `Ledger` separa caixa de estoque exatamente para esse tipo de coisa aparecer.
Um caixa alto com estoque grande e negativo é aposta direcional disfarçada, não
market making.

**Próximo trabalho na Fase 1**: exigir que a estratégia feche posição no mesmo
token antes de contar lucro, e investigar por que o markout sai positivo.

## 8. Estado atual

- **43 testes passando** (`python -m pytest tests/ -q`).
- **Verificação de integridade passa**: comparação WebSocket × snapshot REST deu
  **0,0 centavo** de divergência mediana em 39 pares. O parser está confirmado
  contra fonte independente.
- **O coletor está PARADO.** Foi encerrado a pedido do usuário para que ele o
  rodasse num terminal próprio. O banco em `data/polymarket.duckdb` tem apenas
  alguns minutos de coleta — **insuficiente para qualquer conclusão definitiva.**
- Reassinatura em voo implementada e testada, mas **ainda não exercitada numa
  corrida longa de produção**.

### Como rodar

```powershell
cd D:\trading
python -u -m collector.run          # dashboard em http://127.0.0.1:8787
# Ctrl+C para parar (fecha limpo, dá flush, imprime resumo)

python -m reports.verify            # sempre ANTES de acreditar no gate0
python -m reports.gate0
python -m reports.projecao
```

---

## 9. O que está em aberto

**Bloqueadores técnicos:**
- Falta a série longa: **meta de 7 dias contínuos**. Tudo que existe hoje são
  minutos. Nenhuma conclusão é definitiva antes disso.
- Fórmula exata da taxa (só verificável na Fase 2, on-chain).
- Seleção adversa (só medível na Fase 1, com ordem postada).
- Giro real de capital — a incógnita que domina a projeção.

**Bloqueadores fora da engenharia:**
- **Tributação e estrutura de acesso ao Polymarket a partir do Brasil.** Assunto
  de contador/advogado. **Bloqueante para a Fase 2** (dinheiro real). Fora do
  escopo técnico.

**Próximos marcos, em ordem:**
1. 7 dias de coleta contínua → `reports.verify` → `reports.gate0`
2. Se market making seguir vivo: construir `engine/paper.py` (simulador com
   modelagem de fila e latência de 230ms) + `engine/ledger.py`
3. Gate 1: ≥60 dias de paper trading, PnL positivo líquido, Sharpe > 1
4. Só então Fase 2, com US$ 200–500, cujo propósito é **medir o que o simulador
   errou** — não lucrar

---

## 10. Como trabalhar neste projeto

Postura que o usuário validou ao longo da construção e que deve ser mantida:

- **Medir antes de afirmar.** Quando surgiu dúvida sobre formato de API,
  latência ou taxa, a resposta veio de consulta real, não de memória.
- **Recusar falso positivo.** Duas vezes um resultado "bom" foi rejeitado por
  esconder o custo dominante (o PASS de market making e a primeira versão da
  tabela de projeção, cujo pior cenário ainda era lucro). Se um número parecer
  bom demais, o modelo provavelmente está errado.
- **Separar o que foi medido do que foi suposto.** Todo parâmetro não verificado
  está marcado como tal no código, em comentário, e no relatório.
- O usuário escreve em **português** e pediu explicitamente o estilo de resposta
  "caverna" (`/caveman`) — frases curtas e diretas. Isso vale para a conversa;
  o código e a documentação seguem em português normal.
