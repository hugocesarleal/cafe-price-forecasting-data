# Contrato de Integração de Dados — Componente de Rede Neural

## 1. Visão Geral do Contrato

Este documento formaliza o contrato de dados entre o **Componente de Dados (Engenharia de Dados)** e o **Componente de Previsão (Rede Neural / Machine Learning)**.

O componente de dados é o único responsável pela ingestão, limpeza, engenharia de features, preenchimento causal e versionamento imutável. O componente de rede neural é o consumidor dos dados preparados e o produtor das inferências nos 4 horizontes especificados.

---

## 2. Horizontes Preditivos e Variável-Alvo

- **Ativo-Alvo:** Café Arábica físico CEPEA/ESALQ (R$/saca 60kg).
- **Horizontes ($h$ em dias corridos):**
  - $h = 7$ dias (`y_7d`)
  - $h = 15$ dias (`y_15d`)
  - $h = 30$ dias (`y_30d`)
  - $h = 90$ dias (`y_90d`) (equivalente inicial parametrizado para 3 meses)

---

## 3. Acesso aos Dados Preparados (Consumo das Features)

### 3.1 Consulta Padrão da Última Versão Válida
O componente consumidor obtém o snapshot de treino/inferência por `src.model_contract.load_features(conn)`, que executa a consulta abaixo:

```sql
SELECT 
    mf.data_ref,
    mf.temp_media_cerrado,
    mf.umidade_rel_sulmg,
    mf.umidade_rel_cerrado,
    mf.radiacao_mj_sulmg,
    mf.radiacao_mj_cerrado,
    mf.precip_30d_sulmg,
    mf.precip_30d_cerrado,
    mf.precip_90d_sulmg,
    mf.precip_90d_cerrado,
    mf.tmin_min_30d_sulmg,
    mf.tmin_min_30d_cerrado,
    mf.dias_quente_30d_sulmg,
    mf.dias_quente_30d_cerrado,
    mf.sin_ano,
    mf.cos_ano,
    mf.preco_robusta,
    mf.usd_brl,
    mf.b3_cafe_ajuste,
    mf.ice_kc,
    -- Alvos históricos para treino (NULL na fronteira recente):
    mf.y_7d,
    mf.y_15d,
    mf.y_30d,
    mf.y_90d
FROM features.model_features mf
JOIN features.dataset_versions dv ON mf.dataset_version_id = dv.dataset_version_id
WHERE dv.is_valid = TRUE
  AND dv.dataset_version_id = (
      SELECT dataset_version_id 
      FROM features.dataset_versions 
      WHERE is_valid = TRUE 
      ORDER BY created_at DESC 
      LIMIT 1
  )
  AND mf.is_pruned = FALSE
ORDER BY mf.data_ref ASC;
```

### 3.2 Metadados da Versão Fornecidos ao Modelo
`src.model_contract.get_latest_dataset(conn)` devolve, da última versão válida:
- `dataset_version_id` (UUID): Identificador único da versão imutável.
- `version_tag` (VARCHAR): Ex. `'v1.0.0-20261003'`.
- `cutoff_date` (DATE): Data limite dos dados disponíveis na versão ($t$).
- `start_date` (DATE): Data inicial da janela com que a versão foi criada.
- `row_count` (INTEGER): Quantidade de linhas na grade diária contínua.
- `active_start_date` / `active_row_count`: início e tamanho da janela que a consulta padrão entrega de fato, descontada a poda lógica (`is_pruned`).
- `sha256_checksum` (CHAR(64)): Hash dos dados para garantir reprodutibilidade matemática.
- `columns`: nome e tipo de cada coluna entregue; `feature_columns` e `target_columns` separam entradas e alvos.
- `horizons`: horizontes de previsão configurados.
- `quality`: `{"status": "OK" | "WARNING", "alertas": [...]}` — checagens de alerta reprovadas na construção da versão. Falha crítica nunca aparece aqui: ela impede a versão de existir.

---

## 4. Regras Anti-Leakage (Sem Vazamento de Futuro)

1. Para qualquer data de corte $t$, $X_t$ contém **estritamente** dados conhecidos até o fechamento de $t$.
2. O valor contemporâneo `preco_arabica(t)` **nunca** é fornecido como entrada em $X_t$ para prever $t$.
3. Se o modelo desejar utilizar defasagens do alvo, deverá usar lags estritos: $preco\_arabica_{t-1}, preco\_arabica_{t-2}$, etc.
4. As variáveis `sin_ano` e `cos_ano` devem ser utilizadas conjuntamente para preservar a topologia cíclica da sazonalidade anual.
5. As variáveis de clima entram com o atraso de publicação da fonte (NASA POWER): em $X_t$, o clima é o de $t-3$ (radiação: $t-5$). Assim `precip_30d_*` de $t$ é a chuva acumulada nos 30 dias terminados em $t-3$. É o mesmo clima que estará disponível quando a previsão de $t$ for feita.

---

## 5. Contrato de Retorno das Previsões

O componente de machine learning entrega as inferências a `src.model_contract.register_forecasts(conn, previsoes)`, que valida o lote e grava em `predictions.forecasts`:

```sql
INSERT INTO predictions.forecasts (
    forecast_id,
    reference_date,
    target_date,
    horizon_days,
    predicted_value,
    dataset_version_id,
    model_version,
    generated_at,
    pipeline_run_id,
    forecast_status
) VALUES (
    gen_random_uuid(),
    '2026-10-03',              -- Data de corte t
    '2026-10-10',              -- Data futura prevista (t + 7)
    7,                         -- Horizonte em dias (7, 15, 30 ou 90)
    1745.50,                   -- Previsão em R$/sc 60kg
    '550e8400-e29b-41d4-a716-446655440000', -- UUID do dataset consumido
    'neural_net_lstm_v1.2',    -- Versão do modelo
    clock_timestamp(),
    'c25a7b60-449e-4c1d-8cf7-111122223333', -- Run id do pipeline
    'ACTIVE'
)
ON CONFLICT (reference_date, horizon_days, model_version)
DO UPDATE SET
    target_date = EXCLUDED.target_date,
    predicted_value = EXCLUDED.predicted_value,
    generated_at = clock_timestamp(),
    dataset_version_id = EXCLUDED.dataset_version_id,
    pipeline_run_id = EXCLUDED.pipeline_run_id,
    forecast_status = EXCLUDED.forecast_status;
```

O lote inteiro é rejeitado, sem gravar nada, se alguma previsão:

- não traz `reference_date`, `target_date`, `horizon_days`, `predicted_value`, `dataset_version_id`, `model_version` ou `pipeline_run_id`;
- usa um horizonte fora de `FORECAST_HORIZONS`, ou `target_date` diferente de `reference_date + horizon_days`;
- tem `predicted_value` não numérico ou não positivo;
- aponta para uma versão de dataset inexistente ou inválida, ou diferente da do restante do lote;
- tem `reference_date` posterior ao corte da versão;
- repete `(reference_date, horizon_days, model_version)` dentro do lote.

Cada lote também gera uma linha em `audit.model_runs` por `(pipeline_run_id, model_version)`.

---

## 6. Exemplo de Função Consumidora Simulada (Mock Consumer)

Conforme estabelecido no prompt, **não** implementamos o modelo de rede neural real. A função consumidora simulada, para validação e testes contratuais, está em `src/model_contract.py`; `python -m src.model_contract` executa o ciclo completo (ler a última versão, prever, registrar). Em essência:

```python
"""Exemplo de consumidor simulado de features e gerador de previsões fictícias."""
import uuid
from datetime import timedelta
import pandas as pd

def mock_neural_model_predict(
    dataset_df: pd.DataFrame,
    dataset_version_id: str,
    pipeline_run_id: str,
    model_version: str = "mock_linear_baseline_v0.1"
) -> list[dict]:
    """Simula o consumo de features e produz 4 previsões contratuais (7, 15, 30, 90 dias)."""
    if dataset_df.empty:
        raise ValueError("Dataset de features está vazio.")
    
    # Obtém a linha da data de corte mais recente (cutoff t)
    latest_row = dataset_df.iloc[-1]
    cutoff_date = pd.to_datetime(latest_row["data_ref"]).date()
    base_price = float(latest_row.get("preco_robusta", 1500.0))  # Proxy para simulação
    
    horizons = [7, 15, 30, 90]
    forecast_records = []
    
    for h in horizons:
        target_date = cutoff_date + timedelta(days=h)
        # Predição simulada ingênua (mock): base + variação sazonal mínima
        predicted_price = round(base_price * (1.0 + (h * 0.001)), 2)
        
        forecast_records.append({
            "forecast_id": str(uuid.uuid4()),
            "reference_date": cutoff_date,
            "target_date": target_date,
            "horizon_days": h,
            "predicted_value": predicted_price,
            "dataset_version_id": dataset_version_id,
            "model_version": model_version,
            "pipeline_run_id": pipeline_run_id,
            "forecast_status": "ACTIVE"
        })
        
    return forecast_records
```
