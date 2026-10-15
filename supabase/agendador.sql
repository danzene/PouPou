-- Agendador das coletas: o pg_cron do Supabase dispara os workflows do GitHub Actions.
--
-- Por quê: o cron do próprio GitHub ("on: schedule") atrasa horas ou PULA execuções em
-- horário de pico (as coletas agendadas novas não rodaram nenhuma vez em 3h). O pg_cron
-- roda dentro do banco, é pontual, e só precisa chamar a API "workflow_dispatch".
--
-- Pré-requisito: um token do GitHub (fine-grained, SÓ o repositório danzene/PouPou, SÓ
-- a permissão "Actions: Read and write") guardado no Vault com o nome
-- 'github_dispatch_token' (o script definir_token_github.ps1 faz isso sem o token
-- aparecer em lugar nenhum). Idempotente: pode rodar de novo.

create extension if not exists pg_cron;
create extension if not exists pg_net;

-- schema fora da API pública (PostgREST só expõe 'public'): ninguém chama isso do app
create schema if not exists privado;
revoke all on schema privado from public, anon, authenticated;

create or replace function privado.disparar_workflow(arquivo text)
returns bigint
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  tok text;
  req bigint;
begin
  select decrypted_secret into tok
    from vault.decrypted_secrets where name = 'github_dispatch_token';
  if tok is null then
    raise warning 'disparar_workflow: falta o segredo github_dispatch_token no Vault';
    return null;
  end if;
  select net.http_post(
    url     := 'https://api.github.com/repos/danzene/PouPou/actions/workflows/' || arquivo || '/dispatches',
    headers := jsonb_build_object(
                 'Authorization', 'Bearer ' || tok,
                 'Accept', 'application/vnd.github+json',
                 'X-GitHub-Api-Version', '2022-11-28',
                 'User-Agent', 'poupou-pg-cron'),
    body    := '{"ref":"main"}'::jsonb
  ) into req;
  return req;
end;
$$;
revoke all on function privado.disparar_workflow(text) from public, anon, authenticated;

-- Horários em UTC (Brasília = UTC-3). cron.schedule com o mesmo nome substitui o job.
select cron.schedule('poupou-pague-menos', '0 8,20 * * *',   $$select privado.disparar_workflow('paguemenos-nuvem.yml')$$);  -- 05:00 e 17:00
select cron.schedule('poupou-vtex',        '0 11 * * *',     $$select privado.disparar_workflow('coleta-vtex.yml')$$);       -- 08:00
select cron.schedule('poupou-refresh',     '30 */3 * * *',   $$select privado.disparar_workflow('refresh-vtex.yml')$$);      -- a cada 3h
select cron.schedule('poupou-vip-tenda',   '15 1,9,17 * * *',$$select privado.disparar_workflow('coleta-vip-tenda.yml')$$);  -- 22:15, 06:15, 14:15
select cron.schedule('poupou-gpa',         '0 6 * * *',      $$select privado.disparar_workflow('coleta-gpa.yml')$$);        -- 03:00
-- vigia do botão "coletar agora" do /gestao: só dispara quando HÁ pedido pendente
select cron.schedule('poupou-vigia',       '*/3 * * * *',
  $$select privado.disparar_workflow('coleta-watcher.yml')
      where exists (select 1 from public.coleta_jobs where status = 'pendente')$$);

-- Conferir:   select jobname, schedule, active from cron.job;
-- Histórico:  select * from cron.job_run_details order by start_time desc limit 20;
-- Respostas do GitHub (204 = disparou): select status_code, content from net._http_response order by created desc limit 10;
