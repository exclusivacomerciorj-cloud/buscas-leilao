# Buscas Leilão — Plataforma de Inteligência Imobiliária

Motor de análise automatizada de oportunidades imobiliárias: leilões, retomados bancários e mercado tradicional abaixo do preço.

## Stack

| Camada | Tecnologia |
|---|---|
| API | FastAPI + Python 3.11 |
| Banco | PostgreSQL + PostGIS |
| Cache / Filas | Redis + Celery |
| Scraping | Playwright + BeautifulSoup |
| IA | OpenAI GPT-4o |
| Análise | Scikit-Learn + Pandas |
| Infra | Docker Compose (local) → Railway/AWS |

## Estrutura

```
buscas-leilao/
├── app/
│   ├── api/routes/         # Endpoints FastAPI
│   ├── core/               # Config, segurança, logs
│   ├── db/                 # Sessão, migrations (Alembic)
│   ├── models/             # SQLAlchemy ORM
│   ├── schemas/            # Pydantic schemas
│   ├── services/
│   │   ├── scrapers/       # Caixa, OLX, ZAP, etc.
│   │   ├── analyzers/      # Precificação, score, simulador
│   │   └── notifications/  # WhatsApp, e-mail, alertas
│   └── tasks/              # Celery tasks
├── tests/
├── scripts/                # Seeds, utilitários
├── docs/
├── docker-compose.yml
├── Dockerfile
├── alembic.ini
└── requirements.txt
```

## Quick Start (local)

```bash
# 1. Clone e entre no projeto
git clone https://github.com/seu-usuario/buscas-leilao.git
cd buscas-leilao

# 2. Copie e preencha as variáveis de ambiente
cp .env.example .env

# 3. Suba a infra (Postgres + Redis)
docker-compose up -d db redis

# 4. Crie o virtualenv e instale dependências
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 5. Rode as migrations
alembic upgrade head

# 6. Instale os browsers do Playwright
playwright install chromium

# 7. Suba a API
uvicorn app.main:app --reload

# 8. Em outro terminal, suba o worker Celery
celery -A app.tasks.worker worker --loglevel=info

# 9. Acesse
# API:   http://localhost:8000
# Docs:  http://localhost:8000/docs
```

## Módulos

### Scraping (MVP)
- `CaixaScraper` — retomados e leilões da Caixa Econômica
- `OLXScraper` — imóveis do OLX com filtros por região
- `ZAPScraper` — ZAP Imóveis com dados de m² e histórico

### Análise
- `PricingEngine` — calcula valor de m² e compara com vizinhos
- `OpportunityScorer` — score 0-100 baseado em desconto, liquidez e risco
- `FinancialSimulator` — ROI, reforma (CUB/RJ), ITBI, custos cartoriais

### Notificações
- `AlertService` — dispara alertas quando score > threshold configurado

## Variáveis de Ambiente

Veja `.env.example` para a lista completa.

## Deploy (Railway)

```bash
railway login
railway init
railway add postgresql
railway add redis
railway up
```
