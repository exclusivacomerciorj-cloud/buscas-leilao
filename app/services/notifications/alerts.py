"""
AlertService — Disparo de alertas para oportunidades encontradas.

Canais suportados:
  - E-mail (SMTP)
  - WhatsApp (Evolution API)

Lógica de disparo:
  - Score >= ALERT_MIN_SCORE
  - Desconto >= ALERT_MIN_DISCOUNT %
  - Imóvel novo (não alertado ainda)
"""

import asyncio
from datetime import datetime
from typing import Optional

import httpx
import aiosmtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from app.core.config import get_settings
from app.core.logger import logger
from app.models.property import Property, PropertyAnalysis, Alert, AlertChannel, AlertStatus

settings = get_settings()


def _format_alert_message(property: Property, analysis: PropertyAnalysis) -> str:
    """Formata a mensagem de alerta."""
    price = property.min_bid or property.asking_price or 0
    discount = analysis.real_discount_pct or 0
    score = analysis.opportunity_score or 0
    roi = analysis.estimated_roi_pct or 0

    auction_label = {
        "segundo_leilao": "2º LEILÃO 🔥",
        "primeiro_leilao": "1º Leilão",
        "venda_direta": "Venda Direta",
        "nao_leilao": "Mercado",
    }.get(property.auction_type.value if property.auction_type else "", "Imóvel")

    msg = f"""🏠 *NOVA OPORTUNIDADE — {auction_label}*

📍 {property.neighborhood or 'RJ'} — {property.city or 'Rio de Janeiro'}
💰 R$ {price:,.0f}
📉 {discount:.1f}% abaixo do mercado
🏆 Score: {score:.0f}/100
📈 ROI estimado: {roi:.1f}%

{'🔓 Desocupado' if (property.occupation_status and property.occupation_status.value == 'desocupado') else '⚠️ Verificar ocupação'}

🔗 {property.source_url or 'Ver detalhes na plataforma'}

_Buscas Leilão — Plataforma de Inteligência Imobiliária_"""
    return msg


async def send_whatsapp_alert(
    phone: str,
    property: Property,
    analysis: PropertyAnalysis,
) -> bool:
    """Envia alerta via Evolution API (WhatsApp)."""
    if not settings.EVOLUTION_API_URL or not settings.EVOLUTION_API_KEY:
        logger.warning("[Alert/WhatsApp] Evolution API não configurada.")
        return False

    message = _format_alert_message(property, analysis)
    url = f"{settings.EVOLUTION_API_URL}/message/sendText/{settings.EVOLUTION_INSTANCE}"

    payload = {
        "number": phone,
        "options": {"delay": 1200, "presence": "composing"},
        "textMessage": {"text": message},
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                url,
                json=payload,
                headers={"apikey": settings.EVOLUTION_API_KEY},
            )
            if response.status_code == 200:
                logger.info(f"[Alert/WhatsApp] Enviado para {phone[:5]}...")
                return True
            else:
                logger.error(f"[Alert/WhatsApp] Erro {response.status_code}: {response.text[:200]}")
                return False
    except Exception as e:
        logger.error(f"[Alert/WhatsApp] Falha: {e}")
        return False


async def send_email_alert(
    email: str,
    property: Property,
    analysis: PropertyAnalysis,
) -> bool:
    """Envia alerta por e-mail via SMTP."""
    if not settings.SMTP_USER or not settings.SMTP_PASSWORD:
        logger.warning("[Alert/Email] SMTP não configurado.")
        return False

    score = int(analysis.opportunity_score or 0)
    price = property.min_bid or property.asking_price or 0
    discount = analysis.real_discount_pct or 0

    subject = f"🏠 Nova oportunidade {score}pts — {property.neighborhood} | {discount:.0f}% desconto"

    body_html = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
      <div style="background: #0a2540; color: white; padding: 24px; border-radius: 8px 8px 0 0;">
        <h1 style="margin: 0; font-size: 22px;">Nova Oportunidade Encontrada</h1>
        <p style="margin: 8px 0 0; opacity: 0.7;">Score: {score}/100 — {discount:.1f}% abaixo do mercado</p>
      </div>
      <div style="background: #f8fafc; padding: 24px; border: 1px solid #e2e8f0; border-radius: 0 0 8px 8px;">
        <h2 style="color: #0a2540;">{property.neighborhood}, {property.city}</h2>
        <p style="font-size: 28px; font-weight: bold; color: #16a34a; margin: 8px 0;">R$ {price:,.0f}</p>
        <table style="width: 100%; border-collapse: collapse; margin-top: 16px;">
          <tr style="background: white;">
            <td style="padding: 8px; border-bottom: 1px solid #e2e8f0;">ROI estimado</td>
            <td style="padding: 8px; border-bottom: 1px solid #e2e8f0; font-weight: bold;">
              {analysis.estimated_roi_pct or 0:.1f}%
            </td>
          </tr>
          <tr style="background: #f8fafc;">
            <td style="padding: 8px; border-bottom: 1px solid #e2e8f0;">Lucro estimado</td>
            <td style="padding: 8px; border-bottom: 1px solid #e2e8f0; font-weight: bold;">
              R$ {analysis.estimated_profit or 0:,.0f}
            </td>
          </tr>
          <tr style="background: white;">
            <td style="padding: 8px;">Área</td>
            <td style="padding: 8px; font-weight: bold;">
              {int(property.total_area) if property.total_area else "—"}m²
            </td>
          </tr>
        </table>
        <div style="margin-top: 24px; text-align: center;">
          <a href="{property.source_url or '#'}" 
             style="background: #0a2540; color: white; padding: 12px 32px; text-decoration: none; border-radius: 6px; font-weight: bold;">
            Ver Detalhes
          </a>
        </div>
      </div>
      <p style="text-align: center; font-size: 11px; color: #94a3b8; margin-top: 16px;">
        Buscas Leilão — Plataforma de Inteligência Imobiliária
      </p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.SMTP_FROM
    msg["To"] = email
    msg.attach(MIMEText(body_html, "html"))

    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            start_tls=True,
        )
        logger.info(f"[Alert/Email] Enviado para {email}")
        return True
    except Exception as e:
        logger.error(f"[Alert/Email] Falha: {e}")
        return False


async def dispatch_alert(
    property: Property,
    analysis: PropertyAnalysis,
    recipient: str,
    channel: AlertChannel,
) -> bool:
    """Despacha um alerta pelo canal especificado."""
    if channel == AlertChannel.WHATSAPP:
        return await send_whatsapp_alert(recipient, property, analysis)
    elif channel == AlertChannel.EMAIL:
        return await send_email_alert(recipient, property, analysis)
    else:
        logger.warning(f"[Alert] Canal não implementado: {channel}")
        return False
