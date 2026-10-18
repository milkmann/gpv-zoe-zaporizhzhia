#!/usr/bin/env python3
"""
Schedule PNG Renderer for Zaporizhzhiaoblenergo (ZOE).
Generates visual schedule cards (PNG) for each queue and an overall summary table.
Uses caching (SHA256 hashes & date check) to avoid regenerating unchanged images.
"""

import os
import sys
import json
import hashlib
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

try:
    import matplotlib
    matplotlib.use("Agg")  # Non-interactive headless backend
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
except ImportError:
    print("[ERROR] matplotlib is required: pip install matplotlib")
    sys.exit(1)

TZ_KYIV = ZoneInfo("Europe/Kyiv")

# Palette matching clean modern schedule charts
COLOR_OUTAGE = "#5C82BA"      # Електроенергія відсутня (синій)
COLOR_MAYBE = "#F4D03F"       # Можливе відключення (жовтий)
COLOR_AVAILABLE = "#FFFFFF"   # Світло є (білий)
COLOR_BORDER = "#808080"      # Межа клітинок (сірий)
COLOR_HEADER = "#EAEAEA"      # Заголовок годин
COLOR_LABEL = "#DFDFDF"       # Заголовок дати / черги

SLOTS = list(range(1, 25))
HOURS = [f"{i:02d}-{i+1:02d}" for i in range(24)]
HOURS_SPLIT = [f"{i:02d}\n-\n{i+1:02d}" for i in range(24)]

ALL_QUEUES = [
    "GPV1.1", "GPV1.2",
    "GPV2.1", "GPV2.2",
    "GPV3.1", "GPV3.2",
    "GPV4.1", "GPV4.2",
    "GPV5.1", "GPV5.2",
    "GPV6.1", "GPV6.2",
]

MONTHS_UK = {
    1: "січня", 2: "лютого", 3: "березня", 4: "квітня",
    5: "травня", 6: "червня", 7: "липня", 8: "серпня",
    9: "вересня", 10: "жовтня", 11: "листопада", 12: "грудня"
}


def queue_filename(gpv_key: str) -> str:
    """Convert GPV1.1 to gpv-1-1-emergency.png"""
    cleaned = gpv_key.replace("GPV", "").replace(".", "-").lstrip("-")
    return f"gpv-{cleaned}-emergency.png"


def queue_title(gpv_key: str) -> str:
    """Convert GPV1.1 to 'Черга 1.1'"""
    num = gpv_key.replace("GPV", "")
    return f"Черга {num}"


def calculate_hash(data: dict) -> str:
    data_str = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(data_str.encode("utf-8")).hexdigest()


def check_cache(hash_dir: Path, name: str, current_hash: str, current_date: str, target_file: Path) -> bool:
    """Return True if regeneration is needed, False if cache is valid."""
    if not target_file.exists():
        return True

    hash_file = hash_dir / f"{name}.hash"
    date_file = hash_dir / f"{name}.date"

    if not hash_file.exists() or not date_file.exists():
        return True

    try:
        saved_hash = hash_file.read_text(encoding="utf-8").strip()
        saved_date = date_file.read_text(encoding="utf-8").strip()
        if saved_hash == current_hash and saved_date == current_date:
            return False
    except Exception:
        return True
    return True


def save_cache(hash_dir: Path, name: str, current_hash: str, current_date: str):
    hash_dir.mkdir(parents=True, exist_ok=True)
    (hash_dir / f"{name}.hash").write_text(current_hash, encoding="utf-8")
    (hash_dir / f"{name}.date").write_text(current_date, encoding="utf-8")


def draw_slot(ax, x, y, w, h, state: str):
    """Draw a single hour slot with half-hour support."""
    # Base white cell
    ax.add_patch(Rectangle((x, y), w, h, linewidth=0, facecolor=COLOR_AVAILABLE))

    if state == "no":
        ax.add_patch(Rectangle((x, y), w, h, linewidth=0, facecolor=COLOR_OUTAGE))
    elif state == "maybe":
        ax.add_patch(Rectangle((x, y), w, h, linewidth=0, facecolor=COLOR_MAYBE))
    elif state == "first":
        # Outage in first 30 mins
        ax.add_patch(Rectangle((x, y), w / 2, h, linewidth=0, facecolor=COLOR_OUTAGE))
    elif state == "second":
        # Outage in second 30 mins
        ax.add_patch(Rectangle((x + w / 2, y), w / 2, h, linewidth=0, facecolor=COLOR_OUTAGE))

    # Border
    ax.add_patch(Rectangle((x, y), w, h, linewidth=1, edgecolor=COLOR_BORDER, facecolor="none"))


def render_single_queue(today_slots: dict, tomorrow_slots: dict | None, gkey: str,
                        today_str: str, tomorrow_str: str | None, last_updated: str,
                        output_path: Path):
    """Render 2-day schedule for a single queue."""
    fig, ax = plt.subplots(figsize=(20, 3.6), dpi=130)
    fig.patch.set_facecolor("#FFFFFF")
    ax.set_facecolor("#FFFFFF")

    cell_w = 1.0
    cell_h = 0.52
    label_w = 2.4
    header_h = 0.95

    has_tomorrow = bool(tomorrow_slots)
    rows_count = 2 if has_tomorrow else 1

    table_width = label_w + 24 * cell_w
    table_height = header_h + rows_count * cell_h

    y_pos = 0

    # Row 0: Hours Header
    rect = Rectangle((0, y_pos), label_w, header_h, linewidth=1, edgecolor=COLOR_BORDER, facecolor=COLOR_HEADER)
    ax.add_patch(rect)
    ax.text(label_w / 2, y_pos + header_h / 2, "Дата", fontsize=12, ha="center", va="center", fontweight="bold")

    for i in range(24):
        x = label_w + i * cell_w
        rect = Rectangle((x, y_pos), cell_w, header_h, linewidth=1, edgecolor=COLOR_BORDER, facecolor=COLOR_HEADER)
        ax.add_patch(rect)
        ax.text(x + cell_w / 2, y_pos + header_h / 2, HOURS_SPLIT[i], fontsize=10.5, ha="center", va="center", fontweight="bold", linespacing=1.3)

    y_pos += header_h

    # Row 1: Today
    rect = Rectangle((0, y_pos), label_w, cell_h, linewidth=1, edgecolor=COLOR_BORDER, facecolor=COLOR_LABEL)
    ax.add_patch(rect)
    ax.text(label_w / 2, y_pos + cell_h / 2, today_str, fontsize=11.5, ha="center", va="center", fontweight="bold")

    for i, s_num in enumerate(SLOTS):
        x = label_w + i * cell_w
        state = today_slots.get(str(s_num), "yes")
        draw_slot(ax, x, y_pos, cell_w, cell_h, state)

    y_pos += cell_h

    # Row 2: Tomorrow (if available)
    if has_tomorrow:
        rect = Rectangle((0, y_pos), label_w, cell_h, linewidth=1, edgecolor=COLOR_BORDER, facecolor=COLOR_LABEL)
        ax.add_patch(rect)
        ax.text(label_w / 2, y_pos + cell_h / 2, tomorrow_str, fontsize=11.5, ha="center", va="center", fontweight="bold")

        for i, s_num in enumerate(SLOTS):
            x = label_w + i * cell_w
            state = tomorrow_slots.get(str(s_num), "yes")
            draw_slot(ax, x, y_pos, cell_w, cell_h, state)

    ax.set_xlim(0, table_width)
    ax.set_ylim(0, table_height)
    ax.invert_yaxis()
    ax.axis("off")

    # Header title
    fig.text(0.04, 0.96, "Графік відключень: Запоріжжя та область", fontsize=17, fontweight="bold")

    # Queue badge pill
    q_badge = queue_title(gkey)
    fig.text(0.96, 0.96, q_badge, fontsize=17, fontweight="bold",
             bbox=dict(boxstyle="round,pad=0.5", facecolor="#FFD700", edgecolor="#000000", linewidth=1.5),
             ha="right")

    # Legend at bottom
    leg_y = 0.03
    leg_x = 0.05
    box_w = 0.02
    box_h = 0.06

    # 1. Світло є
    fig.patches.append(Rectangle((leg_x, leg_y), box_w, box_h, linewidth=0.8, edgecolor=COLOR_BORDER, facecolor=COLOR_AVAILABLE, transform=fig.transFigure, clip_on=False))
    fig.text(leg_x + box_w + 0.007, leg_y + box_h / 2, "Електроенергія є", fontsize=10.5, va="center")

    # 2. Світла немає
    leg_x += 0.17
    fig.patches.append(Rectangle((leg_x, leg_y), box_w, box_h, linewidth=0.8, edgecolor=COLOR_BORDER, facecolor=COLOR_OUTAGE, transform=fig.transFigure, clip_on=False))
    fig.text(leg_x + box_w + 0.007, leg_y + box_h / 2, "Відключення", fontsize=10.5, va="center")

    # 3. Перші 30 хв
    leg_x += 0.15
    fig.patches.append(Rectangle((leg_x, leg_y), box_w / 2, box_h, linewidth=0, facecolor=COLOR_OUTAGE, transform=fig.transFigure, clip_on=False))
    fig.patches.append(Rectangle((leg_x + box_w / 2, leg_y), box_w / 2, box_h, linewidth=0, facecolor=COLOR_AVAILABLE, transform=fig.transFigure, clip_on=False))
    fig.patches.append(Rectangle((leg_x, leg_y), box_w, box_h, linewidth=0.8, edgecolor=COLOR_BORDER, facecolor="none", transform=fig.transFigure, clip_on=False))
    fig.text(leg_x + box_w + 0.007, leg_y + box_h / 2, "Відключення перші 30 хв.", fontsize=10.5, va="center")

    # 4. Другі 30 хв
    leg_x += 0.22
    fig.patches.append(Rectangle((leg_x, leg_y), box_w / 2, box_h, linewidth=0, facecolor=COLOR_AVAILABLE, transform=fig.transFigure, clip_on=False))
    fig.patches.append(Rectangle((leg_x + box_w / 2, leg_y), box_w / 2, box_h, linewidth=0, facecolor=COLOR_OUTAGE, transform=fig.transFigure, clip_on=False))
    fig.patches.append(Rectangle((leg_x, leg_y), box_w, box_h, linewidth=0.8, edgecolor=COLOR_BORDER, facecolor="none", transform=fig.transFigure, clip_on=False))
    fig.text(leg_x + box_w + 0.007, leg_y + box_h / 2, "Відключення другі 30 хв.", fontsize=10.5, va="center")

    # Update timestamp
    if last_updated:
        fig.text(0.96, leg_y + box_h / 2, f"Опубліковано: {last_updated}", fontsize=10, ha="right", va="center", style="italic", color="#555555")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, facecolor="#FFFFFF", dpi=130, bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)


def render_all_queues_summary(day_data: dict, date_str: str, last_updated: str, output_path: Path):
    """Render overview chart of all 12 queues on a single day."""
    fig, ax = plt.subplots(figsize=(20, 8.5), dpi=130)
    fig.patch.set_facecolor("#FFFFFF")
    ax.set_facecolor("#FFFFFF")

    cell_w = 1.0
    cell_h = 0.48
    label_w = 2.2
    header_h = 0.85

    table_width = label_w + 24 * cell_w
    table_height = header_h + len(ALL_QUEUES) * cell_h

    y_pos = 0

    # Row 0: Hours
    rect = Rectangle((0, y_pos), label_w, header_h, linewidth=1, edgecolor=COLOR_BORDER, facecolor=COLOR_HEADER)
    ax.add_patch(rect)
    ax.text(label_w / 2, y_pos + header_h / 2, "Черга", fontsize=12, ha="center", va="center", fontweight="bold")

    for i in range(24):
        x = label_w + i * cell_w
        rect = Rectangle((x, y_pos), cell_w, header_h, linewidth=1, edgecolor=COLOR_BORDER, facecolor=COLOR_HEADER)
        ax.add_patch(rect)
        ax.text(x + cell_w / 2, y_pos + header_h / 2, HOURS_SPLIT[i], fontsize=10, ha="center", va="center", fontweight="bold", linespacing=1.2)

    y_pos += header_h

    # 12 Queue rows
    for gkey in ALL_QUEUES:
        q_label = queue_title(gkey)
        q_slots = day_data.get(gkey, {str(s): "yes" for s in SLOTS})

        rect = Rectangle((0, y_pos), label_w, cell_h, linewidth=1, edgecolor=COLOR_BORDER, facecolor=COLOR_LABEL)
        ax.add_patch(rect)
        ax.text(label_w / 2, y_pos + cell_h / 2, q_label, fontsize=11, ha="center", va="center", fontweight="bold")

        for i, s_num in enumerate(SLOTS):
            x = label_w + i * cell_w
            state = q_slots.get(str(s_num), "yes")
            draw_slot(ax, x, y_pos, cell_w, cell_h, state)

        y_pos += cell_h

    ax.set_xlim(0, table_width)
    ax.set_ylim(0, table_height)
    ax.invert_yaxis()
    ax.axis("off")

    # Header title
    fig.text(0.04, 0.97, f"Графік відключень (всі черги): Запоріжжя та область", fontsize=17, fontweight="bold")
    fig.text(0.96, 0.97, f"Дата: {date_str}", fontsize=16, fontweight="bold", ha="right")

    # Legend at bottom
    leg_y = 0.02
    leg_x = 0.05
    box_w = 0.02
    box_h = 0.025

    fig.patches.append(Rectangle((leg_x, leg_y), box_w, box_h, linewidth=0.8, edgecolor=COLOR_BORDER, facecolor=COLOR_AVAILABLE, transform=fig.transFigure, clip_on=False))
    fig.text(leg_x + box_w + 0.007, leg_y + box_h / 2, "Електроенергія є", fontsize=10.5, va="center")

    leg_x += 0.17
    fig.patches.append(Rectangle((leg_x, leg_y), box_w, box_h, linewidth=0.8, edgecolor=COLOR_BORDER, facecolor=COLOR_OUTAGE, transform=fig.transFigure, clip_on=False))
    fig.text(leg_x + box_w + 0.007, leg_y + box_h / 2, "Відключення", fontsize=10.5, va="center")

    leg_x += 0.15
    fig.patches.append(Rectangle((leg_x, leg_y), box_w / 2, box_h, linewidth=0, facecolor=COLOR_OUTAGE, transform=fig.transFigure, clip_on=False))
    fig.patches.append(Rectangle((leg_x + box_w / 2, leg_y), box_w / 2, box_h, linewidth=0, facecolor=COLOR_AVAILABLE, transform=fig.transFigure, clip_on=False))
    fig.patches.append(Rectangle((leg_x, leg_y), box_w, box_h, linewidth=0.8, edgecolor=COLOR_BORDER, facecolor="none", transform=fig.transFigure, clip_on=False))
    fig.text(leg_x + box_w + 0.007, leg_y + box_h / 2, "Відключення перші 30 хв.", fontsize=10.5, va="center")

    leg_x += 0.22
    fig.patches.append(Rectangle((leg_x, leg_y), box_w / 2, box_h, linewidth=0, facecolor=COLOR_AVAILABLE, transform=fig.transFigure, clip_on=False))
    fig.patches.append(Rectangle((leg_x + box_w / 2, leg_y), box_w / 2, box_h, linewidth=0, facecolor=COLOR_OUTAGE, transform=fig.transFigure, clip_on=False))
    fig.patches.append(Rectangle((leg_x, leg_y), box_w, box_h, linewidth=0.8, edgecolor=COLOR_BORDER, facecolor="none", transform=fig.transFigure, clip_on=False))
    fig.text(leg_x + box_w + 0.007, leg_y + box_h / 2, "Відключення другі 30 хв.", fontsize=10.5, va="center")

    if last_updated:
        fig.text(0.96, leg_y + box_h / 2, f"Опубліковано: {last_updated}", fontsize=10, ha="right", va="center", style="italic", color="#555555")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, facecolor="#FFFFFF", dpi=130, bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description="Render ZOE Outage Schedule PNGs")
    parser.add_argument("--json", required=True, help="Path to Zaporizhzhiaoblenergo.json")
    parser.add_argument("--out", default="images/Zaporizhzhiaoblenergo", help="Output directory")
    args = parser.parse_args()

    json_path = Path(args.json)
    out_dir = Path(args.out)
    hash_dir = out_dir / "hash"

    if not json_path.exists():
        print(f"[ERROR] JSON not found: {json_path}")
        sys.exit(1)

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    fact = data.get("fact", {})
    fact_data = fact.get("data", {})
    last_updated = fact.get("update", "")
    today_ts = str(fact.get("today", ""))

    if not today_ts or today_ts not in fact_data:
        # Fallback to current midnight in Kyiv
        now_kyiv = datetime.now(TZ_KYIV).replace(hour=0, minute=0, second=0, microsecond=0)
        today_ts = str(int(now_kyiv.timestamp()))

    tomorrow_ts = str(int(today_ts) + 86400)

    today_dt = datetime.fromtimestamp(int(today_ts), tz=TZ_KYIV)
    tomorrow_dt = datetime.fromtimestamp(int(tomorrow_ts), tz=TZ_KYIV)

    today_str = f"{today_dt.day:02d} {MONTHS_UK[today_dt.month]}"
    tomorrow_str = f"{tomorrow_dt.day:02d} {MONTHS_UK[tomorrow_dt.month]}"
    today_code = today_dt.strftime("%Y-%m-%d")

    today_data = fact_data.get(today_ts, {})
    tomorrow_data = fact_data.get(tomorrow_ts)

    stats = {"checked": 0, "generated": 0, "skipped": 0}

    # 1. Render individual queue cards
    for gkey in ALL_QUEUES:
        stats["checked"] += 1
        q_today = today_data.get(gkey, {str(s): "yes" for s in SLOTS})
        q_tomorrow = tomorrow_data.get(gkey) if tomorrow_data else None

        q_hash = calculate_hash({"today": q_today, "tomorrow": q_tomorrow, "update": last_updated})
        fname = queue_filename(gkey)
        target_png = out_dir / fname

        if check_cache(hash_dir, gkey, q_hash, today_code, target_png):
            render_single_queue(q_today, q_tomorrow, gkey, today_str, tomorrow_str if q_tomorrow else None, last_updated, target_png)
            save_cache(hash_dir, gkey, q_hash, today_code)
            # Also copy to root images dir if nested
            if out_dir.name != "images" and out_dir.parent.name == "images":
                import shutil
                shutil.copy2(target_png, out_dir.parent / fname)
            stats["generated"] += 1
            print(f"[RENDERED] {fname}")
        else:
            stats["skipped"] += 1

    # 2. Render all-queues overview for today
    today_all_hash = calculate_hash({"data": today_data, "update": last_updated})
    today_all_png = out_dir / "gpv-all-today.png"
    stats["checked"] += 1
    if check_cache(hash_dir, "gpv-all-today", today_all_hash, today_code, today_all_png):
        render_all_queues_summary(today_data, today_str, last_updated, today_all_png)
        save_cache(hash_dir, "gpv-all-today", today_all_hash, today_code)
        if out_dir.name != "images" and out_dir.parent.name == "images":
            import shutil
            shutil.copy2(today_all_png, out_dir.parent / "gpv-all-today.png")
        stats["generated"] += 1
        print(f"[RENDERED] gpv-all-today.png")
    else:
        stats["skipped"] += 1

    # 3. Render all-queues overview for tomorrow if tomorrow data exists
    if tomorrow_data:
        stats["checked"] += 1
        tom_all_hash = calculate_hash({"data": tomorrow_data, "update": last_updated})
        tom_all_png = out_dir / "gpv-all-tomorrow.png"
        if check_cache(hash_dir, "gpv-all-tomorrow", tom_all_hash, today_code, tom_all_png):
            render_all_queues_summary(tomorrow_data, tomorrow_str, last_updated, tom_all_png)
            save_cache(hash_dir, "gpv-all-tomorrow", tom_all_hash, today_code)
            if out_dir.name != "images" and out_dir.parent.name == "images":
                import shutil
                shutil.copy2(tom_all_png, out_dir.parent / "gpv-all-tomorrow.png")
            stats["generated"] += 1
            print(f"[RENDERED] gpv-all-tomorrow.png")
        else:
            stats["skipped"] += 1

    print(f"\n[DONE] Checked: {stats['checked']}, Generated: {stats['generated']}, Skipped: {stats['skipped']}")


if __name__ == "__main__":
    main()
