# Base de dados para previsão de preço do café

Pipeline de ETL que monta um banco SQLite com granularidade diária, reunindo as variáveis que influenciam o preço do café arábica no Brasil.

**Recorte geográfico:** região de Bambuí/MG (Centro-Oeste Mineiro)
**Variável-alvo:** Indicador CEPEA/ESALQ Café Arábica (R$/saca 60 kg)
**Horizonte pretendido:** previsão semanal até 3 meses à frente
**Janela histórica:** 3 anos (móvel, calculada a partir da data de execução)

---

## 1. O que foi feito

O script `build_base_cafe.py` coleta dados de **9 fontes públicas**, normaliza tudo para um calendário diário contínuo e grava uma tabela em formato largo (uma linha por dia, uma coluna por variável), pronta para alimentar um modelo de séries temporais.

O trabalho resolve três problemas que costumam consumir a maior parte do tempo neste tipo de projeto:

1. **Fontes heterogêneas.** Preço vem do CEPEA por scraping, câmbio vem da API do Banco Central, clima vem da NASA, posição de fundos vem da CFTC. Cada uma com seu formato, encoding e periodicidade.
2. **Frequências diferentes.** Preço é diário (só em pregão), COT é semanal, ONI é mensal, safra é quadrimestral. O modelo precisa de uma grade única.
3. **Clima cru não serve.** Temperatura e chuva do dia explicam pouco; o que move preço são acumulados, anomalias e contagens de eventos extremos. O script já entrega essas transformações calculadas.

---

## 2. Arquitetura

```
build_base_cafe.py
│
├── CONFIGURAÇÃO           janela, modo de preenchimento, coordenadas, tickers
│
├── COLETORES              1 função por fonte, isoladas por decorator @coletor
│   ├── agrobr             CEPEA · BCB · CFTC · NASA POWER · B3
│   └── externas           yfinance · NOAA CPC
│
├── PREENCHIMENTO          reindexa no calendário diário e propaga valores
│
├── DERIVAÇÃO              clima · mercado · calendário
│
└── PERSISTÊNCIA           SQLite (tabela larga + log de proveniência)
```

### 2.1 Coletores isolados

Cada fonte é envolvida pelo decorator `@coletor`, que captura exceções. Se o CEPEA sair do ar, as outras oito fontes continuam sendo coletadas e a falha aparece no relatório final e na tabela `coleta_log` — em vez de o script morrer sem explicação.

```bash
python build_base_cafe.py --debug   # mostra o traceback completo das falhas
```

### 2.2 Preenchimento para granularidade diária

A função `preencher()` reindexa cada série no calendário diário cheio (inclusive fins de semana e feriados) e preenche os vazios repetindo o último valor conhecido.

O comportamento é controlado pela constante `MODO_PREENCHIMENTO`, no topo do arquivo:

| Modo | Comportamento | Uso |
|---|---|---|
| `"periodo"` *(padrão)* | Propaga para frente **e para trás** dentro do período de referência. Um dado mensal de março preenche 01/03 a 31/03, mesmo tendo sido divulgado no dia 15. | Base de trabalho, exploração |
| `"causal"` | Propaga apenas para frente. O valor só entra na base a partir do dia em que ficou público. | Backtest definitivo |

**Por que os dois modos existem.** O modo `periodo` é mais intuitivo e é o que preenche "todos os dias do mês com aquele valor". Mas ele coloca na base do dia 1º de março uma informação que só existiu no dia 15 — o modelo enxerga o futuro. Isso infla o desempenho no backtest e não se sustenta em produção.

Trocar uma palavra e rodar de novo permite comparar os dois. **Se o R² cair muito ao passar para `"causal"`, o resultado anterior era artefato de vazamento**, não capacidade preditiva.

Séries de mercado (preço, câmbio, futuros) não sofrem desse problema: elas só têm buraco em fim de semana e feriado, e repetir a sexta no sábado é correto nos dois modos.

### 2.3 Variáveis derivadas

Calculadas depois do preenchimento, em três grupos:

- **Clima** — acumulados de chuva (30d/90d), anomalia padronizada, contagem de dias frios e quentes, déficit hídrico aproximado, sequência de dias secos.
- **Mercado** — retorno logarítmico, volatilidade realizada, conversão do ICE KC para R$/saca e a base local resultante, spread arábica-robusta.
- **Calendário** — codificação cíclica do ano em seno/cosseno, fase fenológica, dummy de bienalidade, flag de risco de geada.

---

## 3. Fontes de dados

| Variável | Fonte | Acesso | Frequência original |
|---|---|---|---|
| Preço arábica (Mogiana, Sul de Minas) | CEPEA/ESALQ | agrobr · scraping | Diária (pregão) |
| Preço robusta | CEPEA/ESALQ | agrobr · scraping | Diária (pregão) |
| Câmbio USD/BRL | BCB PTAX | agrobr · API oficial | Diária útil |
| Selic | BCB SGS | agrobr · API oficial | Diária |
| Posição de fundos (COT) | CFTC | agrobr · API oficial | Semanal |
| Futuro café B3 (ICF) | B3 | agrobr · arquivo | Diária |
| Clima (3 regiões) | NASA POWER | agrobr · API oficial | Diária |
| Brent, ICE KC, DXY | Yahoo Finance | yfinance | Diária |
| ONI (El Niño) | NOAA CPC | download direto | Mensal |

### Pontos climáticos

O NASA POWER opera em grid de 0,5° (~55 km), então não faz sentido usar municípios vizinhos — cairiam na mesma célula.

| Chave | Coordenada | Região |
|---|---|---|
| `bambui` | -20,01 / -45,98 | Bambuí/MG — região do projeto |
| `sulmg` | -21,55 / -45,43 | Varginha/MG — Sul de Minas |
| `cerrado` | -18,94 / -46,99 | Patrocínio/MG — Cerrado Mineiro |

Sul de Minas e Cerrado entram porque pesam muito mais na formação do Indicador CEPEA nacional do que o Centro-Oeste Mineiro.

---

### 3.1 Por que o preço do café não vem do agrobr

Esta é a decisão de arquitetura mais importante do projeto, e a que mais custou tempo para descobrir. **O preço do café — a variável-alvo — é a única série que não é coletada automaticamente.** Ela vem de um arquivo baixado à mão do CEPEA. O motivo não é preferência: o agrobr é incapaz de entregar essa série.

#### O sintoma

A primeira versão do script usava `cepea.indicador("cafe", inicio=..., fim=...)` normalmente. O resultado:

```
preco_arabica    1029 valores faltantes (93.9%)
Preco arabica: R$ 1676.28 a R$ 1782.18
```

Apenas 66 cotações em 1.096 dias. E a faixa de preço denunciava o problema: R$ 1.676 a R$ 1.782 é uma variação de 6%. O arábica de fato oscilou entre **R$ 779,90 e R$ 2.769,45** no mesmo período — mais de 3,5x. O que voltou foram os últimos dois meses, não três anos.

#### A causa, no código do agrobr

Três achados na leitura do pacote, que se somam:

**1. A página raspada é estática e curta.** O `cepea.indicador()` chama `_fetch_and_parse()`, que chama `client.fetch_indicador_page(produto)`. A URL montada é:

```
https://cepea.esalq.usp.br/br/indicador/cafe.aspx
```

É a página pública do indicador, que exibe apenas as cotações mais recentes — cerca de 60. Não existe paginação nem parâmetro de período.

**2. A função de fetch não recebe datas.** A assinatura é:

```python
async def fetch_indicador_page(
    produto: str,
    force_browser: bool = False,
    force_alternative: bool = False,
) -> FetchResult
```

Não há `inicio` nem `fim`. Não é limitação de configuração — **não existe mecanismo para pedir um intervalo histórico à fonte**. Os parâmetros `inicio`/`fim` de `indicador()` são repassados apenas para `store.indicadores_query()`, que consulta o cache DuckDB local.

**3. A janela de busca é de 10 dias.** A constante `SOURCE_WINDOW_DAYS = 10` governa a função `_needs_fetch()`, que decide se vale ir à rede:

```python
recent_start = today - timedelta(days=SOURCE_WINDOW_DAYS)
if fim < recent_start:
    return False
```

Ou seja: o agrobr só acessa a rede para completar os **últimos 10 dias**. Qualquer data anterior tem que já estar no cache.

#### Por que isso é por design, não bug

A própria documentação do agrobr declara a filosofia:

> É uma biblioteca de *fetch + normalize*, não um framework de armazenamento. Não há acumulação automática de histórico. Histórico permanente é responsabilidade do consumidor.

O cache DuckDB **acumula ao longo do tempo**: quem roda o script todo dia por três anos termina com a série completa. Mas ele não reconstrói o passado. Numa instalação nova o cache está vazio, e o que se obtém é o que aquela única página mostra.

Vale notar que isso afeta só o CEPEA. Fontes que servem séries históricas de verdade — BCB SGS, CFTC, NASA POWER, CONAB série histórica — retornam o intervalo completo numa requisição, e no script funcionam sem intervenção.

#### A solução adotada

O histórico vem do **Consultas ao Banco de Dados** do CEPEA (seção 5, passo 1), que exporta a série completa desde 1996. O agrobr continua sendo usado para completar os dias mais recentes, que o arquivo baixado ainda não cobre.

Três ganhos além de resolver o problema:

- **Procedência melhor.** É a fonte primária oficial, não scraping de página de exibição. Mais defensável na metodologia.
- **Histórico ilimitado.** Estender de 3 para 15 anos passa a ser só mudar o período no formulário.
- **Independência do elo mais frágil.** O CEPEA era a única fonte por scraping do pipeline, sujeita a Cloudflare e mudança de layout. A série-alvo deixa de depender disso.

O custo é um passo manual. Como é feito uma vez por coleta e a alternativa é não ter a variável-alvo, o troco é claramente favorável.

---

## 4. O que é gerado

Duas saídas.

### 4.1 `cafe_centro_oeste_mg.db` — o banco

SQLite com **12 tabelas normalizadas**, exatamente as do diagrama ER (seção 3.2), criadas a partir do `schema.sql`.

| Tabela | Conteúdo | Ordem de grandeza (3 anos) |
|---|---|---|
| `tb_fonte` | 9 fontes, com URL e licença | 9 |
| `tb_regiao` | Pontos de clima e praças de cotação | 7 |
| `tb_produto` | Arábica e robusta | 2 |
| `tb_contrato` | KC, RC, ICF | 3 |
| `tb_preco_cafe` | Preço físico CEPEA, BRL e USD | ~1.500 |
| `tb_clima` | Clima diário, 7 variáveis × 3 regiões | ~3.300 |
| `tb_cambio` | PTAX compra/venda e DXY | ~780 |
| `tb_macro` | Selic, IPCA, Brent | ~780 |
| `tb_futuros` | Ajuste e open interest por contrato | ~1.560 |
| `tb_cot` | Posição dos fundos, com `data_pub` | ~156 |
| `tb_enso` | ONI mensal e fase | ~36 |
| `tb_coleta_log` | Proveniência de cada execução | 1 por fonte/execução |

A gravação é **idempotente**: cada tabela tem constraint `UNIQUE` sobre suas chaves naturais, e o upsert usa `ON CONFLICT ... DO UPDATE SET col = COALESCE(excluded.col, tabela.col)`. Rodar o script duas vezes atualiza em vez de duplicar — verificado em teste. O `COALESCE` também garante que uma fonte que preenche parte das colunas não apague o que outra já gravou: PTAX e DXY escrevem ambos em `tb_cambio` sem conflito.

### 4.2 `base_modelo.csv` — a matriz do modelo

Gerada a partir da view `vw_cafe_diario`, com calendário diário preenchido e variáveis derivadas calculadas. Aproximadamente **1.096 linhas × 72 colunas**.

O fluxo é: tabelas normalizadas → `vw_cafe_diario` (pivot em SQL) → `preencher()` (ffill/bfill em Python) → `derivar_*()` → CSV.

O preenchimento fica no Python de propósito: o SQLite não tem função de janela adequada para forward fill, e fazer isso em SQL puro ficaria ilegível.

#### Variáveis-fonte

| Coluna | Descrição | Unidade |
|---|---|---|
| `preco_arabica` | **Variável-alvo** | R$/sc 60 kg |
| `preco_arabica_usd`, `preco_robusta_usd` | Mesmas séries em dólar | US$/sc |
| `preco_robusta` | Conilon | R$/sc 60 kg |
| `usd_brl`, `usd_brl_compra` | PTAX venda e compra | R$ |
| `selic`, `ipca` | BCB SGS | % |
| `cot_mm_net`, `cot_mm_long`, `cot_mm_short` | Posição dos fundos | contratos |
| `cot_open_interest` | Contratos em aberto | contratos |
| `b3_cafe_ajuste`, `b3_open_interest` | Futuro ICF | USD/sc, contratos |
| `brent` | Petróleo | USD/bbl |
| `ice_kc` | ICE Arábica Coffee C | ¢USD/lb |
| `dxy` | Índice dólar | pontos |
| `oni`, `oni_fase` | ENSO | °C, categórico |
| `temp_min_*`, `temp_max_*`, `temp_media_*` | Por região | °C |
| `precip_mm_*`, `umidade_rel_*` | Por região | mm, % |
| `radiacao_mj_*`, `vento_ms_*` | Por região | MJ/m², m/s |

#### Variáveis derivadas — clima

Sufixo `_bambui`, `_sulmg` ou `_cerrado`.

| Coluna | Descrição |
|---|---|
| `precip_30d_*`, `precip_90d_*` | Chuva acumulada na janela móvel |
| `precip_90d_anom_*` | Z-score do acumulado contra a média do dia-do-ano |
| `dias_frio_30d_*` | Dias com Tmín < 4 °C nos últimos 30 — risco de geada |
| `tmin_min_30d_*` | Mínima absoluta da janela |
| `dias_quente_30d_*` | Dias com Tmáx > 32 °C — estresse térmico |
| `deficit_hidrico_60d_*` | Chuva menos evapotranspiração aproximada |
| `dias_secos_seq_*` | Dias secos consecutivos — veranico |

#### Variáveis derivadas — mercado e calendário

| Coluna | Descrição |
|---|---|
| `ret_1d`, `vol_20d`, `preco_media_20d` | Retorno log, volatilidade anualizada, média móvel |
| `ice_kc_brl_saca` | ICE KC convertido para R$/saca (132,277 lb/sc) |
| `base_local`, `base_local_pct` | Prêmio/desconto do físico brasileiro contra o ICE |
| `spread_arab_rob` | Diferencial arábica-robusta |
| `b3_spread_2_1` | Estrutura a termo na B3 (backwardation/contango) |
| `cot_mm_net_pct_oi` | Posição líquida dos fundos sobre o open interest |
| `sin_ano`, `cos_ano`, `mes`, `semana_ano` | Codificação cíclica do tempo |
| `ano_carga_alta` | Dummy de bienalidade |
| `fase_fenologica` | `floracao` · `granacao` · `colheita` · `pos_colheita` |
| `risco_geada`, `dia_util` | Dummies |

### 4.3 Cobertura do schema

Toda coluna do DDL é preenchida por algum coletor — **67 de 67**. Isso foi verificado por auditoria cruzando as colunas declaradas em `schema.sql` contra o que cada coletor de fato entrega, e não por inspeção visual.

Vale repetir essa auditoria sempre que uma coluna nova for adicionada ao schema. Uma tabela com colunas permanentemente nulas é pior que não ter a coluna: ela sugere ao leitor do diagrama que o dado existe.

Campos que exigem atenção especial, porque não vêm prontos da fonte:

| Campo | Como é obtido |
|---|---|
| `tb_cot.data_pub` | `data_ref + 3 dias`. A CFTC apura na terça e divulga na sexta. |
| `tb_enso.data_pub` | Primeiro dia do mês seguinte ao de referência. |
| `tb_enso.fase` | Limiares do CPC sobre o ONI: ≥ 0,5 El Niño, ≤ −0,5 La Niña. |
| `tb_futuros.ordem_vencimento` | Ordenação dos vencimentos por ano/mês dentro de cada data. |
| `tb_futuros.open_interest` | Endpoint separado da B3 (`posicoes_abertas`), não vem no histórico de ajustes. |

---

## 5. Como executar

### Passo 1 — Baixar o histórico do CEPEA (obrigatório)

A página de indicador que o agrobr raspa exibe apenas as **últimas ~60 cotações**. Os parâmetros `inicio`/`fim` filtram o que já foi baixado; não pedem mais histórico ao servidor. Para 3 anos é preciso usar o export oficial:

1. Acesse **Consultas ao Banco de Dados**
   `https://www.cepea.esalq.usp.br/br/consultas-ao-banco-de-dados-do-site.aspx`
2. Selecione **Café Arábica**, defina o período de 3 anos e baixe.
3. Repita para **Café Conilon/Robusta**.
4. Salve os arquivos na pasta `dados_manuais/` do projeto:

```
base_cafe/
├── build_base_cafe.py
└── dados_manuais/
    ├── cafe_arabica_cepea_1996_2026.csv
    └── CEPEA_20260816101737.xls
```

**Não é preciso renomear.** O script identifica cada série procurando as palavras `arabica` e `robusta`/`conillon` no nome do arquivo e, se não achar, no título interno da planilha — o export do CEPEA vem com nome genérico do tipo `CEPEA_20260816101737.xls`.

Havendo mais de um arquivo da mesma série na pasta, **o CSV tem preferência** sobre `.xlsx` e `.xls`. Para forçar um arquivo específico, preencha `ARQUIVOS_FIXOS` no topo do script:

```python
ARQUIVOS_FIXOS = {
    "arabica": "cafe_arabica_cepea_1996_2026.csv",
    "robusta": "cafe_robusta_cepea_2001_2026.csv",
}
```

Deixe as strings vazias para manter a detecção automática. O diagnóstico prévio sempre imprime qual arquivo foi escolhido, então dá para conferir antes da coleta.

O leitor aceita `.xlsx`, `.xls` ou `.csv`, localiza a linha de cabeçalho sozinho, detecta se as datas estão em ISO ou `dd/mm/aaaa`, converte números em formato brasileiro (`2.474,42`) e descarta valores zero, que o CEPEA usa como marcador de ausência no início de algumas séries.

Para `.xls`, o `xlrd` costuma falhar com `Workbook corruption` embora o arquivo seja válido; o script cai automaticamente para o `calamine`, que vem instalado com o agrobr.

Além de resolver o problema técnico, essa é a **fonte primária**: procedência melhor que scraping, e mais defensável na metodologia do trabalho.

### Passo 2 — Rodar

```bash
pip install agrobr==1.1.0 yfinance
python build_base_cafe.py
```

Só isso. O `agrobr` já traz `pandas`, `numpy`, `requests`, `openpyxl`, `xlrd`, `duckdb`, `httpx`, `beautifulsoup4` e `lxml` como dependências. O `yfinance` é o único extra, usado para Brent, ICE KC e DXY.

### Diagnóstico prévio

Antes de gastar 5-10 minutos coletando, o script testa todas as fontes e mostra de uma vez tudo que vai falhar:

```
======================================================================
  DIAGNOSTICO PREVIO DAS FONTES
======================================================================
  [OK ] CEPEA cafe arabica           dados_manuais/cafe_arabica_cepea_1996_2026.csv
  [OK ] BCB PTAX (USD/BRL)           olinda.bcb.gov.br  HTTP 200
  [!! ] NOAA ONI (El Nino)           psl.noaa.gov: DNS nao resolve
  [!! ] yfinance (Brent, KC, DXY)    falta o modulo 'yfinance' (pip install yfinance)
----------------------------------------------------------------------
  2 fonte(s) com problema.

  Variaveis que NAO serao coletadas:
    - brent
    - dxy
    - ice_kc
    - oni

  Variaveis derivadas que deixam de existir por consequencia:
    - base_local
    - base_local_pct
    - ice_kc_brl_saca
======================================================================
```

São verificadas três coisas por fonte: módulo Python instalado, arquivo local presente e host acessível na rede. Um HTTP 403 ou 404 conta como acessível — significa que o DNS resolveu e o servidor respondeu, então a fonte está no ar.

O diagnóstico também **propaga o efeito para as variáveis derivadas**: se o `ice_kc` falhar, ele avisa que `base_local` e `ice_kc_brl_saca` deixam de existir, porque dependem dele.

Se houver qualquer problema, o script pede confirmação antes de prosseguir. Se a **variável-alvo** estiver indisponível, exibe alerta crítico — nesse caso a base não serve para treino.

| Flag | Efeito |
|---|---|
| `--check` | Só o diagnóstico, não coleta nada |
| `--force` | Pula a pergunta de confirmação (útil em script agendado) |
| `--debug` | Mostra o traceback completo das falhas |

Ao final da coleta, o script reporta a **cobertura da variável-alvo** e emite alerta em destaque se ficar abaixo de 90%.

A versão do agrobr está fixada de propósito: é uma biblioteca que raspa portais públicos, e mudanças de layout na origem podem quebrar parsers. Antes de uma coleta importante:

```bash
agrobr health --source cepea --deep
```

Tempo estimado: 3 a 10 minutos, dominado pelas três chamadas ao NASA POWER.

---

## 6. Limitações conhecidas

**O CEPEA exige download manual para backfill.** A página de indicador serve apenas ~60 cotações recentes e a função de fetch do agrobr não aceita parâmetro de data — a janela de busca é de 10 dias. O cache DuckDB acumula histórico ao longo do tempo, mas não reconstrói o passado. Explicação completa na **seção 3.1**. Se o arquivo não estiver em `dados_manuais/`, o diagnóstico prévio avisa antes de coletar.

**O NOAA pode falhar por DNS.** O host do CPC nem sempre resolve e alguns firewalls corporativos bloqueiam o domínio. O coletor tenta três URLs diferentes (PSL e CPC), com parsers para os dois layouts de arquivo. Se todas falharem, a coluna `oni` simplesmente não é criada e o resto da base é gerado normalmente.

**A janela de 3 anos é curta.** São ~156 semanas. Com horizonte de 13 semanas e dezenas de variáveis, o risco de overfitting é alto. Recomenda-se selecionar de 15 a 25 variáveis para o modelo, não usar todas as 70. O CEPEA tem série desde 1996 e o NASA POWER desde 1981 — como o download já é manual, estender a janela custa apenas mudar o período no formulário do CEPEA. É a mudança de maior impacto no projeto.

**A anomalia climática é fraca com 3 anos.** O z-score usa a média do dia-do-ano como referência, o que com 3 anos significa n=3 por dia. A variável funciona, mas ganha muito com histórico maior.

**A base local é nacional, não de Bambuí.** O CEPEA só publica arábica para Mogiana e Sul de Minas; não existe indicador para o Centro-Oeste Mineiro. A coluna `base_local` mede o desvio do físico brasileiro contra o ICE. Para obter a base real da região seria preciso a série de preço de balcão de uma cooperativa ou corretora local — dado que não vem de API.

**A regra de bienalidade é simplificada.** `ano % 2 == 0` é uma aproximação. Conferir contra `conab.serie_historica('cafe', ...)` antes de usar em produção.

**Estoques certificados ICE não estão incluídos.** É uma variável relevante e diária, mas exige scraping próprio do ICE Report Center. O `usda.psd()` do agrobr oferece estoque final por país, porém em frequência anual.

**Preço de commodity é próximo de um passeio aleatório.** Qualquer modelo treinado sobre esta base deve ser comparado ao baseline ingênuo (previsão = preço de hoje). Modelar retorno logarítmico acumulado em *h* semanas tende a funcionar melhor que modelar o nível do preço.

---

## 7. Licenças dos dados

O agrobr é MIT, mas os dados pertencem às fontes originais.

Os dados do **CEPEA/ESALQ são CC BY-NC 4.0** — uso não comercial, com **citação obrigatória**. Para pesquisa acadêmica não há impedimento, desde que a fonte seja creditada no trabalho. B3 e Notícias Agrícolas têm licenças de zona cinzenta; o agrobr emite um aviso na primeira chamada.

Referência completa: `docs/licenses.md` no repositório do agrobr.

---

## 8. Reprodutibilidade

Para congelar a base no dia do treino do modelo — útil se um revisor pedir para reproduzir os resultados:

```python
from agrobr.snapshots import create_snapshot
create_snapshot("base-pesquisa-v1", sources=["cepea", "conab"])
```

```python
from agrobr import datasets
async with datasets.deterministic("base-pesquisa-v1"):
    df = datasets.preco_diario("cafe")   # lê só do snapshot, sem rede
```
