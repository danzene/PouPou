"""Interface comum dos coletores + utilidades de coleta responsável."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger("coletor")


class MercadoIndisponivel(Exception):
    """Rate-limit/bloqueio persistente: aborta o mercado rápido (o orquestrador
    trata como falha isolada e mantém o preço antigo, marcado como defasado)."""


@dataclass
class ItemColeta:
    """Um produto+preço observado num mercado, já normalizado para o schema."""
    ean: str
    nome: str | None
    marca: str | None
    unidade: str | None
    qtd_embalagem: float | None
    imagem_url: str | None
    preco: float
    preco_lista: float | None = None  # "de" (ListPrice), quando houver
    categoria: str | None = None      # slug da categoria do termo que achou o item
    url: str | None = None            # link da página do produto no site do mercado
    so_no_site: bool = False          # selo "SÓ NO SITE" (exclusivo online)
    preco_de: float | None = None     # preço "de" (antes do desconto), quando em promoção
    desconto_pct: int | None = None   # % de desconto anunciado no site (quando houver)
    oferta_tipo: str | None = None    # 'fidelidade' (exige cartão/app) | 'oferta' (pra todos) | None


@dataclass
class ItemPerecivel:
    """Item FRESCO/por peso SEM EAN universal (açougue, hortifrúti, padaria).

    Como não tem código de barras que sirva de chave entre mercados, ele NÃO
    entra no fluxo normal (produtos/precos por EAN). Fica numa tabela à parte,
    identificado pelo código interno do mercado, e é cruzado por NOME (aproximado).
    """
    cod_interno: str          # id/slug estável do item DENTRO do mercado
    nome: str
    preco: float
    unidade: str | None = None   # 'kg' | 'un' | ...
    imagem_url: str | None = None
    url: str | None = None
    so_no_site: bool = False          # selo "SÓ NO SITE" (exclusivo online)
    preco_de: float | None = None     # preço "de" (antes do desconto), quando em promoção
    desconto_pct: int | None = None   # % de desconto anunciado no site
    oferta_tipo: str | None = None    # 'fidelidade' (exige cartão/app) | 'oferta' (pra todos) | None


def chave_dedup(item) -> str:
    """Chave de deduplicação por rodada, unindo os dois tipos de item:
    EAN para ItemColeta; cod_interno (prefixado, pra não colidir com EAN) para perecível."""
    return item.ean if isinstance(item, ItemColeta) else f"p:{item.cod_interno}"


def perecivel_se_fresco(*, cod_interno, nome, preco, unidade=None, imagem_url=None,
                        url=None, so_no_site=False, preco_de=None, desconto_pct=None,
                        oferta_tipo=None):
    """Constrói um ItemPerecivel para um item SEM EAN — mas SÓ se o nome for de fato
    fresco (carnes/hortifrúti/padaria/frios/peixes/ovos). Assim os mercados que hoje
    descartam itens sem código de barras passam a alimentar a aba Frescos, sem poluir
    a tabela com item sem-EAN qualquer (que não teria como cruzar entre lojas). None
    quando não é fresco ou faltam dados essenciais."""
    from db.database import classificar_perecivel  # import tardio: evita acoplar no import-time
    if not nome or not cod_interno or preco is None or preco <= 0:
        return None
    if classificar_perecivel(nome) == "outros":
        return None
    return ItemPerecivel(
        cod_interno=str(cod_interno), nome=nome, preco=float(preco), unidade=unidade,
        imagem_url=imagem_url, url=url, so_no_site=so_no_site,
        preco_de=preco_de, desconto_pct=desconto_pct, oferta_tipo=oferta_tipo,
    )


class Coletor(ABC):
    """Cada mercado é um coletor isolado e plugável com esta interface.

    Um mercado que falhar não pode derrubar os outros — quem orquestra
    (`coletar.py`) trata a exceção; aqui a gente só levanta.
    """
    nome: str
    slug: str
    url_base: str
    plataforma: str
    cidade: str | None

    @abstractmethod
    def coletar(self, sementes: list[tuple[str, str]]) -> Iterator[ItemColeta]:
        """Gera ItemColeta para cada produto encontrado.

        `sementes` é uma lista de pares (termo, categoria_slug); cada item gerado
        herda a categoria do termo que o encontrou.
        """
        raise NotImplementedError


def robots_permite(url_base: str, caminho: str, user_agent: str, headers: dict) -> bool:
    """Checa robots.txt antes de coletar. Em caso de dúvida/erro, é permissivo
    apenas se o robots não pôde ser lido (padrão da web); se leu e proibiu, respeita."""
    robots_url = url_base.rstrip("/") + "/robots.txt"
    try:
        resp = httpx.get(robots_url, headers=headers, timeout=15.0, follow_redirects=True)
        if resp.status_code >= 400:
            log.info("robots.txt indisponível (%s) em %s — seguindo com cautela", resp.status_code, robots_url)
            return True
        rp = RobotFileParser()
        rp.parse(resp.text.splitlines())
        permitido = rp.can_fetch(user_agent, url_base.rstrip("/") + caminho)
        if not permitido:
            log.warning("robots.txt PROÍBE %s%s para %s", url_base, caminho, user_agent)
        return permitido
    except Exception as exc:  # noqa: BLE001
        log.info("Não consegui ler robots.txt (%s) — seguindo com cautela", exc)
        return True
