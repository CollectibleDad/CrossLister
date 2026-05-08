"""
Scrapes eBay completed/active listings to get smart pricing.
No API keys required — uses BeautifulSoup directly.
Weighted avg: 60% sold prices + 40% active prices. Floor: $1.22.
"""
import logging
import re
import time
import random
from urllib.parse import urlencode, quote_plus

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

FLOOR_PRICE = 1.22
SOLD_WEIGHT = 0.60
ACTIVE_WEIGHT = 0.40
MAX_SOLD = 5
MAX_ACTIVE = 3
REQUEST_DELAY = (1.5, 3.5)
MAX_RETRIES = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def _sleep():
    time.sleep(random.uniform(*REQUEST_DELAY))


def _fetch(url: str) -> BeautifulSoup | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _sleep()
            resp = SESSION.get(url, timeout=20)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.HTTPError as e:
            if resp.status_code == 429:
                wait = 30 * attempt
                logger.warning("Rate limited — waiting %ds", wait)
                time.sleep(wait)
            else:
                logger.warning("HTTP %d on attempt %d: %s", resp.status_code, attempt, url)
        except Exception as e:
            logger.warning("Fetch error attempt %d: %s", attempt, e)
        if attempt < MAX_RETRIES:
            time.sleep(5 * attempt)
    return None


def _build_query(card_info: dict) -> str:
    parts = []
    if card_info.get("card_name"):
        parts.append(card_info["card_name"])
    if card_info.get("set_name"):
        parts.append(card_info["set_name"])
    if card_info.get("card_number"):
        parts.append(card_info["card_number"])
    if card_info.get("rarity"):
        parts.append(card_info["rarity"])
    return " ".join(parts)


def _extract_price(text: str) -> float | None:
    text = text.replace(",", "").strip()
    match = re.search(r"\$(\d+(?:\.\d{2})?)", text)
    if match:
        return float(match.group(1))
    match = re.search(r"(\d+(?:\.\d{2})?)", text)
    if match:
        return float(match.group(1))
    return None


def _parse_item_prices(soup: BeautifulSoup) -> list[float]:
    prices = []
    selectors = [
        ".s-item__price",
        ".s-item__detail--primary .s-item__price",
        "[itemprop='price']",
    ]
    for selector in selectors:
        elements = soup.select(selector)
        if elements:
            for el in elements:
                text = el.get_text()
                if "to" in text.lower():
                    parts = re.findall(r"\$[\d,]+\.?\d*", text)
                    for p in parts:
                        val = _extract_price(p)
                        if val and val > 0.01:
                            prices.append(val)
                else:
                    val = _extract_price(text)
                    if val and val > 0.01:
                        prices.append(val)
            break
    skip_phrases = ["shop on ebay", "shipping", "postage"]
    prices = [
        p for p in prices
        if p < 5000
    ]
    return prices


def get_sold_prices(card_info: dict) -> list[float]:
    query = _build_query(card_info)
    if not query.strip():
        return []
    params = {
        "_nkw": query,
        "LH_Complete": "1",
        "LH_Sold": "1",
        "_sop": "13",
        "_ipg": "25",
    }
    url = "https://www.ebay.com/sch/i.html?" + urlencode(params)
    logger.info("Fetching sold listings: %s", query)
    soup = _fetch(url)
    if not soup:
        return []
    prices = _parse_item_prices(soup)
    result = sorted(prices)[:MAX_SOLD]
    logger.info("Sold prices found: %s", result)
    return result


def get_active_prices(card_info: dict) -> list[float]:
    query = _build_query(card_info)
    if not query.strip():
        return []
    params = {
        "_nkw": query,
        "LH_BIN": "1",
        "_sop": "15",
        "_ipg": "25",
    }
    url = "https://www.ebay.com/sch/i.html?" + urlencode(params)
    logger.info("Fetching active listings: %s", query)
    soup = _fetch(url)
    if not soup:
        return []
    prices = _parse_item_prices(soup)
    result = sorted(prices)[:MAX_ACTIVE]
    logger.info("Active prices found: %s", result)
    return result


def calculate_price(sold_prices: list[float], active_prices: list[float]) -> float:
    if not sold_prices and not active_prices:
        logger.warning("No price data found — using floor price")
        return FLOOR_PRICE

    sold_avg = sum(sold_prices) / len(sold_prices) if sold_prices else None
    active_avg = sum(active_prices) / len(active_prices) if active_prices else None

    if sold_avg is not None and active_avg is not None:
        price = sold_avg * SOLD_WEIGHT + active_avg * ACTIVE_WEIGHT
    elif sold_avg is not None:
        price = sold_avg
    else:
        price = active_avg

    price = max(price, FLOOR_PRICE)
    price = round(price, 2)
    logger.info(
        "Price calc — sold_avg=%.2f active_avg=%s → $%.2f",
        sold_avg or 0,
        f"${active_avg:.2f}" if active_avg else "N/A",
        price,
    )
    return price


def get_smart_price(card_info: dict) -> dict:
    sold = get_sold_prices(card_info)
    active = get_active_prices(card_info)
    price = calculate_price(sold, active)
    return {
        "asking_price": price,
        "sold_prices": sold,
        "active_prices": active,
        "sold_avg": round(sum(sold) / len(sold), 2) if sold else None,
        "active_avg": round(sum(active) / len(active), 2) if active else None,
        "data_points": len(sold) + len(active),
    }
