"""動態影片的分鏡檢查（不打語音服務、不跑 GPU）。

執行：./venv/bin/python -m unittest tests.test_motion_video
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from api import motion_video as mv

LIB = [{"id": "a", "file": "aaaaaaaaaaaaaaaa.webp", "tags": []},
       {"id": "cr", "file": "bbbbbbbbbbbbbbbb.webp", "tags": [], "credit_required": True}]


def plan(**over):
    lines = [{"kind": "title", "say": "助聽器突然嗶嗶叫？", "photo": "a"},
             {"kind": "feedback", "say": "聲音從耳道漏出來。", "photo": "a"},
             {"kind": "photo", "say": "先輕輕推好。", "photo": "a", "move": "in"},
             {"kind": "end", "say": "歡迎回門市。", "photo": "a"}]
    d = {"title": ["助聽器嗶嗶叫？", "先檢查這兩件事"], "lines": lines, **over}
    return d


class Plan(unittest.TestCase):
    def check(self, d):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "plan.json"
            p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            with mock.patch.object(mv.stock_art, "load", return_value=LIB), \
                    mock.patch.object(mv.stock_art, "available", side_effect=lambda items, media: items):
                return mv.load_plan(p)

    def test_ok(self):
        self.assertEqual(len(self.check(plan())["lines"]), 4)

    def test_bad(self):
        bad = [plan(lines=plan()["lines"][:3]),                                         # 太少段
               plan(lines=plan()["lines"][1:] + plan()["lines"][:1]),                   # 第一段不是片頭
               plan(lines=[*plan()["lines"][:2], {"kind": "dance", "say": "x", "photo": "a"}, plan()["lines"][3]]),
               plan(lines=[*plan()["lines"][:2], {"kind": "photo", "say": "x", "photo": "zz"}, plan()["lines"][3]]),
               plan(lines=[*plan()["lines"][:2], {"kind": "photo", "say": "x", "photo": "cr"}, plan()["lines"][3]]),
               plan(lines=[*plan()["lines"][:2], {"kind": "photo", "say": "x", "photo": "a", "move": "spin"},
                           plan()["lines"][3]]),
               plan(lines=[*plan()["lines"][:2], {"kind": "photo", "say": "保證改善聽力", "photo": "a"},
                           plan()["lines"][3]]),
               plan(lines=[*plan()["lines"][:2], {"kind": "photo", "say": "一副只要 9900 元", "photo": "a"},
                           plan()["lines"][3]])]
        for d in bad:
            with self.assertRaises(ValueError):
                self.check(d)

    def test_new_rules(self):
        ok = self.check(plan(lines=[*plan()["lines"][:2], {"kind": "points", "say": "重點有三個。", "photo": "a",
                                                           "items": ["戴好", "耳塞尺寸", "回門市"]}, plan()["lines"][3]]))
        self.assertEqual(ok["lines"][2]["items"], ["戴好", "耳塞尺寸", "回門市"])
        self.assertEqual(ok["lines"][3]["buttons"], mv.DEFAULT_BUTTONS)                # 片尾按鈕沒給就用預設
        self.assertTrue(ok["lines"][3]["cta"])
        for bad in (plan(title=[]), plan(title=["這一行實在是太長太長太長了喔喔"]),
                    plan(lines=[*plan()["lines"][:2], {"kind": "points", "say": "x", "photo": "a", "items": ["只有一點"]},
                                plan()["lines"][3]]),
                    plan(lines=[*plan()["lines"][:2], {"kind": "photo", "say": "x", "photo": "a",
                                                       "label": "這個標籤寫得實在是太長太長太長了"}, plan()["lines"][3]])):
            with self.assertRaises(ValueError):
                self.check(bad)

    def test_sfx(self):
        self.assertEqual(mv.scene_sfx({"kind": "title"}, 1.2, []), [])                 # 通用片頭沒有嗶嗶聲
        self.assertEqual([k for k, _ in mv.scene_sfx({"kind": "title", "icon": "beep"}, 1.2, [])], ["beep2", "beep2"])
        pts = mv.scene_sfx({"kind": "points", "items": ["a", "b"]}, 0.4, [[0, 1, "x"], [1, 4.0, "y"]])
        self.assertEqual(pts, [["pop", 0.4], ["pop", round(0.4 + 4.0 * 0.88 / 2, 3)]])
        self.assertEqual(len(mv.scene_sfx({"kind": "domes"}, 0.4, [])), 3)
        self.assertEqual(mv.scene_sfx({"kind": "photo"}, 0.4, []), [])

    def test_plan_107_file(self):
        """存在 repo 裡的 #107 分鏡要能通過檢查（照片 id 要在圖庫清單裡）。"""
        p = Path(__file__).resolve().parent.parent / "scripts" / "video" / "plans" / "107.json"
        lib = json.loads((Path(mv.__file__).resolve().parent.parent / "scripts" / "illustrate" / "stock_library.json")
                         .read_text(encoding="utf-8"))
        with mock.patch.object(mv.stock_art, "available", side_effect=lambda items, media: items):
            self.assertEqual(mv.load_plan(p)["lines"][0]["kind"], "title")
        self.assertTrue(all(any(e["id"] == ln["photo"] for e in lib) for ln in json.loads(p.read_text())["lines"]))



POST = {"id": 9, "kind": "article", "slug": "beep", "title": "助聽器出現嗶嗶聲怎麼辦？常見原因與處理方式",
        "summary": "本文說明可能原因。", "body_md": "前言。\n\n- 先檢查是否戴好\n- 耳塞尺寸不對\n- 常發生請回門市",
        "category": "助聽器知識", "status": "published",
        "extra": json.dumps({"keywords": ["助聽器嗶嗶聲", "台北助聽器", "耳塞 尺寸"]}, ensure_ascii=False)}


class Draft(unittest.TestCase):
    def setUp(self):
        for name, fn in (("load", lambda: LIB), ("available", lambda items, media: items)):
            p = mock.patch.object(mv.stock_art, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    def test_header(self):
        self.assertEqual(mv.header_of(POST["title"]), "助聽器出現嗶嗶聲怎麼辦？")
        self.assertEqual(mv.header_of("聽力變差的警訊：家人可以留意的 6 件事"), "聽力變差的警訊")

    def test_ai_plan(self):
        ai = {"title": ["助聽器嗶嗶叫？", "先檢查這兩件事，還有很多很多字"],
              "lines": [{"kind": "title", "say": "助聽器突然 嗶嗶叫？", "topic": "助聽器"},
                        {"kind": "points", "say": "重點有三個。", "label": "重點", "items": ["戴好", "耳塞尺寸，太小或太大都會讓聲音漏出來", "回門市"]},
                        {"kind": "dance", "say": "不認得的段落丟掉"},
                        {"kind": "photo", "say": "先輕輕推好。", "label": "推好", "topic": "配戴"},
                        {"kind": "end", "say": "歡迎回門市。", "cta": ["嗶嗶聲常出現？"]}]}
        with mock.patch.object(mv.fb_drafts, "_chat", return_value=json.dumps(ai, ensure_ascii=False)):
            r = mv.draft_plan(POST)
        self.assertEqual(r["source"], "ai")
        plan = r["plan"]
        self.assertEqual(plan["title"], ["助聽器嗶嗶叫？", "先檢查這兩件事"])           # 太長的在逗號斷
        self.assertEqual([ln["kind"] for ln in plan["lines"]], ["title", "points", "photo", "end"])
        self.assertEqual(plan["lines"][0]["say"], "助聽器突然嗶嗶叫？")
        self.assertEqual(plan["lines"][1]["items"][1], "耳塞尺寸")
        self.assertTrue(all(ln["photo"] == "a" for ln in plan["lines"]))                  # 要標作者的不挑
        self.assertEqual(plan["header"], "助聽器出現嗶嗶聲怎麼辦？")

    def test_fallback(self):
        with mock.patch.object(mv.fb_drafts, "_chat", side_effect=TimeoutError("逾時")):
            r = mv.draft_plan(POST)
        self.assertEqual(r["source"], "list")
        kinds = [ln["kind"] for ln in r["plan"]["lines"]]
        self.assertEqual((kinds[0], kinds[1], kinds[-1]), ("title", "points", "end"))
        self.assertEqual(r["plan"]["lines"][1]["items"], ["先檢查是否戴好", "耳塞尺寸不對", "常發生請回門市"])

    def test_fb_message(self):
        m = mv.fb_message(POST, {"title": ["助聽器嗶嗶叫？", "先檢查這兩件事"]}, "https://y1ear.com")
        self.assertTrue(m.startswith("助聽器嗶嗶叫？先檢查這兩件事 🎬\n\n這支影片說明可能原因。"))
        self.assertIn("👉 完整文章：https://y1ear.com/column/beep", m)
        self.assertIn("#助聽器嗶嗶聲 #耳塞尺寸", m)
        self.assertNotIn("#台北助聽器", m)
        self.assertIn("—— 韻譯助聽器・官網 ——", m)


class Jobs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from api import video_jobs as vj
        self.vj = vj
        for target, attr, val in ((vj, "root", lambda: Path(self.tmp.name)),
                                  (vj.motion_video, "check_plan", lambda plan: plan),
                                  (vj.motion_video, "get_post", lambda pid: {"id": pid, "title": "測試"})):
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)
        vj._running = None

    def wait(self, job_id):
        import time
        for _ in range(100):
            if self.vj.status(job_id)["state"] in ("done", "failed"):
                return self.vj.status(job_id)
            time.sleep(0.02)
        raise AssertionError("工作沒結束")

    def test_flow(self):
        def build(post, plan, work, progress=None):
            progress("旁白 1／1", 0.5)
            for n in ("wide.mp4", "tall.mp4", "wide.jpg", "tall.jpg"):
                (work / n).write_bytes(b"x")
            return {"plan": [], "sec": 12.3}
        with mock.patch.object(self.vj.motion_video, "build", side_effect=build):
            st = self.vj.start(9, {"lines": []}, "john")
            done = self.wait(st["id"])
        self.assertEqual((done["state"], done["sec"], done["post_id"]), ("done", 12.3, 9))
        self.assertEqual(self.vj.latest(9)["id"], st["id"])
        self.assertTrue(self.vj.file_path(st["id"], "tall.mp4").is_file())
        with self.assertRaises(KeyError):
            self.vj.file_path(st["id"], "../plan.json")
        with mock.patch.object(self.vj.motion_video, "install", return_value={"file": "videos/x.mp4"}) as inst:
            self.vj.install(st["id"], "john")
        self.assertEqual(inst.call_args[0][0], 9)
        with self.assertRaises(KeyError):                                     # 放上官網後暫存刪掉
            self.vj.status(st["id"])

    def test_busy_and_failure(self):
        self.vj._running = "9-20261108000000"
        with self.assertRaises(self.vj.Busy):
            self.vj.start(9, {"lines": []}, "john")
        self.vj._running = None
        with mock.patch.object(self.vj.motion_video, "build", side_effect=RuntimeError("顯卡壞了")):
            st = self.vj.start(9, {"lines": []}, "john")
            done = self.wait(st["id"])
        self.assertEqual(done["state"], "failed")
        self.assertIn("顯卡壞了", done["error"])
        self.assertIsNone(self.vj._running)

    def test_cleanup_marks_running(self):
        d = Path(self.tmp.name) / "9-20261108000000"
        d.mkdir()
        (d / "status.json").write_text(json.dumps({"state": "running", "post_id": 9}), encoding="utf-8")
        self.vj.cleanup()
        self.assertEqual(self.vj.status("9-20261108000000")["state"], "failed")
        for bad in ("../x", "9", "abc-20261108000000"):
            with self.assertRaises(KeyError):
                self.vj.status(bad)

if __name__ == "__main__":
    unittest.main()
