"""
Tasks Celery — pipeline assíncrono de scraping e análise.

Tasks:
  - scrape_caixa          → roda scraper da Caixa
  - scrape_olx            → roda scraper do OLX
  - analyze_property      → analisa um imóvel específico
  - analyze_all_pending   → analisa todos os imóveis sem análise
  - dispatch_alerts       → verifica e dispara alertas
  - full_pipeline         → encadeia tudo (scraping → análise → alertas)

Agendamento (Celery Beat):
  - full_pipeline a cada 6 horas
  - analyze_all_pending a cada 1 hora
"""

import asyncio
from datetime import datetime
from typing import Optional

from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings
from app.core.logger import logger

settings = get_settings()

# ─── Configuração do Celery ───────────────────────────────────────────────────

celery_app = Celery(
    "buscas_leilao",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="America/Sao_Paulo",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Agendamento automático
    beat_schedule={
        "full-pipeline-every-6h": {
            "task": "app.tasks.worker.full_pipeline",
            "schedule": crontab(hour="*/6"),
        },
        "analyze-pending-every-hour": {
            "task": "app.tasks.worker.analyze_all_pending",
            "schedule": crontab(minute="*/30"),
        },
    },
)

# Alias para importação
worker = celery_app


def _run_async(coro):
    """Helper para rodar corrotinas async dentro de tasks Celery síncronas."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ─── Tasks de Scraping ────────────────────────────────────────────────────────

@celery_app.task(
    name="app.tasks.worker.scrape_caixa",
    bind=True,
    max_retries=3,
    default_retry_delay=300,
)
def scrape_caixa(self, ufs: Optional[list] = None):
    """Roda o scraper da Caixa e persiste os imóveis no banco."""
    from app.services.scrapers.caixa import CaixaScraper
    from app.db.session import SessionLocal
    from app.models.property import Property, PropertySource, ScrapeLog

    logger.info("[Task] Iniciando scraping da Caixa...")
    db = SessionLocal()
    log = ScrapeLog(source=PropertySource.CAIXA, started_at=datetime.utcnow())

    try:
        scraper = CaixaScraper(ufs=ufs)
        raw_properties = _run_async(scraper.run())

        new_count = 0
        updated_count = 0

        for data in raw_properties:
            external_id = data.get("external_id")
            source = data.get("source")

            # Verifica se já existe
            existing = None
            if external_id:
                existing = db.query(Property).filter(
                    Property.external_id == external_id,
                    Property.source == source,
                ).first()

            if existing:
                # Atualiza preço e status
                existing.asking_price = data.get("asking_price", existing.asking_price)
                existing.min_bid = data.get("min_bid", existing.min_bid)
                existing.last_seen_at = datetime.utcnow()
                updated_count += 1
            else:
                prop = Property(**{
                    k: v for k, v in data.items()
                    if hasattr(Property, k) and k not in ("id",)
                })
                db.add(prop)
                new_count += 1

        db.commit()

        log.total_found = len(raw_properties)
        log.new_properties = new_count
        log.updated_properties = updated_count
        log.finished_at = datetime.utcnow()
        log.success = True
        db.add(log)
        db.commit()

        logger.info(f"[Task/Caixa] Concluído: {new_count} novos, {updated_count} atualizados")
        return {"new": new_count, "updated": updated_count, "total": len(raw_properties)}

    except Exception as e:
        log.success = False
        log.error_details = {"error": str(e)}
        log.finished_at = datetime.utcnow()
        db.add(log)
        db.commit()
        logger.error(f"[Task/Caixa] Erro: {e}")
        raise self.retry(exc=e)
    finally:
        db.close()


@celery_app.task(
    name="app.tasks.worker.scrape_olx",
    bind=True,
    max_retries=3,
    default_retry_delay=300,
)
def scrape_olx(self, neighborhoods: Optional[list] = None):
    """Roda o scraper do OLX."""
    from app.services.scrapers.olx import OLXScraper
    from app.db.session import SessionLocal
    from app.models.property import Property, PropertySource, ScrapeLog

    logger.info("[Task] Iniciando scraping do OLX...")
    db = SessionLocal()
    log = ScrapeLog(source=PropertySource.OLX, started_at=datetime.utcnow())

    try:
        scraper = OLXScraper(neighborhoods=neighborhoods)
        raw_properties = _run_async(scraper.run())

        new_count = 0
        for data in raw_properties:
            external_id = data.get("external_id")
            existing = None
            if external_id:
                existing = db.query(Property).filter(
                    Property.external_id == external_id,
                    Property.source == PropertySource.OLX,
                ).first()

            if not existing:
                prop = Property(**{
                    k: v for k, v in data.items()
                    if hasattr(Property, k) and k not in ("id",)
                })
                db.add(prop)
                new_count += 1

        db.commit()
        log.total_found = len(raw_properties)
        log.new_properties = new_count
        log.finished_at = datetime.utcnow()
        log.success = True
        db.add(log)
        db.commit()

        logger.info(f"[Task/OLX] Concluído: {new_count} novos")
        return {"new": new_count, "total": len(raw_properties)}

    except Exception as e:
        log.success = False
        log.error_details = {"error": str(e)}
        log.finished_at = datetime.utcnow()
        db.add(log)
        db.commit()
        logger.error(f"[Task/OLX] Erro: {e}")
        raise self.retry(exc=e)
    finally:
        db.close()


# ─── Tasks de Análise ─────────────────────────────────────────────────────────

@celery_app.task(name="app.tasks.worker.analyze_property")
def analyze_property(property_id: str):
    """Analisa um imóvel específico."""
    import uuid
    from app.db.session import SessionLocal
    from app.models.property import Property
    from app.services.analyzers.pricing import PricingEngine

    db = SessionLocal()
    try:
        prop = db.query(Property).filter(
            Property.id == uuid.UUID(property_id)
        ).first()

        if not prop:
            logger.warning(f"[Task/Analyze] Imóvel não encontrado: {property_id}")
            return

        engine = PricingEngine(db)
        analysis = engine.analyze(prop)
        db.add(analysis)
        db.commit()

        logger.info(
            f"[Task/Analyze] {property_id[:8]} → score={analysis.opportunity_score:.0f}"
        )
        return {
            "property_id": property_id,
            "score": analysis.opportunity_score,
            "roi": analysis.estimated_roi_pct,
        }
    except Exception as e:
        logger.error(f"[Task/Analyze] Erro: {e}")
        raise
    finally:
        db.close()


@celery_app.task(name="app.tasks.worker.analyze_all_pending")
def analyze_all_pending(limit: int = 100):
    """Analisa todos os imóveis que ainda não têm análise."""
    from sqlalchemy import not_, exists
    from app.db.session import SessionLocal
    from app.models.property import Property, PropertyAnalysis

    db = SessionLocal()
    try:
        # Busca imóveis sem análise
        pending = db.query(Property).filter(
            ~exists().where(PropertyAnalysis.property_id == Property.id)
        ).limit(limit).all()

        logger.info(f"[Task/AnalyzePending] {len(pending)} imóveis para analisar")

        for prop in pending:
            analyze_property.delay(str(prop.id))

        return {"queued": len(pending)}
    finally:
        db.close()


# ─── Task de Alertas ──────────────────────────────────────────────────────────

@celery_app.task(name="app.tasks.worker.dispatch_alerts")
def dispatch_alerts():
    """Verifica oportunidades acima do threshold e dispara alertas."""
    from app.db.session import SessionLocal
    from app.models.property import Property, PropertyAnalysis, Alert, AlertChannel, AlertStatus

    db = SessionLocal()
    try:
        # Busca análises acima do score mínimo que ainda não foram alertadas
        from sqlalchemy import not_, exists

        high_score_analyses = db.query(PropertyAnalysis).filter(
            PropertyAnalysis.opportunity_score >= settings.ALERT_MIN_SCORE,
            PropertyAnalysis.real_discount_pct >= settings.ALERT_MIN_DISCOUNT,
            ~exists().where(Alert.property_id == PropertyAnalysis.property_id),
        ).limit(20).all()

        logger.info(f"[Task/Alerts] {len(high_score_analyses)} alertas para disparar")

        alert_count = 0
        for analysis in high_score_analyses:
            prop = analysis.property
            if not prop:
                continue

            # Por enquanto dispara para e-mail configurado (depois vem do cadastro de usuários)
            if settings.SMTP_USER:
                from app.services.notifications.alerts import send_email_alert
                success = _run_async(send_email_alert(settings.SMTP_USER, prop, analysis))

                alert = Alert(
                    property_id=prop.id,
                    channel=AlertChannel.EMAIL,
                    recipient=settings.SMTP_USER,
                    status=AlertStatus.ENVIADO if success else AlertStatus.FALHOU,
                    sent_at=datetime.utcnow() if success else None,
                )
                db.add(alert)
                alert_count += 1

        db.commit()
        logger.info(f"[Task/Alerts] {alert_count} alertas enviados")
        return {"sent": alert_count}
    finally:
        db.close()


# ─── Pipeline Completo ────────────────────────────────────────────────────────

@celery_app.task(name="app.tasks.worker.full_pipeline")
def full_pipeline():
    """
    Encadeia todo o pipeline:
    scrape_caixa → scrape_olx → analyze_all_pending → dispatch_alerts
    """
    from celery import chain, chord

    logger.info("[Task/Pipeline] Iniciando pipeline completo...")

    pipeline = chain(
        scrape_caixa.s(),
        scrape_olx.s(),
        analyze_all_pending.s(),
        dispatch_alerts.s(),
    )
    result = pipeline.delay()
    return {"pipeline_id": str(result.id)}
