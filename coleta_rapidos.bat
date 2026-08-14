@echo off
REM Crawl COMPLETO dos mercados rapidos (VTEX/VIP/Tenda) — 1x/dia. E o crawl pesado
REM (varre todas as categorias) -> 1x/dia pra nao arriscar bloqueio. Entre um dia e
REM outro, o refrescar_precos.bat (3/3h) mantem os VTEX fresquinhos de forma leve.
REM O historico so grava mudanca de preco (dedup no inserir_preco), entao nao ha
REM snapshot repetido pra colapsar depois.
cd /d "%~dp0"
".venv\Scripts\python.exe" coletar.py savegnago covabra atacadao oba enxuto dalben taquaral tenda
".venv\Scripts\python.exe" notificar_alertas.py
REM Manutencao diaria: poda historico (90 dias) + VACUUM. Roda por ultimo.
".venv\Scripts\python.exe" manutencao.py
REM CONFERIDOR (auto-cura): amostra ao vivo x banco e SINCRONIZA na hora as diferencas,
REM pra garantir que o app nao fica diferente do site. Loga tudo em dados\conferencia.log.
".venv\Scripts\python.exe" conferir_precos.py --corrigir > dados\conferencia.log 2>&1
