"""
CaixaScraper — Imóveis retomados e leilões da Caixa Econômica Federal.
Fonte: https://venda-imoveis.caixa.gov.br/listaweb/Lista_imoveis_{UF}.csv
"""

import csv
import io
from typing import List, Optional

import httpx

from app.core.config import get_settings
from app.core.logger import logger
from app.models.property import PropertySource, AuctionType, OccupationStatus
from app.services.scrapers.base import BaseScraper, BlockedError

settings = get_settings()

CAIXA_CSV_URL = "https://venda-imoveis.caixa.gov.br/listaweb/Lista_imoveis_{uf}.csv"
CAIXA_DETAIL_URL = "https://venda-imoveis.caixa.gov.br/sistema/detalhe-imovel.asp?hdnimovel={codigo}"

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
                "Accept": "*/*",
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
        url = CAIXA_CSV_URL.format(uf=uf)
        logger.debug(f"[Caixa] Baixando CSV: {url}")

        response = await client.get(url)
        if response.status_code == 403:
            raise BlockedError(f"Caixa bloqueou {uf}")
        response.raise_for_status()

        # Remove BOM e decodifica
        text = response.content.decode("utf-8-sig", errors="replace")
        return self._parse_csv(text, uf)

    def _parse_csv(self, text: str, uf: str) -> List[dict]:
        results = []
        lines = text.strip().split("\n")

        # Linha 0: título, Linha 1: cabeçalho, Linha 2+: dados
        if len(lines) < 3:
            return []

        # Detecta separador
        header_line = lines[1]
        sep = ";" if header_line.count(";") > header_line.count(",") else ","

        reader = csv.DictReader(
            io.StringIO("\n".join(lines[1:])),
            delimiter=sep,
        )

        for row in reader:
            parsed = self._parse_row(row, uf)
            if parsed:
                results.append(parsed)

        return results

    def _parse_row(self, row: dict, uf: str) -> Optional[dict]:
        try:
            # Normaliza chaves (remove espaços e BOM)
            row = {k.strip().lstrip("\ufeff"): v.strip() for k, v in row.items() if k}

            codigo = row.get("N° do imóvel", "").strip()
            city = row.get("Cidade", "").strip().title()
            neighborhood = row.get("Bairro", "").strip().title()
            address = row.get("Endereço", "").strip().title()
            price_raw = row.get("Preço", "")
            appraised_raw = row.get("Valor de avaliação", "")
            desconto_raw = row.get("Desconto", "0")
            descricao = row.get("Descrição", "")
            modalidade = row.get("Modalidade de venda", "")
            link = row.get("Link de acesso", "")
            financiamento = row.get("Financiamento", "")

            asking_price = self._parse_price(price_raw)
            appraised_value = self._parse_price(appraised_raw)

            if not asking_price and not appraised_value:
                return None

            # Extrai área e quartos da descrição
            total_area = self._extract_area(descricao)
            bedrooms = self._extract_bedrooms(descricao)
            property_type = self._extract_property_type(descricao)

            return {
                "source": PropertySource.CAIXA,
                "external_id": codigo,
                "source_url": link or CAIXA_DETAIL_URL.format(codigo=codigo),
                "title": f"{property_type.title()} - {neighborhood}, {city}",
                "description": descricao,
                "property_type": self._parse_property_type(property_type),
                "address": address,
                "neighborhood": neighborhood,
                "city": city,
                "state": uf,
                "total_area": total_area,
                "usable_area": total_area,
                "bedrooms": bedrooms,
                "asking_price": asking_price,
                "appraised_value": appraised_value,
                "min_bid": asking_price,
                "auction_type": self._parse_auction_type(modalidade),
                "occupation_status": OccupationStatus.INDEFINIDO,
                "extra_data": {
                    "modalidade": modalidade,
                    "financiamento": financiamento,
                    "desconto_caixa": desconto_raw,
                    "edital_url": link,
                },
            }
        except Exception as e:
            logger.debug(f"[Caixa] Erro ao parsear linha: {e}")
            return None

    def _extract_area(self, descricao: str) -> Optional[float]:
        import re
        match = re.search(r"([\d.,]+)\s*de\s*área\s*(total|privativa|útil)", descricao, re.IGNORECASE)
        if match:
            return self._parse_area(match.group(1))
        match = re.search(r"([\d.,]+)\s*m²", descricao, re.IGNORECASE)
        if match:
            return self._parse_area(match.group(1))
        return None

    def _extract_bedrooms(self, descricao: str) -> Optional[int]:
        import re
        match = re.search(r"(\d+)\s*qto", descricao, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None

    def _extract_property_type(self, descricao: str) -> str:
        d = descricao.lower()
        if "apart" in d or "ap," in d:
            return "apartamento"
        if "casa" in d:
            return "casa"
        if "terreno" in d or "lote" in d:
            return "terreno"
        if "cobertura" in d:
            return "cobertura"
        if "comercial" in d or "sala" in d or "loja" in d:
            return "comercial"
        return "apartamento"

    def _parse_auction_type(self, modalidade: str) -> AuctionType:
        m = modalidade.lower()
        if "2" in m and "leilão" in m:
            return AuctionType.SEGUNDO_LEILAO
        if "leilão" in m or "licitação" in m or "sfi" in m:
            return AuctionType.PRIMEIRO_LEILAO
        if "venda direta" in m or "venda online" in m:
            return AuctionType.VENDA_DIRETA
        return AuctionType.VENDA_DIRETA

    def _parse_property_type(self, raw: str):
        from app.models.property import PropertyType
        r = raw.lower()
        if "apart" in r:
            return PropertyType.APARTAMENTO
        if "casa" in r:
            return PropertyType.CASA
        if "terreno" in r or "lote" in r:
            return PropertyType.TERRENO
        if "cobertura" in r:
            return PropertyType.COBERTURA
        if "comercial" in r:
            return PropertyType.COMERCIAL
        return PropertyType.APARTAMENTO