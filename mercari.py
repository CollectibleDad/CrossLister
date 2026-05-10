"""
Playwright automation for Mercari listings.
Connects to an existing Chrome instance on port 9222.
Start Chrome with: chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\ChromeData
Browser automation ported from the proven list_cards.py (data-testid selectors).
"""
import asyncio
import logging
import re
import time
from pathlib import Path

from playwright.async_api import async_playwright

import database as db

logger = logging.getLogger(__name__)

CDP_URL = "http://localhost:9222"
MERCARI_SELL_URL = "https://www.mercari.com/sell/"
SCREENSHOT_DIR = Path(__file__).parent / "screenshots"
FLOOR_PRICE = 1.22

# Maps our condition strings to Mercari's data-testid values
CONDITION_TESTID_MAP = {
    "Mint":              "ConditionLikeNew",
    "Near Mint":         "ConditionLikeNew",
    "Lightly Played":    "ConditionGood",
    "Moderately Played": "ConditionFair",
    "Heavily Played":    "ConditionPoor",
    "Poor":              "ConditionPoor",
}


# ── Public API (sync wrappers so main.py needs no changes) ─────────────────────

def list_on_mercari(item_id: int) -> str | None:
    return asyncio.run(_list_on_mercari_async(item_id))


def delete_mercari_listing(mercari_id: str) -> bool:
    return asyncio.run(_delete_mercari_listing_async(mercari_id))


# ── Internal async implementation ──────────────────────────────────────────────

async def _save_error_screenshot(page, label: str):
    try:
        SCREENSHOT_DIR.mkdir(exist_ok=True)
        path = SCREENSHOT_DIR / f"mercari_{label}_{int(time.time())}.png"
        await page.screenshot(path=str(path), full_page=True)
        logger.info("Screenshot saved: %s", path)
    except Exception as e:
        logger.warning("Could not save screenshot: %s", e)


async def _list_on_mercari_async(item_id: int) -> str | None:
    item = db.get_item_by_id(item_id)
    if not item:
        logger.error("Item %d not found", item_id)
        return None

    image_path = item["image_path"]
    if not image_path or not Path(image_path).exists():
        logger.error("Image not found for item %d: %s", item_id, image_path)
        return None

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            logger.error("Cannot connect to Chrome on port 9222. %s", e)
            logger.error("Start Chrome with: chrome.exe --remote-debugging-port=9222")
            return None

        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        try:
            return await _do_mercari_listing(page, item, image_path)
        except Exception as e:
            logger.error("Error listing item %d on Mercari: %s", item_id, e)
            await _save_error_screenshot(page, f"error_{item_id}")
            return None


async def _do_mercari_listing(page, item, image_path: str) -> str | None:
    logger.info("Listing '%s' on Mercari at $%.2f", item["title"], item["asking_price"])

    await page.goto(MERCARI_SELL_URL, wait_until="domcontentloaded", timeout=90000)
    await asyncio.sleep(3)

    # ── Photos ────────────────────────────────────────────────────────────────
    try:
        fi = page.locator('[data-testid="SellPhotoInput"]').first
        await fi.wait_for(state="attached", timeout=90000)
        await fi.set_input_files(image_path)
        await asyncio.sleep(3)
        logger.info("Photo uploaded.")
    except Exception as e:
        logger.warning("Photo upload: %s", e)

    # ── Title ─────────────────────────────────────────────────────────────────
    try:
        t = page.locator('[data-testid="Title"]').first
        await t.wait_for(state="visible", timeout=90000)
        await t.fill(item["title"][:80])
    except Exception as e:
        logger.warning("Title: %s", e)

    # ── Description ───────────────────────────────────────────────────────────
    desc = _build_description(item)
    try:
        d = page.locator('[data-testid="Description"]').first
        await d.wait_for(state="visible", timeout=90000)
        await d.fill(desc)
    except Exception as e:
        logger.warning("Description: %s", e)

    # ── Category ──────────────────────────────────────────────────────────────
    try:
        await _select_category(page)
        logger.info("Category set.")
    except Exception as e:
        logger.warning("Category: %s", e)

    # ── Brand ─────────────────────────────────────────────────────────────────
    try:
        brand = page.locator('[data-testid="Brand"]').first
        await brand.wait_for(state="visible", timeout=90000)
        await brand.fill("Pokemon")
        await asyncio.sleep(1)
        try:
            suggestion = page.locator('[data-testid="BrandSuggestion"], [role="option"]').first
            if await suggestion.is_visible(timeout=2000):
                await suggestion.click()
        except Exception:
            pass
    except Exception as e:
        logger.warning("Brand: %s", e)

    # ── Condition ─────────────────────────────────────────────────────────────
    condition_testid = CONDITION_TESTID_MAP.get(item["condition"], "ConditionLikeNew")
    try:
        cond = page.locator(f'[data-testid="{condition_testid}"]').first
        await cond.wait_for(state="visible", timeout=90000)
        await cond.click()
    except Exception as e:
        logger.warning("Condition: %s", e)

    # ── Shipping ──────────────────────────────────────────────────────────────
    try:
        ship = page.locator('[data-testid="MercariShipping"]').first
        if await ship.is_visible(timeout=5000):
            await ship.click()
            await asyncio.sleep(1)
        for txt in ["Envelope", "First Class", "USPS First Class", "First-Class"]:
            try:
                el = page.locator(f"text={txt}").first
                if await el.is_visible(timeout=2000):
                    await el.click()
                    await asyncio.sleep(0.5)
                    break
            except Exception:
                pass
        for sel in ['input[placeholder*="oz"]', 'input[placeholder*="weight" i]']:
            try:
                w = page.locator(sel).first
                if await w.is_visible(timeout=2000):
                    await w.triple_click()
                    await w.fill("1")
                    break
            except Exception:
                pass
    except Exception as e:
        logger.warning("Shipping: %s", e)

    # ── Price ─────────────────────────────────────────────────────────────────
    price_str = f"{item['asking_price']:.2f}"
    try:
        price_el = page.locator('[data-testid="Price"]').first
        await price_el.wait_for(state="visible", timeout=90000)
        await price_el.click()
        await price_el.press("Control+a")
        await price_el.press("Delete")
        await price_el.type(price_str)
        await asyncio.sleep(1.5)

        # Enable Smart Pricing if available
        for sel in [
            "text=Smart Pricing",
            "label:has-text('Smart Pricing')",
            '[data-testid="SmartPricing"]',
            '[aria-label*="Smart Pricing"]',
        ]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2000):
                    await el.click()
                    await asyncio.sleep(1)
                    # Set floor price
                    for fp_sel in [
                        '[data-testid="FloorPrice"]',
                        'input[placeholder*="Floor" i]',
                        'input[placeholder*="Minimum" i]',
                        'input[aria-label*="floor" i]',
                    ]:
                        try:
                            fp = page.locator(fp_sel).first
                            if await fp.is_visible(timeout=2000):
                                await fp.triple_click()
                                await fp.fill(f"{FLOOR_PRICE:.2f}")
                                break
                        except Exception:
                            pass
                    break
            except Exception:
                pass
    except Exception as e:
        logger.warning("Price: %s", e)

    await asyncio.sleep(1)

    # ── Submit ────────────────────────────────────────────────────────────────
    try:
        list_btn = page.locator('[data-testid="ListButton"]:not([disabled])').first
        await list_btn.wait_for(state="visible", timeout=90000)
        await list_btn.click()
        await asyncio.sleep(4)
        logger.info("Submit clicked.")
    except Exception as e:
        logger.warning("Submit (button may still be disabled — check required fields): %s", e)

    # Dismiss confirmation modal if any
    for txt in ["Confirm", "OK", "Done", "Got it"]:
        try:
            btn = page.locator(f"button:has-text('{txt}')").first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await asyncio.sleep(1)
                break
        except Exception:
            pass

    mercari_id = _extract_listing_id(page.url)
    if mercari_id:
        db.update_item(item["id"], {"mercari_id": mercari_id, "status": "active"})
        logger.info("Mercari listing created: %s", mercari_id)
    else:
        logger.warning("Could not extract Mercari listing ID from: %s", page.url)

    return mercari_id


async def _select_category(page):
    """Navigate Toys & Collectibles > Trading Cards > Single Cards."""
    await page.locator('[data-testid="CategoryL0"]').first.click()
    await asyncio.sleep(1.5)
    await page.locator('[data-testid="CategoryL0-option"]', has_text="Toys & Collectibles").first.click()
    await asyncio.sleep(1.5)

    await page.locator('[data-testid="CategoryL1"]').first.click()
    await asyncio.sleep(1.5)
    try:
        await page.locator('[data-testid="CategoryL1-option"]', has_text="Trading Cards").first.click()
    except Exception:
        opts = await page.locator('[data-testid="CategoryL1-option"]').all()
        for o in opts:
            txt = await o.inner_text()
            if "trading" in txt.lower() or "card" in txt.lower():
                await o.click()
                break
    await asyncio.sleep(1.5)

    try:
        await page.locator('[data-testid="CategoryL2"]').first.click()
        await asyncio.sleep(1.5)
        try:
            await page.locator('[data-testid="CategoryL2-option"]', has_text="Single Cards").first.click()
        except Exception:
            opts = await page.locator('[data-testid="CategoryL2-option"]').all()
            for o in opts:
                txt = await o.inner_text()
                if "single" in txt.lower() or "card" in txt.lower():
                    await o.click()
                    break
        await asyncio.sleep(1.5)
    except Exception:
        pass  # L2 may not exist for this category path


def _build_description(item) -> str:
    parts = [f"{item['card_name']} - {item['condition']}"]
    if item.get("set_name"):
        parts.append(f"Set: {item['set_name']}")
    if item.get("card_number"):
        parts.append(f"Number: {item['card_number']}")
    if item.get("rarity"):
        parts.append(f"Rarity: {item['rarity']}")
    parts += ["", "Ships in protective sleeve and top loader.", "Fast shipping! Check my other listings."]
    return "\n".join(parts)


def _extract_listing_id(url: str) -> str | None:
    patterns = [
        r"/item/([A-Za-z0-9]+)",
        r"mercari\.com.*?([Mm]\d{10,})",
        r"listing[_-]?id[=:]([A-Za-z0-9]+)",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


async def _delete_mercari_listing_async(mercari_id: str) -> bool:
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            logger.error("Cannot connect to Chrome: %s", e)
            return False

        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            listing_url = f"https://www.mercari.com/us/item/{mercari_id}/"
            await page.goto(listing_url, timeout=90000)
            await asyncio.sleep(2)
            delete_btn = page.locator(
                "button:has-text('Delete'), button:has-text('Remove'), a:has-text('Delete listing')"
            ).first
            await delete_btn.click()
            await asyncio.sleep(1)
            confirm = page.locator("button:has-text('Yes'), button:has-text('Confirm')").first
            if await confirm.is_visible(timeout=90000):
                await confirm.click()
            await asyncio.sleep(2)
            logger.info("Deleted Mercari listing: %s", mercari_id)
            return True
        except Exception as e:
            logger.error("Failed to delete Mercari listing %s: %s", mercari_id, e)
            return False
