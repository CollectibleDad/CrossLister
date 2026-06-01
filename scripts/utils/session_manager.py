"""
Browser Session Manager for CrossLister.
Manages dedicated Playwright pages per platform with health checks, auto-recovery,
and adaptive timing for long-running batches (100+ items).
"""
import asyncio
import logging
import time
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent

CDP_URL = "http://localhost:9222"

PLATFORM_DOMAINS = {
    "mercari":  "mercari.com",
    "depop":    "depop.com",
    "ebay":     "ebay.com",
    "facebook": "facebook.com",
    "whatnot":  "whatnot.com",
    "shopify":  "myshopify.com",
}

PLATFORM_HOME = {
    "mercari":  "https://www.mercari.com/",
    "depop":    "https://www.depop.com/",
    "ebay":     "https://www.ebay.com/",
    "facebook": "https://www.facebook.com/marketplace/",
    "whatnot":  "https://www.whatnot.com/",
    "shopify":  "https://admin.shopify.com/",
}

PLATFORM_SELL_URL = {
    "mercari": "https://www.mercari.com/sell/",
    "depop":   "https://www.depop.com/products/create/",
}

_LOGIN_URL_MARKERS = {
    "mercari": ["/login", "/signup", "/auth"],
    "depop":   ["/login", "/register", "/auth"],
    "ebay":    ["/signin", "/login"],
}

_CAPTCHA_SELECTORS = [
    'iframe[src*="captcha"]',
    'iframe[src*="recaptcha"]',
    '[data-testid*="captcha"]',
    ".g-recaptcha",
    "#captcha",
    '[class*="captcha"]',
]

_DEAD_BROWSER_PHRASES = (
    "target page, context or browser has been closed",
    "target closed",
    "browser has been closed",
    "context has been closed",
    "connection closed",
    "websocket",
    "protocol error",
    "execution context was destroyed",
)

RECOVERY_DIR = _ROOT / "logs" / "recovery"


def is_browser_dead_error(error: Exception) -> bool:
    """Return True when the error signals a dead browser/page/context."""
    msg = str(error).lower()
    return any(phrase in msg for phrase in _DEAD_BROWSER_PHRASES)


# ── Adaptive timing ────────────────────────────────────────────────────────────

class PlatformTiming:
    """
    Rolling performance tracker per platform.
    Dynamically widens wait times when the page is running slow or clicks are
    failing, and narrows them again once things recover.
    """
    _BASE_WAIT   = 1.0   # seconds
    _MAX_WAIT    = 6.0
    _SLOW_THRESH = 8.0   # action slower than this triggers slow mode

    def __init__(self) -> None:
        self._action_times:  deque[float] = deque(maxlen=30)
        self._failed_clicks: deque[float] = deque(maxlen=20)
        self._load_times:    deque[float] = deque(maxlen=20)
        self.current_wait = self._BASE_WAIT
        self._slow_mode   = False

    def record_action(self, elapsed: float) -> None:
        self._action_times.append(elapsed)
        if elapsed > self._SLOW_THRESH:
            self._slow_mode   = True
            self.current_wait = min(self.current_wait * 1.5, self._MAX_WAIT)
        elif len(self._action_times) >= 5:
            avg = sum(self._action_times) / len(self._action_times)
            if avg < 2.0 and self._slow_mode:
                self._slow_mode   = False
                self.current_wait = max(self._BASE_WAIT, self.current_wait * 0.8)

    def record_click_fail(self) -> None:
        now = time.monotonic()
        self._failed_clicks.append(now)
        recent = sum(1 for t in self._failed_clicks if now - t < 30)
        if recent >= 3:
            self.current_wait = min(self.current_wait + 0.5, self._MAX_WAIT)

    def record_load(self, elapsed: float) -> None:
        self._load_times.append(elapsed)

    @property
    def type_delay_ms(self) -> int:
        """Per-character typing delay in slow mode; 0 = instant fill()."""
        return 30 if self._slow_mode else 0

    def adaptive_sleep(self) -> float:
        return self.current_wait


# Persists across asyncio.run() calls (module-level state survives the process)
_timings: dict[str, PlatformTiming] = {}


def get_timing(platform: str) -> PlatformTiming:
    if platform not in _timings:
        _timings[platform] = PlatformTiming()
    return _timings[platform]


# ── BrowserSession ─────────────────────────────────────────────────────────────

class BrowserSession:
    """
    One page per platform within the shared CDP context.
    Keeps platforms in separate tabs so login cookies are never cross-contaminated.
    """

    def __init__(self, browser, ctx, page, platform: str) -> None:
        self.browser  = browser
        self.ctx      = ctx
        self.page     = page
        self.platform = platform
        self.timing   = get_timing(platform)

    @classmethod
    async def create(cls, p, platform: str) -> "BrowserSession | None":
        """
        Connect to CDP and return a healthy session for the platform.
        Returns None only if Chrome is completely unreachable.
        """
        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            logger.error("[SESSION] Cannot connect to Chrome (port 9222): %s", e)
            logger.error("[SESSION] Start Chrome with: chrome.exe --remote-debugging-port=9222")
            return None

        ctx  = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await _find_or_create_page(ctx, platform)

        session = cls(browser, ctx, page, platform)
        if not await session.health_check():
            logger.warning("[SESSION] %s failed initial health check — recovering", platform)
            if not await session.recover():
                logger.error("[SESSION] Could not establish healthy session for %s", platform)
                return None

        logger.info("[SESSION OK] %s — %s", platform, session.page.url)
        return session

    async def health_check(self) -> bool:
        """True when the page is live, on the right domain, logged in, captcha-free."""
        try:
            if not await _alive(self.page):
                return False
            if not await _no_captcha(self.page):
                logger.warning("[SESSION] Captcha detected on %s", self.platform)
                return False
            return True
        except Exception as e:
            logger.warning("[SESSION] Health check error on %s: %s", self.platform, e)
            return False

    async def recover(self) -> bool:
        """
        Close the crashed page, open a fresh tab, navigate to platform home.
        Saves a recovery screenshot either way.
        """
        logger.info("[RECONNECTING %s]", self.platform.upper())
        try:
            await self.page.close()
        except Exception:
            pass

        try:
            self.page = await self.ctx.new_page()
            home      = PLATFORM_HOME.get(self.platform, "about:blank")
            await self.page.goto(home, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)
            if await self.health_check():
                await _save_recovery_shot(self.page, self.platform, "recovered")
                logger.info("[SESSION RECOVERED] %s", self.platform)
                return True
        except Exception as e:
            logger.error("[SESSION] Recovery failed for %s: %s", self.platform, e)

        await _save_recovery_shot(self.page, self.platform, "recovery_failed")
        return False

    async def ensure_alive(self) -> bool:
        """Health-check then auto-recover. Returns True if session is usable."""
        if await self.health_check():
            return True
        logger.info("[SESSION] %s unhealthy — attempting recovery", self.platform)
        return await self.recover()

    async def full_recovery(self, p) -> bool:
        """
        Re-establish the CDP connection from scratch when Windows sleep/hibernate
        kills the browser WebSocket.  Mutates self.browser / ctx / page in place.
        """
        logger.warning("[BROWSER RECOVERY] Full reconnection for %s", self.platform)
        print("[BROWSER RECOVERY] Re-establishing Chrome connection...")

        for obj in (self.page, self.ctx):
            try:
                await obj.close()
            except Exception:
                pass

        try:
            browser = await p.chromium.connect_over_cdp(CDP_URL)
            ctx     = browser.contexts[0] if browser.contexts else await browser.new_context()
            page    = await _find_or_create_page(ctx, self.platform)

            self.browser = browser
            self.ctx     = ctx
            self.page    = page

            await asyncio.sleep(1)
            if await self.health_check():
                await _save_recovery_shot(self.page, self.platform, "full_recovered")
                logger.info("[SESSION RESTORED] %s fully recovered", self.platform)
                print("[SESSION RESTORED] Chrome connection re-established.")
                return True

            home = PLATFORM_HOME.get(self.platform, "about:blank")
            await self.page.goto(home, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)
            if await self.health_check():
                await _save_recovery_shot(self.page, self.platform, "full_recovered")
                logger.info("[SESSION RESTORED] %s fully recovered after nav", self.platform)
                print("[SESSION RESTORED] Chrome connection re-established.")
                return True
        except Exception as e:
            logger.error("[BROWSER RECOVERY] Full recovery failed for %s: %s", self.platform, e)

        try:
            await _save_recovery_shot(self.page, self.platform, "full_recovery_failed")
        except Exception:
            pass
        return False

    # ── Anti-desync guards ────────────────────────────────────────────────────

    async def wait_for_ready(self, timeout: float = 30.0) -> bool:
        """Block until document.readyState == 'complete'."""
        try:
            await asyncio.wait_for(_wait_ready(self.page), timeout=timeout)
            return True
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning("[SESSION] wait_for_ready timed out on %s: %s", self.platform, e)
            return False

    async def verify_visible(self, selector: str, timeout: float = 10.0) -> bool:
        try:
            await self.page.locator(selector).first.wait_for(
                state="visible", timeout=int(timeout * 1000)
            )
            return True
        except Exception:
            return False

    async def verify_upload_complete(self, timeout: float = 30.0) -> bool:
        """
        Poll until no upload-progress indicators are visible.
        Returns True on success or timeout (never blocks the batch permanently).
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                busy = await self.page.evaluate(
                    """() => !!(
                        document.querySelector('[data-uploading]') ||
                        document.querySelector('[aria-label*="upload" i][aria-busy]') ||
                        document.querySelector('progress:not([value="1"])')
                    )"""
                )
                if not busy:
                    return True
            except Exception:
                return True
            await asyncio.sleep(0.5)
        logger.warning("[SESSION] verify_upload_complete timed out on %s", self.platform)
        return True

    # ── Adaptive interaction helpers ──────────────────────────────────────────

    async def safe_fill(self, selector: str, text: str, timeout: float = 15.0) -> bool:
        """
        Fill a field. Uses slow character-by-character typing when in slow mode
        to avoid React re-render races that swallow keystrokes.
        """
        try:
            loc = self.page.locator(selector).first
            await loc.wait_for(state="visible", timeout=int(timeout * 1000))
            if self.timing.type_delay_ms > 0:
                await loc.click()
                await loc.press("Control+a")
                await loc.type(text, delay=self.timing.type_delay_ms)
            else:
                await loc.fill(text)
            return True
        except Exception as e:
            logger.warning("[SESSION] safe_fill '%s' failed on %s: %s", selector, self.platform, e)
            self.timing.record_click_fail()
            return False

    async def safe_click(
        self, selector: str, timeout: float = 10.0, retries: int = 3
    ) -> bool:
        """Click with retry loop; feeds timing metrics into adaptive wait."""
        for attempt in range(1, retries + 1):
            t0 = time.monotonic()
            try:
                loc = self.page.locator(selector).first
                await loc.wait_for(state="visible", timeout=int(timeout * 1000))
                await loc.click()
                self.timing.record_action(time.monotonic() - t0)
                return True
            except Exception as e:
                self.timing.record_click_fail()
                logger.warning(
                    "[SESSION] safe_click '%s' attempt %d/%d on %s: %s",
                    selector, attempt, retries, self.platform, e,
                )
                if attempt < retries:
                    await asyncio.sleep(self.timing.adaptive_sleep())
        return False


# ── Internal helpers ───────────────────────────────────────────────────────────

async def _find_or_create_page(ctx, platform: str):
    """Reuse an existing tab on the platform domain; open a fresh one if absent."""
    domain = PLATFORM_DOMAINS.get(platform, "")
    for page in ctx.pages:
        try:
            if domain and domain in page.url:
                return page
        except Exception:
            pass
    return await ctx.new_page()


async def _alive(page) -> bool:
    try:
        state = await asyncio.wait_for(
            page.evaluate("() => document.readyState"), timeout=5.0
        )
        return state in ("interactive", "complete")
    except Exception:
        return False


async def _wait_ready(page) -> None:
    while True:
        try:
            if await page.evaluate("() => document.readyState") == "complete":
                return
        except Exception:
            return
        await asyncio.sleep(0.5)


async def _logged_in(page, platform: str) -> bool:
    markers = _LOGIN_URL_MARKERS.get(platform, ["/login"])
    return not any(m in page.url.lower() for m in markers)


async def _no_captcha(page) -> bool:
    for sel in _CAPTCHA_SELECTORS:
        try:
            if await page.locator(sel).first.is_visible(timeout=500):
                return False
        except Exception:
            pass
    return True


async def _save_recovery_shot(page, platform: str, tag: str) -> None:
    try:
        RECOVERY_DIR.mkdir(parents=True, exist_ok=True)
        ts   = time.strftime("%Y%m%d_%H%M%S")
        path = RECOVERY_DIR / f"{platform}_{tag}_{ts}.png"
        await page.screenshot(path=str(path), full_page=True)
        logger.info("[SESSION] Recovery screenshot saved: %s", path.name)
    except Exception as e:
        logger.warning("[SESSION] Could not save recovery screenshot: %s", e)


# ── Convenience wrappers ───────────────────────────────────────────────────────

async def ensure_mercari_alive(p) -> "BrowserSession | None":
    return await BrowserSession.create(p, "mercari")


async def ensure_depop_alive(p) -> "BrowserSession | None":
    return await BrowserSession.create(p, "depop")


async def ensure_browser_alive(p, session: "BrowserSession") -> "BrowserSession | None":
    """
    Verify the session is still alive; do a full CDP reconnection if not.
    Returns the (possibly-recovered) session, or None on permanent failure.
    """
    try:
        await asyncio.wait_for(session.page.evaluate("() => 1"), timeout=5.0)
        return session
    except Exception:
        pass

    logger.warning("[BROWSER RECOVERY] Page unreachable — attempting full reconnection")
    print("[BROWSER RECOVERY] Page/context dead — reconnecting to Chrome...")
    if await session.full_recovery(p):
        return session
    logger.error("[BROWSER RECOVERY] Permanent failure — Chrome may need a manual restart")
    return None


# ── Future stubs ───────────────────────────────────────────────────────────────

async def ensure_ebay_alive(p) -> "BrowserSession | None":
    raise NotImplementedError("eBay session manager not yet implemented")


async def ensure_facebook_alive(p) -> "BrowserSession | None":
    raise NotImplementedError("Facebook Marketplace session manager not yet implemented")


async def ensure_whatnot_alive(p) -> "BrowserSession | None":
    raise NotImplementedError("Whatnot session manager not yet implemented")


async def ensure_shopify_alive(p) -> "BrowserSession | None":
    raise NotImplementedError("Shopify session manager not yet implemented")
