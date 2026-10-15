"""Tira do app os preços que a loja não confirma mais (produto indisponível/fora do site).

Os coletores já ignoram item esgotado — mas aí o produto simplesmente some da coleta e o
ÚLTIMO preço ficava em `precos_atual`/`pereciveis` pra sempre, aparecendo no app como se
fosse de hoje (ex.: oferta de clube de um item 'indisponível' no site, visto há 16 dias).

Regra: remove o que não foi visto há mais de N dias (padrão 3), MAS só nos mercados cuja
coleta está viva — pelo menos metade do catálogo confirmada nas últimas 24h. Assim uma
coleta quebrada ou pela metade nunca esvazia um mercado inteiro (isso aparece na saúde
da coleta do /gestao, não some em silêncio).

O histórico (`precos`, `pereciveis_hist`) não é tocado; se o produto voltar ao site, a
próxima coleta o recoloca sozinha.

Uso:
    python limpar_indisponiveis.py        # N = 3 dias
    python limpar_indisponiveis.py 5      # N = 5 dias
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from db import database

RAIZ = Path(__file__).resolve().parent
load_dotenv(RAIZ / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("limpar_indisponiveis")

MIN_FRESCO = 0.5   # fração do catálogo confirmada em 24h pra considerar a coleta "viva"


def _corte(horas: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat()


def main(argv: list[str]) -> int:
    dias = float(argv[1]) if len(argv) > 1 else 3.0
    conn = database.conectar(os.getenv("DB_PATH", "dados/precos.db"))
    database.inicializar(conn)
    c24, cvelho = _corte(24), _corte(24 * dias)

    removidos = 0
    for m in conn.execute("SELECT id, slug FROM mercados WHERE ativo = 1").fetchall():
        for tabela in ("precos_atual", "pereciveis"):
            r = conn.execute(
                f"SELECT COUNT(*) AS n, SUM(CASE WHEN coletado_em >= ? THEN 1 ELSE 0 END) AS f "
                f"FROM {tabela} WHERE mercado_id = ?", (c24, m["id"])).fetchone()
            n, f = (r["n"] or 0), (r["f"] or 0)
            if not n:
                continue
            if f < MIN_FRESCO * n:
                log.info("[%s] %s: só %d de %d confirmados em 24h -> coleta incompleta, não limpo",
                         m["slug"], tabela, f, n)
                continue
            cur = conn.execute(f"DELETE FROM {tabela} WHERE mercado_id = ? AND coletado_em < ?",
                               (m["id"], cvelho))
            k = cur.rowcount or 0
            removidos += k
            if k:
                log.info("[%s] %s: %d itens sem ver há +%g dias removidos (de %d)",
                         m["slug"], tabela, k, dias, n)

    if removidos:
        database.rebuild_resumo(conn)   # n_mercados/menor_preco sem os itens removidos
    conn.commit()
    conn.close()
    log.info("Fim. %d itens indisponíveis tirados do app.", removidos)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
