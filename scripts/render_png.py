#!/usr/bin/env python3
"""
Schedule PNG Renderer for Zaporizhzhiaoblenergo (ZOE).
Generates visual schedule cards (PNG) for each queue and an overall summary table.
Supports Visual Diff (red border for increased outages, green border for decreased outages).
Uses caching (SHA256 hashes & date check) to avoid regenerating unchanged images.
"""

import os
import sys
import json
import hashlib
import argparse
import subprocess
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

# Visual Diff highlight borders
COLOR_DIFF_MORE = "#D32F2F"   # Більше відключень (червона рамка)
COLOR_DIFF_LESS = "#2E7D32"   # Менше відключень (зелена рамка)

SEVERITY = {"yes": 0, "maybe": 1, "no": 2}

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


def slot_to_halves(state: str) -> tuple[str, str]:
    """Convert DTEK hour state to (first_half, second_half)."""
    if state == "no":
        return ("no", "no")
    if state == "first":
        return ("no", "yes")
    if state == "second":
        return ("yes", "no")
    if state == "maybe":
        return ("maybe", "maybe")
    if state == "mfirst":
        return ("maybe", "yes")
    if state == "msecond":
        return ("yes", "maybe")
    return ("yes", "yes")


def compute_queue_diff(curr_slots: dict, prev_slots: dict | None) -> dict[str, tuple[str | None, str | None]]:
    """
    Compare curr_slots against prev_slots for 24 hours.
    Returns dict mapping hour_str ('1'..'24') -> (half1_diff, half2_diff),
    where diff is 'more', 'less', or None.
    Only hours with at least one non-None half are included.
    """
    if not prev_slots:
        return {}

    diff_map = {}
    for h in range(1, 25):
        h_str = str(h)
        s_curr = curr_slots.get(h_str, "yes")
        s_prev = prev_slots.get(h_str, "yes")
        if s_curr == s_prev:
            continue
        c_h1, c_h2 = slot_to_halves(s_curr)
        p_h1, p_h2 = slot_to_halves(s_prev)

        def diff_dir(c, p):
            c_sev = SEVERITY.get(c, 0)
            p_sev = SEVERITY.get(p, 0)
            if c_sev > p_sev:
                return "more"
            elif c_sev < p_sev:
                return "less"
            return None

        d1 = diff_dir(c_h1, p_h1)
        d2 = diff_dir(c_h2, p_h2)
        if d1 or d2:
            diff_map[h_str] = (d1, d2)
    return diff_map


def compute_schedule_diff(curr_day_data: dict, prev_day_data: dict | None) -> dict[str, dict]:
    """Compute diff for all 12 queues in a day."""
    if not prev_day_data:
        return {}
    res = {}
    for q in ALL_QUEUES:
        q_curr = curr_day_data.get(q, {})
        q_prev = prev_day_data.get(q, {})
        q_diff = compute_queue_diff(q_curr, q_prev)
        if q_diff:
            res[q] = q_diff
    return res


def load_previous_json(current_json_path: Path, prev_arg: str | None = None) -> dict | None:
    """
    Attempt to load previous schedule JSON for diff comparison.
    Checks:
    1. Explicit --prev-json argument if supplied
    2. Sidecar file data/<stem>_prev.json
    3. Git history for the previous revision of the json file
    """
    if prev_arg:
        p = Path(prev_arg)
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[WARN] Failed to read --prev-json: {e}")

    # Check sidecar file in same directory
    sidecar = current_json_path.parent / f"{current_json_path.stem}_prev.json"
    if sidecar.exists():
        try:
            with open(sidecar, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    # Check git history
    try:
        repo_dir = current_json_path.parent.parent
        rel_path = f"data/{current_json_path.name}"
        cmd = ["git", "log", "-n", "3", "--format=%H", "--", rel_path]
        p = subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True)
        if p.returncode == 0:
            commits = [line.strip() for line in p.stdout.splitlines() if line.strip()]
            diff_cmd = ["git", "diff", "--name-only", "--", rel_path]
            d_res = subprocess.run(diff_cmd, cwd=repo_dir, capture_output=True, text=True)
            is_wt_mod = rel_path in d_res.stdout or current_json_path.name in d_res.stdout

            target_commit = commits[0] if is_wt_mod else (commits[1] if len(commits) > 1 else None)
            if target_commit:
                s = subprocess.run(["git", "show", f"{target_commit}:{rel_path}"], cwd=repo_dir, capture_output=True, text=True)
                if s.returncode == 0 and s.stdout.strip():
                    return json.loads(s.stdout)
    except Exception:
        pass

    return None


def draw_slot(ax, x, y, w, h, state: str, diff: tuple[str | None, str | None] | None = None):
    """Draw a single hour slot with half-hour support and optional visual diff borders."""
    # Base white cell
    ax.add_patch(Rectangle((x, y), w, h, linewidth=0, facecolor=COLOR_AVAILABLE, zorder=1))

    if state == "no":
        ax.add_patch(Rectangle((x, y), w, h, linewidth=0, facecolor=COLOR_OUTAGE, zorder=2))
    elif state == "maybe":
        ax.add_patch(Rectangle((x, y), w, h, linewidth=0, facecolor=COLOR_MAYBE, zorder=2))
    elif state == "first":
        ax.add_patch(Rectangle((x, y), w / 2, h, linewidth=0, facecolor=COLOR_OUTAGE, zorder=2))
    elif state == "second":
        ax.add_patch(Rectangle((x + w / 2, y), w / 2, h, linewidth=0, facecolor=COLOR_OUTAGE, zorder=2))
    elif state == "mfirst":
        ax.add_patch(Rectangle((x, y), w / 2, h, linewidth=0, facecolor=COLOR_MAYBE, zorder=2))
    elif state == "msecond":
        ax.add_patch(Rectangle((x + w / 2, y), w / 2, h, linewidth=0, facecolor=COLOR_MAYBE, zorder=2))

    # Standard cell border
    ax.add_patch(Rectangle((x, y), w, h, linewidth=1, edgecolor=COLOR_BORDER, facecolor="none", zorder=3))

    # Overlay visual diff highlight border if present
    if diff:
        d1, d2 = diff
        if d1 and d1 == d2:
            # Full cell changed in the same direction
            color = COLOR_DIFF_MORE if d1 == "more" else COLOR_DIFF_LESS
            ax.add_patch(Rectangle((x, y), w, h, linewidth=2.5, edgecolor=color, facecolor="none", zorder=5))
        else:
            if d1:
                color = COLOR_DIFF_MORE if d1 == "more" else COLOR_DIFF_LESS
                ax.add_patch(Rectangle((x, y), w / 2, h, linewidth=2.5, edgecolor=color, facecolor="none", zorder=5))
            if d2:
                color = COLOR_DIFF_MORE if d2 == "more" else COLOR_DIFF_LESS
                ax.add_patch(Rectangle((x + w / 2, y), w / 2, h, linewidth=2.5, edgecolor=color, facecolor="none", zorder=5))


def render_single_queue(today_slots: dict, tomorrow_slots: dict | None, gkey: str,
                        today_str: str, tomorrow_str: str | None, last_updated: str,
                        output_path: Path, today_diff: dict | None = None, tomorrow_diff: dict | None = None):
    """Render 2-day schedule for a single queue with Visual Diff support."""
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
        diff = today_diff.get(str(s_num)) if today_diff else None
        draw_slot(ax, x, y_pos, cell_w, cell_h, state, diff=diff)

    y_pos += cell_h

    # Row 2: Tomorrow (if available)
    if has_tomorrow:
        rect = Rectangle((0, y_pos), label_w, cell_h, linewidth=1, edgecolor=COLOR_BORDER, facecolor=COLOR_LABEL)
        ax.add_patch(rect)
        ax.text(label_w / 2, y_pos + cell_h / 2, tomorrow_str, fontsize=11.5, ha="center", va="center", fontweight="bold")

        for i, s_num in enumerate(SLOTS):
            x = label_w + i * cell_w
            state = tomorrow_slots.get(str(s_num), "yes")
            diff = tomorrow_diff.get(str(s_num)) if tomorrow_diff else None
            draw_slot(ax, x, y_pos, cell_w, cell_h, state, diff=diff)

    pad_x = 0.04
    pad_y = 0.04
    ax.set_xlim(-pad_x, table_width + pad_x)
    ax.set_ylim(table_height + pad_y, -pad_y)
    ax.axis("off")

    # Clean outer border framing the table
    ax.add_patch(Rectangle((0, 0), table_width, table_height, linewidth=1.5, edgecolor=COLOR_BORDER, facecolor="none"))

    # Header title
    fig.text(0.04, 0.96, "Графік відключень: Запоріжжя та область", fontsize=17, fontweight="bold")

    # Queue badge pill
    q_badge = queue_title(gkey)
    fig.text(0.96, 0.96, q_badge, fontsize=17, fontweight="bold",
             bbox=dict(boxstyle="round,pad=0.5", facecolor="#FFD700", edgecolor="#000000", linewidth=1.5),
             ha="right")

    # Legend at bottom
    leg_y = 0.03
    leg_x = 0.035
    box_w = 0.018
    box_h = 0.055

    # 1. Електроенергія є
    fig.patches.append(Rectangle((leg_x, leg_y), box_w, box_h, linewidth=0.8, edgecolor=COLOR_BORDER, facecolor=COLOR_AVAILABLE, transform=fig.transFigure, clip_on=False))
    fig.text(leg_x + box_w + 0.005, leg_y + box_h / 2, "Електроенергія є", fontsize=10, va="center")

    # 2. Відключення
    leg_x += 0.125
    fig.patches.append(Rectangle((leg_x, leg_y), box_w, box_h, linewidth=0.8, edgecolor=COLOR_BORDER, facecolor=COLOR_OUTAGE, transform=fig.transFigure, clip_on=False))
    fig.text(leg_x + box_w + 0.005, leg_y + box_h / 2, "Відключення", fontsize=10, va="center")

    # 3. Можливе відключення
    leg_x += 0.115
    fig.patches.append(Rectangle((leg_x, leg_y), box_w, box_h, linewidth=0.8, edgecolor=COLOR_BORDER, facecolor=COLOR_MAYBE, transform=fig.transFigure, clip_on=False))
    fig.text(leg_x + box_w + 0.005, leg_y + box_h / 2, "Можливе відключення", fontsize=10, va="center")

    # 4. Більше відключень (diff)
    leg_x += 0.155
    fig.patches.append(Rectangle((leg_x, leg_y), box_w, box_h, linewidth=2.0, edgecolor=COLOR_DIFF_MORE, facecolor=COLOR_AVAILABLE, transform=fig.transFigure, clip_on=False))
    fig.text(leg_x + box_w + 0.005, leg_y + box_h / 2, "Більше відключень", fontsize=10, va="center")

    # 5. Менше відключень (diff)
    leg_x += 0.15
    fig.patches.append(Rectangle((leg_x, leg_y), box_w, box_h, linewidth=2.0, edgecolor=COLOR_DIFF_LESS, facecolor=COLOR_AVAILABLE, transform=fig.transFigure, clip_on=False))
    fig.text(leg_x + box_w + 0.005, leg_y + box_h / 2, "Менше відключень", fontsize=10, va="center")

    # Update timestamp
    if last_updated:
        fig.text(0.965, leg_y + box_h / 2, f"Опубліковано: {last_updated}", fontsize=10, ha="right", va="center", style="italic", color="#555555")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, facecolor="#FFFFFF", dpi=130, bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)


def render_all_queues_summary(day_data: dict, date_str: str, last_updated: str, output_path: Path, day_diff: dict | None = None):
    """Render overview chart of all 12 queues on a single day with Visual Diff support."""
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
        q_diff = day_diff.get(gkey, {}) if day_diff else {}

        rect = Rectangle((0, y_pos), label_w, cell_h, linewidth=1, edgecolor=COLOR_BORDER, facecolor=COLOR_LABEL)
        ax.add_patch(rect)
        ax.text(label_w / 2, y_pos + cell_h / 2, q_label, fontsize=11, ha="center", va="center", fontweight="bold")

        for i, s_num in enumerate(SLOTS):
            x = label_w + i * cell_w
            state = q_slots.get(str(s_num), "yes")
            diff = q_diff.get(str(s_num))
            draw_slot(ax, x, y_pos, cell_w, cell_h, state, diff=diff)

        y_pos += cell_h

    pad_x = 0.04
    pad_y = 0.04
    ax.set_xlim(-pad_x, table_width + pad_x)
    ax.set_ylim(table_height + pad_y, -pad_y)
    ax.axis("off")

    # Clean outer border framing the table
    ax.add_patch(Rectangle((0, 0), table_width, table_height, linewidth=1.5, edgecolor=COLOR_BORDER, facecolor="none"))

    # Header title
    fig.text(0.04, 0.97, f"Графік відключень (всі черги): Запоріжжя та область", fontsize=17, fontweight="bold")
    fig.text(0.96, 0.97, f"Дата: {date_str}", fontsize=16, fontweight="bold", ha="right")

    # Legend at bottom
    leg_y = 0.02
    leg_x = 0.035
    box_w = 0.018
    box_h = 0.025

    # 1. Електроенергія є
    fig.patches.append(Rectangle((leg_x, leg_y), box_w, box_h, linewidth=0.8, edgecolor=COLOR_BORDER, facecolor=COLOR_AVAILABLE, transform=fig.transFigure, clip_on=False))
    fig.text(leg_x + box_w + 0.005, leg_y + box_h / 2, "Електроенергія є", fontsize=10, va="center")

    # 2. Відключення
    leg_x += 0.125
    fig.patches.append(Rectangle((leg_x, leg_y), box_w, box_h, linewidth=0.8, edgecolor=COLOR_BORDER, facecolor=COLOR_OUTAGE, transform=fig.transFigure, clip_on=False))
    fig.text(leg_x + box_w + 0.005, leg_y + box_h / 2, "Відключення", fontsize=10, va="center")

    # 3. Можливе відключення
    leg_x += 0.115
    fig.patches.append(Rectangle((leg_x, leg_y), box_w, box_h, linewidth=0.8, edgecolor=COLOR_BORDER, facecolor=COLOR_MAYBE, transform=fig.transFigure, clip_on=False))
    fig.text(leg_x + box_w + 0.005, leg_y + box_h / 2, "Можливе відключення", fontsize=10, va="center")

    # 4. Більше відключень (diff)
    leg_x += 0.155
    fig.patches.append(Rectangle((leg_x, leg_y), box_w, box_h, linewidth=2.0, edgecolor=COLOR_DIFF_MORE, facecolor=COLOR_AVAILABLE, transform=fig.transFigure, clip_on=False))
    fig.text(leg_x + box_w + 0.005, leg_y + box_h / 2, "Більше відключень", fontsize=10, va="center")

    # 5. Менше відключень (diff)
    leg_x += 0.15
    fig.patches.append(Rectangle((leg_x, leg_y), box_w, box_h, linewidth=2.0, edgecolor=COLOR_DIFF_LESS, facecolor=COLOR_AVAILABLE, transform=fig.transFigure, clip_on=False))
    fig.text(leg_x + box_w + 0.005, leg_y + box_h / 2, "Менше відключень", fontsize=10, va="center")

    if last_updated:
        fig.text(0.965, leg_y + box_h / 2, f"Опубліковано: {last_updated}", fontsize=10, ha="right", va="center", style="italic", color="#555555")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_path, facecolor="#FFFFFF", dpi=130, bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description="Render ZOE Outage Schedule PNGs with Visual Diff")
    parser.add_argument("--json", required=True, help="Path to Zaporizhzhiaoblenergo.json")
    parser.add_argument("--prev-json", default=None, help="Path to previous Zaporizhzhiaoblenergo.json for diff calculation")
    parser.add_argument("--out", default="images/Zaporizhzhiaoblenergo", help="Output directory")
    parser.add_argument("--force", action="store_true", help="Force regenerate all images ignoring cache")
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
        now_kyiv = datetime.now(TZ_KYIV).replace(hour=0, minute=0, second=0, microsecond=0)
        today_ts = str(int(now_kyiv.timestamp()))

    today_dt = datetime.fromtimestamp(int(today_ts), tz=TZ_KYIV)
    tomorrow_date = today_dt.date() + timedelta(days=1)
    tomorrow_dt = datetime(tomorrow_date.year, tomorrow_date.month, tomorrow_date.day, 0, 0, tzinfo=TZ_KYIV)
    tomorrow_ts = str(int(tomorrow_dt.timestamp()))

    today_str = f"{today_dt.day:02d} {MONTHS_UK[today_dt.month]}"
    tomorrow_str = f"{tomorrow_dt.day:02d} {MONTHS_UK[tomorrow_dt.month]}"
    today_code = today_dt.strftime("%Y-%m-%d")

    today_data = fact_data.get(today_ts, {})
    tomorrow_data = fact_data.get(tomorrow_ts)

    # Attempt to load previous schedule for diff computation
    prev_json = load_previous_json(json_path, args.prev_json)
    today_diff = {}
    tomorrow_diff = {}

    if prev_json and "fact" in prev_json:
        prev_fact = prev_json["fact"]
        prev_fact_data = prev_fact.get("data", {})
        prev_today_ts = str(prev_fact.get("today", ""))

        # Only compute diff for today if previous JSON was also for the same calendar date
        # (prevents spurious red/green borders on midnight day rollover)
        if prev_today_ts == today_ts:
            prev_today_data = prev_fact_data.get(today_ts, {})
            today_diff = compute_schedule_diff(today_data, prev_today_data)
            if today_diff:
                print(f"[DIFF] Detected schedule changes for {len(today_diff)} queues on {today_str}")

        # Compute diff for tomorrow if tomorrow was also present in prev_json
        if tomorrow_data and tomorrow_ts in prev_fact_data:
            prev_tom_data = prev_fact_data.get(tomorrow_ts, {})
            tomorrow_diff = compute_schedule_diff(tomorrow_data, prev_tom_data)
            if tomorrow_diff:
                print(f"[DIFF] Detected tomorrow schedule changes for {len(tomorrow_diff)} queues on {tomorrow_str}")

    stats = {"checked": 0, "generated": 0, "skipped": 0}

    # 1. Render individual queue cards
    for gkey in ALL_QUEUES:
        stats["checked"] += 1
        q_today = today_data.get(gkey, {str(s): "yes" for s in SLOTS})
        q_tomorrow = tomorrow_data.get(gkey) if tomorrow_data else None
        q_tdiff = today_diff.get(gkey, {})
        q_tomdiff = tomorrow_diff.get(gkey, {})

        q_hash = calculate_hash({
            "today": q_today,
            "tomorrow": q_tomorrow,
            "update": last_updated,
            "diff_today": q_tdiff,
            "diff_tom": q_tomdiff,
        })
        fname = queue_filename(gkey)
        target_png = out_dir / fname

        if args.force or check_cache(hash_dir, gkey, q_hash, today_code, target_png):
            render_single_queue(
                q_today, q_tomorrow, gkey,
                today_str, tomorrow_str if q_tomorrow else None,
                last_updated, target_png,
                today_diff=q_tdiff, tomorrow_diff=q_tomdiff
            )
            save_cache(hash_dir, gkey, q_hash, today_code)
            if out_dir.name != "images" and out_dir.parent.name == "images":
                import shutil
                shutil.copy2(target_png, out_dir.parent / fname)
            stats["generated"] += 1
            print(f"[RENDERED] {fname}")
        else:
            stats["skipped"] += 1

    # 2. Render all-queues overview for today
    today_all_hash = calculate_hash({
        "data": today_data,
        "update": last_updated,
        "diff": today_diff,
    })
    today_all_png = out_dir / "gpv-all-today.png"
    stats["checked"] += 1
    if args.force or check_cache(hash_dir, "gpv-all-today", today_all_hash, today_code, today_all_png):
        render_all_queues_summary(today_data, today_str, last_updated, today_all_png, day_diff=today_diff)
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
        tom_all_hash = calculate_hash({
            "data": tomorrow_data,
            "update": last_updated,
            "diff": tomorrow_diff,
        })
        tom_all_png = out_dir / "gpv-all-tomorrow.png"
        if args.force or check_cache(hash_dir, "gpv-all-tomorrow", tom_all_hash, today_code, tom_all_png):
            render_all_queues_summary(tomorrow_data, tomorrow_str, last_updated, tom_all_png, day_diff=tomorrow_diff)
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
