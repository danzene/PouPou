"""Refresh RÁPIDO de preços (só os produtos que JÁ estão no banco).

Diferente de `coletar.py` (que varre o catálogo inteiro pra DESCOBRIR produtos novos,
lento ~15 min/mercado), este reconsulta em LOTE os EANs já conhecidos de cada mercado
VTEX — via `fq=alternateIds_Ean` (~40 por requisição) — e atualiza preço/desconto em
poucos minutos. Ideal pra rodar com FREQUÊNCIA (ex.: de 3 em 3h) e manter o preço
sempre fresco entre as varreduras completas diárias.

Uso:
    python refrescar.py                # todos os mercados VTEX ativos
    python refrescar.py savegnago      # só o(s) slug(s) informados

Robusto: retry+backoff e disjuntor (MercadoIndisponivel) já vêm do coletor VTEX; um
mercado rate-limitado é abortado rápido (mantém o preço antigo, que o app marca como
'há X dias') sem travar os outros.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from analise import normalizar
from coletores.base import ItemColeta, MercadoIndisponivel, robots_permite
from coletores.convertiez import ColetorConvertiez
from coletores.vtex import ColetorVTEX, _headers
from db import database

RAIZ = Path(__file__).resolve().parent
load_dotenv(RAIZ / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("refrescar")

LOTE = 40  # EANs por requisição (janela _to da VTEX é 50)


def refrescar_vtex(conn, cfg: dict, delay: float, contato: str) -> int:
    slug, base = cfg["slug"], cfg["url_base"].rstrip("/")
    row = conn.execute("SELECT id FROM mercados WHERE slug = ?", (slug,)).fetchone()
    if not row:
        return 0
    mid = row["id"]
    eans = [r["ean"] for r in conn.execute("SELECT ean FROM precos_atual WHERE mercado_id = ?", (mid,))]
    if not eans:
        log.info("[%s] sem EANs conhecidos ainda (rode coletar.py antes).", slug)
        return 0
    col = ColetorVTEX(slug=slug, nome=cfg["nome"], url_base=base, delay=delay, contato=contato,
                      **cfg.get("params", {}))
    agora = datetime.now(timezone.utc).isoformat()
    n = 0
    try:
        with httpx.Client(headers=_headers(contato), timeout=col.timeout, follow_redirects=True) as client:
            for i in range(0, len(eans), LOTE):
                fq = "&".join(f"fq=alternateIds_Ean:{e}" for e in eans[i:i + LOTE])
                r = col._get(client, f"{base}/api/catalog_system/pub/products/search?{fq}&_from=0&_to=49")
                time.sleep(delay)
                if r is None:
                    continue
                try:
                    prods = r.json()
                except ValueError:
                    continue
                lote = []
                for prod in prods:
                    item = col._parse_produto(prod)
                    if not isinstance(item, ItemColeta):
                        continue  # refresh é por EAN conhecido; ignora perecível eventual
                    medida = normalizar.extrair_medida(item.nome)
                    ppu, _ = normalizar.preco_por_unidade(item.preco, medida)
                    lote.append({"ean": item.ean, "preco": item.preco, "preco_por_unidade": ppu,
                                 "so_no_site": item.so_no_site, "preco_de": item.preco_de,
                                 "desconto_pct": item.desconto_pct, "oferta_tipo": item.oferta_tipo})
                # em lote: da nuvem (EUA -> banco em SP) item a item custava ~3 idas por produto
                n += database.gravar_lote_ean(conn, mid, lote, agora)
                if (i // LOTE) % 10 == 0:
                    conn.commit()
        conn.commit()
        log.info("[%s] OK: %d de %d preços atualizados.", slug, n, len(eans))
    except MercadoIndisponivel as exc:
        conn.commit()
        log.warning("[%s] abortado (rate-limit) após %d atualizados: %s", slug, n, exc)
    return n


def refrescar_convertiez(conn, cfg: dict, delay: float, contato: str) -> int:
    """Refresh do Convertiez (Super Pague Menos): re-busca as PÁGINAS dos produtos já
    conhecidos (URLs na tabela `links`) e atualiza preço/desconto. É 1 requisição por
    produto (o EAN/preço só vêm na página), mas limitado aos EANs conhecidos — mantém o
    preço fresco entre as varreduras completas (que rotacionam o catálogo em ~1 semana e
    deixariam promoções dias atrasadas). Preço promocional passa a refletir o site."""
    slug, base = cfg["slug"], cfg["url_base"].rstrip("/")
    row = conn.execute("SELECT id FROM mercados WHERE slug = ?", (slug,)).fetchone()
    if not row:
        return 0
    mid = row["id"]
    rows = conn.execute(
        "SELECT l.ean, l.url FROM links l "
        "WHERE l.mercado_id = ? AND l.url IS NOT NULL AND EXISTS "
        "(SELECT 1 FROM precos_atual pa WHERE pa.ean = l.ean AND pa.mercado_id = l.mercado_id)",
        (mid,)).fetchall()
    if not rows:
        log.info("[%s] sem URLs conhecidas (rode coletar.py antes).", slug)
        return 0
    col = ColetorConvertiez(slug=slug, nome=cfg["nome"], url_base=base, delay=delay,
                            contato=contato, **cfg.get("params", {}))
    headers = col._headers()
    if not robots_permite(base, "/produto/p", headers["User-Agent"], headers):
        log.warning("[%s] robots.txt bloqueia páginas de produto — pulando refresh.", slug)
        return 0
    agora = datetime.now(timezone.utc).isoformat()
    n = 0
    with httpx.Client(headers=headers, timeout=col.timeout, follow_redirects=True) as client:
        for i, r in enumerate(rows):
            url = r["url"]
            href = url[len(base):] if url.startswith(base) else None
            if not href:
                continue
            item = col._produto(client, href, None)  # re-busca a página; EAN/preço frescos
            if not isinstance(item, ItemColeta):
                continue  # refresh é por EAN; ignora perecível/None (página fora do ar)
            medida = normalizar.extrair_medida(item.nome)
            ppu, _ = normalizar.preco_por_unidade(item.preco, medida)
            database.inserir_preco(conn, ean=item.ean, mercado_id=mid, preco=item.preco,
                preco_por_unidade=ppu, coletado_em=agora, so_no_site=item.so_no_site,
                preco_de=item.preco_de, desconto_pct=item.desconto_pct, oferta_tipo=item.oferta_tipo)
            n += 1
            if i % 50 == 0:
                conn.commit()
    conn.commit()
    log.info("[%s] OK: %d de %d preços atualizados.", slug, n, len(rows))
    return n


# plataforma -> função de refresh rápido (só os produtos já conhecidos)
REFRESHERS = {
    "vtex": refrescar_vtex,
    "convertiez": refrescar_convertiez,
}


def main(argv: list[str]) -> int:
    delay = float(os.getenv("DELAY_REFRESH", "0.4"))
    contato = os.getenv("CONTATO", "contato@exemplo.com")
    db_path = os.getenv("DB_PATH", "dados/precos.db")
    mercados = json.loads((RAIZ / "config" / "mercados.json").read_text(encoding="utf-8"))["mercados"]

    filtro = set(argv[1:])
    alvos = [m for m in mercados if m.get("ativo") and m.get("plataforma") in REFRESHERS
             and (not filtro or m["slug"] in filtro)]
    if not alvos:
        log.error("Nenhum mercado com refresh disponível (filtro=%s). Plataformas: %s",
                  filtro or "—", ", ".join(REFRESHERS))
        return 1

    conn = database.conectar(db_path)
    database.inicializar(conn)
    log.info("Refresh rápido de %d mercado(s): %s", len(alvos), ", ".join(m["slug"] for m in alvos))
    total = 0
    vazios: list[str] = []   # mercado com EANs conhecidos mas 0 atualizados -> termina com erro
    for cfg in alvos:
        fn = REFRESHERS[cfg["plataforma"]]
        try:
            n = fn(conn, cfg, delay, contato)
            total += n
            if n == 0:
                log.error("[%s] refresh VAZIO: 0 preços atualizados. Site mudou ou bloqueou?", cfg["slug"])
                vazios.append(cfg["slug"])
        except MercadoIndisponivel as exc:
            conn.commit()
            log.warning("[%s] abortado (rate-limit): %s", cfg["slug"], exc)
        except Exception:  # noqa: BLE001 — um mercado não derruba os outros
            log.exception("[%s] FALHOU no refresh — seguindo.", cfg["slug"])
            vazios.append(cfg["slug"])
    database.rebuild_resumo(conn)
    conn.commit()
    conn.close()
    log.info("Fim do refresh. %d preços atualizados no total.", total)
    if vazios:   # erro => GitHub marca a rodada como falha e avisa por e-mail
        log.error("ATENÇÃO: refresh sem resultado em: %s", ", ".join(vazios))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
