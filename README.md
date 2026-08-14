# Comparador de Preços — RM Campinas (MVP)

Coleta preços **online** de produtos **industrializados (com EAN)** de supermercados
da Região Metropolitana de Campinas, guarda em SQLite com **histórico**, e (nas
próximas fases) permite buscar um produto e ver o ranking do mais barato ao mais caro.

## Estado atual: **MVP funcional + camada de categorias/identidade**

- Schema SQLite + `database.py` (com `categorias`, identidade de mercado e migração idempotente).
- Coletores plugáveis: **VTEX genérico** (`coletores/vtex.py`) e **VIP Commerce**
  (`coletores/vipcommerce.py`), cada um reaproveitável entre mercados da mesma plataforma.
- **6 mercados ativos**: Savegnago, Covabra, Atacadão (VTEX) + Enxuto e Super Dalben
  (VIP Commerce) + **Tenda Atacado** (API própria). Todos com token/coleta automáticos.
- **Categorias** (`config/categorias.json`): Mercearia, Bebidas, Limpeza, Higiene, Laticínios
  (Hortifrúti entra como "em breve"). Cada produto é etiquetado pela categoria do termo que o achou.
- **Identidade visual** por mercado: `cor_badge` (monograma) + `loja_ref` gravados.
- **Normalização** (`analise/normalizar.py`): extrai gramatura do nome e calcula
  R$/kg, R$/L, R$/un (~97% de cobertura; casos ambíguos ficam sem valor de propósito).
- **Ranking** (`analise/ranking.py`): busca por nome (acento/ordem-insensível) ou EAN
  e ordena do mais barato ao mais caro entre mercados.
- **API FastAPI** (`api/main.py`): `/categorias`, `/categoria/{slug}`, `/buscar?q=`,
  `/produto/{ean}`, `/historico/{ean}`, **cesta** (`/cesta`) e **lista/alertas**
  (`/lista` GET/POST com quantidade, `/lista/{ean}` DELETE, `/alertas`). Cada linha de
  preço vem com identidade do mercado, **link do produto no site**, sparkline e tendência.
- **Cesta comparada** com **duas estratégias claras**: (1) **"tudo numa loja só"** (a loja
  única mais barata para a cesta inteira) e (2) **"dividido"** (cada item no mercado mais
  barato, com a lista do que pegar em cada loja e quanto economiza). Quando nenhuma loja
  tem todos os itens, o app avisa e recomenda o dividido. Itens com **quantidade** editável.
- **Verificador de compra (checklist)**: marque cada item conforme pega no mercado, com
  barra de progresso ("3 de 8 no carrinho"); o estado persiste (campo `lista.comprado`).
- **Alertas de preço** (preço-alvo por item) + **coleta diária agendada** (Windows Task
  Scheduler → `coleta_diaria.bat`) + **notificação toast** (`notificar_alertas.py`).
- Front redesenhado: clean claro com acento verde "economia", cards com miniatura,
  cesta comparada como tela principal. Acessível (AA, teclado), mobile-first, tema claro/escuro.
- **Front** (`front/index.html`): página única acessível (AA, mobile-first) — busca +
  chips de categoria (com "em breve"), cards expansíveis, ranking por mercado com
  **monograma colorido**, **sparkline** de histórico (rótulo textual + `aria-label`),
  selo "menor preço", e rótulo "preço de loja online · atualizado em {data}".
- `coletar.py` popula tudo; `amostra.py` e `analise/renormalizar.py` como utilitários.

### Plataformas identificadas (RM Campinas)
| Mercado          | Plataforma   | Status |
|------------------|--------------|--------|
| Savegnago        | VTEX         | ✅ ativo |
| Covabra          | VTEX         | ✅ ativo |
| Atacadão         | VTEX         | ✅ ativo (atacarejo; loja `campinas-dom-pedro`, preço online uniforme) |
| Enxuto           | VIP Commerce | ✅ ativo (org 153; token gerado automaticamente) |
| Super Dalben     | VIP Commerce | ✅ ativo (org 29; token gerado automaticamente) |
| Tenda Atacado    | API própria  | ✅ ativo (token X-Authorization gerado automaticamente) |
| Super Pague Menos| Convertiez   | ✅ ativo (fallback HTML: EAN só na página do produto) |
| Carrefour        | VTEX         | ⛔ adiado — Cloudflare bloqueia todo `/api/` (503) até no navegador real |

> `paguemenos.com.br` é a **farmácia** (Fortaleza); o supermercado de Campinas é
> `superpaguemenos.com.br`. Enxuto usa `enxuto.com` (não `.com.br`).

> "VTEX-first": onde o mercado é VTEX, consumimos o JSON de catálogo
> (`/api/catalog_system/pub/products/search/`) em vez de parsear HTML.

## Como rodar

### Jeito fácil (Windows): **dê duplo-clique em `iniciar.bat`**
Ele prepara o ambiente na primeira vez, sobe o servidor e abre o navegador em
http://localhost:8080. Para parar, feche a janela preta. Pronto.

### Manual (dev)

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
copy .env.example .env            # ajuste se quiser

python coletar.py                 # todos os mercados ativos
python coletar.py savegnago       # só um mercado
python amostra.py                 # query de amostra (contagens, preços, histórico)

python -m analise.ranking "café brasileiro"   # ranking por nome (CLI)
python -m analise.ranking 7891000100103       # ranking por EAN (CLI)
python -m analise.renormalizar                # recalcula R$/un sem re-scraping

python -m uvicorn api.main:app --port 8000    # sobe API + front em http://localhost:8000

python notificar_alertas.py                   # checa a lista e notifica alertas (toast)
agendar_coleta.bat                            # registra a coleta diária no Windows (roda 1x)
```

## Arquitetura

```
config/mercados.json   # mercados: slug, url_base, plataforma, ativo, classe do coletor
config/sementes.json   # ~40 termos de busca (industrializados comuns)
coletores/base.py      # interface Coletor + ItemColeta + checagem de robots.txt
coletores/vtex.py      # coletor VTEX genérico (paginação, pacing, validação de EAN)
coletores/savegnago.py # config específica do Savegnago (delega ao genérico)
db/schema.sql          # DDL; precos é APPEND-ONLY (histórico)
db/database.py         # conexão + upsert de produtos/mercados + insert de preços
coletar.py             # entrypoint: orquestra, isola falhas por mercado
amostra.py             # query de amostra
```

### Decisões travadas
- **EAN = chave única** entre mercados. Só entram GTINs válidos (8/12/13/14 dígitos);
  itens sem código de barras (granel/hortifrúti) são ignorados de propósito.
- **Histórico append-only**: cada coleta grava um novo snapshot em `precos`; nada é
  sobrescrito. Rodar 2x gera 2 snapshots por produto.
- **Preço ONLINE**, não de balcão físico — marcado em `precos.origem = 'online'`.
- **Coleta responsável**: respeita `robots.txt`, delay ≥ 1s por domínio (configurável),
  User-Agent identificável (header `From`/`X-Bot-Contato`), sem paralelismo no mesmo domínio.

## ⚠️ Aviso legal (ToS)

Fazer scraping pode conflitar com os **Termos de Uso** de cada site. O uso desta
ferramenta é de responsabilidade do operador. Mantenha **volume baixo e uso pessoal**,
respeite `robots.txt` e o pacing. Isto não é aconselhamento jurídico.

## Próximas fases (pós-MVP)
- Mais mercados: **Dalben** (coletor VIP Commerce), **Carrefour** (sessão de browser p/ anti-bot).
- Regionalização do preço VTEX (fixar loja de Campinas via `sc`/CEP).
- Lista de compras otimizada e **alertas** de queda de preço (o schema append-only já comporta).
- Agendar a coleta (ex.: diária) para engordar o histórico.
