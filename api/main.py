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

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse

from analise import normalizar
from analise.ranking import _unaccent, buscar_produtos
from db import database

load_dotenv()
RAIZ = Path(__file__).resolve().parent.parent
FRONT = RAIZ / "front" / "index.html"
LOGOS_DIR = RAIZ / "front" / "logos"

app = FastAPI(title="Comparador de Preços — RM Campinas", version="0.5")

# Garante schema/migrações (tabelas links, lista) ao subir a API.
_boot = database.conectar(os.getenv("DB_PATH", "dados/precos.db"))
database.inicializar(_boot)
_boot.close()

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


def _ranking_enriquecido(conn: sqlite3.Connection, ean: str, com_sparkline: bool = True) -> list[dict]:
    # Lê da tabela quente precos_atual (1 linha por ean+mercado) — rápido mesmo com
    # milhões de snapshots no histórico. Sparkline (que varre o histórico) só sob demanda.
    linhas = conn.execute(
        """
        SELECT m.id AS mercado_id, m.nome AS mercado, m.slug AS mercado_slug,
               m.cor_badge, m.logo_url, m.loja_ref,
               pa.preco, pa.preco_por_unidade, pr.unidade, pa.origem, pa.coletado_em,
               pa.so_no_site, pa.preco_de, pa.desconto_pct, pa.oferta_tipo,
               lk.url AS url
        FROM precos_atual pa
        JOIN mercados m  ON m.id = pa.mercado_id
        JOIN produtos pr ON pr.ean = pa.ean
        LEFT JOIN links lk ON lk.ean = pa.ean AND lk.mercado_id = pa.mercado_id
        WHERE pa.ean = ? AND m.ativo = 1
        ORDER BY pa.preco ASC
        """,
        (ean,),
    ).fetchall()
    out = []
    for r in linhas:
        spark = _sparkline(conn, ean, r["mercado_id"]) if com_sparkline else []
        out.append({
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
        })
    return out


def _produto_dict(conn: sqlite3.Connection, prod: sqlite3.Row, com_sparkline: bool = True) -> dict:
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
            "produtos": [_produto_dict(conn, p, com_sparkline=False) for p in prods],
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
        # buscar_produtos devolve (ean, nome_canonico, marca, n_mercados);
        # busca o produto completo pra enriquecer.
        resultados = []
        for a in achados:
            prod = conn.execute(
                "SELECT ean, nome_canonico, marca, categoria, unidade, qtd_embalagem, imagem_url "
                "FROM produtos WHERE ean = ?", (a["ean"],)
            ).fetchone()
            resultados.append(_produto_dict(conn, prod, com_sparkline=False))
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


@app.get("/cesta")
def cesta():
    """Compara o custo TOTAL da lista em cada mercado (preço × quantidade),
    com cobertura (quantos itens o mercado tem) e a 'melhor divisão' item a item."""
    conn = _conn()
    try:
        itens_lista = conn.execute(
            "SELECT ean, nome, quantidade, preco_alvo, comprado FROM lista ORDER BY criado_em DESC"
        ).fetchall()
        mercados = conn.execute(
            "SELECT id, slug, nome, cor_badge FROM mercados WHERE ativo = 1"
        ).fetchall()
        n_itens = len(itens_lista)
        n_alertas = 0
        divisao: dict[str, dict] = {}  # estratégia "dividido": slug -> {itens, subtotal}

        # acumuladores por mercado
        acc = {m["id"]: {"slug": m["slug"], "nome": m["nome"], "cor_badge": m["cor_badge"],
                         "total": 0.0, "cobertura": 0, "faltantes": []} for m in mercados}
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

        mercados_out = list(acc.values())
        for m in mercados_out:
            m["total"] = round(m["total"], 2)
            m["completo"] = (m["cobertura"] == n_itens and n_itens > 0)
        # ordena: completos primeiro, depois maior cobertura, depois menor total
        mercados_out.sort(key=lambda m: (not m["completo"], -m["cobertura"], m["total"]))

        completos = [m for m in mercados_out if m["completo"]]
        melhor_mercado = completos[0] if completos else (mercados_out[0] if mercados_out else None)
        economia = None
        if len(completos) >= 2:
            economia = round(completos[-1]["total"] - completos[0]["total"], 2)

        # estratégia "dividido": divisão por mercado (maior subtotal primeiro)
        divisao_lst = sorted(divisao.values(), key=lambda d: -d["subtotal"])
        for d in divisao_lst:
            d["subtotal"] = round(d["subtotal"], 2)
        mix_total = round(mix_total, 2)

        # quanto o "dividido" economiza a mais que comprar tudo na melhor loja única
        economia_dividido = None
        if melhor_mercado and melhor_mercado["completo"]:
            economia_dividido = round(melhor_mercado["total"] - mix_total, 2)

        return {
            "n_itens": n_itens,
            "n_alertas": n_alertas,
            "n_comprados": sum(1 for i in itens_out if i["comprado"]),
            "mercados": mercados_out,
            "melhor_mercado": melhor_mercado,          # Opção 1: comprar tudo numa loja só
            "economia_vs_pior": economia,              # loja única mais barata vs mais cara
            "mix": {"total": mix_total, "n_mercados": len(mix_mercados), "divisao": divisao_lst},  # Opção 2
            "economia_dividido_vs_loja": economia_dividido,  # quanto o dividido poupa vs a melhor loja única
            "economia_mercados": round(economia_mercados, 2),  # vs. comprar cada item no mercado mais caro
            "economia_dias": round(economia_dias, 2),          # vs. o preço típico recente (queda nos dias)
            "itens": itens_out,
        }
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
