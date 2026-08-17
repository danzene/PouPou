-- ============================================================================
-- MercadoBarato / PouPou — Pagamentos (Mercado Pago) + TRAVA de segurança do plano
-- Rode no SQL Editor do Supabase. Idempotente (pode rodar de novo sem quebrar).
--
-- ORDEM DE DEPLOY: suba a API nova (com /trial/iniciar e os endpoints de pagamento)
-- e o front novo ANTES de rodar este SQL. Ele TIRA a escrita do cliente em profiles;
-- se o front velho (que grava trial via supabase-js) ainda estiver no ar, o trial
-- para de gravar. Com a API nova, o trial passa a ser gravado pelo servidor.
-- ============================================================================

-- ---------- profiles: validade da assinatura + rastreio Mercado Pago ----------
alter table public.profiles add column if not exists plano_expira_em     timestamptz;
alter table public.profiles add column if not exists ultimo_pagamento_em timestamptz;
alter table public.profiles add column if not exists mp_status           text;  -- last known: approved/authorized/...
alter table public.profiles add column if not exists mp_preapproval_id   text;  -- assinatura recorrente (cartão / pix automático)

-- ---------- SEGURANÇA: o PLANO é do SERVIDOR, não do cliente ----------
-- Antes, a policy "perfil proprio" (FOR ALL) deixava o DONO escrever a própria
-- linha — então dava pra virar 'assinante' de graça pelo console do navegador:
--     sb.from('profiles').upsert({id: meu_id, plano:'assinante'})
-- Agora o cliente só LÊ o próprio perfil. Toda escrita (trial, plano, pagamento)
-- passa pelo backend: conexão Postgres direta como owner (ignora RLS, igual ao
-- admin_set_plano) ou o trigger de cadastro (security definer). O front continua
-- lendo plano/plano_expira_em normalmente (policy de SELECT abaixo).
drop policy if exists "perfil proprio" on public.profiles;
drop policy if exists "perfil leitura propria" on public.profiles;
create policy "perfil leitura propria" on public.profiles
  for select using (auth.uid() = id);

-- corta QUALQUER escrita do cliente (anon/authenticated) na tabela de perfil.
-- o trigger handle_new_user é SECURITY DEFINER -> segue criando o perfil no cadastro.
revoke insert, update, delete on public.profiles from anon, authenticated;

-- ---------- Log de pagamentos/eventos MP: auditoria + idempotência ----------
create table if not exists public.mp_eventos (
  id            bigserial primary key,
  payment_id    text not null,             -- id do pagamento/assinatura no MP (data.id da notificação)
  topico        text,                       -- 'payment' | 'subscription_authorized_payment' | 'subscription_preapproval'
  status        text,                       -- status REAL consultado na API do MP
  uid           uuid,                        -- profiles.id (de external_reference)
  plano_alvo    text,                        -- 'mensal' | 'anual'
  valor         numeric(10,2),
  raw           jsonb,                       -- payload consultado (auditoria)
  processado_em timestamptz not null default now(),
  unique (payment_id, status)               -- mesma (pagamento,status) só processa 1x -> idempotência
);
create index if not exists ix_mp_eventos_uid on public.mp_eventos(uid);

-- mp_eventos é SÓ do backend: liga RLS e NÃO cria policy pública (ninguém via anon/authenticated toca).
alter table public.mp_eventos enable row level security;
revoke all on public.mp_eventos from anon, authenticated;

-- ---------- (opcional, rode DEPOIS de confirmar) faxina de assinantes forjados ----------
-- Enquanto o furo existiu, alguém pode ter setado plano='assinante' sem pagar.
-- Assinante de verdade agora tem plano_expira_em no futuro. Este UPDATE rebaixa quem
-- está 'assinante' SEM validade (forjado/legado). NÃO rode antes de ter certeza —
-- rebaixa também a sua conta de teste se ela virou assinante pelo antigo "Modo teste".
-- update public.profiles set plano='gratis'
--   where plano='assinante' and plano_expira_em is null and is_admin = false;
