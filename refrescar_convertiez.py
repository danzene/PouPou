"""Refresh dos itens JÁ CONHECIDOS do Convertiez (Super Pague Menos).

A varredura por sitemap (coletar.py superpaguemenos) é ROTATIVA — cobre o catálogo ao
longo de vários dias, então um item pode ficar dias sem re-consulta e o preço defasa.
Este script re-busca a URL de CADA item já no banco e atualiza preço/promo, mantendo os
itens conhecidos frescos no DIA. Uma requisição por item (o preço só está na página),
então roda devagar — ideal 1x/dia (de madrugada).

Uso:
    python refrescar_convertiez.py            # todos os itens conhecidos do SPM
    python refrescar_convertiez.py --frescos  # só os perecíveis (rápido)
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from analise import normalizar
from coletores.base import ItemColeta, ItemPerecivel
from coletores.convertiez import BloqueioDetectado, ColetorConvertiez
from db import database

RAIZ = Path(__file__).resolve().parent
load_dotenv(RAIZ / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("refrescar_convertiez")

SLUG = "superpaguemenos"


def main(argv: list[str]) -> int:
    so_frescos = "--frescos" in argv
    contato = os.getenv("CONTATO", "contato@exemplo.com")
    db_path = os.getenv("DB_PATH", "dados/precos.db")
    conn = database.conectar(db_path)
    database.inicializar(conn)

    row = conn.execute("SELECT id FROM mercados WHERE slug = ?", (SLUG,)).fetchone()
    if not row:
        log.error("Mercado %s não existe no banco.", SLUG)
        return 1
    mid = row["id"]

    urls = [r["url"] for r in conn.execute(
        "SELECT url FROM pereciveis WHERE mercado_id = ? AND url IS NOT NULL", (mid,))]
    if not so_frescos:
        urls += [r["url"] for r in conn.execute(
            "SELECT url FROM links WHERE mercado_id = ? AND url IS NOT NULL", (mid,))]
    urls = list(dict.fromkeys(urls))  # dedup preservando ordem
    if not urls:
        log.info("Sem URLs conhecidas do %s (rode coletar.py antes).", SLUG)
        return 0

    # FATIA ROTATIVA: refresca no máximo N itens por rodada (rotação por offset), pra NUNCA
    # martelar o site com ~19 mil requisições de uma vez — foi isso que bloqueou o IP.
    max_rodada = int(os.getenv("CONVERTIEZ_REFRESH_MAX", "2500"))
    off_file = RAIZ / "dados" / "convertiez_refresh_offset.json"
    try:
        offset = int(json.loads(off_file.read_text(encoding="utf-8"))["offset"]) % len(urls)
    except Exception:  # noqa: BLE001
        offset = 0
    if len(urls) > max_rodada:
        fatia = urls[offset:offset + max_rodada]
        if len(fatia) < max_rodada:
            fatia += urls[:max_rodada - len(fatia)]  # dá a volta
        proximo = (offset + max_rodada) % len(urls)
    else:
        fatia, proximo = urls, 0

    cfg = next(m for m in json.loads((RAIZ / "config" / "mercados.json").read_text(encoding="utf-8"))["mercados"]
               if m["slug"] == SLUG)
    col = ColetorConvertiez(slug=SLUG, nome=cfg["nome"], url_base=cfg["url_base"],
                            delay=float(os.getenv("DELAY_CONVERTIEZ", "2.0")), contato=contato)
    base = col.url_base
    agora = datetime.now(timezone.utc).isoformat()
    n = falhas = 0
    bloqueado = False
    log.info("Refrescando %d de %d itens do %s (offset %d, fatia rotativa, delay=%.1fs)…",
             len(fatia), len(urls), SLUG, offset, col.delay)
    with httpx.Client(headers=col._headers(), timeout=col.timeout, follow_redirects=True) as client:
        for u in fatia:
            href = u[len(base):] if u.startswith(base) else u
            try:
                item = col._produto(client, href, None)
            except BloqueioDetectado as exc:
                log.warning("PAROU (disjuntor): %s", exc)
                bloqueado = True
                break
            except Exception as exc:  # noqa: BLE001
                falhas += 1
                item = None
                if falhas <= 5:
                    log.warning("falha em %s: %s", href, exc)
            if item is None:
                continue
            if isinstance(item, ItemPerecivel):
                database.upsert_perecivel(
                    conn, mercado_id=mid, cod_interno=item.cod_interno, nome=item.nome,
                    preco=item.preco, unidade=item.unidade, imagem_url=item.imagem_url,
                    url=item.url, coletado_em=agora, so_no_site=item.so_no_site,
                    preco_de=item.preco_de, desconto_pct=item.desconto_pct)
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
                    desconto_pct=item.desconto_pct)
            n += 1
            if n % 50 == 0:
                conn.commit()
                log.info("  %d/%d atualizados…", n, len(fatia))
    conn.commit()
    database.rebuild_resumo(conn)
    conn.commit()
    # só avança o offset se NÃO foi bloqueado (bloqueou -> repete a mesma fatia na próxima rodada,
    # quando o site já tiver liberado, em vez de pular esses itens).
    if not bloqueado:
        try:
            off_file.parent.mkdir(parents=True, exist_ok=True)
            off_file.write_text(json.dumps({"offset": proximo}), encoding="utf-8")
        except OSError:
            pass
    conn.close()
    log.info("OK: %d itens do %s atualizados (%d falhas)%s.",
             n, SLUG, falhas, "  [BLOQUEADO — rodada abortada pelo disjuntor]" if bloqueado else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
