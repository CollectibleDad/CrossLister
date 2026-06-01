"""
Playwright automation for eBay listings.
Connects to existing Chrome on port 9222.
Uses smart price from pricing.py.
"""
import logging
import re
import time
import random
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import database as db

logger = logging.getLogger(__name__)

CDP_URL = "http://localhost:9222"
EBAY_SELL_URL = "https://www.ebay.com/sl/sell"

CONDITION_MAP = {
    "Mint": "New",
    "Near Mint": "Like New",
    "Lightly Played": "Very Good",
    "Moderately Played": "Good",
    "Heavily Played": "Acceptable",
    "Poor": "For parts or not working",
}

EBAY_CATEGORY_IDS = {
    "Pokemon":   "2536",
    "MTG":       "19107",
    "YuGiOh":   "183454",
    "Sports":    "212",
    "HotWheels": "6028",
    "Other":     "1",
}


def _human_delay(lo=0.8, hi=2.2):
    time.sleep(lo + (hi - lo) * random.random())


def _type_human(element, text: str):
    element.click()
    _human_delay(0.2, 0.5)
    element.fill(text)
    _human_delay(0.3, 0.8)


def list_on_ebay(item_id: int) -> str | None:
    item = db.get_item_by_id(item_id)
    if not item:
        logger.error("Item %d not found", item_id)
        return None

    image_path = item["image_path"]
    if not image_path or not Path(image_path).exists():
        logger.error("Image not found for item %d: %s", item_id, image_path)
        return None

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            logger.error("Cannot connect to Chrome on port 9222: %s", e)
            return None

        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()

        try:
            return _do_ebay_listing(page, item, image_path)
        except PWTimeout as e:
            logger.error("Timeout during eBay listing for item %d: %s", item_id, e)
            return None
        except Exception as e:
            logger.error("Error listing item %d on eBay: %s", item_id, e)
            return None
        finally:
            page.close()


def _do_ebay_listing(page, item, image_path: str) -> str | None:
    logger.info("Listing '%s' on eBay at $%.2f", item["title"], item["asking_price"])

    # Start with keyword-based listing flow
    page.goto(EBAY_SELL_URL, wait_until="domcontentloaded", timeout=60000)
    _human_delay(2, 4)

    # Enter keywords to help eBay find the right category
    keyword_input = page.locator(
        "input[placeholder*='keyword' i], input[name='query'], "
        "input[id*='query'], input[data-testid*='keyword']"
    ).first
    if keyword_input.is_visible(timeout=5000):
        _type_human(keyword_input, item["title"][:60])
        start_btn = page.locator(
            "button:has-text('Get started'), button:has-text('List an item'), "
            "button[type='submit']"
        ).first
        start_btn.click()
        _human_delay(3, 5)

    # Fill in the listing form
    _fill_title(page, item)
    _upload_photo(page, image_path)
    _select_condition(page, item["condition"])
    _fill_description(page, item)
    _fill_price(page, item["asking_price"])
    _fill_item_specifics(page, item)
    _human_delay(1, 2)
    _submit_listing(page)

    ebay_id = _extract_item_id(page)
    if ebay_id:
        db.update_item(item["id"], {"ebay_id": ebay_id, "status": "active"})
        logger.info("eBay listing created: %s", ebay_id)
    else:
        logger.warning("Could not extract eBay item ID from: %s", page.url)
    return ebay_id


def _fill_title(page, item):
    try:
        field = page.locator(
            "input[id*='title'], input[name='title'], "
            "input[placeholder*='Item title' i], input[data-testid*='title']"
        ).first
        field.wait_for(timeout=10000)
        field.click(click_count=3)
        _type_human(field, item["title"][:80])
    except Exception as e:
        logger.warning("eBay title issue: %s", e)


def _upload_photo(page, image_path: str):
    try:
        file_input = page.locator("input[type='file']").first
        if file_input.count() > 0:
            file_input.set_input_files(image_path)
        else:
            with page.expect_file_chooser() as fc_info:
                page.locator(
                    "button:has-text('Add photos'), label:has-text('Add photo'), "
                    "button[aria-label*='photo' i]"
                ).first.click()
            fc_info.value.set_files(image_path)
        _human_delay(3, 6)
        logger.info("Photo uploaded to eBay")
    except Exception as e:
        logger.warning("eBay photo upload issue: %s", e)


def _select_condition(page, condition: str):
    ebay_condition = CONDITION_MAP.get(condition, "Very Good")
    try:
        select = page.locator("select[id*='condition'], select[name*='condition']").first
        if select.is_visible(timeout=3000):
            select.select_option(label=ebay_condition)
            return
        btn = page.locator(f"button:has-text('{ebay_condition}'), *[data-value*='condition']").first
        if btn.is_visible(timeout=2000):
            btn.click()
    except Exception as e:
        logger.warning("eBay condition issue: %s", e)


def _fill_description(page, item):
    desc_parts = [
        f"{item['card_name']}",
        f"Condition: {item['condition']}",
    ]
    if item["set_name"]:
        desc_parts.append(f"Set: {item['set_name']}")
    if item["card_number"]:
        desc_parts.append(f"Card Number: {item['card_number']}")
    if item["rarity"]:
        desc_parts.append(f"Rarity: {item['rarity']}")
    desc_parts += [
        "",
        "Card ships in a protective sleeve inside a rigid top loader.",
        "Combined shipping available. Check my other listings!",
    ]
    desc = "\n".join(desc_parts)
    try:
        desc_field = page.locator(
            "textarea[id*='description'], textarea[name='description'], "
            "div[id*='description'][contenteditable='true']"
        ).first
        if desc_field.is_visible(timeout=5000):
            _type_human(desc_field, desc)
    except Exception as e:
        logger.warning("eBay description issue: %s", e)


def _fill_price(page, price: float):
    price_str = f"{price:.2f}"
    try:
        price_field = page.locator(
            "input[id*='price'], input[name*='price'], "
            "input[placeholder*='price' i], input[data-testid*='price']"
        ).first
        if price_field.is_visible(timeout=5000):
            price_field.click(click_count=3)
            _type_human(price_field, price_str)
    except Exception as e:
        logger.warning("eBay price issue: %s", e)


def _fill_item_specifics(page, item):
    specifics = {}
    if item["card_name"]:
        specifics["Card Name"] = item["card_name"]
    if item["set_name"]:
        specifics["Set"] = item["set_name"]
    if item["card_number"]:
        specifics["Card Number"] = item["card_number"]
    if item["rarity"]:
        specifics["Rarity"] = item["rarity"]
    for label, value in specifics.items():
        try:
            field = page.locator(
                f"input[aria-label*='{label}' i], input[placeholder*='{label}' i]"
            ).first
            if field.is_visible(timeout=1500):
                _type_human(field, value)
        except Exception:
            pass


def _submit_listing(page):
    try:
        btn = page.locator(
            "button:has-text('List it'), button:has-text('Submit'), "
            "button:has-text('List item'), button[id*='submit']"
        ).first
        btn.click()
        _human_delay(4, 8)
    except Exception as e:
        logger.warning("eBay submit issue: %s", e)


def _extract_item_id(page) -> str | None:
    url = page.url
    patterns = [
        r"ebay\.com/itm/(\d{10,})",
        r"itemId=(\d{10,})",
        r"/(\d{10,})\b",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def delete_ebay_listing(ebay_id: str) -> bool:
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            logger.error("Cannot connect to Chrome: %s", e)
            return False

        context = browser.contexts[0]
        page = context.new_page()
        try:
            # Go to seller hub to end the listing
            url = f"https://www.ebay.com/itm/{ebay_id}"
            page.goto(url, timeout=20000)
            _human_delay(2, 3)
            # Look for End listing option
            more_actions = page.locator(
                "button:has-text('More actions'), a:has-text('End listing'), "
                "button[aria-label*='end' i]"
            ).first
            more_actions.click()
            _human_delay(1, 2)
            end_btn = page.locator("a:has-text('End listing'), button:has-text('End')").first
            if end_btn.is_visible(timeout=3000):
                end_btn.click()
                _human_delay(2, 3)
                confirm = page.locator("button:has-text('End my listing')").first
                if confirm.is_visible(timeout=3000):
                    confirm.click()
            _human_delay(2, 3)
            logger.info("Ended eBay listing: %s", ebay_id)
            return True
        except Exception as e:
            logger.error("Failed to end eBay listing %s: %s", ebay_id, e)
            return False
        finally:
            page.close()
