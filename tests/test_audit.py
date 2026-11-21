#!/usr/bin/env python3
"""
Unit and integration tests for ZOE Outage Parser & Renderer.
"""

import os
import sys
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

# Add scripts dir to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import parser as zoe_parser
import render_png

TZ_KYIV = ZoneInfo("Europe/Kyiv")


def _make_day_schedule(overrides: dict[str, dict[str, str]] | None = None) -> dict:
    day = {q: {str(i): "yes" for i in range(1, 25)} for q in zoe_parser.ALL_QUEUES}
    if overrides:
        for q, slots in overrides.items():
            day[q].update(slots)
    return day


class TestParserAndRenderer(unittest.TestCase):

    def test_parse_update_datetime(self):
        """Verify parse_update_datetime parses all standard formats."""
        dt1 = zoe_parser.parse_update_datetime("26.10.2026 08:14")
        self.assertIsNotNone(dt1)
        self.assertEqual((dt1.year, dt1.month, dt1.day, dt1.hour, dt1.minute), (2026, 10, 26, 8, 14))

        dt2 = zoe_parser.parse_update_datetime("26.10.2026 08:14:30")
        self.assertIsNotNone(dt2)
        self.assertEqual(dt2.second, 30)

        dt_rev = zoe_parser.parse_update_datetime("08:14 26.10.2026")
        self.assertIsNotNone(dt_rev)
        self.assertEqual((dt_rev.day, dt_rev.hour, dt_rev.minute), (26, 8, 14))

        # On 2026-10-26 Kyiv is UTC+2 (winter time), so 08:14+03:00 is 07:14 Kyiv time
        dt3 = zoe_parser.parse_update_datetime("2026-10-26T08:14:00+03:00")
        self.assertIsNotNone(dt3)
        self.assertEqual(dt3.astimezone(TZ_KYIV).hour, 7)

        self.assertIsNone(zoe_parser.parse_update_datetime("invalid date"))
        self.assertIsNone(zoe_parser.parse_update_datetime(None))

    def test_normalize_source_time(self):
        """Verify normalize_source_time shifts future same-day stamps back 1 day and rejects further future."""
        now = datetime(2026, 11, 21, 12, 0, tzinfo=TZ_KYIV)
        self.assertEqual(
            zoe_parser.normalize_source_time(datetime(2026, 11, 21, 23, 45, tzinfo=TZ_KYIV), now),
            datetime(2026, 11, 20, 23, 45, tzinfo=TZ_KYIV),
        )
        self.assertEqual(
            zoe_parser.normalize_source_time(datetime(2026, 11, 21, 12, 1, tzinfo=TZ_KYIV), now),
            datetime(2026, 11, 21, 12, 1, tzinfo=TZ_KYIV),
        )
        self.assertIsNone(
            zoe_parser.normalize_source_time(datetime(2026, 11, 22, 23, 45, tzinfo=TZ_KYIV), now),
        )
        self.assertEqual(
            zoe_parser.normalize_source_time(datetime(2026, 11, 21, 10, 0, tzinfo=TZ_KYIV), now),
            datetime(2026, 11, 21, 10, 0, tzinfo=TZ_KYIV),
        )
        self.assertIsNone(zoe_parser.normalize_source_time(None, now))

    def test_html_extractor_isolation_nested_divs_and_attributes(self):
        """Verify HTMLParser prevents cross-article bleed, handles nested divs and <h2 class=...>."""
        mock_html = """
        <article id="post-1">
            <h2 class="entry-title">19 ЛИСТОПАДА: НОВИНА БЕЗ КОНТЕНТУ</h2>
        </article>
        <article id="post-2">
            <h2 class="entry-title">ОНОВЛЕНО ГПВ НА 18 ЛИСТОПАДА (оновлено: 05.57)</h2>
            <div class="entry content main-body">
                <div class="wrapper"><p>• 1.1: з 10:00 по 14:00</p></div>
                <p>- 1.2: 23:30 – 00:30</p>
            </div>
        </article>
        """
        now = datetime(2026, 11, 18, 10, 0, tzinfo=TZ_KYIV)
        days_data, upd = zoe_parser.parse_outages_from_html(mock_html, now_kyiv=now)
        # Must NOT bleed post-2's schedule into post-1's date (19 Nov)
        ts_18 = str(int(datetime(2026, 11, 18, 0, 0, tzinfo=TZ_KYIV).timestamp()))
        ts_19 = str(int(datetime(2026, 11, 19, 0, 0, tzinfo=TZ_KYIV).timestamp()))
        self.assertIn(ts_18, days_data)
        self.assertNotIn(ts_19, days_data)
        self.assertEqual(upd, "18.11.2026 05:57")
        # Bullet marker + "з ... по ..."
        self.assertEqual(days_data[ts_18]["GPV1.1"]["11"], "no")
        self.assertEqual(days_data[ts_18]["GPV1.1"]["14"], "no")
        # Dash marker + cross-midnight 23:30 - 00:30 covers 23:30-24:00
        self.assertEqual(days_data[ts_18]["GPV1.2"]["24"], "second")

    def test_year_transition_real_parser(self):
        """Verify year rollover when today is December and article is for January."""
        mock_html = """
        <article>
            <h2>Графік відключень на 1 січня</h2>
            <div class="content">
                1.1: 10:00 - 14:00
                1.2: 14:00 - 18:00
            </div>
        </article>
        """
        now_in_december = datetime(2026, 12, 31, 23, 30, tzinfo=TZ_KYIV)
        days_data, update_str = zoe_parser.parse_outages_from_html(mock_html, now_kyiv=now_in_december)
        self.assertEqual(len(days_data), 1)

        target_ts = list(days_data.keys())[0]
        dt = datetime.fromtimestamp(int(target_ts), tz=TZ_KYIV)
        self.assertEqual((dt.year, dt.month, dt.day), (2027, 1, 1))
        self.assertEqual(update_str, "31.12.2026 23:30")

    def test_evening_update_for_tomorrow_and_skew_edge(self):
        """Verify resolve_update_datetime never places updates in the future."""
        mock_html = """
        <article>
            <h2>ОНОВЛЕНО ГПВ НА 18 ЛИСТОПАДА ПО ЗАПОРІЗЬКІЙ ОБЛАСТІ (оновлено 23:18)</h2>
            <div class="content">
                1.1: 00:00 – 04:30
                1.2: 04:00 – 08:00
            </div>
        </article>
        """
        # 17 Nov 23:20 -> 17.11 23:18
        _, upd_eve = zoe_parser.parse_outages_from_html(mock_html, now_kyiv=datetime(2026, 11, 17, 23, 20, tzinfo=TZ_KYIV))
        self.assertEqual(upd_eve, "17.11.2026 23:18")

        # 18 Nov 00:05 -> still 17.11 23:18
        _, upd_night = zoe_parser.parse_outages_from_html(mock_html, now_kyiv=datetime(2026, 11, 18, 0, 5, tzinfo=TZ_KYIV))
        self.assertEqual(upd_night, "17.11.2026 23:18")

        # 18 Nov 23:10 (8 min before 23:18!) -> must NOT jump to 18.11 23:18 in the future
        _, upd_late = zoe_parser.parse_outages_from_html(mock_html, now_kyiv=datetime(2026, 11, 18, 23, 10, tzinfo=TZ_KYIV))
        self.assertEqual(upd_late, "17.11.2026 23:18")

        # Genuine morning update on 18 Nov 05:57
        mock_morning = mock_html.replace("23:18", "05:57")
        _, upd_morning = zoe_parser.parse_outages_from_html(mock_morning, now_kyiv=datetime(2026, 11, 18, 6, 0, tzinfo=TZ_KYIV))
        self.assertEqual(upd_morning, "18.11.2026 05:57")

    def test_extract_update_match_avoids_prose_chas(self):
        """Verify generic Ukrainian word 'час' in prose is not misread as update time."""
        self.assertIsNone(zoe_parser.extract_update_match("18 листопада ГПВ", "У цей час 14:30 відбудуться перемикання."))
        m1 = zoe_parser.extract_update_match("ГПВ (оновлено: 05:57)", "")
        self.assertIsNotNone(m1)
        self.assertEqual(m1.group(1), "05:57")
        m2 = zoe_parser.extract_update_match("", "Час: 09:15")
        self.assertIsNotNone(m2)
        self.assertEqual(m2.group(1), "09:15")

    def test_is_no_outages_notice_fp_and_fn(self):
        """Verify both false positives and false negatives in no-outages detection."""
        # Must be FALSE (do NOT wipe active GPV!)
        self.assertFalse(zoe_parser.is_no_outages_notice("18 листопада ГАВ скасовано", "Графіки аварійних відключень скасовано. Продовжують діяти ГПВ."))
        self.assertFalse(zoe_parser.is_no_outages_notice("18 листопада ГАВ скасовано", "ГАВ скасовано, діють ГПВ."))
        self.assertFalse(zoe_parser.is_no_outages_notice("18 листопада ГОП скасовано", "Графіки обмеження потужності (ГОП) для промисловості скасовано."))
        self.assertFalse(zoe_parser.is_no_outages_notice("18 листопада", "Аварійних відключень не заплановано, діють ГПВ."))
        self.assertFalse(zoe_parser.is_no_outages_notice("18 листопада", "З 10:00 до 14:00 ГПВ скасовано."))
        self.assertFalse(zoe_parser.is_no_outages_notice("18 листопада", "До 14:00 погодинні відключення відсутні."))
        self.assertFalse(zoe_parser.is_no_outages_notice("18 листопада", "Для черги 1.1 ГПВ скасовано."))

        # Must be TRUE (valid full-day GPV cancellation / no outages)
        self.assertTrue(zoe_parser.is_no_outages_notice("18 листопада", "Обмеження не стосуються населення."))
        self.assertTrue(zoe_parser.is_no_outages_notice("18 листопада", "ГПВ на 18 листопада СКАСОВАНО."))
        self.assertTrue(zoe_parser.is_no_outages_notice("18 листопада", "ГПВ скасовано. Графіки обмеження потужності (ГОП) продовжують діяти."))
        self.assertTrue(zoe_parser.is_no_outages_notice("18 листопада", "Графіки погодинних відключень не застосовуватимуться."))
        self.assertTrue(zoe_parser.is_no_outages_notice("18 листопада", "ГПВ не застосовуються."))

    def test_pr_notice_date_line_does_not_overwrite_gpv(self):
        """Verify a PR post with '1.1.2026: ...' above real GPV does not overwrite the day."""
        mock_html = """
        <article>
            <h2>18 листопада — планові ремонти</h2>
            <div class="content">
                1.1.2026: з 10:00 до 12:00 профілактика підстанції
            </div>
        </article>
        <article>
            <h2>ОНОВЛЕНО ГПВ НА 18 ЛИСТОПАДА (оновлено 05:57)</h2>
            <div class="content">
                1.1: 00:00 – 04:00
                1.2: 04:00 – 08:00
            </div>
        </article>
        """
        now = datetime(2026, 11, 18, 9, 0, tzinfo=TZ_KYIV)
        days_data, upd = zoe_parser.parse_outages_from_html(mock_html, now_kyiv=now)
        ts_18 = str(int(datetime(2026, 11, 18, 0, 0, tzinfo=TZ_KYIV).timestamp()))
        self.assertEqual(days_data[ts_18]["GPV1.2"]["5"], "no")
        self.assertEqual(upd, "18.11.2026 05:57")

    def test_date_extraction_with_leading_numbers_and_invalid_date(self):
        """Verify date regex skips non-month words, rejects attached digits, and handles invalid dates."""
        mock_html = """
        <article>
            <h2>31 листопада — опечатка</h2>
            <div class="content">1.1: 10:00 - 12:00</div>
        </article>
        <article>
            <h2>3 черги ГПВ на 18 листопада (оновлено 08:00)</h2>
            <div class="content">
                1.1: 21:00 – 00:00
                1.1: 02:00 – 04:00
                1.2: 14:30 - 18:30
            </div>
        </article>
        """
        now = datetime(2026, 11, 18, 9, 0, tzinfo=TZ_KYIV)
        days_data, _ = zoe_parser.parse_outages_from_html(mock_html, now_kyiv=now)
        self.assertEqual(len(days_data), 1)
        day = next(iter(days_data.values()))
        self.assertEqual(day["GPV1.1"]["3"], "no")
        self.assertEqual(day["GPV1.1"]["22"], "no")
        self.assertEqual(day["GPV1.1"]["24"], "no")
        self.assertEqual(day["GPV1.2"]["15"], "second")
        self.assertEqual(day["GPV1.2"]["16"], "no")

    def test_fallback_schema_validation(self):
        """Verify is_valid_days_data matches ha-svitlo-yeah expectations."""
        valid = {"1794952800": _make_day_schedule()}
        self.assertTrue(zoe_parser.is_valid_days_data(valid))
        self.assertFalse(zoe_parser.is_valid_days_data([]))
        self.assertFalse(zoe_parser.is_valid_days_data({"bad-key": _make_day_schedule()}))
        self.assertFalse(zoe_parser.is_valid_days_data({"1794952800": {"GPV1.1": {"1": "invalid"}}}))
        self.assertFalse(zoe_parser.is_valid_days_data({"1794952800": {"GPV1.1": {"1": "yes"}}}))

    def test_build_output_json_does_not_synthesize_today(self):
        now = datetime(2026, 11, 18, 10, 0, tzinfo=TZ_KYIV)
        today_ts = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        yest_ts = str(today_ts - 86400)

        result = zoe_parser.build_output_json({yest_ts: _make_day_schedule()}, "14.11.2026 10:00", now_kyiv=now)
        self.assertEqual(result["fact"]["update"], "14.11.2026 10:00")
        self.assertNotIn(str(today_ts), result["fact"]["data"])

        existing = {"fact": {"data": {yest_ts: _make_day_schedule({"GPV1.1": {"1": "no"}})}}}
        merged = zoe_parser.merge_past_days({str(today_ts): _make_day_schedule()}, existing, today_ts)
        self.assertIn(yest_ts, merged)
        self.assertIn(str(today_ts), merged)

    def test_no_gpv_notices_on_explicit_dates_only(self):
        now = datetime(2026, 7, 14, 10, tzinfo=TZ_KYIV)
        metadata = {}
        html = """
        <article>
          <h2>ГРАФІКИ ПОГОДИННИХ ВІДКЛЮЧЕНЬ НА 15 ЛИПНЯ НЕ ЗАПЛАНОВАНІ</h2>
          <div class="content">15 липня введення ГПВ та ГОП по Запорізькій області не заплановано</div>
        </article>
        <article>
          <h2>ГПВ не заплановано з 16 по 18 липня</h2>
          <div class="content">Наступне повідомлення очікується 19 липня.</div>
        </article>
        """
        days, _ = zoe_parser.parse_outages_from_html(html, now, metadata)
        expected = {str(int(datetime(2026, 7, day, tzinfo=TZ_KYIV).timestamp())) for day in (15, 16, 17, 18)}
        self.assertEqual(set(days), expected)
        self.assertEqual(metadata["availability"], dict.fromkeys(expected, "no_gpv"))
        self.assertTrue(all(day == _make_day_schedule() for day in days.values()))

    def test_parser_keeps_confirmed_days_and_classifies_missing_days(self):
        now = datetime(2026, 11, 18, 12, tzinfo=TZ_KYIV)
        midnight = now.replace(hour=0)
        today, tomorrow = (str(int((midnight + timedelta(days=n)).timestamp())) for n in (0, 1))
        saved_day = _make_day_schedule({"GPV1.1": {"11": "no"}})
        cached = zoe_parser.build_output_json({today: saved_day, tomorrow: saved_day}, "18.11.2026 09:00", now)
        cached["schedule_updates"] = {today: "18.11.2026 09:00", tomorrow: "18.11.2026 09:00"}
        cached_with_status = zoe_parser.build_output_json({today: saved_day}, "18.11.2026 09:00", now)
        cached_with_status["schedule_updates"] = {today: "18.11.2026 09:00"}
        cached_with_status["availability"] = {today: "cached", tomorrow: "unpublished"}
        tomorrow_html = '<article><h2>ГПВ на 19 листопада (оновлено о 11:00)</h2><div class="content">1.1: 12:00–14:00</div></article>'
        unrelated = '<article><h2>Новини на 18 листопада</h2><div class="content">Технічні роботи скасовано.</div></article>'
        malformed = '<article><h2>ГПВ на 18 листопада</h2><div class="content"><p>1.1: з 10 до 12</p><p>1.2: з 12 до 14</p></div></article>'
        stale = '<article><h2>ГПВ на 18 листопада (оновлено о 08:00)</h2><div class="content">1.1: 12:00–14:00</div></article>'
        no_gpv = '<article><h2>ГПВ на 18 листопада не заплановані</h2><div class="content">Введення ГПВ не заплановано.</div></article>'
        legacy_all_yes = zoe_parser.build_output_json({today: _make_day_schedule()}, "18.11.2026 09:00", now)

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now.astimezone(tz)

        cases = (
            ("tomorrow only", tomorrow_html, None, None, "unpublished", False),
            ("tomorrow and fallback today", tomorrow_html, ({today: saved_day}, "18.11.2026 10:00"), None, None, True),
            ("unrelated notice", unrelated, None, None, "unpublished", False),
            ("legacy all-yes and fallback all-yes without notice", unrelated, ({today: _make_day_schedule()}, "18.11.2026 10:00"), legacy_all_yes, "unpublished", False),
            ("malformed schedule", malformed, None, None, "update_failed", False),
            ("malformed newest, older post follows", malformed + stale, None, None, "update_failed", False),
            ("source failure", TimeoutError(), None, None, "update_failed", False),
            ("cached during failure", TimeoutError(), None, cached, None, True),
            ("failure preserves previous availability", TimeoutError(), None, cached_with_status, "cached", True),
            ("fallback future-stamped same day", TimeoutError(), ({today: saved_day}, "18.11.2026 23:45"), None, None, True),
            ("fallback two days ahead rejected", TimeoutError(), ({today: saved_day}, "19.11.2026 23:45"), None, "update_failed", False),
            ("older official update", stale, None, cached, "cached", True),
            ("explicit no GPV", no_gpv, None, None, "no_gpv", True),
            ("same slots newer timestamp", TimeoutError(), ({today: saved_day}, "18.11.2026 10:00"), cached, None, True),
        )
        for name, source, fallback, existing, expected_status, has_today in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "data" / "Zaporizhzhiaoblenergo.json"
                if existing:
                    path.parent.mkdir()
                    path.write_text(json.dumps(existing), encoding="utf-8")
                fetch_args = {"side_effect": source} if isinstance(source, Exception) else {"return_value": source}
                with (
                    patch.object(zoe_parser, "__file__", str(Path(tmp) / "scripts" / "parser.py")),
                    patch.object(zoe_parser, "datetime", FixedDateTime),
                    patch.object(zoe_parser, "fetch_html_with_retries", **fetch_args),
                    patch.object(zoe_parser, "fetch_fallback_data", return_value=fallback),
                ):
                    zoe_parser.main()
                result = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(result.get("availability", {}).get(today), expected_status)
                self.assertEqual(today in result["fact"]["data"], has_today)
                if has_today:
                    expected_day = _make_day_schedule() if expected_status == "no_gpv" else saved_day
                    self.assertEqual(result["fact"]["data"][today], expected_day)
                if source == tomorrow_html or existing is cached:
                    self.assertIn(tomorrow, result["fact"]["data"])
                if expected_status == "no_gpv":
                    self.assertEqual(result["fact"]["update"], "18.11.2026 00:00")
                if name == "same slots newer timestamp":
                    self.assertEqual(result["fact"]["update"], "18.11.2026 10:00")
                if name == "fallback future-stamped same day":
                    self.assertEqual(result["schedule_updates"][today], "17.11.2026 23:45")
                    self.assertEqual(result["fact"]["update"], "17.11.2026 23:45")
                if name == "failure preserves previous availability":
                    self.assertEqual(result["availability"].get(tomorrow), "unpublished")

    def test_fetch_html_with_retries_non_200(self):
        """Verify non-200 response sets error and exhausts retries cleanly."""
        fake_resp = MagicMock()
        fake_resp.status = 204
        fake_resp.__enter__.return_value = fake_resp
        with patch("urllib.request.urlopen", return_value=fake_resp), patch("time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                zoe_parser.fetch_html_with_retries("https://example.invalid", retries=2, timeout=1)
            self.assertIn("HTTP 204", str(ctx.exception))

    def test_visual_diff_survives_tomorrow_addition_and_suppresses_all_yes_storm(self):
        """End-to-end git test: Visual Diff persists when tomorrow is published & suppresses midnight all-yes storm."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)

            data_file = repo / "data" / "Zaporizhzhiaoblenergo.json"
            data_file.parent.mkdir(parents=True, exist_ok=True)

            ts_today = "1794952800"
            ts_tom = "1795039200"

            def commit_json(days: dict, msg: str):
                payload = {"fact": {"data": days, "today": int(ts_today), "update": "18.11.2026 10:00"}}
                data_file.write_text(json.dumps(payload), encoding="utf-8")
                subprocess.run(["git", "add", "."], cwd=repo, check=True)
                subprocess.run(["git", "commit", "-m", msg], cwd=repo, check=True, capture_output=True)

            # Commit 1: midnight synthetic all-yes
            commit_json({ts_today: _make_day_schedule()}, "c1: midnight all-yes")
            # Commit 2: first real schedule (GPV1.1 h1=no, h2=no)
            day_v1 = _make_day_schedule({"GPV1.1": {"1": "no", "2": "no"}})
            commit_json({ts_today: day_v1}, "c2: first real schedule")

            # Verify synthetic all-yes -> real schedule suppresses red storm
            prevs_c2 = render_png.load_previous_jsons(data_file)
            prev_c2_day = render_png.find_previous_day_data(ts_today, day_v1, prevs_c2)
            self.assertEqual(render_png.compute_schedule_diff(day_v1, prev_c2_day), {})

            # Commit 3: real intraday update (GPV1.1 h2 cancelled -> yes)
            day_v2 = _make_day_schedule({"GPV1.1": {"1": "no", "2": "yes"}})
            commit_json({ts_today: day_v2}, "c3: intraday update")

            # Commit 4: tomorrow published (today unchanged!)
            commit_json({ts_today: day_v2, ts_tom: _make_day_schedule({"GPV2.1": {"5": "no"}})}, "c4: tomorrow added")

            # Today's Visual Diff MUST still show GPV1.1 h2=("less", "less") even after Commit 4!
            prevs_c4 = render_png.load_previous_jsons(data_file)
            prev_c4_day = render_png.find_previous_day_data(ts_today, day_v2, prevs_c4)
            diff = render_png.compute_schedule_diff(day_v2, prev_c4_day)
            self.assertEqual(diff.get("GPV1.1", {}).get("2"), ("less", "less"))

    def test_draw_slot_hours_and_placeholder_rendering(self):
        """Verify draw_slot, calc_outage_hours, and tomorrow placeholder render cleanly."""
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots()
        for i, s in enumerate(["yes", "no", "maybe", "first", "second", "mfirst", "msecond"]):
            render_png.draw_slot(ax, i, 0, 1.0, 0.5, s, diff=("less", None) if i == 0 else None)
        self.assertGreater(len(ax.patches), 14)
        plt.close(fig)

        slots = {"1": "yes", "2": "no", "3": "second", "4": "first", "5": "no"}
        self.assertEqual(render_png.calc_outage_hours(slots), 3.0)
        self.assertEqual(render_png.format_hours(3.0), "3")
        self.assertEqual(render_png.format_hours(9.5), "9.5")

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gpv-all-tomorrow.png"
            render_png.render_schedule_unavailable("19 листопада", "18.11.2026 05:57", out, "unpublished")
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 5000)

    def test_renderer_uses_kyiv_date_and_never_renders_missing_slots(self):
        now = datetime(2026, 11, 18, tzinfo=TZ_KYIV)
        today = str(int(now.timestamp()))
        tomorrow = str(int((now + timedelta(days=1)).timestamp()))
        yesterday = str(int((now - timedelta(days=1)).timestamp()))
        day = _make_day_schedule({"GPV1.1": {"11": "no"}})

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now.astimezone(tz)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schedule.json"
            path.write_text(json.dumps({
                "fact": {"data": {yesterday: day, today: {"GPV1.1": {"1": "yes"}}, tomorrow: day}, "today": int(yesterday)},
                "availability": {today: "update_failed", tomorrow: "cached"},
            }), encoding="utf-8")
            with (
                patch.object(sys, "argv", ["render_png.py", "--json", str(path), "--out", tmp, "--force"]),
                patch.object(render_png, "datetime", FixedDateTime),
                patch.object(render_png, "load_previous_jsons", return_value=[]),
                patch.object(render_png, "render_single_queue") as queue,
                patch.object(render_png, "render_schedule_unavailable") as unavailable,
                patch.object(render_png, "render_all_queues_summary") as summary,
            ):
                render_png.main()
            self.assertEqual(queue.call_count, 12)
            self.assertIsNone(queue.call_args_list[0].args[0])
            self.assertEqual(queue.call_args_list[0].args[1], day["GPV1.1"])
            self.assertEqual(queue.call_args_list[0].kwargs["today_status"], "update_failed")
            self.assertEqual(unavailable.call_args.args[0], "18 листопада")
            self.assertEqual(unavailable.call_args.args[3], "update_failed")
            self.assertEqual(summary.call_args.args[:2], (day, "19 листопада"))
            self.assertEqual(summary.call_args.kwargs["status"], "cached")

    def test_ha_parser_parity(self):
        """Optional parity check when HA_ZOE_PARSER_PATH env var points to Home Assistant script."""
        ha_env_path = os.environ.get("HA_ZOE_PARSER_PATH")
        if not ha_env_path:
            self.skipTest("HA_ZOE_PARSER_PATH not set")
        ha_parser_path = Path(ha_env_path)
        self.assertTrue(ha_parser_path.exists(), f"Missing {ha_parser_path}")
        content = ha_parser_path.read_text(encoding="utf-8")
        for token in (
            "_ArticleExtractor",
            "extract_articles",
            "extract_update_match",
            "resolve_update_datetime",
            "is_no_outages_notice",
            "merge_past_days",
            "atomic_write_json",
            "pending_github_sync",
            "pending_dispatch",
            "skip_parser",
        ):
            self.assertIn(token, content)

    def test_telegram_extractor_synthetic(self):
        """Verify Telegram HTML extractor order, reply exclusion, time parsing, and schedule output."""
        tg_html = """
        <div class="tgme_widget_message js-widget_message" data-post="Zaporizhzhyaoblenergo_news/1">
          <div class="tgme_widget_message_text js-message_text">Загальна новина без графіку.</div>
          <time datetime="2026-11-20T10:00:00+00:00"></time>
        </div>
        <div class="tgme_widget_message js-widget_message" data-post="Zaporizhzhyaoblenergo_news/2">
          <div class="tgme_widget_message_text js-message_text">Оновлено графік погодинних відключень на 21.11.2026.<br/><br/>⚡ Черга 1.1<br/> з 03:00 до 08:00<br/> з 12:00 до 17:00<br/><br/>⚡ Черга 1.2<br/> з 03:30 до 08:30</div>
          <time datetime="2026-11-20T21:49:31+00:00"></time>
        </div>
        <div class="tgme_widget_message js-widget_message" data-post="Zaporizhzhyaoblenergo_news/3">
          <a class="tgme_widget_message_reply" href="https://t.me/Zaporizhzhyaoblenergo_news/2">
            <div class="tgme_widget_message_text js-message_reply_text">REPLY_TEXT_MUST_IGNORE 1.1: 00:00-24:00</div>
          </a>
          <div class="tgme_widget_message_text js-message_text">Офіційне пояснення без графіку.</div>
          <time datetime="2026-11-21T09:00:00+00:00"></time>
        </div>
        """
        posts = zoe_parser.extract_telegram_posts(tg_html)
        self.assertEqual(len(posts), 3)
        self.assertNotIn("REPLY_TEXT_MUST_IGNORE", posts[0][1])
        self.assertIn("Офіційне пояснення", posts[0][1])
        self.assertEqual(posts[1][2], datetime(2026, 11, 20, 23, 49, 31, tzinfo=TZ_KYIV))
        self.assertIn("1.1: з 03:00 до 08:00, з 12:00 до 17:00", posts[1][1])
        self.assertIn("21 листопада", posts[1][1])

        now = datetime(2026, 11, 21, 12, 0, tzinfo=TZ_KYIV)
        metadata = {}
        days, _ = zoe_parser.parse_telegram_html(tg_html, now_kyiv=now, metadata=metadata)
        ts = str(int(datetime(2026, 11, 21, 0, 0, tzinfo=TZ_KYIV).timestamp()))
        self.assertIn(ts, days)
        self.assertEqual(days[ts]["GPV1.1"]["4"], "no")
        self.assertEqual(days[ts]["GPV1.1"]["9"], "yes")
        self.assertEqual(days[ts]["GPV1.2"]["4"], "second")
        self.assertEqual(metadata["schedule_updates"][ts], "20.11.2026 23:49")

    def test_telegram_fixture_real_page(self):
        """Verify parsing of saved real Telegram channel HTML fixture."""
        fixture = REPO_ROOT / "tests" / "fixtures" / "telegram_sample.html"
        html_text = fixture.read_text(encoding="utf-8")
        now = datetime(2026, 11, 21, 12, 0, tzinfo=TZ_KYIV)
        days, _ = zoe_parser.parse_telegram_html(html_text, now_kyiv=now)
        ts = str(int(datetime(2026, 11, 21, 0, 0, tzinfo=TZ_KYIV).timestamp()))
        self.assertIn(ts, days)
        self.assertEqual(set(days[ts]), set(zoe_parser.ALL_QUEUES))
        for h in ("4", "5", "6", "7", "8"):
            self.assertEqual(days[ts]["GPV1.1"][h], "no")
        self.assertEqual(days[ts]["GPV2.1"]["1"], "no")

    def test_telegram_secondary_source_and_anti_churn(self):
        """Verify main() falls back to official Telegram when ZOE fails, and keeps HA timestamps on identical slots."""
        now = datetime(2026, 11, 21, 12, 0, tzinfo=TZ_KYIV)
        midnight = now.replace(hour=0)
        today = str(int(midnight.timestamp()))
        tomorrow = str(int((midnight + timedelta(days=1)).timestamp()))
        tg_html = """
        <div class="tgme_widget_message js-widget_message" data-post="Zaporizhzhyaoblenergo_news/2">
          <div class="tgme_widget_message_text js-message_text">Оновлено графік погодинних відключень на 21.11.2026.<br/><br/>⚡ Черга 1.1<br/> з 03:00 до 08:00<br/><br/>⚡ Черга 1.2<br/> з 03:30 до 08:30</div>
          <time datetime="2026-11-20T21:49:31+00:00"></time>
        </div>
        """

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now.astimezone(tz)

        def fake_fetch(url, *args, **kwargs):
            if url == zoe_parser.URL_ZOE_TELEGRAM:
                return tg_html
            raise TimeoutError("geo-blocked")

        # Case 1: fresh fetch via Telegram secondary source
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "Zaporizhzhiaoblenergo.json"
            with (
                patch.object(zoe_parser, "__file__", str(Path(tmp) / "scripts" / "parser.py")),
                patch.object(zoe_parser, "datetime", FixedDateTime),
                patch.object(zoe_parser, "fetch_html_with_retries", side_effect=fake_fetch),
                patch.object(zoe_parser, "fetch_fallback_data", return_value=None),
            ):
                zoe_parser.main()
            result = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn(today, result["fact"]["data"])
            self.assertEqual(result["fact"]["data"][today]["GPV1.1"]["4"], "no")
            self.assertEqual(result["schedule_updates"][today], "20.11.2026 23:49")
            self.assertEqual(result["fact"]["update"], "20.11.2026 23:49")

            # Case 2: anti-churn — existing HA JSON with identical slots & 23:45 stays untouched
            existing = zoe_parser.build_output_json({today: result["fact"]["data"][today]}, "20.11.2026 23:45", now)
            existing["schedule_updates"] = {today: "20.11.2026 23:45"}
            existing["availability"] = {tomorrow: "unpublished"}
            original_raw = json.dumps(existing, ensure_ascii=False, indent=2)
            path.write_text(original_raw, encoding="utf-8")
            with (
                patch.object(zoe_parser, "__file__", str(Path(tmp) / "scripts" / "parser.py")),
                patch.object(zoe_parser, "datetime", FixedDateTime),
                patch.object(zoe_parser, "fetch_html_with_retries", side_effect=fake_fetch),
                patch.object(zoe_parser, "fetch_fallback_data", return_value=None),
            ):
                zoe_parser.main()
            self.assertEqual(path.read_text(encoding="utf-8"), original_raw)
            after = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(after["schedule_updates"][today], "20.11.2026 23:45")
            self.assertEqual(after["availability"].get(tomorrow), "unpublished")

    def test_telegram_ignores_past_days_and_picks_newest_of_tg_and_oe(self):
        """Verify TG only affects current days and OE upgrades today only when newer than TG."""
        now = datetime(2026, 11, 21, 12, 0, tzinfo=TZ_KYIV)
        midnight = now.replace(hour=0)
        yesterday = str(int((midnight - timedelta(days=1)).timestamp()))
        today = str(int(midnight.timestamp()))
        tg_html = """
        <div class="tgme_widget_message js-widget_message" data-post="Zaporizhzhyaoblenergo_news/1">
          <div class="tgme_widget_message_text js-message_text">Оновлено графік погодинних відключень на 20.11.2026.<br/><br/>⚡ Черга 1.1<br/> з 00:00 до 05:00<br/><br/>⚡ Черга 1.2<br/> з 05:00 до 10:00</div>
          <time datetime="2026-11-20T12:00:00+00:00"></time>
        </div>
        <div class="tgme_widget_message js-widget_message" data-post="Zaporizhzhyaoblenergo_news/2">
          <div class="tgme_widget_message_text js-message_text">Оновлено графік погодинних відключень на 21.11.2026.<br/><br/>⚡ Черга 1.1<br/> з 03:00 до 08:00<br/><br/>⚡ Черга 1.2<br/> з 03:30 до 08:30</div>
          <time datetime="2026-11-20T21:49:31+00:00"></time>
        </div>
        """

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now.astimezone(tz)

        def fake_fetch(url, *args, **kwargs):
            if url == zoe_parser.URL_ZOE_TELEGRAM:
                return tg_html
            raise TimeoutError("geo-blocked")

        # 1. Past days from TG ignored even when existing has no schedule_updates for yesterday
        past_day = _make_day_schedule({"GPV6.2": {"20": "no"}})
        oe_day = _make_day_schedule({"GPV2.2": {"10": "no"}})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "Zaporizhzhiaoblenergo.json"
            path.parent.mkdir()
            existing = zoe_parser.build_output_json({yesterday: past_day}, "20.11.2026 23:45", now)
            path.write_text(json.dumps(existing), encoding="utf-8")
            with (
                patch.object(zoe_parser, "__file__", str(Path(tmp) / "scripts" / "parser.py")),
                patch.object(zoe_parser, "datetime", FixedDateTime),
                patch.object(zoe_parser, "fetch_html_with_retries", side_effect=fake_fetch),
                patch.object(zoe_parser, "fetch_fallback_data", return_value=None),
            ):
                zoe_parser.main()
            res = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(res["fact"]["data"][yesterday], past_day)
            self.assertNotIn(yesterday, res["schedule_updates"])

        # 2. OE newer than TG (21.11 11:00 vs 20.11 23:49) wins
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "Zaporizhzhiaoblenergo.json"
            with (
                patch.object(zoe_parser, "__file__", str(Path(tmp) / "scripts" / "parser.py")),
                patch.object(zoe_parser, "datetime", FixedDateTime),
                patch.object(zoe_parser, "fetch_html_with_retries", side_effect=fake_fetch),
                patch.object(zoe_parser, "fetch_fallback_data", return_value=({today: oe_day}, "21.11.2026 11:00")),
            ):
                zoe_parser.main()
            res = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(res["fact"]["data"][today], oe_day)
            self.assertEqual(res["schedule_updates"][today], "21.11.2026 11:00")

        # 3. OE older than TG (20.11 20:00 vs 20.11 23:49) loses
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "Zaporizhzhiaoblenergo.json"
            with (
                patch.object(zoe_parser, "__file__", str(Path(tmp) / "scripts" / "parser.py")),
                patch.object(zoe_parser, "datetime", FixedDateTime),
                patch.object(zoe_parser, "fetch_html_with_retries", side_effect=fake_fetch),
                patch.object(zoe_parser, "fetch_fallback_data", return_value=({today: oe_day}, "20.11.2026 20:00")),
            ):
                zoe_parser.main()
            res = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(res["fact"]["data"][today]["GPV1.1"]["4"], "no")
            self.assertEqual(res["fact"]["data"][today]["GPV2.2"]["10"], "yes")
            self.assertEqual(res["schedule_updates"][today], "20.11.2026 23:49")


if __name__ == "__main__":
    unittest.main()
