"""Normalização de embalagem -> preço por unidade (R$/kg, R$/L, R$/un).

A VTEX não entrega a gramatura limpa (measurementUnit costuma ser 'un'), então
extraímos do NOME do produto. É intencionalmente CONSERVADOR: quando o nome não
deixa a medida clara, devolvemos None e o preço/unidade fica NULL — melhor não ter
o dado do que exibir um R$/kg errado.

Base canônica: peso -> kg, volume -> L, contagem -> un.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Medida", "extrair_medida", "preco_por_unidade"]


@dataclass(frozen=True)
class Medida:
    qtd_base: float   # quantidade total na unidade base (kg, L ou un)
    unidade: str      # 'kg' | 'l' | 'un'
    rotulo: str       # 'R$/kg' | 'R$/L' | 'R$/un'


def _para_float(txt: str) -> float:
    """'1,5' -> 1.5 ; '500' -> 500.0 (vírgula = separador decimal PT-BR)."""
    return float(txt.replace(".", "").replace(",", ".")) if "," in txt else float(txt)


_NUM = r"(\d+(?:[.,]\d+)?)"

# 1) Multipack explícito: "6x350ml", "12 x 90g", "4X1L"
_RE_NXM = re.compile(rf"(\d+)\s*[xX]\s*{_NUM}\s*(kg|g|gr|ml|l)\b", re.IGNORECASE)
# 1b) Multipack com palavra de embalagem: "40 Sachês 5g", "10 cápsulas 10g", "12 sticks 2g"
#     (a palavra tem que ser de EMBALAGEM p/ não confundir com marca tipo "3 Corações")
_RE_NUNIT_M = re.compile(
    rf"(\d+)\s*(saches?|sach[eê]s?|saquinhos?|envelopes?|tabletes?|c[aá]psulas?|sticks?|"
    rf"por[cç][oõ]es?|unidades?)\s*(?:de\s*)?{_NUM}\s*(kg|g|gr|ml|l)\b",
    re.IGNORECASE,
)
# 2) Peso: 500g, 1kg, 1,02 kg
_RE_PESO = re.compile(rf"{_NUM}\s*(kg|kilo|quilo|g|gr|grama|gramas)\b", re.IGNORECASE)
# 3) Volume: 2l, 1,5 L, 350ml
_RE_VOL = re.compile(rf"{_NUM}\s*(litros?|ml|l)\b", re.IGNORECASE)
# 4) Contagem: "c/ 4", "com 12 unidades", "12 rolos", "leve 2"
_RE_CONT = re.compile(
    rf"(?:com|c/|contém|contem|leve|caixa com|pacote com|cartela com)?\s*{_NUM}\s*"
    r"(unidades?|un|rolos?|folhas?|saches?|sach[eê]s?|c[aá]psulas?|comprimidos?|"
    r"fraldas?|pe[cç]as?|guardanapos?|filtros?)\b",
    re.IGNORECASE,
)
# multiplicador de bundle sobre peso/volume: "leve 2", "kit 3", "c/ 6", "com 12"
_RE_MULT = re.compile(r"(?:leve|kit|pack|pacote com|caixa com|com|c/)\s*0*(\d+)\b", re.IGNORECASE)

_PESO_G = {"g", "gr", "grama", "gramas"}     # convertem para kg dividindo por 1000
_VOL_ML = {"ml"}                              # convertem para L dividindo por 1000


def extrair_medida(nome: str | None) -> Medida | None:
    if not nome:
        return None

    # 1) Multipack N x M<unidade> — total = N * M, base pela unidade
    m = _RE_NXM.search(nome)
    if m:
        n = int(m.group(1))
        q = _para_float(m.group(2))
        u = m.group(3).lower()
        if u in _PESO_G or u in {"kg"}:
            base = n * (q / 1000 if u in _PESO_G else q)
            return Medida(round(base, 6), "kg", "R$/kg")
        base = n * (q / 1000 if u in _VOL_ML else q)  # ml/l
        return Medida(round(base, 6), "l", "R$/L")

    # 1b) "N <embalagem> M<unidade>" -> total = N * M
    m = _RE_NUNIT_M.search(nome)
    if m:
        n = int(m.group(1))
        q = _para_float(m.group(3))
        u = m.group(4).lower()
        if u in _PESO_G or u == "kg":
            base = n * (q / 1000 if u in _PESO_G else q)
            return Medida(round(base, 6), "kg", "R$/kg")
        base = n * (q / 1000 if u in _VOL_ML else q)
        return Medida(round(base, 6), "l", "R$/L")

    # 2) Peso
    m = _RE_PESO.search(nome)
    if m:
        q = _para_float(m.group(1))
        u = m.group(2).lower()
        kg = q / 1000 if u in _PESO_G else q
        kg *= _mult_bundle(nome)
        return Medida(round(kg, 6), "kg", "R$/kg")

    # 3) Volume
    m = _RE_VOL.search(nome)
    if m:
        q = _para_float(m.group(1))
        u = m.group(2).lower()
        litros = q / 1000 if u in _VOL_ML else q
        litros *= _mult_bundle(nome)
        return Medida(round(litros, 6), "l", "R$/L")

    # 4) Contagem
    m = _RE_CONT.search(nome)
    if m:
        n = _para_float(m.group(1))
        if n > 0:
            return Medida(n, "un", "R$/un")

    return None


def _mult_bundle(nome: str) -> int:
    """'Leve 2'/'Kit 3' vira multiplicador de embalagem (bundle). Padrão 1."""
    m = _RE_MULT.search(nome)
    if m:
        n = int(m.group(1))
        if 1 < n <= 24:  # sanidade: ignora números absurdos
            return n
    return 1


def preco_por_unidade(preco: float, medida: Medida | None) -> tuple[float | None, str | None]:
    """Devolve (valor_por_unidade_base, rótulo). (None, None) se não dá para normalizar."""
    if medida is None or medida.qtd_base <= 0:
        return None, None
    return round(preco / medida.qtd_base, 4), medida.rotulo


if __name__ == "__main__":  # auto-teste rápido: python -m analise.normalizar
    casos = [
        "Café Torrado e Moído Extraforte Brasileiro 500g",
        "Refrigerante Coca-Cola 2L",
        "Leite Integral Italac 1,5 L",
        "Água Mineral 6x500ml",
        "Margarina Delícia 500g com sal Leve 02 unidade",
        "Toalha Papel Folha Dupla Kitchen Com 2 Rolos",
        "Azeite Português Vila Flor Extra Virgem 500ml",
        "Arroz Tio João 5kg",
        "Sabonete Dove 90g c/ 6",
        "Papel Higiênico Neve 30m",  # não deve normalizar (metros)
    ]
    for nome in casos:
        med = extrair_medida(nome)
        ppu, rot = preco_por_unidade(10.0, med)
        print(f"{nome:52} -> {med}  (R$10 => {ppu} {rot or ''})")
