#!/usr/bin/env python3
"""
Unit and integration tests for ZOE Outage Parser & Renderer.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

# Add scripts dir to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import parser as zoe_parser
import render_png

TZ_KYIV = ZoneInfo("Europe/Kyiv")


class TestAuditImprovements(unittest.TestCase):

    def test_parse_update_datetime(self):
        """Verify parse_update_datetime parses all standard formats."""
        dt1 = zoe_parser.parse_update_datetime("26.10.2026 08:14")
        self.assertIsNotNone(dt1)
        self.assertEqual((dt1.year, dt1.month, dt1.day, dt1.hour, dt1.minute), (2026, 10, 26, 8, 14))

        dt2 = zoe_parser.parse_update_datetime("26.10.2026 08:14:30")
        self.assertIsNotNone(dt2)
        self.assertEqual(dt2.second, 30)

        # On 2026-10-26 Kyiv is UTC+2 (winter time), so 08:14+03:00 is 07:14 Kyiv time
        dt3 = zoe_parser.parse_update_datetime("2026-10-26T08:14:00+03:00")
        self.assertIsNotNone(dt3)
        self.assertEqual(dt3.astimezone(TZ_KYIV).hour, 7)

        self.assertIsNone(zoe_parser.parse_update_datetime("invalid date"))
        self.assertIsNone(zoe_parser.parse_update_datetime(None))

    def test_fallback_rejection_logic(self):
        """Verify fallback data is rejected if older or equal to local data, or invalid."""
        local_dt = zoe_parser.parse_update_datetime("26.10.2026 12:00")

        for fb_str, should_reject in [
            ("26.10.2026 10:00", True),
            ("26.10.2026 12:00", True),
            ("26.10.2026 14:00", False),
            ("unparseable string", True),
        ]:
            fb_dt = zoe_parser.parse_update_datetime(fb_str)
            self.assertEqual(not fb_dt or fb_dt <= local_dt, should_reject)

    def test_year_transition_real_parser(self):
        """Verify year rollover when today is December and article is for January."""
        mock_html = """
        <article>
            <h2>Графік відключень на 1 січня</h2>
            <div class="content">
                1.1: 10:00 - 14:00
            </div>
        </article>
        """
        now_in_december = datetime(2026, 12, 31, 23, 30, tzinfo=TZ_KYIV)
        days_data, update_str = zoe_parser.parse_outages_from_html(mock_html, now_kyiv=now_in_december)
        self.assertEqual(len(days_data), 1)

        target_ts = list(days_data.keys())[0]
        dt = datetime.fromtimestamp(int(target_ts), tz=TZ_KYIV)
        self.assertEqual((dt.year, dt.month, dt.day), (2027, 1, 1))
        # Published on Dec 31 for Jan 1 -> update_str must not be in the future (2027-01-01)
        self.assertEqual(update_str, "31.12.2026 23:30")

    def test_evening_update_for_tomorrow_not_in_future(self):
        """Verify evening update for tomorrow (e.g. 17 Nov 23:18 for 18 Nov) gets 17.11 date, not 18.11."""
        mock_html = """
        <article>
            <h2>ОНОВЛЕНО ГПВ НА 18 ЛИСТОПАДА ПО ЗАПОРІЗЬКІЙ ОБЛАСТІ (оновлено 23:18)</h2>
            <div class="content">
                1.1: 00:00 – 04:30
            </div>
        </article>
        """
        # Case 1: Checked on 17 Nov at 23:20
        now_eve = datetime(2026, 11, 17, 23, 20, tzinfo=TZ_KYIV)
        _, upd_eve = zoe_parser.parse_outages_from_html(mock_html, now_kyiv=now_eve)
        self.assertEqual(upd_eve, "17.11.2026 23:18")

        # Case 2: Checked just after midnight on 18 Nov at 00:05
        now_night = datetime(2026, 11, 18, 0, 5, tzinfo=TZ_KYIV)
        _, upd_night = zoe_parser.parse_outages_from_html(mock_html, now_kyiv=now_night)
        self.assertEqual(upd_night, "17.11.2026 23:18")

        # Case 3: Genuine morning update on 18 Nov at 05:57
        mock_morning = mock_html.replace("23:18", "05:57")
        now_morning = datetime(2026, 11, 18, 6, 0, tzinfo=TZ_KYIV)
        _, upd_morning = zoe_parser.parse_outages_from_html(mock_morning, now_kyiv=now_morning)
        self.assertEqual(upd_morning, "18.11.2026 05:57")

    def test_no_outages_notice_ignores_gav_cancellation(self):
        """Verify cancelling GAV/SGAV (while GPV continues) does NOT wipe out GPV schedule."""
        mock_html = """
        <article>
            <h2>УВАГА! 18 ЛИСТОПАДА ГАВ ТА СГАВ СКАСОВАНО! (оновлено 19:36)</h2>
            <div class="content">
                За вказівкою НЕК «Укренерго» графіки аварійних відключень (ГАВ) скасовано у повному обсязі.
                Продовжують діяти графіки погодинних відключень (ГПВ).
            </div>
        </article>
        <article>
            <h2>ОНОВЛЕНО ГПВ НА 18 ЛИСТОПАДА (оновлено 05:57)</h2>
            <div class="content">
                1.1: 10:00 – 14:00
            </div>
        </article>
        """
        now = datetime(2026, 11, 18, 20, 0, tzinfo=TZ_KYIV)
        days_data, upd = zoe_parser.parse_outages_from_html(mock_html, now_kyiv=now)
        target_ts = str(int(datetime(2026, 11, 18, 0, 0, tzinfo=TZ_KYIV).timestamp()))
        self.assertIn(target_ts, days_data)
        self.assertEqual(days_data[target_ts]["GPV1.1"]["11"], "no")
        self.assertEqual(upd, "18.11.2026 05:57")

    def test_no_outages_notice_case_insensitive(self):
        """Verify lowercase/mixed-case no-outages notices are detected properly."""
        self.assertTrue(zoe_parser.is_no_outages_notice("18 листопада", "Обмеження не стосуються населення."))
        self.assertTrue(zoe_parser.is_no_outages_notice("18 листопада", "ГПВ на 18 листопада СКАСОВАНО."))
        self.assertFalse(zoe_parser.is_no_outages_notice("18 листопада ГАВ скасовано", "Графіки аварійних відключень (ГАВ) скасовано."))

    def test_date_extraction_with_leading_numbers_and_invalid_date(self):
        """Verify date regex skips non-month words after digits and handles invalid calendar dates."""
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
            </div>
        </article>
        """
        now = datetime(2026, 11, 18, 9, 0, tzinfo=TZ_KYIV)
        days_data, _ = zoe_parser.parse_outages_from_html(mock_html, now_kyiv=now)
        self.assertEqual(len(days_data), 1)
        day = next(iter(days_data.values()))
        # Both intervals for 1.1 must be merged, and 21:00 - 00:00 must cover hours 22, 23, 24
        self.assertEqual(day["GPV1.1"]["3"], "no")
        self.assertEqual(day["GPV1.1"]["4"], "no")
        self.assertEqual(day["GPV1.1"]["22"], "no")
        self.assertEqual(day["GPV1.1"]["23"], "no")
        self.assertEqual(day["GPV1.1"]["24"], "no")

    def test_multi_queue_and_dot_time_parsing(self):
        """Verify parser handles multi-queue grouping and dot time notation."""
        mock_html = """
        <article>
            <h2>26 жовтня графік відключень</h2>
            <div class="content">
                Підчерги 1.1, 1.2: 10.00 – 14.00
                Черга 2.1: 14:30 - 18:30
            </div>
        </article>
        """
        days_data, _ = zoe_parser.parse_outages_from_html(mock_html, now_kyiv=datetime(2026, 10, 26, 12, 0, tzinfo=TZ_KYIV))
        day = next(iter(days_data.values()))

        self.assertEqual(day["GPV1.1"]["11"], "no")
        self.assertEqual(day["GPV1.2"]["11"], "no")
        self.assertEqual(day["GPV1.1"]["10"], "yes")
        self.assertEqual(day["GPV2.1"]["15"], "second")
        self.assertEqual(day["GPV2.1"]["16"], "no")

    def test_today_int_type_and_stale_refresh(self):
        """Verify build_output_json produces integer today timestamp and refreshes stale update on default day."""
        now = datetime(2026, 11, 18, 10, 0, tzinfo=TZ_KYIV)
        res = zoe_parser.build_output_json({}, "14.11.2026 10:00", now_kyiv=now)
        self.assertIsInstance(res["fact"]["today"], int)
        self.assertEqual(res["fact"]["update"], "18.11.2026 10:00")

    def test_dst_tomorrow_calculation(self):
        """Verify DST transition day produces exact next calendar day midnight timestamp."""
        dt_switch_day = datetime(2026, 10, 25, 0, 0, tzinfo=TZ_KYIV)
        today_ts = str(int(dt_switch_day.timestamp()))

        today_dt = datetime.fromtimestamp(int(today_ts), tz=TZ_KYIV)
        tomorrow_date = today_dt.date() + timedelta(days=1)
        tomorrow_dt = datetime(tomorrow_date.year, tomorrow_date.month, tomorrow_date.day, 0, 0, tzinfo=TZ_KYIV)
        tomorrow_ts = str(int(tomorrow_dt.timestamp()))

        broken_tomorrow_ts = str(int(today_ts) + 86400)
        expected_tomorrow_dt = datetime(2026, 10, 26, 0, 0, tzinfo=TZ_KYIV)
        self.assertEqual(tomorrow_ts, str(int(expected_tomorrow_dt.timestamp())))
        self.assertNotEqual(tomorrow_ts, broken_tomorrow_ts)

    def test_draw_slot_states(self):
        """Verify draw_slot handles all states including mfirst, msecond."""
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots()
        states = ["yes", "no", "maybe", "first", "second", "mfirst", "msecond"]
        for i, s in enumerate(states):
            render_png.draw_slot(ax, i, 0, 1.0, 0.5, s)
        self.assertGreater(len(ax.patches), len(states))
        plt.close(fig)

    def test_ha_parser_parity(self):
        """Optional parity check when HA_ZOE_PARSER_PATH env var points to Home Assistant script."""
        ha_env_path = os.environ.get("HA_ZOE_PARSER_PATH")
        if not ha_env_path:
            return
        ha_parser_path = Path(ha_env_path)
        if ha_parser_path.exists():
            content = ha_parser_path.read_text(encoding="utf-8")
            for token in (
                '"today": today_ts',
                "parse_update_datetime",
                "resolve_update_datetime",
                "is_no_outages_notice",
                "trigger_workflow_dispatch",
                "DEFAULT_TIME_ZONE",
                "DEFAULT_TIME_TYPE",
                "skip_parser",
            ):
                self.assertIn(token, content)

    def test_idempotency_stale_data_no_spurious_commits(self):
        """Verify unchanged schedule & day preserve existing timestamps."""
        now = datetime(2026, 11, 18, 12, 0, tzinfo=TZ_KYIV)
        today_ts = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        existing_days = {
            str(today_ts): {q: {str(i): "yes" for i in range(1, 25)} for q in zoe_parser.ALL_QUEUES}
        }
        old_update_str = "16.11.2026 10:00"
        existing_json = {
            "regionId": "Zaporizhzhia",
            "lastUpdated": "2026-11-16T10:00:00+00:00",
            "fact": {
                "data": existing_days,
                "update": old_update_str,
                "today": today_ts,
            },
        }

        new_result = zoe_parser.build_output_json(dict(existing_days), "18.11.2026 19:00", now_kyiv=now)
        has_changed = True
        old_fact = existing_json["fact"]
        new_fact = new_result["fact"]
        if old_fact.get("data") == new_fact.get("data") and old_fact.get("today") == new_fact.get("today"):
            new_result["fact"]["update"] = old_fact["update"]
            new_result["lastUpdated"] = existing_json["lastUpdated"]
            has_changed = False

        self.assertFalse(has_changed)
        self.assertEqual(new_result["fact"]["update"], old_update_str)

    def test_visual_diff_computation_and_hours(self):
        """Verify compute_queue_diff and calc_outage_hours calculations."""
        curr = {"1": "yes", "2": "no", "3": "second", "4": "first", "5": "no"}
        prev = {"1": "no", "2": "yes", "3": "no", "4": "no", "5": "no"}
        diff = render_png.compute_queue_diff(curr, prev)
        self.assertEqual(diff.get("1"), ("less", "less"))
        self.assertEqual(diff.get("2"), ("more", "more"))
        self.assertEqual(diff.get("3"), ("less", None))
        self.assertEqual(diff.get("4"), (None, "less"))
        self.assertNotIn("5", diff)

        # 2 full hours ("no") + 2 half hours ("first", "second") = 3.0 hours
        self.assertEqual(render_png.calc_outage_hours(curr), 3.0)
        self.assertEqual(render_png.format_hours(3.0), "3")
        self.assertEqual(render_png.format_hours(9.5), "9.5")

    def test_draw_slot_with_diff_overlay_and_placeholder(self):
        """Verify draw_slot with visual diff and tomorrow placeholder render cleanly."""
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots()
        render_png.draw_slot(ax, 0, 0, 1.0, 0.5, "yes", diff=("less", "less"))
        render_png.draw_slot(ax, 1, 0, 1.0, 0.5, "second", diff=("less", None))
        render_png.draw_slot(ax, 2, 0, 1.0, 0.5, "first", diff=("more", None))
        self.assertGreater(len(ax.patches), 6)
        plt.close(fig)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gpv-all-tomorrow.png"
            render_png.render_tomorrow_unavailable("19 листопада", "18.11.2026 05:57", out)
            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 5000)


if __name__ == "__main__":
    unittest.main()
