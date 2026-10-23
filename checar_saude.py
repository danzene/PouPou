"""Vigia da saúde da coleta: termina com ERRO se algum mercado ativo parou de atualizar.

Roda 1x/dia na nuvem (workflow saude-coleta.yml, disparado pelo pg_cron). Saindo com
erro, o GitHub marca a rodada como falha e manda e-mail pro dono do repositório — assim
um mercado nunca mais fica dias parado sem ninguém saber (o Pague Menos trocou de site
em out/2026 e ficou ~2 dias em 0% com todos os robôs dizendo "sucesso").

Alarme, por mercado ativo de config/mercados.json:
- nenhum preço no banco;
- última coleta há mais de MAX_HORAS (30h: toda coleta roda ao menos 1x/dia);
- menos de MIN_PCT (50%) do catálogo vivo (visto em 7 dias) confirmado nas últimas 24h —
  a mesma conta da "saúde da coleta" do /gestao.

Uso:
    python checar_saude.py
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from db import database

RAIZ = Path(__file__).resolve().parent
load_dotenv(RAIZ / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("saude")

MAX_HORAS = 30
MIN_PCT = 50


def main() -> int:
    mercados = json.loads((RAIZ / "config" / "mercados.json").read_text(encoding="utf-8"))["mercados"]
    ativos = {m["slug"]: m["nome"] for m in mercados if m.get("ativo") and m.get("classe")}
    agora = datetime.now(timezone.utc)
    c24 = (agora - timedelta(hours=24)).isoformat()
    c7d = (agora - timedelta(days=7)).isoformat()

    conn = database.conectar()
    try:
        rows = {r["slug"]: r for r in conn.execute(
            """SELECT m.slug,
                      SUM(CASE WHEN pa.coletado_em >= ? THEN 1 ELSE 0 END) AS frescos_24h,
                      SUM(CASE WHEN pa.coletado_em >= ? THEN 1 ELSE 0 END) AS vistos_7d,
                      MAX(pa.coletado_em) AS ultima
               FROM mercados m LEFT JOIN precos_atual pa ON pa.mercado_id = m.id
               GROUP BY m.slug""", (c24, c7d)).fetchall()}
    finally:
        conn.close()

    problemas = []
    for slug, nome in ativos.items():
        r = rows.get(slug)
        ultima = r["ultima"] if r else None
        if not ultima:
            problemas.append(f"{nome}: nenhum preço no banco")
            continue
        horas = (agora - datetime.fromisoformat(str(ultima))).total_seconds() / 3600
        f, v = (r["frescos_24h"] or 0), (r["vistos_7d"] or 0)
        pct = round(100 * f / v) if v else 0
        log.info("%-18s %3d%% atualizado em 24h | última coleta há %.0fh", nome, pct, horas)
        if horas > MAX_HORAS:
            problemas.append(f"{nome}: sem coleta há {horas:.0f}h")
        elif pct < MIN_PCT:
            problemas.append(f"{nome}: só {pct}% atualizado em 24h")

    if problemas:
        for p in problemas:
            log.error("ALERTA — %s", p)
            print(f"::error::{p}")   # aparece em destaque no resumo da rodada do GitHub
        return 1
    log.info("Tudo certo: %d mercados ativos atualizados.", len(ativos))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
