"""
Task Celery para scraping do ZAP Imóveis.
Adicionar ao worker existente.
"""

from app.tasks.worker import celery_app
from app.core.logger import logger
from typing import Optional


@celery_app.task(
    name="app.tasks.worker.scrape_zap",
    bind=True,
    max_retries=3,
    default_retry_delay=300,
)
def scrape_zap(self, neighborhoods: Optional[list] = None):
    """Roda o scraper do ZAP Imóveis."""
    import asyncio
    from datetime import datetime
    from app.services.scrapers.zap import ZAPScraper
    from app.db.session import SessionLocal
    from app.models.property import Property, PropertySource, ScrapeLog

    logger.info("[Task] Iniciando scraping do ZAP...")
    db = SessionLocal()
    log = ScrapeLog(source=PropertySource.ZAP, started_at=datetime.utcnow())

    try:
        scraper = ZAPScraper(neighborhoods=neighborhoods)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        raw_properties = loop.run_until_complete(scraper.run())
        loop.close()

        new_count = 0
        updated_count = 0

        for data in raw_properties:
            external_id = data.get("external_id")
            existing = None
            if external_id:
                existing = db.query(Property).filter(
                    Property.external_id == external_id,
                    Property.source == PropertySource.ZAP,
                ).first()

            if existing:
                existing.asking_price = data.get("asking_price", existing.asking_price)
                from datetime import datetime
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
        log.finished_at = __import__('datetime').datetime.utcnow()
        log.success = True
        db.add(log)
        db.commit()

        logger.info(f"[Task/ZAP] Concluído: {new_count} novos, {updated_count} atualizados")
        return {"new": new_count, "updated": updated_count, "total": len(raw_properties)}

    except Exception as e:
        log.success = False
        log.error_details = {"error": str(e)}
        import datetime
        log.finished_at = datetime.datetime.utcnow()
        db.add(log)
        db.commit()
        logger.error(f"[Task/ZAP] Erro: {e}")
        raise self.retry(exc=e)
    finally:
        db.close()
