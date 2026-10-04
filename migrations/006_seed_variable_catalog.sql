-- ==============================================================================
-- 6. CARGA INICIAL DO CATÁLOGO DE VARIÁVEIS (SEED)
-- ==============================================================================

INSERT INTO features.variable_catalog (
    variable_name, source_group, source_name, unit, 
    is_raw_available, is_required_for_feature_calculation, is_model_feature, 
    selection_status, selection_reason
) VALUES
-- TARGET
('preco_arabica', 'mercado', 'CEPEA/ESALQ', 'R$/sc 60kg', TRUE, TRUE, FALSE, 'TARGET', 'Variável-alvo principal. Base de y_7d, y_15d, y_30d, y_90d. Não entra contemporânea como feature.'),

-- KEEP: Mercado
('preco_robusta', 'mercado', 'CEPEA/ESALQ', 'R$/sc 60kg', TRUE, TRUE, TRUE, 'KEEP', 'Preço café conilon/robusta CEPEA.'),
('usd_brl', 'mercado', 'BCB PTAX', 'BRL', TRUE, FALSE, TRUE, 'KEEP', 'Taxa de câmbio comercial PTAX Venda.'),
('b3_cafe_ajuste', 'mercado', 'B3', 'USD/sc', TRUE, FALSE, TRUE, 'KEEP', 'Preço de ajuste contrato futuro café ICF B3.'),
('ice_kc', 'mercado', 'ICE US', 'cUSD/lb', TRUE, FALSE, TRUE, 'KEEP', 'Cotação internacional contrato Coffee C ICE NY.'),

-- KEEP: Clima Principal
('temp_media_cerrado', 'clima', 'NASA POWER', '°C', TRUE, FALSE, TRUE, 'KEEP', 'Temperatura média no polo do Cerrado Mineiro (Patrocínio/MG).'),
('umidade_rel_sulmg', 'clima', 'NASA POWER', '%', TRUE, FALSE, TRUE, 'KEEP', 'Umidade relativa do ar no Sul de Minas (Varginha/MG).'),
('umidade_rel_cerrado', 'clima', 'NASA POWER', '%', TRUE, FALSE, TRUE, 'KEEP', 'Umidade relativa do ar no Cerrado Mineiro.'),
('radiacao_mj_sulmg', 'clima', 'NASA POWER', 'MJ/m²', TRUE, FALSE, TRUE, 'KEEP', 'Radiação solar incidente no Sul de Minas.'),
('radiacao_mj_cerrado', 'clima', 'NASA POWER', 'MJ/m²', TRUE, FALSE, TRUE, 'KEEP', 'Radiação solar incidente no Cerrado Mineiro.'),
('precip_30d_sulmg', 'clima', 'Calculada', 'mm', FALSE, FALSE, TRUE, 'KEEP', 'Acumulado móvel de 30 dias de chuva no Sul de Minas.'),
('precip_30d_cerrado', 'clima', 'Calculada', 'mm', FALSE, FALSE, TRUE, 'KEEP', 'Acumulado móvel de 30 dias de chuva no Cerrado.'),
('precip_90d_sulmg', 'clima', 'Calculada', 'mm', FALSE, FALSE, TRUE, 'KEEP', 'Acumulado móvel de 90 dias de chuva no Sul de Minas.'),
('precip_90d_cerrado', 'clima', 'Calculada', 'mm', FALSE, FALSE, TRUE, 'KEEP', 'Acumulado móvel de 90 dias de chuva no Cerrado.'),
('tmin_min_30d_sulmg', 'clima', 'Calculada', '°C', FALSE, FALSE, TRUE, 'KEEP', 'Mínima absoluta de 30 dias no Sul de Minas.'),

-- KEEP: Clima Lote 3 e Calendário
('tmin_min_30d_cerrado', 'clima', 'Calculada', '°C', FALSE, FALSE, TRUE, 'KEEP', 'Mínima absoluta de 30 dias no Cerrado Mineiro.'),
('dias_quente_30d_sulmg', 'clima', 'Calculada', 'dias', FALSE, FALSE, TRUE, 'KEEP', 'Dias com Tmax > 32°C em 30 dias no Sul de Minas.'),
('dias_quente_30d_cerrado', 'clima', 'Calculada', 'dias', FALSE, FALSE, TRUE, 'KEEP', 'Dias com Tmax > 32°C em 30 dias no Cerrado.'),
('sin_ano', 'calendario', 'Calculada', '-', FALSE, FALSE, TRUE, 'KEEP', 'Codificação cíclica anual seno.'),
('cos_ano', 'calendario', 'Calculada', '-', FALSE, FALSE, TRUE, 'KEEP', 'Codificação cíclica anual cosseno.'),

-- INTERNAL_INPUT
('temp_min', 'clima', 'NASA POWER', '°C', TRUE, TRUE, FALSE, 'INTERNAL_INPUT', 'Insumo diário para tmin_min_30d.'),
('temp_max', 'clima', 'NASA POWER', '°C', TRUE, TRUE, FALSE, 'INTERNAL_INPUT', 'Insumo diário para dias_quente_30d.'),
('precip_mm', 'clima', 'NASA POWER', 'mm', TRUE, TRUE, FALSE, 'INTERNAL_INPUT', 'Insumo diário para precip_30d e precip_90d.'),

-- DROP: Bambuí
('precip_mm_bambui', 'clima', 'NASA POWER', 'mm', TRUE, FALSE, FALSE, 'DROP', 'Ponto hiperlocal excluído por auditoria.'),
('precip_30d_bambui', 'clima', 'Calculada', 'mm', FALSE, FALSE, FALSE, 'DROP', 'Excluída por auditoria.'),
('precip_90d_bambui', 'clima', 'Calculada', 'mm', FALSE, FALSE, FALSE, 'DROP', 'Excluída por auditoria.'),
('precip_90d_anom_bambui', 'clima', 'Calculada', 'z-score', FALSE, FALSE, FALSE, 'DROP', 'Excluída por auditoria.'),
('precip_90d_anom_cerrado', 'clima', 'Calculada', 'z-score', FALSE, FALSE, FALSE, 'DROP', 'Excluída por auditoria.'),
('dias_frio_30d_bambui', 'clima', 'Calculada', 'dias', FALSE, FALSE, FALSE, 'DROP', 'Excluída por auditoria.'),
('dias_frio_30d_cerrado', 'clima', 'Calculada', 'dias', FALSE, FALSE, FALSE, 'DROP', 'Excluída por auditoria.'),
('umidade_rel_bambui', 'clima', 'NASA POWER', '%', TRUE, FALSE, FALSE, 'DROP', 'Excluída por auditoria.'),
('radiacao_mj_bambui', 'clima', 'NASA POWER', 'MJ/m²', TRUE, FALSE, FALSE, 'DROP', 'Excluída por auditoria.'),
('vento_ms_bambui', 'clima', 'NASA POWER', 'm/s', TRUE, FALSE, FALSE, 'DROP', 'Excluída por auditoria.'),
('vento_ms_sulmg', 'clima', 'NASA POWER', 'm/s', TRUE, FALSE, FALSE, 'DROP', 'Excluída por auditoria.'),
('vento_ms_cerrado', 'clima', 'NASA POWER', 'm/s', TRUE, FALSE, FALSE, 'DROP', 'Excluída por auditoria.'),
('tmin_min_30d_bambui', 'clima', 'Calculada', '°C', FALSE, FALSE, FALSE, 'DROP', 'Excluída por auditoria.'),
('dias_quente_30d_bambui', 'clima', 'Calculada', 'dias', FALSE, FALSE, FALSE, 'DROP', 'Excluída por auditoria.'),
('dias_secos_seq_bambui', 'clima', 'Calculada', 'dias', FALSE, FALSE, FALSE, 'DROP', 'Lote 3 descarte por ressalva.'),
('dias_secos_seq_sulmg', 'clima', 'Calculada', 'dias', FALSE, FALSE, FALSE, 'DROP', 'Lote 3 descarte por ressalva.'),
('dias_secos_seq_cerrado', 'clima', 'Calculada', 'dias', FALSE, FALSE, FALSE, 'DROP', 'Lote 3 descarte por ressalva.'),
('deficit_hidrico_60d_bambui', 'clima', 'Calculada', 'mm', FALSE, FALSE, FALSE, 'DROP', 'Dependência não validada.'),
('deficit_hidrico_60d_sulmg', 'clima', 'Calculada', 'mm', FALSE, FALSE, FALSE, 'DROP', 'Dependência não validada.'),
('deficit_hidrico_60d_cerrado', 'clima', 'Calculada', 'mm', FALSE, FALSE, FALSE, 'DROP', 'Dependência não validada.'),

-- DROP: Mercado e Macro
('preco_arabica_usd', 'mercado', 'Calculada', 'USD/sc', TRUE, FALSE, FALSE, 'DROP', 'Redundante com preco_arabica e usd_brl.'),
('preco_robusta_usd', 'mercado', 'Calculada', 'USD/sc', TRUE, FALSE, FALSE, 'DROP', 'Redundante com preco_robusta e usd_brl.'),
('usd_brl_compra', 'mercado', 'BCB PTAX', 'BRL', TRUE, FALSE, FALSE, 'DROP', 'PTAX Venda já mantida.'),
('selic', 'macro', 'BCB SGS', '% a.a.', TRUE, FALSE, FALSE, 'DROP', 'Descartada inicialmente.'),
('ipca', 'macro', 'BCB SGS', '% m.m.', TRUE, FALSE, FALSE, 'DROP', 'Frequência mensal.'),
('cot_mm_long', 'mercado', 'CFTC', 'contratos', TRUE, FALSE, FALSE, 'DROP', 'Atraso de divulgação CFTC.'),
('cot_mm_short', 'mercado', 'CFTC', 'contratos', TRUE, FALSE, FALSE, 'DROP', 'Atraso de divulgação CFTC.'),
('cot_mm_net', 'mercado', 'CFTC', 'contratos', TRUE, FALSE, FALSE, 'DROP', 'Atraso de divulgação CFTC.'),
('cot_open_interest', 'mercado', 'CFTC', 'contratos', TRUE, FALSE, FALSE, 'DROP', 'Descartada inicialmente.'),
('b3_open_interest', 'mercado', 'B3', 'contratos', TRUE, FALSE, FALSE, 'DROP', 'Descartada inicialmente.'),
('dxy', 'mercado', 'Yahoo Finance', 'pontos', TRUE, FALSE, FALSE, 'DROP', 'Descartada inicialmente.'),
('brent', 'mercado', 'Yahoo Finance', 'USD/bbl', TRUE, FALSE, FALSE, 'DROP', 'Descartada inicialmente.'),
('oni', 'clima', 'NOAA CPC', '°C', TRUE, FALSE, FALSE, 'DROP', 'Frequência mensal.'),
('oni_fase', 'clima', 'NOAA CPC', 'categorico', TRUE, FALSE, FALSE, 'DROP', 'Descartada inicialmente.'),
('ret_1d', 'mercado', 'Calculada', 'ratio', FALSE, FALSE, FALSE, 'DROP', 'Transformação não autorizada no contrato.'),
('vol_20d', 'mercado', 'Calculada', 'ratio', FALSE, FALSE, FALSE, 'DROP', 'Transformação não autorizada no contrato.'),
('preco_media_20d', 'mercado', 'Calculada', 'R$/sc', FALSE, FALSE, FALSE, 'DROP', 'Transformação não autorizada no contrato.'),
('spread_arab_rob', 'mercado', 'Calculada', 'R$/sc', FALSE, FALSE, FALSE, 'DROP', 'Transformação não autorizada no contrato.'),
('base_local', 'mercado', 'Calculada', 'R$/sc', FALSE, FALSE, FALSE, 'DROP', 'Descartada inicialmente.'),
('base_local_pct', 'mercado', 'Calculada', '%', FALSE, FALSE, FALSE, 'DROP', 'Descartada inicialmente.'),
('ice_kc_brl_saca', 'mercado', 'Calculada', 'R$/sc', FALSE, FALSE, FALSE, 'DROP', 'Descartada inicialmente.'),
('mes', 'calendario', 'Calculada', 'inteiro', FALSE, FALSE, FALSE, 'DROP', 'Usado sin_ano/cos_ano.'),
('semana_ano', 'calendario', 'Calculada', 'inteiro', FALSE, FALSE, FALSE, 'DROP', 'Usado sin_ano/cos_ano.'),
('dia_util', 'calendario', 'Calculada', 'booleano', FALSE, FALSE, FALSE, 'DROP', 'Descartada inicialmente.'),
('ano_carga_alta', 'safra', 'CONAB', 'booleano', FALSE, FALSE, FALSE, 'DROP', 'Descartada inicialmente.'),
('fase_fenologica', 'safra', 'Manual', 'categorico', FALSE, FALSE, FALSE, 'DROP', 'Descartada inicialmente.'),
('risco_geada', 'clima', 'Calculada', 'booleano', FALSE, FALSE, FALSE, 'DROP', 'Descartada inicialmente.')
ON CONFLICT (variable_name) DO UPDATE SET
    source_group = EXCLUDED.source_group,
    source_name = EXCLUDED.source_name,
    unit = EXCLUDED.unit,
    is_raw_available = EXCLUDED.is_raw_available,
    is_required_for_feature_calculation = EXCLUDED.is_required_for_feature_calculation,
    is_model_feature = EXCLUDED.is_model_feature,
    selection_status = EXCLUDED.selection_status,
    selection_reason = EXCLUDED.selection_reason,
    updated_at = clock_timestamp();
