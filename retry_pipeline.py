"""
Retry pipeline for failed/unconfirmed CrossLister listings.

Sources items from failed/failed_log.csv.
Retries per-platform using images stored in failed/mercari/ or failed/depop/.
Writes successes to inventory.csv and marks rows resolved in failed_log.csv.
Never retries items that are already active in inventory or the database.
"""
import csv
from datetime import datetime
from pathlib import Path

import database as db
import mercari
import depop
import failure_handler

MAX_RETRIES   = 3
INVENTORY_CSV = failure_handler.BASE_DIR / "inventory.csv"
_INV_COLS     = ["timestamp", "platform", "filename", "title", "listing_id", "batch_id"]


# ── Inventory helpers ──────────────────────────────────────────────────────────

def load_inventory() -> set[tuple[str, str]]:
    """Return set of (platform, filename) pairs from inventory.csv."""
    if not INVENTORY_CSV.exists():
        return set()
    with INVENTORY_CSV.open(newline="", encoding="utf-8") as f:
        return {
            (r.get("platform", ""), r.get("filename", ""))
            for r in csv.DictReader(f)
            if r.get("platform") and r.get("filename")
        }


def append_inventory(
    platform: str, filename: str, title: str, listing_id: str, batch_id: str
) -> None:
    """Append one successful listing to inventory.csv."""
    write_header = not INVENTORY_CSV.exists()
    try:
        with INVENTORY_CSV.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=_INV_COLS)
            if write_header:
                w.writeheader()
            w.writerow({
                "timestamp":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "platform":   platform,
                "filename":   filename,
                "title":      title,
                "listing_id": listing_id or "",
                "batch_id":   batch_id,
            })
    except Exception as e:
        print(f"[WARN] Could not write inventory.csv: {e}")


# ── Internal helpers ───────────────────────────────────────────────────────────

def _is_already_active(platform: str, filename: str, inventory: set) -> bool:
    """
    Return True if the file is already listed on this platform — checked against
    both inventory.csv (retry successes) and the database (main-pipeline successes).
    """
    if (platform, filename) in inventory:
        return True
    item = db.get_item_by_filename(filename)
    if not item:
        return False
    pid = item["mercari_id"] if platform == "mercari" else item["depop_id"]
    return bool(pid and item["status"] in ("active", "sold"))


def _find_image(platform: str, filename: str) -> Path | None:
    """Locate the image in failed/{platform}/."""
    d = failure_handler.FAILED_MERCARI if platform == "mercari" else failure_handler.FAILED_DEPOP
    p = d / filename
    return p if p.exists() else None


def _prepare_db_item(filename: str, image_path: Path) -> int | None:
    """
    Find the DB item by filename and redirect its image_path to the retry copy.
    Returns item_id, or None if no DB record exists.
    """
    item = db.get_item_by_filename(filename)
    if not item:
        return None
    db.update_item(item["id"], {"image_path": str(image_path)})
    return item["id"]


def _bump_retry(row: dict, reason: str) -> None:
    """Increment retry_count; escalate to permanent_failure once MAX_RETRIES is hit."""
    count = int(row.get("retry_count", 0) or 0) + 1
    row["retry_count"]  = str(count)
    row["last_attempt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row["reason"]       = str(reason)[:250]
    row["status"]       = "permanent_failure" if count >= MAX_RETRIES else "failed"


# ── Main entry point ───────────────────────────────────────────────────────────

def run_retry(batch_id: str) -> dict:
    """
    Process all retryable rows from failed_log.csv.
    Returns a summary stats dict with keys:
        total_failed, retried, recovered, permanent_failures
    """
    failure_handler.ensure_dirs()

    rows      = failure_handler.load_failed_log()
    inventory = load_inventory()

    stats = {
        "total_failed":       0,
        "retried":            0,
        "recovered":          0,
        "permanent_failures": 0,
    }

    retryable = [
        r for r in rows
        if r.get("resolved", "no") != "yes"
        and r.get("status", "") in ("failed", "unconfirmed")
    ]
    stats["total_failed"] = len(retryable)

    print(f"\n  Found {len(retryable)} retryable item(s) in failed_log.csv.")
    if not retryable:
        print("  Nothing to retry.")
        return stats

    for row in retryable:
        platform    = row["platform"].lower()
        filename    = row["filename"]
        title       = row.get("title", "")
        retry_count = int(row.get("retry_count", 0) or 0)
        label       = f"{platform.upper()} / {filename}"

        # ── Already at max retries ────────────────────────────────────────
        if retry_count >= MAX_RETRIES:
            print(f"  [RETRY FAILED] {label} — permanent failure (max {MAX_RETRIES} retries reached)")
            row["status"] = "permanent_failure"
            stats["permanent_failures"] += 1
            continue

        # ── Duplicate detection ───────────────────────────────────────────
        if _is_already_active(platform, filename, inventory):
            print(f"  [SKIP] {label} — already active in inventory, marking resolved")
            row["resolved"] = "yes"
            row["status"]   = "resolved"
            continue

        # ── Locate image ──────────────────────────────────────────────────
        image_path = _find_image(platform, filename)
        if not image_path:
            reason = f"Image not found in failed/{platform}/"
            print(f"  [RETRY FAILED] {label} — {reason}")
            _bump_retry(row, reason)
            if int(row["retry_count"]) >= MAX_RETRIES:
                stats["permanent_failures"] += 1
            stats["retried"] += 1
            continue

        # ── Map DB item to the retry image ────────────────────────────────
        item_id = _prepare_db_item(filename, image_path)
        if not item_id:
            reason = "No matching item in database"
            print(f"  [RETRY FAILED] {label} — {reason}")
            _bump_retry(row, reason)
            if int(row["retry_count"]) >= MAX_RETRIES:
                stats["permanent_failures"] += 1
            stats["retried"] += 1
            continue

        # ── Attempt listing ───────────────────────────────────────────────
        print(f"  Retrying {label} (attempt {retry_count + 1}/{MAX_RETRIES})...")
        listing_id = None
        err        = None
        try:
            listing_id = (
                mercari.list_on_mercari(item_id)
                if platform == "mercari"
                else depop.list_on_depop(item_id)
            )
        except Exception as e:
            err = str(e)

        stats["retried"] += 1

        if listing_id:
            print(f"  [RETRY SUCCESS] {label} — listing ID: {listing_id}")
            row["resolved"]     = "yes"
            row["status"]       = "resolved"
            row["last_attempt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            append_inventory(platform, filename, title, listing_id, batch_id)
            inventory.add((platform, filename))
            stats["recovered"] += 1
        else:
            reason = err or "No listing ID returned"
            print(f"  [RETRY FAILED] {label} — {reason}")
            _bump_retry(row, reason)
            if int(row["retry_count"]) >= MAX_RETRIES:
                stats["permanent_failures"] += 1

    failure_handler.save_failed_log(rows)
    return stats
