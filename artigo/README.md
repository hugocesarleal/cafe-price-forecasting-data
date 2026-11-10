# Reprodução do artigo

Scripts, resultados e figuras do artigo *Auditoria estatística de variáveis para a previsão do preço do café arábica: redundância regional, estrutura temporal e limites da correlação*.

| Arquivo | Conteúdo |
|---|---|
| `comum.py` | Período analisado, leitura da base e correção do preço do arábica |
| `analise.py` | Estrutura temporal do preço, correlação cruzada ICE × CEPEA, correlação com o log-retorno dos 91 dias seguintes, intervalos por *moving block bootstrap*, terços e índice do Niño 3.4 defasado |
| `figuras.py` | As 7 figuras do artigo |
| `tabela2.py` | Conferência da Tabela II (correlações das fichas de auditoria contra as recalculadas) |
| `noaa_fonte.py` | Comparação da coluna `oni` com os arquivos da NOAA PSL e do NOAA CPC |
| `resultados/` | Saídas de `analise.py` |
| `figuras/` | Figuras em PDF, como publicadas |
| `noaa/` | Cópias de `nina34.anom.data.txt` (PSL) e `oni.ascii.txt` (CPC), obtidas em 09/11/2026 |

## Dados

As análises usam o `base_modelo.csv` produzido por [`base/build_base_cafe.py`](../base/build_base_cafe.py) em 16/08/2026 (1.096 dias, de 17/08/2023 a 16/08/2026; as análises vão até 14/08/2026) e o histórico do CEPEA versionado em [`base/dados_manuais/`](../base/dados_manuais).

O `base_modelo.csv` não é distribuído porque contém séries da B3 e do Yahoo Finance cujos termos não permitem redistribuição. Para gerá-lo:

```bash
cd base
python build_base_cafe.py
```

O arquivo é gravado em `base/base_modelo.csv`, caminho usado nos comandos abaixo.

Uma base gerada em outra data cobre outro período e dá resultados diferentes. O arquivo usado no artigo tem SHA-256 `f7b9487551233ed29c2f25293af1997d1e3929e41c46ee22268ffa82860554f9`, o que permite conferir se uma base em mãos é a mesma.

## Execução

```bash
pip install -r artigo/requirements.txt
```

```bash
python artigo/analise.py base/base_modelo.csv artigo/resultados/res_corrigida_46.json --com-temperaturas
```

```bash
python artigo/figuras.py base/base_modelo.csv artigo/resultados/res_corrigida_46.json artigo/figuras
```

```bash
python artigo/tabela2.py base/base_modelo.csv
```

```bash
python artigo/noaa_fonte.py base/base_modelo.csv
```

Os três resultados de `resultados/` correspondem a:

| Arquivo | Comando |
|---|---|
| `res_original.json` | `analise.py <base> <saida> --modo original` |
| `res_corrigida.json` | `analise.py <base> <saida>` |
| `res_corrigida_46.json` | `analise.py <base> <saida> --com-temperaturas` (resultado usado no artigo) |

## Escolhas de método

- **Correção do preço.** O `base_modelo.csv` de 16/08/2026 não tem os 11 pregões do CEPEA de 10 a 24/07/2026. No modo `corrigida`, o preço é refeito a partir do histórico completo do CEPEA e as derivadas de mercado são recalculadas como em `derivar_mercado()`. O modo `original` mantém a base como foi gerada.
- **Amostra comum.** As correlações com o alvo usam os 958 dias em que o alvo e todas as variáveis existem.
- **Intervalos de confiança.** *Moving block bootstrap* com blocos de 91 dias e 2.000 reamostragens, com `numpy.random.default_rng(42)`. As 8 temperaturas diárias ficam no fim da lista, de modo que as 38 variáveis anteriores consomem a mesma sequência aleatória com ou sem elas.
- **Pregões da ICE.** Dias úteis sem os feriados federais dos EUA, exceto Columbus Day e Veterans Day, e sem a Sexta-Feira Santa.
- **Niño 3.4.** A coluna `oni` coincide com a anomalia mensal do Niño 3.4 da PSL em todos os meses já publicados no build (agosto de 2026 ainda não estava disponível e a base repetiu julho) e com o ONI do CPC em nenhum. O valor do mês *m* é tratado como disponível a partir do primeiro dia de *m*+1.
