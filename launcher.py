"""
CrossLister Launcher — Interactive menu for the CrossLister pipeline.
"""
import asyncio
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import failure_handler

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
TEMP_DIR = BASE_DIR / "temp"
MANIFEST = TEMP_DIR / "active_batches.json"

# Listing pages that hold file handles on temp images — navigated away at cleanup
_LISTING_URL_FRAGMENTS = (
    "mercari.com/sell",
    "depop.com/products/create",
)

# ── Long-run mode ───────────────────────────────────────────────────────────────
# Enable for large batches (10+ items), overnight runs, or when sleep
# interruptions are likely.  Trades speed for resilience.
LONG_RUN_MODE = False

_LONG_RUN_CARD_PAUSE_S  = 2   # extra seconds between cards
_LONG_RUN_PAGE_REFRESH  = 5   # navigate Depop tab to home every N listings


# ── Temp-folder helpers ────────────────────────────────────────────────────────

def _try_rmtree(path: Path) -> bool:
    """
    Single best-effort rmtree. Warns and returns False on any failure.
    Never retries, never renames — callers are expected to tolerate failure.
    """
    path = Path(path)
    if not path.exists():
        return True
    try:
        shutil.rmtree(path)
        return True
    except Exception as e:
        print(f"[WARN] Could not delete {path.name}: {e} — skipping")
        return False


def _close_platform_pages_sync() -> None:
    """
    Best-effort: navigate any open Mercari/Depop listing tabs to about:blank
    so Windows releases the file handles on uploaded images inside temp/.
    Silently skips if Chrome is not running.
    """
    async def _close():
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await asyncio.wait_for(
                    p.chromium.connect_over_cdp("http://localhost:9222"),
                    timeout=5.0,
                )
                ctx = browser.contexts[0] if browser.contexts else None
                if not ctx:
                    return
                for page in ctx.pages:
                    try:
                        url = page.url
                        if any(frag in url for frag in _LISTING_URL_FRAGMENTS):
                            await page.goto("about:blank", timeout=5000)
                            print(f"[CLEANUP] Navigated away: {url[:70]}")
                    except Exception:
                        pass
        except Exception:
            pass  # Chrome not running is fine

    try:
        asyncio.run(_close())
    except Exception:
        pass


def _navigate_depop_to_home() -> None:
    """Navigate the open Depop tab to depop.com home to clear accumulated SPA state."""
    async def _nav():
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await asyncio.wait_for(
                    p.chromium.connect_over_cdp("http://localhost:9222"),
                    timeout=5.0,
                )
                ctx = browser.contexts[0] if browser.contexts else None
                if not ctx:
                    return
                for page in ctx.pages:
                    try:
                        if "depop.com" in page.url:
                            await page.goto("https://www.depop.com/", timeout=15000)
                            print("[LONG-RUN] Depop tab refreshed to home")
                            return
                    except Exception:
                        pass
        except Exception:
            pass
    try:
        asyncio.run(_nav())
    except Exception:
        pass


def _load_manifest() -> dict:
    if not MANIFEST.exists():
        return {"batches": []}
    try:
        with MANIFEST.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"batches": []}


def _save_manifest(data: dict) -> None:
    try:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        with MANIFEST.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[WARN] Could not save batch manifest: {e}")


def _cleanup_old_batches() -> None:
    """
    Delete temp batch folders recorded in the manifest that are older than 24 h.
    Skips any folder it cannot delete — never raises.
    """
    data    = _load_manifest()
    cutoff  = datetime.now().timestamp() - 86400
    survive = []

    for entry in data.get("batches", []):
        if entry.get("created_ts", 0) < cutoff:
            for key in ("mercari_batch", "depop_batch"):
                p = Path(entry.get(key, ""))
                if p.exists():
                    if _try_rmtree(p):
                        print(f"[CLEANUP] Removed old batch folder: {p.name}")
                    # If delete fails, just leave it — next run will try again
        else:
            survive.append(entry)

    data["batches"] = survive
    _save_manifest(data)


def batch_prep() -> tuple[Path, Path]:
    """
    Prepare fresh timestamped temp batch folders for this run.

    Strategy (never crashes — temp folders are disposable):
      1. Navigate browser away from listing pages → release file handles
      2. Wait for OneDrive/Windows to catch up
      3. Age-out old batch folders (>24 h) from the manifest
      4. Create new uniquely-named folders for this run
      5. Record them in the manifest so the next run can clean them up

    Returns (mercari_batch_path, depop_batch_path).
    """
    failure_handler.ensure_dirs()
    print("[STARTUP CLEANUP]")

    # Release browser file handles so Windows unlocks temp images
    _close_platform_pages_sync()
    print("[CLEANUP WAIT] Releasing browser/OneDrive handles (4s)...")
    time.sleep(4)

    # Remove batch folders older than 24 h (best-effort)
    _cleanup_old_batches()

    # Create fresh, uniquely-named staging folders for this batch run
    ts            = datetime.now().strftime("%Y%m%d_%H%M%S")
    mercari_batch = TEMP_DIR / f"mercari_batch_{ts}"
    depop_batch   = TEMP_DIR / f"depop_batch_{ts}"

    try:
        mercari_batch.mkdir(parents=True, exist_ok=True)
        depop_batch.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"[WARN] Could not create temp folders: {e}")

    # Register this batch so the next startup knows to age it out
    data = _load_manifest()
    data["batches"].append({
        "batch_id":      f"BATCH_{ts}",
        "mercari_batch": str(mercari_batch),
        "depop_batch":   str(depop_batch),
        "created_ts":    datetime.now().timestamp(),
        "created_at":    datetime.now().isoformat(timespec="seconds"),
    })
    _save_manifest(data)

    print(f"[BATCH PREP READY] mercari_batch_{ts}  depop_batch_{ts}")
    return mercari_batch, depop_batch


# ── Menu actions ───────────────────────────────────────────────────────────────

def run_full_pipeline():
    import main
    main.run_pipeline()


def check_sales_only():
    import database as db
    import sales_checker
    db.init_db()
    results = sales_checker.check_all_sales()
    sold = results.get("sold", [])
    if sold:
        print(f"  {len(sold)} item(s) sold:")
        for s in sold:
            print(f"    {s['title'][:40]} @ ${s['price']:.2f}")
    else:
        print("  No new sales.")


def sync_deletions_only():
    import database as db
    import sales_checker
    import sync_manager
    db.init_db()
    results = sales_checker.check_all_sales()
    sold = results.get("sold", [])
    if sold:
        stats = sync_manager.sync_sold_items(sold)
        print(f"  Sync complete: {stats}")
    else:
        print("  No sold items to sync.")


def identify_and_price():
    import logging
    import shutil as _shutil
    from pathlib import Path as _Path
    import database as db
    import identifier
    import pricing

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logger = logging.getLogger("launcher.identify_price")

    INPUT_FOLDER  = _Path(r"C:\Users\mrozo\OneDrive\Desktop\CardsToList")
    FAILED_DIR    = INPUT_FOLDER / "Failed"
    SUPPORTED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"}

    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()

    if not INPUT_FOLDER.exists():
        print(f"  Input folder not found: {INPUT_FOLDER}")
        return

    images = [f for f in INPUT_FOLDER.iterdir()
              if f.is_file() and f.suffix.lower() in SUPPORTED_EXT]
    if not images:
        print("  No images found.")
        return

    if not identifier.check_ollama_available():
        print("  Ollama not running — run: ollama serve && ollama pull llava")
        return

    for img in sorted(images):
        print(f"  Processing: {img.name}")
        try:
            card = identifier.identify_card(img, None)
            card["title"] = identifier.build_title(card)
            price_data = pricing.get_smart_price(card)
            card["asking_price"] = price_data["asking_price"]
            print(f"    → {card.get('card_name')} | ${card['asking_price']:.2f}"
                  f" ({price_data['data_points']} data points)")
        except Exception as e:
            print(f"    [ERROR] {e}")
            _shutil.move(str(img), str(FAILED_DIR / img.name))


def batch_list():
    """Option 5 — Batch Prep then list items on Mercari + Depop."""
    mercari_batch, depop_batch = batch_prep()

    import database as db
    import mercari
    import depop as _depop

    db.init_db()
    batch_id = datetime.now().strftime("BATCH_%Y%m%d_%H%M%S")
    items = db.get_pending_items()

    if not items:
        print("  No pending items to list.")
        return

    # Push long-run config into the depop module
    _depop.LONG_RUN_MODE = LONG_RUN_MODE
    if LONG_RUN_MODE:
        print(f"[LONG-RUN MODE] Enabled — {_LONG_RUN_CARD_PAUSE_S}s pause between cards, "
              f"page refresh every {_LONG_RUN_PAGE_REFRESH} listings")

    print(f"  Listing {len(items)} pending item(s)...")
    listings_done = 0

    for item in items:
        item_id    = item["id"]
        image_path = item["image_path"]
        title      = item["title"] or ""
        card_name  = item["card_name"] or ""

        print(f"\n  ── Item {item_id}: {card_name or title[:30]} ──")

        mid = None
        mercari_err = None
        try:
            mid = mercari.list_on_mercari(item_id)
        except Exception as e:
            print(f"    [ERROR] Mercari: {e}")
            mercari_err = str(e)
        if mid:
            print(f"    Mercari OK: {mid}")
        else:
            _reason = mercari_err or "No listing ID returned"
            _status = "failed" if mercari_err else "unconfirmed"
            failure_handler.record_failure(
                "mercari", image_path, title, _reason, batch_id,
                card_name=card_name, status=_status,
            )

        time.sleep(5)

        did = None
        depop_err = None
        try:
            did = _depop.list_on_depop(item_id)
        except Exception as e:
            print(f"    [ERROR] Depop: {e}")
            depop_err = str(e)
        if did:
            print(f"    Depop OK: {did}")
        else:
            _reason = depop_err or "No listing ID returned"
            _status = "failed" if depop_err else "unconfirmed"
            failure_handler.record_failure(
                "depop", image_path, title, _reason, batch_id,
                card_name=card_name, status=_status,
            )

        listings_done += 1

        if LONG_RUN_MODE:
            time.sleep(_LONG_RUN_CARD_PAUSE_S)
            if listings_done % _LONG_RUN_PAGE_REFRESH == 0:
                print(f"[LONG-RUN] {listings_done} listings done — refreshing Depop tab")
                _navigate_depop_to_home()

        print("[BATCH CONTINUING]")


def resume_interrupted_batch():
    import database as db
    db.init_db()

    data    = _load_manifest()
    cutoff  = datetime.now().timestamp() - 86400
    recent  = [b for b in data.get("batches", []) if b.get("created_ts", 0) > cutoff]
    pending = db.get_pending_items()

    if not recent:
        print("  No interrupted batches found in the last 24 h.")
        if pending:
            print(f"  {len(pending)} pending item(s) exist — use 'Batch Prep & List Items' to list them.")
        return

    if not pending:
        print("  No pending items remain — all batches appear complete.")
        return

    print(f"  {len(recent)} recent batch(es) found, {len(pending)} pending item(s) remain:")
    for b in recent:
        print(f"    {b['batch_id']}  started {b.get('created_at', '?')}")

    ans = input(f"\n  Resume listing {len(pending)} pending item(s)? [y/N]: ").strip().lower()
    if ans == "y":
        batch_list()
    else:
        print("  Resume cancelled.")


def retry_failed():
    import retry_pipeline
    batch_id = datetime.now().strftime("RETRY_%Y%m%d_%H%M%S")
    stats = retry_pipeline.run_retry(batch_id)
    print(f"\n  Failed:             {stats['total_failed']}")
    print(f"  Retried:            {stats['retried']}")
    print(f"  Recovered:          {stats['recovered']}")
    print(f"  Permanent failures: {stats['permanent_failures']}")


def view_queue_status():
    import database as db
    db.init_db()

    pending = db.get_pending_items()
    active  = db.get_active_items()

    rows      = failure_handler.load_failed_log()
    by_status: dict[str, int] = {}
    for row in rows:
        s = row.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1
    unresolved = [r for r in rows if r.get("resolved", "no") != "yes"]

    data    = _load_manifest()
    cutoff  = datetime.now().timestamp() - 86400
    recent  = [b for b in data.get("batches", []) if b.get("created_ts", 0) > cutoff]

    print(f"\n  ── Database ─────────────────────────────")
    print(f"  Pending items : {len(pending)}")
    print(f"  Active items  : {len(active)}")

    print(f"\n  ── Failed Log ───────────────────────────")
    if rows:
        for status, count in sorted(by_status.items()):
            print(f"  {status:<22}: {count}")
        print(f"  {'Unresolved total':<22}: {len(unresolved)}")
    else:
        print("  failed_log.csv is empty — no recorded failures.")

    print(f"\n  ── Active Batches (last 24 h) ───────────")
    if recent:
        for b in recent:
            print(f"  {b['batch_id']}  started {b.get('created_at', '?')}")
    else:
        print("  No recent batches.")


def daily_dashboard():
    _utils = BASE_DIR / "scripts" / "utils"
    if str(_utils) not in sys.path:
        sys.path.insert(0, str(_utils))
    import dashboard
    dashboard.run_dashboard(export=True)


def generate_report():
    import report
    report.generate_daily_report()
    print("  Report generated.")


# ── Main menu ──────────────────────────────────────────────────────────────────

MENU = [
    ("Run Full Pipeline",             run_full_pipeline),
    ("Check Sales Only",              check_sales_only),
    ("Sync Deletions Only",           sync_deletions_only),
    ("Identify & Price New Cards",    identify_and_price),
    ("Batch Prep & List Items",       batch_list),
    ("Resume Interrupted Batch",      resume_interrupted_batch),
    ("Retry Failed Jobs",             retry_failed),
    ("View Queue Status",             view_queue_status),
    ("Daily Report / Dashboard",      daily_dashboard),
    ("Generate Legacy Report",        generate_report),
]


def print_menu():
    print("\n" + "═" * 44)
    print("  CrossLister — Main Menu")
    print("═" * 44)
    for i, (label, _) in enumerate(MENU, 1):
        print(f"  {i}. {label}")
    print("  0. Exit")
    print("═" * 44)


def main():
    while True:
        print_menu()
        raw = input("  Select option: ").strip()
        if raw == "0":
            print("  Goodbye.")
            sys.exit(0)
        if not raw.isdigit() or not (1 <= int(raw) <= len(MENU)):
            print("  Invalid option — try again.")
            continue
        label, action = MENU[int(raw) - 1]
        print(f"\n─── {label} ───")
        try:
            action()
        except Exception as e:
            print(f"\n[ERROR] {label} failed: {e}")
        input("\n  Press Enter to return to menu...")


if __name__ == "__main__":
    main()
