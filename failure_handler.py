"""
Centralised failure handling for CrossLister batch processing.
Records failures to CSV, copies images to platform-specific folders,
and captures Playwright screenshots — without ever raising to the caller.
"""
import csv
import shutil
import time
from datetime import datetime
from pathlib import Path

BASE_DIR        = Path(__file__).parent
FAILED_DIR      = BASE_DIR / "failed"
FAILED_MERCARI  = FAILED_DIR / "mercari"
FAILED_DEPOP    = FAILED_DIR / "depop"
SCREENSHOTS_DIR = BASE_DIR / "logs" / "screenshots"
FAILED_LOG      = FAILED_DIR / "failed_log.csv"

_CSV_COLUMNS = [
    "timestamp", "platform", "filename", "title", "reason", "status", "batch_id",
    "retry_count", "last_attempt", "resolved",
]

_PLATFORM_DIRS = {
    "mercari": FAILED_MERCARI,
    "depop":   FAILED_DEPOP,
}


def ensure_dirs() -> None:
    """Create required directories and initialise the CSV header if missing."""
    for d in (FAILED_DIR, FAILED_MERCARI, FAILED_DEPOP, SCREENSHOTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    if not FAILED_LOG.exists():
        with FAILED_LOG.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=_CSV_COLUMNS).writeheader()


def load_failed_log() -> list[dict]:
    """Read all rows from failed_log.csv, back-filling missing columns with defaults."""
    ensure_dirs()
    if not FAILED_LOG.exists():
        return []
    with FAILED_LOG.open(newline="", encoding="utf-8") as f:
        rows = []
        for row in csv.DictReader(f):
            row.setdefault("retry_count",  "0")
            row.setdefault("last_attempt", "")
            row.setdefault("resolved",     "no")
            rows.append(row)
        return rows


def save_failed_log(rows: list[dict]) -> None:
    """Rewrite failed_log.csv from a list of row dicts. Handles schema migration."""
    ensure_dirs()
    try:
        with FAILED_LOG.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
            w.writeheader()
            for row in rows:
                row.setdefault("retry_count",  "0")
                row.setdefault("last_attempt", "")
                row.setdefault("resolved",     "no")
                w.writerow(row)
    except Exception as e:
        print(f"[WARN] Could not save failed_log.csv: {e}")


def record_failure(
    platform: str,
    image_path,
    title: str,
    reason: str,
    batch_id: str,
    card_name: str = "",
    status: str = "failed",
) -> None:
    """
    Copy the image to failed/{platform}/ and append a row to failed_log.csv.
    Images are copied (not moved) so the original is still available for the
    overall archive decision in the calling pipeline.
    Never raises.
    """
    ensure_dirs()
    image_path  = Path(image_path) if image_path else None
    platform_lc = platform.lower()
    dest_dir    = _PLATFORM_DIRS.get(platform_lc, FAILED_DIR)
    filename    = image_path.name if image_path else "unknown"

    # Copy image into the platform failed folder
    if image_path and image_path.exists():
        dest = dest_dir / filename
        if dest.exists():
            dest = dest_dir / f"{image_path.stem}_{int(time.time())}{image_path.suffix}"
        try:
            shutil.copy2(str(image_path), str(dest))
            print(f"[FAILED ITEM MOVED] {filename} → failed/{platform_lc}/")
        except Exception as e:
            print(f"[WARN] Could not copy failed image {filename}: {e}")

    # Append to CSV
    row = {
        "timestamp":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "platform":     platform,
        "filename":     filename,
        "title":        title,
        "reason":       str(reason)[:250],
        "status":       status,
        "batch_id":     batch_id,
        "retry_count":  "0",
        "last_attempt": "",
        "resolved":     "no",
    }
    try:
        with FAILED_LOG.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=_CSV_COLUMNS).writerow(row)
    except Exception as e:
        print(f"[WARN] Could not write to failed_log.csv: {e}")


async def save_failure_screenshot(page, platform: str, card_name: str = "") -> str | None:
    """
    Capture a full-page Playwright screenshot on failure or unconfirmed listing.
    Filename: platform_YYYYMMDD_HHMMSS_cardname.png
    Returns the saved path string, or None on error. Never raises.
    """
    ensure_dirs()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = (
        "".join(c if c.isalnum() or c in "-_" else "_" for c in card_name)
        .strip("_")[:40]
    )
    parts    = [p for p in (platform, timestamp, safe_name) if p]
    filename = "_".join(parts) + ".png"
    path     = SCREENSHOTS_DIR / filename
    try:
        await page.screenshot(path=str(path), full_page=True)
        print(f"[FAILURE SCREENSHOT SAVED] {filename}")
        return str(path)
    except Exception as e:
        print(f"[WARN] Could not save failure screenshot: {e}")
        return None
