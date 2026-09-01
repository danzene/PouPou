-- Correção de segurança (alerta Supabase: rls_disabled_in_public).
-- As tabelas da COLETA foram criadas sem RLS. A pior: `app_config` guarda o TOKEN da coleta e
-- estava legível/gravável por qualquer um com a anon key (que é PÚBLICA, fica no front).
--
-- Ligar RLS sem nenhuma POLICY = ninguém via anon/authenticated acessa. O backend (FastAPI) e o
-- robô do GitHub conectam como DONO (via DATABASE_URL), que IGNORA RLS — então continua tudo OK.
-- Já aplicado no banco de produção; rode isto de novo só se recriar o banco.

alter table public.app_config       enable row level security;
alter table public.coleta_cursor    enable row level security;
alter table public.coleta_url_vista enable row level security;

revoke all on public.app_config       from anon, authenticated;
revoke all on public.coleta_cursor    from anon, authenticated;
revoke all on public.coleta_url_vista from anon, authenticated;

-- Obs.: o token da coleta (app_config.coleta_token) foi ROTACIONADO ao aplicar isto, pois ficou
-- exposto enquanto a tabela estava sem RLS. Backend e GitHub releem o novo valor do banco sozinhos.
