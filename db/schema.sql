-- Schema do comparador de preços (MVP). Histórico append-only em `precos`.

CREATE TABLE IF NOT EXISTS mercados (
  id            INTEGER PRIMARY KEY,
  slug          TEXT UNIQUE NOT NULL,
  nome          TEXT NOT NULL,
  url_base      TEXT NOT NULL,
  plataforma    TEXT,              -- 'vtex' | 'vipcommerce' | 'html' | outro
  logo_url      TEXT,              -- logo licenciado (opcional); senão usa monograma
  cor_badge     TEXT,              -- cor do monograma no front, ex. '#C8102E'
  cidade        TEXT,
  loja_ref      TEXT,              -- CEP/ID/slug da loja fixada (atacarejo/região)
  ativo         INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS categorias (
  slug          TEXT PRIMARY KEY,  -- 'mercearia','bebidas','limpeza','higiene','laticinios'
  nome          TEXT NOT NULL,
  disponivel    INTEGER DEFAULT 1, -- 0 = "em breve" (ex.: hortifruti no MVP)
  ordem         INTEGER
);

CREATE TABLE IF NOT EXISTS produtos (
  ean           TEXT PRIMARY KEY,  -- código de barras = chave única entre mercados
  nome_canonico TEXT,              -- melhor nome observado (mais longo vence)
  marca         TEXT,
  categoria     TEXT REFERENCES categorias(slug),
  unidade       TEXT,              -- 'un', 'kg', 'l', 'ml', 'g'
  qtd_embalagem REAL,              -- preenchido na normalização
  imagem_url    TEXT,
  n_mercados    INTEGER DEFAULT 0, -- em quantos mercados tem preço atual (recomputado na coleta)
  menor_preco   REAL               -- menor preço atual entre os mercados
);

CREATE TABLE IF NOT EXISTS precos (
  id            INTEGER PRIMARY KEY,
  ean           TEXT NOT NULL REFERENCES produtos(ean),
  mercado_id    INTEGER NOT NULL REFERENCES mercados(id),
  preco         REAL NOT NULL,
  preco_por_unidade REAL,          -- calculado na normalização: preco / qtd normalizada
  origem        TEXT DEFAULT 'online',  -- preço ONLINE, não de balcão físico
  coletado_em   TEXT NOT NULL      -- ISO 8601, UTC
);

-- Lista de compras / alertas (single-user local no MVP; multi-usuário vem no modo online).
CREATE TABLE IF NOT EXISTS lista (
  ean         TEXT PRIMARY KEY REFERENCES produtos(ean),
  quantidade  REAL DEFAULT 1,    -- quantas unidades desse item na compra
  preco_alvo  REAL,              -- alerta dispara quando o menor preço < preco_alvo
  comprado    INTEGER DEFAULT 0, -- checklist: 1 = já peguei no mercado
  criado_em   TEXT,
  alertado_em TEXT               -- última vez que o alerta foi marcado como disparado
);

-- URL da página do produto em cada mercado (para o botão "ver no site").
CREATE TABLE IF NOT EXISTS links (
  ean           TEXT NOT NULL,
  mercado_id    INTEGER NOT NULL,
  url           TEXT,
  atualizado_em TEXT,
  PRIMARY KEY (ean, mercado_id)
);

CREATE INDEX IF NOT EXISTS idx_precos_ean ON precos(ean);
CREATE INDEX IF NOT EXISTS idx_precos_mercado ON precos(mercado_id);
CREATE INDEX IF NOT EXISTS idx_precos_coletado ON precos(coletado_em);
CREATE INDEX IF NOT EXISTS idx_precos_ean_merc_col ON precos(ean, mercado_id, coletado_em);
CREATE INDEX IF NOT EXISTS idx_produtos_categoria ON produtos(categoria);

-- Último preço de cada (ean, mercado) — tabela "quente" p/ ranking/listagem/cesta rápidos.
-- O histórico completo continua em `precos`; aqui é só o mais recente (upsert na coleta).
CREATE TABLE IF NOT EXISTS precos_atual (
  ean               TEXT NOT NULL,
  mercado_id        INTEGER NOT NULL,
  preco             REAL NOT NULL,
  preco_por_unidade REAL,
  origem            TEXT DEFAULT 'online',
  coletado_em       TEXT NOT NULL,
  PRIMARY KEY (ean, mercado_id)
);
CREATE INDEX IF NOT EXISTS idx_precos_atual_ean ON precos_atual(ean);
-- (idx_produtos_cat_nmerc é criado em database._migrar, após garantir a coluna n_mercados)

-- Itens FRESCOS/por peso SEM EAN universal (açougue, hortifrúti, padaria).
-- Não cruzam por EAN — ficam à parte, identificados pelo código interno de cada
-- mercado, e são casados por NOME (aproximado) no app. Só o preço atual (upsert).
CREATE TABLE IF NOT EXISTS pereciveis (
  mercado_id   INTEGER NOT NULL REFERENCES mercados(id),
  cod_interno  TEXT NOT NULL,     -- id/slug do item dentro do mercado
  nome         TEXT,
  nome_norm    TEXT,              -- nome sem acento/minúsculo p/ busca e casamento
  categoria    TEXT,              -- seção do mercado: carnes/hortifruti/padaria/frios/peixes/ovos/outros
  preco        REAL NOT NULL,
  unidade      TEXT,              -- 'kg' | 'un' | ...
  imagem_url   TEXT,
  url          TEXT,
  coletado_em  TEXT NOT NULL,
  PRIMARY KEY (mercado_id, cod_interno)
);
CREATE INDEX IF NOT EXISTS idx_pereciveis_norm ON pereciveis(nome_norm);
-- (idx_pereciveis_cat é criado em database._migrar, após garantir a coluna categoria)
