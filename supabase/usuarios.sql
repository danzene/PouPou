-- ============================================================================
-- MercadoBarato / PouPou — dados POR USUÁRIO (Supabase Postgres)
-- Perfil (plano/trial), feira (lista de compras) e mercados favoritos.
-- RLS: cada pessoa só enxerga/edita os PRÓPRIOS dados. Rode no SQL Editor do Supabase.
-- ============================================================================

-- ---------- PERFIL: 1 linha por usuário do Supabase Auth ----------
create table if not exists public.profiles (
  id          uuid primary key references auth.users(id) on delete cascade,
  nome        text,
  plano       text not null default 'gratis',   -- 'gratis' | 'trial' | 'assinante'
  trial_ini   timestamptz,                       -- início do reverse trial de 3 dias
  trial_usado boolean not null default false,    -- já usou o trial alguma vez?
  criado_em   timestamptz not null default now(),
  atualizado_em timestamptz not null default now()
);

-- ---------- FEIRA: lista de compras de cada usuário ----------
-- ean SEM foreign key p/ produtos (tabela de preços pode estar noutra base/etapa);
-- o app valida o ean na escrita. Frescos entram como ean = 'f:<chave>'.
create table if not exists public.lista (
  user_id     uuid not null references auth.users(id) on delete cascade,
  ean         text not null,
  quantidade  double precision not null default 1,
  preco_alvo  double precision,                  -- alerta quando menor preço < alvo
  comprado    boolean not null default false,    -- checklist "já peguei"
  nome        text,                              -- rótulo do item (p/ frescos e cache)
  criado_em   timestamptz not null default now(),
  alertado_em timestamptz,
  primary key (user_id, ean)
);
create index if not exists idx_lista_user on public.lista(user_id);

-- ---------- FAVORITOS: mercados que o usuário usa (ordenados) ----------
create table if not exists public.favoritos (
  user_id      uuid not null references auth.users(id) on delete cascade,
  mercado_slug text not null,
  ordem        integer not null default 0,       -- posição escolhida (0 = topo)
  primary key (user_id, mercado_slug)
);
create index if not exists idx_favoritos_user on public.favoritos(user_id);

-- ---------- RLS: dono-só ----------
alter table public.profiles  enable row level security;
alter table public.lista     enable row level security;
alter table public.favoritos enable row level security;

drop policy if exists "perfil proprio" on public.profiles;
create policy "perfil proprio" on public.profiles
  for all using (auth.uid() = id) with check (auth.uid() = id);

drop policy if exists "lista propria" on public.lista;
create policy "lista propria" on public.lista
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists "favoritos proprios" on public.favoritos;
create policy "favoritos proprios" on public.favoritos
  for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- ---------- Trigger: cria o perfil automaticamente ao cadastrar ----------
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.profiles (id, nome)
  values (new.id, coalesce(new.raw_user_meta_data->>'nome', split_part(new.email, '@', 1)))
  on conflict (id) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();
