# Imagem de produção do PouPou (FastAPI + front) — roda no Fly.io, região GRU (São Paulo),
# do lado do banco (Supabase sa-east-1) pra latência ~2ms por query em vez de ~150ms.
FROM python:3.12-slim

WORKDIR /app

# Dependências primeiro (cache de layer: só reinstala quando requirements muda)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código da aplicação (o .dockerignore tira .env, .venv, dados/, .git…)
COPY . .

# A app escuta nesta porta; o fly.toml (internal_port) aponta pra ela.
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT}"]
