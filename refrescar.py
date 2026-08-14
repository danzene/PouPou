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
from coletores.base import ItemColeta, MercadoIndisponivel
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
    col = ColetorVTEX(slug=slug, nome=cfg["nome"], url_base=base, delay=delay, contato=contato)
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
                for prod in prods:
                    item = col._parse_produto(prod)
                    if not isinstance(item, ItemColeta):
                        continue  # refresh é por EAN conhecido; ignora perecível eventual
                    medida = normalizar.extrair_medida(item.nome)
                    ppu, _ = normalizar.preco_por_unidade(item.preco, medida)
                    database.inserir_preco(conn, ean=item.ean, mercado_id=mid, preco=item.preco,
                        preco_por_unidade=ppu, coletado_em=agora, so_no_site=item.so_no_site,
                        preco_de=item.preco_de, desconto_pct=item.desconto_pct)
                    n += 1
                if (i // LOTE) % 10 == 0:
                    conn.commit()
        conn.commit()
        log.info("[%s] OK: %d de %d preços atualizados.", slug, n, len(eans))
    except MercadoIndisponivel as exc:
        conn.commit()
        log.warning("[%s] abortado (rate-limit) após %d atualizados: %s", slug, n, exc)
    return n


def main(argv: list[str]) -> int:
    delay = float(os.getenv("DELAY_REFRESH", "0.4"))
    contato = os.getenv("CONTATO", "contato@exemplo.com")
    db_path = os.getenv("DB_PATH", "dados/precos.db")
    mercados = json.loads((RAIZ / "config" / "mercados.json").read_text(encoding="utf-8"))["mercados"]

    filtro = set(argv[1:])
    alvos = [m for m in mercados if m.get("ativo") and m.get("plataforma") == "vtex"
             and (not filtro or m["slug"] in filtro)]
    if not alvos:
        log.error("Nenhum mercado VTEX ativo (filtro=%s).", filtro or "—")
        return 1

    conn = database.conectar(db_path)
    database.inicializar(conn)
    log.info("Refresh rápido de %d mercado(s) VTEX: %s", len(alvos), ", ".join(m["slug"] for m in alvos))
    total = sum(refrescar_vtex(conn, cfg, delay, contato) for cfg in alvos)
    database.rebuild_resumo(conn)
    conn.commit()
    conn.close()
    log.info("Fim do refresh. %d preços atualizados no total.", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
