"""
Task Celery para análise jurídica em background.

Chamada automaticamente após scraping da Caixa
quando o imóvel tem URL de edital disponível.
"""

import asyncio
from app.tasks.worker import celery_app
from app.core.logger import logger


@celery_app.task(
    name="app.tasks.legal.analyze_edital",
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    time_limit=300,  # 5 min por edital
)
def analyze_edital(self, property_id: str):
    """Analisa juridicamente o edital de um imóvel em background."""
    from app.db.session import SessionLocal
    from app.models.property import Property
    from app.services.analyzers.legal import LegalAnalyzer
    from app.api.routes.legal import _save_legal_result

    db = SessionLocal()
    try:
        import uuid
        prop = db.query(Property).filter(
            Property.id == uuid.UUID(property_id)
        ).first()

        if not prop:
            logger.warning(f"[Task/Legal] Imóvel não encontrado: {property_id}")
            return

        extra = prop.extra_data or {}
        edital_url = extra.get("edital_url") or extra.get("link_edital")

        if not edital_url:
            logger.debug(f"[Task/Legal] Imóvel {property_id[:8]} sem edital, pulando")
            return

        analyzer = LegalAnalyzer()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(analyzer.analyze_from_url(edital_url))
        loop.close()

        if result:
            loop2 = asyncio.new_event_loop()
            asyncio.set_event_loop(loop2)
            loop2.run_until_complete(_save_legal_result(db, property_id, result))
            loop2.close()

            risk = result.get("score_risco", {})
            logger.info(
                f"[Task/Legal] {property_id[:8]} | "
                f"risco={risk.get('nivel')} | "
                f"recomendação={risk.get('recomendacao')}"
            )
            return {
                "property_id": property_id,
                "risk_level": risk.get("nivel"),
                "recommendation": risk.get("recomendacao"),
            }

    except Exception as e:
        logger.error(f"[Task/Legal] Erro: {e}")
        raise self.retry(exc=e)
    finally:
        db.close()


@celery_app.task(name="app.tasks.legal.analyze_all_caixa_editais")
def analyze_all_caixa_editais(limit: int = 50):
    """
    Dispara análise jurídica para todos os imóveis da Caixa
    que têm edital e ainda não foram analisados.
    """
    from sqlalchemy import not_, exists
    from sqlalchemy.orm import Session
    from app.db.session import SessionLocal
    from app.models.property import Property, PropertySource

    db = SessionLocal()
    try:
        # Imóveis da Caixa com edital_url e sem análise jurídica ainda
        props = db.query(Property).filter(
            Property.source == PropertySource.CAIXA,
            Property.extra_data["edital_url"].as_string() != "",
        ).limit(limit).all()

        queued = 0
        for prop in props:
            extra = prop.extra_data or {}
            if extra.get("edital_url") and not extra.get("legal_analysis"):
                analyze_edital.delay(str(prop.id))
                queued += 1

        logger.info(f"[Task/Legal] {queued} análises jurídicas enfileiradas")
        return {"queued": queued}
    finally:
        db.close()
