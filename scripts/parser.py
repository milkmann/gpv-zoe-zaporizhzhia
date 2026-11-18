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
from html.parser import HTMLParser
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

TZ_KYIV = ZoneInfo("Europe/Kyiv")

MONTHS_UA = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4,
    "травня": 5, "червня": 6, "липня": 7, "серпня": 8,
    "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}
MONTH_PATTERN = "|".join(MONTHS_UA.keys())
DATE_RE = re.compile(rf"(?<!\d)(\d{{1,2}})\s+({MONTH_PATTERN})\b", re.IGNORECASE)

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
VALID_SLOT_STATES = {"yes", "no", "maybe", "first", "second", "mfirst", "msecond"}


class _ArticleExtractor(HTMLParser):
    """Stdlib HTML parser that extracts (title, body) pairs isolated per <article>."""

    BLOCK_TAGS = {"br", "p", "div", "li", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.articles: list[tuple[str, str]] = []
        self._in_article = False
        self._in_h2 = False
        self._h2_done = False
        self._content_depth = 0
        self._title_parts: list[str] = []
        self._body_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        t = tag.lower()
        if t == "article":
            self._in_article = True
            self._in_h2 = False
            self._h2_done = False
            self._content_depth = 0
            self._title_parts = []
            self._body_parts = []
            return
        if not self._in_article:
            return
        if t == "h2" and not self._h2_done:
            self._in_h2 = True
            return
        if t == "div":
            if self._content_depth > 0:
                self._content_depth += 1
                self._body_parts.append("\n")
            else:
                classes = dict(attrs).get("class") or ""
                if "content" in classes.split():
                    self._content_depth = 1
            return
        if self._content_depth > 0 and t in self.BLOCK_TAGS:
            self._body_parts.append("\n")

    def handle_endtag(self, tag: str):
        t = tag.lower()
        if not self._in_article:
            return
        if t == "h2" and self._in_h2:
            self._in_h2 = False
            self._h2_done = True
        elif t == "div" and self._content_depth > 0:
            self._content_depth -= 1
            self._body_parts.append("\n")
        elif t == "article":
            title = " ".join("".join(self._title_parts).split())
            body = "".join(self._body_parts)
            if title or body.strip():
                self.articles.append((title, body))
            self._in_article = False
            self._in_h2 = False
            self._content_depth = 0
        elif self._content_depth > 0 and t in self.BLOCK_TAGS:
            self._body_parts.append("\n")

    def handle_data(self, data: str):
        if self._in_h2:
            self._title_parts.append(data)
        elif self._content_depth > 0:
            self._body_parts.append(data)


def extract_articles(html_text: str) -> list[tuple[str, str]]:
    """Extract (clean_title, clean_body) pairs from HTML without cross-article bleed."""
    parser = _ArticleExtractor()
    parser.feed(html_text)
    parser.close()
    return parser.articles


def atomic_write_json(path: str, data: dict) -> None:
    """Write JSON atomically via temp file + os.replace."""
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def parse_update_datetime(upd_str: str | None) -> datetime | None:
    """Parse update date/time string in various formats to timezone-aware datetime."""
    if not upd_str or not isinstance(upd_str, str):
        return None
    cleaned = upd_str.strip()
    for fmt in (
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%H:%M %d.%m.%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    ):
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


def extract_update_match(clean_title: str, clean_body: str) -> re.Match | None:
    """Extract explicit update time from article title/body without matching generic 'час' prose."""
    m = re.search(
        r"оновлено\s*(?::\s*)?(?:о\s*)?(\d{1,2}[:.]\d{2})",
        f"{clean_title} {clean_body}",
        re.IGNORECASE,
    )
    if m:
        return m
    return re.search(
        r"\b(?:станом\s+на|опубліковано|час\s*:)\s*(\d{1,2}[:.]\d{2})",
        clean_body,
        re.IGNORECASE,
    )


def resolve_update_datetime(target_date: datetime, upd_match: re.Match | None, now_kyiv: datetime) -> str:
    """Resolve article update timestamp without placing updates in the future."""
    skew = timedelta(minutes=2)
    if upd_match:
        h, m = map(int, re.split(r"[:.]", upd_match.group(1)))
        if 0 <= h <= 23 and 0 <= m <= 59:
            base_date = now_kyiv if target_date.date() > now_kyiv.date() else target_date
            candidate = base_date.replace(hour=h, minute=m, second=0, microsecond=0)
            if candidate > now_kyiv + skew:
                candidate = now_kyiv.replace(hour=h, minute=m, second=0, microsecond=0)
                if candidate > now_kyiv + skew:
                    candidate -= timedelta(days=1)
            return candidate.strftime("%d.%m.%Y %H:%M")
    if target_date.date() > now_kyiv.date():
        return now_kyiv.strftime("%d.%m.%Y %H:%M")
    return target_date.strftime("%d.%m.%Y 00:00")


def resolve_article_date(day_num: int, month_num: int, now_kyiv: datetime) -> datetime | None:
    """Pick the calendar year (prev/curr/next) closest to now_kyiv and validate date."""
    best: datetime | None = None
    best_delta: int | None = None
    for y in (now_kyiv.year - 1, now_kyiv.year, now_kyiv.year + 1):
        try:
            cand = datetime(y, month_num, day_num, 0, 0, tzinfo=TZ_KYIV)
        except ValueError:
            continue
        delta = abs((cand.date() - now_kyiv.date()).days)
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best = cand
    return best


def is_no_outages_notice(clean_title: str, clean_body: str) -> bool:
    """Return True if an article without queue lines explicitly states GPV will not apply to population."""
    text = f"{clean_title}\n{clean_body}".lower()
    sentences = [s.strip() for s in re.split(r"(?:(?<!\d)\.(?!\d)|[?!\n])+", text) if s.strip()]

    gpv_markers = ("гпв", "погодинн", "населення")
    non_gpv_markers = ("аварійних", "гав", "сгав", "гоп", "обмеження потужності", "промисловост")
    active_gpv_phrases = ("продовжують діяти", "діють", "залишаються чинними", "будуть застосовані")

    # If any sentence states GPV is active/continuing, never wipe the day
    for s in sentences:
        if any(p in s for p in active_gpv_phrases) and any(g in s for g in ("гпв", "погодинн")):
            # Ignore negated forms like "ГПВ не діятимуть"
            if not re.search(r"\bне\s+(?:діють|діятимуть|застосову|вводитимуться)", s):
                return False

    population_clear = (
        "не стосуються населення",
        "відключення відсутні",
        "без відключень",
        "обмежень не передбачено",
    )
    cancel_keywords = (
        "не заплановано",
        "не вводитимуться",
        "не застосовуються",
        "не застосовуватимуться",
        "скасован",
    )
    time_range_re = re.compile(r"\d{1,2}[:.]\d{2}\s*(?:-|–|—|до|по)\s*\d{1,2}[:.]\d{2}")
    specific_queue_re = re.compile(r"(?<![.\d])[1-6]\.[1-2](?![.\d])")

    for s in sentences:
        if any(k in s for k in population_clear):
            if not (any(m in s for m in non_gpv_markers) and not any(g in s for g in gpv_markers)):
                return True

        if any(k in s for k in cancel_keywords):
            # Ignore partial cancellations for specific hours or single queues
            if time_range_re.search(s) or specific_queue_re.search(s):
                continue
            has_gpv = any(g in s for g in gpv_markers)
            has_non_gpv = any(m in s for m in non_gpv_markers)
            if has_gpv and not (has_non_gpv and "скасован" in s and not re.search(r"(?:гпв|погодинн)[^,.;]*скасован|скасован[^,.;]*(?:гпв|погодинн)", s)):
                return True
            if not has_non_gpv:
                return True

    return False


def is_valid_days_data(days_data: object) -> bool:
    """Validate DTEK/OE fact.data schema compatible with ha-svitlo-yeah."""
    if not isinstance(days_data, dict) or not days_data:
        return False
    for day_key, groups in days_data.items():
        if not (isinstance(day_key, str) and day_key.isascii() and day_key.isdigit()):
            return False
        if not isinstance(groups, dict) or not groups:
            return False
        for g_key, hours in groups.items():
            if not isinstance(g_key, str) or not isinstance(hours, dict) or not hours:
                return False
            if not all(isinstance(hk, str) and isinstance(hv, str) and hv in VALID_SLOT_STATES for hk, hv in hours.items()):
                return False
    return True


def fetch_fallback_data() -> tuple[dict, str | None] | None:
    """Fetch, validate, and normalize backup source if official site is geo-blocked."""
    try:
        req = urllib.request.Request(FALLBACK_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                fact = data.get("fact", {})
                days_data = fact.get("data")
                raw_update = fact.get("update")
                if not is_valid_days_data(days_data):
                    print("[WARN] Fallback data failed schema validation.")
                    return None
                parsed_upd = parse_update_datetime(raw_update)
                norm_update = parsed_upd.strftime("%d.%m.%Y %H:%M") if parsed_upd else None
                print(f"[OK] Successfully fetched fallback data from {FALLBACK_URL} ({len(days_data)} days, update: {norm_update})")
                return days_data, norm_update
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
                last_err = RuntimeError(f"HTTP {resp.status}")
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
                        last_err = RuntimeError(f"HTTP {resp.status}")
                        print(f"[WARN] Attempt {attempt} (unverified TLS): HTTP {resp.status}")
                except Exception as inner_e:
                    last_err = inner_e
                    print(f"[WARN] Attempt {attempt} (unverified TLS) failed: {inner_e}")
            else:
                last_err = e
                print(f"[WARN] Attempt {attempt} failed: {e}")
        except Exception as e:
            last_err = e
            print(f"[WARN] Attempt {attempt} failed: {e}")
        if attempt < retries:
            time.sleep(attempt * 2)
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts: {last_err}")


def parse_time_intervals(intervals_str: str) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Parse string like '10:30 – 13:30, 17:00 – 18:30' or 'з 10.00 по 14.00'."""
    res = []
    clean = html_lib.unescape(intervals_str)
    clean = clean.replace("—", "-").replace("–", "-").replace("−", "-").replace("\xa0", " ")
    for m in re.finditer(r"(\d{1,2})[:.](\d{2})\s*(?:-|до|по)\s*(\d{1,2})[:.](\d{2})", clean, re.IGNORECASE):
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
        if eh == 24 or (eh == 23 and em >= 59) or (eh == 0 and (sh > 0 or sm > 0)):
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
    articles = extract_articles(html)
    print(f"[INFO] Found {len(articles)} articles on page.")

    if now_kyiv is None:
        now_kyiv = datetime.now(TZ_KYIV)

    days_data = {}
    latest_update_str = None

    for clean_title, clean_body in articles:
        date_match = DATE_RE.search(clean_title) or DATE_RE.search(clean_body)
        if not date_match:
            continue

        day_num = int(date_match.group(1))
        month_num = MONTHS_UA[date_match.group(2).lower()]
        target_date = resolve_article_date(day_num, month_num, now_kyiv)
        if target_date is None:
            continue
        day_ts = str(int(target_date.timestamp()))

        # If already parsed a newer article for this day, skip earlier updates
        if day_ts in days_data:
            continue

        queue_lines = []
        for line in clean_body.splitlines():
            line = line.replace("\xa0", " ").strip()
            m = re.match(
                r"^[•\-–—*\s]*(?:(?:черг[аи]|підчерг[аи]|груп[аи])\s*)?([0-9.,\sтаiі/]+)\s*:\s*(.+)$",
                line,
                re.IGNORECASE,
            )
            if m:
                q_matches = re.findall(r"(?<![.\d])([1-6]\.[1-2])(?![.\d])", m.group(1))
                if q_matches:
                    for q_num in q_matches:
                        queue_lines.append((q_num, m.group(2).strip()))

        if queue_lines:
            queue_intervals: dict[str, list] = {q: [] for q in ALL_QUEUES}
            matched_queues: set[str] = set()
            total_intervals = 0
            for q_name, intervals_str in queue_lines:
                q_key = f"GPV{q_name}"
                if q_key in queue_intervals:
                    matched_queues.add(q_key)
                    ivs = parse_time_intervals(intervals_str)
                    total_intervals += len(ivs)
                    queue_intervals[q_key].extend(ivs)

            # Reject accidental single-line match in non-schedule PR notices
            is_gpv_article = bool(re.search(r"гпв|погодинн|черг|підчерг", f"{clean_title} {clean_body}", re.IGNORECASE))
            if len(matched_queues) == 1 and (total_intervals == 0 or not is_gpv_article):
                continue

            upd_match = extract_update_match(clean_title, clean_body)
            update_dt = resolve_update_datetime(target_date, upd_match, now_kyiv)

            if latest_update_str is None:
                latest_update_str = update_dt

            days_data[day_ts] = {q: intervals_to_hours(iv) for q, iv in queue_intervals.items()}
            print(f"[OK] Parsed schedule for {target_date.strftime('%Y-%m-%d')} ({len(matched_queues)} queues). Updated: {update_dt}")
        elif is_no_outages_notice(clean_title, clean_body):
            days_data[day_ts] = {q: {str(i): "yes" for i in range(1, 25)} for q in ALL_QUEUES}
            print(f"[OK] Parsed NO outages for population on {target_date.strftime('%Y-%m-%d')}")
            if latest_update_str is None:
                upd_match = extract_update_match(clean_title, clean_body)
                latest_update_str = resolve_update_datetime(target_date, upd_match, now_kyiv)
    return days_data, latest_update_str


def build_output_json(
    days_data: dict,
    update_str: str | None,
    now_kyiv: datetime | None = None,
    allow_synthetic_today: bool = True,
) -> dict:
    """Construct full output structure compatible with DtekAPIJson."""
    if now_kyiv is None:
        now_kyiv = datetime.now(TZ_KYIV)
    today_midnight = now_kyiv.replace(hour=0, minute=0, second=0, microsecond=0)
    today_ts = int(today_midnight.timestamp())
    today_key = str(today_ts)

    days_copy = dict(days_data)
    today_defaulted = False
    if today_key not in days_copy and allow_synthetic_today:
        print(f"[INFO] Today ({today_midnight.strftime('%Y-%m-%d')}) not explicitly in schedule; defaulting to all 'yes'")
        days_copy[today_key] = {q: {str(i): "yes" for i in range(1, 25)} for q in ALL_QUEUES}
        today_defaulted = True

    if not update_str:
        update_str = now_kyiv.strftime("%d.%m.%Y 00:00")
    elif today_defaulted and allow_synthetic_today:
        upd_dt = parse_update_datetime(update_str)
        if upd_dt is None or (now_kyiv - upd_dt).days >= FRESH_DATA_MAX_AGE_DAYS:
            update_str = now_kyiv.strftime("%d.%m.%Y %H:%M")

    # Keep only valid numeric day timestamps within [today - 7d, today + 7d]
    min_ts = today_ts - 7 * 86400
    max_ts = today_ts + 7 * 86400
    filtered_days = {
        k: v
        for k, v in days_copy.items()
        if isinstance(k, str) and k.isascii() and k.isdigit() and min_ts <= int(k) <= max_ts
    }

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


def merge_past_days(new_days: dict, existing_data: dict | None, today_ts: int) -> dict:
    """Preserve recent past days (< today_ts) from existing_data to avoid spurious commits when old posts scroll off."""
    merged = dict(new_days)
    if existing_data and isinstance(existing_data.get("fact", {}).get("data"), dict):
        min_ts = today_ts - 7 * 86400
        for k, v in existing_data["fact"]["data"].items():
            if isinstance(k, str) and k.isascii() and k.isdigit():
                ts = int(k)
                if min_ts <= ts < today_ts and k not in merged:
                    merged[k] = v
    return merged


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
    sources_failed = False

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

    today_ts = int(now_kyiv.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    # Midnight rollover safeguard: ONLY advance fact.today if today's schedule ALREADY existed in local data
    if not days_data:
        sources_failed = True
        if existing_data and "fact" in existing_data:
            old_fact = existing_data.get("fact", {})
            old_days = old_fact.get("data")
            if (
                old_fact.get("today") != today_ts
                and isinstance(old_days, dict)
                and str(today_ts) in old_days
            ):
                print("[INFO] Calendar day rolled over and today's schedule is already cached; advancing fact.today.")
                days_data = old_days
                update_str = old_fact.get("update")
            else:
                print("[INFO] No new schedule data obtained. Preserving existing data file.")
                sys.exit(0)
        else:
            print("[WARN] No schedule data obtained from any source.")
            sys.exit(1)

    days_data = merge_past_days(days_data, existing_data, today_ts)
    result = build_output_json(
        days_data,
        update_str,
        now_kyiv=now_kyiv,
        allow_synthetic_today=not sources_failed,
    )

    # Check if schedule data actually changed (ignore lastUpdated timestamp when comparing)
    has_changed = True
    if existing_data and "fact" in existing_data:
        old_fact = existing_data.get("fact", {})
        new_fact = result.get("fact", {})
        if old_fact.get("data") == new_fact.get("data") and old_fact.get("today") == new_fact.get("today"):
            has_changed = False
            print("[INFO] Schedule data and day unchanged. Skipping file write.")

    if has_changed or not os.path.exists(out_file):
        atomic_write_json(out_file, result)
        print(f"[SUCCESS] Saved {out_file} (changed={has_changed})")
    else:
        print(f"[SUCCESS] Kept {out_file} unchanged (changed=False)")


if __name__ == "__main__":
    main()
