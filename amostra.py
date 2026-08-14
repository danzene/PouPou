"""Query de amostra da Fase 1: prova que produtos/precos foram populados
e que o histórico append-only funciona. Uso: python amostra.py"""
import os
import sqlite3

from dotenv import load_dotenv

load_dotenv()
c = sqlite3.connect(os.getenv("DB_PATH", "dados/precos.db"))
c.row_factory = sqlite3.Row

print("=== CONTAGENS ===")
prod = c.execute("SELECT COUNT(*) n FROM produtos").fetchone()["n"]
prec = c.execute("SELECT COUNT(*) n FROM precos").fetchone()["n"]
merc = c.execute("SELECT COUNT(*) n FROM mercados").fetchone()["n"]
print(f"  mercados={merc}  produtos(EAN únicos)={prod}  precos(snapshots)={prec}\n")

print("=== AMOSTRA: 8 produtos com último preço (Savegnago, preço ONLINE) ===")
sql = """
SELECT pr.ean, substr(pr.nome_canonico, 1, 46) nome, pr.marca, x.preco
FROM produtos pr
JOIN precos x ON x.ean = pr.ean
JOIN (SELECT ean, MAX(coletado_em) m FROM precos GROUP BY ean) u
     ON u.ean = x.ean AND u.m = x.coletado_em
WHERE pr.marca IS NOT NULL
ORDER BY RANDOM() LIMIT 8
"""
for r in c.execute(sql):
    print(f"  {r['ean']:>14}  R$ {r['preco']:>7.2f}  {r['nome']:<47} [{r['marca']}]")

print("\n=== HISTÓRICO: um EAN com 2 snapshots (2 coletas) ===")
row = c.execute("SELECT ean FROM precos GROUP BY ean HAVING COUNT(*)>=2 LIMIT 1").fetchone()
if row:
    ean = row["ean"]
    nome = c.execute("SELECT nome_canonico FROM produtos WHERE ean=?", (ean,)).fetchone()[0]
    print(f"  EAN {ean}  ({nome[:44]})")
    for r in c.execute(
        "SELECT preco, origem, coletado_em FROM precos WHERE ean=? ORDER BY coletado_em", (ean,)
    ):
        print(f"    R$ {r['preco']:>7.2f}  origem={r['origem']}  em {r['coletado_em']}")
else:
    print("  (rode a coleta 2x para ver o histórico)")
c.close()
