"""Entrypoint da coleta.

Uso:
    python coletar.py                 # roda todos os mercados ativos
    python coletar.py savegnago       # roda só o(s) slug(s) informados

Garantias:
- Um mercado que falhar (timeout, layout mudou) é logado e NÃO derruba os outros.
- `precos` é append-only: rodar duas vezes gera dois snapshots (histórico).
- Dentro de uma rodada, cada EAN por mercado vira UM snapshot (dedupe).
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from analise import normalizar
from coletores.base import Coletor, ItemPerecivel
from db import database

RAIZ = Path(__file__).resolve().parent
load_dotenv(RAIZ / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("coletar")


def _carregar_json(caminho: Path) -> dict:
    return json.loads(caminho.read_text(encoding="utf-8"))


def _instanciar(cfg: dict, *, delay: float, timeout: float, max_por_termo: int, contato: str) -> Coletor:
    """Importa a classe de 'modulo:Classe' e instancia com os params do JSON."""
    modulo_nome, _, classe_nome = cfg["classe"].partition(":")
    modulo = importlib.import_module(modulo_nome)
    classe = getattr(modulo, classe_nome)
    return classe(
        slug=cfg["slug"],
        nome=cfg["nome"],
        url_base=cfg["url_base"],
        cidade=cfg.get("cidade"),
        delay=delay,
        timeout=timeout,
        max_por_termo=max_por_termo,
        contato=contato,
        **cfg.get("params", {}),
    )


def _catalogo_ou_sementes(coletor: Coletor, sementes: list[tuple[str, str]], slug: str):
    """Catálogo completo; se não vier NADA (ex.: a árvore de categorias do Tenda dando
    504 do lado deles), cai pros termos-semente — cobre menos produtos, mas o mercado
    não fica dias sem atualizar."""
    vazio = True
    for item in coletor.coletar_catalogo():
        vazio = False
        yield item
    if vazio:
        log.warning("[%s] catálogo completo veio vazio -> caindo pros termos-semente", slug)
        yield from coletor.coletar(sementes)


def coletar_mercado(conn, cfg: dict, sementes: list[str], **opts) -> tuple[int, int]:
    """Coleta um mercado. Devolve (produtos_novos_ou_vistos, snapshots_gravados)."""
    mercado_id = database.upsert_mercado(
        conn,
        slug=cfg["slug"],
        nome=cfg["nome"],
        url_base=cfg["url_base"],
        plataforma=cfg.get("plataforma"),
        cidade=cfg.get("cidade"),
        ativo=cfg.get("ativo", True),
        logo_url=cfg.get("logo_url"),
        cor_badge=cfg.get("cor_badge"),
        loja_ref=cfg.get("loja_ref"),
    )
    coletor = _instanciar(cfg, **opts)
    agora = datetime.now(timezone.utc).isoformat()

    # Engine de CATÁLOGO COMPLETO (varredura por categoria) quando o coletor suporta;
    # senão, cai no modo antigo por termos-semente. A categoria vem do próprio item.
    if hasattr(coletor, "coletar_catalogo"):
        log.info("[%s] modo: catálogo completo (varredura por categoria)", cfg["slug"])
        fonte = _catalogo_ou_sementes(coletor, sementes, cfg["slug"])
    else:
        log.info("[%s] modo: termos-semente", cfg["slug"])
        fonte = coletor.coletar(sementes)

    vistos: set[str] = set()
    vistos_per: set[str] = set()
    n_snapshots = 0
    n_pereciveis = 0
    # Grava EM LOTE (database.gravar_lote_*): item a item eram 3-5 idas ao banco por produto,
    # o que da nuvem (EUA -> Supabase em SP) transformava minutos em horas.
    LOTE = 200
    buf_ean: list[dict] = []
    buf_per: list[dict] = []

    def descarregar() -> None:
        database.gravar_lote_ean(conn, mercado_id, buf_ean, agora)
        database.gravar_lote_pereciveis(conn, mercado_id, buf_per, agora)
        buf_ean.clear()
        buf_per.clear()

    try:
        for item in fonte:
            # Item fresco/por peso SEM EAN: vai pra tabela `pereciveis` (cruza por nome).
            if isinstance(item, ItemPerecivel):
                if item.cod_interno in vistos_per:
                    continue
                vistos_per.add(item.cod_interno)
                buf_per.append({
                    "cod_interno": item.cod_interno, "nome": item.nome, "preco": item.preco,
                    "unidade": item.unidade, "imagem_url": item.imagem_url, "url": item.url,
                    "so_no_site": item.so_no_site, "preco_de": item.preco_de,
                    "desconto_pct": item.desconto_pct, "oferta_tipo": item.oferta_tipo,
                })
                n_pereciveis += 1
            else:
                if item.ean in vistos:
                    continue  # 1 snapshot por EAN por rodada
                vistos.add(item.ean)

                # Normalização: extrai gramatura do nome -> R$/kg, R$/L ou R$/un.
                medida = normalizar.extrair_medida(item.nome)
                ppu, _ = normalizar.preco_por_unidade(item.preco, medida)
                buf_ean.append({
                    "ean": item.ean, "preco": item.preco, "preco_por_unidade": ppu,
                    "so_no_site": item.so_no_site, "preco_de": item.preco_de,
                    "desconto_pct": item.desconto_pct, "oferta_tipo": item.oferta_tipo,
                    "url": item.url,
                    "produto": (item.nome, item.marca, item.categoria,
                                medida.unidade if medida else item.unidade,
                                medida.qtd_base if medida else None, item.imagem_url),
                })
                n_snapshots += 1
            if len(buf_ean) + len(buf_per) >= LOTE:
                descarregar()
    finally:
        descarregar()   # nada do lote se perde se o coletor quebrar no meio

    conn.commit()
    if n_pereciveis:
        log.info("[%s] + %d perecíveis (frescos/por peso, sem EAN) na tabela à parte.",
                 cfg["slug"], n_pereciveis)
    return len(vistos), n_snapshots


def main(argv: list[str]) -> int:
    delay = float(os.getenv("DELAY_SEGUNDOS", "1.2"))
    timeout = float(os.getenv("TIMEOUT_SEGUNDOS", "20"))
    max_por_termo = int(os.getenv("MAX_POR_TERMO", "50"))
    contato = os.getenv("CONTATO", "contato@exemplo.com")
    db_path = os.getenv("DB_PATH", "dados/precos.db")

    mercados = _carregar_json(RAIZ / "config" / "mercados.json")["mercados"]
    categorias = _carregar_json(RAIZ / "config" / "categorias.json")["categorias"]

    # Semeia a tabela de categorias e monta os pares (termo, categoria_slug).
    # Só categorias disponíveis são coletadas; as "em breve" ficam no banco p/ o front.
    sementes: list[tuple[str, str]] = []
    for cat in categorias:
        if cat.get("disponivel", 1):
            sementes.extend((termo, cat["slug"]) for termo in cat.get("termos", []))

    filtro = set(argv[1:])
    alvos = [
        m for m in mercados
        if m.get("ativo") and m.get("classe") and (not filtro or m["slug"] in filtro)
    ]
    if not alvos:
        log.error("Nenhum mercado ativo/coletável (filtro=%s). Veja config/mercados.json.", filtro or "—")
        return 1

    conn = database.conectar(db_path)
    database.inicializar(conn)
    for cat in categorias:
        database.upsert_categoria(
            conn, slug=cat["slug"], nome=cat["nome"],
            disponivel=bool(cat.get("disponivel", 1)), ordem=cat.get("ordem", 0),
        )
    conn.commit()

    log.info("Coletando %d mercado(s): %s", len(alvos), ", ".join(m["slug"] for m in alvos))
    log.info("%d termos-semente | delay=%.1fs | max/termo=%d", len(sementes), delay, max_por_termo)

    total_snap = 0
    for cfg in alvos:
        log.info("── %s (%s) ──", cfg["nome"], cfg["plataforma"])
        try:
            n_prod, n_snap = coletar_mercado(
                conn, cfg, sementes,
                delay=delay, timeout=timeout, max_por_termo=max_por_termo, contato=contato,
            )
            total_snap += n_snap
            log.info("[%s] OK: %d EANs, %d snapshots de preço gravados.", cfg["slug"], n_prod, n_snap)
        except Exception:  # noqa: BLE001 — isolamento: um mercado não derruba os outros
            log.exception("[%s] FALHOU — seguindo para o próximo mercado.", cfg["slug"])

    database.rebuild_resumo(conn)  # atualiza n_mercados/menor_preco por produto (listagem rápida)
    log.info("Fim. produtos=%d | precos=%d | snapshots nesta rodada=%d",
             database.contar(conn, "produtos"), database.contar(conn, "precos"), total_snap)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
