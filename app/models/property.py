"""
Models da plataforma Buscas Leilão.

Tabelas principais:
  - properties        → imóveis capturados (leilão + mercado)
  - property_analyses → análise financeira e score de cada imóvel
  - alerts            → alertas disparados para usuários
  - users             → usuários do sistema (SaaS)
  - scrape_logs       → logs de execução dos scrapers
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer,
    String, Text, JSON, Enum as SAEnum, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.db.session import Base


# ─── Enums ────────────────────────────────────────────────────────────────────

import enum


class PropertySource(str, enum.Enum):
    CAIXA = "caixa"
    OLX = "olx"
    ZAP = "zap"
    VIVAREAL = "vivareal"
    IMOVELWEB = "imovelweb"
    SANTANDER = "santander"
    ITAU = "itau"
    BRADESCO = "bradesco"


class PropertyType(str, enum.Enum):
    APARTAMENTO = "apartamento"
    CASA = "casa"
    TERRENO = "terreno"
    COMERCIAL = "comercial"
    COBERTURA = "cobertura"


class PropertyStatus(str, enum.Enum):
    ATIVO = "ativo"
    VENDIDO = "vendido"
    EXPIRADO = "expirado"
    SUSPENSO = "suspenso"


class OccupationStatus(str, enum.Enum):
    DESOCUPADO = "desocupado"
    OCUPADO = "ocupado"
    INDEFINIDO = "indefinido"


class AuctionType(str, enum.Enum):
    PRIMEIRO_LEILAO = "primeiro_leilao"
    SEGUNDO_LEILAO = "segundo_leilao"
    VENDA_DIRETA = "venda_direta"
    NAO_LEILAO = "nao_leilao"


class AlertChannel(str, enum.Enum):
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    WEBHOOK = "webhook"


class AlertStatus(str, enum.Enum):
    PENDENTE = "pendente"
    ENVIADO = "enviado"
    FALHOU = "falhou"


# ─── Models ───────────────────────────────────────────────────────────────────

class Property(Base):
    """Imóvel capturado de qualquer fonte."""
    __tablename__ = "properties"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Identificação externa (evita duplicatas)
    external_id: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    source: Mapped[PropertySource] = mapped_column(SAEnum(PropertySource), index=True)
    source_url: Mapped[Optional[str]] = mapped_column(Text)

    # Dados básicos
    title: Mapped[Optional[str]] = mapped_column(String(500))
    description: Mapped[Optional[str]] = mapped_column(Text)
    property_type: Mapped[Optional[PropertyType]] = mapped_column(SAEnum(PropertyType))
    status: Mapped[PropertyStatus] = mapped_column(
        SAEnum(PropertyStatus), default=PropertyStatus.ATIVO, index=True
    )

    # Localização
    address: Mapped[Optional[str]] = mapped_column(String(500))
    neighborhood: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    city: Mapped[Optional[str]] = mapped_column(String(200), default="Rio de Janeiro")
    state: Mapped[Optional[str]] = mapped_column(String(2), default="RJ")
    zipcode: Mapped[Optional[str]] = mapped_column(String(10))
    latitude: Mapped[Optional[float]] = mapped_column(Float)
    longitude: Mapped[Optional[float]] = mapped_column(Float)

    # Dimensões
    total_area: Mapped[Optional[float]] = mapped_column(Float)       # m² total
    usable_area: Mapped[Optional[float]] = mapped_column(Float)      # m² útil
    bedrooms: Mapped[Optional[int]] = mapped_column(Integer)
    bathrooms: Mapped[Optional[int]] = mapped_column(Integer)
    parking_spots: Mapped[Optional[int]] = mapped_column(Integer)

    # Preços
    asking_price: Mapped[Optional[float]] = mapped_column(Float)     # Preço anunciado
    appraised_value: Mapped[Optional[float]] = mapped_column(Float)  # Valor avaliado (edital)
    min_bid: Mapped[Optional[float]] = mapped_column(Float)          # Lance mínimo (leilão)

    # Leilão
    auction_type: Mapped[AuctionType] = mapped_column(
        SAEnum(AuctionType), default=AuctionType.NAO_LEILAO
    )
    auction_date: Mapped[Optional[datetime]] = mapped_column(DateTime)
    occupation_status: Mapped[OccupationStatus] = mapped_column(
        SAEnum(OccupationStatus), default=OccupationStatus.INDEFINIDO
    )

    # Dados extras (JSON flexível para campos específicos de cada fonte)
    extra_data: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    photos: Mapped[Optional[list]] = mapped_column(JSON, default=list)

    # Timestamps
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    # Relacionamentos
    analyses: Mapped[list["PropertyAnalysis"]] = relationship(
        back_populates="property", cascade="all, delete-orphan"
    )
    alerts: Mapped[list["Alert"]] = relationship(
        back_populates="property"
    )

    @property
    def price_per_sqm(self) -> Optional[float]:
        price = self.asking_price or self.min_bid
        area = self.usable_area or self.total_area
        if price and area and area > 0:
            return round(price / area, 2)
        return None

    def __repr__(self) -> str:
        return f"<Property {self.source.value} | {self.neighborhood} | R${self.asking_price:,.0f}>"


class PropertyAnalysis(Base):
    """Análise financeira e score de oportunidade de um imóvel."""
    __tablename__ = "property_analyses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    property_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("properties.id"), index=True
    )

    # Score de oportunidade (0-100)
    opportunity_score: Mapped[Optional[float]] = mapped_column(Float, index=True)
    score_breakdown: Mapped[Optional[dict]] = mapped_column(JSON)
    # Ex: {"discount": 35, "liquidity": 20, "legal_risk": -5, "location": 15, ...}

    # Precificação de mercado
    market_value_estimated: Mapped[Optional[float]] = mapped_column(Float)
    market_price_per_sqm: Mapped[Optional[float]] = mapped_column(Float)
    comparable_count: Mapped[Optional[int]] = mapped_column(Integer)
    real_discount_pct: Mapped[Optional[float]] = mapped_column(Float)  # % abaixo do mercado

    # Simulação financeira
    renovation_cost: Mapped[Optional[float]] = mapped_column(Float)   # Custo reforma
    itbi_cost: Mapped[Optional[float]] = mapped_column(Float)          # ITBI (~3%)
    registry_cost: Mapped[Optional[float]] = mapped_column(Float)      # Cartório
    legal_cost: Mapped[Optional[float]] = mapped_column(Float)         # Advogado/jurídico
    total_acquisition_cost: Mapped[Optional[float]] = mapped_column(Float)  # Tudo incluso
    estimated_sale_price: Mapped[Optional[float]] = mapped_column(Float)    # Preço de saída
    estimated_profit: Mapped[Optional[float]] = mapped_column(Float)        # Lucro estimado
    estimated_roi_pct: Mapped[Optional[float]] = mapped_column(Float)       # ROI %
    payback_months: Mapped[Optional[int]] = mapped_column(Integer)

    # Risco jurídico
    legal_risk_score: Mapped[Optional[int]] = mapped_column(Integer)   # 0=baixo, 100=alto
    legal_issues: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    # Ex: ["iptu_debito", "ocupado", "acao_judicial"]

    # Liquidez da região
    avg_days_on_market: Mapped[Optional[int]] = mapped_column(Integer)
    liquidity_score: Mapped[Optional[int]] = mapped_column(Integer)    # 0-100

    # Metadata da análise
    analyzed_by: Mapped[str] = mapped_column(String(50), default="engine_v1")
    ai_summary: Mapped[Optional[str]] = mapped_column(Text)  # Resumo gerado pela IA
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relacionamentos
    property: Mapped["Property"] = relationship(back_populates="analyses")

    def __repr__(self) -> str:
        return f"<Analysis score={self.opportunity_score} roi={self.estimated_roi_pct}%>"


class User(Base):
    """Usuário do sistema (para SaaS)."""
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    hashed_password: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    # Preferências de alerta
    alert_channels: Mapped[list] = mapped_column(JSON, default=lambda: ["email"])
    alert_min_score: Mapped[int] = mapped_column(Integer, default=70)
    alert_neighborhoods: Mapped[list] = mapped_column(JSON, default=list)
    alert_max_price: Mapped[Optional[float]] = mapped_column(Float)
    alert_min_discount: Mapped[float] = mapped_column(Float, default=25.0)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    alerts: Mapped[list["Alert"]] = relationship(back_populates="user")


class Alert(Base):
    """Alertas disparados quando uma oportunidade é encontrada."""
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    property_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("properties.id"), index=True
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    channel: Mapped[AlertChannel] = mapped_column(SAEnum(AlertChannel))
    recipient: Mapped[str] = mapped_column(String(200))  # e-mail ou número
    message: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[AlertStatus] = mapped_column(
        SAEnum(AlertStatus), default=AlertStatus.PENDENTE
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    property: Mapped["Property"] = relationship(back_populates="alerts")
    user: Mapped[Optional["User"]] = relationship(back_populates="alerts")


class ScrapeLog(Base):
    """Histórico de execução dos scrapers."""
    __tablename__ = "scrape_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source: Mapped[PropertySource] = mapped_column(SAEnum(PropertySource), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    total_found: Mapped[int] = mapped_column(Integer, default=0)
    new_properties: Mapped[int] = mapped_column(Integer, default=0)
    updated_properties: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    error_details: Mapped[Optional[dict]] = mapped_column(JSON)
    success: Mapped[bool] = mapped_column(Boolean, default=True)

    def __repr__(self) -> str:
        return f"<ScrapeLog {self.source.value} found={self.total_found} new={self.new_properties}>"
