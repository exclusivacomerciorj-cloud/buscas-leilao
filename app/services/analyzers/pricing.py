"""
PricingEngine — Motor de precificacao e score de oportunidade.

Score 0-100:
  - Desconto real (40 pts): quanto abaixo do mercado
  - Liquidez da regiao (25 pts): velocidade de venda do bairro
  - Tipo de modalidade (20 pts): 2 leilao > 1 leilao > venda direta > mercado
  - Situacao juridica (15 pts): desocupado > indefinido > ocupado
"""

from typing import Optional
from sqlalchemy.orm import Session

from app.core.logger import logger
from app.models.property import Property, PropertyAnalysis, AuctionType, OccupationStatus

# CUB/RJ 2024
CUB_RJ = {
    "baixo": 1_850.0,
    "medio": 2_400.0,
    "alto": 3_200.0,
    "padrao": 2_400.0,
}

# Tempo medio de venda por bairro (dias) — dados mercado RJ
LIQUIDITY_BY_NEIGHBORHOOD = {
    # Zona Oeste / Barra
    "barra da tijuca": 75,
    "recreio dos bandeirantes": 85,
    "jacarepagua": 90,
    "freguesia": 95,
    "pechincha": 100,
    "taquara": 100,
    "curicica": 105,
    "gardenia azul": 105,
    "itanhanga": 80,
    "anil": 100,
    "sao conrado": 70,
    # Zona Sul
    "leblon": 45,
    "ipanema": 50,
    "copacabana": 60,
    "botafogo": 65,
    "flamengo": 70,
    "catete": 75,
    "laranjeiras": 70,
    "gloria": 80,
    "cosme velho": 75,
    # Tijuca / Norte
    "tijuca": 80,
    "andarai": 85,
    "grajau": 90,
    "maracana": 85,
    "vila isabel": 85,
    # Default
    "_default": 110,
}

# Normalizacao de nomes de bairros (CSV da Caixa tem variantes)
NEIGHBORHOOD_NORMALIZE = {
    "freg de jacarepagua": "jacarepagua",
    "freg jacarepagua": "jacarepagua",
    "freguesia jacarepagu": "jacarepagua",
    "freg. jacarepagua": "jacarepagua",
    "freguesia (jacarepagua)": "jacarepagua",
    "freg. de jacarepagua": "jacarepagua",
}

# Custos cartoriais RJ
ITBI_RATE = 0.03
REGISTRY_BASE = 3_500.0
LAWYER_COST = 2_500.0


def normalize_neighborhood(name: Optional[str]) -> str:
    if not name:
        return ""
    n = name.lower().strip()
    return NEIGHBORHOOD_NORMALIZE.get(n, n)


class PricingEngine:

    def __init__(self, db: Session):
        self.db = db

    def analyze(self, property: Property) -> PropertyAnalysis:
        logger.info(f"Analisando: {property.id} | {property.neighborhood}")

        neighborhood_norm = normalize_neighborhood(property.neighborhood)

        market_value, market_sqm, comparable_count = self._estimate_market_value(
            property, neighborhood_norm
        )

        acquisition_price = property.min_bid or property.asking_price or 0
        real_discount_pct = None
        if market_value and acquisition_price and acquisition_price > 0:
            real_discount_pct = round(
                (1 - acquisition_price / market_value) * 100, 2
            )

        financial = self._simulate_financial(property, acquisition_price, market_value)
        liquidity_score, avg_days = self._calculate_liquidity(neighborhood_norm)

        score, breakdown = self._calculate_score(
            real_discount_pct=real_discount_pct,
            liquidity_score=liquidity_score,
            auction_type=property.auction_type,
            occupation_status=property.occupation_status,
            comparable_count=comparable_count,
            extra_data=property.extra_data or {},
        )

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
            analyzed_by="engine_v2",
        )

        return analysis

    def _estimate_market_value(
        self, prop: Property, neighborhood_norm: str
    ) -> tuple:
        from app.models.property import PropertyStatus

        if not prop.neighborhood:
            return None, None, 0

        # Busca comparaveis no mesmo bairro (normalizado)
        query = self.db.query(Property).filter(
            Property.status == PropertyStatus.ATIVO,
            Property.id != prop.id,
            Property.asking_price.isnot(None),
            Property.asking_price > 0,
        )

        # Filtra por bairro — tenta nome normalizado e variantes
        bairros_busca = [prop.neighborhood]
        for k, v in NEIGHBORHOOD_NORMALIZE.items():
            if v == neighborhood_norm:
                bairros_busca.append(k.title())

        from sqlalchemy import or_
        query = query.filter(
            or_(*[Property.neighborhood.ilike(f"%{b}%") for b in bairros_busca])
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
            return None, None, 0

        sqm_prices = []
        for c in comparables:
            area = c.usable_area or c.total_area
            if area and area > 0 and c.asking_price:
                sqm_prices.append(c.asking_price / area)

        if not sqm_prices:
            prices = [c.asking_price for c in comparables if c.asking_price]
            if prices:
                avg_price = sum(prices) / len(prices)
                return avg_price, None, len(comparables)
            return None, None, 0

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

    def _simulate_financial(self, prop, acquisition_price, market_value):
        if not acquisition_price:
            return self._empty_financial()

        area = prop.usable_area or prop.total_area or 0
        cub_standard = CUB_RJ["medio"]

        if prop.auction_type in (AuctionType.PRIMEIRO_LEILAO, AuctionType.SEGUNDO_LEILAO):
            renovation_pct = 0.25
        elif prop.auction_type == AuctionType.VENDA_DIRETA:
            renovation_pct = 0.15
        else:
            renovation_pct = 0.08

        renovation_cost = round(area * cub_standard * renovation_pct, 2) if area > 0 else 0
        itbi_cost = round(acquisition_price * ITBI_RATE, 2)
        registry_cost = REGISTRY_BASE
        legal_cost = LAWYER_COST if prop.auction_type != AuctionType.NAO_LEILAO else 0

        total_acquisition_cost = round(
            acquisition_price + renovation_cost + itbi_cost + registry_cost + legal_cost, 2
        )

        estimated_sale_price = market_value or round(acquisition_price * 1.35, 2)
        estimated_profit = round(estimated_sale_price - total_acquisition_cost, 2)
        estimated_roi_pct = round(
            (estimated_profit / total_acquisition_cost) * 100, 2
        ) if total_acquisition_cost > 0 else 0

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

    def _empty_financial(self):
        return {
            "renovation_cost": None, "itbi_cost": None,
            "registry_cost": None, "legal_cost": None,
            "total_acquisition_cost": None, "estimated_sale_price": None,
            "estimated_profit": None, "estimated_roi_pct": None,
            "payback_months": None,
        }

    def _calculate_liquidity(self, neighborhood_norm: str) -> tuple:
        avg_days = next(
            (v for k, v in LIQUIDITY_BY_NEIGHBORHOOD.items() if k in neighborhood_norm),
            LIQUIDITY_BY_NEIGHBORHOOD["_default"],
        )
        score = max(0, min(100, int(100 - (avg_days - 45) / 0.9)))
        return score, avg_days

    def _calculate_score(
        self,
        real_discount_pct,
        liquidity_score,
        auction_type,
        occupation_status,
        comparable_count,
        extra_data,
    ) -> tuple:
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
        breakdown["discount"] = discount_score

        # 2. Liquidez (25 pts)
        liq_points = round(liquidity_score * 0.25, 1)
        breakdown["liquidity"] = liq_points

        # 3. Modalidade (20 pts) — CORRIGIDO
        # 2 leilao: lance minimo 50% do avaliado — melhor oportunidade
        # 1 leilao: lance minimo 75% do avaliado
        # Venda direta: preco negociado, sem competicao — bom mas diferente
        # Licitacao aberta: similar ao leilao
        auction_map = {
            AuctionType.SEGUNDO_LEILAO: 20,   # Melhor: menor lance minimo
            AuctionType.PRIMEIRO_LEILAO: 12,  # Bom mas lance mais alto
            AuctionType.VENDA_DIRETA: 16,     # Sem competicao, preco negociavel
            AuctionType.NAO_LEILAO: 0,
        }
        auction_score = auction_map.get(auction_type, 0)
        breakdown["auction_type"] = auction_score

        # 4. Ocupacao (15 pts)
        occupation_map = {
            OccupationStatus.DESOCUPADO: 15,
            OccupationStatus.INDEFINIDO: 5,
            OccupationStatus.OCUPADO: -15,  # Penalidade maior — risco real
        }
        occupation_score = occupation_map.get(occupation_status, 0)
        breakdown["occupation"] = occupation_score

        # 5. Dividas identificadas — penalidade
        divida_score = 0
        if extra_data.get("iptu_debito"):
            divida_score -= 10
        if extra_data.get("condominio_debito"):
            divida_score -= 8
        if extra_data.get("acao_judicial"):
            divida_score -= 15
        breakdown["dividas"] = divida_score

        # 6. Confianca dos comparaveis (+5 bonus)
        confidence_bonus = 5 if comparable_count >= 10 else (2 if comparable_count >= 5 else 0)
        breakdown["confidence_bonus"] = confidence_bonus

        total = max(0, min(100, round(
            discount_score + liq_points + auction_score +
            occupation_score + divida_score + confidence_bonus, 1
        )))

        return total, breakdown

    def _calculate_legal_risk(self, prop: Property) -> int:
        risk = 0
        if prop.occupation_status == OccupationStatus.OCUPADO:
            risk += 50
        elif prop.occupation_status == OccupationStatus.INDEFINIDO:
            risk += 20
        if prop.auction_type == AuctionType.PRIMEIRO_LEILAO:
            risk += 15
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