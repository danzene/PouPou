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

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse

from analise import normalizar
from analise.ranking import _unaccent, buscar_produtos
from db import database

load_dotenv()
RAIZ = Path(__file__).resolve().parent.parent
FRONT = RAIZ / "front" / "index.html"
LOGOS_DIR = RAIZ / "front" / "logos"

app = FastAPI(title="Comparador de Preços — RM Campinas", version="0.5")

# ---- Auth (Supabase): valida o Bearer token e diz QUEM é o usuário + se é premium ----
SUPABASE_URL = os.getenv("SUPABASE_URL", "https://ijtnozcomqzxfaidurso.supabase.co").rstrip("/")
SUPABASE_ANON = os.getenv(
    "SUPABASE_ANON_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImlqdG5vemNvbXF6"
    "eGZhaWR1cnNvIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODY2NzY2MTAsImV4cCI6MjEwMjI1MjYxMH0."
    "9Hl9d2fK0yiD7WAaMZZfRuqAJeJ5F8skGIpWNkpfl9o")  # chave PÚBLICA (anon)
_tok_cache: dict = {}   # token -> (user_id, expira_em): evita bater no Supabase a cada request


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


def _premium(conn, uid: str | None) -> bool:
    """True se o usuário é assinante OU está com o reverse trial de 3 dias ativo (lê profiles)."""
    if not uid:
        return False
    r = conn.execute("SELECT plano, trial_ini FROM profiles WHERE id = ?", (uid,)).fetchone()
    if not r:
        return False
    if r["plano"] == "assinante":
        return True
    if r["plano"] == "trial" and r["trial_ini"]:
        ini = r["trial_ini"]
        if isinstance(ini, str):
            try:
                ini = datetime.fromisoformat(ini)
            except ValueError:
                return False
        if ini.tzinfo is None:
            ini = ini.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - ini < timedelta(days=3)
    return False


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

# Garante schema/migrações (tabelas links, lista) ao subir a API. RESILIENTE: se o banco
# não responder no boot (host novo/rede lenta), NÃO derruba a API — senão o uvicorn nem
# começa a servir e o proxy do host devolve 502 em TUDO (até no /, que nem toca o banco).
# Cada request reconecta por conta própria; o schema no Postgres já existe (init é no-op lá).
try:
    _boot = database.conectar(os.getenv("DB_PATH", "dados/precos.db"))
    database.inicializar(_boot)
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
    linhas = conn.execute(
        f"""
        SELECT {_RANKING_COLS}
        FROM precos_atual pa
        JOIN mercados m  ON m.id = pa.mercado_id
        JOIN produtos pr ON pr.ean = pa.ean
        LEFT JOIN links lk ON lk.ean = pa.ean AND lk.mercado_id = pa.mercado_id
        WHERE pa.ean = ? AND m.ativo = 1
        ORDER BY pa.preco ASC
        """,
        (ean,),
    ).fetchall()
    return [
        _linha_ranking(r, _sparkline(conn, ean, r["mercado_id"]) if com_sparkline else [])
        for r in linhas
    ]


def _rankings_lote(conn, eans: list[str]) -> dict[str, list[dict]]:
    """Ranking de VÁRIOS eans numa query só (evita o N+1 das listagens). Sem sparkline.
    Devolve {ean: [linhas ordenadas por preço]}. Uma query em vez de uma por produto —
    é o que derruba /categoria de ~60 idas ao banco pra 1."""
    if not eans:
        return {}
    ph = ",".join(["?"] * len(eans))
    linhas = conn.execute(
        f"""
        SELECT {_RANKING_COLS}
        FROM precos_atual pa
        JOIN mercados m  ON m.id = pa.mercado_id
        JOIN produtos pr ON pr.ean = pa.ean
        LEFT JOIN links lk ON lk.ean = pa.ean AND lk.mercado_id = pa.mercado_id
        WHERE pa.ean IN ({ph}) AND m.ativo = 1
        ORDER BY pa.ean, pa.preco ASC
        """,
        tuple(eans),
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in linhas:
        out.setdefault(r["ean"], []).append(_linha_ranking(r))
    return out


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
    cond = " AND ".join(["p.nome_norm LIKE ?"] * len(tokens))
    params = [f"%{t}%" for t in tokens]
    rows = conn.execute(
        f"""
        SELECT p.nome, p.nome_norm, p.preco, p.unidade, p.url, p.imagem_url, p.coletado_em,
               p.so_no_site, p.preco_de, p.desconto_pct, p.oferta_tipo,
               m.nome AS mercado, m.slug AS mercado_slug, m.cor_badge, m.plataforma
        FROM pereciveis p JOIN mercados m ON m.id = p.mercado_id
        WHERE {cond}
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
def buscar(q: str):
    q = (q or "").strip()
    if len(q) < 2:
        return {"q": q, "resultados": [], "pereciveis": []}
    conn = _conn()
    try:
        achados = buscar_produtos(conn, q, limite=40)
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
        pereciveis = _buscar_pereciveis(conn, q)
        return {"q": q, "resultados": resultados, "pereciveis": pereciveis}
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
        linhas = conn.execute(
            """
            SELECT m.nome AS mercado, m.cor_badge, p.preco, p.preco_por_unidade, p.coletado_em
            FROM precos p JOIN mercados m ON m.id = p.mercado_id
            WHERE p.ean = ?
            ORDER BY m.nome, p.coletado_em
            """,
            (ean,),
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
               FROM precos_atual pa JOIN mercados m ON m.id = pa.mercado_id WHERE pa.ean = ?""",
            (ean,),
        ):
            s = series.setdefault(r["mercado"], {"mercado": r["mercado"],
                                                 "cor_badge": r["cor_badge"], "pontos": []})
            if not s["pontos"] or s["pontos"][-1]["coletado_em"] != r["coletado_em"]:
                s["pontos"].append({"preco": round(r["preco"], 2), "coletado_em": r["coletado_em"]})
        return {"ean": ean, "nome": prod["nome_canonico"], "origem": "online",
                "series": list(series.values())}
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
                # COMPARAÇÃO JUSTA: quem NÃO tem esse item "completa" pelo menor preço em outra
                # loja — assim cada loja tem o total da FEIRA INTEIRA (mesma cesta, comparável).
                custo_i = melhor["preco"] * qtd
                for m in mercados:
                    if m["slug"] not in slugs_com:
                        acc[m["id"]]["completar_custo"] += custo_i
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


@app.get("/")
def home():
    return FileResponse(FRONT)
