"""粉專貼文草稿的純函式（不連資料庫、不打本機模型、不打 Graph API）。

執行：./venv/bin/python -m unittest tests.test_fb_drafts
"""
import random
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

from api import fb_drafts as fd

SITE = "https://y1ear.com"

RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>測試</title>
<item><title>寒流來襲 長輩&amp;外出注意保暖</title><link>https://news.example/a1</link>
  <description><![CDATA[<p>氣象署表示<b>低溫</b>下探 10 度。</p>]]></description>
  <pubDate>Sun, 08 Nov 2026 08:28:22 +0800</pubDate></item>
<item><title>沒有日期的新聞</title><link>https://news.example/a2</link><description>耳鼻喉科提醒</description></item>
<item><title>不是 https 的連結</title><link>http://news.example/a3</link></item>
<item><title></title><link>https://news.example/a4</link></item>
</channel></rss>""".encode()


def news(title, desc="", link=None, when=None):
    return {"source": "測試", "title": title, "desc": desc, "link": link or f"https://n.example/{title}",
            "when": when}


class Rss(unittest.TestCase):
    def test_parse(self):
        items = fd.parse_rss(RSS, "測試")
        self.assertEqual([i["link"] for i in items], ["https://news.example/a1", "https://news.example/a2"])
        a = items[0]
        self.assertEqual(a["title"], "寒流來襲 長輩&外出注意保暖")
        self.assertEqual(a["desc"], "氣象署表示 低溫 下探 10 度。")
        self.assertEqual(a["when"], datetime(2026, 11, 8, 8, 28, 22))      # 主機在台灣時區
        self.assertIsNone(items[1]["when"])

    def test_score(self):
        self.assertEqual(fd.news_score(news("今彩539開獎", "長輩中獎")), 0)            # 擋掉的題材
        self.assertEqual(fd.news_score(news("政院不副署老人福利法修法")), 0)
        self.assertGreater(fd.news_score(news("聽力保健講座")), fd.news_score(news("講座", "聽力保健")))
        self.assertEqual(fd.news_score(news("颱風動態")), 0)

    def test_pick(self):
        now = datetime(2026, 11, 8, 7)
        items = [news("降溫", when=now - timedelta(hours=2)),                     # 分數不夠
                 news("長輩聽力檢查", when=now - timedelta(days=1)),
                 news("耳鳴別輕忽", when=now - timedelta(days=5)),                 # 太舊
                 news("助聽器補助", when=None),                                    # 沒日期也可以
                 news("聽損與失智", when=now)]
        used = {fd.news_key("https://n.example/聽損與失智")}
        got = [i["title"] for i in fd.pick_news(items, used, now)]
        self.assertEqual(got, ["長輩聽力檢查", "助聽器補助"])

    def test_control_char(self):
        """11-09 自由時報的 RSS 夾了 \\x08：XML 不允許，整份讀不進來 → 先拿掉。"""
        bad = RSS.replace("下探 10 度".encode(), "下探 10 度\x08".encode())
        self.assertEqual(len(fd.parse_rss(bad, "測試")), 2)

    def test_atom(self):
        atom = """<feed xmlns="http://www.w3.org/2005/Atom"><title>公視</title>
<entry><title><![CDATA[長輩聽力篩檢開跑]]></title><link rel="alternate" href="https://news.example/b1" />
  <id>https://news.example/b1</id><summary type="html"><![CDATA[<p>社區據點</p>]]></summary>
  <updated>2026-11-08T19:31:05+08:00</updated></entry>
<entry><title>沒有連結</title><link rel="self" href="https://news.example/feed" /></entry>
</feed>""".encode()
        items = fd.parse_rss(atom, "公視")
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["title"], items[0]["link"], items[0]["desc"]),
                         ("長輩聽力篩檢開跑", "https://news.example/b1", "社區據點"))
        self.assertEqual(items[0]["when"], datetime(2026, 11, 8, 19, 31, 5))

    def test_rotate_source(self):
        """John 11-09「不要每天都自由時報的」：最近用過的媒體往後排；相關得多的還是會選到。"""
        now = datetime(2026, 11, 9, 6)
        a = {**news("長輩聽力檢查"), "source": "自由時報"}
        b = {**news("長輩健檢", "長者"), "source": "公視"}
        self.assertEqual(fd.pick_news([a, b], set(), now)[0]["source"], "自由時報")       # 分數高的
        got = fd.pick_news([a, b], set(), now, recent={"自由時報": 1})
        self.assertEqual([i["source"] for i in got], ["公視", "自由時報"])                # 用過的排後面、還在名單
        c = {**news("助聽器補助 聽力 耳鳴"), "source": "自由時報"}
        self.assertEqual(fd.pick_news([c, b], set(), now, recent={"自由時報": 1})[0]["source"], "自由時報")


class Events(unittest.TestCase):
    def test_dates(self):
        ev = dict((k, d) for d, k in fd.events(2027))
        self.assertEqual(ev["mother"], date(2027, 5, 9))
        self.assertEqual(ev["cny"], date(2027, 2, 5))          # 除夕
        self.assertEqual(ev["double9"], date(2027, 10, 8))
        self.assertNotIn("cny", dict((k, d) for d, k in fd.events(2040)))   # 表外的年份不出農曆節日

    def test_window(self):
        self.assertIsNone(fd.event_spec(date(2027, 2, 27), set()))   # 離 3/3 還有 4 天
        s = fd.event_spec(date(2027, 3, 1), set())
        self.assertEqual((s["category"], s["topic_key"], s["topic"]), ("season", "ev:hearingday:2027", "世界聽力日（3/3）"))
        self.assertIsNone(fd.event_spec(date(2027, 3, 1), {"ev:hearingday:2027"}))
        s = fd.event_spec(date(2026, 12, 30), set())           # 跨年
        self.assertEqual(s["topic_key"], "ev:newyear:2027")


class Plan(unittest.TestCase):
    WEB = [{"id": 40, "title": "第一次戴助聽器", "summary": "適應期", "url": f"{SITE}/column/first"}]

    def test_news_bank_web(self):
        day = date(2026, 11, 8)
        out = fd.plan(day, 3, set(), [news("長輩聽力")], self.WEB, SITE)
        self.assertEqual([s["category"] for s in out][0::2], ["news", "web"])
        self.assertIn(out[1]["category"], fd.ROTATION)
        self.assertEqual(out[2]["link"], f"{SITE}/column/first")
        self.assertEqual(out[2]["web_post_id"], 40)
        self.assertEqual(out[0]["link"], "https://n.example/長輩聽力")

    def test_event_first(self):
        out = fd.plan(date(2027, 3, 2), 3, set(), [news("長輩聽力")], self.WEB, SITE)
        self.assertEqual([s["category"] for s in out][:2], ["season", "news"])
        self.assertEqual(len(out), 3)

    def test_no_news_no_web(self):
        day = date(2026, 11, 8)
        out = fd.plan(day, 3, {"web:40"}, [], self.WEB, SITE)
        cats = [s["category"] for s in out]
        self.assertEqual(len(out), 3)
        self.assertEqual(len(set(cats)), 3)                     # 同一天類別不重複
        self.assertIn("tool", cats)                            # 官網文章用過了：改介紹官網工具
        tool = next(s for s in out if s["category"] == "tool")
        self.assertTrue(tool["link"].startswith(SITE + "/"))

    def test_avoid_used_and_season(self):
        rng = random.Random(1)
        cares = {t.key for t in fd.TOPICS if t.cat == "care"}
        for _ in range(30):
            s = fd.topic_spec("care", date(2026, 7, 1), cares - {"care-dry"}, rng)
            self.assertEqual(s["topic_key"], "care-dry")        # 只剩它沒用過
        july = {fd.topic_spec("care", date(2026, 7, 1), set(), random.Random(i))["topic_key"] for i in range(200)}
        self.assertNotIn("care-cold", july)                     # 冬天的題目夏天不出
        self.assertIn("care-sweat", july)

    def test_titles(self):
        self.assertEqual(fd.topic_title("問大家：最喜歡的聲音？"), "最喜歡的聲音？")
        self.assertEqual(fd.topic_title("官網的「聽損模擬」：讓家人聽聽看"), "官網的「聽損模擬」")

    def test_topic_keys_unique(self):
        keys = [t.key for t in fd.TOPICS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(all(t.cat in fd.CATEGORIES for t in fd.TOPICS))
        self.assertTrue(all(c in fd.CATEGORIES for c in fd.ROTATION))


class Clean(unittest.TestCase):
    def test_clean(self):
        raw = ("<think>想一下</think>\n## 冬天保養\n**天冷**了！\n- 每天除濕\n* 帶備用電池\n"
               "詳見 https://fake.example/x\n\n\n#助聽器 #冬天")
        t = fd.clean_post(raw)
        self.assertNotIn("think", t)
        self.assertNotIn("**", t)
        self.assertNotIn("http", t)
        self.assertTrue(t.startswith("冬天保養\n天冷了！\n・每天除濕\n・帶備用電池"))
        self.assertTrue(t.endswith(f"{fd.STORE_TAG} #助聽器 #冬天"))

    def test_tags_split_and_store_tag(self):
        self.assertEqual(fd.clean_post("歡迎留言～ #日常"), f"歡迎留言～\n\n{fd.STORE_TAG} #日常")
        self.assertEqual(fd.clean_post("沒有標籤"), f"沒有標籤\n\n{fd.STORE_TAG}")

    def test_footer(self):
        t = fd.add_footer(f"本文\n\n{fd.STORE_TAG} #保養", SITE)
        body, foot, tags = t.split("\n\n")
        self.assertEqual(body, "本文")
        self.assertTrue(foot.startswith(fd.FOOTER_HEAD))
        self.assertIn(f"{SITE}/practice/", foot)
        self.assertIn(f"{SITE}/hearing-test", foot)
        self.assertEqual(tags, f"{fd.STORE_TAG} #保養")
        self.assertEqual(fd.add_footer(t, SITE), t)            # 不重複加
        self.assertEqual(fd.add_footer("本文", ""), "本文")


class Check(unittest.TestCase):
    def test_check(self):
        e, w = fd.check("每天除濕。")
        self.assertEqual((e, w), ([], []))
        e, _ = fd.check("現在特價 1,000 元")
        self.assertTrue(e)
        e, _ = fd.check("春節營業時間：【營業時間請店員填寫】")
        self.assertTrue(any("【】" in x for x in e))
        _, w = fd.check("免費聽力檢查，ReSound 新品")
        self.assertTrue(any("免費" in x for x in w))
        self.assertTrue(any("resound" in x for x in w))
        _, w = fd.check("耳機戴好")
        self.assertTrue(any("耳機" in x for x in w))


class Write(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(fd, "seo_meta", side_effect=lambda text, cat, timeout=0: fd.clean_seo({}, cat, text))
        p.start()
        self.addCleanup(p.stop)

    def test_warning_retry(self):
        answers = iter(["耳機要戴好。", "助聽器要戴好。"])
        with mock.patch.object(fd, "_chat", side_effect=lambda msgs, timeout: next(answers)) as chat:
            res = fd.write(fd.own_spec("配戴"), {}, SITE)
        self.assertEqual(chat.call_count, 2)
        self.assertTrue(res["message"].startswith("助聽器要戴好。"))
        self.assertIn("耳機", chat.call_args[0][0][-1]["content"])        # 告訴模型要改哪裡
        self.assertTrue(res["web_sync"])                                  # 自己出題預設同步聽力專欄

    def test_keep_best_when_still_warned(self):
        answers = iter(["耳機、免費。", "耳機。", "免費、耳機、ReSound。"])
        with mock.patch.object(fd, "_chat", side_effect=lambda msgs, timeout: next(answers)):
            res = fd.write(fd.own_spec("x"), {}, SITE)
        self.assertTrue(res["message"].startswith("耳機。"))               # 提醒最少的那版

    def test_price_retry_and_footer(self):
        spec = fd.own_spec("冬天保養")
        answers = iter(["助聽器現在特價！", "冬天記得除濕。\n\n#保養"])
        with mock.patch.object(fd, "_chat", side_effect=lambda msgs, timeout: next(answers)) as chat:
            res = fd.write(spec, {"name": "測試門市"}, SITE)
        self.assertEqual(chat.call_count, 2)
        self.assertTrue(res["message"].startswith("冬天記得除濕。"))
        self.assertIn(fd.FOOTER_HEAD, res["message"])
        msgs = chat.call_args[0][0]
        self.assertNotIn("images", str(msgs))                  # 573 那張卡只能送文字
        self.assertIn("測試門市", msgs[0]["content"])

    def test_empty(self):
        with mock.patch.object(fd, "_chat", return_value="  "):
            with self.assertRaises(RuntimeError):
                fd.write(fd.own_spec("x"), {}, SITE)

    def test_prompt(self):
        sp = fd.news_spec(news("長輩聽力", "摘要", when=None))
        self.assertIn("根據測試報導", fd.user_prompt(sp))
        self.assertIn("新聞摘要：摘要", fd.user_prompt(sp))
        sysp = fd.system_prompt({"hours": [{"days": "週一至週六", "time": "09:00–18:00"}], "phone": "02-1"})
        self.assertIn("週一至週六 09:00–18:00", sysp)
        self.assertIn("【日常保養】", sysp)
        self.assertNotIn("【門市】", sysp)                       # 地址電話另外給，不重複
        self.assertIn("身體不適要就醫", sysp)                    # 11-09：AI 曾把「打完疫苗不舒服」導到門市
        self.assertIn("不可以請讀者為了身體不適來電或到門市", sysp)


class Column(unittest.TestCase):
    MSG = (f"冬天到了。\n・每天除濕\n・多帶電池\n\n有問題歡迎來電。\n\n{fd.footer(SITE)}\n\n"
           f"{fd.STORE_TAG} #保養")

    def test_strip_and_body(self):
        self.assertEqual(fd.strip_extras(self.MSG), "冬天到了。\n・每天除濕\n・多帶電池\n\n有問題歡迎來電。")
        self.assertEqual(fd.column_body(self.MSG),
                         "冬天到了。\n\n- 每天除濕\n- 多帶電池\n\n有問題歡迎來電。")
        self.assertTrue(fd.column_body("新聞。", "中央社", "https://n.example/1")
                        .endswith("資料來源：[中央社](https://n.example/1)"))

    def test_clean_seo(self):
        d = {"title": "「最好的助聽器保養」", "summary": "冬天保養重點", "category": "不存在",
             "keywords": ["助聽器保養", "#冬天", "最便宜助聽器", "ReSound 保養", "免費檢查", "助聽器保養", "x" * 30],
             "slug": "Hearing Aid_Winter!!"}
        s = fd.clean_seo(d, "care", self.MSG)
        self.assertEqual(s["title"], "冬天到了")                        # 有最高級用語：改用本文第一句
        self.assertEqual(s["summary"], "冬天保養重點")
        self.assertEqual(s["category"], "助聽器知識")                    # 保養→助聽器知識
        self.assertEqual(s["keywords"], ["助聽器保養", "冬天", *fd.LOCAL_KEYWORDS])
        self.assertEqual(s["slug"], "hearing-aid-winter")
        self.assertEqual(fd.clean_seo({}, "family", self.MSG)["category"], "生活樂活")
        self.assertEqual(fd.clean_seo({"slug": "--"}, "care", self.MSG)["slug"], "")

    def test_clean_seo_input(self):
        from api import web_admin
        ok = fd.clean_seo_input({"title": "標題", "summary": "", "category": "聽力保健", "keywords": "耳鳴、台北助聽器"})
        self.assertEqual(ok["keywords"], ["耳鳴", "台北助聽器"])
        for bad in ({"title": "x" * 41, "category": "聽力保健"}, {"title": "t", "category": "亂填"},
                    {"title": "t", "category": "聽力保健", "keywords": "特價"},
                    {"title": "t", "category": "聽力保健", "slug": "中文"}):
            with self.assertRaises(web_admin.Invalid):
                fd.clean_seo_input(bad)

    def test_column_errors(self):
        r = {"message": self.MSG, "seo": {"title": "", "category": "聽力保健"}}
        self.assertTrue(fd.column_errors(r))
        r["seo"] = {"title": "冬天保養", "category": "聽力保健", "keywords": ["保養"]}
        self.assertEqual(fd.column_errors(r), [])


class Auto(unittest.TestCase):
    D = date(2026, 11, 9)

    def row(self, **kw):
        r = {"day": self.D, "created_by": None, "status": "new", "picked": 1, "web_sync": 0,
             "created_at": datetime(2026, 11, 9, 6, 0), "message": "冬天記得除濕。", "seo": None}
        r.update(kw)
        return r

    def test_deadline(self):
        with mock.patch.dict(fd.config.SOCIAL, {"auto_publish": "10:30"}):
            self.assertEqual(fd.auto_deadline(self.D), datetime(2026, 11, 9, 10, 30))
        for off in ("", "25:00", "10點半", None):
            with mock.patch.dict(fd.config.SOCIAL, {"auto_publish": off}):
                self.assertIsNone(fd.auto_deadline(self.D))

    def test_status(self):
        now = datetime(2026, 11, 9, 10, 30)
        with mock.patch.dict(fd.config.SOCIAL, {"auto_publish": "10:30"}):
            self.assertEqual(fd.auto_status(self.row(), now), (True, "10:30 自動發"))
            self.assertFalse(fd.auto_status(self.row(picked=0), now)[0])
            self.assertFalse(fd.auto_status(self.row(created_by="john"), now)[0])          # 自己請 AI 寫的
            self.assertFalse(fd.auto_status(self.row(message="耳機戴好"), now)[0])          # 有提醒
            self.assertFalse(fd.auto_status(self.row(message="特價 100 元"), now)[0])       # 有錯誤
            self.assertFalse(fd.auto_status(self.row(created_at=datetime(2026, 11, 9, 9, 40)), now)[0])   # 太晚寫好
            self.assertFalse(fd.auto_status(self.row(), datetime(2026, 11, 10, 10, 30))[0])  # 隔天不補前一天的
            self.assertFalse(fd.auto_status(self.row(web_sync=1), now)[0])                  # 要同步卻沒有官網標題
        with mock.patch.dict(fd.config.SOCIAL, {"auto_publish": ""}):
            self.assertEqual(fd.auto_status(self.row(), now), (False, ""))
        later = datetime(2026, 11, 9, 11, 0)
        with mock.patch.dict(fd.config.SOCIAL, {"auto_publish": "10:30"}):
            self.assertEqual(fd.auto_status(self.row(), later)[0], False)               # 畫面：過了期限就不再標「會自動發」
            self.assertIn("已過 10:30", fd.auto_status(self.row(), later)[1])
            self.assertEqual(fd.auto_status(self.row(picked=0), later), (False, ""))
            self.assertTrue(fd.auto_status(self.row(), later, run=True)[0])            # 主機停機晚跑：排程照樣會發


if __name__ == "__main__":
    unittest.main()
