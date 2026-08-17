# Plano de Integração Mercado Pago — PouPou+

Sintetizado sobre o código real do MercadoBarato (`api/main.py`, `db/database.py`, `front/index.html`).
Convenções respeitadas: `_conn()` (wrapper com placeholders `?`, autocommit, linhas `row["col"]`),
`_uid_do_header()` para auth, escrita de `profiles` via conexão Postgres direta (ignora RLS, igual ao
`admin_set_plano`), timestamps `datetime.now(timezone.utc).isoformat()`, front same-origin (`API=""`)
com `fetchAuth()` já mandando o Bearer.

> **Decisão-chave do projeto:** "escrever `profiles` com service_role" = escrever via `_conn()` direto
> (conexão Postgres como `postgres`, que ignora RLS), idêntico ao `admin_set_plano` (main.py:1284).
> Não precisa da REST API do Supabase com service_role key no webhook.

---

## 0. SEGURANÇA (pré-requisito) — o plano vira 100% server-side

**Furo atual:** `salvarPerfil()` (front) grava em `profiles` via supabase-js, e a policy RLS
`"perfil proprio" FOR ALL` deixa o dono escrever a própria linha. Logo, qualquer um consegue
`sb.from('profiles').upsert({id: meu_id, plano:'assinante'})` pelo console e virar assinante de graça.
Sem fechar isso, plugar pagamento é inútil.

**Correção (migração `supabase/pagamentos.sql`):**
- `drop policy "perfil proprio"` → cria só `"perfil leitura propria" FOR SELECT`.
- `revoke insert, update, delete on profiles from anon, authenticated`.
- O trigger `handle_new_user` (SECURITY DEFINER) segue criando o perfil no cadastro; o backend escreve
  via `_conn()` (owner, ignora RLS). O front só LÊ.

**O que o cliente escrevia e some:** só `plano`/`trial_*` (via `salvarPerfil`). `nome` entra pelo trigger
no signup; `lista`/`favoritos` são tabelas próprias. Então cortar a escrita do cliente em `profiles` não
quebra nada — só o **trial** precisa migrar pro servidor:

- **`POST /trial/iniciar`** (auth): se `plano=='gratis'` e `not trial_usado`, seta `plano='trial'`,
  `trial_ini=now`, `trial_usado=true` via `_conn()`. O front `iniciarTrial()` passa a chamar isso.
- **Nada mais** no fluxo do usuário escreve `profiles` — só webhook (assinante) e admin.

**Escopo da cobrança (decisão do dono):** recorrente no cartão + Pix avulso + (se a API do MP suportar)
Pix Automático recorrente. Ver §3/§10.

---

## 1. Recomendação — **Checkout Pro avulso (libera N dias)**, recorrente como upsell futuro

Cada pagamento aprovado libera 30 (mensal) ou 365 (anual) dias, gravados em `profiles.plano_expira_em`.
Renovação = novo pagamento.

**Por quê (dev solo, quer Pix+cartão, "cancele quando quiser"):**
- **Pix domina o público e é o mais barato** (~0,99%, na hora) vs. cartão (~3,98–4,98%, D+14/D+30). O
  `/preapproval` (recorrente) **não faz débito automático em Pix** — só cartão. Recorrente hoje seria
  cartão-only, matando o principal meio.
- **"Cancele quando quiser" fica trivial**: no avulso não há nada a cancelar — o acesso expira sozinho.
- **Menos superfície de bug/segurança**: uma preference + um webhook, sem tokenização de cartão no front,
  sem máquina de estados `pending→authorized→paused→cancelled`, sem dunning.
- **Servidor-autoritativo garantido**: plano só muda no webhook validado.

**Trade-off honesto:** avulso **não renova sozinho** → mais churn; precisa de lembrete de vencimento +
botão "renovar em 1 clique". O recorrente renovaria sozinho, mas cartão-only e mais complexo.

**Híbrido futuro (não no MVP):** botão opcional "PouPou+ automático (cartão)" via `/preapproval_plan` +
`/preapproval`. O schema já reserva `mp_preapproval_id`/`mp_status`, então sem migração destrutiva depois.

---

## 2. Banco (Supabase / Postgres)

```sql
-- profiles: validade da assinatura + rastreio MP
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS plano_expira_em     timestamptz;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS ultimo_pagamento_em timestamptz;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS mp_status           text;
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS mp_preapproval_id   text;   -- reservada p/ recorrente futuro

-- log de pagamentos/eventos MP: auditoria + idempotência
CREATE TABLE IF NOT EXISTS mp_eventos (
    id            bigserial PRIMARY KEY,
    payment_id    text NOT NULL,
    topico        text,
    status        text,
    uid           uuid,
    plano_alvo    text,          -- 'mensal' | 'anual'
    valor         numeric(10,2),
    raw           jsonb,
    processado_em timestamptz NOT NULL DEFAULT now(),
    UNIQUE (payment_id, status)  -- mesma (pagamento,status) só processa 1x
);
CREATE INDEX IF NOT EXISTS ix_mp_eventos_uid ON mp_eventos(uid);
```

- `profiles` mantém RLS dono-só de LEITURA (front lê `plano`/`plano_expira_em`). A escrita do webhook usa
  `_conn()` direto (ignora RLS). `mp_eventos` fica **sem** política pública — só o backend a toca.
- `plano='assinante'` deixa de ser eterno: passa a valer `plano='assinante' AND plano_expira_em > now()`.

---

## 3. Endpoints novos no FastAPI (`api/main.py`)

Topo: `import mercadopago, hmac, hashlib, time, json`; `MP_ACCESS_TOKEN`/`MP_WEBHOOK_SECRET` de env;
`def _mp(): return mercadopago.SDK(MP_ACCESS_TOKEN)`. Preços num só lugar (servidor decide o valor):

```python
PLANOS = {"mensal": {"valor": 9.90,  "dias": 30,  "titulo": "PouPou+ Mensal"},
          "anual":  {"valor": 99.90, "dias": 365, "titulo": "PouPou+ Anual"}}
BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://poupou.fly.dev")
```

### 3.1 `POST /assinatura/criar` (autenticado)
Valida uid; escolhe preço do `PLANOS` no servidor; cria preference com `external_reference="{uid}:{plano}"`;
retorna `init_point`. `payment_methods` inclui Pix + cartão (`excluded_payment_types: ticket`, `installments:1`).

### 3.2 `POST /webhooks/mercadopago` (público, mas assinado)
Valida HMAC (§4) → `sdk.payment().get(data_id)` → se `status=="approved"` e não processado, estende
`plano_expira_em` e grava evento (idempotente via `ON CONFLICT (payment_id,status) DO NOTHING`).
Estende a partir de `max(agora, validade_atual)` — não perde dias já pagos.

### 3.3 `GET /assinatura/status` (autenticado)
Retorna `{plano, expira_em, ativo, dias_restantes}`. Front confirma após voltar do checkout (não confia na back_url).

### 3.4 Rebaixamento por expiração — **lazy, sem cron**
`_premium()` passa a exigir `plano_expira_em > now()` (assinantes legados sem data seguem passando por compat).
Opcional: `UPDATE profiles SET plano='gratis' WHERE plano='assinante' AND plano_expira_em < now()` 1x/dia.

### 3.5 (Futuro) `POST /assinatura/cancelar`
Só no híbrido recorrente: `_mp().preapproval().update(id, {"status":"cancelled"})`. No avulso é desnecessário.

---

## 4. Verificação de assinatura do webhook (HMAC — código pronto)

```python
import hmac, hashlib, time
MP_WEBHOOK_SECRET = os.getenv("MP_WEBHOOK_SECRET", "")
_MP_TOLERANCIA_TS = 300  # 5 min anti-replay

def _valida_assinatura_mp(x_signature, x_request_id, data_id) -> bool:
    if not (x_signature and x_request_id and data_id and MP_WEBHOOK_SECRET):
        return False
    ts = v1 = None
    for parte in x_signature.split(","):
        chave, _, valor = parte.partition("=")
        if chave.strip() == "ts":  ts = valor.strip()
        elif chave.strip() == "v1": v1 = valor.strip()
    if not (ts and v1): return False
    try:
        if abs(time.time() - int(ts)) > _MP_TOLERANCIA_TS: return False
    except ValueError:
        return False
    did = data_id.lower() if data_id.isalnum() else data_id
    manifest = f"id:{did};request-id:{x_request_id};ts:{ts};"
    esperado = hmac.new(MP_WEBHOOK_SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, v1)
```

---

## 5. Front (`front/index.html`)

O **trial de 3 dias continua igual** (liberado local em `iniciarTrial()`). Só a **conversão em assinante**
deixa de ser fake.

- **`assinarAgora(plano)`** (linha ~1569): remove o `setPlano("assinante")` fake; chama
  `POST /assinatura/criar` via `fetchAuth` e `window.location = init_point`.
- Botões da tela de assinatura: `assinarAgora('mensal')` e `assinarAgora('anual')`.
- **Retorno `?pagamento=`** no boot (~linha 1932): re-hidrata o perfil algumas vezes (o webhook leva
  segundos), toast de sucesso/pendente, `history.replaceState` limpa a query.
- `setPlano` fake **não** é removida (o admin usa), mas nada do fluxo do usuário comum a chama.
- **Public Key não é necessária** no Checkout Pro por redirect (cartão tokenizado nas telas do MP).

---

## 6. Segredos / config

| Var | Onde | Sigilo |
|---|---|---|
| `MP_ACCESS_TOKEN` | Fly secret | **SECRETO** |
| `MP_WEBHOOK_SECRET` | Fly secret | **SECRETO** |
| `PUBLIC_BASE_URL` | `[env]` do fly.toml | público |
| Public Key | não usar no MVP | público |

`requirements.txt`: `mercadopago>=2.2`.

```bash
fly secrets set MP_ACCESS_TOKEN="APP_USR-xxxx" -a poupou
fly secrets set MP_WEBHOOK_SECRET="assinatura_secreta_do_painel" -a poupou
```
`.env` local usa credenciais de **teste** (`TEST-...`).

---

## 7. Teste em sandbox

1. Credenciais de teste no `.env`.
2. `uvicorn` local + **ngrok** (`ngrok http 8000`) — o MP precisa de HTTPS público p/ o `notification_url`;
   aponte `PUBLIC_BASE_URL` pra URL do ngrok.
3. Criar preference pelo front → redireciona ao `init_point` de sandbox.
4. Pagar como comprador de teste, titular **`APRO`**, CPF `12345678909` → aprovado.
5. **Simulador de webhook** do painel (tópico `payment`) → confirma HMAC + `profiles` vira `assinante`
   com `plano_expira_em` certo.
6. Idempotência: reenviar a mesma notificação → `mp_eventos` bloqueia.
7. Pix: gera QR de teste, `pending` → webhook `approved` libera.
8. Expiração: força `plano_expira_em` no passado → `_premium()` volta a travar.

---

## 8. Checklist do DONO (manual — só você faz)

- [ ] Criar/validar conta Mercado Pago (é a que **recebe o dinheiro**).
- [ ] Cadastrar **dados bancários** + **chave Pix** p/ saque.
- [ ] *Suas integrações* → **Criar aplicação** "PouPou" (Pagamentos online). Gera credenciais teste+produção.
- [ ] Criar **contas de teste** (1 Seller, 1 Buyer) p/ o sandbox.
- [ ] Configurar **Webhook** (produção): URL `https://poupou.fly.dev/webhooks/mercadopago`, evento `payment`;
      **gerar a assinatura secreta** e guardar.
- [ ] **Ativar credenciais de produção**.
- [ ] Entregar (fora do chat) o **Access Token** e a **assinatura secreta** de produção → `fly secrets set`.

---

## 9. Ordem de implementação (fases pequenas e testáveis)

1. **Migração DB** (§2) no Supabase.
2. **`_premium()` com validade** (§3.4) + `mercadopago` no requirements.
3. **`POST /assinatura/criar`** (§3.1) — testável via `curl` autenticado.
4. **HMAC** (§4) + **`POST /webhooks/mercadopago`** + `_aplicar_pagamento` (§3.2) — testável no simulador do painel.
5. **Front `assinarAgora()` + retorno `?pagamento=`** (§5) — ponta a ponta no sandbox.
6. **`GET /assinatura/status`** (§3.3) + lembretes de vencimento.
7. **Deploy produção**: secrets reais, webhook de produção, 1 pagamento real de valor baixo.
8. **(Futuro)** Híbrido recorrente (`/preapproval_plan`).

---

## Arquivos tocados
- `api/main.py` — endpoints, HMAC, `_premium()`, `PLANOS`, imports.
- `front/index.html` — `assinarAgora()` (~1569), boot/retorno (~1932).
- `requirements.txt` — `mercadopago>=2.2`.
- `.env.example` / `.env` — `MP_ACCESS_TOKEN`, `MP_WEBHOOK_SECRET`, `PUBLIC_BASE_URL`.
- Supabase — nova migração SQL (§2).
