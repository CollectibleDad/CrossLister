"""
Prices cards using PriceCharting.com (primary, 50%) and eBay sold listings (50%).

Flow per card:
  1. Build a PriceCharting search URL and find the best matching product
  2. Extract the ungraded market price (and recent sold prices if available)
  3. Search eBay completed/sold listings for the same card (up to 5 prices)
  4. Weighted average: 50% PriceCharting + 50% eBay sold, floor $1.22
  5. If one source fails the other carries full weight
  6. If all sources fail, use rarity-based defaults
"""
import logging
import re
import time
import random
from urllib.parse import urlencode, quote_plus

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

FLOOR_PRICE      = 1.22
PC_WEIGHT        = 0.50   # PriceCharting ungraded market price
EBAY_SOLD_WEIGHT = 0.50   # eBay completed/sold listings
MAX_SOLD         = 5
NAV_TIMEOUT      = 30000  # ms — page navigation / load waits
ELEM_TIMEOUT     = 8000   # ms — individual element waits

RARITY_DEFAULT_PRICES = {
    "common":     1.49,
    "uncommon":   1.99,
    "rare":       3.99,
    "holo rare":  5.99,
    "ultra rare": 9.99,
}

_BROWSER_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-extensions",
]
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ── Query builders ─────────────────────────────────────────────────────────────

def _build_query(card_info: dict) -> str:
    """eBay search query."""
    card_type = card_info.get("card_type", "Other")
    if card_type == "Sports":
        return _query_sports(card_info)
    if card_type == "Pokemon":
        return _query_pokemon(card_info)
    if card_type == "OnePiece":
        return _query_one_piece(card_info)
    if card_type == "Lorcana":
        return _query_lorcana(card_info)
    if card_type == "MTG":
        return _query_mtg(card_info)
    parts = [
        card_info.get("card_name"),
        card_info.get("set_name"),
        card_info.get("card_number"),
        card_info.get("rarity"),
    ]
    return " ".join(p for p in parts if p)


def _build_pc_query(card_info: dict) -> str:
    """PriceCharting-optimised search query."""
    card_type = card_info.get("card_type", "Other")
    name   = (card_info.get("card_name")   or "").strip()
    number = (card_info.get("card_number") or "").strip()

    if card_type == "Pokemon":
        # e.g. "Toedscool 017/142 Pokemon"
        return " ".join(p for p in [name, number, "Pokemon"] if p)
    if card_type == "Sports":
        # e.g. "Patrick Mahomes 2020 Panini"
        year  = str(card_info.get("year", "") or "").strip()
        brand = (card_info.get("brand") or "").strip()
        return " ".join(p for p in [name, year, brand] if p)
    if card_type == "OnePiece":
        # e.g. "Monkey D. Luffy OP01-001 One Piece"
        return " ".join(p for p in [name, number, "One Piece"] if p)
    if card_type == "Lorcana":
        set_name = (card_info.get("set_name") or "").strip()
        return " ".join(p for p in [name, number, set_name, "Lorcana"] if p)
    if card_type == "MTG":
        set_name = (card_info.get("set_name") or "").strip()
        return " ".join(p for p in [name, set_name, "MTG"] if p)
    return _build_query(card_info)


def _query_sports(card_info: dict) -> str:
    name   = (card_info.get("card_name") or "").strip()
    year   = card_info.get("year", "")
    brand  = (card_info.get("brand") or "").strip()
    number = (card_info.get("card_number") or "").strip()
    sport  = (card_info.get("sport") or "").lower().strip()
    rookie = (card_info.get("rookie_card") or "").lower() == "yes"

    if year and year != "unknown":
        if sport == "basketball":
            y = int(year)
            year_str = f"{y}-{str(y + 1)[-2:]}"
        else:
            year_str = year
    else:
        year_str = ""

    num_str = f"#{number}" if number else ""
    parts = [name, year_str, brand, num_str]
    if sport in ("football", "baseball") and rookie:
        parts.append("RC")
    return " ".join(p for p in parts if p)


def _query_pokemon(card_info: dict) -> str:
    name   = (card_info.get("card_name") or "").strip()
    number = (card_info.get("card_number") or "").strip()
    abbrev = (card_info.get("set_abbreviation") or card_info.get("set_name") or "").strip()
    return " ".join(p for p in [name, number, abbrev, "Pokemon"] if p)


def _query_one_piece(card_info: dict) -> str:
    name   = (card_info.get("card_name") or "").strip()
    number = (card_info.get("card_number") or "").strip()
    rarity = (card_info.get("rarity") or "").strip()
    return " ".join(p for p in [name, number, rarity, "One Piece"] if p)


def _query_lorcana(card_info: dict) -> str:
    name     = (card_info.get("card_name") or "").strip()
    number   = (card_info.get("card_number") or "").strip()
    set_name = (card_info.get("set_name") or "").strip()
    return " ".join(p for p in [name, number, set_name, "Lorcana"] if p)


def _query_mtg(card_info: dict) -> str:
    name     = (card_info.get("card_name") or "").strip()
    set_name = (card_info.get("set_name") or "").strip()
    return " ".join(p for p in [name, set_name, "MTG"] if p)


# ── Playwright browser ─────────────────────────────────────────────────────────

def _launch_browser(p):
    browser = p.chromium.launch(headless=True, args=_BROWSER_ARGS)
    context = browser.new_context(
        user_agent=_USER_AGENT,
        viewport={"width": 1280, "height": 900},
        locale="en-US",
    )
    # Suppress the webdriver flag so sites don't detect automation
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return browser, context


def _rand_sleep(lo: float = 1.0, hi: float = 2.5):
    time.sleep(random.uniform(lo, hi))


def _parse_price(text: str) -> float | None:
    text = text.replace(",", "").strip()
    m = re.search(r"\$?(\d+(?:\.\d{2})?)", text)
    if m:
        val = float(m.group(1))
        return val if 0.01 < val < 5000 else None
    return None


# ── PriceCharting scraper ──────────────────────────────────────────────────────

def _fetch_pricecharting(query: str) -> tuple[float | None, str, list[float]]:
    """
    Search PriceCharting, navigate to the best matching product, extract the
    ungraded market price and recent sold prices.
    Returns (ungraded_price, url_used, recent_sold_prices).
    Prints the URL and price for transparency.
    """
    search_url = f"https://www.pricecharting.com/search-products?q={quote_plus(query)}"
    print(f"[PriceCharting] Search URL: {search_url}")

    try:
        with sync_playwright() as p:
            browser, context = _launch_browser(p)
            page = context.new_page()

            page.goto(search_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            _rand_sleep(1, 2)

            # PriceCharting sometimes redirects straight to the product page
            if "/game/" in page.url and page.url.rstrip("/") != search_url.rstrip("/"):
                product_url = page.url
            else:
                product_url = _pick_best_pc_result(page, query)

            if product_url is None:
                print(f"[PriceCharting] No match found for: {query!r}")
                browser.close()
                return None, search_url, []

            print(f"[PriceCharting] Product URL: {product_url}")
            if page.url != product_url:
                page.goto(product_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                _rand_sleep(1, 2)

            price   = _extract_pc_ungraded_price(page)
            pc_sold = _extract_pc_recent_sales(page)
            browser.close()

        if price is not None:
            print(f"[PriceCharting] Ungraded market price: ${price:.2f}")
            if pc_sold:
                print(f"[PriceCharting] Recent sold: {[f'${v:.2f}' for v in pc_sold]}")
        else:
            print("[PriceCharting] Could not extract ungraded price")

        return price, product_url, pc_sold

    except Exception as e:
        logger.error("PriceCharting session failed: %s", e)
        return None, search_url, []


def _pick_best_pc_result(page, query: str) -> str | None:
    """
    From a PriceCharting search results page, return the URL of the best
    matching product (scored by query-term overlap in the result title).
    """
    try:
        links = page.locator("table#games_table td:first-child a").all()
        if not links:
            links = page.locator("a[href*='/game/']").all()
        if not links:
            return None

        query_terms = [t.lower() for t in query.split() if len(t) > 1]
        best_url, best_score = None, -1

        for link in links[:10]:
            try:
                href  = link.get_attribute("href") or ""
                title = (link.inner_text() or "").lower()
                if not href or "/game/" not in href:
                    continue
                score = sum(1 for t in query_terms if t in title)
                if score > best_score:
                    best_score = score
                    best_url   = href
            except Exception:
                continue

        if best_url and not best_url.startswith("http"):
            best_url = "https://www.pricecharting.com" + best_url
        return best_url

    except Exception as e:
        logger.warning("Error picking PriceCharting result: %s", e)
        return None


def _extract_pc_ungraded_price(page) -> float | None:
    """Extract the ungraded market price from a PriceCharting product page."""
    selectors = [
        "#used_price .price",
        "#used_price span.js-price",
        "#used_price",
        "span.js-price",          # first price on page is typically ungraded
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                val = _parse_price(el.inner_text())
                if val:
                    return val
        except Exception:
            continue
    return None


def _extract_pc_recent_sales(page) -> list[float]:
    """Extract recent sold prices from a PriceCharting product page (best-effort)."""
    prices = []
    try:
        for sel in [
            "table#sold_table .completed-auction-price",
            "#sold_table td.price",
            ".completed-auction-price",
        ]:
            els = page.locator(sel).all()
            for el in els[:10]:
                try:
                    val = _parse_price(el.inner_text())
                    if val:
                        prices.append(val)
                except Exception:
                    continue
            if prices:
                break
    except Exception:
        pass
    return sorted(prices)[:5]


# ── eBay sold scraper ──────────────────────────────────────────────────────────

def _extract_prices_from_page(page) -> list[float]:
    """
    Extract sold prices from the current eBay page.
    Tries .POSITIVE (green price spans) first, falls back to .s-item__price.
    """
    prices = []
    for selector in [".POSITIVE", ".s-item__price"]:
        try:
            elements = page.locator(selector).all()
            if not elements:
                continue
            for el in elements:
                text = el.inner_text()
                if " to " in text.lower():
                    for raw in re.findall(r"\$[\d,]+\.?\d*", text):
                        val = _parse_price(raw)
                        if val:
                            prices.append(val)
                else:
                    val = _parse_price(text)
                    if val:
                        prices.append(val)
            if prices:
                break
        except Exception:
            continue
    return prices


def _apply_sidebar_filter(page, label_text: str, url_param: str) -> bool:
    """Check a sidebar filter checkbox on eBay, or fall back to URL parameter."""
    checkbox_selectors = [
        f"input[type='checkbox'][aria-label*='{label_text}' i]",
        f"label:has-text('{label_text}') input[type='checkbox']",
        f"li:has-text('{label_text}') input[type='checkbox']",
        f"span:has-text('{label_text}') ~ input[type='checkbox']",
    ]
    for sel in checkbox_selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=3000):
                if not el.is_checked():
                    el.click()
                    page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
                    _rand_sleep(1, 2)
                logger.info("Sidebar filter applied: %s", label_text)
                return True
        except Exception:
            continue

    link_selectors = [
        f"a:has-text('{label_text}')",
        f"[data-sp*='LH'] span:has-text('{label_text}')",
    ]
    for sel in link_selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                el.click()
                page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
                _rand_sleep(1, 2)
                logger.info("Sidebar filter applied via link: %s", label_text)
                return True
        except Exception:
            continue

    current_url = page.url
    if url_param not in current_url:
        sep = "&" if "?" in current_url else "?"
        page.goto(current_url + sep + url_param + "=1",
                  wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        _rand_sleep(1, 2)
        logger.info("Sidebar filter applied via URL param: %s", url_param)
    return False


def _fetch_sold_prices(page, query: str) -> list[float] | None:
    """Navigate eBay, apply Completed + Sold filters, return green sold prices."""
    try:
        page.goto("https://www.ebay.com", wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        _rand_sleep(1, 2)

        search_box = page.locator("#gh-ac, input[name='_nkw'][type='text']").first
        search_box.wait_for(state="visible", timeout=ELEM_TIMEOUT)
        search_box.fill(query)
        _rand_sleep(0.3, 0.7)
        search_box.press("Enter")
        page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
        _rand_sleep(1.5, 2.5)
        logger.info("eBay search submitted: %s", query)

        _apply_sidebar_filter(page, "Completed items", "LH_Complete")
        _apply_sidebar_filter(page, "Sold items", "LH_Sold")

        prices = _extract_prices_from_page(page)
        result = sorted(prices)[:MAX_SOLD]
        logger.info("eBay sold prices found (%d): %s", len(result), result)
        return result

    except PWTimeout as e:
        logger.warning("Timeout fetching eBay sold prices for '%s': %s", query, e)
        return None
    except Exception as e:
        logger.warning("Error fetching eBay sold prices for '%s': %s", query, e)
        return None


def _fetch_ebay_sold_only(query: str) -> list[float] | None:
    """Launch one headless browser and return eBay sold prices."""
    try:
        with sync_playwright() as p:
            browser, context = _launch_browser(p)
            page = context.new_page()
            sold = _fetch_sold_prices(page, query)
            browser.close()
            return sold
    except Exception as e:
        logger.error("eBay sold session failed: %s", e)
        return None


# ── Price calculation ──────────────────────────────────────────────────────────

def _get_rarity_default(card_info: dict) -> float:
    rarity = (card_info.get("rarity") or "").lower().strip()
    for key, price in RARITY_DEFAULT_PRICES.items():
        if key in rarity:
            return price
    return RARITY_DEFAULT_PRICES["common"]


def get_smart_price(card_info: dict) -> dict:
    query    = _build_query(card_info)
    pc_query = _build_pc_query(card_info)

    if not query.strip():
        logger.warning("Empty query for %s — using rarity default", card_info.get("card_name"))
        return _default_result(_get_rarity_default(card_info), "no_query")

    logger.info("Pricing '%s' — PC query: %s | eBay query: %s",
                card_info.get("card_name"), pc_query, query)

    # Primary: PriceCharting (50%)
    pc_price, pc_url, pc_sold = _fetch_pricecharting(pc_query)

    # Secondary: eBay sold (50%)
    sold     = _fetch_ebay_sold_only(query) or []
    ebay_avg = (sum(sold) / len(sold)) if sold else None

    if pc_price is not None and ebay_avg is not None:
        price  = pc_price * PC_WEIGHT + ebay_avg * EBAY_SOLD_WEIGHT
        source = "pricecharting+ebay"
    elif pc_price is not None:
        price  = pc_price
        source = "pricecharting"
    elif ebay_avg is not None:
        price  = ebay_avg
        source = "ebay_sold"
    else:
        default = _get_rarity_default(card_info)
        logger.warning(
            "All sources failed — rarity default $%.2f (rarity=%r)",
            default, card_info.get("rarity"),
        )
        return _default_result(default, "rarity_default")

    price = max(price, FLOOR_PRICE)
    price = round(price, 2)

    logger.info(
        "Price calc — pc=$%s  ebay_sold=$%s → $%.2f  (source=%s)",
        f"{pc_price:.2f}" if pc_price else "N/A",
        f"{ebay_avg:.2f}" if ebay_avg else "N/A",
        price,
        source,
    )

    return {
        "asking_price":  price,
        "pc_price":      pc_price,
        "pc_url":        pc_url,
        "pc_sold":       pc_sold,
        "sold_prices":   sold,
        "active_prices": [],
        "sold_avg":      round(ebay_avg, 2) if ebay_avg else None,
        "active_avg":    None,
        "data_points":   (1 if pc_price else 0) + len(sold),
        "price_source":  source,
    }


def _default_result(price: float, source: str) -> dict:
    return {
        "asking_price":  price,
        "pc_price":      None,
        "pc_url":        None,
        "pc_sold":       [],
        "sold_prices":   [],
        "active_prices": [],
        "sold_avg":      None,
        "active_avg":    None,
        "data_points":   0,
        "price_source":  source,
    }
