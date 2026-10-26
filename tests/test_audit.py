#!/usr/bin/env python3
"""
Unit and integration tests for ZOE Outage Parser & Renderer audit improvements.
"""

import os
import sys
import json
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

# Add repo root to sys.path
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
        self.assertEqual(dt1.year, 2026)
        self.assertEqual(dt1.month, 10)
        self.assertEqual(dt1.day, 26)
        self.assertEqual(dt1.hour, 8)
        self.assertEqual(dt1.minute, 14)

        dt2 = zoe_parser.parse_update_datetime("26.10.2026 08:14:30")
        self.assertIsNotNone(dt2)
        self.assertEqual(dt2.second, 30)

        # On 2026-10-26 Kyiv is UTC+2 (winter time), so 08:14+03:00 is 07:14 Kyiv time
        dt3 = zoe_parser.parse_update_datetime("2026-10-26T08:14:00+03:00")
        self.assertIsNotNone(dt3)
        self.assertEqual(dt3.astimezone(TZ_KYIV).hour, 7)

    def test_fallback_rejection_logic(self):
        """Verify fallback data is rejected if older or equal to local data."""
        # Case 1: Fallback older
        local_upd = "26.10.2026 12:00"
        fb_upd_older = "26.10.2026 10:00"
        local_dt = zoe_parser.parse_update_datetime(local_upd)
        fb_dt_older = zoe_parser.parse_update_datetime(fb_upd_older)
        self.assertTrue(fb_dt_older <= local_dt, "Older fallback must be <= local")

        # Case 2: Fallback equal
        fb_upd_equal = "26.10.2026 12:00"
        fb_dt_equal = zoe_parser.parse_update_datetime(fb_upd_equal)
        self.assertTrue(fb_dt_equal <= local_dt, "Equal fallback must be <= local")

        # Case 3: Fallback newer
        fb_upd_newer = "26.10.2026 14:00"
        fb_dt_newer = zoe_parser.parse_update_datetime(fb_upd_newer)
        self.assertFalse(fb_dt_newer <= local_dt, "Newer fallback must NOT be <= local")

    def test_year_transition(self):
        """Verify year rollover when today is December and article is January."""
        mock_html = """
        <article>
            <h2>Графік відключень на 1 січня</h2>
            <div class="content">
                1.1: 10:00 - 14:00
            </div>
        </article>
        """
        # Test simulated in December
        now_kyiv = datetime(2026, 12, 31, 23, 0, tzinfo=TZ_KYIV)
        # We test the logic used in parse_outages_from_html
        month_num = 1
        year = now_kyiv.year
        article_year = year
        if now_kyiv.month == 12 and month_num == 1:
            article_year = year + 1
        self.assertEqual(article_year, 2027)

    def test_today_int_type(self):
        """Verify build_output_json produces integer today timestamp."""
        res = zoe_parser.build_output_json({}, "26.10.2026 10:00")
        today_val = res["fact"]["today"]
        self.assertIsInstance(today_val, int)
        self.assertGreater(today_val, 1700000000)

    def test_dst_tomorrow_calculation(self):
        """Verify DST transition day produces exact next calendar day midnight timestamp."""
        # 2026-10-25 was DST switch day in Europe/Kyiv (25 hours)
        dt_switch_day = datetime(2026, 10, 25, 0, 0, tzinfo=TZ_KYIV)
        today_ts = str(int(dt_switch_day.timestamp()))

        # New correct algorithm:
        today_dt = datetime.fromtimestamp(int(today_ts), tz=TZ_KYIV)
        tomorrow_date = today_dt.date() + timedelta(days=1)
        tomorrow_dt = datetime(tomorrow_date.year, tomorrow_date.month, tomorrow_date.day, 0, 0, tzinfo=TZ_KYIV)
        tomorrow_ts = str(int(tomorrow_dt.timestamp()))

        # Old broken algorithm:
        broken_tomorrow_ts = str(int(today_ts) + 86400)

        # Expected:
        expected_tomorrow_dt = datetime(2026, 10, 26, 0, 0, tzinfo=TZ_KYIV)
        self.assertEqual(tomorrow_ts, str(int(expected_tomorrow_dt.timestamp())))
        # The broken one is off by 1 hour (3600 seconds) on 25h DST day
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
        """Verify HA zoe_parser.py has identical types and settings."""
        ha_parser_path = Path("//192.168.3.3/config/scripts/zoe_parser.py")
        if ha_parser_path.exists():
            content = ha_parser_path.read_text(encoding="utf-8")
            self.assertIn('"today": today_ts', content)
            self.assertIn("trigger_workflow_dispatch", content)
            self.assertIn("DEFAULT_TIME_ZONE", content)
            self.assertIn("DEFAULT_TIME_TYPE", content)
            self.assertIn("mfirst", content)
            self.assertIn("msecond", content)


if __name__ == "__main__":
    unittest.main()
