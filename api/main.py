"""API de busca e comparação de preços (Fase 3 — com categorias, identidade e sparkline).

Endpoints:
    GET /categorias          -> categorias (com flag disponivel)
    GET /categoria/{slug}     -> produtos da categoria + ranking enriquecido
    GET /buscar?q=            -> produtos que casam (nome/EAN) + ranking enriquecido
    GET /produto/{ean}        -> um produto + ranking enriquecido
    GET /historico/{ean}      -> todos os snapshots por mercado (série completa)
    GET /                     -> front (front/index.html)

Cada linha de ranking traz: mercado (nome, slug, cor_badge, logo_url), preço,
preço/unidade, origem, data, sparkline (série curta) e tendência textual.
Roda com:  python -m uvicorn api.main:app --port 8000
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import Body, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse

from analise import normalizar
from analise.ranking import _unaccent, buscar_com_tipos
from db import database

load_dotenv()
RAIZ = Path(__file__).resolve().parent.parent
FRONT = RAIZ / "front" / "index.html"
LOGOS_DIR = RAIZ / "front" / "logos"

app = FastAPI(title="Comparador de Preços — RM Campinas", version="0.5")


# ===================== COLETA NA NUVEM (Pague Menos, IP no Brasil) =====================
# O Pague Menos bloqueia IP de fora do BR. Este servidor (Vercel gru1/SP) tem IP brasileiro
# e PASSA, então a coleta roda AQUI, uma leva pequena por chamada (cabe nos 60s do Vercel).
# Um disparador externo (GitHub Actions) bate nestes endpoints em sequência — ele nunca fala
# com o Pague Menos, só com a gente, então não sofre o bloqueio geográfico.
_COLETA_PM_SLUG = "superpaguemenos"


def _coleta_pm_token_ok(conn, token: str | None) -> bool:
    """O token esperado fica na tabela app_config (chave='coleta_token'). Vercel e o disparador
    (GitHub) leem o MESMO banco, então compartilham o segredo sem precisar de env em dois lugares.
    Quem não tem acesso ao banco não consegue o token -> não dispara coleta pelo nosso servidor."""
    if not token:
        return False
    try:
        row = conn.execute("SELECT valor FROM app_config WHERE chave = ?",
                           ("coleta_token",)).fetchone()
    except Exception:  # noqa: BLE001  (tabela ainda não existe)
        return False
    esperado = (row["valor"] if row else "") or ""
    return bool(esperado) and hmac.compare_digest(token, esperado)


def _pm_upsert_item(conn, mid: int, item, agora: str) -> None:
    """Grava um item coletado (mesma lógica do coletar.py): perecível -> tabela pereciveis;
    industrializado -> produto + preço + registra a URL em `links` (pra o refresh reachá-lo)."""
    from coletores.base import ItemColeta, ItemPerecivel
    if isinstance(item, ItemPerecivel):
        database.upsert_perecivel(
            conn, mercado_id=mid, cod_interno=item.cod_interno, nome=item.nome,
            preco=item.preco, unidade=item.unidade, imagem_url=item.imagem_url,
            url=item.url, coletado_em=agora, so_no_site=item.so_no_site,
            preco_de=item.preco_de, desconto_pct=item.desconto_pct,
            oferta_tipo=item.oferta_tipo)
    elif isinstance(item, ItemColeta):
        medida = normalizar.extrair_medida(item.nome)
        ppu, _ = normalizar.preco_por_unidade(item.preco, medida)
        database.upsert_produto(
            conn, ean=item.ean, nome=item.nome, marca=item.marca, categoria=item.categoria,
            unidade=(medida.unidade if medida else item.unidade),
            qtd_embalagem=(medida.qtd_base if medida else None), imagem_url=item.imagem_url)
        database.inserir_preco(
            conn, ean=item.ean, mercado_id=mid, preco=item.preco, preco_por_unidade=ppu,
            coletado_em=agora, so_no_site=item.so_no_site, preco_de=item.preco_de,
            desconto_pct=item.desconto_pct, oferta_tipo=item.oferta_tipo)
        if item.url:
            database.upsert_link(conn, ean=item.ean, mercado_id=mid, url=item.url,
                                 atualizado_em=agora)


@app.get("/cron/pm-batch")
def cron_pm_batch(x_coleta_token: str | None = Header(None)):
    """Coleta UMA leva (~30 produtos) do Pague Menos a partir deste servidor (IP no Brasil).
    O cursor (posição no catálogo) fica no banco e roda sozinho a cada chamada — cobre o
    catálogo inteiro em várias chamadas e segue reatualizando."""
    from coletores.convertiez import BloqueioDetectado, ColetorConvertiez

    LEVA, DELAY = 30, 0.3
    conn = _conn()
    try:
        if not _coleta_pm_token_ok(conn, x_coleta_token):
            raise HTTPException(status_code=401, detail="token inválido")
        m = conn.execute("SELECT id, nome, url_base FROM mercados WHERE slug = ?",
                         (_COLETA_PM_SLUG,)).fetchone()
        if not m:
            raise HTTPException(status_code=500, detail="mercado não existe")
        mid = m["id"]
        conn.execute("CREATE TABLE IF NOT EXISTS coleta_cursor ("
                     "mercado_id integer PRIMARY KEY, pos integer NOT NULL DEFAULT 0, "
                     "atualizado_em text)")
        total = conn.execute(
            "SELECT (SELECT count(*) FROM links WHERE mercado_id=? AND url IS NOT NULL) + "
            "(SELECT count(*) FROM pereciveis WHERE mercado_id=? AND url IS NOT NULL) AS t",
            (mid, mid)).fetchone()["t"]
        if not total:
            return {"ok": True, "feitos": 0, "total": 0, "obs": "sem URLs conhecidas"}
        crow = conn.execute("SELECT pos FROM coleta_cursor WHERE mercado_id = ?", (mid,)).fetchone()
        pos = (crow["pos"] if crow else 0) % total
        rows = conn.execute(
            "SELECT url FROM ("
            "  SELECT url FROM links WHERE mercado_id=? AND url IS NOT NULL "
            "  UNION SELECT url FROM pereciveis WHERE mercado_id=? AND url IS NOT NULL"
            ") q ORDER BY url LIMIT ? OFFSET ?", (mid, mid, LEVA, pos)).fetchall()
        urls = [r["url"] for r in rows]

        col = ColetorConvertiez(slug=_COLETA_PM_SLUG, nome=m["nome"], url_base=m["url_base"],
                                delay=DELAY, contato="contato@poupou.app")
        base = col.url_base
        agora = datetime.now(timezone.utc).isoformat()
        feitos = bloqueado = 0
        with httpx.Client(headers=col._headers(), timeout=col.timeout,
                          follow_redirects=True) as client:
            for u in urls:
                href = u[len(base):] if u.startswith(base) else u
                try:
                    item = col._produto(client, href, None)
                except BloqueioDetectado:
                    bloqueado = 1
                    break
                except Exception:  # noqa: BLE001
                    continue
                if item is None:
                    continue
                _pm_upsert_item(conn, mid, item, agora)
                feitos += 1
        # só avança o cursor se NÃO bloqueou (bloqueou -> repete a mesma fatia na próxima)
        novo = pos if bloqueado else (pos + LEVA) % total
        conn.execute(
            "INSERT INTO coleta_cursor (mercado_id, pos, atualizado_em) VALUES (?, ?, ?) "
            "ON CONFLICT (mercado_id) DO UPDATE SET pos = excluded.pos, "
            "atualizado_em = excluded.atualizado_em", (mid, novo, agora))
        return {"ok": True, "feitos": feitos, "de": pos, "para": novo,
                "total": total, "bloqueado": bool(bloqueado)}
    finally:
        conn.close()


@app.get("/cron/pm-lista")
def cron_pm_lista(x_coleta_token: str | None = Header(None)):
    """Refresh do Pague Menos PELA LISTAGEM: cada página de departamento traz ~40 produtos
    com preço, então ~450 páginas cobrem o catálogo (vs ~20 mil páginas de produto no
    pm-batch). Atualiza só produtos já conhecidos (URL -> EAN via `links`); item por peso
    e descoberta de novos seguem no pm-batch/pm-descobrir. Cursor 'depto:pagina' no
    app_config; 'volta'=True quando fecha o ciclo de todos os departamentos."""
    from coletores.convertiez import (DEPARTAMENTOS, BloqueioDetectado, ColetorConvertiez,
                                      extrair_listagem)

    PAGINAS, ORCAMENTO_S, DELAY = 8, 35.0, 0.3   # cabe folgado no maxDuration=60 do Vercel
    conn = _conn()
    try:
        if not _coleta_pm_token_ok(conn, x_coleta_token):
            raise HTTPException(status_code=401, detail="token inválido")
        m = conn.execute("SELECT id, nome, url_base FROM mercados WHERE slug = ?",
                         (_COLETA_PM_SLUG,)).fetchone()
        if not m:
            raise HTTPException(status_code=500, detail="mercado não existe")
        mid = m["id"]
        row = conn.execute("SELECT valor FROM app_config WHERE chave = 'pm_lista_cursor'").fetchone()
        try:
            i, p = (int(x) for x in (row["valor"] if row else "0:1").split(":"))
        except ValueError:
            i, p = 0, 1
        if not (0 <= i < len(DEPARTAMENTOS)) or p < 1:
            i, p = 0, 1

        col = ColetorConvertiez(slug=_COLETA_PM_SLUG, nome=m["nome"], url_base=m["url_base"],
                                delay=DELAY, contato="contato@poupou.app")
        base = col.url_base
        agora = datetime.now(timezone.utc).isoformat()
        t0 = time.monotonic()
        paginas = lidos = gravados = 0
        bloqueado = volta = False
        with httpx.Client(headers=col._headers(), timeout=col.timeout,
                          follow_redirects=True) as client:
            while paginas < PAGINAS and time.monotonic() - t0 < ORCAMENTO_S:
                try:
                    resp = col._get(client, f"{base}/{DEPARTAMENTOS[i]}/?p={p}")
                except BloqueioDetectado:
                    bloqueado = True
                    break
                if resp is None or (resp.status_code != 200 and resp.status_code != 404):
                    break   # falha pontual: não avança o cursor, repete na próxima chamada
                paginas += 1
                # 404 = passou da última página (o site não devolve página vazia)
                itens = extrair_listagem(resp.text) if resp.status_code == 200 else []
                if not itens:   # passou da última página do departamento -> próximo
                    i, p = i + 1, 1
                    if i >= len(DEPARTAMENTOS):
                        i, volta = 0, True
                        break
                    continue
                lidos += len(itens)
                por_url = {base + it["href"]: it for it in itens}
                marcas = ",".join("?" * len(por_url))
                conhecidos = conn.execute(
                    "SELECT l.url, l.ean, pr.nome_canonico AS nome, pa.so_no_site FROM links l "
                    "JOIN produtos pr ON pr.ean = l.ean "
                    "LEFT JOIN precos_atual pa ON pa.ean = l.ean AND pa.mercado_id = l.mercado_id "
                    f"WHERE l.mercado_id = ? AND l.url IN ({marcas})",
                    (mid, *por_url)).fetchall()
                for r in conhecidos:
                    it = por_url[r["url"]]
                    ppu, _ = normalizar.preco_por_unidade(it["preco"], normalizar.extrair_medida(r["nome"]))
                    database.inserir_preco(
                        conn, ean=r["ean"], mercado_id=mid, preco=it["preco"], preco_por_unidade=ppu,
                        coletado_em=agora, so_no_site=bool(r["so_no_site"]),
                        preco_de=it["preco_de"], desconto_pct=it["desconto_pct"])
                    gravados += 1
                p += 1
        conn.execute(
            "INSERT INTO app_config (chave, valor) VALUES ('pm_lista_cursor', ?) "
            "ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor", (f"{i}:{p}",))
        return {"ok": True, "paginas": paginas, "lidos": lidos, "gravados": gravados,
                "cursor": f"{DEPARTAMENTOS[i]}:{p}", "volta": volta, "bloqueado": bloqueado}
    finally:
        conn.close()


@app.get("/cron/pm-descobrir")
def cron_pm_descobrir(x_coleta_token: str | None = Header(None)):
    """Descobre produtos NOVOS do Pague Menos: lê o sitemap (poucas requisições), acha URLs que
    ainda não estão no banco e coleta uma leva delas (limitada, pra ser gentil). Uma vez no banco,
    o refresh diário passa a mantê-las frescas. Chamar 1x/dia."""
    from coletores.convertiez import BloqueioDetectado, ColetorConvertiez

    LEVA_DESC, DELAY = 20, 0.3
    conn = _conn()
    try:
        if not _coleta_pm_token_ok(conn, x_coleta_token):
            raise HTTPException(status_code=401, detail="token inválido")
        m = conn.execute("SELECT id, nome, url_base FROM mercados WHERE slug = ?",
                         (_COLETA_PM_SLUG,)).fetchone()
        if not m:
            raise HTTPException(status_code=500, detail="mercado não existe")
        mid = m["id"]
        # URLs já TENTADAS (deram item OU vieram vazias): marcamos pra NÃO re-tentar todo dia —
        # muitas URLs do sitemap são variantes de um EAN já conhecido ou páginas fora de estoque
        # que voltam None. Sem isso o "backlog" nunca drenava. Assim cada URL é tentada 1x.
        conn.execute("CREATE TABLE IF NOT EXISTS coleta_url_vista ("
                     "mercado_id integer, url text, vista_em text, PRIMARY KEY (mercado_id, url))")
        col = ColetorConvertiez(slug=_COLETA_PM_SLUG, nome=m["nome"], url_base=m["url_base"],
                                delay=DELAY, contato="contato@poupou.app")
        base = col.url_base
        agora = datetime.now(timezone.utc).isoformat()
        known = set()
        for r in conn.execute("SELECT url FROM links WHERE mercado_id=? AND url IS NOT NULL", (mid,)):
            known.add(r["url"])
        for r in conn.execute("SELECT url FROM pereciveis WHERE mercado_id=? AND url IS NOT NULL", (mid,)):
            known.add(r["url"])
        for r in conn.execute("SELECT url FROM coleta_url_vista WHERE mercado_id=?", (mid,)):
            known.add(r["url"])
        novos_coletados = bloqueado = 0
        with httpx.Client(headers=col._headers(), timeout=col.timeout,
                          follow_redirects=True) as client:
            try:
                sitemap = col._urls_sitemap(client)
            except BloqueioDetectado:
                return {"ok": False, "bloqueado": True, "obs": "bloqueio ao ler sitemap"}
            if not sitemap:
                return {"ok": True, "sitemap_total": 0, "obs": "sitemap vazio/indisponível"}
            novos = [u for u in sitemap if u not in known]
            for u in novos[:LEVA_DESC]:
                href = u[len(base):] if u.startswith(base) else u
                try:
                    item = col._produto(client, href, None)
                except BloqueioDetectado:
                    bloqueado = 1
                    break
                except Exception:  # noqa: BLE001
                    item = None
                if item is not None:
                    _pm_upsert_item(conn, mid, item, agora)
                    novos_coletados += 1
                # marca a URL como TENTADA (com item ou vazia) pra não re-tentar amanhã
                conn.execute("INSERT INTO coleta_url_vista (mercado_id, url, vista_em) "
                             "VALUES (?, ?, ?) ON CONFLICT (mercado_id, url) DO NOTHING",
                             (mid, u, agora))
        return {"ok": True, "sitemap_total": len(sitemap), "conhecidos": len(known),
                "novos_no_sitemap": len(novos), "novos_coletados": novos_coletados,
                "bloqueado": bool(bloqueado)}
    finally:
        conn.close()


@app.get("/cron/pm-finalizar")
def cron_pm_finalizar(x_coleta_token: str | None = Header(None)):
    """Reconstrói o resumo (chamar UMA vez ao fim da rodada diária)."""
    conn = _conn()
    try:
        if not _coleta_pm_token_ok(conn, x_coleta_token):
            raise HTTPException(status_code=401, detail="token inválido")
        database.rebuild_resumo(conn)
        return {"ok": True}
    finally:
        conn.close()

# ---- Auth (Supabase): valida o Bearer token e diz QUEM é o usuário + se é premium ----
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://ijtnozcomqzxfaidurso.supabase.co").rstrip("/")
SUPABASE_ANON = os.getenv(
    "SUPABASE_ANON_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImlqdG5vemNvbXF6"
    "eGZhaWR1cnNvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODY2NzY2MTAsImV4cCI6MjEwMjI1MjYxMH0."
    "9Hl9d2fK0yiD7WAaMZZfRuqAJeJ5F8skGIpWNkpfl9o")  # chave PÚBLICA (anon)
_tok_cache: dict = {}   # token -> (user_id, expira_em): evita bater no Supabase a cada request

# ---- Mercado Pago (pagamentos) ----
# .strip(): blinda contra espaço/tab/newline que vem colado no copy-paste do painel de env.
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()            # SECRETO — nunca no front
MP_WEBHOOK_SECRET = os.getenv("MP_WEBHOOK_SECRET", "").strip()        # SECRETO — assina os webhooks
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://poupou.fly.dev").strip().rstrip("/")
_MP_TOLERANCIA_TS = 300   # janela anti-replay do webhook (5 min)
# Preço e duração num só lugar: o SERVIDOR escolhe o valor a partir do id do plano —
# o cliente NUNCA manda preço. 'meses' é pra recorrência no cartão (auto_recurring).
PLANOS = {
    "mensal": {"valor": 9.90, "dias": 30, "meses": 1, "titulo": "PouPou+ Mensal"},
    "anual": {"valor": 99.90, "dias": 365, "meses": 12, "titulo": "PouPou+ Anual"},
}


def _mp():
    """SDK do Mercado Pago (import tardio: não quebra o boot se o pacote faltar num env)."""
    import mercadopago
    return mercadopago.SDK(MP_ACCESS_TOKEN)


def _uid_do_header(authorization: str | None) -> str | None:
    """Valida o token do Supabase (via /auth/v1/user) e devolve o user_id (uuid), ou None."""
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1]
    ent = _tok_cache.get(token)
    if ent and ent[1] > time.time():
        return ent[0]
    try:
        r = httpx.get(f"{SUPABASE_URL}/auth/v1/user",
                      headers={"Authorization": f"Bearer {token}", "apikey": SUPABASE_ANON}, timeout=8)
        uid = r.json().get("id") if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        uid = None
    if uid:
        _tok_cache[token] = (uid, time.time() + 300)  # cache 5 min
    return uid


_email_cache: dict = {}


def _email_do_header(authorization: str | None) -> str | None:
    """E-mail do usuário logado (via Supabase /auth/v1/user). Necessário pro payer_email do
    preapproval. Cacheado 5 min por token."""
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1]
    ent = _email_cache.get(token)
    if ent and ent[1] > time.time():
        return ent[0]
    try:
        r = httpx.get(f"{SUPABASE_URL}/auth/v1/user",
                      headers={"Authorization": f"Bearer {token}", "apikey": SUPABASE_ANON}, timeout=8)
        email = r.json().get("email") if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        email = None
    if email:
        _email_cache[token] = (email, time.time() + 300)
    return email


def _premium(conn, uid: str | None) -> bool:
    """True se o usuário é assinante COM validade no futuro OU está com o reverse trial
    de 3 dias ativo (lê profiles). A assinatura agora expira: plano='assinante' só libera
    enquanto plano_expira_em > agora (cada pagamento aprovado estende essa data no webhook)."""
    if not uid:
        return False
    r = conn.execute("SELECT plano, trial_ini, plano_expira_em, trial_dias FROM profiles WHERE id = ?", (uid,)).fetchone()
    if not r:
        return False
    if r["plano"] == "assinante":
        exp = r["plano_expira_em"]
        if not exp:
            return True   # compat: assinante legado sem validade (pré-Mercado Pago)
        if isinstance(exp, str):
            try:
                exp = datetime.fromisoformat(exp)
            except ValueError:
                return True
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < exp
    if r["plano"] == "trial" and r["trial_ini"]:
        ini = r["trial_ini"]
        if isinstance(ini, str):
            try:
                ini = datetime.fromisoformat(ini)
            except ValueError:
                return False
        if ini.tzinfo is None:
            ini = ini.replace(tzinfo=timezone.utc)
        dias = r["trial_dias"] or TRIAL_DIAS_PADRAO   # indicado teve trial dobrado (ver trial_iniciar)
        return datetime.now(timezone.utc) - ini < timedelta(days=dias)
    return False


def _admin_uid(conn, authorization: str | None) -> str | None:
    """Devolve o uid SÓ se o usuário autenticado for admin (is_admin em profiles); senão None.
    Todo endpoint /admin/* passa por aqui — sem admin, 403."""
    uid = _uid_do_header(authorization)
    if not uid:
        return None
    r = conn.execute("SELECT is_admin FROM profiles WHERE id = ?", (uid,)).fetchone()
    return uid if (r and r["is_admin"]) else None


def _cutoff_horas(horas: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat()


def _gate_cesta(resp: dict, premium: bool) -> dict:
    """TRAVA NO SERVIDOR: pra quem NÃO é premium, remove o "onde comprar" (rota, ranking,
    preço por mercado e qual loja) do payload — o dado acionável nem sai do backend. Mantém
    os NÚMEROS (economia, totais) que são a isca. Assinante/trial recebe tudo."""
    if premium:
        return resp
    resp["mix"]["divisao"] = []          # tira a rota "compre X no mercado Y"
    resp["mercados"] = []                # tira o ranking completo de lojas
    mm = resp.get("melhor_mercado")
    if mm:                               # mantém o TOTAL, mascara QUAL loja
        mm["nome"] = mm["slug"] = mm["cor_badge"] = None
    for it in resp.get("itens", []):
        m = it.get("melhor")
        it["melhor"] = {"preco": m.get("preco"), "rotulo": m.get("rotulo")} if m else None  # só o menor preço
        it["precos"] = []                # tira os preços por mercado
    return resp

def _valida_assinatura_mp(x_signature: str | None, x_request_id: str | None,
                          data_id: str | None) -> bool:
    """Valida o webhook do Mercado Pago: HMAC-SHA256 sobre o manifest 'id:..;request-id:..;ts:..;'.
    Sem secret/assinatura -> rejeita. Janela anti-replay de 5 min."""
    if not (x_signature and x_request_id and data_id and MP_WEBHOOK_SECRET):
        return False
    ts = v1 = None
    for parte in x_signature.split(","):
        chave, _, valor = parte.partition("=")
        chave, valor = chave.strip(), valor.strip()
        if chave == "ts":
            ts = valor
        elif chave == "v1":
            v1 = valor
    if not (ts and v1):
        return False
    try:
        if abs(time.time() - int(ts)) > _MP_TOLERANCIA_TS:
            return False
    except ValueError:
        return False
    did = data_id.lower() if data_id.isalnum() else data_id
    manifest = f"id:{did};request-id:{x_request_id};ts:{ts};"
    esperado = hmac.new(MP_WEBHOOK_SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, v1)


def _registra_evento(conn, payment_id, topico, status, uid, plano_id, valor, raw) -> bool:
    """Grava o evento MP e devolve True só se for NOVO. Idempotência: a mesma (payment_id,status)
    entra 1x (UNIQUE), então reprocessar um webhook não estende o acesso de novo."""
    cur = conn.execute(
        "INSERT INTO mp_eventos (payment_id, topico, status, uid, plano_alvo, valor, raw) "
        "VALUES (?, ?, ?, ?, ?, ?, ?::jsonb) ON CONFLICT (payment_id, status) DO NOTHING",
        (str(payment_id), topico, status, uid, plano_id, valor, json.dumps(raw, default=str)))
    return getattr(cur, "rowcount", 0) != 0


def _estende_assinatura(conn, uid, plano_id, *, mp_status="approved", preapproval_id=None) -> str:
    """Marca plano='assinante' e estende plano_expira_em a partir do MAIOR entre agora e a
    validade atual (não perde dias já pagos). Escreve via _conn() (owner, ignora RLS)."""
    dias = PLANOS[plano_id]["dias"]
    agora = datetime.now(timezone.utc)
    base = agora
    r = conn.execute("SELECT plano_expira_em FROM profiles WHERE id = ?", (uid,)).fetchone()
    if r and r["plano_expira_em"]:
        atual = r["plano_expira_em"]
        if isinstance(atual, str):
            try:
                atual = datetime.fromisoformat(atual)
            except ValueError:
                atual = agora
        if atual.tzinfo is None:
            atual = atual.replace(tzinfo=timezone.utc)
        if atual > agora:
            base = atual
    nova = (base + timedelta(days=dias)).isoformat()
    if preapproval_id is not None:
        conn.execute(
            "UPDATE profiles SET plano='assinante', plano_expira_em=?, ultimo_pagamento_em=?, "
            "mp_status=?, mp_preapproval_id=?, atualizado_em=? WHERE id=?",
            (nova, agora.isoformat(), mp_status, preapproval_id, agora.isoformat(), uid))
    else:
        conn.execute(
            "UPDATE profiles SET plano='assinante', plano_expira_em=?, ultimo_pagamento_em=?, "
            "mp_status=?, atualizado_em=? WHERE id=?",
            (nova, agora.isoformat(), mp_status, agora.isoformat(), uid))
    return nova


# ===== INDIQUE E GANHE (programa de indicação) =============================================
# Regra (mão-dupla):
#   - INDICADO: quem se cadastra por um link ganha um TRIAL DOBRADO (7 dias em vez de 3),
#     logo no cadastro, sem precisar pagar (ver TRIAL_DIAS_* e trial_iniciar).
#   - PADRINHO: a cada 2 indicados que ASSINAREM (viram pagantes) ganha 1 mês grátis.
# Anti-fraude: o crédito do padrinho só conta indicado que PAGOU (não basta cadastrar);
# veta auto-indicação; tudo idempotente (a recorrência dispara o webhook todo mês).
REF_INDICADOS_POR_MES = 2      # a cada N indicados convertidos, o PADRINHO ganha 1 mês
REF_DIAS_MES = 30              # "1 mês grátis" = 30 dias
_REF_ALFABETO = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # sem 0/O/1/I/L (evita confusão ao digitar)
TRIAL_DIAS_PADRAO = 3          # reverse trial normal
TRIAL_DIAS_INDICADO = 7        # trial de quem chegou por indicação (o "dobro" — perk do indicado)


def _dar_dias(conn, uid, dias) -> None:
    """Concede `dias` de acesso PouPou+ a `uid`, estendendo a partir do MAIOR entre agora e a
    validade atual (não descarta dias já pagos). Vira 'assinante'. Usado nos bônus de indicação
    — NÃO mexe em mp_status/preapproval (não é pagamento, é cortesia)."""
    if not uid or dias <= 0:
        return
    agora = datetime.now(timezone.utc)
    base = agora
    r = conn.execute("SELECT plano_expira_em FROM profiles WHERE id = ?", (uid,)).fetchone()
    if r and r["plano_expira_em"]:
        atual = r["plano_expira_em"]
        if isinstance(atual, str):
            try:
                atual = datetime.fromisoformat(atual)
            except ValueError:
                atual = agora
        if atual.tzinfo is None:
            atual = atual.replace(tzinfo=timezone.utc)
        if atual > agora:
            base = atual
    nova = (base + timedelta(days=int(dias))).isoformat()
    conn.execute(
        "UPDATE profiles SET plano='assinante', plano_expira_em=?, atualizado_em=? WHERE id=?",
        (nova, agora.isoformat(), uid))


def _gerar_ref_code(conn, uid) -> str | None:
    """Devolve o ref_code do usuário; cria um curto e único (6 chars) se ainda não tiver.
    Idempotente: a claim é o UPDATE ... WHERE ref_code IS NULL (rowcount==1 ganhou)."""
    if not uid:
        return None
    r = conn.execute("SELECT ref_code FROM profiles WHERE id = ?", (uid,)).fetchone()
    if r and r["ref_code"]:
        return r["ref_code"]
    for _ in range(8):
        code = "".join(secrets.choice(_REF_ALFABETO) for _ in range(6))
        try:
            cur = conn.execute(
                "UPDATE profiles SET ref_code = ? WHERE id = ? AND ref_code IS NULL", (code, uid))
            if getattr(cur, "rowcount", 0) == 1:
                return code
        except Exception:   # noqa: BLE001 — colisão no índice único: tenta outro código
            pass
        r = conn.execute("SELECT ref_code FROM profiles WHERE id = ?", (uid,)).fetchone()
        if r and r["ref_code"]:
            return r["ref_code"]
    return None


def _processar_indicacao(conn, uid) -> None:
    """Fecha a indicação quando `uid` vira pagante (chamado do webhook). Roda UMA vez só por
    indicado (claim atômica em ref_creditado) e credita o padrinho a cada REF_INDICADOS_POR_MES
    convertidos (idempotente por ref_meses_dados). Best-effort: qualquer falha aqui NUNCA
    pode derrubar a confirmação do pagamento — por isso todo o corpo vai num try/except amplo."""
    if not uid:
        return
    try:
        r = conn.execute(
            "SELECT indicado_por, ref_creditado FROM profiles WHERE id = ?", (uid,)).fetchone()
        if not r:
            return
        codigo_padrinho = (r["indicado_por"] or "").strip()
        if r["ref_creditado"] or not codigo_padrinho:
            return
        # claim atômica: só quem transformar false->true segue (evita crédito dobrado em
        # reenvio de webhook ou concorrência). No Postgres o WHERE é reavaliado após o lock.
        cur = conn.execute(
            "UPDATE profiles SET ref_creditado = true WHERE id = ? AND ref_creditado = false",
            (uid,))
        if getattr(cur, "rowcount", 0) != 1:
            return
        # padrinho existe? (e não é auto-indicação)
        pad = conn.execute(
            "SELECT id FROM profiles WHERE ref_code = ?", (codigo_padrinho,)).fetchone()
        if not pad or pad["id"] == uid:
            return   # código inválido/próprio: claim gasta, mas não credita ninguém
        padrinho_id = pad["id"]
        # (o indicado já foi recompensado no cadastro com o trial dobrado — ver trial_iniciar;
        #  aqui só marcamos a conversão e creditamos o padrinho)
        # credita o PADRINHO: conta convertidos e dá 1 mês a cada REF_INDICADOS_POR_MES
        convertidos = conn.execute(
            "SELECT COUNT(*) AS n FROM profiles WHERE indicado_por = ? AND ref_creditado = true",
            (codigo_padrinho,)).fetchone()["n"]
        meses_merecidos = convertidos // REF_INDICADOS_POR_MES
        row = conn.execute(
            "SELECT ref_meses_dados FROM profiles WHERE id = ?", (padrinho_id,)).fetchone()
        ja_dados = (row["ref_meses_dados"] or 0) if row else 0
        if meses_merecidos > ja_dados:
            novos = meses_merecidos - ja_dados
            _dar_dias(conn, padrinho_id, REF_DIAS_MES * novos)
            conn.execute("UPDATE profiles SET ref_meses_dados = ? WHERE id = ?",
                         (meses_merecidos, padrinho_id))
            try:
                _enviar_push(conn, padrinho_id, "🎉 Você ganhou um mês grátis!",
                             "Suas indicações renderam — aproveite o PouPou+ por nossa conta.", "/")
            except Exception:   # noqa: BLE001
                pass
    except Exception as e:   # noqa: BLE001
        logging.getLogger("api").warning("indicacao: falha ao processar uid=%s: %s", uid, e)


# Garante schema/migrações (tabelas links, lista) ao subir a API. RESILIENTE: se o banco
# não responder no boot (host novo/rede lenta), NÃO derruba a API — senão o uvicorn nem
# começa a servir e o proxy do host devolve 502 em TUDO (até no /, que nem toca o banco).
# Cada request reconecta por conta própria; o schema no Postgres já existe (init é no-op lá).
try:
    _boot = database.conectar(os.getenv("DB_PATH", "dados/precos.db"))
    database.inicializar(_boot)
    database.garantir_equivalentes(_boot)   # de/para de EANs do mesmo produto + view unificada
    database.garantir_acessos(_boot)        # acessos por dia (painel /gestao)
    _boot.close()
except Exception as _boot_err:  # noqa: BLE001
    logging.getLogger("api").warning("boot: init do banco falhou, seguindo mesmo assim: %s", _boot_err)

_ROTULO = {"kg": "R$/kg", "l": "R$/L", "un": "R$/un"}
_SPARK_N = 12  # nº máximo de pontos na sparkline


def _conn():
    """Conexão por request via backend dual (Postgres se DATABASE_URL; senão SQLite).
    O `unaccent` é função custom no SQLite; no Postgres é a extensão nativa (via search_path)."""
    c = database.conectar(os.getenv("DB_PATH", "dados/precos.db"))
    if hasattr(c, "create_function"):   # só o sqlite3.Connection tem isso
        c.create_function("unaccent", 1, _unaccent, deterministic=True)
    return c


def _sparkline(conn: sqlite3.Connection, ean: str, mercado_id: int) -> list[float]:
    linhas = conn.execute(
        "SELECT preco FROM precos WHERE ean=? AND mercado_id=? ORDER BY coletado_em",
        (ean, mercado_id),
    ).fetchall()
    pts = [round(r["preco"], 2) for r in linhas]
    return pts[-_SPARK_N:]


def _tendencia(pts: list[float]) -> str:
    """'caindo' | 'subindo' | 'estavel' — compara o último ponto com o anterior."""
    if len(pts) < 2:
        return "estavel"
    if pts[-1] < pts[-2]:
        return "caindo"
    if pts[-1] > pts[-2]:
        return "subindo"
    return "estavel"


def _ref_historico(conn: sqlite3.Connection, ean: str, mercado_id: int, atual: float) -> float | None:
    """Preço típico recente (média dos últimos ~30 snapshots, excluindo o atual) para
    dar a sensação de 'quanto caiu vs. os dias anteriores'. None se histórico curto."""
    rows = conn.execute(
        "SELECT preco FROM precos WHERE ean=? AND mercado_id=? ORDER BY coletado_em DESC LIMIT 30",
        (ean, mercado_id),
    ).fetchall()
    precos = [r["preco"] for r in rows]
    if len(precos) < 4:
        return None
    anteriores = precos[1:]  # exclui o mais recente (que é ~o preço atual)
    return sum(anteriores) / len(anteriores)


# colunas do ranking (reusadas na versão por-1-ean e na versão EM LOTE)
_RANKING_COLS = """m.id AS mercado_id, m.nome AS mercado, m.slug AS mercado_slug,
               m.cor_badge, m.logo_url, m.loja_ref,
               pa.ean AS ean, pa.preco, pa.preco_por_unidade, pr.unidade, pa.origem, pa.coletado_em,
               pa.so_no_site, pa.preco_de, pa.desconto_pct, pa.oferta_tipo,
               lk.url AS url"""


def _linha_ranking(r, spark: list | None = None) -> dict:
    """Monta uma linha do ranking a partir da row do banco (mesma forma nas duas versões)."""
    spark = spark or []
    return {
        "mercado_id": r["mercado_id"],
        "mercado": r["mercado"],
        "mercado_slug": r["mercado_slug"],
        "cor_badge": r["cor_badge"],
        "logo_url": r["logo_url"],
        "loja_ref": r["loja_ref"],
        "preco": round(r["preco"], 2),
        "preco_por_unidade": r["preco_por_unidade"],
        "rotulo_unidade": _ROTULO.get(r["unidade"]),
        "origem": r["origem"],
        "coletado_em": r["coletado_em"],
        "url": r["url"],
        "so_no_site": bool(r["so_no_site"]),
        "preco_de": r["preco_de"],
        "desconto_pct": r["desconto_pct"],
        "oferta_tipo": r["oferta_tipo"] if "oferta_tipo" in r.keys() else None,
        "sparkline": spark,
        "tendencia": _tendencia(spark),
    }


def _ranking_enriquecido(conn: sqlite3.Connection, ean: str, com_sparkline: bool = True) -> list[dict]:
    # Lê da tabela quente precos_atual (1 linha por ean+mercado) — rápido mesmo com
    # milhões de snapshots no histórico. Sparkline (que varre o histórico) só sob demanda.
    # mesmo produto com EAN diferente em outro mercado entra junto (precos_atual_unif); um
    # EAN 'antigo' salvo numa lista é traduzido pro principal antes de buscar
    principal = database.principais(conn, [ean])[ean]
    linhas = conn.execute(
        f"""
        SELECT {_RANKING_COLS}, pa.ean_original
        FROM precos_atual_unif pa
        JOIN mercados m  ON m.id = pa.mercado_id
        JOIN produtos pr ON pr.ean = pa.ean
        LEFT JOIN links lk ON lk.ean = pa.ean_original AND lk.mercado_id = pa.mercado_id
        WHERE pa.ean = ? AND m.ativo = 1
        ORDER BY pa.preco ASC
        """,
        (principal,),
    ).fetchall()
    # sparkline pelo EAN que AQUELE mercado usa (o histórico fica no código original)
    return [
        _linha_ranking(r, _sparkline(conn, r["ean_original"], r["mercado_id"]) if com_sparkline else [])
        for r in linhas
    ]


def _rankings_lote(conn, eans: list[str]) -> dict[str, list[dict]]:
    """Ranking de VÁRIOS eans numa query só (evita o N+1 das listagens). Sem sparkline.
    Devolve {ean: [linhas ordenadas por preço]}. Uma query em vez de uma por produto —
    é o que derruba /categoria de ~60 idas ao banco pra 1."""
    if not eans:
        return {}
    # EANs equivalentes (mesmo produto, código diferente) viram o principal; o resultado
    # volta indexado pelo EAN que foi pedido
    prin = database.principais(conn, eans)
    alvo = sorted(set(prin.values()))
    ph = ",".join(["?"] * len(alvo))
    linhas = conn.execute(
        f"""
        SELECT {_RANKING_COLS}
        FROM precos_atual_unif pa
        JOIN mercados m  ON m.id = pa.mercado_id
        JOIN produtos pr ON pr.ean = pa.ean
        LEFT JOIN links lk ON lk.ean = pa.ean_original AND lk.mercado_id = pa.mercado_id
        WHERE pa.ean IN ({ph}) AND m.ativo = 1
        ORDER BY pa.ean, pa.preco ASC
        """,
        tuple(alvo),
    ).fetchall()
    por_principal: dict[str, list[dict]] = {}
    for r in linhas:
        por_principal.setdefault(r["ean"], []).append(_linha_ranking(r))
    return {e: por_principal[p] for e, p in prin.items() if p in por_principal}


def _produto_dict(conn: sqlite3.Connection, prod: sqlite3.Row, com_sparkline: bool = True,
                  ranking: list[dict] | None = None) -> dict:
    # ranking pré-calculado (via _rankings_lote) evita o N+1 nas listagens; senão busca só deste ean.
    if ranking is None:
        ranking = _ranking_enriquecido(conn, prod["ean"], com_sparkline=com_sparkline)
    return {
        "ean": prod["ean"],
        "nome": prod["nome_canonico"],
        "marca": prod["marca"],
        "categoria": prod["categoria"],
        "unidade": prod["unidade"],
        "qtd_embalagem": prod["qtd_embalagem"],
        "imagem_url": prod["imagem_url"],
        "n_mercados": len(ranking),
        "menor_preco": ranking[0]["preco"] if ranking else None,
        "ranking": ranking,
    }


def _produtos_lote(conn, prods) -> list[dict]:
    """Monta uma lista de produtos com UMA query de ranking pro conjunto todo (sem N+1).
    Substitui o padrão antigo de 1 query por produto — é o que derruba /categoria de ~60
    idas ao banco pra ~2."""
    rk = _rankings_lote(conn, [p["ean"] for p in prods])
    return [_produto_dict(conn, p, com_sparkline=False, ranking=rk.get(p["ean"], [])) for p in prods]


# Cesta básica: staples do dia a dia. Pra cada termo, pega o produto presente no MAIOR número
# de mercados (comparável em quase todas) e mais barato — pronto pra montar a feira num toque.
CESTA_BASICA_TERMOS = [
    ("Arroz", "arroz"), ("Feijão", "feijao"), ("Óleo de soja", "oleo de soja"),
    ("Açúcar", "acucar refinado"), ("Café", "cafe"), ("Leite", "leite integral"),
    ("Macarrão", "macarrao"), ("Farinha de trigo", "farinha de trigo"),
    ("Sal", "sal refinado"), ("Margarina", "margarina"),
    ("Extrato de tomate", "extrato de tomate"), ("Biscoito", "biscoito"),
    ("Sabão em pó", "sabao em po"), ("Detergente", "detergente"),
    ("Papel higiênico", "papel higienico"),
]


@app.get("/cesta-basica")
def cesta_basica_itens():
    """Itens de cesta básica: pra cada um, o produto presente em MAIS mercados (comparável em
    quase todas) e mais barato. Uma feira pronta com o essencial do dia a dia."""
    conn = _conn()
    try:
        encontrados, vistos = [], set()
        for rotulo, termo in CESTA_BASICA_TERMOS:
            base = database.normalizar_nome(termo)
            padrao, inicio = f"%{base}%", f"{base}%"
            # prioriza quem COMEÇA com o termo (Arroz... > Macarrão de Arroz), depois quem está
            # em MAIS mercados (comparável em quase todas), depois o mais barato.
            r = conn.execute(
                """SELECT ean, nome_canonico, marca, categoria, unidade, qtd_embalagem,
                          imagem_url, n_mercados
                   FROM produtos
                   WHERE lower(unaccent(nome_canonico)) LIKE ? AND n_mercados > 0
                     AND ean NOT IN (SELECT ean FROM produto_equivalente)   -- só o principal
                   ORDER BY (lower(unaccent(nome_canonico)) LIKE ?) DESC,
                            n_mercados DESC, menor_preco ASC LIMIT 1""",
                (padrao, inicio),
            ).fetchone()
            if r and r["ean"] not in vistos:
                vistos.add(r["ean"])
                encontrados.append((rotulo, r))
        itens = _produtos_lote(conn, [r for _, r in encontrados])  # 1 query de ranking p/ todos
        for (rotulo, _r), d in zip(encontrados, itens):
            d["rotulo_basico"] = rotulo
        return {"itens": itens, "n": len(itens)}
    finally:
        conn.close()


@app.get("/categorias")
def categorias():
    conn = _conn()
    try:
        linhas = conn.execute(
            "SELECT slug, nome, disponivel, ordem FROM categorias ORDER BY ordem"
        ).fetchall()
        return {"categorias": [
            {"slug": r["slug"], "nome": r["nome"],
             "disponivel": bool(r["disponivel"]), "ordem": r["ordem"]}
            for r in linhas
        ]}
    finally:
        conn.close()


@app.get("/categoria/{slug}")
def categoria(slug: str, limite: int = 60):
    conn = _conn()
    try:
        cat = conn.execute(
            "SELECT slug, nome, disponivel FROM categorias WHERE slug = ?", (slug,)
        ).fetchone()
        if not cat:
            raise HTTPException(status_code=404, detail=f"Categoria '{slug}' não existe")
        prods = conn.execute(
            """
            SELECT pr.ean, pr.nome_canonico, pr.marca, pr.categoria, pr.unidade,
                   pr.qtd_embalagem, pr.imagem_url, pr.n_mercados
            FROM produtos pr
            WHERE pr.categoria = ? AND pr.n_mercados > 0
              AND pr.ean NOT IN (SELECT ean FROM produto_equivalente)   -- só o principal
            ORDER BY pr.n_mercados DESC
            LIMIT ?
            """,
            (slug, limite),
        ).fetchall()
        return {
            "categoria": {"slug": cat["slug"], "nome": cat["nome"],
                          "disponivel": bool(cat["disponivel"])},
            "produtos": _produtos_lote(conn, prods),
        }
    finally:
        conn.close()


# seções de frescos (perecíveis sem EAN) — nome de exibição + emoji por slug.
# 'carnes' e 'bandeja' são a MESMA categoria no banco (categoria='carnes'), separadas
# pela forma de venda: 'carnes' = vendida a peso (preço por kg); 'bandeja' = pacote/
# bandeja com gramatura fixa (preço do pacote). A separação é por `unidade` ('kg' vs não).
FRESCOS_META = {
    "carnes":     {"nome": "Carnes (por kg)", "emoji": "🥩"},
    "bandeja":    {"nome": "Bandeja e variados", "emoji": "🥩"},
    "hortifruti": {"nome": "Hortifrúti", "emoji": "🥬"},
    "peixes":     {"nome": "Peixes", "emoji": "🐟"},
    "frios":      {"nome": "Frios", "emoji": "🧀"},
    "padaria":    {"nome": "Padaria", "emoji": "🍞"},
    "ovos":       {"nome": "Ovos", "emoji": "🥚"},
    "outros":     {"nome": "Outros frescos", "emoji": "🧺"},
}
# filtro SQL por "forma de venda" das seções virtuais de carne
_COND_BANDEJA = "(p.unidade IS NULL OR lower(trim(p.unidade)) != 'kg')"
_COND_POR_KG = "lower(trim(p.unidade)) = 'kg'"


def _por_kg(unidade) -> bool:
    """True se o item é vendido A PESO (aba Carnes, não Bandeja)."""
    return bool(unidade) and str(unidade).strip().lower() == "kg"


def _preco_comparavel(preco: float, unidade, nome: str, plataforma=None) -> tuple[float, str | None]:
    """Preço COMPARÁVEL entre lojas (R$/kg quando dá).

    Os coletores JÁ normalizam item vendido a peso para R$/kg (VTEX/SPM via measurementUnit;
    VIP via preco_original/peso). Então unidade 'kg' => o preço já É por kg e NÃO se divide
    de novo (senão vira o bug do 'contra filé R$15/kg'). Só pacote/bandeja (unidade 'un',
    'CXA', 'SCO'...) é preço TOTAL: divide pela gramatura do nome -> R$/kg."""
    if _por_kg(unidade):
        return round(preco, 2), "R$/kg"
    med = normalizar.extrair_medida(nome)
    ppu, rot = normalizar.preco_por_unidade(preco, med)
    if ppu is not None:
        return round(ppu, 2), rot
    u = (unidade or "").lower()
    return round(preco, 2), ("R$/kg" if u in ("kg", "g") else ("R$/un" if u in ("un", "und", "unidade") else None))


def _agrupar_pereciveis(rows, limite: int) -> list[dict]:
    """Agrupa linhas de `pereciveis` pela CHAVE DE FRESCO (nome sem peso/embalagem/
    marca, corte/variedade preservados) — casa o mesmo item entre lojas mesmo quando
    o nome varia ('Abacate 900g' x 'Abacate Bandeja 800g'). 1 entrada por MERCADO
    (menor preço daquele mercado), pra não repetir a mesma loja com vários SKUs."""
    grupos: dict[str, dict] = {}
    for r in rows:
        plat = r["plataforma"] if "plataforma" in r.keys() else None
        comp, rotulo = _preco_comparavel(r["preco"], r["unidade"], r["nome"], plat)
        ch = database.chave_fresco(r["nome"])
        g = grupos.setdefault(ch, {
            "chave": ch, "nome": r["nome"], "unidade": r["unidade"],
            "imagem_url": r["imagem_url"], "coletado_em": None, "por_mercado": {}})
        if r["nome"] and len(r["nome"]) < len(g["nome"]):
            g["nome"] = r["nome"]   # nome mais curto = título mais limpo (menos ruído de peso)
        if r["imagem_url"] and not g["imagem_url"]:
            g["imagem_url"] = r["imagem_url"]
        col = r["coletado_em"] if "coletado_em" in r.keys() else None
        if col and (g["coletado_em"] is None or col > g["coletado_em"]):
            g["coletado_em"] = col
        atual = g["por_mercado"].get(r["mercado_slug"])
        if atual is None or comp < atual["comp"]:
            g["por_mercado"][r["mercado_slug"]] = {
                "mercado": r["mercado"], "mercado_slug": r["mercado_slug"],
                "cor_badge": r["cor_badge"], "preco": r["preco"], "coletado_em": col,
                "comp": round(comp, 2), "rotulo": rotulo, "url": r["url"],
                "so_no_site": bool(r["so_no_site"]) if "so_no_site" in r.keys() else False,
                "preco_de": r["preco_de"] if "preco_de" in r.keys() else None,
                "desconto_pct": r["desconto_pct"] if "desconto_pct" in r.keys() else None,
                "oferta_tipo": r["oferta_tipo"] if "oferta_tipo" in r.keys() else None}
    saida = []
    for g in grupos.values():
        precos = sorted(g.pop("por_mercado").values(), key=lambda x: x["comp"])
        saida.append({**g, "precos": precos, "menor_preco": precos[0]["comp"],
                      "menor_pacote": min(p["preco"] for p in precos),
                      "rotulo": precos[0]["rotulo"], "n_mercados": len(precos)})
    saida.sort(key=lambda x: x["nome"])
    return saida[:limite]


def _buscar_pereciveis(conn: sqlite3.Connection, q: str, limite: int = 30) -> list[dict]:
    """Itens frescos/por peso (sem EAN) que casam com a busca, agrupados por nome
    (casamento APROXIMADO entre mercados). No piloto só há 1 mercado, mas a
    estrutura já compara N lojas pelo mesmo nome normalizado."""
    full = (database.normalizar_nome(q) or "").strip()
    tokens = [t for t in full.split() if t]
    if not tokens:
        return []
    # início de palavra ('arroz' não casa no meio de outra palavra) e só frescos de verdade
    cond = " AND ".join(["(p.nome_norm LIKE ? OR p.nome_norm LIKE ?)"] * len(tokens))
    params = [x for t in tokens for x in (f"{t}%", f"% {t}%")]
    rows = conn.execute(
        f"""
        SELECT p.nome, p.nome_norm, p.preco, p.unidade, p.url, p.imagem_url, p.coletado_em,
               p.so_no_site, p.preco_de, p.desconto_pct, p.oferta_tipo,
               m.nome AS mercado, m.slug AS mercado_slug, m.cor_badge, m.plataforma
        FROM pereciveis p JOIN mercados m ON m.id = p.mercado_id
        WHERE {cond} AND p.categoria <> 'outros'
        ORDER BY p.nome_norm, p.preco
        """,
        params,
    ).fetchall()
    grupos = _agrupar_pereciveis(rows, 500)
    tok0 = f" {tokens[0]} "
    # relevância: começa com a busca > 1º token como palavra inteira > mais lojas
    def _rel(g):
        nn = database.normalizar_nome(g["nome"]) or ""
        return (nn.startswith(full), tok0 in f" {nn} ", g["n_mercados"])
    grupos.sort(key=lambda g: _rel(g), reverse=True)
    return grupos[:limite]


@app.get("/frescos/categorias")
def frescos_categorias():
    """Seções de frescos que TÊM itens (pra montar as abas Carnes/Hortifrúti/…)."""
    conn = _conn()
    try:
        cnt = {r["categoria"]: r["n"] for r in conn.execute(
            "SELECT categoria, COUNT(*) n FROM pereciveis GROUP BY categoria")}
        # carne vira duas abas: 'carnes' (vendida por kg) e 'bandeja' (pacote/gramatura)
        cnt["carnes"] = conn.execute(
            f"SELECT COUNT(*) n FROM pereciveis p WHERE p.categoria='carnes' AND {_COND_POR_KG}").fetchone()["n"]
        cnt["bandeja"] = conn.execute(
            f"SELECT COUNT(*) n FROM pereciveis p WHERE p.categoria='carnes' AND {_COND_BANDEJA}").fetchone()["n"]
        out = [{"slug": slug, "nome": m["nome"], "emoji": m["emoji"], "n": cnt[slug]}
               for slug, m in FRESCOS_META.items() if cnt.get(slug)]
        return {"categorias": out}
    finally:
        conn.close()


@app.get("/frescos/{categoria}")
def frescos(categoria: str, limite: int = 200):
    """Itens de uma seção de frescos, agrupados por nome (aproximado entre lojas)."""
    conn = _conn()
    try:
        # 'carnes' e 'bandeja' filtram a MESMA categoria do banco pela forma de venda
        if categoria == "bandeja":
            where, params = f"p.categoria = 'carnes' AND {_COND_BANDEJA}", ()
        elif categoria == "carnes":
            where, params = f"p.categoria = 'carnes' AND {_COND_POR_KG}", ()
        else:
            where, params = "p.categoria = ?", (categoria,)
        rows = conn.execute(
            f"""
            SELECT p.nome, p.nome_norm, p.preco, p.unidade, p.url, p.imagem_url, p.coletado_em,
                   p.so_no_site, p.preco_de, p.desconto_pct, p.oferta_tipo,
                   m.nome AS mercado, m.slug AS mercado_slug, m.cor_badge, m.plataforma
            FROM pereciveis p JOIN mercados m ON m.id = p.mercado_id
            WHERE {where}
            ORDER BY p.nome_norm, p.preco
            """,
            params,
        ).fetchall()
        meta = FRESCOS_META.get(categoria, {"nome": categoria, "emoji": "🧺"})
        return {"categoria": {"slug": categoria, "modo": "bandeja" if categoria == "bandeja" else "kg", **meta},
                "itens": _agrupar_pereciveis(rows, limite)}
    finally:
        conn.close()


@app.get("/ofertas")
def ofertas(limite: int = 120):
    """Itens que estão EM DESCONTO no site (produtos com EAN + frescos), do maior % ao menor."""
    conn = _conn()
    try:
        itens = []
        for r in conn.execute(
            """
            SELECT pr.ean, pr.nome_canonico AS nome, pr.imagem_url,
                   pa.preco, pa.preco_de, pa.desconto_pct, pa.oferta_tipo, pa.so_no_site,
                   m.nome AS mercado, m.slug AS mercado_slug, m.cor_badge, m.plataforma, lk.url AS url
            FROM precos_atual pa
            JOIN produtos pr ON pr.ean = pa.ean
            JOIN mercados m  ON m.id = pa.mercado_id
            LEFT JOIN links lk ON lk.ean = pa.ean AND lk.mercado_id = pa.mercado_id
            WHERE pa.desconto_pct > 0 AND m.ativo = 1
              AND (pa.oferta_tipo IS NULL OR pa.oferta_tipo = 'oferta')
            ORDER BY pa.desconto_pct DESC LIMIT ?
            """,
            (limite,),
        ):
            itens.append({"tipo": "produto", "nome": r["nome"], "unidade": None,
                          "imagem_url": r["imagem_url"], "preco": round(r["preco"], 2),
                          "preco_de": r["preco_de"], "desconto_pct": r["desconto_pct"],
                          "oferta_tipo": r["oferta_tipo"] if "oferta_tipo" in r.keys() else None,
                          "so_no_site": bool(r["so_no_site"]), "mercado": r["mercado"],
                          "mercado_slug": r["mercado_slug"], "cor_badge": r["cor_badge"], "url": r["url"]})
        for r in conn.execute(
            """
            SELECT p.nome, p.preco, p.preco_de, p.desconto_pct, p.oferta_tipo, p.so_no_site,
                   p.imagem_url, p.unidade, p.url,
                   m.nome AS mercado, m.slug AS mercado_slug, m.cor_badge, m.plataforma
            FROM pereciveis p JOIN mercados m ON m.id = p.mercado_id
            WHERE p.desconto_pct > 0
              AND (p.oferta_tipo IS NULL OR p.oferta_tipo = 'oferta')
            ORDER BY p.desconto_pct DESC LIMIT ?
            """,
            (limite,),
        ):
            itens.append({"tipo": "fresco", "nome": r["nome"], "unidade": r["unidade"],
                          "imagem_url": r["imagem_url"], "preco": round(r["preco"], 2),
                          "preco_de": r["preco_de"], "desconto_pct": r["desconto_pct"],
                          "oferta_tipo": r["oferta_tipo"] if "oferta_tipo" in r.keys() else None,
                          "so_no_site": bool(r["so_no_site"]), "mercado": r["mercado"],
                          "mercado_slug": r["mercado_slug"], "cor_badge": r["cor_badge"], "url": r["url"]})
        itens.sort(key=lambda x: -(x["desconto_pct"] or 0))
        return {"itens": itens[:limite], "n": len(itens)}
    finally:
        conn.close()


@app.get("/clube")
def clube(limite: int = 500):
    """Ofertas de CLUBE/FIDELIDADE — o preço que SÓ vale com o programa do mercado (Viva,
    Fidelidade, etc.). Separado das ofertas comuns pra não misturar com o que é pra todos.
    Agrupa por mercado+programa: a pessoa escolhe o mercado cujo cartão ela tem e vê os itens."""
    conn = _conn()
    try:
        itens = []
        for r in conn.execute(
            """
            SELECT pr.nome_canonico AS nome, pr.imagem_url,
                   pa.preco, pa.preco_de, pa.desconto_pct, pa.oferta_tipo,
                   m.nome AS mercado, m.slug AS mercado_slug, m.cor_badge, lk.url AS url
            FROM precos_atual pa
            JOIN produtos pr ON pr.ean = pa.ean
            JOIN mercados m  ON m.id = pa.mercado_id
            LEFT JOIN links lk ON lk.ean = pa.ean AND lk.mercado_id = pa.mercado_id
            WHERE pa.oferta_tipo IS NOT NULL AND pa.oferta_tipo != 'oferta' AND m.ativo = 1
            ORDER BY pa.desconto_pct DESC LIMIT ?
            """,
            (limite,),
        ):
            itens.append({"tipo": "produto", "nome": r["nome"], "unidade": None,
                          "imagem_url": r["imagem_url"], "preco": round(r["preco"], 2),
                          "preco_de": r["preco_de"], "desconto_pct": r["desconto_pct"],
                          "oferta_tipo": r["oferta_tipo"], "mercado": r["mercado"],
                          "mercado_slug": r["mercado_slug"], "cor_badge": r["cor_badge"], "url": r["url"]})
        for r in conn.execute(
            """
            SELECT p.nome, p.preco, p.preco_de, p.desconto_pct, p.oferta_tipo,
                   p.imagem_url, p.unidade, p.url,
                   m.nome AS mercado, m.slug AS mercado_slug, m.cor_badge
            FROM pereciveis p JOIN mercados m ON m.id = p.mercado_id
            WHERE p.oferta_tipo IS NOT NULL AND p.oferta_tipo != 'oferta' AND m.ativo = 1
            ORDER BY p.desconto_pct DESC LIMIT ?
            """,
            (limite,),
        ):
            itens.append({"tipo": "fresco", "nome": r["nome"], "unidade": r["unidade"],
                          "imagem_url": r["imagem_url"], "preco": round(r["preco"], 2),
                          "preco_de": r["preco_de"], "desconto_pct": r["desconto_pct"],
                          "oferta_tipo": r["oferta_tipo"], "mercado": r["mercado"],
                          "mercado_slug": r["mercado_slug"], "cor_badge": r["cor_badge"], "url": r["url"]})
        # agrupa por mercado -> quais programas tem e quantos itens (pros chips de escolha)
        grupos: dict = {}
        for it in itens:
            g = grupos.setdefault(it["mercado_slug"], {
                "mercado_slug": it["mercado_slug"], "mercado": it["mercado"],
                "cor_badge": it["cor_badge"], "programas": set(), "n": 0})
            g["programas"].add(it["oferta_tipo"])
            g["n"] += 1
        mercados = sorted(
            ({"mercado_slug": g["mercado_slug"], "mercado": g["mercado"], "cor_badge": g["cor_badge"],
              "programa": " · ".join(sorted(g["programas"])), "n": g["n"]} for g in grupos.values()),
            key=lambda x: -x["n"])
        itens.sort(key=lambda x: -(x["desconto_pct"] or 0))
        return {"mercados": mercados, "itens": itens, "n": len(itens)}
    finally:
        conn.close()


@app.get("/buscar")
def buscar(q: str, tipo: str | None = None):
    q = (q or "").strip()
    if len(q) < 2:
        return {"q": q, "resultados": [], "pereciveis": [], "tipos": [], "frescos_primeiro": False}
    conn = _conn()
    try:
        # relevância + botões de tipo ('leite' -> condensado, longa vida, em pó...); `tipo` filtra
        achados, tipos = buscar_com_tipos(conn, q, limite=40, tipo=tipo or None)
        # busca os produtos completos + rankings EM LOTE (sem N+1: era 1 query de produto
        # + 1 de ranking por resultado ~= 80 idas; vira ~2).
        eans = [a["ean"] for a in achados]
        resultados = []
        if eans:
            ph = ",".join(["?"] * len(eans))
            prod_rows = conn.execute(
                "SELECT ean, nome_canonico, marca, categoria, unidade, qtd_embalagem, imagem_url "
                f"FROM produtos WHERE ean IN ({ph})", tuple(eans)
            ).fetchall()
            por_ean = {p["ean"]: p for p in prod_rows}
            rk = _rankings_lote(conn, eans)
            resultados = [
                _produto_dict(conn, por_ean[e], com_sparkline=False, ranking=rk.get(e, []))
                for e in eans if e in por_ean  # preserva a ordem de relevância
            ]
        pereciveis = [] if tipo else _buscar_pereciveis(conn, q)
        # frescos vão na frente só quando a busca É de fresco ('picanha', 'banana'): algum
        # grupo começa com o termo. Em 'arroz'/'leite' os embalados vêm primeiro.
        base = database.normalizar_nome(q) or ""
        frescos_primeiro = any((database.normalizar_nome(g["nome"]) or "").startswith(base)
                               for g in pereciveis)
        return {"q": q, "resultados": resultados, "pereciveis": pereciveis,
                "tipos": tipos, "frescos_primeiro": frescos_primeiro}
    finally:
        conn.close()


@app.get("/produto/{ean}")
def produto(ean: str):
    conn = _conn()
    try:
        prod = conn.execute(
            "SELECT ean, nome_canonico, marca, categoria, unidade, qtd_embalagem, imagem_url "
            "FROM produtos WHERE ean = ?", (ean,)
        ).fetchone()
        if not prod:
            raise HTTPException(status_code=404, detail=f"EAN {ean} não encontrado")
        return _produto_dict(conn, prod)
    finally:
        conn.close()


@app.get("/historico/{ean}")
def historico(ean: str):
    conn = _conn()
    try:
        prod = conn.execute(
            "SELECT nome_canonico FROM produtos WHERE ean = ?", (ean,)
        ).fetchone()
        if not prod:
            raise HTTPException(status_code=404, detail=f"EAN {ean} não encontrado")
        # o histórico de cada mercado fica no EAN que ELE usa: junta todos os códigos do produto
        principal = database.principais(conn, [ean])[ean]
        grupo = [principal] + [r["ean"] for r in conn.execute(
            "SELECT ean FROM produto_equivalente WHERE ean_principal = ?", (principal,)).fetchall()]
        ph = ",".join("?" * len(grupo))
        linhas = conn.execute(
            f"""
            SELECT m.nome AS mercado, m.cor_badge, p.preco, p.preco_por_unidade, p.coletado_em
            FROM precos p JOIN mercados m ON m.id = p.mercado_id
            WHERE p.ean IN ({ph})
            ORDER BY m.nome, p.coletado_em
            """,
            tuple(grupo),
        ).fetchall()
        series: dict[str, dict] = {}
        for r in linhas:
            s = series.setdefault(r["mercado"], {"mercado": r["mercado"],
                                                 "cor_badge": r["cor_badge"], "pontos": []})
            s["pontos"].append({"preco": round(r["preco"], 2), "coletado_em": r["coletado_em"]})
        # Âncora: como o histórico só guarda MUDANÇAS (dedup), junta o preço ATUAL como
        # último ponto — assim um preço estável ainda desenha uma linha até hoje.
        for r in conn.execute(
            """SELECT m.nome AS mercado, m.cor_badge, pa.preco, pa.coletado_em
               FROM precos_atual_unif pa JOIN mercados m ON m.id = pa.mercado_id WHERE pa.ean = ?""",
            (principal,),
        ):
            s = series.setdefault(r["mercado"], {"mercado": r["mercado"],
                                                 "cor_badge": r["cor_badge"], "pontos": []})
            if not s["pontos"] or s["pontos"][-1]["coletado_em"] != r["coletado_em"]:
                s["pontos"].append({"preco": round(r["preco"], 2), "coletado_em": r["coletado_em"]})
        return {"ean": ean, "nome": prod["nome_canonico"], "origem": "online",
                "series": list(series.values())}
    finally:
        conn.close()


@app.get("/historico-fresco/{chave}")
def historico_fresco(chave: str):
    """Séries de preço (por mercado) de um item FRESCO, casado pela chave de agrupamento.
    Mesma estrutura do /historico dos EAN — reusa o mesmo gráfico no front. Por mercado,
    segue o item MAIS BARATO (alinha com o ranking)."""
    conn = _conn()
    try:
        atuais = conn.execute(
            """SELECT p.mercado_id, p.cod_interno, p.nome, p.preco, p.coletado_em,
                      m.nome AS mercado, m.cor_badge
               FROM pereciveis p JOIN mercados m ON m.id = p.mercado_id WHERE m.ativo = 1"""
        ).fetchall()
        por_mercado: dict[str, dict] = {}
        for r in atuais:
            if database.chave_fresco(r["nome"]) != chave:
                continue
            cur = por_mercado.get(r["mercado"])
            if cur is None or r["preco"] < cur["preco"]:
                por_mercado[r["mercado"]] = {
                    "mercado_id": r["mercado_id"], "cod_interno": r["cod_interno"],
                    "cor_badge": r["cor_badge"], "preco": r["preco"], "coletado_em": r["coletado_em"]}
        series = []
        for merc, v in por_mercado.items():
            pontos = [{"preco": round(h["preco"], 2), "coletado_em": h["coletado_em"]}
                      for h in conn.execute(
                          "SELECT preco, coletado_em FROM pereciveis_hist "
                          "WHERE mercado_id = ? AND cod_interno = ? ORDER BY coletado_em",
                          (v["mercado_id"], v["cod_interno"]))]
            # âncora: o histórico só guarda MUDANÇAS; junta o preço ATUAL como último ponto
            if not pontos or pontos[-1]["coletado_em"] != v["coletado_em"]:
                pontos.append({"preco": round(v["preco"], 2), "coletado_em": v["coletado_em"]})
            series.append({"mercado": merc, "cor_badge": v["cor_badge"], "pontos": pontos})
        return {"chave": chave, "series": series}
    finally:
        conn.close()


FRESCO_PREFIX = "f:"  # chave sintética de item fresco na lista (não tem EAN)


def _eh_fresco_key(ean) -> bool:
    return isinstance(ean, str) and ean.startswith(FRESCO_PREFIX)


def _fresco_chave(ean: str) -> str:
    return ean[len(FRESCO_PREFIX):]


def _ranking_fresco(conn: sqlite3.Connection, chave: str) -> list[dict]:
    """Ranking (menor preço por mercado, em R$/kg quando dá) de um item FRESCO,
    casado pela chave de agrupamento. Espelha _ranking_enriquecido, mas p/ perecíveis."""
    rows = conn.execute(
        """SELECT p.nome, p.preco, p.unidade, p.url,
                  m.id AS mercado_id, m.slug AS mercado_slug, m.nome AS mercado, m.cor_badge, m.plataforma
           FROM pereciveis p JOIN mercados m ON m.id = p.mercado_id WHERE m.ativo = 1"""
    ).fetchall()
    por_mercado: dict[str, dict] = {}
    for r in rows:
        if database.chave_fresco(r["nome"]) != chave:
            continue
        comp, rotulo = _preco_comparavel(r["preco"], r["unidade"], r["nome"],
                                         r["plataforma"] if "plataforma" in r.keys() else None)
        cur = por_mercado.get(r["mercado_slug"])
        if cur is None or comp < cur["preco"]:
            por_mercado[r["mercado_slug"]] = {
                "mercado_id": r["mercado_id"], "mercado_slug": r["mercado_slug"],
                "mercado": r["mercado"], "cor_badge": r["cor_badge"],
                "preco": round(comp, 2), "rotulo": rotulo, "url": r["url"]}
    return sorted(por_mercado.values(), key=lambda x: x["preco"])


def _item_lista(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    if _eh_fresco_key(row["ean"]):
        chave = _fresco_chave(row["ean"])
        ranking = _ranking_fresco(conn, chave)
        melhor = ranking[0] if ranking else None
        alvo = row["preco_alvo"]
        return {
            "ean": row["ean"], "tipo": "fresco",
            "nome": (row["nome"] if "nome" in row.keys() else None) or chave,
            "marca": None, "imagem_url": None,
            "rotulo": melhor["rotulo"] if melhor else None,
            "quantidade": row["quantidade"] if "quantidade" in row.keys() else 1,
            "comprado": bool(row["comprado"]) if "comprado" in row.keys() else False,
            "preco_alvo": alvo, "criado_em": row["criado_em"], "melhor": melhor,
            "disparado": bool(melhor and alvo is not None and melhor["preco"] < alvo),
        }
    return _item_lista_produto(conn, row)


def _item_lista_produto(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    prod = conn.execute(
        "SELECT ean, nome_canonico, marca, imagem_url FROM produtos WHERE ean = ?", (row["ean"],)
    ).fetchone()
    ranking = _ranking_enriquecido(conn, row["ean"], com_sparkline=False)
    melhor = ranking[0] if ranking else None
    alvo = row["preco_alvo"]
    disparado = bool(melhor and alvo is not None and melhor["preco"] < alvo)
    return {
        "ean": row["ean"], "tipo": "produto",
        "nome": prod["nome_canonico"] if prod else row["ean"],
        "marca": prod["marca"] if prod else None,
        "imagem_url": prod["imagem_url"] if prod else None,
        "quantidade": row["quantidade"] if "quantidade" in row.keys() else 1,
        "comprado": bool(row["comprado"]) if "comprado" in row.keys() else False,
        "preco_alvo": alvo,
        "criado_em": row["criado_em"],
        "melhor": melhor,          # linha de ranking (mercado, preco, cor_badge, url, ...)
        "disparado": disparado,    # menor preço já está abaixo do alvo
    }


@app.get("/lista")
def lista_get():
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT ean, nome, quantidade, preco_alvo, comprado, criado_em FROM lista ORDER BY criado_em DESC"
        ).fetchall()
        itens = [_item_lista(conn, r) for r in rows]
        return {"itens": itens, "n_alertas": sum(1 for i in itens if i["disparado"])}
    finally:
        conn.close()


@app.post("/lista")
def _lista_add_fresco(payload: dict) -> dict:
    """Adiciona/atualiza um item FRESCO na lista (chave sintética 'f:<chave>')."""
    chave = str(payload.get("fresco") or "").strip()
    if not chave:  # também aceita ean sintético 'f:<chave>' (updates de qtd/alvo/comprado)
        e = str(payload.get("ean") or "").strip()
        chave = _fresco_chave(e) if _eh_fresco_key(e) else ""
    if not chave:
        raise HTTPException(status_code=400, detail="fresco (chave) obrigatório")
    key = FRESCO_PREFIX + chave
    conn = _conn()
    try:
        rk = _ranking_fresco(conn, chave)
        if not rk:
            raise HTTPException(status_code=404, detail=f"Fresco '{chave}' não encontrado")
        nome = str(payload.get("nome") or chave).strip()[:120]
        atual = conn.execute(
            "SELECT quantidade, preco_alvo, comprado, nome FROM lista WHERE ean = ?", (key,)
        ).fetchone()
        qtd = payload.get("quantidade")
        qtd = max(1, int(float(qtd))) if qtd is not None else (int(atual["quantidade"]) if atual else 1)
        if "preco_alvo" in payload:
            alvo = float(payload["preco_alvo"]) if payload["preco_alvo"] is not None else None
        elif atual:
            alvo = atual["preco_alvo"]
        else:
            alvo = rk[0]["preco"]
        if "comprado" in payload:
            comprado = 1 if payload["comprado"] else 0
        else:
            comprado = atual["comprado"] if atual else 0
        if atual and payload.get("nome") is None:
            nome = atual["nome"] or nome
        conn.execute(
            """INSERT INTO lista (ean, nome, quantidade, preco_alvo, comprado, criado_em)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(ean) DO UPDATE SET nome = excluded.nome, quantidade = excluded.quantidade,
                   preco_alvo = excluded.preco_alvo, comprado = excluded.comprado""",
            (key, nome, qtd, alvo, comprado, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT ean, nome, quantidade, preco_alvo, comprado, criado_em FROM lista WHERE ean = ?", (key,)
        ).fetchone()
        return _item_lista(conn, row)
    finally:
        conn.close()


def lista_add(payload: dict = Body(...)):
    """Adiciona/atualiza um item da lista. Mantém valores existentes se não vierem no payload.
    Aceita produto por EAN (`ean`) OU item fresco por chave de agrupamento (`fresco` + `nome`)."""
    if payload.get("fresco") or _eh_fresco_key(str(payload.get("ean") or "").strip()):
        return _lista_add_fresco(payload)
    ean = str(payload.get("ean") or "").strip()
    conn = _conn()
    try:
        prod = conn.execute("SELECT ean FROM produtos WHERE ean = ?", (ean,)).fetchone()
        if not prod:
            raise HTTPException(status_code=404, detail=f"EAN {ean} não encontrado")
        atual = conn.execute(
            "SELECT quantidade, preco_alvo, comprado FROM lista WHERE ean = ?", (ean,)
        ).fetchone()

        qtd = payload.get("quantidade")
        if qtd is None:
            qtd = atual["quantidade"] if atual else 1
        qtd = max(1, int(float(qtd)))

        if "preco_alvo" in payload:
            alvo = float(payload["preco_alvo"]) if payload["preco_alvo"] is not None else None
        elif atual:
            alvo = atual["preco_alvo"]
        else:  # novo item sem alvo -> menor preço atual (alerta quando cair abaixo)
            rk = _ranking_enriquecido(conn, ean)
            alvo = rk[0]["preco"] if rk else None

        if "comprado" in payload:
            comprado = 1 if payload["comprado"] else 0
        else:
            comprado = atual["comprado"] if atual else 0

        conn.execute(
            """INSERT INTO lista (ean, quantidade, preco_alvo, comprado, criado_em) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(ean) DO UPDATE SET quantidade = excluded.quantidade,
                                              preco_alvo = excluded.preco_alvo,
                                              comprado   = excluded.comprado""",
            (ean, qtd, alvo, comprado, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT ean, quantidade, preco_alvo, criado_em FROM lista WHERE ean = ?", (ean,)
        ).fetchone()
        return _item_lista(conn, row)
    finally:
        conn.close()


@app.delete("/lista/{ean}")
def lista_del(ean: str):
    conn = _conn()
    try:
        conn.execute("DELETE FROM lista WHERE ean = ?", (ean,))
        conn.commit()
        return {"ok": True, "ean": ean}
    finally:
        conn.close()


@app.get("/alertas")
def alertas():
    conn = _conn()
    try:
        rows = conn.execute("SELECT ean, nome, quantidade, preco_alvo, comprado, criado_em FROM lista").fetchall()
        itens = [_item_lista(conn, r) for r in rows]
        return {"alertas": [i for i in itens if i["disparado"]]}
    finally:
        conn.close()


_RM_CAMPINAS = {  # municípios da Região Metropolitana de Campinas (compara com a cidade do ViaCEP)
    "campinas", "hortolandia", "sumare", "valinhos", "vinhedo", "paulinia", "indaiatuba",
    "americana", "santa barbara d'oeste", "nova odessa", "cosmopolis", "artur nogueira",
    "jaguariuna", "pedreira", "monte mor", "engenheiro coelho", "holambra",
    "santo antonio de posse", "morungaba",
}


def _sem_acento(s: str | None) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c)).lower().strip()


@app.get("/cep/{cep}")
def cep_lookup(cep: str):
    """Consulta o CEP (ViaCEP) e sugere os mercados da REGIÃO da pessoa. Serve pra
    personalizar o cadastro ('digite o CEP → já marco seus mercados') e confirmar
    cobertura. Não guarda o CEP; é só uma consulta pública de localização."""
    digs = "".join(ch for ch in (cep or "") if ch.isdigit())
    if len(digs) != 8:
        return {"ok": False, "erro": "CEP inválido (precisa de 8 números)."}
    try:
        r = httpx.get(f"https://viacep.com.br/ws/{digs}/json/", timeout=8.0,
                      headers={"User-Agent": "PouPou/1.0"})
        j = r.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "erro": "Não consegui consultar o CEP agora."}
    if not isinstance(j, dict) or j.get("erro"):
        return {"ok": False, "erro": "CEP não encontrado."}
    cidade, uf, bairro = j.get("localidade") or "", j.get("uf") or "", j.get("bairro") or ""
    cnorm = _sem_acento(cidade)
    conn = _conn()
    try:
        mercados = conn.execute("SELECT slug, cidade FROM mercados WHERE ativo = 1").fetchall()
    finally:
        conn.close()
    if cnorm in _RM_CAMPINAS:
        sugeridos = [m["slug"] for m in mercados if "campinas" in _sem_acento(m["cidade"])]
        regiao = "Região de Campinas"
    elif cnorm == "sao paulo":
        sugeridos = [m["slug"] for m in mercados if "sao paulo" in _sem_acento(m["cidade"])]
        regiao = "São Paulo (capital)"
    else:
        sugeridos, regiao = [], None
    return {"ok": True, "cidade": cidade, "uf": uf, "bairro": bairro,
            "regiao": regiao, "coberto": bool(sugeridos), "mercados": sugeridos}


@app.get("/cesta")
def cesta(authorization: str | None = Header(None), mercados: str | None = None):
    """Compara o custo TOTAL da lista (do usuário logado) em cada mercado, com cobertura e a
    'melhor divisão' item a item. Grátis recebe só os NÚMEROS; o "onde" é trancado no servidor.

    `mercados` (slugs separados por vírgula) restringe TUDO a esses mercados — é o modo
    'Meus mercados': ranking, menor preço por item, divisão e economia passam a considerar
    só eles. Sem o parâmetro, compara todos (modo 'Todos')."""
    conn = _conn()
    try:
        uid = _uid_do_header(authorization)
        premium = _premium(conn, uid)
        filtro_slugs = {s.strip() for s in (mercados or "").split(",") if s.strip()} or None
        itens_lista = conn.execute(
            "SELECT ean, nome, quantidade, preco_alvo, comprado FROM lista "
            "WHERE user_id = ? ORDER BY criado_em DESC", (uid,)
        ).fetchall() if uid else []
        mercados = conn.execute(
            "SELECT id, slug, nome, cor_badge FROM mercados WHERE ativo = 1"
        ).fetchall()
        if filtro_slugs:   # modo "Meus mercados": só os favoritos do usuário entram
            mercados = [m for m in mercados if m["slug"] in filtro_slugs]
        n_itens = len(itens_lista)
        n_alertas = 0
        divisao: dict[str, dict] = {}  # estratégia "dividido": slug -> {itens, subtotal}

        # acumuladores por mercado
        acc = {m["id"]: {"slug": m["slug"], "nome": m["nome"], "cor_badge": m["cor_badge"],
                         "total": 0.0, "cobertura": 0, "faltantes": [], "completar_custo": 0.0}
               for m in mercados}
        itens_out = []
        mix_total = 0.0
        mix_mercados = set()
        economia_mercados = 0.0   # vs. comprar cada item no mercado mais caro que o tem
        economia_dias = 0.0       # vs. o preço típico recente (queda de preço nos dias)

        for it in itens_lista:
            ean, qtd = it["ean"], (it["quantidade"] or 1)
            if _eh_fresco_key(ean):
                prod = None
                ranking = _ranking_fresco(conn, _fresco_chave(ean))
                nome_prod = (it["nome"] if "nome" in it.keys() else None) or _fresco_chave(ean)
                marca_prod = img_prod = None
            else:
                prod = conn.execute(
                    "SELECT nome_canonico, marca, imagem_url FROM produtos WHERE ean = ?", (ean,)
                ).fetchone()
                ranking = _ranking_enriquecido(conn, ean, com_sparkline=False)  # último preço por mercado
                nome_prod = prod["nome_canonico"] if prod else ean
                marca_prod = prod["marca"] if prod else None
                img_prod = prod["imagem_url"] if prod else None
            if filtro_slugs:   # modo "Meus mercados": só os favoritos entram na comparação do item
                ranking = [r for r in ranking if r["mercado_slug"] in filtro_slugs]
            por_mercado = {r["mercado_slug"]: r for r in ranking}
            # total por mercado
            slugs_com = set()
            for r in ranking:
                # mapeia slug -> id
                mid = next((m["id"] for m in mercados if m["slug"] == r["mercado_slug"]), None)
                if mid is None:
                    continue
                acc[mid]["total"] += r["preco"] * qtd
                acc[mid]["cobertura"] += 1
                slugs_com.add(r["mercado_slug"])
            for m in mercados:
                if m["slug"] not in slugs_com:
                    acc[m["id"]]["faltantes"].append({"ean": ean, "nome": nome_prod})
            # melhor (estratégia "dividido": cada item no mercado mais barato)
            melhor = ranking[0] if ranking else None
            if melhor:
                mix_total += melhor["preco"] * qtd
                mix_mercados.add(melhor["mercado_slug"])
                dv = divisao.setdefault(melhor["mercado_slug"], {
                    "slug": melhor["mercado_slug"], "nome": melhor["mercado"],
                    "cor_badge": melhor["cor_badge"], "itens": [], "subtotal": 0.0})
                dv["itens"].append({"ean": ean, "nome": nome_prod, "quantidade": qtd,
                                    "preco": melhor["preco"], "subtotal": round(melhor["preco"] * qtd, 2),
                                    "url": melhor["url"]})
                dv["subtotal"] += melhor["preco"] * qtd
                # COMPARAÇÃO JUSTA: quem NÃO tem esse item "completa" pela ESTIMATIVA do preço
                # MÉDIO entre as lojas que o vendem (a loja não tem o item — é estimativa, não
                # preço real). Usar a MÉDIA (e não o menor) evita que lojas incompletas pareçam
                # baratas demais só por "emprestar" o preço de fundo de poço, deixa a comparação
                # da feira inteira mais fiel e as diferenças entre lojas mais visíveis/reais.
                precos_item = [r["preco"] for r in ranking]
                media_item = sum(precos_item) / len(precos_item)
                custo_falta_i = media_item * qtd
                for m in mercados:
                    if m["slug"] not in slugs_com:
                        acc[m["id"]]["completar_custo"] += custo_falta_i
            # ---- ECONOMIA (sensação de economia) ----
            eco_merc_item = eco_dias_item = 0.0
            ref_hist = None
            if melhor:
                precos_i = [r["preco"] for r in ranking]
                if len(precos_i) >= 2:                       # vs. mercado mais caro que tem o item
                    eco_merc_item = round((max(precos_i) - melhor["preco"]) * qtd, 2)
                    economia_mercados += eco_merc_item
                ref_hist = _ref_historico(conn, ean, melhor["mercado_id"], melhor["preco"])
                if ref_hist and ref_hist > melhor["preco"]:  # preço caiu vs. dias anteriores
                    eco_dias_item = round((ref_hist - melhor["preco"]) * qtd, 2)
                    economia_dias += eco_dias_item

            alvo = it["preco_alvo"]
            disparado = bool(melhor and alvo is not None and melhor["preco"] < alvo)
            if disparado:
                n_alertas += 1
            itens_out.append({
                "ean": ean, "nome": nome_prod,
                "tipo": "fresco" if _eh_fresco_key(ean) else "produto",
                "rotulo": (melhor.get("rotulo") if melhor else None),
                "marca": marca_prod, "imagem_url": img_prod,
                "quantidade": qtd, "preco_alvo": alvo, "disparado": disparado,
                "comprado": bool(it["comprado"]),
                "melhor": melhor,
                "preco_ref_dias": round(ref_hist, 2) if ref_hist else None,
                "economia_mercados_item": eco_merc_item,
                "economia_dias_item": eco_dias_item,
                "precos": [{"slug": r["mercado_slug"], "nome": r["mercado"],
                            "cor_badge": r["cor_badge"], "preco": r["preco"], "url": r["url"],
                            "rotulo": r.get("rotulo")} for r in ranking],
            })

        LIMITE_FALTA = 4  # loja "pra fazer a feira" = falta no MÁXIMO isso; o resto some do ranking
        mercados_out = list(acc.values())
        for m in mercados_out:
            m["total"] = round(m["total"], 2)
            m["completo"] = (m["cobertura"] == n_itens and n_itens > 0)
            m["faltam"] = n_itens - m["cobertura"]
            # total da FEIRA INTEIRA nessa loja: o que ela tem + o que falta pelo menor preço fora
            m["completo_total"] = round(m["total"] + m["completar_custo"], 2)
            m["viavel"] = (m["cobertura"] > 0 and m["faltam"] <= LIMITE_FALTA)  # dá pra fazer a feira aqui
            m.pop("completar_custo", None)
        # ranking JUSTO: lojas viáveis primeiro, ordenadas pelo total da feira inteira (menor 1º)
        mercados_out.sort(key=lambda m: (not m["viavel"], m["completo_total"]))

        viaveis = [m for m in mercados_out if m["viavel"]]
        melhor_mercado = viaveis[0] if viaveis else (mercados_out[0] if mercados_out else None)
        # economia do ranking = feira inteira mais cara vs mais barata entre as lojas viáveis
        economia = None
        if len(viaveis) >= 2:
            economia = round(viaveis[-1]["completo_total"] - viaveis[0]["completo_total"], 2)

        # estratégia "dividido": divisão por mercado (maior subtotal primeiro)
        divisao_lst = sorted(divisao.values(), key=lambda d: -d["subtotal"])
        for d in divisao_lst:
            d["subtotal"] = round(d["subtotal"], 2)
        mix_total = round(mix_total, 2)

        # quanto o "dividido" (otimizado) economiza vs fazer a feira inteira na loja única mais barata
        economia_dividido = None
        if melhor_mercado:
            economia_dividido = round(melhor_mercado["completo_total"] - mix_total, 2)

        return _gate_cesta({
            "n_itens": n_itens,
            "n_alertas": n_alertas,
            "n_comprados": sum(1 for i in itens_out if i["comprado"]),
            "premium": premium,                        # o front sabe se destranca (bate com o servidor)
            "mercados": mercados_out,
            "melhor_mercado": melhor_mercado,          # Opção 1: comprar tudo numa loja só
            "economia_vs_pior": economia,              # loja única mais barata vs mais cara
            "mix": {"total": mix_total, "n_mercados": len(mix_mercados), "divisao": divisao_lst},  # Opção 2
            "economia_dividido_vs_loja": economia_dividido,  # quanto o dividido poupa vs a melhor loja única
            "economia_mercados": round(economia_mercados, 2),  # vs. comprar cada item no mercado mais caro
            "economia_dias": round(economia_dias, 2),          # vs. o preço típico recente (queda nos dias)
            "itens": itens_out,
        }, premium)
    finally:
        conn.close()


@app.get("/mercados")
def mercados_lista():
    """Mercados ativos com logo (quando temos o arquivo em front/logos/{slug}.png)."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT slug, nome, cor_badge FROM mercados WHERE ativo = 1 ORDER BY nome"
        ).fetchall()
        out = []
        for r in rows:
            tem_logo = (LOGOS_DIR / f"{r['slug']}.png").is_file()
            out.append({"slug": r["slug"], "nome": r["nome"], "cor_badge": r["cor_badge"],
                        "logo_url": f"/logos/{r['slug']}.png" if tem_logo else None})
        return {"mercados": out}
    finally:
        conn.close()


@app.get("/logos/{fname}")
def logo(fname: str):
    """Serve a logo de um mercado (arquivo estático simples, sem travessia de path)."""
    p = (LOGOS_DIR / fname).resolve()
    if p.parent != LOGOS_DIR.resolve() or not p.is_file():
        raise HTTPException(status_code=404, detail="logo não encontrada")
    return FileResponse(p, headers={"Cache-Control": "public, max-age=86400"})


# ============================ ADMIN (exige is_admin) ============================
@app.get("/admin/coleta")
def admin_coleta(authorization: str | None = Header(None)):
    """Saúde da coleta por mercado: produtos, % atualizado nas últimas 24h, última coleta, carnes frescas."""
    conn = _conn()
    try:
        if not _admin_uid(conn, authorization):
            raise HTTPException(status_code=403, detail="Acesso restrito")
        c24, c7d = _cutoff_horas(24), _cutoff_horas(24 * 7)
        # % sobre o catálogo VIVO (visto nos últimos 7 dias): produto que a loja tirou do
        # site nunca mais é revisto e travava a saúde em ~80-90% mesmo com coleta perfeita.
        rows = conn.execute(
            """SELECT m.slug, m.nome, m.plataforma, m.ativo,
                      COUNT(pa.ean) AS n_produtos,
                      SUM(CASE WHEN pa.coletado_em >= ? THEN 1 ELSE 0 END) AS frescos_24h,
                      SUM(CASE WHEN pa.coletado_em >= ? THEN 1 ELSE 0 END) AS vistos_7d,
                      MAX(pa.coletado_em) AS ultima
               FROM mercados m LEFT JOIN precos_atual pa ON pa.mercado_id = m.id
               GROUP BY m.id, m.slug, m.nome, m.plataforma, m.ativo""",
            (c24, c7d),
        ).fetchall()
        carnes = {r["slug"]: (r["n"] or 0) for r in conn.execute(
            """SELECT m.slug, SUM(CASE WHEN p.coletado_em >= ? THEN 1 ELSE 0 END) AS n
               FROM mercados m LEFT JOIN pereciveis p ON p.mercado_id = m.id AND p.categoria = 'carnes'
               GROUP BY m.slug""", (c24,))}
        out = []
        for r in rows:
            n, f, v = (r["n_produtos"] or 0), (r["frescos_24h"] or 0), (r["vistos_7d"] or 0)
            out.append({"slug": r["slug"], "nome": r["nome"], "plataforma": r["plataforma"],
                        "ativo": bool(r["ativo"]), "n_produtos": n, "n_no_site": v, "n_fora_site": n - v,
                        "pct_24h": round(100 * f / v) if v else 0, "ultima": r["ultima"],
                        "carnes_frescas_24h": carnes.get(r["slug"], 0)})
        out.sort(key=lambda x: (not x["ativo"], -x["n_produtos"]))
        return {"mercados": out}
    finally:
        conn.close()


_RE_VISITANTE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_FUSO_BR = timezone(timedelta(hours=-3))   # dia de Brasília (sem horário de verão desde 2019)


@app.post("/acesso", status_code=204)
def registrar_acesso(body: dict = Body(...), authorization: str | None = Header(None)):
    """O app chama ao abrir: 1 linha por aparelho por dia (n conta as aberturas). Com login,
    grava também QUEM é. Sem dado pessoal além do uid: visitante é um id aleatório do aparelho."""
    vis = str((body or {}).get("visitante") or "")
    if not _RE_VISITANTE.match(vis):
        raise HTTPException(status_code=400, detail="visitante inválido")
    uid = _uid_do_header(authorization) if authorization else None
    agora = datetime.now(timezone.utc)
    dia = agora.astimezone(_FUSO_BR).date().isoformat()
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO acessos (dia, visitante, user_id, primeiro, ultimo, n) VALUES (?, ?, ?, ?, ?, 1) "
            "ON CONFLICT (dia, visitante) DO UPDATE SET ultimo = excluded.ultimo, n = acessos.n + 1, "
            "user_id = COALESCE(excluded.user_id, acessos.user_id)",
            (dia, vis, uid, agora.isoformat(), agora.isoformat()))
        conn.commit()
    finally:
        conn.close()
    return Response(status_code=204)


@app.get("/admin/acessos")
def admin_acessos(dias: int = 30, authorization: str | None = Header(None)):
    """Fluxo de pessoas no app: por dia (aparelhos, logados, cadastros novos), totais de
    hoje/7/30 dias e, por usuário cadastrado, último acesso e em quantos dias usou."""
    dias = max(7, min(int(dias or 30), 120))
    conn = _conn()
    try:
        if not _admin_uid(conn, authorization):
            raise HTTPException(status_code=403, detail="Acesso restrito")
        hoje = datetime.now(_FUSO_BR).date()
        inicio = (hoje - timedelta(days=dias - 1)).isoformat()
        por_dia = {r["dia"]: r for r in conn.execute(
            "SELECT dia, COUNT(*) AS aparelhos, COUNT(DISTINCT user_id) AS logados, SUM(n) AS aberturas "
            "FROM acessos WHERE dia >= ? GROUP BY dia", (inicio,)).fetchall()}
        cad: dict = {}
        for r in conn.execute("SELECT criado_em FROM profiles WHERE criado_em IS NOT NULL").fetchall():
            ce = r["criado_em"]
            ce = datetime.fromisoformat(ce) if isinstance(ce, str) else ce
            if ce.tzinfo is None:
                ce = ce.replace(tzinfo=timezone.utc)
            d = ce.astimezone(_FUSO_BR).date().isoformat()
            if d >= inicio:
                cad[d] = cad.get(d, 0) + 1
        serie = []
        for k in range(dias):
            d = (hoje - timedelta(days=dias - 1 - k)).isoformat()
            r = por_dia.get(d)
            serie.append({"dia": d, "aparelhos": (r["aparelhos"] if r else 0),
                          "logados": (r["logados"] if r else 0),
                          "aberturas": (r["aberturas"] if r else 0) or 0, "cadastros": cad.get(d, 0)})

        def unicos(desde: str) -> int:
            return conn.execute("SELECT COUNT(DISTINCT visitante) AS n FROM acessos WHERE dia >= ?",
                                (desde,)).fetchone()["n"]
        totais = {"hoje": unicos(hoje.isoformat()),
                  "dias_7": unicos((hoje - timedelta(days=6)).isoformat()),
                  "dias_30": unicos((hoje - timedelta(days=29)).isoformat()),
                  "desde": conn.execute("SELECT MIN(dia) AS d FROM acessos").fetchone()["d"]}

        ativos = {r["user_id"]: r for r in conn.execute(
            "SELECT user_id, MAX(ultimo) AS ultimo, COUNT(DISTINCT dia) AS dias_ativos "
            "FROM acessos WHERE user_id IS NOT NULL AND dia >= ? GROUP BY user_id", (inicio,)).fetchall()}
        usuarios = []
        for r in conn.execute(
                "SELECT p.id, u.email, p.nome, p.plano, p.criado_em, u.last_sign_in_at "
                "FROM profiles p LEFT JOIN auth.users u ON u.id = p.id").fetchall():
            a = ativos.get(str(r["id"]))
            ult = (a["ultimo"] if a else None) or r["last_sign_in_at"]
            usuarios.append({"id": str(r["id"]), "email": r["email"], "nome": r["nome"],
                             "plano": r["plano"] or "gratis", "criado_em": r["criado_em"],
                             "ultimo_acesso": ult, "dias_ativos": (a["dias_ativos"] if a else 0)})
        usuarios.sort(key=lambda u: str(u["ultimo_acesso"] or ""), reverse=True)
        return {"serie": serie, "totais": totais, "usuarios": usuarios, "dias": dias}
    finally:
        conn.close()


@app.get("/admin/usuarios")
def admin_usuarios(authorization: str | None = Header(None)):
    conn = _conn()
    try:
        if not _admin_uid(conn, authorization):
            raise HTTPException(status_code=403, detail="Acesso restrito")
        rows = conn.execute(
            """SELECT p.id, u.email, p.nome, p.plano, p.trial_ini, p.criado_em, p.is_admin
               FROM profiles p LEFT JOIN auth.users u ON u.id = p.id
               ORDER BY p.criado_em DESC"""
        ).fetchall()
        usuarios, por_plano = [], {}
        for r in rows:
            plano = r["plano"] or "gratis"
            por_plano[plano] = por_plano.get(plano, 0) + 1
            usuarios.append({"id": r["id"], "email": r["email"], "nome": r["nome"],
                             "plano": plano, "trial_ini": r["trial_ini"],
                             "criado_em": r["criado_em"], "is_admin": bool(r["is_admin"])})
        return {"usuarios": usuarios, "total": len(usuarios), "por_plano": por_plano}
    finally:
        conn.close()


@app.post("/assinatura/criar")
def assinatura_criar(body: dict = Body(...), authorization: str | None = Header(None)):
    """Cria o pagamento no Mercado Pago e devolve o init_point pra redirecionar o usuário.
      - metodo='pix'    -> Checkout Pro avulso (Pix ou cartão à vista); libera N dias no webhook.
      - metodo='cartao' -> assinatura recorrente (/preapproval); renova sozinho.
    O SERVIDOR escolhe o valor pelo id do plano — o cliente NUNCA manda preço."""
    uid = _uid_do_header(authorization)
    if not uid:
        raise HTTPException(status_code=401, detail="Não autenticado")
    if not MP_ACCESS_TOKEN:
        raise HTTPException(status_code=503, detail="Pagamento ainda não configurado")
    b = body or {}
    plano_id = b.get("plano")
    metodo = (b.get("metodo") or "pix").lower()
    if plano_id not in PLANOS:
        raise HTTPException(status_code=400, detail="plano inválido")
    p = PLANOS[plano_id]
    if metodo == "cartao":
        # assinatura recorrente no cartão (renova sozinho) — fluxo redirect (/preapproval)
        return _criar_preapproval(uid, plano_id, _email_do_header(authorization))
    # ---- Pix / avulso: Checkout Pro (uma cobrança; libera N dias) ----
    pref = {
        "items": [{"id": f"poupou-{plano_id}", "title": p["titulo"],
                   "description": f"PouPou+ — acesso por {p['dias']} dias",
                   "quantity": 1, "currency_id": "BRL", "unit_price": p["valor"]}],
        "external_reference": f"{uid}:{plano_id}:avulso",
        "back_urls": {"success": f"{PUBLIC_BASE_URL}/?pagamento=sucesso",
                      "failure": f"{PUBLIC_BASE_URL}/?pagamento=falha",
                      "pending": f"{PUBLIC_BASE_URL}/?pagamento=pendente"},
        "auto_return": "approved",
        "notification_url": f"{PUBLIC_BASE_URL}/webhooks/mercadopago",
        "statement_descriptor": "POUPOU",
        "metadata": {"uid": uid, "plano": plano_id},
    }
    try:
        res = _mp().preference().create(pref).get("response", {})
    except Exception as e:  # noqa: BLE001
        logging.getLogger("api").warning("mp preference: %s", e)
        raise HTTPException(status_code=502, detail="Falha ao criar pagamento")
    init = res.get("init_point") or res.get("sandbox_init_point")
    if not init:
        raise HTTPException(status_code=502, detail="Falha ao criar pagamento")
    return {"init_point": init, "ref_id": res.get("id")}


def _criar_preapproval(uid, plano_id, email) -> dict:
    """Assinatura recorrente no cartão via REDIRECT: cria o preapproval 'pending' (o MP coleta
    o cartão na página dele) e devolve o init_point. O acesso só é liberado no webhook."""
    if not email:
        raise HTTPException(status_code=400, detail="E-mail do usuário indisponível")
    p = PLANOS[plano_id]
    body = {
        "reason": p["titulo"],
        "external_reference": f"{uid}:{plano_id}:cartao",
        "payer_email": email,
        "auto_recurring": {"frequency": p["meses"], "frequency_type": "months",
                           "transaction_amount": p["valor"], "currency_id": "BRL"},
        "back_url": f"{PUBLIC_BASE_URL}/?pagamento=sucesso",
        "status": "pending",
    }
    try:
        res = _mp().preapproval().create(body).get("response", {})
    except Exception as e:  # noqa: BLE001
        logging.getLogger("api").warning("mp preapproval: %s", e)
        raise HTTPException(status_code=502, detail="Falha ao criar assinatura")
    init = res.get("init_point")
    pid = res.get("id")
    if not init:
        raise HTTPException(status_code=502, detail="Falha ao criar assinatura")
    if pid:   # guarda a assinatura pendente (pra cancelar depois); acesso só vem no webhook
        conn = _conn()
        try:
            conn.execute("UPDATE profiles SET mp_preapproval_id=?, mp_status='pending', atualizado_em=? "
                         "WHERE id=?", (pid, datetime.now(timezone.utc).isoformat(), uid))
        finally:
            conn.close()
    return {"init_point": init, "ref_id": pid, "recorrente": True}


def _mp_get(path: str) -> dict:
    """GET cru na API do MP (recursos sem método no SDK Python, ex.: /authorized_payments/{id})."""
    try:
        r = httpx.get(f"https://api.mercadopago.com{path}",
                      headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}, timeout=15)
        return r.json() if r.status_code == 200 else {}
    except Exception:  # noqa: BLE001
        return {}


def _proc_payment(payment_id) -> None:
    """Confirma um pagamento avulso (Checkout Pro) e, se aprovado, estende o acesso."""
    if not payment_id:
        return
    pay = _mp().payment().get(payment_id).get("response", {})
    status = pay.get("status")
    ext = (pay.get("external_reference") or "").split(":")
    uid = ext[0] if ext else ""
    plano_id = ext[1] if len(ext) > 1 else ""
    if status == "approved" and uid and plano_id in PLANOS:
        conn = _conn()
        try:
            if _registra_evento(conn, payment_id, "payment", status, uid, plano_id,
                                 pay.get("transaction_amount"), pay):
                _estende_assinatura(conn, uid, plano_id, mp_status="approved")
                _processar_indicacao(conn, uid)
        finally:
            conn.close()


def _proc_authorized_payment(ap_id) -> None:
    """Cada cobrança recorrente do cartão. Sucesso = status 'processed' + payment.status
    'approved'. O external_reference já vem no objeto -> identifica o usuário sem buscar o
    preapproval. Idempotente pelo id da fatura (não estende 2x em reenvio)."""
    if not ap_id:
        return
    ap = _mp_get(f"/authorized_payments/{ap_id}")
    ok = ap.get("status") == "processed" and (ap.get("payment") or {}).get("status") == "approved"
    if not ok:
        return
    ext = (ap.get("external_reference") or "").split(":")
    uid = ext[0] if ext else ""
    plano_id = ext[1] if len(ext) > 1 else ""
    pre_id = ap.get("preapproval_id")
    if uid and plano_id in PLANOS:
        conn = _conn()
        try:
            if _registra_evento(conn, ap_id, "subscription_authorized_payment", "approved",
                                 uid, plano_id, ap.get("transaction_amount"), ap):
                _estende_assinatura(conn, uid, plano_id, mp_status="authorized", preapproval_id=pre_id)
                _processar_indicacao(conn, uid)
        finally:
            conn.close()


def _proc_preapproval(pre_id) -> None:
    """Mudança de status da assinatura (pending/authorized/paused/cancelled). Só atualiza o
    mp_status; o acesso é estendido pelas cobranças. Cancelar NÃO tira o acesso já pago
    (mantém até plano_expira_em; só não renova)."""
    if not pre_id:
        return
    sub = _mp_get(f"/preapproval/{pre_id}")
    status = sub.get("status")
    ext = (sub.get("external_reference") or "").split(":")
    uid = ext[0] if ext else ""
    if uid and status:
        conn = _conn()
        try:
            conn.execute("UPDATE profiles SET mp_status=?, mp_preapproval_id=?, atualizado_em=? "
                         "WHERE id=?", (status, pre_id, datetime.now(timezone.utc).isoformat(), uid))
        finally:
            conn.close()


@app.post("/webhooks/mercadopago")
async def webhook_mp(request: Request):
    """Recebe notificações do Mercado Pago. Valida a assinatura (HMAC), consulta o status REAL
    na API do MP e só então mexe no plano (nunca confia no payload). Responde 200 rápido."""
    data_id = request.query_params.get("data.id") or request.query_params.get("id")
    if not _valida_assinatura_mp(request.headers.get("x-signature"),
                                 request.headers.get("x-request-id"), data_id):
        raise HTTPException(status_code=401, detail="assinatura inválida")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    topico = (body.get("type") or body.get("topic")
              or request.query_params.get("type") or request.query_params.get("topic"))
    try:
        if topico == "payment":
            _proc_payment(data_id)
        elif topico == "subscription_authorized_payment":   # cada cobrança recorrente do cartão
            _proc_authorized_payment(data_id)
        elif topico == "subscription_preapproval":           # status da assinatura (authorized/cancelled/…)
            _proc_preapproval(data_id)
    except Exception as e:  # noqa: BLE001
        logging.getLogger("api").warning("webhook mp %s/%s: %s", topico, data_id, e)
    return Response(status_code=200)


@app.get("/assinatura/status")
def assinatura_status(authorization: str | None = Header(None)):
    """Status da assinatura do usuário (o front confirma por aqui após voltar do checkout —
    a fonte da verdade é o profiles, atualizado pelo webhook, não a back_url)."""
    uid = _uid_do_header(authorization)
    if not uid:
        raise HTTPException(status_code=401, detail="Não autenticado")
    conn = _conn()
    try:
        r = conn.execute(
            "SELECT plano, plano_expira_em, mp_preapproval_id, mp_status FROM profiles WHERE id = ?",
            (uid,)).fetchone()
        ativo = _premium(conn, uid)
        exp = r["plano_expira_em"] if r else None
        dias = None
        if exp:
            e = exp if not isinstance(exp, str) else datetime.fromisoformat(exp)
            if e.tzinfo is None:
                e = e.replace(tzinfo=timezone.utc)
            dias = max(0, (e - datetime.now(timezone.utc)).days)
        return {"plano": (r["plano"] if r else "gratis"), "ativo": ativo, "expira_em": exp,
                "dias_restantes": dias,
                "recorrente": bool(r and r["mp_preapproval_id"] and r["mp_status"] not in ("cancelled", None)),
                "mp_status": (r["mp_status"] if r else None)}
    finally:
        conn.close()


@app.post("/assinatura/cancelar")
def assinatura_cancelar(authorization: str | None = Header(None)):
    """Cancela a assinatura recorrente no Mercado Pago. As cobranças param; o acesso já pago
    segue até plano_expira_em (sem reembolso do ciclo corrente)."""
    uid = _uid_do_header(authorization)
    if not uid:
        raise HTTPException(status_code=401, detail="Não autenticado")
    if not MP_ACCESS_TOKEN:
        raise HTTPException(status_code=503, detail="Pagamento não configurado")
    conn = _conn()
    try:
        r = conn.execute("SELECT mp_preapproval_id FROM profiles WHERE id = ?", (uid,)).fetchone()
        pid = r["mp_preapproval_id"] if r else None
        if not pid:
            raise HTTPException(status_code=400, detail="Sem assinatura recorrente ativa")
        try:
            _mp().preapproval().update(pid, {"status": "cancelled"})
        except Exception as e:  # noqa: BLE001
            logging.getLogger("api").warning("mp cancelar: %s", e)
            raise HTTPException(status_code=502, detail="Falha ao cancelar no Mercado Pago")
        conn.execute("UPDATE profiles SET mp_status='cancelled', atualizado_em=? WHERE id=?",
                     (datetime.now(timezone.utc).isoformat(), uid))
        return {"ok": True, "status": "cancelled",
                "aviso": "As cobranças param. Seu acesso segue até o fim do período já pago."}
    finally:
        conn.close()


@app.post("/trial/iniciar")
def trial_iniciar(authorization: str | None = Header(None)):
    """Começa o reverse trial no servidor (o cliente não escreve mais profiles). Só libera se
    ainda não usou o trial e está no grátis. Quem chegou por indicação (indicado_por setado)
    ganha o TRIAL DOBRADO (TRIAL_DIAS_INDICADO). Idempotente-ish: se já é assinante/trial,
    devolve o estado atual sem regredir."""
    uid = _uid_do_header(authorization)
    if not uid:
        raise HTTPException(status_code=401, detail="Não autenticado")
    conn = _conn()
    try:
        r = conn.execute(
            "SELECT plano, trial_ini, trial_usado, indicado_por FROM profiles WHERE id = ?",
            (uid,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Perfil não encontrado")
        if r["plano"] == "assinante":
            return {"ok": True, "plano": "assinante", "ja": True}
        if r["trial_usado"] or r["plano"] == "trial":
            return {"ok": False, "plano": r["plano"], "trial_ini": r["trial_ini"],
                    "motivo": "trial_ja_usado"}
        dias = TRIAL_DIAS_INDICADO if (r["indicado_por"] or "").strip() else TRIAL_DIAS_PADRAO
        agora = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE profiles SET plano = 'trial', trial_ini = ?, trial_usado = true, "
            "trial_dias = ?, atualizado_em = ? WHERE id = ?", (agora, dias, agora, uid))
        return {"ok": True, "plano": "trial", "trial_ini": agora, "trial_dias": dias}
    finally:
        conn.close()


@app.post("/admin/usuario/{uid}/plano")
def admin_set_plano(uid: str, body: dict = Body(...), authorization: str | None = Header(None)):
    conn = _conn()
    try:
        if not _admin_uid(conn, authorization):
            raise HTTPException(status_code=403, detail="Acesso restrito")
        plano = (body or {}).get("plano")
        if plano not in ("gratis", "trial", "assinante"):
            raise HTTPException(status_code=400, detail="plano inválido")
        agora = datetime.now(timezone.utc).isoformat()
        if plano == "trial":
            conn.execute("UPDATE profiles SET plano = ?, trial_ini = ?, atualizado_em = ? WHERE id = ?",
                         (plano, agora, agora, uid))
        elif plano == "assinante":
            # Assinante dado à mão = sem validade (como os legados). Sem limpar, um
            # plano_expira_em antigo (assinatura/bônus vencido) fazia o _premium negar acesso.
            conn.execute("UPDATE profiles SET plano = ?, plano_expira_em = NULL, atualizado_em = ? WHERE id = ?",
                         (plano, agora, uid))
        else:
            conn.execute("UPDATE profiles SET plano = ?, atualizado_em = ? WHERE id = ?",
                         (plano, agora, uid))
        return {"ok": True, "id": uid, "plano": plano}
    finally:
        conn.close()


@app.get("/admin/mercados")
def admin_mercados(authorization: str | None = Header(None)):
    conn = _conn()
    try:
        if not _admin_uid(conn, authorization):
            raise HTTPException(status_code=403, detail="Acesso restrito")
        rows = conn.execute(
            "SELECT slug, nome, plataforma, cidade, ativo, loja_ref FROM mercados ORDER BY ativo DESC, nome"
        ).fetchall()
        return {"mercados": [{"slug": r["slug"], "nome": r["nome"], "plataforma": r["plataforma"],
                              "cidade": r["cidade"], "ativo": bool(r["ativo"]), "loja_ref": r["loja_ref"]}
                             for r in rows]}
    finally:
        conn.close()


@app.post("/admin/mercado/{slug}/ativo")
def admin_toggle_mercado(slug: str, body: dict = Body(...), authorization: str | None = Header(None)):
    conn = _conn()
    try:
        if not _admin_uid(conn, authorization):
            raise HTTPException(status_code=403, detail="Acesso restrito")
        ativo = 1 if (body or {}).get("ativo") else 0
        conn.execute("UPDATE mercados SET ativo = ? WHERE slug = ?", (ativo, slug))
        return {"ok": True, "slug": slug, "ativo": bool(ativo)}
    finally:
        conn.close()


@app.post("/admin/coleta/solicitar")
def admin_coleta_solicitar(authorization: str | None = Header(None)):
    """Enfileira um pedido de coleta (o coleta_watcher.py no PC do dono é quem roda o
    coletar.py). Se já houver um pendente/rodando, devolve esse — não duplica."""
    conn = _conn()
    try:
        uid = _admin_uid(conn, authorization)
        if not uid:
            raise HTTPException(status_code=403, detail="Acesso restrito")
        r = conn.execute(
            "SELECT id, status FROM coleta_jobs WHERE status IN ('pendente','rodando') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if r:
            return {"ok": True, "ja_existe": True, "id": r["id"], "status": r["status"]}
        cur = conn.execute(
            "INSERT INTO coleta_jobs (status, solicitado_por, solicitado_em) VALUES ('pendente', ?, ?) RETURNING id",
            (uid, datetime.now(timezone.utc).isoformat()))
        novo = cur.fetchone()
        return {"ok": True, "id": (novo["id"] if novo else None), "status": "pendente"}
    finally:
        conn.close()


@app.get("/admin/coleta/jobs")
def admin_coleta_jobs(authorization: str | None = Header(None)):
    """Últimos pedidos de coleta + status (o painel usa pra acompanhar)."""
    conn = _conn()
    try:
        if not _admin_uid(conn, authorization):
            raise HTTPException(status_code=403, detail="Acesso restrito")
        rows = conn.execute(
            "SELECT id, status, solicitado_em, iniciado_em, terminado_em, resultado "
            "FROM coleta_jobs ORDER BY id DESC LIMIT 10").fetchall()
        return {"jobs": [{"id": r["id"], "status": r["status"], "solicitado_em": r["solicitado_em"],
                          "iniciado_em": r["iniciado_em"], "terminado_em": r["terminado_em"],
                          "resultado": r["resultado"]} for r in rows]}
    finally:
        conn.close()


@app.get("/admin/metricas")
def admin_metricas(authorization: str | None = Header(None)):
    conn = _conn()
    try:
        if not _admin_uid(conn, authorization):
            raise HTTPException(status_code=403, detail="Acesso restrito")
        def n(sql):
            return conn.execute(sql).fetchone()["n"]
        return {
            "usuarios": n("SELECT COUNT(*) n FROM profiles"),
            "assinantes": n("SELECT COUNT(*) n FROM profiles WHERE plano = 'assinante'"),
            "em_trial": n("SELECT COUNT(*) n FROM profiles WHERE plano = 'trial'"),
            "usuarios_com_lista": n("SELECT COUNT(DISTINCT user_id) n FROM lista"),
            "itens_em_listas": n("SELECT COUNT(*) n FROM lista"),
            "favoritos": n("SELECT COUNT(*) n FROM favoritos"),
            "produtos": n("SELECT COUNT(*) n FROM produtos"),
            "precos_atuais": n("SELECT COUNT(*) n FROM precos_atual"),
            "frescos": n("SELECT COUNT(*) n FROM pereciveis"),
            "mercados_ativos": n("SELECT COUNT(*) n FROM mercados WHERE ativo = 1"),
        }
    finally:
        conn.close()


@app.get("/gestao")
def gestao():
    """Página SEPARADA de administração (login próprio, sem link no app do cliente).
    A trava real continua nos endpoints /admin/* (checam is_admin); esta página é só a casca."""
    return FileResponse(RAIZ / "front" / "admin.html")


# ===================== PUSH (notificações no celular, mesmo com o app fechado) =====================
def _vapid_cfg(conn):
    rows = {r["chave"]: r["valor"] for r in conn.execute(
        "SELECT chave, valor FROM app_config WHERE chave IN "
        "('vapid_public','vapid_private','vapid_subject')")}
    return (rows.get("vapid_public"), rows.get("vapid_private"),
            rows.get("vapid_subject") or "mailto:contato@poupou.app")


def _enviar_push(conn, uid, titulo, corpo, url="/"):
    """Envia push a TODAS as assinaturas do usuário. Remove as mortas (404/410)."""
    from pywebpush import WebPushException, webpush
    _pub, priv, subject = _vapid_cfg(conn)
    if not priv:
        return 0
    subs = conn.execute("SELECT endpoint, p256dh, auth FROM push_subs WHERE user_id = ?",
                        (uid,)).fetchall()
    base_url = PUBLIC_BASE_URL or ""
    payload = json.dumps({"title": titulo, "body": corpo, "url": url,
                          "icon": base_url + "/icons/icon-v2-192.png",
                          "badge": base_url + "/icons/badge.png"})
    enviados = 0
    for s in subs:
        try:
            webpush(subscription_info={"endpoint": s["endpoint"],
                                       "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}},
                    data=payload, vapid_private_key=priv, vapid_claims={"sub": subject})
            enviados += 1
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (404, 410):
                conn.execute("DELETE FROM push_subs WHERE endpoint = ?", (s["endpoint"],))
        except Exception:  # noqa: BLE001
            pass
    return enviados


@app.get("/push/config")
def push_config():
    conn = _conn()
    try:
        pub, _priv, _sub = _vapid_cfg(conn)
        return {"publicKey": pub or ""}
    finally:
        conn.close()


@app.post("/push/subscribe")
def push_subscribe(sub: dict = Body(...), authorization: str | None = Header(None)):
    conn = _conn()
    try:
        uid = _uid_do_header(authorization)
        if not uid:
            raise HTTPException(status_code=401, detail="entre pra ativar notificações")
        endpoint = (sub.get("endpoint") or "").strip()
        keys = sub.get("keys") or {}
        p256dh, auth = keys.get("p256dh"), keys.get("auth")
        if not (endpoint and p256dh and auth):
            raise HTTPException(status_code=400, detail="assinatura inválida")
        conn.execute(
            "INSERT INTO push_subs (user_id, endpoint, p256dh, auth, criado_em) VALUES (?,?,?,?,?) "
            "ON CONFLICT (endpoint) DO UPDATE SET user_id=excluded.user_id, "
            "p256dh=excluded.p256dh, auth=excluded.auth",
            (uid, endpoint, p256dh, auth, datetime.now(timezone.utc).isoformat()))
        return {"ok": True}
    finally:
        conn.close()


@app.post("/push/unsubscribe")
def push_unsubscribe(sub: dict = Body(None), authorization: str | None = Header(None)):
    conn = _conn()
    try:
        uid = _uid_do_header(authorization)
        endpoint = ((sub or {}).get("endpoint") or "").strip()
        if uid and endpoint:
            conn.execute("DELETE FROM push_subs WHERE user_id=? AND endpoint=?", (uid, endpoint))
        return {"ok": True}
    finally:
        conn.close()


@app.post("/push/test")
def push_test(authorization: str | None = Header(None)):
    conn = _conn()
    try:
        uid = _uid_do_header(authorization)
        if not uid:
            raise HTTPException(status_code=401, detail="entre primeiro")
        n = _enviar_push(conn, uid, "PouPou 🛒",
                         "Notificações ativadas! É assim que a gente vai te avisar de queda de preço.",
                         "/")
        return {"ok": True, "enviados": n}
    finally:
        conn.close()


@app.get("/indicacao/status")
def indicacao_status(authorization: str | None = Header(None)):
    """Painel de indicação do usuário: o código/link pra compartilhar e o progresso
    (quantos indicou, quantos assinaram, quantos meses já ganhou e quanto falta pro próximo)."""
    uid = _uid_do_header(authorization)
    if not uid:
        raise HTTPException(status_code=401, detail="Não autenticado")
    conn = _conn()
    try:
        code = _gerar_ref_code(conn, uid)
        indicados = convertidos = 0
        if code:
            indicados = conn.execute(
                "SELECT COUNT(*) AS n FROM profiles WHERE indicado_por = ?", (code,)).fetchone()["n"]
            convertidos = conn.execute(
                "SELECT COUNT(*) AS n FROM profiles WHERE indicado_por = ? AND ref_creditado = true",
                (code,)).fetchone()["n"]
        meses_ganhos = convertidos // REF_INDICADOS_POR_MES
        faltam = REF_INDICADOS_POR_MES - (convertidos % REF_INDICADOS_POR_MES)
        return {
            "code": code,
            "link": f"{PUBLIC_BASE_URL}/?ref={code}" if code else None,
            "indicados": indicados,
            "convertidos": convertidos,
            "meses_ganhos": meses_ganhos,
            "faltam_pro_proximo": faltam,
            "por_mes": REF_INDICADOS_POR_MES,
            "trial_indicado_dias": TRIAL_DIAS_INDICADO,   # o amigo ganha esse trial ao cadastrar
            "trial_padrao_dias": TRIAL_DIAS_PADRAO,
        }
    finally:
        conn.close()


@app.post("/indicacao/entrar")
def indicacao_entrar(body: dict = Body(...), authorization: str | None = Header(None)):
    """Vincula quem se cadastrou a um padrinho (uma vez só). O front chama isso logo após o
    cadastro, com o código capturado do link ?ref=CODE. Valida existência e veta auto-indicação;
    o crédito de verdade só acontece quando o indicado ASSINA (ver _processar_indicacao)."""
    uid = _uid_do_header(authorization)
    if not uid:
        raise HTTPException(status_code=401, detail="Não autenticado")
    code = (body.get("code") or "").strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="Código vazio")
    conn = _conn()
    try:
        r = conn.execute(
            "SELECT indicado_por, ref_code, plano FROM profiles WHERE id = ?", (uid,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="Perfil não encontrado")
        if r["indicado_por"]:
            return {"ok": True, "ja": True}          # já tem padrinho: não troca
        # quem JÁ É assinante não pode entrar como "indicado" (fecharia a brecha de um pagante
        # se atribuir a um amigo e os dois ganharem crédito na próxima renovação).
        if r["plano"] == "assinante":
            return {"ok": False, "motivo": "ja_assinante"}
        if r["ref_code"] and r["ref_code"] == code:
            raise HTTPException(status_code=400, detail="Você não pode usar o próprio código")
        pad = conn.execute("SELECT id FROM profiles WHERE ref_code = ?", (code,)).fetchone()
        if not pad or pad["id"] == uid:
            raise HTTPException(status_code=400, detail="Código de indicação inválido")
        # grava só se ainda estiver vazio (idempotente sob corrida)
        conn.execute(
            "UPDATE profiles SET indicado_por = ? WHERE id = ? AND indicado_por IS NULL",
            (code, uid))
        return {"ok": True}
    finally:
        conn.close()


@app.get("/manifest.webmanifest")
def pwa_manifest():
    return FileResponse(RAIZ / "front" / "manifest.webmanifest",
                        media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def pwa_sw():
    # servido da raiz => escopo "/" (controla o app todo); no-cache pra atualizações pegarem
    return FileResponse(RAIZ / "front" / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/icons/{fname}")
def pwa_icon(fname: str):
    base = (RAIZ / "front" / "icons").resolve()
    p = (base / fname).resolve()
    if p.parent != base or not p.is_file():
        raise HTTPException(status_code=404, detail="ícone não encontrado")
    return FileResponse(p, headers={"Cache-Control": "public, max-age=604800"})


@app.get("/vendor/{fname}")
def vendor(fname: str):
    """Libs de terceiros hospedadas no próprio app (ex.: supabase-js) — mesma origem, o SW
    cacheia, o PWA abre rápido e o login não quebra quando um CDN externo falha."""
    base = (RAIZ / "front" / "vendor").resolve()
    p = (base / fname).resolve()
    if p.parent != base or not p.is_file():
        raise HTTPException(status_code=404, detail="arquivo não encontrado")
    return FileResponse(p, headers={"Cache-Control": "public, max-age=604800"})


@app.get("/")
def home():
    # no-cache no HTML: garante que atualizações (manifest/ícones/SW) sempre cheguem ao navegador
    return FileResponse(FRONT, headers={"Cache-Control": "no-cache"})
