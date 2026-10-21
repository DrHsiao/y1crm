"""LINE 預約可選日期：明天起 14 天、週日公休不列（和官網 y1web app/booking.py 相同）。

執行：./venv/bin/python -m unittest tests.test_booking_dates
"""
import unittest
from datetime import date, timedelta

from api import line_bot


class BookingDates(unittest.TestCase):
    def test_no_sunday(self):
        ds = [date.fromisoformat(d["v"]) for d in line_bot._booking_dates()]
        self.assertEqual(len(ds), 12)                         # 任何連續 14 天都剛好有 2 個週日
        self.assertTrue(all(d.weekday() != 6 for d in ds))
        self.assertGreaterEqual(ds[0], date.today() + timedelta(days=1))
        self.assertLessEqual(ds[-1], date.today() + timedelta(days=14))
        self.assertTrue(all("（日）" not in d["label"] for d in line_bot._booking_dates()))


if __name__ == "__main__":
    unittest.main()
