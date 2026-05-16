"""
OLXScraper — Imóveis do OLX Brasil.

Estratégia: Playwright (headless) para contornar proteções.
URL alvo: https://www.olx.com.br/imoveis/venda/estado-rj

Campos extraídos:
  - Título, preço, endereço, bairro
  - Área, quartos, banheiros, vagas
  - URL do anúncio, fotos
  - Data de publicação (para calcular tempo no mercado)
"""

import json
import re
from typing import List, Optional

from app.core.logger import logger
from app.models.property import PropertySource, AuctionType, OccupationStatus
from app.services.scrapers.base import BaseScraper, BlockedError

OLX_BASE = "https://www.olx.com.br/imoveis/venda/estado-rj"
OLX_BAIRROS = {
    "barra-da-tijuca": "https://www.olx.com.br/imoveis/venda/estado-rj/rio-de-janeiro-e-regiao/barra-da-tijuca",
    "jacarepagua": "https://www.olx.com.br/imoveis/venda/estado-rj/rio-de-janeiro-e-regiao/jacarepagua",
    "recreio": "https://www.olx.com.br/imoveis/venda/estado-rj/rio-de-janeiro-e-regiao/recreio-dos-bandeirantes",
}


class OLXScraper(BaseScraper):
    source = PropertySource.OLX
    BASE_URL = "https://www.olx.com.br"
    REQUEST_DELAY = (2.0, 5.0)  # OLX é mais sensível a scraping

    def __init__(self, neighborhoods: Optional[List[str]] = None, max_pages: int = 5):
        super().__init__()
        self.neighborhoods = neighborhoods or list(OLX_BAIRROS.keys())
        self.max_pages = max_pages

    async def scrape(self) -> List[dict]:
        all_properties = []

        async with self._get_browser() as browser:
            for neighborhood in self.neighborhoods:
                if neighborhood not in OLX_BAIRROS:
                    logger.warning(f"[OLX] Bairro desconhecido: {neighborhood}")
                    continue

                base_url = OLX_BAIRROS[neighborhood]
                try:
                    props = await self._scrape_neighborhood(browser, neighborhood, base_url)
                    all_properties.extend(props)
                    logger.info(f"[OLX/{neighborhood}] {len(props)} imóveis encontrados")
                except Exception as e:
                    logger.error(f"[OLX/{neighborhood}] Erro: {e}")
                    self._stats["errors"] += 1

        return all_properties

    async def _scrape_neighborhood(
        self, browser, neighborhood: str, base_url: str
    ) -> List[dict]:
        properties = []

        async with self._get_context(browser) as context:
            async with self._get_page(context) as page:
                for page_num in range(1, self.max_pages + 1):
                    url = f"{base_url}?o={page_num}" if page_num > 1 else base_url

                    try:
                        await page.goto(url, wait_until="networkidle", timeout=30_000)
                        await self._random_delay()

                        html = await page.content()
                        if self._is_blocked(html):
                            logger.warning(f"[OLX] Bloqueado na página {page_num}")
                            break

                        # OLX injeta dados no __NEXT_DATA__ ou em JSON no script
                        page_data = await self._extract_next_data(page)
                        if page_data:
                            listings = self._parse_next_data(page_data, neighborhood)
                        else:
                            # Fallback: parse HTML direto
                            listings = await self._parse_html_listings(page, neighborhood)

                        if not listings:
                            logger.info(f"[OLX/{neighborhood}] Sem mais resultados na página {page_num}")
                            break

                        properties.extend(listings)
                        logger.debug(f"[OLX/{neighborhood}] Página {page_num}: {len(listings)} anúncios")

                        # Verifica se há próxima página
                        has_next = await page.query_selector('[data-testid="next-page-button"]:not([disabled])')
                        if not has_next:
                            break

                    except Exception as e:
                        logger.error(f"[OLX/{neighborhood}] Erro página {page_num}: {e}")
                        self._stats["errors"] += 1
                        break

        return properties

    async def _extract_next_data(self, page) -> Optional[dict]:
        """Extrai __NEXT_DATA__ injetado pelo Next.js."""
        try:
            data = await page.evaluate(
                "() => window.__NEXT_DATA__ ? JSON.stringify(window.__NEXT_DATA__) : null"
            )
            if data:
                return json.loads(data)
        except Exception:
            pass
        return None

    def _parse_next_data(self, data: dict, neighborhood: str) -> List[dict]:
        """Parseia o JSON do Next.js do OLX."""
        try:
            # Navega na estrutura do JSON (pode mudar com updates do OLX)
            ads = (
                data.get("props", {})
                    .get("pageProps", {})
                    .get("ads", [])
            )
            if not ads:
                ads = (
                    data.get("props", {})
                        .get("pageProps", {})
                        .get("listingProps", {})
                        .get("listing", {})
                        .get("listingData", {})
                        .get("listingList", [])
                )
        except (AttributeError, KeyError):
            return []

        results = []
        for ad in ads:
            parsed = self._parse_ad(ad, neighborhood)
            if parsed:
                results.append(parsed)
        return results

    def _parse_ad(self, ad: dict, neighborhood: str) -> Optional[dict]:
        try:
            ad_id = str(ad.get("listId") or ad.get("id", ""))
            title = ad.get("subject") or ad.get("title", "")
            price_raw = ad.get("price") or ad.get("priceValue", "")
            url = ad.get("url") or ad.get("link", "")

            # Localização
            location = ad.get("location") or {}
            address = location.get("address", "")
            bairro = location.get("neighbourhood") or location.get("neighborhood") or neighborhood
            city = location.get("municipality") or "Rio de Janeiro"

            # Atributos do imóvel
            properties = {
                p.get("label", "").lower(): p.get("value", "")
                for p in ad.get("properties", [])
            }
            area_raw = properties.get("área total") or properties.get("area total") or ""
            bedrooms_raw = properties.get("quartos") or ""
            bathrooms_raw = properties.get("banheiros") or ""
            parking_raw = properties.get("vagas") or ""

            asking_price = self._parse_price(str(price_raw))
            if not asking_price:
                return None

            return {
                "source": PropertySource.OLX,
                "external_id": ad_id,
                "source_url": url if url.startswith("http") else f"https://www.olx.com.br{url}",
                "title": title,
                "neighborhood": bairro,
                "city": city,
                "state": "RJ",
                "address": address,
                "total_area": self._parse_area(str(area_raw)),
                "usable_area": self._parse_area(str(area_raw)),
                "bedrooms": int(bedrooms_raw) if str(bedrooms_raw).isdigit() else None,
                "bathrooms": int(bathrooms_raw) if str(bathrooms_raw).isdigit() else None,
                "parking_spots": int(parking_raw) if str(parking_raw).isdigit() else None,
                "asking_price": asking_price,
                "auction_type": AuctionType.NAO_LEILAO,
                "occupation_status": OccupationStatus.DESOCUPADO,  # Assume desocupado para mercado tradicional
                "photos": [img.get("original", "") for img in ad.get("images", [])[:5]],
                "extra_data": {
                    "published_at": ad.get("date"),
                    "category": ad.get("category"),
                },
            }
        except Exception as e:
            logger.debug(f"[OLX] Erro ao parsear anúncio: {e}")
            return None

    async def _parse_html_listings(self, page, neighborhood: str) -> List[dict]:
        """Fallback: extrai listagens diretamente do HTML."""
        results = []
        try:
            cards = await page.query_selector_all('[data-testid="listing-card"]')
            if not cards:
                cards = await page.query_selector_all(".fnmrjs-0")  # Classe do OLX atual

            for card in cards:
                try:
                    title_el = await card.query_selector("h2, h3")
                    title = await title_el.inner_text() if title_el else ""

                    price_el = await card.query_selector('[data-testid="listing-price"], .price')
                    price_raw = await price_el.inner_text() if price_el else ""

                    link_el = await card.query_selector("a")
                    url = await link_el.get_attribute("href") if link_el else ""

                    asking_price = self._parse_price(price_raw)
                    if not asking_price:
                        continue

                    results.append({
                        "source": PropertySource.OLX,
                        "external_id": None,
                        "source_url": url if url.startswith("http") else f"https://www.olx.com.br{url}",
                        "title": title,
                        "neighborhood": neighborhood,
                        "city": "Rio de Janeiro",
                        "state": "RJ",
                        "asking_price": asking_price,
                        "auction_type": AuctionType.NAO_LEILAO,
                        "occupation_status": OccupationStatus.DESOCUPADO,
                    })
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"[OLX] Fallback HTML parse falhou: {e}")

        return results
