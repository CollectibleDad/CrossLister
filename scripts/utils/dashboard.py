"""
CardBot Daily Report & Status Dashboard

Sources:
  crosslister.db          — all inventory, pricing, and sales data
  failed/failed_log.csv   — per-platform failure history
  inventory.csv           — retry-success log

Run standalone:  python scripts/utils/dashboard.py [--export]
Via launcher:    Option 8 — Daily Report / Dashboard
"""
import csv
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# ── Bootstrap: add CrossLister root to sys.path ────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import database as db
import failure_handler

INVENTORY_CSV = _ROOT / "inventory.csv"
REPORTS_DIR   = _ROOT / "reports"


# ── ANSI color support ─────────────────────────────────────────────────────────

def _enable_ansi() -> bool:
    """Enable VT100/ANSI escape codes on Windows; always True on other platforms."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        # ENABLE_PROCESSED_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        k.SetConsoleMode(k.GetStdHandle(-11), 7)
        return True
    except Exception:
        return False


_ANSI = _enable_ansi()

_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_RED    = "\033[91m"
_CYAN   = "\033[96m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_RST    = "\033[0m"


def _c(text, code: str) -> str:
    return f"{code}{text}{_RST}" if _ANSI else str(text)


def _g(t) -> str:  return _c(t, _GREEN)
def _y(t) -> str:  return _c(t, _YELLOW)
def _r(t) -> str:  return _c(t, _RED)
def _b(t) -> str:  return _c(t, _BOLD)
def _d(t) -> str:  return _c(t, _DIM)
def _hdr(title: str) -> str:
    return _c(f"━━━ {title} ━━━", _CYAN + _BOLD)


def _color_int(n: int, warn: int, bad: int) -> str:
    """Green / yellow / red based on ascending thresholds (higher = worse)."""
    s = str(n)
    if n >= bad:  return _r(s)
    if n >= warn: return _y(s)
    return _g(s)


def _color_pct(pct: float, warn: float, bad: float, *, lower_is_worse: bool = False) -> str:
    """Color a percentage value. Use lower_is_worse=True when high % is good."""
    s = f"{pct:.1f}%"
    if lower_is_worse:
        color = _RED if pct <= bad else _YELLOW if pct <= warn else _GREEN
    else:
        color = _RED if pct >= bad else _YELLOW if pct >= warn else _GREEN
    return _c(s, color)


def _avg(vals: list) -> float:
    return sum(vals) / len(vals) if vals else 0.0


# ── Data loaders ───────────────────────────────────────────────────────────────

def _load_items() -> list[dict]:
    db.init_db()
    with db.get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM items").fetchall()]


def _load_failed() -> list[dict]:
    return failure_handler.load_failed_log()


def _load_inventory() -> list[dict]:
    if not INVENTORY_CSV.exists():
        return []
    with INVENTORY_CSV.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ══════════════════════════════════════════════════════════════════════════════
# Section 1 — Inventory Overview
# ══════════════════════════════════════════════════════════════════════════════

def _s1_inventory(items: list[dict], failed_rows: list[dict]) -> tuple[list[str], dict]:
    active  = sum(1 for i in items if i["status"] == "active")
    sold    = sum(1 for i in items if i["status"] == "sold")
    deleted = sum(1 for i in items if i["status"] in ("deleted", "removed"))
    pending = sum(
        1 for r in failed_rows
        if r.get("resolved", "no") != "yes"
        and r.get("status", "") in ("failed", "unconfirmed")
    )
    perm    = sum(1 for r in failed_rows if r.get("status") == "permanent_failure")

    lines = [
        _hdr("INVENTORY OVERVIEW"),
        f"  Active listings:    {_g(active)}",
        f"  Sold:               {_g(sold)}",
        f"  Removed / deleted:  {_y(deleted) if deleted else _g(deleted)}",
        f"  Pending retries:    {_y(pending) if pending else _g(pending)}",
        f"  Permanent failures: {_r(perm)    if perm    else _g(perm)}",
    ]
    return lines, {
        "active": active, "sold": sold, "deleted": deleted,
        "pending_retries": pending, "permanent_failures": perm,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Section 2 — Platform Performance
# ══════════════════════════════════════════════════════════════════════════════

def _s2_platforms(items: list[dict], failed_rows: list[dict]) -> tuple[list[str], dict]:
    lines = [_hdr("PLATFORM PERFORMANCE")]
    data  = {}

    for p in ("mercari", "depop", "ebay"):
        pid_col = f"{p}_id"
        listed  = [i for i in items if i.get(pid_col)]
        sold_p  = [i for i in items if i.get("platform_sold") == p and i["status"] == "sold"]

        n_listed = len(listed)
        n_sold   = len(sold_p)
        sell_pct = n_sold / n_listed * 100 if n_listed else 0.0

        avg_list = _avg([i["asking_price"] for i in listed if i.get("asking_price")])
        avg_sold = _avg([i["sold_price"]   for i in sold_p if i.get("sold_price")])

        days_list: list[float] = []
        for i in sold_p:
            try:
                dl = datetime.fromisoformat(i["date_listed"])
                ds = datetime.fromisoformat(i["date_sold"])
                days_list.append(float((ds - dl).days))
            except Exception:
                pass
        avg_days = _avg(days_list)

        p_fails   = sum(
            1 for r in failed_rows
            if r.get("platform", "").lower() == p
            and r.get("resolved", "no") != "yes"
        )
        fail_rate = p_fails / n_listed * 100 if n_listed else 0.0

        sell_str = _color_pct(sell_pct, 10.0, 5.0, lower_is_worse=True)
        fail_str = _color_pct(fail_rate, 10.0, 25.0)

        lines += [
            f"\n  {_b(p.upper())}",
            f"    Listed:             {n_listed}",
            f"    Sold:               {n_sold}  ({sell_str} sell-through)",
            f"    Avg list price:     ${avg_list:.2f}",
            f"    Avg sold price:     ${avg_sold:.2f}",
            f"    Avg days to sell:   {avg_days:.1f}",
            f"    Failure rate:       {fail_str}",
        ]
        data[p] = {
            "listed": n_listed, "sold": n_sold,
            "sell_pct": round(sell_pct, 2),
            "avg_list_price": round(avg_list, 2),
            "avg_sold_price": round(avg_sold, 2),
            "avg_days_to_sell": round(avg_days, 1),
            "failure_rate": round(fail_rate, 2),
        }

    return lines, data


# ══════════════════════════════════════════════════════════════════════════════
# Section 3 — Batch Metrics
# ══════════════════════════════════════════════════════════════════════════════

def _s3_batch(
    items: list[dict], failed_rows: list[dict], inv_rows: list[dict]
) -> tuple[list[str], dict]:

    all_ids   = (
        [r["batch_id"] for r in failed_rows if r.get("batch_id")]
        + [r["batch_id"] for r in inv_rows  if r.get("batch_id")]
    )
    last_batch = max(all_ids, default="—")

    batch_fails = [r for r in failed_rows if r.get("batch_id") == last_batch]
    n_errors    = sum(1 for r in batch_fails if r.get("status") == "failed")
    n_unconf    = sum(1 for r in batch_fails if r.get("status") == "unconfirmed")
    n_ok_inv    = sum(1 for r in inv_rows    if r.get("batch_id") == last_batch)

    n_db = 0
    if last_batch not in ("—", "") and "_" in last_batch:
        parts = last_batch.split("_")
        if len(parts) >= 2 and len(parts[1]) == 8 and parts[1].isdigit():
            try:
                bd  = datetime.strptime(parts[1], "%Y%m%d").date().isoformat()
                n_db = sum(1 for i in items if (i.get("created_at") or "").startswith(bd))
            except Exception:
                pass

    n_processed = max(n_db, n_errors + n_unconf + n_ok_inv)
    n_success   = max(0, n_processed - n_errors - n_unconf)
    succ_pct    = n_success / n_processed * 100 if n_processed else 0.0

    lines = [
        _hdr("LAST BATCH"),
        f"  Batch ID:         {_d(last_batch)}",
        f"  Items processed:  {n_processed}",
        f"  Succeeded:        {_g(n_success)}  ({_color_pct(succ_pct, 70.0, 50.0, lower_is_worse=True)})",
        f"  Errors:           {_color_int(n_errors, 1, 3)}",
        f"  Unconfirmed:      {_color_int(n_unconf, 1, 3)}",
    ]
    return lines, {
        "last_batch_id": last_batch,
        "items_processed": n_processed,
        "succeeded": n_success,
        "success_pct": round(succ_pct, 2),
        "errors": n_errors,
        "unconfirmed": n_unconf,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Section 4 — Failure Summary
# ══════════════════════════════════════════════════════════════════════════════

_REASON_BUCKETS: list[tuple[str, str]] = [
    ("timeout",     "Timeout"),
    ("timed out",   "Timeout"),
    ("category",    "Category selection"),
    ("photo",       "Photo / upload failure"),
    ("upload",      "Photo / upload failure"),
    ("connect",     "Chrome / CDP connection"),
    ("cdp",         "Chrome / CDP connection"),
    ("9222",        "Chrome / CDP connection"),
    ("chrome",      "Chrome / CDP connection"),
    ("listing id",  "Verification / no listing ID"),
    ("unconfirmed", "Verification / no listing ID"),
    ("verify",      "Verification / no listing ID"),
    ("not found",   "Image / item not found"),
    ("missing",     "Image / item not found"),
    ("permission",  "Permission / access error"),
    ("access",      "Permission / access error"),
    ("database",    "Database error"),
    ("price",       "Pricing failure"),
    ("submit",      "Submission failure"),
    ("post",        "Submission failure"),
]


def _bucket_reason(reason: str) -> str:
    r = reason.lower()
    for needle, label in _REASON_BUCKETS:
        if needle in r:
            return label
    return "Other"


def _s4_failures(failed_rows: list[dict]) -> tuple[list[str], dict]:
    active  = [r for r in failed_rows if r.get("resolved", "no") != "yes"]
    reasons = Counter(_bucket_reason(r.get("reason", "")) for r in active)
    total   = sum(reasons.values())

    lines = [_hdr("TOP FAILURE REASONS")]
    if not reasons:
        lines.append(f"  {_g('No active failures — all clear!')}")
    else:
        for reason, count in reasons.most_common(8):
            pct     = count / total * 100
            bar     = "█" * min(int(pct / 5), 20)
            cnt_str = f"{count:>3}"
            lines.append(
                f"  {reason:<40}  {_r(cnt_str)}  ({pct:.1f}%)  {_d(bar)}"
            )

    return lines, dict(reasons)


# ══════════════════════════════════════════════════════════════════════════════
# Section 5 — Retry Summary
# ══════════════════════════════════════════════════════════════════════════════

def _s5_retries(failed_rows: list[dict]) -> tuple[list[str], dict]:
    total_attempts = sum(int(r.get("retry_count", 0) or 0) for r in failed_rows)
    recovered      = sum(1 for r in failed_rows if r.get("resolved") == "yes")
    perm_fail      = sum(1 for r in failed_rows if r.get("status") == "permanent_failure")
    total_tracked  = len(failed_rows)
    rec_pct        = recovered / total_tracked * 100 if total_tracked else 0.0

    lines = [
        _hdr("RETRY SUMMARY"),
        f"  Total retry attempts:  {total_attempts}",
        f"  Recovered:             {_g(recovered)}"
        f"  ({_color_pct(rec_pct, 30.0, 10.0, lower_is_worse=True)} recovery rate)",
        f"  Permanent failures:    {_r(perm_fail) if perm_fail else _g(perm_fail)}",
    ]
    return lines, {
        "total_attempts": total_attempts,
        "recovered": recovered,
        "recovery_pct": round(rec_pct, 2),
        "permanent_failures": perm_fail,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Section 6 — Category Breakdown
# ══════════════════════════════════════════════════════════════════════════════

_CAT_LABELS: dict[str, str] = {
    "Pokemon":   "Pokemon",
    "OnePiece":  "One Piece",
    "Lorcana":   "Lorcana",
    "Sports":    "Sports Cards",
    "MTG":       "Magic: the Gathering",
    "YuGiOh":   "Yu-Gi-Oh!",
    "HotWheels": "Hot Wheels",
    "Other":     "Other",
}


def _s6_categories(items: list[dict]) -> tuple[list[str], dict]:
    tally: dict[str, dict[str, int]] = defaultdict(lambda: {"active": 0, "sold": 0, "other": 0})
    for i in items:
        ct = i.get("card_type") or "Other"
        s  = i.get("status", "")
        if s == "active":
            tally[ct]["active"] += 1
        elif s == "sold":
            tally[ct]["sold"] += 1
        else:
            tally[ct]["other"] += 1

    lines = [_hdr("CATEGORY BREAKDOWN")]
    for ct in sorted(tally, key=lambda k: -(tally[k]["active"] + tally[k]["sold"])):
        label      = _CAT_LABELS.get(ct, ct)
        active_str = f"{tally[ct]['active']:>3}"
        sold_str   = f"{tally[ct]['sold']:>3}"
        lines.append(
            f"  {label:<26}  {_g(active_str)} active   {_g(sold_str)} sold"
        )

    return lines, {ct: dict(v) for ct, v in tally.items()}


# ══════════════════════════════════════════════════════════════════════════════
# CSV Export
# ══════════════════════════════════════════════════════════════════════════════

def _export_csv(sections_data: dict, path: Path) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["section", "metric", "value"])
            for section, data in sections_data.items():
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, dict):
                            for kk, vv in v.items():
                                w.writerow([section, f"{k} — {kk}", vv])
                        else:
                            w.writerow([section, k, v])
        print(f"\n  Report saved → {path}")
    except Exception as e:
        print(f"\n  [WARN] CSV export failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Future Hooks — placeholders for upcoming platform integrations
# ══════════════════════════════════════════════════════════════════════════════

def import_mercari_sales() -> None:
    """
    Placeholder — import sold order history from Mercari.
    Implementation plan:
      1. Navigate to Mercari sold-items page via CDP.
      2. Scrape order rows (title, sold price, date).
      3. Match each row to a DB item by title or mercari_id.
      4. Call db.mark_sold(item_id, "mercari", sold_price) for each match.
    """
    print("  [PLACEHOLDER] Mercari sales import — not yet implemented.")


def import_depop_sales() -> None:
    """
    Placeholder — import sold order history from Depop.
    Implementation plan:
      1. Navigate to Depop sold items or use Depop API if available.
      2. Scrape order rows (title, sold price, date).
      3. Match to DB items by depop_id or title.
      4. Call db.mark_sold(item_id, "depop", sold_price) for each match.
    """
    print("  [PLACEHOLDER] Depop sales import — not yet implemented.")


def import_ebay_sales() -> None:
    """
    Placeholder — eBay order import via Trading API or Seller Hub CSV.
    Implementation plan:
      1. Download Seller Hub order CSV or use eBay Finding/Trading API.
      2. Parse rows (item ID, title, sold price, date).
      3. Match to DB items by ebay_id.
      4. Call db.mark_sold(item_id, "ebay", sold_price) for each match.
    """
    print("  [PLACEHOLDER] eBay integration — not yet implemented.")


# ══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ══════════════════════════════════════════════════════════════════════════════

_SEP = "═" * 52

_SECTIONS = [
    ("Inventory Overview",  _s1_inventory,  lambda i, f, v: (i, f)),
    ("Platform Performance",_s2_platforms,  lambda i, f, v: (i, f)),
    ("Batch Metrics",       _s3_batch,      lambda i, f, v: (i, f, v)),
    ("Failure Summary",     _s4_failures,   lambda i, f, v: (f,)),
    ("Retry Summary",       _s5_retries,    lambda i, f, v: (f,)),
    ("Category Breakdown",  _s6_categories, lambda i, f, v: (i,)),
]


def run_dashboard(export: bool = False) -> dict:
    """
    Print the full dashboard to stdout and optionally export a CSV report.
    Returns the raw data dict for each section.
    """
    now = datetime.now()

    print()
    print(_b(_c(_SEP, _CYAN)))
    print(_b(_c(
        f"  CardBot Operations Dashboard  —  {now.strftime('%Y-%m-%d %H:%M')}",
        _CYAN,
    )))
    print(_b(_c(_SEP, _CYAN)))

    items       = _load_items()
    failed_rows = _load_failed()
    inv_rows    = _load_inventory()
    all_data    = {}

    for label, fn, arg_selector in _SECTIONS:
        args = arg_selector(items, failed_rows, inv_rows)
        lines, data = fn(*args)
        print("\n" + "\n".join(lines))
        all_data[label] = data

    print("\n" + _b(_c(_SEP, _CYAN)))

    if export:
        date_str = now.strftime("%Y%m%d")
        _export_csv(all_data, REPORTS_DIR / f"daily_report_{date_str}.csv")

    return all_data


if __name__ == "__main__":
    run_dashboard(export="--export" in sys.argv or "--csv" in sys.argv)
