"""
Checks sold status on Mercari and Depop by scraping each active listing's page.
Run daily — updates database when items are marked sold.
"""
import logging
import re
import time
import random
from typing import Optional

import requests
from bs4 import BeautifulSoup

import database as db

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def _sleep():
    time.sleep(random.uniform(1.5, 4.0))


def _fetch(url: str) -> Optional[BeautifulSoup]:
    for attempt in range(1, 4):
        try:
            _sleep()
            resp = SESSION.get(url, timeout=20, allow_redirects=True)
            if resp.status_code == 404:
                logger.debug("404 for %s — may be deleted", url)
                return None
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            logger.warning("Fetch attempt %d failed for %s: %s", attempt, url, e)
            if attempt < 3:
                time.sleep(5 * attempt)
    return None


def _check_mercari_listing(mercari_id: str) -> dict:
    url = f"https://www.mercari.com/us/item/{mercari_id}/"
    soup = _fetch(url)
    result = {"sold": False, "price": None, "deleted": False}

    if soup is None:
        result["deleted"] = True
        return result

    page_text = soup.get_text().lower()

    sold_indicators = ["sold out", "this item has sold", "item sold", "no longer available"]
    if any(phrase in page_text for phrase in sold_indicators):
        result["sold"] = True
        price_el = soup.select_one(".merPrice, [data-testid='price'], .price-tag")
        if price_el:
            price_text = price_el.get_text()
            m = re.search(r"\$?([\d,]+\.?\d*)", price_text)
            if m:
                result["price"] = float(m.group(1).replace(",", ""))
        return result

    if "page not found" in page_text or "404" in page_text:
        result["deleted"] = True

    return result


def _check_depop_listing(depop_id: str) -> dict:
    url = f"https://www.depop.com/products/{depop_id}/"
    soup = _fetch(url)
    result = {"sold": False, "price": None, "deleted": False}

    if soup is None:
        result["deleted"] = True
        return result

    page_text = soup.get_text().lower()

    sold_indicators = ["sold", "this item has sold", "no longer available"]
    status_el = soup.select_one(
        "[data-testid='productStatus'], .styles__SoldStatus, "
        ".sc-eoEacH, div[class*='sold' i]"
    )
    if status_el and "sold" in status_el.get_text().lower():
        result["sold"] = True
    elif any(phrase in page_text for phrase in ["item sold", "this item has sold"]):
        result["sold"] = True

    if result["sold"]:
        price_el = soup.select_one(
            "[data-testid='price'], .styles__Price, .sc-htpNat, p[class*='price' i]"
        )
        if price_el:
            m = re.search(r"\$?([\d,]+\.?\d*)", price_el.get_text())
            if m:
                result["price"] = float(m.group(1).replace(",", ""))

    if "page not found" in page_text or "404" in page_text:
        result["deleted"] = True

    return result


def _check_ebay_listing(ebay_id: str) -> dict:
    url = f"https://www.ebay.com/itm/{ebay_id}"
    soup = _fetch(url)
    result = {"sold": False, "price": None, "deleted": False}

    if soup is None:
        result["deleted"] = True
        return result

    page_text = soup.get_text().lower()

    if "this listing was ended" in page_text or "item not found" in page_text:
        result["deleted"] = True
        return result

    if "sold" in page_text and "watch" not in page_text[:500]:
        sold_el = soup.select_one(".vi-lk-txt-sold, [data-testid='sold'], .u-lnkBtnV2")
        if sold_el:
            result["sold"] = True

    return result


def check_all_sales() -> dict:
    logger.info("=== Sales Check Started ===")
    active_items = db.get_active_items()
    logger.info("Checking %d active items", len(active_items))

    stats = {"checked": 0, "sold": [], "deleted": [], "errors": []}

    for item in active_items:
        item_id = item["id"]
        title = item["title"] or f"Item #{item_id}"
        stats["checked"] += 1

        # Check Mercari
        if item["mercari_id"]:
            try:
                status = _check_mercari_listing(item["mercari_id"])
                if status["sold"]:
                    price = status["price"] or item["asking_price"]
                    db.mark_sold(item_id, "mercari", price)
                    stats["sold"].append({"id": item_id, "title": title, "platform": "mercari", "price": price})
                    logger.info("SOLD on Mercari: %s — $%.2f", title, price)
                elif status["deleted"]:
                    db.update_item(item_id, {"mercari_id": None})
                    logger.info("Mercari listing gone (deleted externally): %s", title)
            except Exception as e:
                logger.error("Error checking Mercari for item %d: %s", item_id, e)
                stats["errors"].append(item_id)

        # Check Depop (only if not already sold)
        fresh_item = db.get_item_by_id(item_id)
        if fresh_item and fresh_item["status"] == "active" and item["depop_id"]:
            try:
                status = _check_depop_listing(item["depop_id"])
                if status["sold"]:
                    price = status["price"] or item["asking_price"]
                    db.mark_sold(item_id, "depop", price)
                    stats["sold"].append({"id": item_id, "title": title, "platform": "depop", "price": price})
                    logger.info("SOLD on Depop: %s — $%.2f", title, price)
                elif status["deleted"]:
                    db.update_item(item_id, {"depop_id": None})
            except Exception as e:
                logger.error("Error checking Depop for item %d: %s", item_id, e)
                stats["errors"].append(item_id)

        # Check eBay (only if not already sold)
        fresh_item = db.get_item_by_id(item_id)
        if fresh_item and fresh_item["status"] == "active" and item["ebay_id"]:
            try:
                status = _check_ebay_listing(item["ebay_id"])
                if status["sold"]:
                    price = status["price"] or item["asking_price"]
                    db.mark_sold(item_id, "ebay", price)
                    stats["sold"].append({"id": item_id, "title": title, "platform": "ebay", "price": price})
                    logger.info("SOLD on eBay: %s — $%.2f", title, price)
                elif status["deleted"]:
                    db.update_item(item_id, {"ebay_id": None})
            except Exception as e:
                logger.error("Error checking eBay for item %d: %s", item_id, e)
                stats["errors"].append(item_id)

    logger.info(
        "=== Sales Check Complete: %d checked, %d sold, %d errors ===",
        stats["checked"], len(stats["sold"]), len(stats["errors"])
    )
    return stats
