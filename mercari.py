"""
Playwright automation for Mercari listings.
Connects to an existing Chrome instance on port 9222.
Start Chrome with: chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\ChromeData
"""
import logging
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import database as db

logger = logging.getLogger(__name__)

CDP_URL = "http://localhost:9222"
MERCARI_SELL_URL = "https://www.mercari.com/sell/"
DEPOP_PRICE_OVERRIDE = None  # None = use smart price

# Condition map from our standard to Mercari's UI
CONDITION_MAP = {
    "Mint": "Like New",
    "Near Mint": "Like New",
    "Lightly Played": "Good",
    "Moderately Played": "Fair",
    "Heavily Played": "Poor",
    "Poor": "Poor",
}


def _human_delay(lo=0.8, hi=2.0):
    time.sleep(lo + (hi - lo) * __import__("random").random())


def _type_human(element, text: str):
    element.click()
    _human_delay(0.2, 0.5)
    element.fill(text)
    _human_delay(0.3, 0.8)


def list_on_mercari(item_id: int) -> str | None:
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
            logger.error("Cannot connect to Chrome on port 9222. %s", e)
            logger.error("Start Chrome with: chrome.exe --remote-debugging-port=9222")
            return None

        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()

        try:
            return _do_mercari_listing(page, item, image_path)
        except PWTimeout as e:
            logger.error("Timeout during Mercari listing for item %d: %s", item_id, e)
            return None
        except Exception as e:
            logger.error("Error listing item %d on Mercari: %s", item_id, e)
            return None
        finally:
            page.close()


def _do_mercari_listing(page, item, image_path: str) -> str | None:
    logger.info("Listing '%s' on Mercari", item["title"])
    page.goto(MERCARI_SELL_URL, wait_until="networkidle", timeout=30000)
    _human_delay(2, 4)

    # Photo upload
    _upload_photo(page, image_path)

    # Title
    title_field = page.locator(
        "input[placeholder*='title' i], input[name='name'], textarea[name='name'], "
        "input[data-testid*='title' i], div[data-testid='name'] input"
    ).first
    _type_human(title_field, item["title"][:80])

    # Category — search by keyword
    _select_category(page, item)

    # Condition
    _select_condition(page, item["condition"])

    # Description
    desc = _build_description(item)
    desc_field = page.locator(
        "textarea[placeholder*='description' i], textarea[name='description'], "
        "div[data-testid='description'] textarea"
    ).first
    _type_human(desc_field, desc)

    # Price
    price_str = f"{item['asking_price']:.2f}"
    price_field = page.locator(
        "input[placeholder*='price' i], input[name='price'], "
        "div[data-testid='price'] input"
    ).first
    _type_human(price_field, price_str)

    _human_delay(1, 2)

    # Submit / List
    submit = page.locator(
        "button:has-text('List'), button:has-text('Submit'), "
        "button[data-testid*='submit' i], button[type='submit']"
    ).first
    submit.click()
    _human_delay(3, 6)

    # Extract listing ID from URL or confirmation page
    mercari_id = _extract_listing_id(page)
    if mercari_id:
        db.update_item(item["id"], {"mercari_id": mercari_id, "status": "active"})
        logger.info("Mercari listing created: %s", mercari_id)
    else:
        logger.warning("Could not extract Mercari listing ID from: %s", page.url)

    return mercari_id


def _upload_photo(page, image_path: str):
    try:
        upload_btn = page.locator(
            "input[type='file'], button:has-text('Add photo'), "
            "label[for*='photo' i], div[data-testid*='photo' i]"
        ).first
        if upload_btn.get_attribute("type") == "file":
            upload_btn.set_input_files(image_path)
        else:
            with page.expect_file_chooser() as fc_info:
                upload_btn.click()
            fc = fc_info.value
            fc.set_files(image_path)
        _human_delay(2, 4)
        logger.info("Photo uploaded: %s", image_path)
    except Exception as e:
        logger.warning("Photo upload issue: %s", e)


def _select_category(page, item):
    card_type = item["card_type"]
    category_keywords = {
        "Pokemon": "Pokemon",
        "MTG": "Magic: The Gathering",
        "YuGiOh": "Yu-Gi-Oh",
        "Sports": "Sports Cards",
        "HotWheels": "Hot Wheels",
        "Other": "Collectibles",
    }
    keyword = category_keywords.get(card_type, "Collectibles")
    try:
        cat_btn = page.locator(
            "button:has-text('Category'), div[data-testid*='category' i], "
            "button:has-text('Select category')"
        ).first
        cat_btn.click()
        _human_delay(1, 2)
        search_box = page.locator("input[placeholder*='search' i]").first
        if search_box.is_visible():
            _type_human(search_box, keyword)
            _human_delay(1, 2)
            first_result = page.locator("li, div[role='option']").first
            first_result.click()
    except Exception as e:
        logger.warning("Category selection issue: %s", e)


def _select_condition(page, condition: str):
    mercari_condition = CONDITION_MAP.get(condition, "Good")
    try:
        cond_btn = page.locator(
            "button:has-text('Condition'), div[data-testid*='condition' i], "
            f"button:has-text('{mercari_condition}')"
        ).first
        cond_btn.click()
        _human_delay(0.5, 1.5)
        option = page.locator(
            f"li:has-text('{mercari_condition}'), div[role='option']:has-text('{mercari_condition}')"
        ).first
        if option.is_visible():
            option.click()
    except Exception as e:
        logger.warning("Condition selection issue: %s", e)


def _build_description(item) -> str:
    parts = [
        f"{item['card_name']} - {item['condition']}",
    ]
    if item["set_name"]:
        parts.append(f"Set: {item['set_name']}")
    if item["card_number"]:
        parts.append(f"Number: {item['card_number']}")
    if item["rarity"]:
        parts.append(f"Rarity: {item['rarity']}")
    parts += [
        "",
        "Ships in protective sleeve and top loader.",
        "Fast shipping! Check my other listings.",
    ]
    return "\n".join(parts)


def _extract_listing_id(page) -> str | None:
    url = page.url
    patterns = [
        r"/item/([A-Za-z0-9]+)",
        r"mercari\.com.*?([Mm]\d{10,})",
        r"listing[_-]?id[=:]([A-Za-z0-9]+)",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    try:
        confirm = page.locator("text=Your item is listed, text=Listed successfully").first
        if confirm.is_visible(timeout=3000):
            return "LISTED_" + str(int(time.time()))
    except Exception:
        pass
    return None


def delete_mercari_listing(mercari_id: str) -> bool:
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            logger.error("Cannot connect to Chrome: %s", e)
            return False

        context = browser.contexts[0]
        page = context.new_page()
        try:
            listing_url = f"https://www.mercari.com/us/item/{mercari_id}/"
            page.goto(listing_url, timeout=20000)
            _human_delay(2, 3)
            delete_btn = page.locator(
                "button:has-text('Delete'), button:has-text('Remove'), "
                "a:has-text('Delete listing')"
            ).first
            delete_btn.click()
            _human_delay(1, 2)
            confirm = page.locator("button:has-text('Yes'), button:has-text('Confirm')").first
            if confirm.is_visible(timeout=3000):
                confirm.click()
            _human_delay(2, 3)
            logger.info("Deleted Mercari listing: %s", mercari_id)
            return True
        except Exception as e:
            logger.error("Failed to delete Mercari listing %s: %s", mercari_id, e)
            return False
        finally:
            page.close()
