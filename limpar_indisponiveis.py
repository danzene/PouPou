"""Tira do app os preços que a loja não confirma mais (produto indisponível/fora do site).

Os coletores já ignoram item esgotado — mas aí o produto simplesmente some da coleta e o
ÚLTIMO preço ficava em `precos_atual`/`pereciveis` pra sempre, aparecendo no app como se
fosse de hoje (ex.: oferta de clube de um item 'indisponível' no site, visto há 16 dias).

Regra: remove o que não foi visto há mais de N dias (padrão 1,5 = 36h: toda coleta roda
ao menos 1x/dia, então 36h = perdeu a rodada do dia) nos mercados cuja coleta está viva
— pelo menos metade do catálogo confirmada nas últimas 24h. Em mercado com coleta
incompleta/quebrada, o corte é o teto absoluto de LIMITE_DIAS (3): nenhum preço sem
confirmação passa disso no app; se a coleta parar de vez, o mercado some do app e a
saúde da coleta no /gestao mostra o problema.

O histórico (`precos`, `pereciveis_hist`) não é tocado; se o produto voltar ao site, a
próxima coleta o recoloca sozinha.

Uso:
    python limpar_indisponiveis.py        # N = 1,5 dia (36h)
    python limpar_indisponiveis.py 3      # N = 3 dias
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
LIMITE_DIAS = 3.0  # teto absoluto: nem com coleta incompleta um preço passa disso no app


def _corte(horas: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat()


def main(argv: list[str]) -> int:
    dias = float(argv[1]) if len(argv) > 1 else 1.5
    conn = database.conectar(os.getenv("DB_PATH", "dados/precos.db"))
    database.inicializar(conn)
    c24, cvelho, climite = _corte(24), _corte(24 * dias), _corte(24 * LIMITE_DIAS)

    removidos = 0
    for m in conn.execute("SELECT id, slug FROM mercados WHERE ativo = 1").fetchall():
        # coleta viva é medida no MERCADO todo (EAN + frescos): por tabela, um punhado de
        # frescos velhos (ex.: 3 de 15 no Pão de Açúcar) travava a limpeza de um mercado 100% em dia
        n = f = 0
        for tabela in ("precos_atual", "pereciveis"):
            r = conn.execute(
                f"SELECT COUNT(*) AS n, SUM(CASE WHEN coletado_em >= ? THEN 1 ELSE 0 END) AS f "
                f"FROM {tabela} WHERE mercado_id = ?", (c24, m["id"])).fetchone()
            n, f = n + (r["n"] or 0), f + (r["f"] or 0)
        if not n:
            continue
        viva = f >= MIN_FRESCO * n
        # coleta incompleta: não aplica o corte curto, mas NUNCA mostra preço sem
        # confirmação há mais de LIMITE_DIAS (ex.: Tenda com a árvore de categorias fora do ar)
        corte, prazo = (cvelho, dias) if viva else (climite, LIMITE_DIAS)
        if not viva:
            log.info("[%s] só %d de %d confirmados em 24h -> coleta incompleta; só tiro o que "
                     "passou de %g dias", m["slug"], f, n, LIMITE_DIAS)
        for tabela in ("precos_atual", "pereciveis"):
            cur = conn.execute(f"DELETE FROM {tabela} WHERE mercado_id = ? AND coletado_em < ?",
                               (m["id"], corte))
            k = cur.rowcount or 0
            removidos += k
            if k:
                log.info("[%s] %s: %d itens sem ver há +%g dias removidos",
                         m["slug"], tabela, k, prazo)

    if removidos:
        database.rebuild_resumo(conn)   # n_mercados/menor_preco sem os itens removidos
    conn.commit()
    conn.close()
    log.info("Fim. %d itens indisponíveis tirados do app.", removidos)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
