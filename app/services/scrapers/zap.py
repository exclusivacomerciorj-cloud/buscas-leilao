"""
ZAPScraper — Imóveis do ZAP Imóveis.

O ZAP usa GraphQL + Next.js. Estratégia:
  1. Tenta endpoint de busca interno (JSON) via httpx
  2. Fallback: Playwright headless + extração do __NEXT_DATA__

URL alvo: https://www.zapimoveis.com.br/venda/imoveis/rj+rio-de-janeiro/
"""

import json
import re
from typing import List, Optional
from urllib.parse import urlencode

import httpx

from app.core.config import get_settings
from app.core.logger import logger
from app.models.property import PropertySource, AuctionType, OccupationStatus, PropertyType
from app.services.scrapers.base import BaseScraper, BlockedError

settings = get_settings()

ZAP_API_URL = "https://glue-api.zapimoveis.com.br/v2/listings"
ZAP_BASE_URL = "https://www.zapimoveis.com.br"

# Bairros-alvo do MVP
ZAP_NEIGHBORHOODS = ["Barra da Tijuca", "Jacarepaguá", "Recreio dos Bandeirantes"]

ZAP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "x-domain": "www.zapimoveis.com.br",
    "Referer": "https://www.zapimoveis.com.br/",
    "Origin": "https://www.zapimoveis.com.br",
}


class ZAPScraper(BaseScraper):
    source = PropertySource.ZAP
    BASE_URL = ZAP_BASE_URL
    REQUEST_DELAY = (2.5, 5.5)

    def __init__(self, neighborhoods: Optional[List[str]] = None, max_pages: int = 5):
        super().__init__()
        self.neighborhoods = neighborhoods or ZAP_NEIGHBORHOODS
        self.max_pages = max_pages

    async def scrape(self) -> List[dict]:
        all_properties = []

        async with httpx.AsyncClient(
            timeout=20.0, headers=ZAP_HEADERS, follow_redirects=True
        ) as client:
            for neighborhood in self.neighborhoods:
                try:
                    props = await self._scrape_neighborhood_api(client, neighborhood)
                    if not props:
                        # Fallback: Playwright
                        props = await self._scrape_neighborhood_playwright(neighborhood)

                    all_properties.extend(props)
                    logger.info(f"[ZAP/{neighborhood}] {len(props)} imóveis encontrados")
                    await self._random_delay()
                except Exception as e:
                    logger.error(f"[ZAP/{neighborhood}] Erro: {e}")
                    self._stats["errors"] += 1

        return all_properties

    # ─── Estratégia 1: API interna ────────────────────────────────────────────

    async def _scrape_neighborhood_api(
        self, client: httpx.AsyncClient, neighborhood: str
    ) -> List[dict]:
        """Tenta o endpoint de busca interno do ZAP."""
        properties = []

        for page in range(1, self.max_pages + 1):
            params = {
                "page": page,
                "pageSize": 24,
                "listingType": "USED",
                "business": "SALE",
                "unitTypes": "APARTMENT,HOME",
                "neighborhoods": neighborhood,
                "citySlug": "rio-de-janeiro",
                "stateSlug": "rj",
                "sort": "updatedAt DESC",
            }

            try:
                response = await client.get(ZAP_API_URL, params=params)

                if response.status_code == 403:
                    raise BlockedError("ZAP bloqueou a requisição")

                if response.status_code != 200:
                    logger.warning(f"[ZAP] Status {response.status_code} para {neighborhood}")
                    break

                data = response.json()
                listings = (
                    data.get("search", {}).get("result", {}).get("listings", [])
                    or data.get("listings", [])
                )

                if not listings:
                    break

                for item in listings:
                    parsed = self._parse_api_listing(item, neighborhood)
                    if parsed:
                        properties.append(parsed)

                # Verifica paginação
                total_count = data.get("search", {}).get("totalCount", 0)
                if page * 24 >= total_count:
                    break

                await self._random_delay()

            except BlockedError:
                raise
            except Exception as e:
                logger.warning(f"[ZAP/API] Falha na página {page}: {e}")
                break

        return properties

    def _parse_api_listing(self, item: dict, neighborhood: str) -> Optional[dict]:
        """Parseia um listing da API do ZAP."""
        try:
            listing = item.get("listing") or item
            account = item.get("account") or {}

            external_id = listing.get("id") or listing.get("externalId", "")
            title = listing.get("title", "")
            description = listing.get("description", "")

            # Endereço
            address_obj = listing.get("address") or {}
            street = address_obj.get("street", "")
            bairro = address_obj.get("neighborhood") or neighborhood
            city = address_obj.get("city", "Rio de Janeiro")
            zipcode = address_obj.get("zipCode", "")
            lat = address_obj.get("point", {}).get("lat")
            lon = address_obj.get("point", {}).get("lon")

            # Preços
            pricing = listing.get("pricingInfos") or [{}]
            price_info = next(
                (p for p in pricing if p.get("businessType") == "SALE"),
                pricing[0] if pricing else {}
            )
            price_raw = price_info.get("price") or price_info.get("yearlyIptu", "")

            # Área e quartos
            usable_area = listing.get("usableAreas", [None])[0]
            total_area = listing.get("totalAreas", [None])[0] or usable_area
            bedrooms = listing.get("bedrooms", [None])
            bedrooms = bedrooms[0] if bedrooms else None
            bathrooms = listing.get("bathrooms", [None])
            bathrooms = bathrooms[0] if bathrooms else None
            parking = listing.get("parkingSpaces", [None])
            parking = parking[0] if parking else None

            # Tipo
            unit_types = listing.get("unitTypes", ["APARTMENT"])
            prop_type = self._map_property_type(unit_types[0] if unit_types else "")

            # Fotos
            media = listing.get("medias") or []
            photos = [m.get("url", "").replace("{action}", "fit-in") for m in media[:5]]

            asking_price = self._parse_price(str(price_raw))
            if not asking_price:
                return None

            return {
                "source": PropertySource.ZAP,
                "external_id": str(external_id),
                "source_url": f"{ZAP_BASE_URL}/imovel/{external_id}/",
                "title": title,
                "description": description[:500] if description else None,
                "property_type": prop_type,
                "address": street,
                "neighborhood": bairro,
                "city": city,
                "state": "RJ",
                "zipcode": zipcode,
                "latitude": float(lat) if lat else None,
                "longitude": float(lon) if lon else None,
                "total_area": float(total_area) if total_area else None,
                "usable_area": float(usable_area) if usable_area else None,
                "bedrooms": int(bedrooms) if bedrooms is not None else None,
                "bathrooms": int(bathrooms) if bathrooms is not None else None,
                "parking_spots": int(parking) if parking is not None else None,
                "asking_price": asking_price,
                "auction_type": AuctionType.NAO_LEILAO,
                "occupation_status": OccupationStatus.DESOCUPADO,
                "photos": [p for p in photos if p],
                "extra_data": {
                    "advertiser": account.get("name"),
                    "published_at": listing.get("createdAt"),
                    "updated_at": listing.get("updatedAt"),
                    "highlight": listing.get("highlights", []),
                },
            }
        except Exception as e:
            logger.debug(f"[ZAP] Erro ao parsear listing: {e}")
            return None

    # ─── Estratégia 2: Playwright fallback ────────────────────────────────────

    async def _scrape_neighborhood_playwright(self, neighborhood: str) -> List[dict]:
        """Fallback com Playwright para quando a API está bloqueada."""
        results = []
        neighborhood_slug = neighborhood.lower().replace(" ", "-").replace("ã", "a").replace("é", "e")
        url = f"{ZAP_BASE_URL}/venda/imoveis/rj+rio-de-janeiro+{neighborhood_slug}/"

        try:
            async with self._get_browser() as browser:
                async with self._get_context(browser) as context:
                    async with self._get_page(context) as page:
                        await page.goto(url, wait_until="networkidle", timeout=35_000)
                        await self._random_delay()

                        html = await page.content()
                        if self._is_blocked(html):
                            logger.warning(f"[ZAP/Playwright] Bloqueado em {neighborhood}")
                            return []

                        # Tenta extrair __NEXT_DATA__
                        next_data_raw = await page.evaluate(
                            "() => { const el = document.getElementById('__NEXT_DATA__'); return el ? el.textContent : null; }"
                        )

                        if next_data_raw:
                            try:
                                next_data = json.loads(next_data_raw)
                                listings = self._extract_listings_from_next_data(next_data)
                                for item in listings:
                                    parsed = self._parse_api_listing(item, neighborhood)
                                    if parsed:
                                        results.append(parsed)
                                return results
                            except Exception as e:
                                logger.warning(f"[ZAP/Playwright] Erro no __NEXT_DATA__: {e}")

                        # Fallback final: parse HTML
                        results = await self._parse_playwright_html(page, neighborhood)

        except Exception as e:
            logger.error(f"[ZAP/Playwright] Falha geral: {e}")

        return results

    def _extract_listings_from_next_data(self, data: dict) -> list:
        """Navega na estrutura do __NEXT_DATA__ do ZAP."""
        try:
            return (
                data["props"]["pageProps"]["data"]["search"]["result"]["listings"]
            )
        except (KeyError, TypeError):
            pass
        try:
            return data["props"]["pageProps"]["listings"]
        except (KeyError, TypeError):
            return []

    async def _parse_playwright_html(self, page, neighborhood: str) -> List[dict]:
        """Último recurso: extrai dados diretamente dos cards HTML do ZAP."""
        results = []
        try:
            cards = await page.query_selector_all('[data-type="property"]')
            if not cards:
                cards = await page.query_selector_all(".listings-wrapper__card")

            for card in cards[:24]:
                try:
                    price_el = await card.query_selector('[class*="price"]')
                    price_raw = await price_el.inner_text() if price_el else ""

                    area_el = await card.query_selector('[class*="area"]')
                    area_raw = await area_el.inner_text() if area_el else ""

                    link_el = await card.query_selector("a")
                    url = await link_el.get_attribute("href") if link_el else ""

                    asking_price = self._parse_price(price_raw)
                    if not asking_price:
                        continue

                    results.append({
                        "source": PropertySource.ZAP,
                        "external_id": None,
                        "source_url": url if url.startswith("http") else f"{ZAP_BASE_URL}{url}",
                        "neighborhood": neighborhood,
                        "city": "Rio de Janeiro",
                        "state": "RJ",
                        "total_area": self._parse_area(area_raw),
                        "asking_price": asking_price,
                        "auction_type": AuctionType.NAO_LEILAO,
                        "occupation_status": OccupationStatus.DESOCUPADO,
                    })
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"[ZAP/HTML] Parse falhou: {e}")
        return results

    def _map_property_type(self, raw: str) -> PropertyType:
        mapping = {
            "APARTMENT": PropertyType.APARTAMENTO,
            "HOME": PropertyType.CASA,
            "PENTHOUSE": PropertyType.COBERTURA,
            "LAND": PropertyType.TERRENO,
            "COMMERCIAL": PropertyType.COMERCIAL,
        }
        return mapping.get(raw.upper(), PropertyType.APARTAMENTO)
