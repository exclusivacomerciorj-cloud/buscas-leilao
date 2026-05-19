"""
Script para buscar imoveis do OLX via API interna.
Roda no GitHub Actions e importa para a API do Railway.
"""

import requests
import os
import json
import time

API_URL = os.environ["API_URL"]

BAIRROS_ALVO = [
    "barra-da-tijuca",
    "jacarepagua",
    "recreio-dos-bandeirantes",
    "botafogo",
    "copacabana",
    "tijuca",
    "andarai",
    "pechincha",
    "taquara",
    "itanhanga",
    "sao-conrado",
    "gardenia-azul",
    "curicica",
    "anil",
    "gloria",
    "cosme-velho",
    "grajau",
    "maracana",
    "laranjeiras",
    "catete",
    "flamengo",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Referer": "https://www.olx.com.br/",
    "Origin": "https://www.olx.com.br",
}

def fetch_olx_listings(bairro: str, page: int = 1) -> list:
    """Busca imoveis do OLX para um bairro."""
    url = f"https://www.olx.com.br/api/relevance/v2/search"
    params = {
        "q": "",
        "category": "1020",  # Imoveis
        "state": "rj",
        "city": "rio-de-janeiro",
        "neighborhood": bairro,
        "page": page,
        "size": 50,
        "listingType": "s",
        "business": "s",
    }

    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=30)
        print(f"  OLX/{bairro} p{page}: status {r.status_code}")

        if r.status_code == 200:
            data = r.json()
            listings = data.get("data", {}).get("ads", [])
            return listings
        return []
    except Exception as e:
        print(f"  Erro {bairro}: {e}")
        return []


def parse_listing(ad: dict, bairro: str) -> dict:
    """Converte um anuncio do OLX para o formato da API."""
    try:
        price_str = ad.get("price", "")
        price = None
        if price_str:
            price_clean = price_str.replace("R$", "").replace(".", "").replace(",", ".").strip()
            try:
                price = float(price_clean)
            except Exception:
                pass

        if not price or price <= 0:
            return None

        props = {p.get("label", "").lower(): p.get("value", "") for p in ad.get("properties", [])}
        area_raw = props.get("area total", props.get("area", ""))
        bedrooms_raw = props.get("quartos", "")

        area = None
        if area_raw:
            try:
                area = float(area_raw.replace("m²", "").replace(",", ".").strip().split()[0])
            except Exception:
                pass

        bedrooms = None
        if bedrooms_raw:
            try:
                bedrooms = int(bedrooms_raw)
            except Exception:
                pass

        return {
            "source": "olx",
            "external_id": str(ad.get("listId", "")),
            "source_url": ad.get("url", ""),
            "title": ad.get("subject", ""),
            "neighborhood": bairro.replace("-", " ").title(),
            "city": "Rio De Janeiro",
            "state": "RJ",
            "asking_price": price,
            "total_area": area,
            "usable_area": area,
            "bedrooms": bedrooms,
            "auction_type": "nao_leilao",
            "occupation_status": "desocupado",
        }
    except Exception as e:
        return None


def import_to_api(properties: list) -> int:
    """Importa lista de imoveis para a API."""
    if not properties:
        return 0

    try:
        r = requests.post(
            f"{API_URL}/api/v1/import/market",
            json={"properties": properties},
            timeout=60,
        )
        if r.status_code == 200:
            return r.json().get("imported", 0)
        else:
            print(f"  Import erro {r.status_code}: {r.text[:200]}")
            return 0
    except Exception as e:
        print(f"  Import excecao: {e}")
        return 0


# Main
print(f"Iniciando scraping OLX...")
print(f"API: {API_URL}")

total_imported = 0
all_properties = []

for bairro in BAIRROS_ALVO:
    print(f"\nBuscando: {bairro}")
    for page in range(1, 4):  # 3 paginas por bairro
        listings = fetch_olx_listings(bairro, page)
        if not listings:
            break

        for ad in listings:
            parsed = parse_listing(ad, bairro)
            if parsed:
                all_properties.append(parsed)

        print(f"  Pagina {page}: {len(listings)} anuncios")
        time.sleep(1)

    time.sleep(2)

print(f"\nTotal coletado: {len(all_properties)} imoveis")

# Importa em lotes de 100
for i in range(0, len(all_properties), 100):
    lote = all_properties[i:i+100]
    imported = import_to_api(lote)
    total_imported += imported
    print(f"Lote {i//100+1}: {imported} importados")
    time.sleep(0.5)

print(f"\nTOTAL IMPORTADO: {total_imported} imoveis do OLX")