"""Mapeia o nome do departamento/categoria NATIVO de cada mercado para as
categorias do app (os chips do front). Usado pela varredura de catálogo completo,
onde a categoria vem da árvore do mercado, não de um termo-semente.

Default = 'mercearia' (a maioria dos industrializados). Produtos perecíveis
(açougue, padaria, hortifrúti) geralmente não têm EAN-13 e são filtrados por
ean_valido() antes de chegar aqui.
"""
from __future__ import annotations

import unicodedata


def _norm(s: str | None) -> str:
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


# (slug_do_app, [palavras-chave no nome do departamento]) — ordem importa.
_REGRAS: list[tuple[str, list[str]]] = [
    ("bebidas",    ["bebida", "adega", "cerveja", "vinho", "refrigerante", "suco",
                    "agua", "destilado", "whisky", "vodka", "energetic", "cachaca", "espumante"]),
    ("limpeza",    ["limpeza", "lava roupa", "lava-roupa", "lava loucas"]),
    ("higiene",    ["higiene", "perfumaria", "beleza", "cuidado", "cabelo", "farmacia",
                    "saude", "cosmetic", "bem estar", "bem-estar", "capilar", "bucal"]),
    ("laticinios", ["laticinio", "frios", "queijo", "iogurte", "leite e", "leites e"]),
    ("hortifruti", ["hortifruti", "frutas", "verdura", "legume", "feira", "flv"]),
]


def mapear_categoria(nome: str | None) -> str:
    n = _norm(nome)
    for slug, palavras in _REGRAS:
        if any(p in n for p in palavras):
            return slug
    return "mercearia"
