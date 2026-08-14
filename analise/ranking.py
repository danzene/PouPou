"""Ranking de preços por produto (EAN), do mais barato ao mais caro entre mercados.

Usa sempre o ÚLTIMO snapshot de cada par (ean, mercado) — o histórico fica
intacto em `precos`, mas a comparação olha o preço mais recente de cada loja.

Uso:
    python -m analise.ranking "café brasileiro"     # busca por nome
    python -m analise.ranking 7891018427643         # busca por EAN
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unicodedata

from dotenv import load_dotenv


def _unaccent(s: str | None) -> str | None:
    """Remove acentos e baixa a caixa: 'Café' -> 'cafe'. Para busca tolerante."""
    if s is None:
        return None
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _conn() -> sqlite3.Connection:
    load_dotenv()
    c = sqlite3.connect(os.getenv("DB_PATH", "dados/precos.db"))
    c.row_factory = sqlite3.Row
    c.create_function("unaccent", 1, _unaccent, deterministic=True)
    return c


def _eh_ean(q: str) -> bool:
    q = q.strip()
    return q.isdigit() and len(q) in {8, 12, 13, 14}


def buscar_produtos(conn: sqlite3.Connection, termo: str, limite: int = 15) -> list[sqlite3.Row]:
    """Produtos cujo nome contém TODOS os tokens do termo (acento/ordem-insensível),
    ordenados por RELEVÂNCIA: nome que começa com a busca > 1º token como palavra
    inteira > nome mais curto (mais específico) > nº de mercados. Assim buscar
    'picanha' traz 'Picanha ...' antes de 'Hambúrguer sabor Picanha', e 'alcatra'
    não deixa 'Conhaque de Alcatrão' no topo."""
    full = (_unaccent(termo) or "").strip()
    tokens = [t for t in full.split() if t]
    if not tokens:
        return []
    cond = " AND ".join(["unaccent(pr.nome_canonico) LIKE ?"] * len(tokens))
    params = [f"%{t}%" for t in tokens]
    return conn.execute(
        f"""
        SELECT pr.ean, pr.nome_canonico, pr.marca,
               COUNT(DISTINCT pa.mercado_id) AS n_mercados
        FROM produtos pr
        JOIN precos_atual pa ON pa.ean = pr.ean
        WHERE {cond}
        GROUP BY pr.ean
        ORDER BY
          (unaccent(pr.nome_canonico) LIKE ?) DESC,                     -- começa com a busca
          (' ' || unaccent(pr.nome_canonico) || ' ' LIKE ?) DESC,       -- 1º token = palavra inteira
          n_mercados DESC, length(pr.nome_canonico), pr.nome_canonico
        LIMIT ?
        """,
        (*params, f"{full}%", f"% {tokens[0]} %", limite),
    ).fetchall()


def ranking_por_ean(conn: sqlite3.Connection, ean: str) -> list[sqlite3.Row]:
    """Último preço de cada mercado para um EAN, do mais barato ao mais caro."""
    return conn.execute(
        """
        SELECT m.nome AS mercado, p.preco, p.preco_por_unidade,
               pr.unidade, p.origem, p.coletado_em
        FROM precos p
        JOIN mercados m  ON m.id = p.mercado_id
        JOIN produtos pr ON pr.ean = p.ean
        JOIN (
            SELECT ean, mercado_id, MAX(coletado_em) AS mx
            FROM precos WHERE ean = ? GROUP BY ean, mercado_id
        ) u ON u.ean = p.ean AND u.mercado_id = p.mercado_id AND u.mx = p.coletado_em
        WHERE p.ean = ?
        ORDER BY p.preco ASC
        """,
        (ean, ean),
    ).fetchall()


def _fmt_ppu(preco_por_unidade, unidade) -> str:
    if preco_por_unidade is None:
        return "—"
    rot = {"kg": "R$/kg", "l": "R$/L", "un": "R$/un"}.get(unidade, "R$/?")
    return f"R$ {preco_por_unidade:.2f} {rot}"


def imprimir_ranking(conn: sqlite3.Connection, ean: str) -> None:
    prod = conn.execute("SELECT nome_canonico, marca FROM produtos WHERE ean = ?", (ean,)).fetchone()
    if not prod:
        print(f"EAN {ean} não encontrado.")
        return
    linhas = ranking_por_ean(conn, ean)
    print(f"\n■ {prod['nome_canonico']}  [{prod['marca'] or '—'}]  (EAN {ean})")
    if not linhas:
        print("  sem preços.")
        return
    print(f"  {'#':<2}{'MERCADO':<26}{'PREÇO':>10}   {'POR UNIDADE':<16} {'COLETA':<20}")
    for i, r in enumerate(linhas, 1):
        marca_barato = " ◄ mais barato" if i == 1 and len(linhas) > 1 else ""
        data = r["coletado_em"][:16].replace("T", " ")
        print(f"  {i:<2}{r['mercado']:<26}{('R$ ' + format(r['preco'], '.2f')):>10}   "
              f"{_fmt_ppu(r['preco_por_unidade'], r['unidade']):<16} {data:<20}{marca_barato}")
    print("  (preço ONLINE, não de balcão físico)")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    termo = " ".join(argv[1:])
    conn = _conn()

    if _eh_ean(termo):
        imprimir_ranking(conn, termo)
        conn.close()
        return 0

    achados = buscar_produtos(conn, termo)
    if not achados:
        print(f"Nada encontrado para '{termo}'.")
        conn.close()
        return 1

    print(f"{len(achados)} produto(s) para '{termo}' (nº de mercados com preço):")
    for r in achados:
        print(f"  [{r['n_mercados']}x] {r['ean']:>14}  {r['nome_canonico'][:52]}")

    # Mostra o ranking do melhor caso de comparação (mais mercados).
    melhor = achados[0]
    if melhor["n_mercados"] >= 2:
        imprimir_ranking(conn, melhor["ean"])
    else:
        print("\n(nenhum produto desse termo aparece em 2+ mercados ainda — sem comparação cruzada)")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
