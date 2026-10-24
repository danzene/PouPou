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


import re as _re

# Tipo "básico" de cada item do dia a dia: quem busca "leite" quer leite de caixinha, não
# condensado/de coco/fermentado. 'prefere' sobe, 'derivado' desce (ordem; nada some).
INTENCAO_BUSCA = {
    "leite":   (r"longa vida|\buht\b|integral|desnatado|semidesnatado|pasteurizado|tipo a\b|zero lactose",
                r"condensado|de coco|fermentado|de rosas|em po|achocolat|aromatizad|vegetal|amendoa|aveia|soja"),
    "arroz":   (r"tipo 1|agulhinha|branco|polido|parboilizado|integral",
                r"carreteiro|instantaneo|risoto|panelinha|doce|sabor|culinaria|arbori|basmati|sushi|negro|grao"),
    "feijao":  (r"carioca|preto|tipo 1|carioquinha",
                r"cozido|temperado|conserva|vapor|feijoada|caldo|branco|fradinho|rajado|bolinha"),
    "cafe":    (r"torrado e moido|torrado|extraforte|tradicional",
                r"capsula|soluvel|cappuccino|com leite|gelado|filtro de papel|descafeinado"),
    "acucar":  (r"refinado|cristal", r"confeiteiro|mascavo|demerara|light|organico|sache"),
    "oleo":    (r"\bsoja\b", r"composto|motor|corporal|amendoa|girassol|milho|canola|coco"),
    "sal":     (r"refinado|iodado|\bsal \d", r"grosso|rosa|parrilla|light|temperado|marinho|ervas"),
    "macarrao":(r"espaguete|parafuso|penne|ninho|talharim|padre nosso|ave maria",
                r"instantaneo|lamen|cup|miojo|yakisoba|arroz"),
    "farinha": (r"\btrigo\b", r"rosca|mandioca|milho|aveia|linhaca|arroz|amendoa|tapioca"),
    "ovo":     (r"\bovos?\b.*\b(branco|vermelho|caipira|extra|grande)\b", r"pascoa|chocolate|codorna"),
    "picanha": (r"bovina|\bbov\b", r"cordeiro|suina|hamburguer|sabor|tempero"),
    "carne":   (r"bovina|moida|suina|acem|patinho|cubos|bife",
                r"whiskas|pedigree|friskies|racao|sache|\bcaes\b|\bgatos?\b|vegetal|vegan|jaca|soja|futuro"),
}

# palavras que, logo depois do termo, NÃO são um "tipo" (só ligam/medem)
_LIGA = {"de", "da", "do", "em", "com", "sem", "e", "tipo", "para", "p", "c", "s", "a", "o"}
_RE_MEDIDA = _re.compile(r"^\d|^(kg|g|ml|l|un|und|lt|cx|pct|pacote|caixa|lata|garrafa)$")


# Medida digitada -> grafias que aparecem no nome do produto. Quem busca "coca 2 litros"
# tem que achar "Coca-Cola Garrafa 2l" (e "350 ml" achar "350ml", "1 quilo" achar "1kg").
_UNIDADE = {"l": "l", "lt": "l", "lts": "l", "litro": "l", "litros": "l",
            "ml": "ml", "mls": "ml", "mililitro": "ml", "mililitros": "ml",
            "g": "g", "gr": "g", "grs": "g", "grama": "g", "gramas": "g",
            "kg": "kg", "kgs": "kg", "quilo": "kg", "quilos": "kg", "kilo": "kg", "kilos": "kg"}
_GRAFIAS = {"l": ("l", "lt", "litro"), "ml": ("ml",), "g": ("g", "gr", "grama"),
            "kg": ("kg", "quilo", "kilo")}
_RE_NUM = _re.compile(r"^\d+(?:[.,]\d+)?$")
_RE_NUM_UNID = _re.compile(r"^(\d+(?:[.,]\d+)?)([a-z]+)$")


def _sem_hifen(s: str) -> str:
    """'coca-cola' -> 'coca cola': hífen e barra separam palavras (busca e nome iguais)."""
    return s.replace("-", " ").replace("/", " ")


def _termos_busca(full: str) -> list[tuple[str, str]]:
    """Separa a busca em ('palavra', 'coca') e ('medida', '2l'). Número + unidade, junto
    ou separado ('2 litros', '2lt', '2l'), vira UMA medida com a unidade padronizada."""
    pal = full.split()
    out, i = [], 0
    while i < len(pal):
        w = pal[i]
        m = _RE_NUM_UNID.match(w)
        if m and m.group(2) in _UNIDADE:
            out.append(("medida", m.group(1) + _UNIDADE[m.group(2)]))
        elif _RE_NUM.match(w) and i + 1 < len(pal) and pal[i + 1] in _UNIDADE:
            out.append(("medida", w + _UNIDADE[pal[i + 1]]))
            i += 1
        elif w == "zero" or (w == "sem" and i + 1 < len(pal) and pal[i + 1] == "acucar"):
            # "zero" e "sem açúcar" são o mesmo pra quem busca: uma loja escreve
            # "Coca-Cola Zero Açúcar", outra "Coca-Cola Sem Açúcar"
            out.append(("zero", "zero"))
            i += 1 if w == "sem" else 0
        else:
            out.append(("palavra", w))
        i += 1
    return out


def _padroes_medida(medida: str) -> list[str]:
    """LIKEs que acham a medida no nome em qualquer grafia: '2l' -> '2l', '2lt', '2 litros',
    '2 l'... ('2,5' e '2.5' valem igual). Sempre no início de palavra ('12l' não casa '2l')."""
    m = _RE_NUM_UNID.match(medida)
    num, unid = m.group(1), m.group(2)
    pads = []
    for n in {num, num.replace(",", "."), num.replace(".", ",")}:
        pads += [f"{n}{unid}%", f"% {n}{unid}%"]          # colado: 2l, 2lt, 2litros
        for g in _GRAFIAS[unid]:
            if len(g) == 1:                               # '2 l' sozinho ('2 latas' não)
                pads += [f"% {n} {g}", f"% {n} {g} %"]
            else:
                pads.append(f"% {n} {g}%")                # '2 lt', '2 litros'
    return pads


def _tipo_do_nome(nome_norm: str, tokens: list[str]) -> tuple[int, int] | None:
    """Posição (início, nº de palavras) do 'tipo' logo depois do termo buscado:
    'leite condensado moca' -> 'condensado'; 'leite em po ninho' -> 'em po';
    'leite de coco' -> 'de coco'. É o que vira botão de filtro na busca."""
    pal = nome_norm.split()
    try:
        i = max(pal.index(t) for t in tokens if t in pal) + 1
    except ValueError:
        return None
    if i >= len(pal):
        return None
    w = pal[i]
    if w in ("em", "de") and i + 1 < len(pal) and not _RE_MEDIDA.match(pal[i + 1]):
        return (i, 2)
    if w in _LIGA or _RE_MEDIDA.match(w) or len(w) < 3 or not any(ch.isalpha() for ch in w):
        return None
    if w == "longa" and i + 1 < len(pal) and pal[i + 1] == "vida":
        return (i, 2)
    return (i, 1)


def buscar_com_tipos(conn, termo: str, limite: int = 40, tipo: str | None = None):
    """Busca por nome (ou EAN) com RELEVÂNCIA pensada no cliente:
      1. código de barras exato, se a busca for um EAN;
      2. nome que COMEÇA com a busca ('Arroz ...' antes de 'Macarrão de Arroz');
      3. categoria dominante dos resultados ('Leite de Rosas'/higiene desce em 'leite');
      4. tipo básico do item do dia a dia (INTENCAO_BUSCA: leite de caixinha antes de condensado);
      5. em mais mercados (dá pra comparar) e nome mais curto.
    Casa por INÍCIO de palavra. Devolve (achados, tipos): tipos = botões de filtro
    [{tipo, n}] montados dos próprios resultados; `tipo` filtra por um deles."""
    full = " ".join((_unaccent(termo) or "").split())
    if _eh_ean(full):
        rows = conn.execute(
            "SELECT pr.ean, pr.nome_canonico, pr.marca, pr.categoria, "
            "COUNT(DISTINCT pa.mercado_id) AS n_mercados FROM produtos pr "
            "JOIN precos_atual_unif pa ON pa.ean = pr.ean "
            "WHERE pr.ean = COALESCE((SELECT ean_principal FROM produto_equivalente WHERE ean = ?), ?) "
            "GROUP BY pr.ean, pr.nome_canonico, pr.marca, pr.categoria", (full, full)).fetchall()
        return [dict(r) for r in rows], []

    termos = _termos_busca(_sem_hifen(full))
    if not termos:
        return [], []
    tokens = [t for _, t in termos]
    full = " ".join(tokens)
    # hífen/barra viram espaço também no nome: 'Coca-Cola' casa com a busca 'coca cola'
    nome = "replace(replace(lower(unaccent(pr.nome_canonico)), '-', ' '), '/', ' ')"
    conds, params = [], []
    for tipo_t, t in termos:
        if tipo_t == "medida":
            pads = _padroes_medida(t)
        elif tipo_t == "zero":
            pads = ["zero%", "% zero%", "sem acucar%", "% sem acucar%"]
        else:
            pads = [f"{t}%", f"% {t}%"]
        conds.append("(" + " OR ".join([f"{nome} LIKE ?"] * len(pads)) + ")")
        params += pads
    cond = " AND ".join(conds)
    rows = [dict(r) for r in conn.execute(
        f"""SELECT pr.ean, pr.nome_canonico, pr.marca, pr.categoria,
                   COUNT(DISTINCT pa.mercado_id) AS n_mercados
            FROM produtos pr JOIN precos_atual_unif pa ON pa.ean = pr.ean   -- mesmo produto c/ EAN
            WHERE {cond}                                                   -- diferente conta junto
            GROUP BY pr.ean, pr.nome_canonico, pr.marca, pr.categoria
            ORDER BY n_mercados DESC LIMIT 600""", params).fetchall()]
    if not rows:
        return [], []

    # marca não é tipo ('Arroz Tio João', 'Leite Nestlé'): nem todo item tem a marca
    # preenchida, então vale qualquer palavra de marca vista nos resultados
    marcas = {w for r in rows for w in (_unaccent(r["marca"]) or "").replace("-", " ").split()
              if len(w) >= 3} - set(tokens)
    rotulos: dict = {}   # tipo sem acento -> como está escrito no produto ('em po' -> 'em pó')
    for r in rows:
        r["_n"] = " ".join(_sem_hifen(_unaccent(r["nome_canonico"]) or "").split())
        pos = _tipo_do_nome(r["_n"], tokens)
        r["tipo"] = None
        if pos:
            i, k = pos
            t = " ".join(r["_n"].split()[i:i + k])
            if t.split()[-1] not in marcas:
                r["tipo"] = t
                orig = _sem_hifen(r["nome_canonico"] or "").split()[i:i + k]
                if len(orig) == k:
                    rotulos.setdefault(t, " ".join(orig).lower())
    # categoria dominante: entre quem começa com a busca (senão, entre todos), pesada por mercados
    base = [r for r in rows if r["_n"].startswith(full)] or rows
    peso: dict = {}
    for r in base:
        peso[r["categoria"]] = peso.get(r["categoria"], 0) + (r["n_mercados"] or 0)
    dominante = max(peso, key=peso.get) if peso else None
    pref, deriv = (None, None)
    if full in INTENCAO_BUSCA:
        pref, deriv = (_re.compile(x) for x in INTENCAO_BUSCA[full])

    def chave(r):
        n = r["_n"]
        basico = 0
        if pref is not None:
            basico = (1 if pref.search(n) else 0) - (2 if deriv.search(n) else 0)
        return (n.startswith(full), r["categoria"] == dominante, basico,
                r["n_mercados"] or 0, -len(n))
    rows.sort(key=chave, reverse=True)

    # botões de tipo: os mais frequentes (>= 2 itens), na ordem de quantidade
    cont: dict = {}
    for r in rows:
        if r["tipo"]:
            cont[r["tipo"]] = cont.get(r["tipo"], 0) + 1
    # plural vira o singular ('em capsulas' -> 'em capsula') quando os dois existem
    for t in [t for t in cont if t.endswith("s") and t[:-1] in cont]:
        cont[t[:-1]] += cont.pop(t)
        for r in rows:
            if r["tipo"] == t:
                r["tipo"] = t[:-1]
    tipos = [{"tipo": t, "rotulo": rotulos.get(t, t), "n": n}
             for t, n in sorted(cont.items(), key=lambda x: -x[1]) if n >= 2][:10]
    if tipo:
        rows = [r for r in rows if r["tipo"] == tipo]
    for r in rows:
        r.pop("_n", None)
    return rows[:limite], tipos


def buscar_produtos(conn, termo: str, limite: int = 15) -> list[dict]:
    """Só os produtos (sem os botões de tipo) — ver buscar_com_tipos."""
    return buscar_com_tipos(conn, termo, limite)[0]


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
