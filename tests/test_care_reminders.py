"""保養提醒：時間點、去重、訊息草稿（純函式，不連資料庫）。

執行：./venv/bin/python -m unittest tests.test_care_reminders
"""
import unittest
from datetime import date

from api import care_reminders as cr


def buy(pid=1, cid=10, d=date(2026, 1, 31), end=None):
    return {"id": pid, "customer_id": cid, "transaction_date": d, "warranty_end_date": end}


class Milestones(unittest.TestCase):
    def test_schedule(self):
        ms = cr.milestones(buy(end=date(2028, 1, 31)))
        self.assertEqual(ms[0]["due"], date(2026, 2, 14))                       # 交機後 14 天
        maint = [m for m in ms if m["type"] == "maintenance"]
        self.assertEqual([m["months"] for m in maint], [3, 6, 12, 18, 24, 30, 36, 42, 48, 54, 60])
        self.assertEqual(maint[0]["due"], date(2026, 4, 30))                    # 1/31＋3 個月＝4/30（月底）
        self.assertEqual(maint[0]["label"], "第一次保養")
        self.assertEqual(ms[-1]["due"], date(2028, 1, 1))                       # 保固到期前 30 天
        self.assertEqual(len([m for m in cr.milestones(buy()) if m["type"] == "other"]), 0)

    def test_add_months_leap(self):
        self.assertEqual(cr.add_months(date(2027, 11, 30), 3), date(2028, 2, 29))


class DueItems(unittest.TestCase):
    TODAY = date(2026, 5, 10)

    def test_window(self):
        items = cr.due_items([buy()], [], self.TODAY)
        self.assertEqual([(i["type"], i["due"]) for i in items], [("maintenance", date(2026, 4, 30))])
        self.assertTrue(items[0]["overdue"])                                     # 過期 10 天：還在 14 天內
        self.assertEqual(cr.due_items([buy()], [], self.TODAY, back=5), [])

    def test_already_scheduled(self):
        ex = [{"customer_id": 10, "reminder_type": "maintenance", "scheduled_date": date(2026, 5, 20), "status": "pending"}]
        self.assertEqual(cr.due_items([buy()], ex, self.TODAY), [])
        ex[0]["status"] = "cancelled"                                            # 取消的不算
        self.assertEqual(len(cr.due_items([buy()], ex, self.TODAY)), 1)
        ex = [{"customer_id": 99, "reminder_type": "maintenance", "scheduled_date": date(2026, 5, 1), "status": "pending"}]
        self.assertEqual(len(cr.due_items([buy()], ex, self.TODAY)), 1)          # 別的客人

    def test_two_ears_once(self):
        items = cr.due_items([buy(1), buy(2)], [], self.TODAY)
        self.assertEqual(len(items), 1)


class Message(unittest.TestCase):
    def test_salutation(self):
        self.assertEqual(cr.salutation("王大明", "M"), "王先生")
        self.assertEqual(cr.salutation("陳美", "F"), "陳小姐")
        self.assertEqual(cr.salutation("林一", None), "林一")
        self.assertEqual(cr.salutation("", None), "您")

    def test_text(self):
        it = cr.due_items([buy()], [], date(2026, 5, 10))[0]
        t = cr.message(it, "王先生", "Vivia 等級4", "韻譯助聽器 睿聲中正門市", "02-2341-5968", "https://example.com/care")
        self.assertIn("王先生您好", t)
        self.assertIn("約 3 個月", t)
        self.assertIn("https://example.com/care", t)
        self.assertNotIn("元", t)                                                 # 不提價格
        w = {"type": "other", "label": "保固即將到期", "months": 0, "warranty_end": date(2027, 3, 1)}
        self.assertIn("116/03/01", cr.message(w, "您", "", "店", "02", ""))


if __name__ == "__main__":
    unittest.main()
