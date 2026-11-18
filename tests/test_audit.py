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

    def test_build_output_json_synthetic_control_and_merge_past_days(self):
        """Verify allow_synthetic_today=False does not fabricate fresh update or synthetic day."""
        now = datetime(2026, 11, 18, 10, 0, tzinfo=TZ_KYIV)
        today_ts = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        yest_ts = str(today_ts - 86400)

        # When allow_synthetic_today=True (live ZOE returned no new posts for >2d), refresh update
        res_live = zoe_parser.build_output_json({}, "14.11.2026 10:00", now_kyiv=now, allow_synthetic_today=True)
        self.assertEqual(res_live["fact"]["update"], "18.11.2026 10:00")
        self.assertIn(str(today_ts), res_live["fact"]["data"])

        # When allow_synthetic_today=False (sources failed), must NOT fabricate update or today
        res_fail = zoe_parser.build_output_json({yest_ts: _make_day_schedule()}, "14.11.2026 10:00", now_kyiv=now, allow_synthetic_today=False)
        self.assertEqual(res_fail["fact"]["update"], "14.11.2026 10:00")
        self.assertNotIn(str(today_ts), res_fail["fact"]["data"])

        # merge_past_days preserves yesterday when fallback only has today
        existing = {"fact": {"data": {yest_ts: _make_day_schedule({"GPV1.1": {"1": "no"}})}}}
        merged = zoe_parser.merge_past_days({str(today_ts): _make_day_schedule()}, existing, today_ts)
        self.assertIn(yest_ts, merged)
        self.assertIn(str(today_ts), merged)

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
            render_png.render_tomorrow_unavailable("19 листопада", "18.11.2026 05:57", out)
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 5000)

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


if __name__ == "__main__":
    unittest.main()
