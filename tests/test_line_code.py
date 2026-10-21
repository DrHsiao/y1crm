"""官網預約碼（y1web）在 LINE 的辨識規則（純函式，不連資料庫、不打 LINE API）。

執行：./venv/bin/python -m unittest tests.test_line_code
"""
import unittest

from api import line_bot


class FindCode(unittest.TestCase):
    def test_explicit(self):
        self.assertEqual(line_bot._find_link_code("預約碼 K7Q2XM"), ("K7Q2XM", True))
        self.assertEqual(line_bot._find_link_code("預約碼：k7q2xm"), ("K7Q2XM", True))
        self.assertEqual(line_bot._find_link_code("您好，我的預約碼K7Q2XM 謝謝"), ("K7Q2XM", True))

    def test_bare_code(self):
        self.assertEqual(line_bot._find_link_code(" k7q2xm "), ("K7Q2XM", False))
        self.assertEqual(line_bot._find_link_code("K7Q20M"), (None, False))     # 含 0：不是官網發的碼
        self.assertEqual(line_bot._find_link_code("K7Q2XMA"), (None, False))

    def test_not_code(self):
        # 一般預約訊息要繼續走「預約」關鍵字（回表單連結），不能被預約碼攔走
        self.assertEqual(line_bot._find_link_code("我要預約"), (None, False))
        self.assertEqual(line_bot._find_link_code("預約碼是什麼"), (None, False))
        self.assertEqual(line_bot._find_link_code("你好"), (None, False))


if __name__ == "__main__":
    unittest.main()
