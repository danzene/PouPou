"""Atacadão — atacarejo VTEX (FastStore) com presença na RM de Campinas.

Loja alvo: /loja/campinas-dom-pedro (storeId 663, seller atacadaobr663).
Verificado empiricamente que o PREÇO ONLINE é uniforme: a simulação de checkout
para o seller da loja 663 devolve o mesmo valor do seller default "1". Logo, o
`catalog_system` (seller 1) já reflete o preço da loja de Campinas, e coletamos
com o coletor VTEX genérico — só registramos `loja_ref` para transparência.

(Se algum dia um item divergir por loja, o refino é a API de simulação por seller.)
"""
from __future__ import annotations

from .vtex import ColetorVTEX


class ColetorAtacadao(ColetorVTEX):
    def __init__(self, *, url_base: str = "https://www.atacadao.com.br", **kwargs) -> None:
        kwargs.setdefault("slug", "atacadao")
        kwargs.setdefault("nome", "Atacadão")
        kwargs.setdefault("cidade", "Campinas (Dom Pedro)")
        super().__init__(url_base=url_base, **kwargs)

    def _parse_produto(self, prod: dict):
        # O VTEX do Atacadão devolve o link no host de checkout `secure.atacadao.com.br`,
        # que não é navegável (dá erro/404). O site público é o www.
        item = super()._parse_produto(prod)
        if item is not None and item.url:
            item.url = item.url.replace("secure.atacadao.com.br", "www.atacadao.com.br")
        return item
