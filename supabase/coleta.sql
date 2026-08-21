-- ============================================================================
-- MercadoBarato / PouPou — Fila de coleta manual (botão no /gestao)
-- O admin clica no botão -> grava um "pedido" aqui -> o coleta_watcher.py (no PC
-- do dono, via Agendador) pega o pendente e roda o coletar.py. Só o backend toca.
-- ============================================================================
create table if not exists public.coleta_jobs (
  id             bigserial primary key,
  status         text not null default 'pendente',  -- pendente | rodando | concluido | erro
  solicitado_por uuid,
  solicitado_em  timestamptz not null default now(),
  iniciado_em    timestamptz,
  terminado_em   timestamptz,
  resultado      text
);
create index if not exists ix_coleta_jobs_status on public.coleta_jobs(status, id desc);

-- só o backend (conexão owner) e o watcher (conexão owner) tocam. Nada via anon/authenticated.
alter table public.coleta_jobs enable row level security;
revoke all on public.coleta_jobs from anon, authenticated;
