"""會員集點：店員加點／扣點（api/member_points.py；不連資料庫，用假游標）。

John 11-19：「門市聽檢讓店員在點數管理中增加，並註明增加原因。…活動兌換時，也讓店員在點數管理中減少，並註明減少原因。」
執行：cd /home/john/y1crm && ./venv/bin/python -m unittest tests.test_member_points
"""
import unittest
from datetime import datetime
from unittest import mock

from api import member_points as mp


class FakeCur:
    """依 SQL 內容回假資料。ledger＝帳本點數合計、fb＝粉專點數、store＝這個月第一次門市聽檢的時間。"""

    def __init__(self, ledger=0, fb=0, store=None, member=True, lock=1, void_row=None):
        self.ledger, self.fb, self.store, self.member, self.lock, self.void_row = ledger, fb, store, member, lock, void_row
        self.sql, self.inserted, self.updated, self._next = [], [], [], None
        self.lastrowid = 77
        self.rowcount = 1

    def execute(self, sql, args=()):
        self.sql.append(sql)
        if "GET_LOCK" in sql:
            self._next = {"ok": self.lock}
        elif "RELEASE_LOCK" in sql:
            self._next = None
        elif "FROM y1member.members" in sql:
            self._next = {"id": 5} if self.member else None
        elif "source = 'hearing_store'" in sql:
            self._next = {"t": self.store}
        elif "SUM(points)" in sql and "member_points" in sql:
            self._next = {"n": self.ledger}
        elif "fb_comment_points" in sql:
            self._next = {"n": self.fb}
        elif sql.startswith("INSERT"):
            self.inserted.append(args)
        elif "status='void'" in sql and sql.startswith("SELECT"):
            self._next = self.void_row
        elif sql.startswith("UPDATE"):
            self.updated.append(args)

    def fetchone(self):
        return self._next

    def fetchall(self):
        return []


def _patch(cur):
    con = mock.MagicMock()
    con.__enter__.return_value = con
    con.cursor.return_value.__enter__.return_value = cur
    return mock.patch.object(mp, "_conn", return_value=con)


class Adjust(unittest.TestCase):
    def test_input_checks(self):
        for kind, pts, reason in [("x", 10, "a"), ("redeem", 0, "a"), ("redeem", "abc", "a"),
                                  ("add", mp.MAX_ADJUST + 1, "a"), ("add", 5, "  "), ("add", 5, "字" * (mp.NOTE_MAX + 1))]:
            with self.assertRaises(ValueError, msg=(kind, pts, reason)):
                mp.adjust(5, kind, pts, reason, "店員")

    def test_store_hearing_adds_positive_with_reason(self):
        cur = FakeCur(ledger=3, fb=1)
        with _patch(cur):
            r = mp.adjust(5, "hearing_store", "10", " 11/19 門市聽力檢查 ", "王店員")
        self.assertEqual(r, {"id": 77, "points": 10, "balance": 14})
        mid, source, ref, pts, note, by = cur.inserted[0]
        self.assertEqual((mid, source, pts, note, by), (5, "hearing_store", 10, "11/19 門市聽力檢查", "王店員"))
        self.assertRegex(ref, r"^s[0-9a-f]{16}$")
        self.assertTrue(any("RELEASE_LOCK" in s for s in cur.sql))

    def test_redeem_is_negative_and_needs_balance(self):
        cur = FakeCur(ledger=15, fb=2)
        with _patch(cur):
            r = mp.adjust(5, "redeem", 17, "兌換電池", "王店員")
        self.assertEqual((r["points"], r["balance"]), (-17, 0))
        self.assertEqual(cur.inserted[0][1], "redeem")

        cur = FakeCur(ledger=15, fb=2)
        with _patch(cur):
            with self.assertRaisesRegex(ValueError, "點數不夠：目前 17 點"):
                mp.adjust(5, "redeem", 18, "兌換電池", "王店員")
        self.assertEqual(cur.inserted, [])
        self.assertTrue(any("RELEASE_LOCK" in s for s in cur.sql))    # 擋下來也要放鎖

    def test_other_sub_uses_adjust_source(self):
        cur = FakeCur(ledger=5)
        with _patch(cur):
            r = mp.adjust(5, "sub", 5, "重複登記", "王店員")
        self.assertEqual((cur.inserted[0][1], r["points"], r["balance"]), ("adjust", -5, 0))

    def test_second_store_hearing_in_month_needs_confirm(self):
        cur = FakeCur(store=datetime(2026, 11, 5, 14, 0))
        with _patch(cur):
            with self.assertRaisesRegex(mp.NeedConfirm, "11/5"):
                mp.adjust(5, "hearing_store", 10, "門市聽檢", "王店員")
        self.assertEqual(cur.inserted, [])
        cur = FakeCur(store=datetime(2026, 11, 5, 14, 0))
        with _patch(cur):
            mp.adjust(5, "hearing_store", 10, "門市聽檢（複檢）", "王店員", confirm=True)
        self.assertEqual(len(cur.inserted), 1)

    def test_unknown_member_and_busy(self):
        with _patch(FakeCur(member=False)):
            with self.assertRaises(LookupError):
                mp.adjust(5, "add", 1, "a", "王店員")
        with _patch(FakeCur(lock=0)):
            with self.assertRaises(RuntimeError):
                mp.adjust(5, "add", 1, "a", "王店員")


class Restore(unittest.TestCase):
    def test_restore_deduction_needs_balance(self):
        cur = FakeCur(ledger=3, void_row={"member_id": 5, "points": -10})
        with _patch(cur):
            with self.assertRaisesRegex(ValueError, "點數不夠"):
                mp.restore(9, "經理")
        self.assertEqual(cur.updated, [])

    def test_restore_ok(self):
        cur = FakeCur(ledger=30, void_row={"member_id": 5, "points": -10})
        with _patch(cur):
            self.assertEqual(mp.restore(9, "經理"), "counted")
        self.assertEqual(cur.updated, [(9,)])


class Totals(unittest.TestCase):
    def test_totals(self):
        by = {"practice": 3, "hearing_online": 10, "hearing_store": 10, "fb": 2, "redeem": -20, "adjust": 1}
        self.assertEqual(mp.totals(by), {"total": 6, "other": -19})


if __name__ == "__main__":
    unittest.main()
