"""Savegnago — mercado VTEX com lojas na RM de Campinas.

Só declara config e delega toda a lógica ao coletor VTEX genérico.
Confirmado empiricamente como VTEX (endpoint /api/catalog_system devolve JSON
com ean + sellers[].commertialOffer.Price).
"""
from __future__ import annotations

from .vtex import ColetorVTEX


class ColetorSavegnago(ColetorVTEX):
    def __init__(self, *, url_base: str = "https://www.savegnago.com.br", **kwargs) -> None:
        kwargs.setdefault("slug", "savegnago")
        kwargs.setdefault("nome", "Savegnago")
        kwargs.setdefault("cidade", "Campinas (RM)")
        # sales_channel: None = canal público padrão. Fixar a loja de Campinas
        # (regionalização) fica para a Fase 2, quando refinarmos o preço regional.
        super().__init__(url_base=url_base, **kwargs)
