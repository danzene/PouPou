"""Contagem de cobertura por mercado (quantos produtos com preço atual cada um tem).

Uso:
  python cobertura.py            # todos os mercados ativos, do maior p/ o menor
  python cobertura.py atacadao   # só o(s) slug(s) informados
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent
load_dotenv(RAIZ / ".env")

from db import database  # noqa: E402


def main(argv: list[str]) -> int:
    filtro = set(argv[1:])
    conn = database.conectar()
    cur = conn.cursor()
    cur.execute(
        "SELECT m.slug, m.nome, COUNT(pa.ean) AS n, "
        "       COUNT(pa.ean) FILTER (WHERE pa.preco_de IS NOT NULL) AS n_promo "
        "FROM mercados m LEFT JOIN precos_atual pa ON pa.mercado_id = m.id "
        "WHERE m.ativo = 1 GROUP BY m.slug, m.nome ORDER BY n DESC"
    )
    linhas = cur.fetchall()
    print(f"{'mercado':16} {'produtos':>9} {'em oferta':>10}")
    print("-" * 40)
    total = 0
    for r in linhas:
        slug, nome, n, n_promo = r["slug"], r["nome"], r["n"], r["n_promo"]
        if filtro and slug not in filtro:
            continue
        total += n
        print(f"{slug:16} {n:>9} {n_promo:>10}")
    print("-" * 40)
    print(f"{'TOTAL':16} {total:>9}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
