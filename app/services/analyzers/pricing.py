"""
PricingEngine — Motor de precificação e score de oportunidade.

Lógica:
  1. Busca imóveis comparáveis na região (mesmo bairro, tipo, área similar)
  2. Calcula o valor de mercado estimado por m²
  3. Calcula o desconto real do imóvel analisado
  4. Gera score de oportunidade 0-100

Score de oportunidade:
  - Desconto real (40 pts máx): quanto abaixo do mercado está
  - Liquidez da região (25 pts máx): velocidade de venda do bairro
  - Tipo de leilão (20 pts máx): 2º leilão > venda direta > mercado
  - Situação jurídica (15 pts máx): desocupado > indefinido > ocupado
"""

from typing import Optional
from sqlalchemy.orm import Session

from app.core.logger import logger
from app.models.property import Property, PropertyAnalysis, AuctionType, OccupationStatus

# CUB/RJ 2024 — Custo Unitário Básico de construção por padrão (R$/m²)
CUB_RJ = {
    "baixo": 1_850.0,
    "medio": 2_400.0,
    "alto": 3_200.0,
    "padrao": 2_400.0,
}

# Tempo médio de venda por bairro (dias) — base de conhecimento inicial
# Será atualizado com dados reais ao longo do tempo
LIQUIDITY_BY_NEIGHBORHOOD = {
    "barra da tijuca": 90,
    "recreio dos bandeirantes": 100,
    "jacarepaguá": 120,
    "leblon": 60,
    "ipanema": 65,
    "copacabana": 75,
    "botafogo": 80,
    "flamengo": 85,
    "tijuca": 110,
    "campo grande": 150,
    "santa cruz": 180,
    # default para bairros desconhecidos
    "_default": 120,
}

# Custos cartoriais RJ (aproximados, 2024)
ITBI_RATE = 0.03         # 3% do valor venal
REGISTRY_BASE = 3_500.0  # Escritura + registro médio
LAWYER_COST = 2_500.0    # Honorários advocatícios básicos


class PricingEngine:
    """
    Motor de precificação e análise de oportunidade.
    Usa o banco de dados para buscar comparáveis reais.
    """

    def __init__(self, db: Session):
        self.db = db

    def analyze(self, property: Property) -> PropertyAnalysis:
        """Executa análise completa de um imóvel e retorna um PropertyAnalysis."""

        logger.info(f"Analisando: {property.id} | {property.neighborhood}")

        # 1. Valor de mercado estimado
        market_value, market_sqm, comparable_count = self._estimate_market_value(property)

        # 2. Desconto real
        acquisition_price = property.min_bid or property.asking_price or 0
        real_discount_pct = None
        if market_value and acquisition_price and acquisition_price > 0:
            real_discount_pct = round(
                (1 - acquisition_price / market_value) * 100, 2
            )

        # 3. Simulação financeira
        financial = self._simulate_financial(property, acquisition_price, market_value)

        # 4. Liquidez
        liquidity_score, avg_days = self._calculate_liquidity(property.neighborhood)

        # 5. Score de oportunidade
        score, breakdown = self._calculate_score(
            real_discount_pct=real_discount_pct,
            liquidity_score=liquidity_score,
            auction_type=property.auction_type,
            occupation_status=property.occupation_status,
            comparable_count=comparable_count,
        )

        # 6. Resumo IA (gerado sob demanda, não no pipeline síncrono)
        analysis = PropertyAnalysis(
            property_id=property.id,
            opportunity_score=score,
            score_breakdown=breakdown,
            market_value_estimated=market_value,
            market_price_per_sqm=market_sqm,
            comparable_count=comparable_count,
            real_discount_pct=real_discount_pct,
            renovation_cost=financial["renovation_cost"],
            itbi_cost=financial["itbi_cost"],
            registry_cost=financial["registry_cost"],
            legal_cost=financial["legal_cost"],
            total_acquisition_cost=financial["total_acquisition_cost"],
            estimated_sale_price=financial["estimated_sale_price"],
            estimated_profit=financial["estimated_profit"],
            estimated_roi_pct=financial["estimated_roi_pct"],
            payback_months=financial["payback_months"],
            legal_risk_score=self._calculate_legal_risk(property),
            legal_issues=self._identify_legal_issues(property),
            avg_days_on_market=avg_days,
            liquidity_score=liquidity_score,
            analyzed_by="engine_v1",
        )

        return analysis

    # ─── Estimativa de valor de mercado ───────────────────────────────────────

    def _estimate_market_value(
        self, prop: Property
    ) -> tuple[Optional[float], Optional[float], int]:
        """
        Busca imóveis comparáveis no banco e calcula o valor de mercado.
        Retorna: (valor_total_estimado, preco_m2_mercado, quantidade_comparaveis)
        """
        from app.models.property import PropertyStatus

        if not prop.neighborhood:
            return None, None, 0

        # Busca comparáveis: mesmo bairro, mesmo tipo, área similar (±40%)
        query = self.db.query(Property).filter(
            Property.neighborhood.ilike(f"%{prop.neighborhood}%"),
            Property.status == PropertyStatus.ATIVO,
            Property.id != prop.id,
            Property.asking_price.isnot(None),
            Property.asking_price > 0,
        )

        if prop.property_type:
            query = query.filter(Property.property_type == prop.property_type)

        if prop.total_area and prop.total_area > 0:
            area_min = prop.total_area * 0.6
            area_max = prop.total_area * 1.4
            query = query.filter(
                Property.total_area >= area_min,
                Property.total_area <= area_max,
            )

        comparables = query.limit(50).all()

        if not comparables:
            # Fallback: busca por cidade se não encontrar no bairro
            comparables = self.db.query(Property).filter(
                Property.city.ilike(f"%{prop.city or 'Rio de Janeiro'}%"),
                Property.property_type == prop.property_type,
                Property.asking_price.isnot(None),
                Property.id != prop.id,
            ).limit(20).all()

        if not comparables:
            return None, None, 0

        # Calcula preço médio por m²
        sqm_prices = []
        for c in comparables:
            area = c.usable_area or c.total_area
            if area and area > 0 and c.asking_price:
                sqm_prices.append(c.asking_price / area)

        if not sqm_prices:
            # Fallback: usa preço médio absoluto
            prices = [c.asking_price for c in comparables if c.asking_price]
            if prices:
                avg_price = sum(prices) / len(prices)
                return avg_price, None, len(comparables)
            return None, None, 0

        # Remove outliers (percentis 10-90)
        sqm_prices.sort()
        p10 = int(len(sqm_prices) * 0.1)
        p90 = int(len(sqm_prices) * 0.9)
        filtered = sqm_prices[p10:p90] if p90 > p10 else sqm_prices

        market_sqm = sum(filtered) / len(filtered)
        area = prop.usable_area or prop.total_area or 0
        market_value = market_sqm * area if area > 0 else None

        return (
            round(market_value, 2) if market_value else None,
            round(market_sqm, 2),
            len(comparables),
        )

    # ─── Simulação financeira ─────────────────────────────────────────────────

    def _simulate_financial(
        self,
        prop: Property,
        acquisition_price: float,
        market_value: Optional[float],
    ) -> dict:
        if not acquisition_price:
            return self._empty_financial()

        area = prop.usable_area or prop.total_area or 0

        # Reforma estimada (CUB/RJ)
        # Para leilão/retomado assumimos reforma média; mercado tradicional = leve
        cub_standard = CUB_RJ["medio"]
        if prop.auction_type in (AuctionType.PRIMEIRO_LEILAO, AuctionType.SEGUNDO_LEILAO):
            renovation_pct = 0.25   # 25% do custo de construção
        elif prop.auction_type == AuctionType.VENDA_DIRETA:
            renovation_pct = 0.15
        else:
            renovation_pct = 0.08   # Mercado tradicional: reforma leve

        renovation_cost = round(area * cub_standard * renovation_pct, 2) if area > 0 else 0

        # Custos de aquisição
        itbi_cost = round(acquisition_price * ITBI_RATE, 2)
        registry_cost = REGISTRY_BASE
        legal_cost = LAWYER_COST if prop.auction_type != AuctionType.NAO_LEILAO else 0

        total_acquisition_cost = round(
            acquisition_price + renovation_cost + itbi_cost +
            registry_cost + legal_cost, 2
        )

        # Preço de saída estimado (valor de mercado ou +10% da aquisição)
        estimated_sale_price = market_value or round(acquisition_price * 1.35, 2)

        # Lucro e ROI
        estimated_profit = round(estimated_sale_price - total_acquisition_cost, 2)
        estimated_roi_pct = round(
            (estimated_profit / total_acquisition_cost) * 100, 2
        ) if total_acquisition_cost > 0 else 0

        # Tempo estimado de retorno (meses)
        # Assume 6-12 meses de reforma + venda
        payback_months = 8 if prop.auction_type != AuctionType.NAO_LEILAO else 4

        return {
            "renovation_cost": renovation_cost,
            "itbi_cost": itbi_cost,
            "registry_cost": registry_cost,
            "legal_cost": legal_cost,
            "total_acquisition_cost": total_acquisition_cost,
            "estimated_sale_price": estimated_sale_price,
            "estimated_profit": estimated_profit,
            "estimated_roi_pct": estimated_roi_pct,
            "payback_months": payback_months,
        }

    def _empty_financial(self) -> dict:
        return {
            "renovation_cost": None, "itbi_cost": None,
            "registry_cost": None, "legal_cost": None,
            "total_acquisition_cost": None, "estimated_sale_price": None,
            "estimated_profit": None, "estimated_roi_pct": None,
            "payback_months": None,
        }

    # ─── Liquidez ─────────────────────────────────────────────────────────────

    def _calculate_liquidity(self, neighborhood: Optional[str]) -> tuple[int, int]:
        if not neighborhood:
            avg_days = LIQUIDITY_BY_NEIGHBORHOOD["_default"]
        else:
            n = neighborhood.lower()
            avg_days = next(
                (v for k, v in LIQUIDITY_BY_NEIGHBORHOOD.items() if k in n),
                LIQUIDITY_BY_NEIGHBORHOOD["_default"],
            )

        # Converte dias → score (60 dias=100, 180 dias=0)
        score = max(0, min(100, int(100 - (avg_days - 60) / 1.2)))
        return score, avg_days

    # ─── Score de oportunidade ────────────────────────────────────────────────

    def _calculate_score(
        self,
        real_discount_pct: Optional[float],
        liquidity_score: int,
        auction_type: AuctionType,
        occupation_status: OccupationStatus,
        comparable_count: int,
    ) -> tuple[float, dict]:
        breakdown = {}

        # 1. Desconto real (40 pts)
        discount_score = 0
        if real_discount_pct is not None:
            if real_discount_pct >= 50:
                discount_score = 40
            elif real_discount_pct >= 40:
                discount_score = 35
            elif real_discount_pct >= 30:
                discount_score = 28
            elif real_discount_pct >= 20:
                discount_score = 18
            elif real_discount_pct >= 10:
                discount_score = 8
            # Desconto negativo (caro demais) = 0
        breakdown["discount"] = discount_score

        # 2. Liquidez (25 pts)
        liq_points = round(liquidity_score * 0.25, 1)
        breakdown["liquidity"] = liq_points

        # 3. Tipo de leilão / modalidade (20 pts)
        auction_map = {
            AuctionType.SEGUNDO_LEILAO: 20,  # Melhor: segundo leilão tem lance mínimo menor
            AuctionType.PRIMEIRO_LEILAO: 14,
            AuctionType.VENDA_DIRETA: 10,
            AuctionType.NAO_LEILAO: 0,
        }
        auction_score = auction_map.get(auction_type, 0)
        breakdown["auction_type"] = auction_score

        # 4. Situação jurídica / ocupação (15 pts)
        occupation_map = {
            OccupationStatus.DESOCUPADO: 15,
            OccupationStatus.INDEFINIDO: 5,
            OccupationStatus.OCUPADO: -10,  # Penalidade: imóvel ocupado é problema
        }
        occupation_score = occupation_map.get(occupation_status, 0)
        breakdown["occupation"] = occupation_score

        # Bônus: muitos comparáveis = análise mais confiável (+5 pts)
        confidence_bonus = 5 if comparable_count >= 10 else (2 if comparable_count >= 5 else 0)
        breakdown["confidence_bonus"] = confidence_bonus

        total = max(0, min(100, round(
            discount_score + liq_points + auction_score + occupation_score + confidence_bonus,
            1
        )))

        return total, breakdown

    # ─── Risco jurídico ───────────────────────────────────────────────────────

    def _calculate_legal_risk(self, prop: Property) -> int:
        """Score de risco jurídico: 0=baixo risco, 100=alto risco."""
        risk = 0
        if prop.occupation_status == OccupationStatus.OCUPADO:
            risk += 50
        elif prop.occupation_status == OccupationStatus.INDEFINIDO:
            risk += 20
        if prop.auction_type == AuctionType.PRIMEIRO_LEILAO:
            risk += 15  # Primeiro leilão tem mais incerteza
        extra = prop.extra_data or {}
        if extra.get("iptu_debito"):
            risk += 20
        if extra.get("acao_judicial"):
            risk += 30
        return min(100, risk)

    def _identify_legal_issues(self, prop: Property) -> list:
        issues = []
        if prop.occupation_status == OccupationStatus.OCUPADO:
            issues.append("imovel_ocupado")
        elif prop.occupation_status == OccupationStatus.INDEFINIDO:
            issues.append("ocupacao_indefinida")
        extra = prop.extra_data or {}
        if extra.get("iptu_debito"):
            issues.append("debito_iptu")
        if extra.get("acao_judicial"):
            issues.append("acao_judicial")
        return issues
