"""
Playwright automation for Depop listings.
Connects to existing Chrome on port 9222.
Fixed price: $2.00 on Depop (best for quick turnover on lower-value cards).
Browser automation ported from the proven list_cards_depop.py.
"""
import asyncio
import logging
import re
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright, Page

import database as db
import failure_handler

_UTILS = Path(__file__).resolve().parent / "scripts" / "utils"
if str(_UTILS) not in sys.path:
    sys.path.insert(0, str(_UTILS))
from session_manager import ensure_depop_alive, ensure_browser_alive, is_browser_dead_error

logger = logging.getLogger(__name__)

CDP_URL = "http://localhost:9222"
DEPOP_CREATE_URL = "https://www.depop.com/products/create/"
DEPOP_FIXED_PRICE = "2"

LONG_RUN_MODE = False       # Set True by launcher for large/overnight batches
_MAX_RECOVERY_ATTEMPTS = 2  # Extra listing attempts after browser recovery
_GOTO_TIMEOUT_MS = 90_000

_MODAL_TEXT = "Help buyers discover your items"


# ── Public API (sync wrappers so main.py needs no changes) ─────────────────────

def list_on_depop(item_id: int) -> str | None:
    return asyncio.run(_list_on_depop_async(item_id))


def delete_depop_listing(depop_id: str) -> bool:
    return asyncio.run(_delete_depop_listing_async(depop_id))


# ── Internal async implementation ──────────────────────────────────────────────

async def _list_on_depop_async(item_id: int) -> str | None:
    item = db.get_item_by_id(item_id)
    if not item:
        logger.error("Item %d not found", item_id)
        return None

    image_path = item["image_path"]
    if not image_path or not Path(image_path).exists():
        logger.error("Image not found for item %d: %s", item_id, image_path)
        return None

    submitted = False
    final_url = ""
    recovery_count = 0

    async with async_playwright() as p:
        session = await ensure_depop_alive(p)
        if not session:
            return None

        for attempt in range(1, _MAX_RECOVERY_ATTEMPTS + 2):
            # Proactive alive check catches deaths that occurred between card listings
            live = await ensure_browser_alive(p, session)
            if not live:
                logger.error("[BROWSER RECOVERY] Cannot recover browser — aborting item %d", item_id)
                return None
            session = live

            try:
                submitted = await _do_depop_listing(session.page, item, image_path)
                try:
                    final_url = session.page.url
                except Exception:
                    pass
                break
            except Exception as e:
                if is_browser_dead_error(e) and recovery_count < _MAX_RECOVERY_ATTEMPTS:
                    recovery_count += 1
                    print(
                        f"[BROWSER RECOVERY] Browser/context died during listing "
                        f"(recovery {recovery_count}/{_MAX_RECOVERY_ATTEMPTS}) — {e}"
                    )
                    logger.warning(
                        "[BROWSER RECOVERY] Dead browser on item %d attempt %d: %s",
                        item_id, attempt, e,
                    )
                    session = await ensure_browser_alive(p, session)
                    if not session:
                        logger.error(
                            "[BROWSER RECOVERY] Unrecoverable — aborting item %d", item_id
                        )
                        return None
                    print(f"[PAGE RESTART] Restarting Depop listing for item {item_id} (attempt {attempt + 1})")
                    logger.info("[PAGE RESTART] Re-entering _do_depop_listing for item %d", item_id)
                    await asyncio.sleep(2)
                    continue
                logger.error("Error listing item %d on Depop: %s", item_id, e)
                try:
                    await failure_handler.save_failure_screenshot(
                        session.page, "depop", item.get("card_name", "")
                    )
                except Exception:
                    pass
                return None

        if not submitted:
            try:
                await failure_handler.save_failure_screenshot(
                    session.page, "depop", item.get("card_name", "")
                )
            except Exception:
                pass

    if submitted:
        depop_id = _extract_listing_id(final_url) or f"DEPOP_{int(time.time())}"
        db.update_item(item["id"], {"depop_id": depop_id, "status": "active"})
        logger.info("Depop listing created: %s", depop_id)
        return depop_id

    logger.warning("Depop listing not confirmed submitted for item %d", item_id)
    return None


async def _do_depop_listing(page: Page, item: dict, image_path: str) -> bool:
    logger.info("Listing '%s' on Depop at $%s", item["title"], DEPOP_FIXED_PRICE)
    title = item["title"][:60]
    print(f"[DEPOP POKEMON LIVE TITLE TO FILL] {title!r}")
    _goto_timeout = 180_000 if LONG_RUN_MODE else _GOTO_TIMEOUT_MS
    _idle_timeout = 15_000 if LONG_RUN_MODE else 8_000

    # Navigate to create form with retry until the form is actually loaded
    for attempt in range(4):
        try:
            await page.wait_for_load_state("networkidle", timeout=_idle_timeout)
        except Exception:
            pass
        try:
            await page.goto(DEPOP_CREATE_URL, wait_until="domcontentloaded", timeout=_goto_timeout)
        except Exception as e:
            if is_browser_dead_error(e):
                raise  # Propagate so outer recovery loop can handle it
            logger.warning("goto /create/ attempt %d: %s", attempt + 1, e)
            await asyncio.sleep(4)
            continue
        await asyncio.sleep(3)

        on_form = await page.evaluate(
            """() => !!(
                document.querySelector('input[type="file"]') ||
                document.querySelector('input[accept*="image"]') ||
                document.querySelector('[data-testid*="photo"]') ||
                document.querySelector('[data-testid*="upload"]') ||
                (document.title && document.title.toLowerCase().includes('sell'))
            )"""
        )
        if on_form:
            break
        logger.warning("Create form not detected (attempt %d) — URL: %s", attempt + 1, page.url)
        try:
            sell_btn = page.locator("a:has-text('Sell now'), button:has-text('Sell now')").first
            if await sell_btn.is_visible(timeout=3000):
                await sell_btn.click()
                await asyncio.sleep(3)
        except Exception:
            pass

    # ── Photo ─────────────────────────────────────────────────────────────────
    await close_blocking_modals(page)
    uploaded = False
    for sel in [
        'button:has-text("Add a photo")',
        'button:has-text("Add photos")',
        '[aria-label*="Add a photo" i]',
        '[aria-label*="Add photo" i]',
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=4000):
                async with page.expect_file_chooser(timeout=5000) as fc_info:
                    await btn.click()
                fc = await fc_info.value
                await fc.set_files(image_path)
                await asyncio.sleep(4)
                uploaded = True
                logger.info("Photo uploaded via button.")
                break
        except Exception:
            pass

    if not uploaded:
        for sel in ['input[type="file"]', 'input[accept*="image"]']:
            try:
                fi = page.locator(sel).first
                if await fi.count() > 0:
                    await fi.set_input_files(image_path)
                    await asyncio.sleep(4)
                    uploaded = True
                    logger.info("Photo uploaded via hidden input.")
                    break
            except Exception:
                pass

    if not uploaded:
        logger.warning("Could not upload photo to Depop")

    # Give Depop time to auto-detect details from the uploaded photo
    print("[DEPOP AUTO-DETECT WAIT]")
    logger.info("[DEPOP AUTO-DETECT WAIT] 5 s for Depop auto-detect after photo upload")
    await asyncio.sleep(5)
    await close_blocking_modals(page)

    # ── Title / Item name ─────────────────────────────────────────────────────
    await close_blocking_modals(page)
    filled = False
    for attempt in [
        lambda: page.get_by_label(re.compile(r"^name$", re.I)),
        lambda: page.get_by_label(re.compile(r"item name", re.I)),
        lambda: page.get_by_label(re.compile(r"title", re.I)),
        lambda: page.locator('input[name="name"]'),
        lambda: page.locator('input[name="productName"]'),
        lambda: page.locator('[data-testid="product-name-input"]'),
        lambda: page.locator('[data-testid="title-input"]'),
        lambda: page.locator('input[placeholder*="name" i]'),
        lambda: page.locator('input[placeholder*="title" i]'),
        lambda: page.locator('input[placeholder*="item" i]'),
        lambda: page.locator(
            'main input:not([type="file"]):not([type="hidden"]):not([type="number"])'
            ':not([type="submit"]):not([type="checkbox"]):not([type="radio"]):not([type="search"])'
        ),
        lambda: page.locator(
            'form input:not([type="file"]):not([type="hidden"]):not([type="number"])'
            ':not([type="submit"]):not([type="checkbox"]):not([type="radio"]):not([type="search"])'
        ),
    ]:
        try:
            el = attempt().first
            if await el.is_visible(timeout=3000):
                await el.click()
                await el.fill(title)
                filled = True
                break
        except Exception:
            pass
    if not filled:
        logger.warning("Could not fill Depop title")
    else:
        logger.info("Title filled.")

    # ── Description ───────────────────────────────────────────────────────────
    await close_blocking_modals(page)
    desc = _build_description(item)
    print(f"[DEPOP POKEMON LIVE DESCRIPTION TO FILL] {desc!r}")
    filled = False
    for attempt in [
        lambda: page.get_by_label(re.compile(r"description", re.I)),
        lambda: page.locator('textarea[name="description"]'),
        lambda: page.locator('textarea[placeholder*="description" i]'),
        lambda: page.locator('textarea[placeholder*="describe" i]'),
        lambda: page.locator('[data-testid="product-description-input"]'),
        lambda: page.locator('[data-testid="description-input"]'),
        lambda: page.locator('[aria-label*="description" i]'),
        lambda: page.locator('textarea'),
    ]:
        try:
            el = attempt().first
            if await el.is_visible(timeout=3000):
                await el.click()
                await el.fill(desc)
                filled = True
                break
        except Exception:
            pass
    if not filled:
        logger.warning("Could not fill Depop description")

    # Give Depop time to auto-detect category/brand from the description
    print("[DEPOP AUTO-DETECT WAIT]")
    logger.info("[DEPOP AUTO-DETECT WAIT] 4 s for Depop auto-detect after description")
    await asyncio.sleep(4)
    await close_blocking_modals(page)

    # ── Category ──────────────────────────────────────────────────────────────
    await close_blocking_modals(page)
    try:
        await _select_category(page)
    except Exception as e:
        logger.warning("Category: %s", e)

    # ── Brand ─────────────────────────────────────────────────────────────────
    await close_blocking_modals(page)
    try:
        await _fill_brand(page)
    except Exception as e:
        logger.warning("Brand: %s", e)

    # ── Condition ─────────────────────────────────────────────────────────────
    await close_blocking_modals(page)
    try:
        await _select_condition(page)
    except Exception as e:
        logger.warning("Condition: %s", e)

    # ── Price ─────────────────────────────────────────────────────────────────
    await close_blocking_modals(page)
    filled = False
    for attempt in [
        lambda: page.get_by_label(re.compile(r"price", re.I)),
        lambda: page.locator('input[name="price"]'),
        lambda: page.locator('[data-testid="price-input"]'),
        lambda: page.locator('input[placeholder="0.00"]'),
        lambda: page.locator('input[placeholder*="0.00"]'),
        lambda: page.locator('[aria-label*="price" i]'),
        lambda: page.locator('input[type="number"]'),
    ]:
        try:
            el = attempt().first
            if await el.is_visible(timeout=3000):
                await el.click()
                await el.fill(DEPOP_FIXED_PRICE)
                filled = True
                break
        except Exception:
            pass
    if not filled:
        logger.warning("Could not fill Depop price")

    await asyncio.sleep(1)

    # ── Shipping — pick smallest package size ─────────────────────────────────
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(1)
        selected = False
        for size_text in ["Extra Small", "Small", "XS", "S", "Letter", "Envelope"]:
            pattern = re.compile(re.escape(size_text), re.I)
            for loc in [
                page.locator("button").filter(has_text=pattern),
                page.locator("[role='radio']").filter(has_text=pattern),
                page.locator("[role='button']").filter(has_text=pattern),
                page.locator("label").filter(has_text=pattern),
                page.locator("li").filter(has_text=pattern),
            ]:
                try:
                    el = loc.first
                    if await el.is_visible(timeout=1500):
                        await el.click()
                        await asyncio.sleep(0.5)
                        await close_blocking_modals(page)
                        selected = True
                        logger.info("Package size: %s", size_text)
                        break
                except Exception:
                    pass
            if selected:
                break
        if not selected:
            # Coordinate-based fallback
            for term in ["Extra Small", "Small", "XS"]:
                if await _click_by_text(page, term):
                    await close_blocking_modals(page)
                    selected = True
                    logger.info("Package size set via coordinates: %s", term)
                    break
    except Exception as e:
        logger.warning("Shipping: %s", e)

    await asyncio.sleep(1)

    # ── Submit ────────────────────────────────────────────────────────────────
    await close_blocking_modals(page)

    await page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(0.5)

    # JS DOM click on the 'Post' button (proven to work with React synthetic events)
    clicked = await page.evaluate(
        """() => {
            const btn = Array.from(document.querySelectorAll('button'))
                .find(b => b.textContent.trim() === 'Post' && !b.disabled);
            if (!btn) return 'not_found';
            btn.scrollIntoView({block: 'center'});
            btn.click();
            return 'clicked';
        }"""
    )
    logger.info("Post button DOM click: %s", clicked)

    submitted = False
    if clicked == "clicked":
        await asyncio.sleep(6)
        content = await page.content()
        if "listed" in content.lower() or "/create/" not in page.url:
            submitted = True
            logger.info("Depop listing submitted.")
        else:
            logger.warning("DOM click fired but success page not detected")
    else:
        logger.warning("Post button not found on page")

    # Dismiss post-submit modal
    for txt in ["Done", "OK", "Got it", "Close", "Continue selling"]:
        try:
            btn = page.locator(f'button:has-text("{txt}")').first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                await asyncio.sleep(1)
                break
        except Exception:
            pass

    return submitted


async def close_blocking_modals(page: Page) -> bool:
    """
    Detect and close the 'Help buyers discover your items' popup and any other
    blocking dialog.  Returns True when clear to proceed.  If the modal cannot
    be dismissed, saves a screenshot and returns False.
    """
    # Check for the specific blocking popup by text content
    try:
        modal_visible = await page.evaluate(
            """(text) => Array.from(document.querySelectorAll('*')).some(el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && el.textContent.includes(text);
            })""",
            _MODAL_TEXT,
        )
    except Exception:
        modal_visible = False

    # Also catch any generic dialog/alertdialog that may be blocking
    if not modal_visible:
        for loc in [
            page.locator('[role="dialog"]'),
            page.locator('[role="alertdialog"]'),
        ]:
            try:
                if await loc.first.is_visible(timeout=400):
                    modal_visible = True
                    break
            except Exception:
                pass

    if not modal_visible:
        return True

    logger.info("Blocking modal detected — attempting to close.")

    # Close by clicking the X button (preferred) or any dialog close button
    closed = False
    for sel in [
        '[role="dialog"] button[aria-label="Close"]',
        '[role="dialog"] button[aria-label="close"]',
        '[role="alertdialog"] button[aria-label="Close"]',
        'button[aria-label="Close"]',
        'button[aria-label="close"]',
        'button:has-text("×")',
        'button:has-text("✕")',
        '[role="dialog"] button',
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=800):
                await btn.click()
                await asyncio.sleep(1)
                closed = True
                logger.info("Closed blocking modal via selector: %s", sel)
                break
        except Exception:
            pass

    if not closed:
        for sym in ["×", "✕"]:
            if await _click_by_text(page, sym):
                closed = True
                await asyncio.sleep(1)
                logger.info("Closed blocking modal via text symbol: %s", sym)
                break

    # Verify the specific popup text is gone
    try:
        still_visible = await page.evaluate(
            """(text) => Array.from(document.querySelectorAll('*')).some(el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0 && el.textContent.includes(text);
            })""",
            _MODAL_TEXT,
        )
    except Exception:
        still_visible = False

    if still_visible:
        logger.warning("'%s' modal persists after close attempt", _MODAL_TEXT)
        await failure_handler.save_failure_screenshot(page, "depop_modal", "blocking_modal")
        return False

    return True


async def _click_by_text(page: Page, search: str) -> bool:
    """Find the smallest visible element containing search text and mouse-click it."""
    coords = await page.evaluate(
        """([search]) => {
            const lower = search.toLowerCase();
            const candidates = Array.from(document.querySelectorAll('*'))
                .filter(el => {
                    if (!el.textContent.toLowerCase().includes(lower)) return false;
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && r.width < 600 && r.height < 200;
                })
                .sort((a, b) => a.textContent.trim().length - b.textContent.trim().length);
            if (candidates.length === 0) return null;
            const r = candidates[0].getBoundingClientRect();
            return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
        }""",
        [search],
    )
    if coords:
        await page.mouse.click(coords["x"], coords["y"])
        await asyncio.sleep(1)
        return True
    return False


async def _select_category(page: Page):
    """Click the 'Trading cards' category chip — skip if already selected."""
    await close_blocking_modals(page)

    # Check if Depop already auto-selected the category
    try:
        already_selected = await page.evaluate(
            """() => {
                const pat = /trading cards/i;
                return Array.from(document.querySelectorAll(
                    'button, [role="button"], [role="option"], li, a'
                )).some(el => {
                    if (!pat.test(el.textContent.trim())) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return false;
                    return (
                        el.getAttribute('aria-selected') === 'true' ||
                        el.getAttribute('aria-pressed') === 'true' ||
                        el.getAttribute('data-selected') === 'true' ||
                        el.classList.contains('selected') ||
                        el.classList.contains('active')
                    );
                });
            }"""
        )
        if already_selected:
            logger.info("Category already set to Trading cards — skipping.")
            return
    except Exception:
        pass

    await asyncio.sleep(4)  # wait for Depop to analyse description and show suggestion
    await close_blocking_modals(page)
    pattern = re.compile(r"trading cards", re.I)

    for loc in [
        page.locator("button").filter(has_text=pattern),
        page.locator("[role='button']").filter(has_text=pattern),
        page.locator("li").filter(has_text=pattern),
        page.locator("a").filter(has_text=pattern),
        page.locator("p").filter(has_text=pattern),
    ]:
        try:
            el = loc.first
            if await el.is_visible(timeout=2000):
                await el.click()
                await asyncio.sleep(1)
                await close_blocking_modals(page)
                logger.info("Category chip clicked.")
                return
        except Exception:
            pass

    for term in ["Trading cards", "Everything else / Trading cards"]:
        if await _click_by_text(page, term):
            await close_blocking_modals(page)
            logger.info("Category chip clicked via coordinates.")
            return

    logger.warning("Could not click Depop category suggestion")


async def _select_condition(page: Page):
    """Open the condition picker and select 'Like new'."""
    await close_blocking_modals(page)
    for loc in [
        page.get_by_role("button", name=re.compile(r"condition", re.I)),
        page.locator("button").filter(has_text=re.compile(r"^condition$", re.I)),
        page.get_by_label(re.compile(r"condition", re.I)),
    ]:
        try:
            el = loc.first
            if await el.is_visible(timeout=2000):
                await el.click()
                await asyncio.sleep(1)
                # Dropdown is now open — clear any modal before selecting
                await close_blocking_modals(page)
                break
        except Exception:
            pass

    pattern = re.compile(r"like new", re.I)
    for loc in [
        page.locator("button").filter(has_text=pattern),
        page.locator("[role='button']").filter(has_text=pattern),
        page.locator("[role='radio']").filter(has_text=pattern),
        page.locator("label").filter(has_text=pattern),
        page.locator("li").filter(has_text=pattern),
        page.locator("p").filter(has_text=pattern),
    ]:
        try:
            el = loc.first
            if await el.is_visible(timeout=2000):
                await el.click()
                await asyncio.sleep(0.5)
                await close_blocking_modals(page)
                logger.info("Condition set to Like new.")
                return
        except Exception:
            pass

    if await _click_by_text(page, "Like new"):
        await close_blocking_modals(page)
        logger.info("Condition set via coordinates.")
        return

    logger.warning("Could not set Depop condition")


async def _fill_brand(page: Page):
    """Fill the brand field with 'Other' if it is empty (skip if already populated)."""
    brand_locators = [
        page.get_by_label(re.compile(r"^brand$", re.I)),
        page.locator('input[name="brand"]'),
        page.locator('[data-testid="brand-input"]'),
        page.locator('[aria-label*="brand" i]'),
        page.locator('input[placeholder*="brand" i]'),
    ]

    for loc_fn in brand_locators:
        try:
            el = loc_fn if not callable(loc_fn) else loc_fn
            field = el.first
            if not await field.is_visible(timeout=2000):
                continue
            current_val = await field.input_value()
            if current_val and current_val.strip():
                logger.info("Brand already populated (%s) — skipping.", current_val.strip())
                return
            await field.click()
            await field.fill("Other")
            await asyncio.sleep(0.5)
            await close_blocking_modals(page)
            # Accept first autocomplete suggestion if one appears
            for suggestion_loc in [
                page.locator('[role="option"]').first,
                page.locator('[role="listbox"] li').first,
            ]:
                try:
                    if await suggestion_loc.is_visible(timeout=1000):
                        await suggestion_loc.click()
                        await asyncio.sleep(0.3)
                        break
                except Exception:
                    pass
            logger.info("Brand set to Other.")
            return
        except Exception:
            pass

    logger.warning("Could not find or fill Depop brand field")


def _build_description(item: dict) -> str:
    parts = [f"{item['card_name']} - {item['condition']}"]
    if item.get("set_name"):
        parts.append(f"Set: {item['set_name']}")
    if item.get("card_number"):
        parts.append(f"Card #: {item['card_number']}")
    if item.get("rarity"):
        parts.append(f"Rarity: {item['rarity']}")
    parts += ["", "Ships in protective sleeve. Fast shipping!"]
    return "\n".join(parts)


def _extract_listing_id(url: str) -> str | None:
    patterns = [
        r"depop\.com/products/([A-Za-z0-9_-]+)",
        r"/([A-Za-z0-9]{8,})/?\s*$",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


async def _delete_depop_listing_async(depop_id: str) -> bool:
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            logger.error("Cannot connect to Chrome: %s", e)
            return False

        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            url = f"https://www.depop.com/products/{depop_id}/"
            await page.goto(url, timeout=90000)
            await asyncio.sleep(2)
            delete_btn = page.locator(
                "button:has-text('Delete'), a:has-text('Delete'), button[aria-label*='delete' i]"
            ).first
            await delete_btn.click()
            await asyncio.sleep(1)
            confirm = page.locator("button:has-text('Yes'), button:has-text('Delete item')").first
            if await confirm.is_visible(timeout=90000):
                await confirm.click()
            await asyncio.sleep(2)
            logger.info("Deleted Depop listing: %s", depop_id)
            return True
        except Exception as e:
            logger.error("Failed to delete Depop listing %s: %s", depop_id, e)
            return False
