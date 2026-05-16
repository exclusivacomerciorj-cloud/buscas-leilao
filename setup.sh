#!/bin/bash
# setup.sh — Configura o ambiente local do Buscas Leilão

set -e

echo "🏠 Buscas Leilão — Setup"
echo "========================"

# Verifica Python
if ! command -v python3 &>/dev/null; then
    echo "❌ Python 3 não encontrado. Instale Python 3.11+"
    exit 1
fi

# Cria .env se não existir
if [ ! -f .env ]; then
    cp .env.example .env
    echo "✅ .env criado — preencha com suas chaves antes de rodar"
fi

# Virtualenv
if [ ! -d venv ]; then
    python3 -m venv venv
    echo "✅ Virtualenv criado"
fi

source venv/bin/activate

# Instala dependências
pip install -r requirements.txt --quiet
echo "✅ Dependências instaladas"

# Playwright
playwright install chromium --quiet
echo "✅ Browser Playwright instalado"

# Docker (infra)
if command -v docker &>/dev/null; then
    docker-compose up -d db redis
    echo "✅ PostgreSQL e Redis rodando"
    sleep 3

    # Migrations
    alembic upgrade head
    echo "✅ Migrations executadas"
else
    echo "⚠️  Docker não encontrado. Suba PostgreSQL e Redis manualmente."
    echo "   DATABASE_URL e REDIS_URL estão no .env"
fi

echo ""
echo "🚀 Tudo pronto! Para iniciar:"
echo "   source venv/bin/activate"
echo "   uvicorn app.main:app --reload"
echo ""
echo "   Em outro terminal (worker):"
echo "   celery -A app.tasks.worker worker --loglevel=info"
echo ""
echo "   Docs: http://localhost:8000/docs"
