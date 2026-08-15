"""Conexão e helpers de acesso ao SQLite.

Regras de ouro:
- `produtos` é upsert por EAN (nome canônico = o mais longo/completo observado).
- `mercados` é upsert por slug.
- `precos` é APPEND-ONLY: cada coleta grava um novo snapshot; nunca sobrescreve.
"""
from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SCHEMA_SQL = Path(__file__).resolve().parent / "schema.sql"


def _pg_dsn() -> str:
    """Se DATABASE_URL estiver setada, o app usa Postgres (Supabase); senão, SQLite local."""
    return os.getenv("DATABASE_URL", "").strip()


class _PgConn:
    """Deixa o código que fala 'SQLite' (placeholders '?', row['coluna'], conn.execute)
    rodar sobre Postgres/Supabase SEM reescrever as queries. Traduz '?'->'%s' (escapando
    '%' literais antes) e devolve linhas como dict. autocommit=True: robusto pros coletores
    (um erro num item não envenena a transação dos próximos)."""

    def __init__(self, dsn: str) -> None:
        import psycopg
        from psycopg.rows import dict_row
        # prepare_threshold=None: compatível com o pooler de transação do Supabase (pgbouncer).
        # connect_timeout: falha rápido (10s) em vez de pendurar o request/boot indefinidamente
        # se o banco não responder (ex.: rede do host novo ainda não resolveu a rota).
        self._c = psycopg.connect(dsn, autocommit=True, row_factory=dict_row,
                                  prepare_threshold=None, connect_timeout=10)
        # inclui 'extensions' no path pra achar unaccent() (Supabase instala extensões lá).
        try:
            self._c.execute("SET search_path TO public, extensions")
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _q(sql: str) -> str:
        return sql.replace("%", "%%").replace("?", "%s")

    def execute(self, sql, params=()):
        return self._c.execute(self._q(sql), params)

    def executemany(self, sql, seq):
        cur = self._c.cursor()
        cur.executemany(self._q(sql), list(seq))
        return cur

    def commit(self):
        pass  # autocommit=True já efetiva cada statement

    def rollback(self):
        try:
            self._c.rollback()
        except Exception:  # noqa: BLE001
            pass

    def cursor(self):
        return self._c.cursor()

    def close(self):
        try:
            self._c.close()
        except Exception:  # noqa: BLE001
            pass


def conectar(db_path: str | Path = "dados/precos.db"):
    """Postgres (Supabase) se DATABASE_URL estiver setada; senão, SQLite local (dev)."""
    dsn = _pg_dsn()
    if dsn:
        return _PgConn(dsn)
    db_path = Path(db_path)
    if not db_path.is_absolute():
        db_path = RAIZ / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")     # leitor não trava durante a coleta
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def inicializar(conn) -> None:
    """Cria tabelas/índices e migra colunas (SQLite). No Postgres o schema já foi aplicado
    via supabase/*.sql, então aqui é no-op."""
    if _pg_dsn():
        return
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    _migrar(conn)
    conn.commit()


def _migrar(conn: sqlite3.Connection) -> None:
    """Adiciona colunas novas a bancos antigos (idempotente). CREATE TABLE IF NOT
    EXISTS não altera tabelas já criadas, então garantimos as colunas aqui."""
    faltantes = {
        "mercados": {"logo_url": "TEXT", "cor_badge": "TEXT", "loja_ref": "TEXT"},
        "produtos": {"categoria": "TEXT", "n_mercados": "INTEGER DEFAULT 0", "menor_preco": "REAL"},
        "lista": {"quantidade": "REAL DEFAULT 1", "comprado": "INTEGER DEFAULT 0",
                  "nome": "TEXT"},
        "pereciveis": {"categoria": "TEXT", "so_no_site": "INTEGER DEFAULT 0",
                       "preco_de": "REAL", "desconto_pct": "INTEGER", "oferta_tipo": "TEXT"},
        "precos_atual": {"so_no_site": "INTEGER DEFAULT 0", "preco_de": "REAL",
                         "desconto_pct": "INTEGER", "oferta_tipo": "TEXT"},
    }
    for tabela, colunas in faltantes.items():
        existentes = {r["name"] for r in conn.execute(f"PRAGMA table_info({tabela})")}
        if not existentes:
            continue  # tabela ainda não existe (schema.sql já a cria com a coluna)
        for col, tipo in colunas.items():
            if col not in existentes:
                conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {col} {tipo}")
    # índices que dependem de colunas garantidas acima (criados só após o ALTER)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_produtos_cat_nmerc ON produtos(categoria, n_mercados)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pereciveis_cat ON pereciveis(categoria)")


def upsert_categoria(
    conn: sqlite3.Connection, *, slug: str, nome: str, disponivel: bool, ordem: int
) -> None:
    conn.execute(
        """
        INSERT INTO categorias (slug, nome, disponivel, ordem)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
            nome = excluded.nome, disponivel = excluded.disponivel, ordem = excluded.ordem
        """,
        (slug, nome, 1 if disponivel else 0, ordem),
    )


def upsert_mercado(
    conn: sqlite3.Connection,
    *,
    slug: str,
    nome: str,
    url_base: str,
    plataforma: str | None,
    cidade: str | None,
    ativo: bool = True,
    logo_url: str | None = None,
    cor_badge: str | None = None,
    loja_ref: str | None = None,
) -> int:
    """Insere/atualiza um mercado e devolve seu id."""
    conn.execute(
        """
        INSERT INTO mercados (slug, nome, url_base, plataforma, cidade, ativo,
                              logo_url, cor_badge, loja_ref)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
            nome = excluded.nome,
            url_base = excluded.url_base,
            plataforma = excluded.plataforma,
            cidade = excluded.cidade,
            ativo = excluded.ativo,
            logo_url = excluded.logo_url,
            cor_badge = excluded.cor_badge,
            loja_ref = excluded.loja_ref
        """,
        (slug, nome, url_base, plataforma, cidade, 1 if ativo else 0,
         logo_url, cor_badge, loja_ref),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM mercados WHERE slug = ?", (slug,)).fetchone()
    return int(row["id"])


def upsert_produto(
    conn: sqlite3.Connection,
    *,
    ean: str,
    nome: str | None,
    marca: str | None = None,
    categoria: str | None = None,
    unidade: str | None = None,
    qtd_embalagem: float | None = None,
    imagem_url: str | None = None,
) -> None:
    """Upsert por EAN. Nome canônico determinístico: o mais LONGO vence.

    Não sobrescreve campos existentes com NULL (usa COALESCE), pra um mercado
    com dado mais pobre não apagar o que outro já preencheu. A categoria é a
    primeira observada (vem do termo-semente que achou o produto).
    """
    conn.execute(
        """
        INSERT INTO produtos (ean, nome_canonico, marca, categoria, unidade, qtd_embalagem, imagem_url)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ean) DO UPDATE SET
            nome_canonico = CASE
                WHEN excluded.nome_canonico IS NOT NULL
                     AND length(COALESCE(excluded.nome_canonico, '')) > length(COALESCE(produtos.nome_canonico, ''))
                THEN excluded.nome_canonico
                ELSE produtos.nome_canonico
            END,
            marca         = COALESCE(produtos.marca, excluded.marca),
            categoria     = COALESCE(produtos.categoria, excluded.categoria),
            unidade       = COALESCE(excluded.unidade, produtos.unidade),
            qtd_embalagem = COALESCE(produtos.qtd_embalagem, excluded.qtd_embalagem),
            imagem_url    = COALESCE(produtos.imagem_url, excluded.imagem_url)
        """,
        (ean, nome, marca, categoria, unidade, qtd_embalagem, imagem_url),
    )


def inserir_preco(
    conn: sqlite3.Connection,
    *,
    ean: str,
    mercado_id: int,
    preco: float,
    preco_por_unidade: float | None,
    coletado_em: str,
    origem: str = "online",
    so_no_site: bool = False,
    preco_de: float | None = None,
    desconto_pct: int | None = None,
    oferta_tipo: str | None = None,
) -> None:
    """Upsert em `precos_atual` (preço quente, sempre atualizado) + histórico em `precos`
    SÓ QUANDO O PREÇO MUDA (dedup). Assim a coleta pode rodar com frequência sem inchar o
    banco: o preço fica sempre fresco, mas o histórico só ganha linha em mudança real."""
    # Se há desconto mas o coletor não classificou o tipo, assume 'oferta' (aberta a todos):
    # só o VIP marca 'fidelidade' explicitamente (preço que exige cartão/app).
    if oferta_tipo is None and desconto_pct:
        oferta_tipo = "oferta"
    ant = conn.execute(
        "SELECT preco FROM precos_atual WHERE ean = ? AND mercado_id = ?", (ean, mercado_id)
    ).fetchone()
    mudou = ant is None or ant["preco"] is None or abs(float(ant["preco"]) - float(preco)) > 0.005
    if mudou:  # histórico é append-only, mas só de mudanças (dedup)
        conn.execute(
            """
            INSERT INTO precos (ean, mercado_id, preco, preco_por_unidade, origem, coletado_em)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ean, mercado_id, preco, preco_por_unidade, origem, coletado_em),
        )
    conn.execute(
        """
        INSERT INTO precos_atual (ean, mercado_id, preco, preco_por_unidade, origem,
                                  coletado_em, so_no_site, preco_de, desconto_pct, oferta_tipo)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ean, mercado_id) DO UPDATE SET
            preco = excluded.preco,
            preco_por_unidade = excluded.preco_por_unidade,
            origem = excluded.origem,
            coletado_em = excluded.coletado_em,
            so_no_site = excluded.so_no_site,
            preco_de = excluded.preco_de,
            desconto_pct = excluded.desconto_pct,
            oferta_tipo = excluded.oferta_tipo
        """,
        (ean, mercado_id, preco, preco_por_unidade, origem, coletado_em,
         1 if so_no_site else 0, preco_de, desconto_pct, oferta_tipo),
    )


def backfill_precos_atual(conn: sqlite3.Connection) -> int:
    """(Re)constrói precos_atual a partir do último snapshot de cada (ean, mercado) em precos."""
    conn.execute("DELETE FROM precos_atual")
    conn.execute(
        """
        INSERT INTO precos_atual (ean, mercado_id, preco, preco_por_unidade, origem, coletado_em)
        SELECT p.ean, p.mercado_id, p.preco, p.preco_por_unidade, p.origem, p.coletado_em
        FROM precos p
        JOIN (SELECT ean, mercado_id, MAX(coletado_em) mx FROM precos GROUP BY ean, mercado_id) u
          ON u.ean = p.ean AND u.mercado_id = p.mercado_id AND u.mx = p.coletado_em
        """
    )
    rebuild_resumo(conn)
    conn.commit()
    return int(conn.execute("SELECT COUNT(*) AS n FROM precos_atual").fetchone()["n"])


def rebuild_resumo(conn: sqlite3.Connection) -> None:
    """Recomputa produtos.n_mercados e produtos.menor_preco a partir de precos_atual.
    Chamar ao fim de cada coleta (rápido: precos_atual tem ~1 linha por produto+mercado)."""
    conn.execute(
        """
        UPDATE produtos SET
            n_mercados  = (SELECT COUNT(*)   FROM precos_atual pa WHERE pa.ean = produtos.ean),
            menor_preco = (SELECT MIN(preco) FROM precos_atual pa WHERE pa.ean = produtos.ean)
        """
    )
    conn.commit()


def podar_historico(conn: sqlite3.Connection, manter_dias: int = 90) -> dict:
    """Limita o histórico append-only pra o banco não crescer pra sempre:
      1) mantém no MÁXIMO 1 snapshot por (ean, mercado, dia) — o último do dia;
      2) descarta snapshots mais antigos que `manter_dias` dias.
    `precos_atual` (tabela quente) e o snapshot mais recente de cada par são sempre
    preservados. Devolve a contagem antes/depois. NÃO roda VACUUM (ver manutencao.py)."""
    from datetime import datetime, timedelta, timezone
    antes = int(conn.execute("SELECT COUNT(*) AS n FROM precos").fetchone()["n"])
    # 1) colapsa intra-dia: por (ean, mercado, dia) sobra só o id mais alto (= mais recente).
    # substr(coletado_em,1,10) = o dia (YYYY-MM-DD); funciona igual no SQLite e no Postgres.
    conn.execute(
        """
        DELETE FROM precos
        WHERE id NOT IN (
            SELECT MAX(id) FROM precos
            GROUP BY ean, mercado_id, substr(coletado_em, 1, 10)
        )
        """
    )
    # 2) corta histórico antigo, mas nunca o último snapshot de cada par. Corte por STRING ISO
    # (coletado_em é ISO 8601 UTC, que ordena lexicograficamente = cronologicamente) — sem
    # julianday, roda nos dois bancos.
    corte = (datetime.now(timezone.utc) - timedelta(days=manter_dias)).isoformat()
    conn.execute(
        """
        DELETE FROM precos
        WHERE coletado_em < ?
          AND id NOT IN (SELECT MAX(id) FROM precos GROUP BY ean, mercado_id)
        """,
        (corte,),
    )
    conn.commit()
    depois = int(conn.execute("SELECT COUNT(*) AS n FROM precos").fetchone()["n"])
    return {"antes": antes, "depois": depois, "removidos": antes - depois}


def normalizar_nome(s: str | None) -> str | None:
    """Remove acento e baixa caixa: 'Filé Mignon' -> 'file mignon'. Mesma regra da
    busca (analise.ranking._unaccent), pra casar perecível por nome de forma tolerante."""
    if s is None:
        return None
    nfkd = unicodedata.normalize("NFKD", s)
    return " ".join("".join(c for c in nfkd if not unicodedata.combining(c)).lower().split())


# Palavras-chave p/ classificar um item fresco (sem EAN) numa "seção de mercado".
# Ordem importa: o 1º grupo que casar vence. 'peixes' antes de 'carnes' (p/ "filé de
# peixe" não virar carne); 'frios' ANTES de 'carnes' (p/ "peito de peru"/"lombo
# canadense" venceram "peru"/"lombo" de carnes). Casamento é por PALAVRA INTEIRA
# (fronteira \w), então "asa" não casa "lasanha" nem "maca" casa "macarrao".
_CLASSES_PERECIVEL = [
    ("peixes", ("peixe", "tilapia", "salmao", "merluza", "bacalhau", "pescada",
                "camarao", "sardinha fresca", "file de peixe", "posta", "lula", "polvo")),
    ("frios",   ("queijo", "mussarela", "mucarela", "presunto", "mortadela", "salame",
                 "frios", "apresuntado", "peito de peru", "provolone", "parmesao",
                 "prato lanche", "copa", "lombo canadense")),
    ("carnes", ("carne", "bovin", "boi", "acem", "alcatra", "patinho", "coxao", "musculo",
                "picanha", "fraldinha", "maminha", "costela", "cupim", "file",
                "lombo", "pernil", "suino", "porco", "frango", "coxa", "sobrecoxa", "peito de frango",
                "asa", "linguica", "bisteca", "moida", "carne seca",
                "contra file", "capa de file", "paleta", "figado", "coracao",
                "hamburguer", "chester", "peru", "bacon", "carre")),
    ("padaria", ("pao", "paes", "bisnaga", "baguete", "sonho", "croissant",
                 "rosca", "bolo", "torta", "folhado", "padaria", "brioche", "focaccia")),
    ("ovos",    ("ovo", "ovos", "duzia de ovo", "ovo caipira")),
    ("hortifruti", ("abacaxi", "abacate", "abobora", "abobrinha", "acelga", "alface", "banana",
                    "tomate", "cebola", "batata", "cenoura", "maca", "laranja", "limao",
                    "mamao", "manga", "melancia", "melao", "uva", "pera", "morango", "alho",
                    "pimentao", "couve", "brocolis", "repolho", "beterraba", "mandioca", "inhame",
                    "chuchu", "quiabo", "vagem", "espinafre", "rucula", "salsa", "cebolinha",
                    "coentro", "gengibre", "pepino", "berinjela", "milho verde", "verdura",
                    "legume", "fruta", "hortalica", "mexerica", "tangerina", "kiwi", "goiaba",
                    "maracuja", "caqui", "figo", "ameixa", "nectarina", "pessego",
                    "rabanete", "nabo", "aipim", "mandioquinha", "escarola", "almeirao", "agriao")),
]
# Pré-compila 1 regex por classe (alternância com fronteira de palavra), com as
# chaves já sem acento (mesma normalização do nome), pra casar palavra inteira.
_CLASSES_RE = [
    (slug, re.compile(r"(?<!\w)(?:%s)(?!\w)" % "|".join(
        sorted((re.escape(normalizar_nome(c)) for c in chaves), key=len, reverse=True))))
    for slug, chaves in _CLASSES_PERECIVEL
]


# ---------------------------------------------------------------------------
# VETO de não-frescos + CHAVE de agrupamento cruzado entre lojas.
# As listas de regex foram construídas analisando os nomes REAIS de ~950 itens
# (ver docs/frescos-regras). Casam contra o nome já normalizado (minúsculo, sem
# acento, espaço simples). Ajustes sobre a análise: 'cerveja/vinho' não vetam
# marinada ('frango na cerveja', 'file ao vinho'); 'tempero' saiu do veto (falso-
# vetava cheiro-verde/frango temperado); 'macarrao' não veta 'vagem macarrao'.
# ---------------------------------------------------------------------------
_NEGATIVOS_FRESCO = [
    r"\bracao\b|pedigree|whiskas|alimento para (caes|gatos)",
    r"phalaenopsis|\bvaso\b|\bbonsai\b|hibisq",
    r"\bsalgadinho\b", r"\bamendoim\b",
    r"\bchips\b|batata palha|yokitos|tostitos", r"\btorresmo\b", r"milho espanhol",
    r"\blimpeza\b|mr\.? ?musculo",
    r"\bdetergente\b|lava louc|\bsabonete\b|\bremovedor\b|\blapisera\b",
    r"\bacendedor(es)?\b|\bisqueir[oa]s?\b", r"papel toalha|\braid\b|bronze",
    r"proteina\s+(de\s+)?soja",
    r"\brefrigerante\b", r"\brefresco\b", r"\bsuco\b",
    r"\b(bebida|isotonico|energetico|coquetel|espumante|prosecco)\b",
    r"(?<!na )(?<!ao )(?<!no )\b(cerveja|vinho)\b",   # não veta marinada
    r"cabernet|chardonnay|sauvignon", r"saborizad", r"\btwinings\b|\bcha\b",
    r"\b(sorvete|gelatina|geleia|manjar)\b", r"\bchocolat",
    r"\biogurte\b|petit suisse|danoninho|leite fermentado",
    r"uva passa", r"desidr", r"cristalizad", r"\bem po\b", r"\balho frito\b",
    r"\bmolho\b|passata|extrato de tomate|polpa de (fruta|tomate)",
    r"em conserva", r"\bsardinha\b",
    r"\bfarinha\b|\bfibra de\b|\bgnocchi\b|\bvinagre\b", r"\bpate\b",
    r"(?<!vagem )\bmacarr(ao|oes)\b",
    r"\bovomaltine\b|\bachocol\w*", r"^semente\b",
    r"marmitex|\bsopa\b|salpicao|esfiha|croissant|sanduiche|rondelli|\bempanadas?\b|embutido|mini coxinha",
    r"\bpizza\b", r"\bpao de queijo\b", r"\bbolinha\b", r"\blanche no pao\b",
    r"pao.*hamburg", r"\bbauducco\b|\bmelhor bocado\b",
    r"\b(hossomaki|uramaki|temaki|niguiri|nigiri|sashimi|sushi|combinado)\b",
    r"kit combinacao", r"strogonoff de|estrogonofe|(^|exec\.?\s*)strogonoff", r"\bbatata palito\b",
]
_NEG_FRESCO_RE = re.compile("|".join(f"(?:{p})" for p in _NEGATIVOS_FRESCO))

# Ruído removido para formar a chave de grupo (peso/embalagem/estado/marca/marketing).
# Preserva palavras de variedade/corte (não fundir 'abobora menina' x 'moranga' etc.).
_RUIDO_FRESCO = [re.compile(p) for p in [
    r"\d+[.,]?\d*\s?(kg|mg|ml|gr|g|l)\b", r"\b(kg|quilo|kilo|gr|g|ml|l)\b",
    r"\d+\s?(un|und|unid|unidades?|duzia)\b", r"\d+\s*a\s*\d+\s*(un|und|unidades?)?",
    r"\b(un|und|unid|unidade|unidades|duzia)\b", r"\d{1,3}\s?x\s?\d{1,3}",
    r"\b(bandeja|bd|bdja|caixa|cx|pacote|pct|pote|cumbuca|redinha|saco|sache|peca|pca|pedaco|pdco|ped|vacuo|vac|granel|manta|mantinha|balcao|embalad[oa]|bisnaga|forma|forminha|fracionad[oa]|maco|vidro|lata|pet|garrafa|emb|embalagem|porcao|porcoes|ring|palito|nozinho|bola|meia lua)\b",
    r"\b(congelad[oa]|cong|resfriad[oa]|resf|refrige\w*|fresc[oa]|inteir[oa]|porcionad[oa]|higienizad[oa]|descascad[oa]|picad[oa]|ralad[oa]|cortad[oa]|cristalizad[oa]s?|eviscerad[oa]|limp[oa])\b",
    r"sem caroco|com caroco|sem semente", r"(peso\s+)?aprox\w*\.?|~",
    r"\b(friboi|maturatta|better\s?beef|beter\s?beef|bassi|minerva|seara|sadia|perdigao|aurora|copacol|big\s?frango|itabom|nho\s?bento|frinni|cataby|soft\s?pig|marchiori|ceratti|marba|braganca|pul|tortuguita|origem|enxuto|frimesa|combacon|congelatto|leve\s?mais)\b",
    r"\b(tirolez|vigor|scala|quata|ipanema|parlak|crioulo|piracanjuba|president|aviacao|polenghi|reliquia|bandeira|verde campo|sol brilhante|dona vaca|forno de minas|cheese bits|campo lindo|bom destino|nacon|la serenissima|laserenissima|faixa azul|porto alegre|letti|atilatte|montanhes|alpes minas|davaca|levitare|celebrar|qualycon|pague menos)\b",
    r"\b(dterra|dalben|bonfim|boa roca|fresh1|vpj|bom peixe|gold boys|daucy)\b",
    r"\b(balcao|tp|tipo|gourmet|longo|cry|pas|pt|pta|def)\b",
    r"\b(nobre|extra|reserva|premium|primeira|selecionad[oa]|fracionado|exportacao|grauda)\b",
    r"\bkit\b|\bcombo\b",
    r"na\s+brasa|churrasco|p\s?chur\w*|para\s+churrasco|grill|assa\s?facil|para\s+assar",
    r"\([^)]*\)", r"\bc\b|\bp\b|\+", r"leve\s+\d+.*pague\s+\d+",
    r"\bbovin[oa]\b|\bbov\b|\bboi\b",   # qualificador default de carne vermelha
]]
# Conectivos/artigos que, isolados, só atrapalham o casamento (não carregam corte).
_FILLER_FRESCO = re.compile(r"\b(a|ao|as|os|o|de|da|do|das|dos|e|em|no|na|para)\b")


def eh_falso_fresco(nome: str | None) -> bool:
    """True se o nome é claramente NÃO-fresco (ração, flor, bebida, industrializado,
    limpeza, prato pronto...) — não deve entrar nas seções de frescos."""
    return bool(_NEG_FRESCO_RE.search(normalizar_nome(nome) or ""))


def chave_fresco(nome: str | None) -> str:
    """Chave de agrupamento CRUZADO entre lojas: tira peso/embalagem/estado/marca do
    nome e mantém o corte/variedade, pra 'Abacate 900g'/'Abacate Bandeja 800g' virarem
    o mesmo grupo mas 'Abóbora Menina'/'Abóbora Moranga' ficarem separados."""
    s = normalizar_nome(nome) or ""
    s = re.sub(r"[-–—/&]", " ", s)            # hífen/barra viram espaço (manga-palmer)
    s = re.sub(r"\b(mu[cs]+arela|mo[cz]+arela|muzzarela)\b", "mussarela", s)  # grafias do mesmo queijo
    for rx in _RUIDO_FRESCO:
        s = rx.sub(" ", s)
    s = _FILLER_FRESCO.sub(" ", s)
    s = re.sub(r"[.,;:]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or (normalizar_nome(nome) or "")


def classificar_perecivel(nome: str | None) -> str:
    """Classifica um item fresco numa seção (carnes/hortifruti/padaria/frios/peixes/ovos).
    Casa por palavra inteira no nome sem acento; 'outros' quando nada casa. Aproximado.
    Itens claramente NÃO-frescos (ver eh_falso_fresco) caem em 'outros'."""
    n = normalizar_nome(nome) or ""
    if _NEG_FRESCO_RE.search(n):
        return "outros"
    for slug, rx in _CLASSES_RE:
        if rx.search(n):
            return slug
    return "outros"


def upsert_perecivel(
    conn: sqlite3.Connection,
    *,
    mercado_id: int,
    cod_interno: str,
    nome: str,
    preco: float,
    unidade: str | None,
    imagem_url: str | None,
    url: str | None,
    coletado_em: str,
    so_no_site: bool = False,
    preco_de: float | None = None,
    desconto_pct: int | None = None,
    oferta_tipo: str | None = None,
) -> None:
    """Upsert do preço ATUAL de um item fresco/por peso (sem EAN) num mercado.
    Itens claramente não-frescos (ração, bebida, industrializado...) são ignorados."""
    if eh_falso_fresco(nome):
        return
    if oferta_tipo is None and desconto_pct:
        oferta_tipo = "oferta"
    conn.execute(
        """
        INSERT INTO pereciveis (mercado_id, cod_interno, nome, nome_norm, categoria, preco,
                                unidade, imagem_url, url, coletado_em,
                                so_no_site, preco_de, desconto_pct, oferta_tipo)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(mercado_id, cod_interno) DO UPDATE SET
            nome = excluded.nome,
            nome_norm = excluded.nome_norm,
            categoria = excluded.categoria,
            preco = excluded.preco,
            unidade = COALESCE(excluded.unidade, pereciveis.unidade),
            imagem_url = COALESCE(excluded.imagem_url, pereciveis.imagem_url),
            url = COALESCE(excluded.url, pereciveis.url),
            coletado_em = excluded.coletado_em,
            so_no_site = excluded.so_no_site,
            preco_de = excluded.preco_de,
            desconto_pct = excluded.desconto_pct,
            oferta_tipo = excluded.oferta_tipo
        """,
        (mercado_id, cod_interno, nome, normalizar_nome(nome), classificar_perecivel(nome), preco,
         unidade, imagem_url, url, coletado_em,
         1 if so_no_site else 0, preco_de, desconto_pct, oferta_tipo),
    )


def upsert_link(
    conn: sqlite3.Connection, *, ean: str, mercado_id: int, url: str, atualizado_em: str
) -> None:
    """Guarda/atualiza a URL da página do produto num mercado (para 'ver no site')."""
    conn.execute(
        """
        INSERT INTO links (ean, mercado_id, url, atualizado_em)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(ean, mercado_id) DO UPDATE SET
            url = excluded.url, atualizado_em = excluded.atualizado_em
        """,
        (ean, mercado_id, url, atualizado_em),
    )


def contar(conn: sqlite3.Connection, tabela: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {tabela}").fetchone()["n"])
