-- ===========================================================================
-- BANCO DE DADOS PARA PREVISAO DE PRECO DO CAFE
-- Recorte: regiao de Bambui/MG (Centro-Oeste Mineiro)
-- Variavel-alvo: Indicador CEPEA/ESALQ Cafe Arabica (R$/saca 60 kg)
--
-- Modelo: estrutura normalizada por dominio, com dois hubs dimensionais
--   tb_fonte   -> procedencia (hub principal: 9 fontes distintas)
--   tb_regiao  -> geografia   (hub secundario: clima e praca de cotacao)
--
-- A tabela larga usada para treinar o modelo e a view vw_cafe_diario,
-- construida sobre as tabelas de fato.
--
-- SGBD: SQLite 3.35+ (usa STRICT e generated columns quando disponivel)
-- ===========================================================================

PRAGMA foreign_keys = ON;

-- ===========================================================================
-- 1. TABELAS DIMENSAO
-- ===========================================================================

-- Fonte de dados. Guarda licenca porque o CEPEA e CC BY-NC 4.0 e exige
-- citacao; B3 e Noticias Agricolas estao em zona cinzenta.
CREATE TABLE tb_fonte (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    nome         VARCHAR(60)  NOT NULL UNIQUE,
    url          VARCHAR(200),
    licenca      VARCHAR(40),
    tipo_acesso  VARCHAR(20)  NOT NULL
                 CHECK (tipo_acesso IN ('api', 'scraping', 'arquivo', 'manual'))
);

-- Regiao geografica. Serve tanto para pontos de clima (lat/lon do grid
-- NASA POWER) quanto para pracas de cotacao do CEPEA.
CREATE TABLE tb_regiao (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    nome       VARCHAR(60) NOT NULL UNIQUE,
    sigla      VARCHAR(12) NOT NULL UNIQUE,
    latitude   REAL,
    longitude  REAL,
    tipo       VARCHAR(20) NOT NULL
               CHECK (tipo IN ('produtora', 'praca', 'referencia'))
);

-- Produto cotado.
CREATE TABLE tb_produto (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    nome     VARCHAR(40) NOT NULL UNIQUE,
    especie  VARCHAR(20) NOT NULL CHECK (especie IN ('arabica', 'robusta')),
    unidade  VARCHAR(20) NOT NULL DEFAULT 'R$/sc 60kg'
);

-- Contrato futuro. fator_saca converte a cotacao para R$/saca 60 kg:
-- ICE KC vem em centavos de USD por libra -> 132.277 lb por saca.
CREATE TABLE tb_contrato (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo       VARCHAR(12) NOT NULL UNIQUE,
    bolsa        VARCHAR(20) NOT NULL,
    moeda        VARCHAR(6)  NOT NULL,
    fator_saca   REAL,
    descricao    VARCHAR(80)
);

-- ===========================================================================
-- 2. TABELAS FATO
--
-- Todas carregam data_ref (data a que o dado se refere) e fk_fonte_id.
-- Series nao diarias carregam tambem data_pub (data em que o dado ficou
-- publico), o que permite montar a base sem vazamento de informacao.
-- ===========================================================================

-- Preco fisico do cafe (CEPEA/ESALQ)
CREATE TABLE tb_preco_cafe (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    data_ref       DATE    NOT NULL,
    fk_produto_id  INTEGER NOT NULL REFERENCES tb_produto(id),
    fk_regiao_id   INTEGER          REFERENCES tb_regiao(id),
    fk_fonte_id    INTEGER NOT NULL REFERENCES tb_fonte(id),
    preco_brl      REAL    NOT NULL CHECK (preco_brl > 0),
    preco_usd      REAL             CHECK (preco_usd > 0),
    UNIQUE (data_ref, fk_produto_id, fk_regiao_id)
);

-- Clima diario (NASA POWER, grid 0.5 graus)
CREATE TABLE tb_clima (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    data_ref      DATE    NOT NULL,
    fk_regiao_id  INTEGER NOT NULL REFERENCES tb_regiao(id),
    fk_fonte_id   INTEGER NOT NULL REFERENCES tb_fonte(id),
    temp_min      REAL,
    temp_max      REAL,
    temp_media    REAL,
    precip_mm     REAL CHECK (precip_mm >= 0),
    umidade_rel   REAL CHECK (umidade_rel BETWEEN 0 AND 100),
    radiacao_mj   REAL,
    vento_ms      REAL,
    UNIQUE (data_ref, fk_regiao_id)
);

-- Cambio (BCB PTAX) e indice dolar
CREATE TABLE tb_cambio (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    data_ref        DATE    NOT NULL UNIQUE,
    fk_fonte_id     INTEGER NOT NULL REFERENCES tb_fonte(id),
    usd_brl_venda   REAL CHECK (usd_brl_venda > 0),
    usd_brl_compra  REAL CHECK (usd_brl_compra > 0),
    dxy             REAL
);

-- Futuros (ICE KC, ICE Robusta, B3 ICF)
CREATE TABLE tb_futuros (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    data_ref         DATE    NOT NULL,
    fk_contrato_id   INTEGER NOT NULL REFERENCES tb_contrato(id),
    fk_fonte_id      INTEGER NOT NULL REFERENCES tb_fonte(id),
    ajuste           REAL,
    open_interest    REAL,
    ordem_vencimento INTEGER DEFAULT 1,
    UNIQUE (data_ref, fk_contrato_id, ordem_vencimento)
);

-- Posicao especulativa (CFTC Commitments of Traders)
-- data_ref e a terca-feira de referencia; data_pub e a sexta da divulgacao.
CREATE TABLE tb_cot (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    data_ref        DATE    NOT NULL,
    data_pub        DATE,
    fk_contrato_id  INTEGER NOT NULL REFERENCES tb_contrato(id),
    fk_fonte_id     INTEGER NOT NULL REFERENCES tb_fonte(id),
    mm_long         INTEGER,
    mm_short        INTEGER,
    mm_net          INTEGER,
    open_interest   INTEGER,
    UNIQUE (data_ref, fk_contrato_id)
);

-- Macroeconomia (BCB SGS) e petroleo
CREATE TABLE tb_macro (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    data_ref     DATE    NOT NULL UNIQUE,
    fk_fonte_id  INTEGER NOT NULL REFERENCES tb_fonte(id),
    selic        REAL,
    ipca         REAL,
    brent        REAL
);

-- ENSO / El Nino (NOAA CPC). Serie mensal: data_ref e o primeiro dia do mes.
CREATE TABLE tb_enso (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    data_ref     DATE    NOT NULL UNIQUE,
    data_pub     DATE,
    fk_fonte_id  INTEGER NOT NULL REFERENCES tb_fonte(id),
    oni          REAL,
    fase         VARCHAR(12) CHECK (fase IN ('el_nino', 'la_nina', 'neutro'))
);

-- Log de proveniencia de cada execucao do ETL
CREATE TABLE tb_coleta_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fk_fonte_id   INTEGER NOT NULL REFERENCES tb_fonte(id),
    executado_em  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status        VARCHAR(12) NOT NULL CHECK (status IN ('ok', 'vazio', 'erro')),
    linhas        INTEGER DEFAULT 0,
    observacao    VARCHAR(300)
);

-- ===========================================================================
-- 3. INDICES
-- Todas as consultas do pipeline filtram ou ordenam por data_ref.
-- ===========================================================================

CREATE INDEX idx_preco_data     ON tb_preco_cafe (data_ref);
CREATE INDEX idx_preco_produto  ON tb_preco_cafe (fk_produto_id, data_ref);
CREATE INDEX idx_clima_data     ON tb_clima      (data_ref);
CREATE INDEX idx_clima_regiao   ON tb_clima      (fk_regiao_id, data_ref);
CREATE INDEX idx_cambio_data    ON tb_cambio     (data_ref);
CREATE INDEX idx_futuros_data   ON tb_futuros    (data_ref);
CREATE INDEX idx_cot_data       ON tb_cot        (data_ref);
CREATE INDEX idx_cot_pub        ON tb_cot        (data_pub);
CREATE INDEX idx_macro_data     ON tb_macro      (data_ref);
CREATE INDEX idx_enso_data      ON tb_enso       (data_ref);
CREATE INDEX idx_log_fonte      ON tb_coleta_log (fk_fonte_id, executado_em);

-- ===========================================================================
-- 4. CARGA INICIAL DAS DIMENSOES
-- ===========================================================================

INSERT INTO tb_fonte (nome, url, licenca, tipo_acesso) VALUES
  ('CEPEA/ESALQ',  'https://cepea.esalq.usp.br',        'CC BY-NC 4.0', 'manual'),
  ('BCB PTAX',     'https://olinda.bcb.gov.br',         'Publica',      'api'),
  ('BCB SGS',      'https://api.bcb.gov.br',            'Publica',      'api'),
  ('CFTC',         'https://publicreporting.cftc.gov',  'Publica',      'api'),
  ('NASA POWER',   'https://power.larc.nasa.gov',       'Publica',      'api'),
  ('B3',           'https://arquivos.b3.com.br',        'Zona cinza',   'arquivo'),
  ('Yahoo Finance','https://finance.yahoo.com',         'Zona cinza',   'api'),
  ('NOAA CPC',     'https://psl.noaa.gov',              'Publica',      'arquivo'),
  ('CONAB',        'https://www.conab.gov.br',          'Publica',      'arquivo');

INSERT INTO tb_regiao (nome, sigla, latitude, longitude, tipo) VALUES
  ('Bambui/MG',           'bambui',      -20.01, -45.98, 'produtora'),
  ('Varginha/MG',         'sulmg',       -21.55, -45.43, 'produtora'),
  ('Patrocinio/MG',       'cerrado',     -18.94, -46.99, 'produtora'),
  ('Mogiana',             'mogiana',      NULL,   NULL,  'praca'),
  ('Sul de Minas',        'sul_minas',    NULL,   NULL,  'praca'),
  ('Sao Paulo/SP',        'sao_paulo',    NULL,   NULL,  'praca'),
  ('Espirito Santo',      'es',           NULL,   NULL,  'praca');

INSERT INTO tb_produto (nome, especie, unidade) VALUES
  ('Cafe Arabica',            'arabica', 'R$/sc 60kg'),
  ('Cafe Robusta/Conilon',    'robusta', 'R$/sc 60kg');

INSERT INTO tb_contrato (codigo, bolsa, moeda, fator_saca, descricao) VALUES
  ('KC',  'ICE US',     'USD', 1.32277, 'Coffee C Arabica (cUSD/lb)'),
  ('RC',  'ICE Europe', 'USD', 0.06,    'Robusta Coffee (USD/t)'),
  ('ICF', 'B3',         'USD', 1.0,     'Cafe Arabica B3 (USD/sc 60kg)');

-- ===========================================================================
-- 5. VIEW LARGA - entrada do modelo
--
-- Reconstroi a tabela diaria de 1 linha por dia com uma coluna por variavel,
-- que e o formato consumido pelo modelo de series temporais.
--
-- O calendario cheio vem de uma CTE recursiva: fins de semana e feriados
-- nao existem nas tabelas de fato, mas precisam existir na base final.
-- O preenchimento (ffill) e feito no Python, nao aqui.
-- ===========================================================================

CREATE VIEW vw_cafe_diario AS
WITH RECURSIVE calendario(data_ref) AS (
    SELECT (SELECT MIN(data_ref) FROM tb_preco_cafe)
    UNION ALL
    SELECT DATE(data_ref, '+1 day') FROM calendario
    WHERE data_ref < (SELECT MAX(data_ref) FROM tb_preco_cafe)
)
SELECT
    c.data_ref,

    -- Precos fisicos
    MAX(CASE WHEN pr.especie = 'arabica' THEN p.preco_brl END) AS preco_arabica,
    MAX(CASE WHEN pr.especie = 'robusta' THEN p.preco_brl END) AS preco_robusta,

    -- Cambio e macro
    cb.usd_brl_venda AS usd_brl,
    cb.dxy,
    mc.selic,
    mc.brent,

    -- Clima por regiao (todos os campos gravados em tb_clima)
    MAX(CASE WHEN rg.sigla = 'bambui' THEN cl.temp_min END) AS temp_min_bambui,
    MAX(CASE WHEN rg.sigla = 'bambui' THEN cl.temp_max END) AS temp_max_bambui,
    MAX(CASE WHEN rg.sigla = 'bambui' THEN cl.temp_media END) AS temp_media_bambui,
    MAX(CASE WHEN rg.sigla = 'bambui' THEN cl.precip_mm END) AS precip_mm_bambui,
    MAX(CASE WHEN rg.sigla = 'bambui' THEN cl.umidade_rel END) AS umidade_rel_bambui,
    MAX(CASE WHEN rg.sigla = 'bambui' THEN cl.radiacao_mj END) AS radiacao_mj_bambui,
    MAX(CASE WHEN rg.sigla = 'bambui' THEN cl.vento_ms END) AS vento_ms_bambui,

    MAX(CASE WHEN rg.sigla = 'sulmg' THEN cl.temp_min END) AS temp_min_sulmg,
    MAX(CASE WHEN rg.sigla = 'sulmg' THEN cl.temp_max END) AS temp_max_sulmg,
    MAX(CASE WHEN rg.sigla = 'sulmg' THEN cl.temp_media END) AS temp_media_sulmg,
    MAX(CASE WHEN rg.sigla = 'sulmg' THEN cl.precip_mm END) AS precip_mm_sulmg,
    MAX(CASE WHEN rg.sigla = 'sulmg' THEN cl.umidade_rel END) AS umidade_rel_sulmg,
    MAX(CASE WHEN rg.sigla = 'sulmg' THEN cl.radiacao_mj END) AS radiacao_mj_sulmg,
    MAX(CASE WHEN rg.sigla = 'sulmg' THEN cl.vento_ms END) AS vento_ms_sulmg,

    MAX(CASE WHEN rg.sigla = 'cerrado' THEN cl.temp_min END) AS temp_min_cerrado,
    MAX(CASE WHEN rg.sigla = 'cerrado' THEN cl.temp_max END) AS temp_max_cerrado,
    MAX(CASE WHEN rg.sigla = 'cerrado' THEN cl.temp_media END) AS temp_media_cerrado,
    MAX(CASE WHEN rg.sigla = 'cerrado' THEN cl.precip_mm END) AS precip_mm_cerrado,
    MAX(CASE WHEN rg.sigla = 'cerrado' THEN cl.umidade_rel END) AS umidade_rel_cerrado,
    MAX(CASE WHEN rg.sigla = 'cerrado' THEN cl.radiacao_mj END) AS radiacao_mj_cerrado,
    MAX(CASE WHEN rg.sigla = 'cerrado' THEN cl.vento_ms END) AS vento_ms_cerrado,

    -- Futuros
    MAX(CASE WHEN ct.codigo = 'KC'  THEN f.ajuste END) AS ice_kc,
    MAX(CASE WHEN ct.codigo = 'RC'  THEN f.ajuste END) AS ice_rc,
    MAX(CASE WHEN ct.codigo = 'ICF' THEN f.ajuste END) AS b3_cafe_ajuste,

    -- Posicao especulativa: junta por data_pub, nao por data_ref.
    -- E isso que impede o modelo de enxergar na terca um dado que
    -- so ficou publico na sexta.
    ct2.mm_net        AS cot_mm_net,
    ct2.open_interest AS cot_open_interest,

    -- ENSO
    en.oni

FROM calendario c
LEFT JOIN tb_preco_cafe p  ON p.data_ref  = c.data_ref
LEFT JOIN tb_produto    pr ON pr.id       = p.fk_produto_id
LEFT JOIN tb_clima      cl ON cl.data_ref = c.data_ref
LEFT JOIN tb_regiao     rg ON rg.id       = cl.fk_regiao_id
LEFT JOIN tb_cambio     cb ON cb.data_ref = c.data_ref
LEFT JOIN tb_macro      mc ON mc.data_ref = c.data_ref
LEFT JOIN tb_futuros    f  ON f.data_ref  = c.data_ref AND f.ordem_vencimento = 1
LEFT JOIN tb_contrato   ct ON ct.id       = f.fk_contrato_id
LEFT JOIN tb_cot        ct2 ON ct2.data_pub = c.data_ref
LEFT JOIN tb_enso       en ON en.data_ref = DATE(c.data_ref, 'start of month')
GROUP BY c.data_ref
ORDER BY c.data_ref;

-- ===========================================================================
-- 6. CONSULTAS DE VERIFICACAO
-- ===========================================================================

-- Cobertura da variavel-alvo
-- SELECT COUNT(*) AS dias,
--        COUNT(preco_arabica) AS com_preco,
--        ROUND(100.0 * COUNT(preco_arabica) / COUNT(*), 1) AS pct
-- FROM vw_cafe_diario;

-- Faixa de precos (conferir se e plausivel antes de treinar)
-- SELECT MIN(preco_arabica), MAX(preco_arabica) FROM vw_cafe_diario;

-- Ultima coleta por fonte
-- SELECT f.nome, MAX(l.executado_em), l.status, l.linhas
-- FROM tb_coleta_log l JOIN tb_fonte f ON f.id = l.fk_fonte_id
-- GROUP BY f.nome;
