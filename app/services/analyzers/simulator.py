"""
FinancialSimulator — Gera relatório de viabilidade em PDF.

Usa WeasyPrint + Jinja2 para produzir um PDF profissional com:
  - Dados do imóvel
  - Simulação financeira detalhada
  - Score de oportunidade
  - Resumo de riscos
"""

import io
from datetime import datetime
from typing import Optional

from jinja2 import Environment, BaseLoader

from app.core.logger import logger
from app.models.property import Property, PropertyAnalysis

# Template HTML do relatório (inline para simplicidade)
REPORT_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<style>
  @page { size: A4; margin: 2cm; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'Helvetica Neue', Arial, sans-serif; font-size: 11pt; color: #1a1a1a; line-height: 1.5; }
  .header { background: #0a2540; color: white; padding: 24px 28px; border-radius: 6px; margin-bottom: 24px; }
  .header h1 { font-size: 20pt; font-weight: 700; margin-bottom: 4px; }
  .header .subtitle { font-size: 10pt; opacity: 0.7; }
  .header .date { font-size: 9pt; opacity: 0.5; margin-top: 8px; }
  .score-badge { display: inline-block; background: {{ score_color }}; color: white; font-size: 24pt; font-weight: 700; padding: 12px 24px; border-radius: 8px; }
  .score-label { font-size: 9pt; color: #666; margin-top: 4px; }
  .section { margin-bottom: 20px; }
  .section-title { font-size: 12pt; font-weight: 700; color: #0a2540; border-bottom: 2px solid #e8ecf0; padding-bottom: 6px; margin-bottom: 12px; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }
  .card { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 14px; }
  .card-label { font-size: 8pt; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 4px; }
  .card-value { font-size: 14pt; font-weight: 700; color: #0a2540; }
  .card-value.highlight { color: #16a34a; }
  .card-value.warning { color: #dc2626; }
  table { width: 100%; border-collapse: collapse; font-size: 10pt; }
  tr:nth-child(even) { background: #f8fafc; }
  td, th { padding: 8px 12px; text-align: left; border-bottom: 1px solid #e2e8f0; }
  th { background: #0a2540; color: white; font-weight: 600; font-size: 9pt; }
  .value-right { text-align: right; font-weight: 600; }
  .total-row { background: #0a2540 !important; color: white; font-weight: 700; }
  .total-row td { color: white; }
  .profit-row { background: #dcfce7 !important; }
  .profit-row td { color: #16a34a; font-weight: 700; }
  .risk-low { color: #16a34a; font-weight: 600; }
  .risk-med { color: #f59e0b; font-weight: 600; }
  .risk-high { color: #dc2626; font-weight: 600; }
  .footer { margin-top: 32px; padding-top: 12px; border-top: 1px solid #e2e8f0; font-size: 8pt; color: #94a3b8; text-align: center; }
  .tag { display: inline-block; padding: 2px 10px; border-radius: 99px; font-size: 8pt; font-weight: 600; }
  .tag-green { background: #dcfce7; color: #16a34a; }
  .tag-yellow { background: #fef3c7; color: #d97706; }
  .tag-red { background: #fee2e2; color: #dc2626; }
</style>
</head>
<body>

<div class="header">
  <div style="display: flex; justify-content: space-between; align-items: flex-start;">
    <div>
      <h1>Relatório de Viabilidade</h1>
      <div class="subtitle">Plataforma Buscas Leilão — Análise de Oportunidade Imobiliária</div>
      <div class="date">Gerado em {{ generated_at }}</div>
    </div>
    <div style="text-align: right;">
      <div class="score-badge">{{ score }}pts</div>
      <div class="score-label" style="color: #94a3b8;">Score de oportunidade</div>
    </div>
  </div>
</div>

<!-- Dados do Imóvel -->
<div class="section">
  <div class="section-title">Dados do Imóvel</div>
  <div class="grid-3">
    <div class="card">
      <div class="card-label">Endereço</div>
      <div style="font-size: 10pt; font-weight: 600;">{{ address }}</div>
    </div>
    <div class="card">
      <div class="card-label">Bairro / Cidade</div>
      <div style="font-size: 11pt; font-weight: 700;">{{ neighborhood }}</div>
    </div>
    <div class="card">
      <div class="card-label">Modalidade</div>
      <div style="font-size: 11pt; font-weight: 700;">{{ auction_type }}</div>
    </div>
    <div class="card">
      <div class="card-label">Área total</div>
      <div class="card-value">{{ area }}m²</div>
    </div>
    <div class="card">
      <div class="card-label">Quartos</div>
      <div class="card-value">{{ bedrooms or "—" }}</div>
    </div>
    <div class="card">
      <div class="card-label">Situação</div>
      <div class="card-value {% if occupation == 'Desocupado' %}highlight{% elif occupation == 'Ocupado' %}warning{% endif %}">
        {{ occupation }}
      </div>
    </div>
  </div>
</div>

<!-- Análise de Valor -->
<div class="section">
  <div class="section-title">Análise de Valor de Mercado</div>
  <div class="grid-2">
    <div>
      <table>
        <tr><th colspan="2">Comparativo de Preços</th></tr>
        <tr><td>Valor pedido / Lance mínimo</td><td class="value-right">R$ {{ asking_price }}</td></tr>
        <tr><td>Valor estimado de mercado</td><td class="value-right">R$ {{ market_value }}</td></tr>
        <tr><td>Preço por m² do imóvel</td><td class="value-right">R$ {{ price_sqm }}/m²</td></tr>
        <tr><td>Preço médio da região (m²)</td><td class="value-right">R$ {{ market_sqm }}/m²</td></tr>
        <tr class="profit-row"><td><b>Desconto real</b></td><td class="value-right"><b>{{ discount }}%</b></td></tr>
      </table>
      <div style="margin-top: 8px; font-size: 8pt; color: #64748b;">
        Baseado em {{ comparable_count }} imóveis comparáveis na região.
      </div>
    </div>
    <div>
      <table>
        <tr><th colspan="2">Score — Detalhamento</th></tr>
        {% for item in score_breakdown %}
        <tr><td>{{ item.label }}</td><td class="value-right">+{{ item.value }}pts</td></tr>
        {% endfor %}
        <tr class="total-row"><td>Score Total</td><td class="value-right">{{ score }}pts</td></tr>
      </table>
    </div>
  </div>
</div>

<!-- Simulação Financeira -->
<div class="section">
  <div class="section-title">Simulação Financeira Completa</div>
  <table>
    <tr><th>Item</th><th style="text-align:right">Valor Estimado</th><th style="text-align:right">%</th></tr>
    <tr><td>Valor de aquisição (lance/compra)</td><td class="value-right">R$ {{ asking_price }}</td><td class="value-right">—</td></tr>
    <tr><td>Reforma estimada (CUB/RJ)</td><td class="value-right">R$ {{ renovation_cost }}</td><td class="value-right">{{ renovation_pct }}%</td></tr>
    <tr><td>ITBI ({{ itbi_rate }}% do valor venal)</td><td class="value-right">R$ {{ itbi_cost }}</td><td class="value-right">—</td></tr>
    <tr><td>Escritura + Registro</td><td class="value-right">R$ {{ registry_cost }}</td><td class="value-right">—</td></tr>
    <tr><td>Honorários jurídicos</td><td class="value-right">R$ {{ legal_cost }}</td><td class="value-right">—</td></tr>
    <tr class="total-row"><td><b>Custo total da operação</b></td><td class="value-right"><b>R$ {{ total_cost }}</b></td><td class="value-right">—</td></tr>
    <tr><td>Preço estimado de venda</td><td class="value-right">R$ {{ sale_price }}</td><td class="value-right">—</td></tr>
    <tr class="profit-row"><td><b>Lucro estimado</b></td><td class="value-right"><b>R$ {{ profit }}</b></td><td class="value-right"><b>{{ roi }}% ROI</b></td></tr>
  </table>
  <div style="margin-top: 8px; font-size: 8pt; color: #64748b;">
    Reforma calculada com base no CUB/RJ 2024 (R$ 2.400/m²). 
    Prazo estimado da operação: {{ payback_months }} meses.
  </div>
</div>

<!-- Análise de Risco -->
<div class="section">
  <div class="section-title">Análise de Risco</div>
  <div class="grid-3">
    <div class="card">
      <div class="card-label">Risco jurídico</div>
      <div class="card-value {% if legal_risk < 30 %}highlight{% elif legal_risk < 60 %}warning{% else %}warning{% endif %}">
        {% if legal_risk < 30 %}Baixo{% elif legal_risk < 60 %}Médio{% else %}Alto{% endif %}
      </div>
    </div>
    <div class="card">
      <div class="card-label">Liquidez do bairro</div>
      <div class="card-value">{{ avg_days }} dias médios</div>
    </div>
    <div class="card">
      <div class="card-label">Pendências identificadas</div>
      <div style="font-size: 9pt; margin-top: 4px;">
        {% if legal_issues %}
          {% for issue in legal_issues %}
            <span class="tag tag-red">{{ issue }}</span>
          {% endfor %}
        {% else %}
          <span class="tag tag-green">Nenhuma identificada</span>
        {% endif %}
      </div>
    </div>
  </div>
</div>

<div class="footer">
  Buscas Leilão — Plataforma de Inteligência Imobiliária<br>
  Este relatório é gerado automaticamente com base em dados públicos e estimativas. 
  Não substitui due diligence jurídica e avaliação presencial. {{ generated_at }}
</div>

</body>
</html>
"""

SCORE_COLORS = {
    "high": "#16a34a",    # Verde: 70+
    "medium": "#f59e0b",  # Amarelo: 40-69
    "low": "#dc2626",     # Vermelho: <40
}

AUCTION_TYPE_LABELS = {
    "segundo_leilao": "2º Leilão",
    "primeiro_leilao": "1º Leilão",
    "venda_direta": "Venda Direta / Licitação",
    "nao_leilao": "Mercado Tradicional",
}

OCCUPATION_LABELS = {
    "desocupado": "Desocupado",
    "ocupado": "Ocupado",
    "indefinido": "Indefinido",
}

SCORE_BREAKDOWN_LABELS = {
    "discount": "Desconto real de mercado",
    "liquidity": "Liquidez da região",
    "auction_type": "Tipo de modalidade",
    "occupation": "Situação do imóvel",
    "confidence_bonus": "Confiança dos dados",
}


def _fmt_money(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}".replace(",", ".")


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:.1f}"


def generate_pdf_report(
    property: Property,
    analysis: PropertyAnalysis,
) -> bytes:
    """
    Gera o PDF de viabilidade financeira.
    Retorna os bytes do PDF.
    """
    try:
        from weasyprint import HTML as WeasyHTML
    except ImportError:
        raise RuntimeError(
            "WeasyPrint não instalado. Execute: pip install weasyprint"
        )

    score = int(analysis.opportunity_score or 0)
    if score >= 70:
        score_color = SCORE_COLORS["high"]
    elif score >= 40:
        score_color = SCORE_COLORS["medium"]
    else:
        score_color = SCORE_COLORS["low"]

    asking_price = property.min_bid or property.asking_price or 0
    renovation_cost = analysis.renovation_cost or 0

    score_breakdown_items = []
    if analysis.score_breakdown:
        for key, value in analysis.score_breakdown.items():
            score_breakdown_items.append({
                "label": SCORE_BREAKDOWN_LABELS.get(key, key),
                "value": value,
            })

    context = {
        "generated_at": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "score": score,
        "score_color": score_color,
        "address": property.address or "Não informado",
        "neighborhood": f"{property.neighborhood or '—'} / {property.city or 'RJ'}",
        "auction_type": AUCTION_TYPE_LABELS.get(
            (property.auction_type.value if property.auction_type else ""), "—"
        ),
        "area": int(property.total_area) if property.total_area else "—",
        "bedrooms": property.bedrooms,
        "occupation": OCCUPATION_LABELS.get(
            (property.occupation_status.value if property.occupation_status else ""), "—"
        ),
        "asking_price": _fmt_money(asking_price),
        "market_value": _fmt_money(analysis.market_value_estimated),
        "price_sqm": _fmt_money(property.price_per_sqm),
        "market_sqm": _fmt_money(analysis.market_price_per_sqm),
        "discount": _fmt_pct(analysis.real_discount_pct),
        "comparable_count": analysis.comparable_count or 0,
        "score_breakdown": score_breakdown_items,
        "renovation_cost": _fmt_money(renovation_cost),
        "renovation_pct": _fmt_pct(
            (renovation_cost / asking_price * 100) if asking_price > 0 else None
        ),
        "itbi_rate": "3",
        "itbi_cost": _fmt_money(analysis.itbi_cost),
        "registry_cost": _fmt_money(analysis.registry_cost),
        "legal_cost": _fmt_money(analysis.legal_cost),
        "total_cost": _fmt_money(analysis.total_acquisition_cost),
        "sale_price": _fmt_money(analysis.estimated_sale_price),
        "profit": _fmt_money(analysis.estimated_profit),
        "roi": _fmt_pct(analysis.estimated_roi_pct),
        "payback_months": analysis.payback_months or "—",
        "legal_risk": analysis.legal_risk_score or 0,
        "avg_days": analysis.avg_days_on_market or "—",
        "legal_issues": analysis.legal_issues or [],
    }

    env = Environment(loader=BaseLoader())
    template = env.from_string(REPORT_TEMPLATE)
    html_content = template.render(**context)

    pdf_bytes = WeasyHTML(string=html_content).write_pdf()
    logger.info(f"PDF gerado: {len(pdf_bytes)} bytes | imóvel {property.id}")
    return pdf_bytes
