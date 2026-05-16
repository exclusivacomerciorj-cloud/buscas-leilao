"""
CaixaScraper — Imóveis retomados e leilões da Caixa Econômica Federal.

Fonte: https://venda-imoveis.caixa.gov.br
API pública da Caixa (usa endpoint JSON não documentado).

Campos extraídos:
  - Endereço, bairro, cidade, UF
  - Área, quartos, vagas
  - Valor avaliado, valor mínimo de venda
  - Modalidade (licitação, venda direta, leilão)
  - Status de ocupação
  - Link do edital (PDF)
"""

import re
from typing import List, Optional
from urllib.parse import urlencode

import httpx

from app.core.config import get_settings
from app.core.logger import logger
from app.models.property import PropertySource, AuctionType, OccupationStatus
from app.services.scrapers.base import BaseScraper, BlockedError

settings = get_settings()


# Endpoint da API da Caixa (descoberto via análise do tráfego do site)
CAIXA_API_BASE = "https://venda-imoveis.caixa.gov.br/listaweb/Lista_imoveis_{uf}.json"
CAIXA_DETAIL_URL = "https://venda-imoveis.caixa.gov.br/sistema/detalhe-imovel.asp?hdnOrigem=index&hdnimovel={codigo}"

# Estados alvo (começando pelo RJ para o MVP)
TARGET_UFS = ["RJ"]


class CaixaScraper(BaseScraper):
    source = PropertySource.CAIXA
    BASE_URL = "https://venda-imoveis.caixa.gov.br"

    def __init__(self, ufs: Optional[List[str]] = None):
        super().__init__()
        self.ufs = ufs or TARGET_UFS

    async def scrape(self) -> List[dict]:
        all_properties = []

        async with httpx.AsyncClient(
            timeout=30.0,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://venda-imoveis.caixa.gov.br/",
            },
            follow_redirects=True,
        ) as client:
            for uf in self.ufs:
                try:
                    props = await self._scrape_uf(client, uf)
                    all_properties.extend(props)
                    logger.info(f"[Caixa/{uf}] {len(props)} imóveis encontrados")
                    await self._random_delay()
                except Exception as e:
                    logger.error(f"[Caixa/{uf}] Erro: {e}")
                    self._stats["errors"] += 1

        return all_properties

    async def _scrape_uf(self, client: httpx.AsyncClient, uf: str) -> List[dict]:
        url = CAIXA_API_BASE.format(uf=uf)
        logger.debug(f"[Caixa] Buscando: {url}")

        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                raise BlockedError(f"Caixa bloqueou a requisição para {uf}")
            raise

        # A Caixa retorna um JSON às vezes com BOM ou encoding diferente
        text = response.text.lstrip("\ufeff")

        try:
            data = response.json() if response.headers.get("content-type", "").startswith("application/json") \
                else __import__("json").loads(text)
        except Exception:
            logger.warning(f"[Caixa/{uf}] Resposta não é JSON válido, tentando parse alternativo")
            return self._parse_csv_fallback(text, uf)

        if not data:
            return []

        # O JSON da Caixa é uma lista de registros
        properties = []
        for item in data if isinstance(data, list) else data.get("imoveis", []):
            parsed = self._parse_item(item, uf)
            if parsed:
                properties.append(parsed)

        return properties

    def _parse_item(self, item: dict, uf: str) -> Optional[dict]:
        """Normaliza um item do JSON da Caixa para o schema padrão."""
        try:
            # Campos variam conforme a versão do JSON da Caixa
            # Tentamos múltiplos nomes de campo
            codigo = (
                item.get("NUMERO_IMOVEL") or item.get("codigo") or
                item.get("id") or item.get("NumeroImovel", "")
            )
            address = (
                item.get("LOGRADOURO") or item.get("logradouro") or
                item.get("Endereco", "")
            )
            neighborhood = (
                item.get("BAIRRO") or item.get("bairro") or
                item.get("Bairro", "")
            )
            city = item.get("MUNICIPIO") or item.get("municipio") or item.get("Cidade", "")
            
            price_raw = (
                item.get("PRECO") or item.get("preco") or
                item.get("ValorVenda") or item.get("valor_venda", "")
            )
            appraised_raw = (
                item.get("VALOR_AVALIACAO") or item.get("valor_avaliacao") or
                item.get("ValorAvaliacao", "")
            )
            area_raw = (
                item.get("AREA_TOTAL") or item.get("area_total") or
                item.get("AreaTotal") or item.get("area", "")
            )
            bedrooms_raw = (
                item.get("NUMERO_QUARTOS") or item.get("quartos") or
                item.get("NumeroQuartos", 0)
            )
            modality = (
                item.get("MODALIDADE_VENDA") or item.get("modalidade") or
                item.get("TipoVenda", "")
            )
            occupation_raw = (
                item.get("DESCRICAO_OCUPACAO") or item.get("ocupacao") or
                item.get("Ocupacao", "")
            )
            property_type_raw = (
                item.get("TIPO_IMOVEL") or item.get("tipo") or
                item.get("TipoImovel", "")
            )
            edital_url = item.get("LINK_EDITAL") or item.get("link_edital") or ""

            asking_price = self._parse_price(str(price_raw))
            appraised_value = self._parse_price(str(appraised_raw))
            total_area = self._parse_area(str(area_raw))

            # Mínimo necessário: ter preço ou valor de avaliação
            if not asking_price and not appraised_value:
                return None

            # Para leilão, o preço mínimo pode ser o asking ou um desconto do avaliado
            min_bid = asking_price or (appraised_value * 0.6 if appraised_value else None)

            return {
                "source": PropertySource.CAIXA,
                "external_id": str(codigo) if codigo else None,
                "source_url": CAIXA_DETAIL_URL.format(codigo=codigo) if codigo else None,
                "address": address,
                "neighborhood": neighborhood,
                "city": city or "Rio de Janeiro",
                "state": uf,
                "total_area": total_area,
                "usable_area": total_area,  # Caixa não separa área útil
                "bedrooms": int(bedrooms_raw) if bedrooms_raw else None,
                "asking_price": asking_price,
                "appraised_value": appraised_value,
                "min_bid": min_bid,
                "auction_type": self._parse_auction_type(str(modality)),
                "occupation_status": self._parse_occupation(str(occupation_raw)),
                "property_type": self._parse_property_type(str(property_type_raw)),
                "extra_data": {
                    "modalidade": modality,
                    "edital_url": edital_url,
                    "raw": item,
                },
            }
        except Exception as e:
            logger.warning(f"[Caixa] Erro ao parsear item: {e} | item={item}")
            return None

    def _parse_auction_type(self, modality: str) -> AuctionType:
        m = modality.lower()
        if "1" in m and "leilão" in m or "primeiro" in m:
            return AuctionType.PRIMEIRO_LEILAO
        if "2" in m and "leilão" in m or "segundo" in m:
            return AuctionType.SEGUNDO_LEILAO
        if "venda direta" in m or "licitação" in m:
            return AuctionType.VENDA_DIRETA
        return AuctionType.VENDA_DIRETA  # Default Caixa

    def _parse_occupation(self, raw: str) -> OccupationStatus:
        r = raw.lower()
        if "desocup" in r or "vago" in r or "livre" in r:
            return OccupationStatus.DESOCUPADO
        if "ocupado" in r or "ocupad" in r:
            return OccupationStatus.OCUPADO
        return OccupationStatus.INDEFINIDO

    def _parse_property_type(self, raw: str) -> str:
        from app.models.property import PropertyType
        r = raw.lower()
        if "apart" in r or "ap " in r:
            return PropertyType.APARTAMENTO
        if "casa" in r or "resid" in r:
            return PropertyType.CASA
        if "terreno" in r or "lote" in r:
            return PropertyType.TERRENO
        if "cobertura" in r:
            return PropertyType.COBERTURA
        if "comercial" in r or "sala" in r or "loja" in r:
            return PropertyType.COMERCIAL
        return PropertyType.APARTAMENTO

    def _parse_csv_fallback(self, text: str, uf: str) -> List[dict]:
        """
        Fallback: a Caixa às vezes retorna CSV em vez de JSON.
        """
        logger.info(f"[Caixa/{uf}] Tentando parse CSV...")
        lines = text.strip().split("\n")
        if len(lines) < 2:
            return []

        # Tenta detectar separador
        sep = ";" if ";" in lines[0] else ","
        headers = [h.strip().strip('"') for h in lines[0].split(sep)]

        results = []
        for line in lines[1:]:
            values = [v.strip().strip('"') for v in line.split(sep)]
            if len(values) != len(headers):
                continue
            item = dict(zip(headers, values))
            parsed = self._parse_item(item, uf)
            if parsed:
                results.append(parsed)

        return results
