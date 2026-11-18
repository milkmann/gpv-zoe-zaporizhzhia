#!/usr/bin/env python3
"""
Parser for Zaporizhzhiaoblenergo (ZOE) power outage schedules.
Fetches https://www.zoe.com.ua/outage/ and generates standardized DTEK/OE JSON
compatible with Home Assistant (ha-svitlo-yeah) and other tools.
"""

import os
import re
import ssl
import sys
import json
import time
import html as html_lib
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

TZ_KYIV = ZoneInfo("Europe/Kyiv")

MONTHS_UA = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4,
    "травня": 5, "червня": 6, "липня": 7, "серпня": 8,
    "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}
MONTH_PATTERN = "|".join(MONTHS_UA.keys())
DATE_RE = re.compile(rf"(\d{{1,2}})\s+({MONTH_PATTERN})\b", re.IGNORECASE)

ALL_QUEUES = [
    "GPV1.1", "GPV1.2",
    "GPV2.1", "GPV2.2",
    "GPV3.1", "GPV3.2",
    "GPV4.1", "GPV4.2",
    "GPV5.1", "GPV5.2",
    "GPV6.1", "GPV6.2",
]

DEFAULT_TIME_ZONE = {
    str(i): [
        f"{i-1:02d}-{i:02d}",
        f"{i-1:02d}:00",
        f"{i:02d}:00" if i < 24 else "24:00",
    ]
    for i in range(1, 25)
}

DEFAULT_TIME_TYPE = {
    "yes": "Світло є",
    "maybe": "Можливе відключення",
    "no": "Відключення",
    "first": "Відключення перші 30 хв.",
    "second": "Відключення другі 30 хв.",
    "mfirst": "Можливе відключення перші 30 хв.",
    "msecond": "Можливе відключення другі 30 хв.",
}

URL_ZOE_OUTAGE = "https://www.zoe.com.ua/outage/"
FALLBACK_URL = "https://raw.githubusercontent.com/yaroslav2901/OE_OUTAGE_DATA/main/data/Zaporizhzhiaoblenergo.json"
FRESH_DATA_MAX_AGE_DAYS = 2


def parse_update_datetime(upd_str: str | None) -> datetime | None:
    """Parse update date/time string in various formats to timezone-aware datetime."""
    if not upd_str or not isinstance(upd_str, str):
        return None
    cleaned = upd_str.strip()
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=TZ_KYIV)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_KYIV)
        return dt.astimezone(TZ_KYIV)
    except Exception:
        pass
    return None


def resolve_update_datetime(target_date: datetime, upd_match: re.Match | None, now_kyiv: datetime) -> str:
    """Resolve article update timestamp without placing evening updates for tomorrow in the future."""
    if upd_match:
        h, m = map(int, upd_match.group(1).split(":"))
        if 0 <= h <= 23 and 0 <= m <= 59:
            candidate = target_date.replace(hour=h, minute=m, second=0, microsecond=0)
            if candidate > now_kyiv + timedelta(minutes=10):
                candidate = now_kyiv.replace(hour=h, minute=m, second=0, microsecond=0)
                if candidate > now_kyiv + timedelta(minutes=10):
                    candidate -= timedelta(days=1)
            return candidate.strftime("%d.%m.%Y %H:%M")
    if target_date.date() > now_kyiv.date():
        return now_kyiv.strftime("%d.%m.%Y %H:%M")
    return target_date.strftime("%d.%m.%Y 00:00")


def is_no_outages_notice(clean_title: str, clean_body: str) -> bool:
    """Return True if an article without queue lines explicitly states GPV will not apply to population."""
    text = f"{clean_title}\n{clean_body}".lower()
    if "продовжують діяти" in text:
        return False

    strong_keywords = (
        "не стосуються населення",
        "не заплановано",
        "не вводитимуться",
        "відключення відсутні",
        "без відключень",
        "обмежень не передбачено",
    )
    if any(k in text for k in strong_keywords):
        return True

    if "скасован" in text:
        emergency_markers = ("аварійних відключень", "гав", "сгав")
        gpv_markers = ("гпв", "погодинних відключень")
        if any(m in text for m in emergency_markers) and not any(m in text for m in gpv_markers):
            return False
        return True

    return False


def fetch_fallback_data() -> tuple[dict, str | None] | None:
    """Fetch from backup source if official site is geo-blocked for cloud runners."""
    try:
        req = urllib.request.Request(FALLBACK_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                fact = data.get("fact", {})
                days_data = fact.get("data", {})
                update_str = fact.get("update")
                if days_data:
                    print(f"[OK] Successfully fetched fallback data from {FALLBACK_URL} ({len(days_data)} days, update: {update_str})")
                    return days_data, update_str
    except Exception as e:
        print(f"[WARN] Fallback fetch failed: {e}")
    return None


def fetch_html_with_retries(url: str, retries: int = 2, timeout: int = 10) -> str:
    """Fetch HTML content with browser-like headers, standard TLS first, and retries."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    ctx = ssl.create_default_context()

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
                if resp.status == 200:
                    return resp.read().decode("utf-8", errors="replace")
                print(f"[WARN] Attempt {attempt}: HTTP {resp.status}")
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLCertVerificationError) and ctx.verify_mode != ssl.CERT_NONE:
                print(f"[WARN] SSL verification failed ({e.reason}); retrying with unverified TLS context...")
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                try:
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
                        if resp.status == 200:
                            return resp.read().decode("utf-8", errors="replace")
                except Exception as inner_e:
                    last_err = inner_e
                    print(f"[WARN] Attempt {attempt} (unverified TLS) failed: {inner_e}")
            else:
                last_err = e
                print(f"[WARN] Attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(attempt * 2)
        except Exception as e:
            last_err = e
            print(f"[WARN] Attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(attempt * 2)
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts: {last_err}")


def parse_time_intervals(intervals_str: str) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Parse string like '10:30 – 13:30, 17:00 – 18:30' or '10.00 - 14.00' into list of ((start_h, start_m), (end_h, end_m))."""
    res = []
    clean = html_lib.unescape(intervals_str)
    clean = clean.replace("—", "-").replace("–", "-").replace("−", "-").replace("\xa0", " ")
    for m in re.finditer(r"(\d{1,2})[:.](\d{2})\s*(?:-|до)\s*(\d{1,2})[:.](\d{2})", clean, re.IGNORECASE):
        sh, sm = int(m.group(1)), int(m.group(2))
        eh, em = int(m.group(3)), int(m.group(4))
        res.append(((sh, sm), (eh, em)))
    return res


def intervals_to_hours(intervals: list[tuple[tuple[int, int], tuple[int, int]]]) -> dict[str, str]:
    """Convert intervals to DTEK 24-hour dictionary ('1' to '24') with half-hour resolution."""
    slots = [False] * 48
    for (sh, sm), (eh, em) in intervals:
        start_slot = sh * 2 + (1 if sm >= 30 else 0)
        end_slot = eh * 2 + (1 if 0 < em <= 30 else (2 if em > 30 else 0))
        if eh == 24 or (eh == 23 and em >= 59) or (eh == 0 and em == 0 and (sh > 0 or sm > 0)):
            end_slot = 48
        for s in range(max(0, start_slot), min(end_slot, 48)):
            slots[s] = True

    hours = {}
    for h in range(24):
        first_half = slots[h * 2]
        second_half = slots[h * 2 + 1]
        hour_key = str(h + 1)
        if first_half and second_half:
            hours[hour_key] = "no"
        elif first_half and not second_half:
            hours[hour_key] = "first"
        elif not first_half and second_half:
            hours[hour_key] = "second"
        else:
            hours[hour_key] = "yes"
    return hours


def parse_outages_from_html(html: str, now_kyiv: datetime | None = None) -> tuple[dict, str | None]:
    """Parse articles from HTML and build fact schedule data and latest update timestamp."""
    articles = re.findall(
        r'<article[^>]*>.*?<h2>(.*?)</h2>.*?<div class="content">(.*?)</div>.*?</article>',
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    print(f"[INFO] Found {len(articles)} articles on page.")

    if now_kyiv is None:
        now_kyiv = datetime.now(TZ_KYIV)
    year = now_kyiv.year

    days_data = {}
    latest_update_str = None

    for h2, content in articles:
        clean_title = html_lib.unescape(re.sub(r"<[^>]+>", " ", h2).strip())

        # Preserve newlines between block elements to avoid joining distinct lines
        content_newlines = re.sub(r"<\s*(?:br|/?p|/?div|/?li|/?tr)\b[^>]*>", "\n", content, flags=re.IGNORECASE)
        clean_body = html_lib.unescape(re.sub(r"<[^>]+>", " ", content_newlines))

        date_match = DATE_RE.search(clean_title) or DATE_RE.search(clean_body)
        if not date_match:
            continue

        day_num = int(date_match.group(1))
        month_num = MONTHS_UA[date_match.group(2).lower()]

        article_year = year
        if now_kyiv.month == 12 and month_num == 1:
            article_year = year + 1
        elif now_kyiv.month == 1 and month_num == 12:
            article_year = year - 1

        try:
            target_date = datetime(article_year, month_num, day_num, 0, 0, tzinfo=TZ_KYIV)
        except ValueError:
            continue
        day_ts = str(int(target_date.timestamp()))

        # If already parsed a newer article for this day, skip earlier updates
        if day_ts in days_data:
            continue

        queue_lines = []
        for line in clean_body.splitlines():
            line = line.replace("\xa0", " ").strip()
            m = re.match(r"^(?:(?:черг[аи]|підчерг[аи]|груп[аи])\s*)?([0-9.,\sтаiі/]+)\s*:\s*(.+)$", line, re.IGNORECASE)
            if m:
                q_matches = re.findall(r"\b([1-6]\.[1-2])\b", m.group(1))
                if q_matches:
                    for q_num in q_matches:
                        queue_lines.append((q_num, m.group(2).strip()))

        if queue_lines:
            upd_match = re.search(r"оновлено\s*(?:о\s*)?(\d{1,2}:\d{2})", clean_title + " " + clean_body, re.IGNORECASE)
            if not upd_match:
                upd_match = re.search(r"(?:станом на|опубліковано|час[:\s]+)\s*(\d{1,2}:\d{2})", clean_body, re.IGNORECASE)
            update_dt = resolve_update_datetime(target_date, upd_match, now_kyiv)

            if latest_update_str is None:
                latest_update_str = update_dt

            queue_intervals: dict[str, list] = {q: [] for q in ALL_QUEUES}
            for q_name, intervals_str in queue_lines:
                q_key = f"GPV{q_name}"
                if q_key in queue_intervals:
                    queue_intervals[q_key].extend(parse_time_intervals(intervals_str))

            days_data[day_ts] = {q: intervals_to_hours(iv) for q, iv in queue_intervals.items()}
            print(f"[OK] Parsed schedule for {target_date.strftime('%Y-%m-%d')} ({len(queue_lines)} queues). Updated: {update_dt}")
        elif is_no_outages_notice(clean_title, clean_body):
            days_data[day_ts] = {q: {str(i): "yes" for i in range(1, 25)} for q in ALL_QUEUES}
            print(f"[OK] Parsed NO outages for population on {target_date.strftime('%Y-%m-%d')}")
            if latest_update_str is None:
                upd_match = re.search(r"оновлено\s*(?:о\s*)?(\d{1,2}:\d{2})", clean_title + " " + clean_body, re.IGNORECASE)
                if not upd_match:
                    upd_match = re.search(r"(?:станом на|опубліковано|час[:\s]+)\s*(\d{1,2}:\d{2})", clean_body, re.IGNORECASE)
                latest_update_str = resolve_update_datetime(target_date, upd_match, now_kyiv)
    return days_data, latest_update_str


def build_output_json(days_data: dict, update_str: str | None, now_kyiv: datetime | None = None) -> dict:
    """Construct full output structure compatible with DtekAPIJson."""
    if now_kyiv is None:
        now_kyiv = datetime.now(TZ_KYIV)
    today_midnight = now_kyiv.replace(hour=0, minute=0, second=0, microsecond=0)
    today_ts = int(today_midnight.timestamp())
    today_key = str(today_ts)

    days_copy = dict(days_data)
    today_defaulted = False
    if today_key not in days_copy:
        print(f"[INFO] Today ({today_midnight.strftime('%Y-%m-%d')}) not explicitly in schedule; defaulting to all 'yes'")
        days_copy[today_key] = {q: {str(i): "yes" for i in range(1, 25)} for q in ALL_QUEUES}
        today_defaulted = True

    if not update_str:
        update_str = now_kyiv.strftime("%d.%m.%Y 00:00")
    elif today_defaulted:
        upd_dt = parse_update_datetime(update_str)
        if upd_dt is None or (now_kyiv - upd_dt).days >= FRESH_DATA_MAX_AGE_DAYS:
            update_str = now_kyiv.strftime("%d.%m.%Y %H:%M")

    # Keep only days within reasonable range (last 7 days and future days)
    min_ts = today_ts - 7 * 86400
    filtered_days = {k: v for k, v in days_copy.items() if int(k) >= min_ts}

    output = {
        "regionId": "Zaporizhzhia",
        "lastUpdated": datetime.now(timezone.utc).isoformat(),
        "fact": {
            "data": filtered_days,
            "update": update_str,
            "today": today_ts,
        },
        "preset": {
            "time_zone": DEFAULT_TIME_ZONE,
            "time_type": DEFAULT_TIME_TYPE,
        },
    }
    return output


def main():
    now_kyiv = datetime.now(TZ_KYIV)
    print(f"=== Starting ZOE Outage Parser at {now_kyiv} ===")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)
    data_dir = os.path.join(repo_root, "data")
    os.makedirs(data_dir, exist_ok=True)
    out_file = os.path.join(data_dir, "Zaporizhzhiaoblenergo.json")

    existing_data = None
    if os.path.exists(out_file):
        try:
            with open(out_file, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
        except Exception as e:
            print(f"[WARN] Could not read existing {out_file}: {e}")

    days_data = None
    update_str = None

    try:
        html = fetch_html_with_retries(URL_ZOE_OUTAGE)
        days_data, update_str = parse_outages_from_html(html, now_kyiv=now_kyiv)
    except Exception as e:
        print(f"[WARN] Failed to fetch {URL_ZOE_OUTAGE} directly: {e}")

    # Fallback to secondary source if direct scraping failed or returned no schedule days
    if not days_data:
        print("[INFO] Attempting fallback data source...")
        fallback = fetch_fallback_data()
        if fallback:
            fb_days, fb_update = fallback
            existing_upd_str = existing_data.get("fact", {}).get("update") if existing_data else None
            fb_dt = parse_update_datetime(fb_update)
            ex_dt = parse_update_datetime(existing_upd_str)

            if ex_dt and (not fb_dt or fb_dt <= ex_dt):
                print(f"[WARN] Fallback update ({fb_update}) is not newer than local file ({existing_upd_str}). Fallback rejected.")
            else:
                days_data, update_str = fb_days, fb_update

    # Midnight rollover safeguard: if sources unavailable/unchanged but calendar day advanced, roll `fact.today` forward
    if not days_data:
        if existing_data and "fact" in existing_data:
            today_ts = int(now_kyiv.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            old_fact = existing_data.get("fact", {})
            if old_fact.get("today") != today_ts and isinstance(old_fact.get("data"), dict):
                print("[INFO] Calendar day rolled over while sources were unchanged; advancing fact.today.")
                days_data = old_fact["data"]
                update_str = old_fact.get("update")
            else:
                print("[INFO] No new schedule data obtained. Preserving existing data file.")
                sys.exit(0)
        else:
            print("[WARN] No schedule data obtained from any source.")
            sys.exit(1)

    result = build_output_json(days_data, update_str, now_kyiv=now_kyiv)

    # Check if schedule data actually changed (ignore lastUpdated timestamp when comparing)
    has_changed = True
    if existing_data and "fact" in existing_data:
        old_fact = existing_data.get("fact", {})
        new_fact = result.get("fact", {})
        if old_fact.get("data") == new_fact.get("data") and old_fact.get("today") == new_fact.get("today"):
            old_upd_dt = old_fact.get("update", "")
            if old_upd_dt:
                result["fact"]["update"] = old_upd_dt
            if "lastUpdated" in existing_data:
                result["lastUpdated"] = existing_data["lastUpdated"]
            has_changed = False
            print("[INFO] Schedule data and day unchanged. Preserving existing update timestamp.")

    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"[SUCCESS] Saved {out_file} (changed={has_changed})")


if __name__ == "__main__":
    main()
