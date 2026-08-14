"""Manutenção do banco: mantém o histórico enxuto pra o app seguir leve mesmo
coletando todo dia. Roda automático no fim da coleta diária (ver coleta_*.bat).

Uso:
    python manutencao.py            # poda (mantém 120 dias) + VACUUM
    python manutencao.py 90         # poda mantendo N dias

O que faz:
- colapsa snapshots do mesmo dia (sobra 1 por ean+mercado+dia);
- descarta histórico mais antigo que N dias (nunca o último de cada par);
- VACUUM pra devolver o espaço em disco ao sistema.
`precos_atual` (preço quente do app) nunca é tocado — a listagem/cesta seguem iguais.
"""
from __future__ import annotations

import logging
import os
import sys
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
log = logging.getLogger("manutencao")


def _mb(caminho: Path) -> float:
    try:
        return caminho.stat().st_size / 1e6
    except OSError:
        return 0.0


def main(argv: list[str]) -> int:
    manter_dias = int(argv[1]) if len(argv) > 1 else 90
    db_path = os.getenv("DB_PATH", "dados/precos.db")
    caminho = Path(db_path)
    if not caminho.is_absolute():
        caminho = RAIZ / caminho

    tam_antes = _mb(caminho)
    conn = database.conectar(db_path)
    database.inicializar(conn)

    log.info("Podando histórico (mantendo %d dias)…", manter_dias)
    r = database.podar_historico(conn, manter_dias=manter_dias)
    log.info("precos: %d -> %d  (removidos %d)", r["antes"], r["depois"], r["removidos"])

    log.info("VACUUM (recuperando espaço em disco)…")
    conn.isolation_level = None  # VACUUM não roda dentro de transação
    conn.execute("VACUUM")
    conn.close()

    tam_depois = _mb(caminho)
    log.info("Banco: %.1f MB -> %.1f MB  (liberado %.1f MB)",
             tam_antes, tam_depois, max(0.0, tam_antes - tam_depois))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
