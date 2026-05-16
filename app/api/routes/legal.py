"""
Rotas de IA Jurídica — /api/v1/legal

Endpoints:
  POST /api/v1/legal/analyze/url      → analisa edital por URL do PDF
  POST /api/v1/legal/analyze/upload   → analisa edital por upload de PDF
  POST /api/v1/legal/analyze/{id}     → analisa edital do imóvel cadastrado
  GET  /api/v1/legal/analysis/{id}    → busca análise jurídica salva
"""

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.logger import logger
from app.db.session import get_db
from app.models.property import Property, PropertyAnalysis
from app.services.analyzers.legal import LegalAnalyzer

router = APIRouter(prefix="/api/v1/legal", tags=["legal-ai"])


class AnalyzeByURLRequest(BaseModel):
    pdf_url: str
    property_id: Optional[str] = None


class AnalyzeByTextRequest(BaseModel):
    text: str
    property_id: Optional[str] = None


async def _save_legal_result(
    db: Session,
    property_id: Optional[str],
    result: dict,
):
    """Salva o resultado jurídico no PropertyAnalysis existente ou cria um novo."""
    if not property_id:
        return

    try:
        pid = uuid.UUID(property_id)
    except ValueError:
        return

    analysis = db.query(PropertyAnalysis).filter(
        PropertyAnalysis.property_id == pid
    ).order_by(PropertyAnalysis.created_at.desc()).first()

    if not analysis:
        analysis = PropertyAnalysis(property_id=pid)
        db.add(analysis)

    # Atualiza campos jurídicos
    score_risco = result.get("score_risco", {})
    ocupacao = result.get("ocupacao", {})
    dividas = result.get("dividas", {})
    acoes = result.get("acoes_judiciais", {})

    analysis.legal_risk_score = score_risco.get("total")

    issues = []
    if ocupacao.get("status") == "ocupado":
        issues.append("imovel_ocupado")
    elif ocupacao.get("status") == "indefinido":
        issues.append("ocupacao_indefinida")
    if dividas.get("iptu", {}).get("tem_debito"):
        issues.append("debito_iptu")
    if dividas.get("condominio", {}).get("tem_debito"):
        issues.append("debito_condominio")
    if acoes.get("tem_acao"):
        issues.append("acao_judicial")

    analysis.legal_issues = issues
    analysis.ai_summary = result.get("resumo_executivo")

    # Salva resultado completo no extra_data do imóvel
    prop = db.query(Property).filter(Property.id == pid).first()
    if prop:
        extra = prop.extra_data or {}
        extra["legal_analysis"] = result
        prop.extra_data = extra

        # Atualiza status de ocupação baseado na IA
        occ_status = ocupacao.get("status")
        if occ_status == "desocupado":
            from app.models.property import OccupationStatus
            prop.occupation_status = OccupationStatus.DESOCUPADO
        elif occ_status == "ocupado":
            from app.models.property import OccupationStatus
            prop.occupation_status = OccupationStatus.OCUPADO

    db.commit()
    logger.info(f"[Legal] Resultado salvo para imóvel {property_id[:8]}...")


@router.post("/analyze/url")
async def analyze_by_url(
    request: AnalyzeByURLRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Analisa um edital a partir da URL do PDF."""
    analyzer = LegalAnalyzer()
    result = await analyzer.analyze_from_url(request.pdf_url)

    if not result:
        raise HTTPException(status_code=500, detail="Falha na análise do edital")

    if request.property_id:
        background_tasks.add_task(
            _save_legal_result, db, request.property_id, result
        )

    return result


@router.post("/analyze/upload")
async def analyze_by_upload(
    file: UploadFile = File(...),
    property_id: Optional[str] = None,
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
):
    """Analisa um edital a partir do upload do PDF."""
    if not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Apenas arquivos PDF são aceitos")

    if file.size and file.size > 50 * 1024 * 1024:  # 50MB
        raise HTTPException(status_code=400, detail="PDF muito grande (máx 50MB)")

    pdf_bytes = await file.read()
    analyzer = LegalAnalyzer()
    result = await analyzer.analyze_from_bytes(pdf_bytes)

    if not result:
        raise HTTPException(status_code=500, detail="Falha na análise do edital")

    if property_id and background_tasks:
        background_tasks.add_task(_save_legal_result, db, property_id, result)

    return result


@router.post("/analyze/text")
async def analyze_by_text(
    request: AnalyzeByTextRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Analisa um edital a partir do texto já extraído (útil para testes)."""
    if len(request.text) < 100:
        raise HTTPException(status_code=400, detail="Texto muito curto para análise")

    analyzer = LegalAnalyzer()
    result = await analyzer.analyze_from_text(request.text)

    if not result:
        raise HTTPException(status_code=500, detail="Falha na análise")

    if request.property_id:
        background_tasks.add_task(
            _save_legal_result, db, request.property_id, result
        )

    return result


@router.post("/analyze/{property_id}")
async def analyze_property_edital(
    property_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Analisa o edital de um imóvel já cadastrado no banco.
    Busca a URL do edital no extra_data do imóvel.
    """
    try:
        pid = uuid.UUID(property_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="ID inválido")

    prop = db.query(Property).filter(Property.id == pid).first()
    if not prop:
        raise HTTPException(status_code=404, detail="Imóvel não encontrado")

    # Busca URL do edital
    extra = prop.extra_data or {}
    edital_url = extra.get("edital_url") or extra.get("link_edital")

    if not edital_url:
        raise HTTPException(
            status_code=422,
            detail="Imóvel não possui URL de edital. Use /analyze/url ou /analyze/upload"
        )

    analyzer = LegalAnalyzer()
    result = await analyzer.analyze_from_url(edital_url)

    if not result:
        raise HTTPException(status_code=500, detail="Falha na análise do edital")

    background_tasks.add_task(_save_legal_result, db, property_id, result)

    return {
        "property_id": property_id,
        "edital_url": edital_url,
        "analysis": result,
    }


@router.get("/analysis/{property_id}")
def get_legal_analysis(property_id: str, db: Session = Depends(get_db)):
    """Retorna a análise jurídica salva de um imóvel."""
    try:
        pid = uuid.UUID(property_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="ID inválido")

    prop = db.query(Property).filter(Property.id == pid).first()
    if not prop:
        raise HTTPException(status_code=404, detail="Imóvel não encontrado")

    extra = prop.extra_data or {}
    legal = extra.get("legal_analysis")

    if not legal:
        raise HTTPException(
            status_code=404,
            detail="Análise jurídica não encontrada. Execute POST /analyze/{id} primeiro."
        )

    analysis = db.query(PropertyAnalysis).filter(
        PropertyAnalysis.property_id == pid
    ).order_by(PropertyAnalysis.created_at.desc()).first()

    return {
        "property_id": property_id,
        "neighborhood": prop.neighborhood,
        "source": prop.source.value,
        "legal_risk_score": analysis.legal_risk_score if analysis else None,
        "legal_issues": analysis.legal_issues if analysis else [],
        "ai_summary": analysis.ai_summary if analysis else None,
        "full_analysis": legal,
    }
