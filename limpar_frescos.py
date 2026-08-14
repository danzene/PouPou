"""Limpeza única da tabela `pereciveis`: remove itens que NÃO são frescos de verdade
(ração, bebida, industrializado, limpeza, prato pronto...), segundo db.database.eh_falso_fresco.

As coletas novas já barram esses itens na entrada; este script apaga os que entraram
ANTES da regra existir. Idempotente — pode rodar quantas vezes quiser.

Uso:
    python limpar_frescos.py            # aplica a limpeza
    python limpar_frescos.py --dry      # só mostra o que removeria
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from db import database

RAIZ = Path(__file__).resolve().parent
load_dotenv(RAIZ / ".env")


def main(argv: list[str]) -> int:
    dry = "--dry" in argv
    db_path = os.getenv("DB_PATH", "dados/precos.db")
    conn = database.conectar(db_path)

    rows = conn.execute(
        """
        SELECT p.rowid AS id, p.nome, p.categoria, m.slug AS mercado
        FROM pereciveis p JOIN mercados m ON m.id = p.mercado_id
        """
    ).fetchall()

    remover = [r for r in rows if database.eh_falso_fresco(r["nome"])]
    print(f"pereciveis total: {len(rows)} | a remover (não-fresco): {len(remover)}")

    por_mercado: dict[str, int] = {}
    por_cat: dict[str, int] = {}
    for r in remover:
        por_mercado[r["mercado"]] = por_mercado.get(r["mercado"], 0) + 1
        por_cat[r["categoria"]] = por_cat.get(r["categoria"], 0) + 1
    print("  por mercado:", dict(sorted(por_mercado.items(), key=lambda x: -x[1])))
    print("  por categoria:", dict(sorted(por_cat.items(), key=lambda x: -x[1])))

    if dry:
        print("\n[dry-run] nada removido. Amostra do que sairia:")
        for r in remover[:30]:
            print(f"  [{r['mercado']}/{r['categoria']}] {r['nome']}")
        return 0

    ids = [r["id"] for r in remover]
    for i in range(0, len(ids), 500):
        lote = ids[i:i + 500]
        conn.execute(f"DELETE FROM pereciveis WHERE rowid IN ({','.join('?' * len(lote))})", lote)
    conn.commit()
    depois = conn.execute("SELECT COUNT(*) FROM pereciveis").fetchone()[0]
    print(f"\nOK: removidos {len(ids)}. pereciveis agora: {depois}")

    print("\nFrescos por mercado (depois):")
    for r in conn.execute(
        "SELECT m.slug, COUNT(*) n FROM pereciveis p JOIN mercados m ON m.id=p.mercado_id "
        "GROUP BY m.slug ORDER BY n DESC"
    ):
        print(f"  {r['slug']:16} {r['n']}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
