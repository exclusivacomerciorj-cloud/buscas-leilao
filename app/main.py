"""
API principal — Buscas Leilão.

Endpoints:
  GET  /                        → health check
  GET  /docs                    → Swagger UI

  POST /api/v1/scrape/caixa     → dispara scraping da Caixa
  POST /api/v1/scrape/olx       → dispara scraping do OLX
  GET  /api/v1/scrape/logs      → histórico de scraping

  GET  /api/v1/properties       → lista imóveis (com filtros)
  GET  /api/v1/properties/{id}  → detalhe de um imóvel
  GET  /api/v1/properties/{id}/report → PDF de viabilidade

  POST /api/v1/properties/{id}/analyze → força análise de um imóvel
  GET  /api/v1/analyses         → lista análises recentes (top oportunidades)

  POST /api/v1/alerts/test      → testa disparo de alerta
"""

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import desc, func

from app.core.config import get_settings
from app.core.logger import logger
from app.db.session import get_db, check_db_connection
from app.api.routes.legal import router as legal_router
from app.models.property import (
    Property, PropertyAnalysis, ScrapeLog,
    PropertySource, PropertyStatus, PropertyType, AuctionType
)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Buscas Leilão API iniciando...")
    check_db_connection()
    # Cria todas as tabelas automaticamente
    from app.db.session import engine, Base
    from app.models import property  # noqa
    Base.metadata.create_all(bind=engine)
    logger.info("✅ Tabelas criadas/verificadas")
    yield
    logger.info("API encerrada.")


app = FastAPI(
    title="Buscas Leilão — API",
    description="Plataforma de Inteligência Imobiliária: leilões, retomados e mercado tradicional",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(legal_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Em produção: restringir para o domínio do frontend
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Health Check ─────────────────────────────────────────────────────────────

@app.get("/", tags=["status"])
def health_check():
    return {
        "status": "online",
        "service": "Buscas Leilão API",
        "version": "1.0.0",
        "env": settings.APP_ENV,
    }


@app.get("/health", tags=["status"])
def health_detail(db: Session = Depends(get_db)):
    db_ok = True
    try:
        db.execute(__import__("sqlalchemy").text("SELECT 1"))
    except Exception:
        db_ok = False

    return {
        "api": "ok",
        "database": "ok" if db_ok else "error",
        "settings": {
            "openai_configured": bool(settings.OPENAI_API_KEY),
            "whatsapp_configured": bool(settings.EVOLUTION_API_URL),
            "email_configured": bool(settings.SMTP_USER),
            "proxies_count": len(settings.proxies),
            "alert_min_score": settings.ALERT_MIN_SCORE,
        },
    }


# ─── Scraping ─────────────────────────────────────────────────────────────────

@app.post("/api/v1/scrape/caixa", tags=["scraping"])
def trigger_caixa_scraping(ufs: Optional[list] = None):
    """Dispara o scraping da Caixa Econômica Federal (assíncrono via Celery)."""
    from app.tasks.worker import scrape_caixa
    task = scrape_caixa.delay(ufs=ufs or ["RJ"])
    return {"task_id": task.id, "status": "queued", "source": "caixa"}


@app.post("/api/v1/scrape/olx", tags=["scraping"])
def trigger_olx_scraping(neighborhoods: Optional[list] = None):
    """Dispara o scraping do OLX."""
    from app.tasks.worker import scrape_olx
    task = scrape_olx.delay(neighborhoods=neighborhoods)
    return {"task_id": task.id, "status": "queued", "source": "olx"}


@app.post("/api/v1/scrape/zap", tags=["scraping"])
def trigger_zap_scraping(neighborhoods: Optional[list] = None):
    """Dispara o scraping do ZAP Imóveis."""
    from app.tasks.zap import scrape_zap
    task = scrape_zap.delay(neighborhoods=neighborhoods)
    return {"task_id": task.id, "status": "queued", "source": "zap"}


@app.post("/api/v1/scrape/pipeline", tags=["scraping"])
def trigger_full_pipeline():
    """Dispara o pipeline completo: scraping → análise → alertas."""
    from app.tasks.worker import full_pipeline
    task = full_pipeline.delay()
    return {"task_id": task.id, "status": "queued", "message": "Pipeline completo iniciado"}


@app.get("/api/v1/scrape/logs", tags=["scraping"])
def get_scrape_logs(
    source: Optional[str] = None,
    limit: int = Query(default=20, le=100),
    db: Session = Depends(get_db),
):
    """Histórico de execuções dos scrapers."""
    query = db.query(ScrapeLog).order_by(desc(ScrapeLog.started_at))
    if source:
        query = query.filter(ScrapeLog.source == source)
    logs = query.limit(limit).all()

    return [
        {
            "id": str(log.id),
            "source": log.source.value,
            "started_at": log.started_at,
            "finished_at": log.finished_at,
            "total_found": log.total_found,
            "new_properties": log.new_properties,
            "updated_properties": log.updated_properties,
            "errors": log.errors,
            "success": log.success,
        }
        for log in logs
    ]


# ─── Imóveis ──────────────────────────────────────────────────────────────────

@app.get("/api/v1/properties", tags=["properties"])
def list_properties(
    source: Optional[str] = None,
    neighborhood: Optional[str] = None,
    property_type: Optional[str] = None,
    auction_type: Optional[str] = None,
    min_score: Optional[float] = None,
    min_discount: Optional[float] = None,
    max_price: Optional[float] = None,
    min_price: Optional[float] = None,
    only_unoccupied: bool = False,
    order_by: str = Query(default="score", enum=["score", "discount", "price", "created_at"]),
    limit: int = Query(default=20, le=100),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """
    Lista imóveis com filtros. Retorna os dados básicos + score de cada um.
    Use min_score >= 70 para ver apenas as melhores oportunidades.
    """
    query = db.query(Property, PropertyAnalysis).outerjoin(
        PropertyAnalysis, PropertyAnalysis.property_id == Property.id
    ).filter(Property.status == PropertyStatus.ATIVO)

    if source:
        query = query.filter(Property.source == source)
    if neighborhood:
        query = query.filter(Property.neighborhood.ilike(f"%{neighborhood}%"))
    if property_type:
        query = query.filter(Property.property_type == property_type)
    if auction_type:
        query = query.filter(Property.auction_type == auction_type)
    if min_score is not None:
        query = query.filter(PropertyAnalysis.opportunity_score >= min_score)
    if min_discount is not None:
        query = query.filter(PropertyAnalysis.real_discount_pct >= min_discount)
    if max_price is not None:
        query = query.filter(Property.asking_price <= max_price)
    if min_price is not None:
        query = query.filter(Property.asking_price >= min_price)
    if only_unoccupied:
        from app.models.property import OccupationStatus
        query = query.filter(Property.occupation_status == OccupationStatus.DESOCUPADO)

    # Ordenação
    if order_by == "score":
        query = query.order_by(desc(PropertyAnalysis.opportunity_score))
    elif order_by == "discount":
        query = query.order_by(desc(PropertyAnalysis.real_discount_pct))
    elif order_by == "price":
        query = query.order_by(Property.asking_price)
    else:
        query = query.order_by(desc(Property.created_at))

    total = query.count()
    results = query.offset(offset).limit(limit).all()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "properties": [
            _serialize_property(prop, analysis)
            for prop, analysis in results
        ],
    }


@app.get("/api/v1/properties/top", tags=["properties"])
def get_top_opportunities(
    limit: int = Query(default=10, le=50),
    db: Session = Depends(get_db),
):
    """Top oportunidades do momento (score >= 70, ordenadas por score)."""
    results = db.query(Property, PropertyAnalysis).join(
        PropertyAnalysis, PropertyAnalysis.property_id == Property.id
    ).filter(
        Property.status == PropertyStatus.ATIVO,
        PropertyAnalysis.opportunity_score >= 70,
    ).order_by(
        desc(PropertyAnalysis.opportunity_score)
    ).limit(limit).all()

    return {
        "count": len(results),
        "opportunities": [_serialize_property(p, a) for p, a in results],
    }


@app.get("/api/v1/properties/{property_id}", tags=["properties"])
def get_property(property_id: str, db: Session = Depends(get_db)):
    """Detalhe completo de um imóvel."""
    import uuid
    try:
        pid = uuid.UUID(property_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="ID inválido")

    result = db.query(Property, PropertyAnalysis).outerjoin(
        PropertyAnalysis, PropertyAnalysis.property_id == Property.id
    ).filter(Property.id == pid).first()

    if not result:
        raise HTTPException(status_code=404, detail="Imóvel não encontrado")

    prop, analysis = result
    data = _serialize_property(prop, analysis)

    # Dados extras no detalhe
    data["description"] = prop.description
    data["photos"] = prop.photos or []
    data["extra_data"] = prop.extra_data or {}
    if analysis:
        data["score_breakdown"] = analysis.score_breakdown
        data["legal_issues"] = analysis.legal_issues
        data["ai_summary"] = analysis.ai_summary

    return data


@app.get("/api/v1/properties/{property_id}/report", tags=["properties"])
def get_property_report(property_id: str, db: Session = Depends(get_db)):
    """Gera e retorna o PDF de viabilidade financeira."""
    import uuid
    try:
        pid = uuid.UUID(property_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="ID inválido")

    prop = db.query(Property).filter(Property.id == pid).first()
    if not prop:
        raise HTTPException(status_code=404, detail="Imóvel não encontrado")

    analysis = db.query(PropertyAnalysis).filter(
        PropertyAnalysis.property_id == pid
    ).order_by(desc(PropertyAnalysis.created_at)).first()

    if not analysis:
        # Gera análise na hora se não existir
        from app.services.analyzers.pricing import PricingEngine
        engine = PricingEngine(db)
        analysis = engine.analyze(prop)
        db.add(analysis)
        db.commit()

    from app.services.analyzers.simulator import generate_pdf_report
    try:
        pdf_bytes = generate_pdf_report(prop, analysis)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao gerar PDF: {str(e)}")

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="viabilidade_{property_id[:8]}.pdf"'
        },
    )


@app.post("/api/v1/properties/{property_id}/analyze", tags=["properties"])
def trigger_analysis(property_id: str, db: Session = Depends(get_db)):
    """Força a análise (re)processamento de um imóvel."""
    from app.tasks.worker import analyze_property
    task = analyze_property.delay(property_id)
    return {"task_id": task.id, "property_id": property_id, "status": "queued"}


# ─── Estatísticas ─────────────────────────────────────────────────────────────

@app.get("/api/v1/stats", tags=["stats"])
def get_stats(db: Session = Depends(get_db)):
    """Estatísticas gerais da plataforma."""
    total_properties = db.query(func.count(Property.id)).scalar()
    total_analyzed = db.query(func.count(PropertyAnalysis.id)).scalar()
    top_opportunities = db.query(func.count(PropertyAnalysis.id)).filter(
        PropertyAnalysis.opportunity_score >= 70
    ).scalar()

    by_source = db.query(
        Property.source, func.count(Property.id)
    ).group_by(Property.source).all()

    avg_score = db.query(func.avg(PropertyAnalysis.opportunity_score)).scalar()
    avg_discount = db.query(func.avg(PropertyAnalysis.real_discount_pct)).filter(
        PropertyAnalysis.real_discount_pct.isnot(None)
    ).scalar()

    return {
        "total_properties": total_properties,
        "total_analyzed": total_analyzed,
        "top_opportunities": top_opportunities,
        "avg_score": round(avg_score, 1) if avg_score else None,
        "avg_discount_pct": round(avg_discount, 1) if avg_discount else None,
        "by_source": {s.value: c for s, c in by_source},
    }

@app.post("/api/v1/import/caixa", tags=["scraping"])
async def import_caixa_csv(
    file: UploadFile,
    db: Session = Depends(get_db),
):
    """Importa CSV da Caixa enviado localmente."""
    from app.services.scrapers.caixa import CaixaScraper
    from app.models.property import ScrapeLog
    from datetime import datetime

    content = await file.read()
    text = content.decode("utf-8-sig", errors="replace")

    scraper = CaixaScraper()
    raw_properties = scraper._parse_csv(text, "RJ")

    new_count = 0
    for data in raw_properties:
        external_id = data.get("external_id")
        existing = None
        if external_id:
            existing = db.query(Property).filter(
                Property.external_id == external_id,
                Property.source == PropertySource.CAIXA,
            ).first()
        if not existing:
            prop = Property(**{
                k: v for k, v in data.items()
                if hasattr(Property, k) and k not in ("id",)
            })
            db.add(prop)
            new_count += 1

    log = ScrapeLog(
        source=PropertySource.CAIXA,
        started_at=datetime.utcnow(),
        finished_at=datetime.utcnow(),
        total_found=len(raw_properties),
        new_properties=new_count,
        success=True,
    )
    db.add(log)
    db.commit()

    return {"imported": new_count, "total_found": len(raw_properties)}
    
# ─── Helpers ──────────────────────────────────────────────────────────────────

def _serialize_property(prop: Property, analysis: Optional[PropertyAnalysis]) -> dict:
    price = prop.min_bid or prop.asking_price
    return {
        "id": str(prop.id),
        "source": prop.source.value,
        "source_url": prop.source_url,
        "title": prop.title,
        "neighborhood": prop.neighborhood,
        "city": prop.city,
        "state": prop.state,
        "address": prop.address,
        "property_type": prop.property_type.value if prop.property_type else None,
        "auction_type": prop.auction_type.value if prop.auction_type else None,
        "occupation_status": prop.occupation_status.value if prop.occupation_status else None,
        "asking_price": prop.asking_price,
        "min_bid": prop.min_bid,
        "appraised_value": prop.appraised_value,
        "total_area": prop.total_area,
        "bedrooms": prop.bedrooms,
        "bathrooms": prop.bathrooms,
        "parking_spots": prop.parking_spots,
        "price_per_sqm": prop.price_per_sqm,
        "first_seen_at": prop.first_seen_at,
        "last_seen_at": prop.last_seen_at,
        # Análise
        "opportunity_score": analysis.opportunity_score if analysis else None,
        "real_discount_pct": analysis.real_discount_pct if analysis else None,
        "market_value_estimated": analysis.market_value_estimated if analysis else None,
        "estimated_roi_pct": analysis.estimated_roi_pct if analysis else None,
        "estimated_profit": analysis.estimated_profit if analysis else None,
        "legal_risk_score": analysis.legal_risk_score if analysis else None,
        "analyzed": analysis is not None,
    }
