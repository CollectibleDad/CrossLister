"""
Generates daily summary reports: sales, revenue, new listings, active inventory.
Saves to reports/ folder and prints to console.
"""
import logging
from datetime import datetime, date
from pathlib import Path

import database as db

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).parent / "reports"
REPORTS_DIR.mkdir(exist_ok=True)


def generate_daily_report(report_date: str = None) -> str:
    report_date = report_date or date.today().isoformat()
    stats = db.get_daily_stats(report_date)
    all_items = db.get_all_items()

    sold_today = [i for i in all_items if i["date_sold"] == report_date]
    listed_today = [i for i in all_items if i["date_listed"] == report_date]
    active_items = [i for i in all_items if i["status"] == "active"]

    revenue_this_month = _calculate_monthly_revenue(all_items)
    total_revenue = sum(i["sold_price"] for i in all_items if i["sold_price"])
    total_sold = sum(1 for i in all_items if i["status"] == "sold")

    report = _build_report(
        report_date=report_date,
        stats=stats,
        sold_today=sold_today,
        listed_today=listed_today,
        active_items=active_items,
        revenue_this_month=revenue_this_month,
        total_revenue=total_revenue,
        total_sold=total_sold,
        all_items=all_items,
    )

    report_file = REPORTS_DIR / f"report_{report_date}.txt"
    report_file.write_text(report, encoding="utf-8")
    logger.info("Report saved to %s", report_file)

    print(report)
    return report


def _build_report(
    report_date, stats, sold_today, listed_today, active_items,
    revenue_this_month, total_revenue, total_sold, all_items
) -> str:
    separator = "=" * 55
    thin_sep = "-" * 55

    lines = [
        separator,
        f"  CROSSLISTER DAILY REPORT — {report_date}",
        f"  Generated: {datetime.now().strftime('%I:%M %p')}",
        separator,
        "",
        "TODAY'S SUMMARY",
        thin_sep,
        f"  Items Sold Today:    {len(sold_today)}",
        f"  Revenue Today:       ${stats['revenue']:.2f}",
        f"  New Items Listed:    {len(listed_today)}",
        f"  Active Inventory:    {len(active_items)}",
        "",
    ]

    if sold_today:
        lines += ["TODAY'S SALES", thin_sep]
        for item in sold_today:
            price = item["sold_price"] or 0
            platform = item["platform_sold"] or "unknown"
            lines.append(f"  ✓ {item['title'] or item['card_name'] or 'Unknown'}  ${price:.2f}  [{platform.upper()}]")
        lines.append("")

    if listed_today:
        lines += ["NEW LISTINGS TODAY", thin_sep]
        for item in listed_today:
            platforms = []
            if item["mercari_id"]:
                platforms.append("Mercari")
            if item["depop_id"]:
                platforms.append("Depop")
            if item["ebay_id"]:
                platforms.append("eBay")
            price = item["asking_price"] or 0
            plat_str = ", ".join(platforms) if platforms else "Pending"
            lines.append(f"  + {item['title'] or item['card_name'] or 'Unknown'}  ${price:.2f}  [{plat_str}]")
        lines.append("")

    lines += [
        "INVENTORY BREAKDOWN BY TYPE",
        thin_sep,
    ]
    if stats["by_type"]:
        for card_type, count in sorted(stats["by_type"].items(), key=lambda x: -x[1]):
            lines.append(f"  {card_type:<15} {count:>4} active")
    else:
        lines.append("  No active inventory")
    lines.append("")

    platform_counts = _count_by_platform(active_items)
    lines += [
        "ACTIVE LISTINGS BY PLATFORM",
        thin_sep,
        f"  Mercari:  {platform_counts['mercari']:>4}",
        f"  Depop:    {platform_counts['depop']:>4}",
        f"  eBay:     {platform_counts['ebay']:>4}",
        "",
    ]

    month_label = datetime.now().strftime("%B %Y")
    lines += [
        "ALL-TIME STATISTICS",
        thin_sep,
        f"  Total Items Sold:    {total_sold}",
        f"  Total Revenue:       ${total_revenue:.2f}",
        f"  Revenue This Month:  ${revenue_this_month:.2f}",
        "",
        separator,
        "",
    ]

    return "\n".join(lines)


def _count_by_platform(active_items) -> dict:
    counts = {"mercari": 0, "depop": 0, "ebay": 0}
    for item in active_items:
        if item["mercari_id"]:
            counts["mercari"] += 1
        if item["depop_id"]:
            counts["depop"] += 1
        if item["ebay_id"]:
            counts["ebay"] += 1
    return counts


def _calculate_monthly_revenue(all_items) -> float:
    current_month = datetime.now().strftime("%Y-%m")
    return sum(
        i["sold_price"]
        for i in all_items
        if i["sold_price"] and i["date_sold"] and i["date_sold"].startswith(current_month)
    )


def generate_inventory_csv(output_path: str = None) -> str:
    output_path = output_path or str(REPORTS_DIR / f"inventory_{date.today().isoformat()}.csv")
    items = db.get_all_items()
    import csv
    fields = [
        "id", "filename", "title", "card_name", "card_number", "set_name",
        "rarity", "card_type", "condition", "mercari_id", "depop_id", "ebay_id",
        "date_listed", "date_sold", "platform_sold", "asking_price", "sold_price", "status"
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(dict(item) for item in items)
    logger.info("Inventory CSV saved to %s", output_path)
    return output_path


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Report date YYYY-MM-DD (default: today)")
    parser.add_argument("--csv", action="store_true", help="Also export inventory CSV")
    args = parser.parse_args()
    db.init_db()
    generate_daily_report(args.date)
    if args.csv:
        path = generate_inventory_csv()
        print(f"Inventory CSV: {path}")
