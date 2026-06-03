"""
Playwright automation for Mercari listings.
Connects to an existing Chrome instance on port 9222.
Start Chrome with: chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\ChromeData
Browser automation ported from the proven list_cards.py (data-testid selectors).
"""
import asyncio
import logging
import re
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

import database as db
import failure_handler

_UTILS = Path(__file__).resolve().parent / "scripts" / "utils"
if str(_UTILS) not in sys.path:
    sys.path.insert(0, str(_UTILS))
from session_manager import ensure_mercari_alive

logger = logging.getLogger(__name__)

CDP_URL = "http://localhost:9222"
MERCARI_SELL_URL = "https://www.mercari.com/sell/"
FLOOR_PRICE = 1.22

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
        session = await ensure_mercari_alive(p)
        if not session:
            return None
        try:
            return await _do_mercari_listing(session.page, item, image_path)
        except Exception as e:
            logger.error("Error listing item %d on Mercari: %s", item_id, e)
            await failure_handler.save_failure_screenshot(session.page, "mercari", item.get("card_name", ""))
            return None


async def _do_mercari_listing(page, item, image_path: str) -> str | None:
    logger.info("Listing '%s' on Mercari at $%.2f", item["title"], item["asking_price"])
    print(f"[POKEMON LIVE RAW FILENAME] {item.get('filename', 'N/A')}")
    print(f"[POKEMON LIVE PARSED DATA] card_name={item.get('card_name')!r} | set_name={item.get('set_name')!r} | rarity={item.get('rarity')!r} | condition={item.get('condition')!r} | card_type={item.get('card_type')!r}")

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
                    await w.click(click_count=3)
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
                                await fp.click(click_count=3)
                                await fp.fill(f"{FLOOR_PRICE:.2f}")
                                break
                        except Exception:
                            pass
                    break
            except Exception:
                pass
    except Exception as e:
        logger.warning("Price: %s", e)

    # ── Wait for form to settle after category/brand/condition ────────────────
    await asyncio.sleep(2)

    # ── Title (filled LAST to avoid form auto-refresh overwrite) ──────────────
    pokemon_title = item["title"][:80]
    pokemon_desc = _build_description(item)
    print(f"[POKEMON LIVE TITLE TO FILL] {pokemon_title!r}")
    print(f"[POKEMON LIVE DESCRIPTION TO FILL] {pokemon_desc!r}")

    title_before = ""
    try:
        t = page.locator('[data-testid="Title"]').first
        await t.wait_for(state="visible", timeout=90000)
        title_before = await t.input_value()
        print(f"[MERCARI TITLE BEFORE FILL] {title_before!r}")
        await t.triple_click()
        await t.fill(pokemon_title)
        await asyncio.sleep(0.5)
        title_after = await t.input_value()
        print(f"[MERCARI TITLE AFTER FILL] {title_after!r}")
    except Exception as e:
        logger.warning("Title: %s", e)

    # ── Description (filled LAST) ─────────────────────────────────────────────
    try:
        d = page.locator('[data-testid="Description"]').first
        await d.wait_for(state="visible", timeout=90000)
        await d.triple_click()
        await d.fill(pokemon_desc)
        await asyncio.sleep(0.5)
        desc_after = await d.input_value()
        print(f"[MERCARI DESCRIPTION AFTER FILL] {desc_after!r}")
    except Exception as e:
        logger.warning("Description: %s", e)

    await asyncio.sleep(1)

    # ── Pre-submit verification ───────────────────────────────────────────────
    title_before_submit = ""
    desc_before_submit = ""
    try:
        title_before_submit = await page.locator('[data-testid="Title"]').first.input_value()
    except Exception:
        pass
    try:
        desc_before_submit = await page.locator('[data-testid="Description"]').first.input_value()
    except Exception:
        pass
    print(f"[MERCARI TITLE BEFORE SUBMIT] {title_before_submit!r}")
    print(f"[MERCARI DESCRIPTION BEFORE SUBMIT] {desc_before_submit!r}")

    desc_word_count = len(desc_before_submit.split())
    needs_refill = desc_word_count < 5 or title_before_submit.strip() in ("", "Near Mint")
    if needs_refill:
        logger.warning(
            "Pre-submit fields stale (title=%r, desc_words=%d) — refilling",
            title_before_submit[:60], desc_word_count,
        )
        try:
            t = page.locator('[data-testid="Title"]').first
            await t.triple_click()
            await t.fill(pokemon_title)
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning("Refill title: %s", e)
        try:
            d = page.locator('[data-testid="Description"]').first
            await d.triple_click()
            await d.fill(pokemon_desc)
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning("Refill description: %s", e)
        try:
            desc_before_submit = await page.locator('[data-testid="Description"]').first.input_value()
        except Exception:
            desc_before_submit = pokemon_desc
        desc_word_count = len(desc_before_submit.split())
        print(f"[MERCARI TITLE BEFORE SUBMIT] {await page.locator('[data-testid=\"Title\"]').first.input_value()!r} (after refill)")
        print(f"[MERCARI DESCRIPTION BEFORE SUBMIT] {desc_before_submit!r} (after refill)")

    if desc_word_count < 5:
        print("[MERCARI DESCRIPTION TOO SHORT - NOT LISTED]")
        logger.error("[MERCARI DESCRIPTION TOO SHORT - NOT LISTED] desc=%r", desc_before_submit)
        return None

    # ── Submit ────────────────────────────────────────────────────────────────
    try:
        list_btn = page.locator('[data-testid="ListButton"]:not([disabled])').first
        await list_btn.wait_for(state="visible", timeout=90000)
        await list_btn.click()
        logger.info("Submit clicked.")
    except Exception as e:
        logger.warning("Submit (button may still be disabled — check required fields): %s", e)

    # Wait for Mercari to redirect to the actual listing page
    try:
        await page.wait_for_url(lambda url: "/item/" in url, timeout=20000)
    except Exception:
        pass

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

    final_url = page.url
    if "/item/" not in final_url:
        logger.warning("Mercari did not reach listing page — submit failed. URL: %s", final_url)
        await failure_handler.save_failure_screenshot(page, "mercari", item.get("card_name", ""))
        return None

    mercari_id = _extract_listing_id(final_url)
    if mercari_id:
        db.update_item(item["id"], {"mercari_id": mercari_id, "status": "active"})
        logger.info("Mercari listing created: %s", mercari_id)
    else:
        logger.warning("Could not extract Mercari listing ID from: %s", final_url)
        await failure_handler.save_failure_screenshot(page, "mercari", item.get("card_name", ""))

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
    condition = item.get("condition", "Near Mint")
    if item.get("card_type") == "Pokemon":
        title = item.get("title", "").strip()
        return f"{title} Pokemon trading card. Condition: {condition}."
    parts = [f"{item['card_name']} - {condition}"]
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
