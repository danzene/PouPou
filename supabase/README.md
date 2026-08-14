# Fase 2 — Supabase (cadastro, dados por usuário, gating no servidor)

## O que você faz (uma vez)
1. Crie um projeto grátis em https://supabase.com (New project). Guarde a senha do banco.
2. Em **SQL Editor**, rode os dois arquivos (nesta ordem):
   - `usuarios.sql` — perfil, feira e favoritos por usuário (com RLS e trigger de cadastro).
   - `precos.sql` — tabelas de preço (RLS travado; só o backend lê).
3. Em **Project Settings → API**, copie e me mande **só**:
   - **Project URL** (ex.: `https://xxxx.supabase.co`)
   - **anon public** key (é pública, pode).
   - ⚠️ **NUNCA** me mande a **service_role** key nem a senha do banco. A service_role vai
     só no `.env` do backend, no seu computador. Se colar aqui por engano, gere outra no painel.
4. Em **Authentication → Providers → Email**: deixe "Email" ligado. Pra testar rápido,
   desligue "Confirm email" (senão precisa confirmar por e-mail antes de logar).
5. Em **Authentication → URL Configuration**: adicione `http://localhost:8000` em Site URL
   e Redirect URLs (pro dev local).

## O que eu faço (código)
- Login/cadastro real com supabase-js no front (troca o simulado por conta de verdade).
- Perfil/plano/trial + feira + favoritos lendo/gravando no Supabase (sincroniza entre aparelhos).
- Migração dos dados de preço do SQLite → Postgres (script único).
- Backend lê o Postgres com service_role e aplica o gating (o "onde" só sai pra assinante/trial).
- Depois: Mercado Pago (assinatura recorrente + webhook) — exige você criar a conta MP.

## Sequência
1. **Cadastro real** (auth) ← precisa dos passos 1–5 acima.
2. **Perfil + feira + favoritos por usuário.**
3. **Migração dos preços pro Postgres** + backend apontando pro Supabase + gating no servidor.
4. **Mercado Pago.**
