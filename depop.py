"""
Playwright automation for Depop listings.
Connects to existing Chrome on port 9222.
Fixed price: $2.00 on Depop (best for quick turnover on lower-value cards).
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
DEPOP_SELL_URL = "https://www.depop.com/sell/"
DEPOP_FIXED_PRICE = 2.00

CONDITION_MAP = {
    "Mint": "New with tags",
    "Near Mint": "New without tags",
    "Lightly Played": "Like new",
    "Moderately Played": "Good",
    "Heavily Played": "Fair",
    "Poor": "Poor",
}


def _human_delay(lo=0.8, hi=2.2):
    time.sleep(lo + (hi - lo) * random.random())


def _type_human(element, text: str):
    element.click()
    _human_delay(0.2, 0.5)
    element.fill(text)
    _human_delay(0.3, 0.8)


def list_on_depop(item_id: int) -> str | None:
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
            return _do_depop_listing(page, item, image_path)
        except PWTimeout as e:
            logger.error("Timeout during Depop listing for item %d: %s", item_id, e)
            return None
        except Exception as e:
            logger.error("Error listing item %d on Depop: %s", item_id, e)
            return None
        finally:
            page.close()


def _do_depop_listing(page, item, image_path: str) -> str | None:
    logger.info("Listing '%s' on Depop at $%.2f", item["title"], DEPOP_FIXED_PRICE)
    page.goto(DEPOP_SELL_URL, wait_until="networkidle", timeout=30000)
    _human_delay(2, 4)

    _upload_photo(page, image_path)
    _fill_title(page, item)
    _fill_description(page, item)
    _select_category(page, item)
    _select_condition(page, item["condition"])
    _fill_price(page)
    _human_delay(1, 2)
    _submit_listing(page)

    depop_id = _extract_listing_id(page)
    if depop_id:
        db.update_item(item["id"], {"depop_id": depop_id, "status": "active"})
        logger.info("Depop listing created: %s", depop_id)
    else:
        logger.warning("Could not extract Depop ID from URL: %s", page.url)
    return depop_id


def _upload_photo(page, image_path: str):
    try:
        file_input = page.locator("input[type='file']").first
        if file_input.is_visible():
            file_input.set_input_files(image_path)
        else:
            with page.expect_file_chooser() as fc_info:
                page.locator(
                    "button:has-text('Add photo'), label:has-text('Add photo'), "
                    "div[data-testid*='photo']"
                ).first.click()
            fc_info.value.set_files(image_path)
        _human_delay(2, 4)
    except Exception as e:
        logger.warning("Depop photo upload issue: %s", e)


def _fill_title(page, item):
    try:
        title = item["title"][:75]
        field = page.locator(
            "input[name='title'], input[placeholder*='title' i], "
            "input[data-testid*='title' i]"
        ).first
        _type_human(field, title)
    except Exception as e:
        logger.warning("Title fill issue: %s", e)


def _fill_description(page, item):
    desc_parts = [
        f"{item['card_name']} - {item['condition']}",
    ]
    if item["set_name"]:
        desc_parts.append(f"Set: {item['set_name']}")
    if item["card_number"]:
        desc_parts.append(f"Card #: {item['card_number']}")
    if item["rarity"]:
        desc_parts.append(f"Rarity: {item['rarity']}")
    desc_parts += ["", "Ships in protective sleeve. Fast shipping!"]
    desc = "\n".join(desc_parts)
    try:
        field = page.locator(
            "textarea[name='description'], textarea[placeholder*='description' i], "
            "div[contenteditable='true']"
        ).first
        _type_human(field, desc)
    except Exception as e:
        logger.warning("Description fill issue: %s", e)


def _select_category(page, item):
    card_type = item["card_type"]
    keywords = {
        "Pokemon": "Trading Cards",
        "MTG": "Trading Cards",
        "YuGiOh": "Trading Cards",
        "Sports": "Sports",
        "HotWheels": "Toys",
        "Other": "Collectibles",
    }
    keyword = keywords.get(card_type, "Collectibles")
    try:
        cat_btn = page.locator(
            "button:has-text('Category'), select[name='category'], "
            "div[data-testid*='category']"
        ).first
        if cat_btn.is_visible(timeout=3000):
            cat_btn.click()
            _human_delay(0.8, 1.5)
            option = page.locator(f"*:has-text('{keyword}')").first
            if option.is_visible(timeout=3000):
                option.click()
    except Exception as e:
        logger.warning("Category selection issue: %s", e)


def _select_condition(page, condition: str):
    depop_condition = CONDITION_MAP.get(condition, "Like new")
    try:
        cond_area = page.locator(
            "select[name='condition'], div[data-testid*='condition'], "
            f"label:has-text('Condition')"
        ).first
        if cond_area.is_visible(timeout=3000):
            cond_area.click()
            _human_delay(0.5, 1)
            opt = page.locator(
                f"option:has-text('{depop_condition}'), "
                f"li:has-text('{depop_condition}')"
            ).first
            if opt.is_visible(timeout=2000):
                opt.click()
    except Exception as e:
        logger.warning("Condition selection issue: %s", e)


def _fill_price(page):
    try:
        price_field = page.locator(
            "input[name='price'], input[placeholder*='price' i], "
            "input[data-testid*='price']"
        ).first
        _type_human(price_field, f"{DEPOP_FIXED_PRICE:.2f}")
    except Exception as e:
        logger.warning("Price fill issue: %s", e)


def _submit_listing(page):
    try:
        btn = page.locator(
            "button:has-text('List'), button:has-text('Sell'), "
            "button[type='submit']:has-text('List')"
        ).first
        btn.click()
        _human_delay(3, 6)
    except Exception as e:
        logger.warning("Submit issue: %s", e)


def _extract_listing_id(page) -> str | None:
    url = page.url
    patterns = [
        r"depop\.com/products/([A-Za-z0-9_-]+)",
        r"/([A-Za-z0-9]{8,})/?\s*$",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def delete_depop_listing(depop_id: str) -> bool:
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            logger.error("Cannot connect to Chrome: %s", e)
            return False

        context = browser.contexts[0]
        page = context.new_page()
        try:
            url = f"https://www.depop.com/products/{depop_id}/"
            page.goto(url, timeout=20000)
            _human_delay(2, 3)
            delete_btn = page.locator(
                "button:has-text('Delete'), a:has-text('Delete'), "
                "button[aria-label*='delete' i]"
            ).first
            delete_btn.click()
            _human_delay(1, 2)
            confirm = page.locator("button:has-text('Yes'), button:has-text('Delete item')").first
            if confirm.is_visible(timeout=3000):
                confirm.click()
            _human_delay(2, 3)
            logger.info("Deleted Depop listing: %s", depop_id)
            return True
        except Exception as e:
            logger.error("Failed to delete Depop listing %s: %s", depop_id, e)
            return False
        finally:
            page.close()
