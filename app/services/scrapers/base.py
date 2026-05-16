"""
BaseScraper — classe base para todos os scrapers da plataforma.

Recursos:
  - Rotação de proxies
  - User-agent aleatório
  - Retries com backoff exponencial
  - Headless browser via Playwright
  - Rate limiting por domínio
"""

import asyncio
import random
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncGenerator, List, Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from fake_useragent import UserAgent
from tenacity import (
    retry, stop_after_attempt, wait_exponential,
    retry_if_exception_type, before_sleep_log
)

from app.core.config import get_settings
from app.core.logger import logger
from app.models.property import PropertySource

settings = get_settings()
ua = UserAgent()


class ScraperError(Exception):
    pass


class BlockedError(ScraperError):
    """Levantada quando o portal detectou e bloqueou o scraper."""
    pass


class BaseScraper(ABC):
    """
    Classe base para todos os scrapers.

    Subclasses precisam implementar:
        - source: PropertySource
        - scrape() -> List[dict]
    """

    source: PropertySource
    BASE_URL: str
    REQUEST_DELAY = (1.5, 4.0)   # segundos entre requisições (min, max)
    MAX_RETRIES = 3

    def __init__(self):
        self._proxies = settings.proxies
        self._proxy_index = 0
        self._browser: Optional[Browser] = None
        self._stats = {
            "total_found": 0,
            "errors": 0,
            "started_at": None,
            "finished_at": None,
        }

    # ─── Proxy rotation ───────────────────────────────────────────────────────

    def _next_proxy(self) -> Optional[dict]:
        if not self._proxies:
            return None
        proxy_url = self._proxies[self._proxy_index % len(self._proxies)]
        self._proxy_index += 1
        logger.debug(f"Usando proxy: {proxy_url[:30]}...")
        return {"server": proxy_url}

    # ─── Browser context ──────────────────────────────────────────────────────

    @asynccontextmanager
    async def _get_browser(self) -> AsyncGenerator[Browser, None]:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            try:
                yield browser
            finally:
                await browser.close()

    @asynccontextmanager
    async def _get_context(self, browser: Browser) -> AsyncGenerator[BrowserContext, None]:
        proxy = self._next_proxy()
        context = await browser.new_context(
            user_agent=ua.random,
            proxy=proxy,
            viewport={"width": random.randint(1200, 1920), "height": random.randint(700, 1080)},
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            extra_http_headers={
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        # Mascara a propriedade navigator.webdriver
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        try:
            yield context
        finally:
            await context.close()

    @asynccontextmanager
    async def _get_page(self, context: BrowserContext) -> AsyncGenerator[Page, None]:
        page = await context.new_page()
        page.set_default_timeout(30_000)
        try:
            yield page
        finally:
            await page.close()

    # ─── Helpers ──────────────────────────────────────────────────────────────

    async def _random_delay(self) -> None:
        delay = random.uniform(*self.REQUEST_DELAY)
        logger.debug(f"Aguardando {delay:.1f}s...")
        await asyncio.sleep(delay)

    def _is_blocked(self, html: str) -> bool:
        """Detecta páginas de bloqueio comuns (Cloudflare, Captcha)."""
        blocked_signals = [
            "just a moment",
            "cf-browser-verification",
            "captcha",
            "acesso negado",
            "access denied",
            "403 forbidden",
            "too many requests",
        ]
        html_lower = html.lower()
        return any(signal in html_lower for signal in blocked_signals)

    def _parse_price(self, raw: str) -> Optional[float]:
        """Converte 'R$ 1.250.000' ou '850000.0' → float correto."""
        if not raw:
            return None
        import re
        # Remove prefixos e espaços
        cleaned = raw.replace("R$", "").replace("\xa0", "").replace(" ", "").strip()
        if not cleaned or cleaned in ("-", "—"):
            return None

        # Formato brasileiro: 1.250.000 ou 1.250.000,50
        # Detecta: tem vírgula E ponto → vírgula é decimal, ponto é milhar
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        # Só vírgula → vírgula é decimal (ex: 850000,00)
        elif "," in cleaned and "." not in cleaned:
            cleaned = cleaned.replace(",", ".")
        # Só ponto: se tem MÚLTIPLOS pontos → milhar (1.250.000)
        elif cleaned.count(".") > 1:
            cleaned = cleaned.replace(".", "")
        # Único ponto: é decimal (850000.0 ou 1250.50)
        # → mantém como está

        try:
            value = float(cleaned)
            return value if value > 0 else None
        except (ValueError, TypeError):
            return None

    def _parse_area(self, raw: str) -> Optional[float]:
        """Converte '85 m²' → 85.0"""
        if not raw:
            return None
        cleaned = raw.replace("m²", "").replace("m2", "").replace(",", ".").strip()
        try:
            return float(cleaned.split()[0])
        except (ValueError, TypeError, IndexError):
            return None

    # ─── Interface pública ────────────────────────────────────────────────────

    @abstractmethod
    async def scrape(self) -> List[dict]:
        """
        Executa o scraping e retorna lista de dicts com dados brutos dos imóveis.
        Cada dict deve conter pelo menos: source, source_url, asking_price.
        """
        ...

    async def run(self) -> List[dict]:
        """Wrapper com logging e tratamento de erros."""
        self._stats["started_at"] = datetime.utcnow()
        logger.info(f"[{self.source.value}] Iniciando scraping...")

        try:
            results = await self.scrape()
            self._stats["total_found"] = len(results)
            self._stats["finished_at"] = datetime.utcnow()
            elapsed = (self._stats["finished_at"] - self._stats["started_at"]).seconds
            logger.info(
                f"[{self.source.value}] Concluído: {len(results)} imóveis em {elapsed}s"
            )
            return results
        except Exception as e:
            self._stats["errors"] += 1
            self._stats["finished_at"] = datetime.utcnow()
            logger.error(f"[{self.source.value}] Erro no scraping: {e}")
            raise
