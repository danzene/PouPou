-- ============================================================================
-- MercadoBarato / PouPou — dados de PREÇO (Supabase Postgres)
-- Tradução do schema SQLite (db/schema.sql) + colunas que o app já usa.
-- RLS LIGADO E SEM POLICY pública de propósito: ninguém lê preço direto pelo
-- Supabase. Só o backend (chave service_role, que ignora RLS) lê e aplica o
-- gating (o "onde comprar" só sai pra assinante/trial). Rode no SQL Editor.
-- ============================================================================

create table if not exists public.mercados (
  id         bigint generated always as identity primary key,
  slug       text unique not null,
  nome       text not null,
  url_base   text not null,
  plataforma text,
  logo_url   text,
  cor_badge  text,
  cidade     text,
  loja_ref   text,
  ativo      integer default 1
);

create table if not exists public.categorias (
  slug       text primary key,
  nome       text not null,
  disponivel integer default 1,
  ordem      integer
);

create table if not exists public.produtos (
  ean           text primary key,
  nome_canonico text,
  marca         text,
  categoria     text references public.categorias(slug),
  unidade       text,
  qtd_embalagem double precision,
  imagem_url    text,
  n_mercados    integer default 0,
  menor_preco   double precision
);
create index if not exists idx_produtos_categoria on public.produtos(categoria);
create index if not exists idx_produtos_cat_nmerc on public.produtos(categoria, n_mercados);

-- Histórico append-only (só grava em mudança de preço — dedup no app).
create table if not exists public.precos (
  id                bigint generated always as identity primary key,
  ean               text not null references public.produtos(ean),
  mercado_id        bigint not null references public.mercados(id),
  preco             double precision not null,
  preco_por_unidade double precision,
  origem            text default 'online',
  coletado_em       text not null            -- ISO 8601 UTC (string, ordena lexicograficamente)
);
create index if not exists idx_precos_ean on public.precos(ean);
create index if not exists idx_precos_mercado on public.precos(mercado_id);
create index if not exists idx_precos_coletado on public.precos(coletado_em);
create index if not exists idx_precos_ean_merc_col on public.precos(ean, mercado_id, coletado_em);

-- Preço quente (último por ean+mercado) — listagem/ranking/cesta rápidos.
create table if not exists public.precos_atual (
  ean               text not null,
  mercado_id        bigint not null,
  preco             double precision not null,
  preco_por_unidade double precision,
  origem            text default 'online',
  coletado_em       text not null,
  so_no_site        integer default 0,
  preco_de          double precision,
  desconto_pct      integer,
  oferta_tipo       text,                     -- nome do programa (Viva/Fidelidade) | 'oferta' | null
  primary key (ean, mercado_id)
);
create index if not exists idx_precos_atual_ean on public.precos_atual(ean);

-- Frescos/por peso SEM EAN (açougue/hortifrúti/padaria) — casados por nome.
create table if not exists public.pereciveis (
  mercado_id   bigint not null references public.mercados(id),
  cod_interno  text not null,
  nome         text,
  nome_norm    text,
  categoria    text,
  preco        double precision not null,
  unidade      text,
  imagem_url   text,
  url          text,
  coletado_em  text not null,
  so_no_site   integer default 0,
  preco_de     double precision,
  desconto_pct integer,
  oferta_tipo  text,
  primary key (mercado_id, cod_interno)
);
create index if not exists idx_pereciveis_norm on public.pereciveis(nome_norm);
create index if not exists idx_pereciveis_cat on public.pereciveis(categoria);

-- URL da página do produto em cada mercado (botão "ver no site").
create table if not exists public.links (
  ean           text not null,
  mercado_id    bigint not null,
  url           text,
  atualizado_em text,
  primary key (ean, mercado_id)
);

-- RLS ligado e SEM policy = bloqueia anon/authenticated. Só service_role (backend) lê.
alter table public.mercados     enable row level security;
alter table public.categorias   enable row level security;
alter table public.produtos     enable row level security;
alter table public.precos       enable row level security;
alter table public.precos_atual enable row level security;
alter table public.pereciveis   enable row level security;
alter table public.links        enable row level security;
