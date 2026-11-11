"""官網管理：欄位檢查、網址代稱、圖片處理（純函式，不連資料庫）。

執行：./venv/bin/python -m unittest tests.test_web_admin
"""
import io
import tempfile
import unittest
from unittest import mock
from datetime import datetime
from pathlib import Path

from PIL import Image

from api import web_admin as wa

NOW = datetime(2026, 10, 21, 15, 30, 45)


def img_bytes(fmt="PNG", size=(40, 30), mode="RGB", exif_rotate=False):
    im = Image.new(mode, size, (200, 30, 30) if mode == "RGB" else (200, 30, 30, 128))
    buf = io.BytesIO()
    if exif_rotate:
        ex = Image.Exif()
        ex[0x0112] = 6          # 手機直拍：要轉 90 度
        im.save(buf, fmt, exif=ex)
    else:
        im.save(buf, fmt)
    return buf.getvalue()


class CleanPost(unittest.TestCase):
    def test_article(self):
        c = wa.clean_post("article", {"title": " 標題 ", "category": "聽力保健", "status": "published"}, NOW)
        self.assertEqual(c["title"], "標題")
        self.assertEqual(c["slug"], "a261021153045")          # 沒填代稱＝自動
        self.assertEqual(c["publish_at"], NOW)                 # 發佈沒填日期＝現在
        self.assertEqual(c["category"], "聽力保健")

    def test_article_needs_category(self):
        with self.assertRaises(wa.Invalid):
            wa.clean_post("article", {"title": "x", "category": "亂填"}, NOW)

    def test_title_required_and_length(self):
        with self.assertRaises(wa.Invalid):
            wa.clean_post("about", {"title": "  "}, NOW)
        with self.assertRaises(wa.Invalid):
            wa.clean_post("about", {"title": "長" * 121}, NOW)

    def test_slug_rules(self):
        self.assertEqual(wa.clean_post("about", {"title": "x", "slug": "Brand-Story"}, NOW)["slug"], "brand-story")
        for bad in ("中文", "a b", "../x", "-x"):
            with self.assertRaises(wa.Invalid):
                wa.clean_post("about", {"title": "x", "slug": bad}, NOW)

    def test_publish_date(self):
        c = wa.clean_post("article", {"title": "x", "category": "聽力保健", "publish_date": "2026-11-01"}, NOW)
        self.assertEqual(c["publish_at"], datetime(2026, 11, 1, 9, 0))
        with self.assertRaises(wa.Invalid):
            wa.clean_post("article", {"title": "x", "category": "聽力保健", "publish_date": "2026-13-01"}, NOW)
        self.assertIsNone(wa.clean_post("about", {"title": "x", "status": "published"}, NOW)["publish_at"])

    def test_share_needs_consent_to_publish(self):
        base = {"title": "x", "person": "王先生"}
        self.assertEqual(wa.clean_post("share", {**base, "status": "draft"}, NOW)["status"], "draft")
        with self.assertRaises(wa.Invalid):
            wa.clean_post("share", {**base, "status": "published"}, NOW)
        c = wa.clean_post("share", {**base, "status": "published", "consent": True}, NOW)
        self.assertTrue(c["extra"]["consent"])
        with self.assertRaises(wa.Invalid):                    # 一定要有分享人稱呼
            wa.clean_post("share", {"title": "x"}, NOW)

    def test_care(self):
        c = wa.clean_post("care", {"title": "x", "types": ["rechargeable", "亂填", "battery"],
                                   "video": "https://example.com/v"}, NOW)
        self.assertEqual(c["extra"]["types"], ["battery", "rechargeable"])   # 只留認得的，順序固定
        self.assertIsNone(c["publish_at"])                                    # 照排序，不看發佈日
        self.assertEqual(wa.clean_post("care", {"title": "x"}, NOW)["extra"], {"types": [], "video": "", "keywords": []})
        for bad in ("javascript:alert(1)", "www.example.com", "https://a b"):
            with self.assertRaises(wa.Invalid):
                wa.clean_post("care", {"title": "x", "video": bad}, NOW)
        with self.assertRaises(wa.Invalid):
            wa.clean_post("care", {"title": "x", "types": "battery"}, NOW)

    def test_subsidy(self):
        c = wa.clean_post("subsidy", {"title": "x", "when": ["disability", "taipei"], "agency": "區公所",
                                      "link": "https://www.gov.taipei/"}, NOW)
        self.assertEqual(c["extra"], {"when": ["taipei", "disability"], "agency": "區公所",
                                      "link": "https://www.gov.taipei/"})
        with self.assertRaises(wa.Invalid):
            wa.clean_post("subsidy", {"title": "x", "link": "ftp://x"}, NOW)

    def test_bad_kind_and_status(self):
        with self.assertRaises(wa.Invalid):
            wa.clean_post("product", {"title": "x"}, NOW)      # 產品系列不在官網管理改
        with self.assertRaises(wa.Invalid):
            wa.clean_post("about", {"title": "x", "status": "hidden"}, NOW)


class ShareQuote(unittest.TestCase):
    """好評分享的評論轉貼：拆解、遮名、節錄、用語檢查（內容都是編的測試資料）。"""
    PASTE = ("測試請忽略\n在地嚮導 · 52 則評論 · 120 張相片\n3 週前\n新\n"
             "店員很有耐心，解說很清楚。\n價格實在，最專業！\n讚\n分享\n業主回應\n2 週前\n謝謝您！")
    TODAY = NOW.date()

    def quote(self, **kw):
        base = {"format": "quote", "platform": "google", "rating": 5, "author": "測試請忽略",
                "original": "老闆很細心。價格實在。服務很好。", "body_md": "老闆很細心。……服務很好。"}
        return wa.clean_post("share", {**base, **kw}, NOW)

    def test_mask_name(self):
        self.assertEqual(wa.mask_name("王小明"), "王**")           # 不到 4 個字留第一個字
        self.assertEqual(wa.mask_name("歐陽小明"), "歐***")
        self.assertEqual(wa.mask_name(" Mary Chen "), "Mary C***")
        self.assertEqual(wa.mask_name("小明"), "小*")
        self.assertEqual(wa.mask_name("X"), "X")
        self.assertEqual(wa.mask_name(""), "")

    def test_excerpt(self):
        o = "老闆很細心。價格實在。服務很好。"
        for ok in (o, "老闆很細心。……服務很好。", "……服務很好。", "老闆很細心。……", "老闆很細心。...服務很好。",
                   "老闆 很細心。\n價格實在。服務很好。"):          # 空白、換行不算改字
            self.assertIsNone(wa.excerpt_problem(o, ok), ok)
        for bad in ("老闆很細心。服務很好。",                          # 刪了沒標「……」
                    "服務很好。", "老闆很細心。",                       # 開頭／結尾刪了沒標
                    "老闆很細心！", "服務很好。……老闆很細心。", "", "……"):  # 改字、調換順序、空的
            self.assertIsNotNone(wa.excerpt_problem(o, bad), bad)
        self.assertIsNone(wa.excerpt_problem("謝謝。服務好。謝謝。", "……謝謝。"))   # 結尾那一句

    def test_suggest_excerpt(self):
        o = "老闆很細心。價格實在。服務很好。\n保證聽得清楚！\n下次再來。"
        s = wa.suggest_excerpt(o)
        self.assertEqual(s, "老闆很細心。……服務很好。……\n下次再來。")
        self.assertIsNone(wa.excerpt_problem(o, s))
        self.assertEqual(wa.suggest_excerpt("服務很好。"), "服務很好。")
        self.assertEqual(wa.suggest_excerpt("價格實在。"), "……")

    def test_parse_google_paste(self):
        d = wa.parse_review(self.PASTE, self.TODAY)
        self.assertEqual(d["author"], "測試請忽略")
        self.assertEqual(d["posted"], "2026-09-30")               # 3 週前
        self.assertEqual(d["original"], "店員很有耐心，解說很清楚。\n價格實在，最專業！")
        self.assertEqual(d["owner_reply"], "謝謝您！")
        self.assertIsNone(d["rating"])                            # 星星是圖片，複製不到
        self.assertEqual(d["warnings"], [])

    def test_parse_variants(self):
        d = wa.parse_review("Test Ignore\n1 review\na month ago\n★★★★☆\nVery patient.\nLike\nShare", self.TODAY)
        self.assertEqual((d["author"], d["posted"], d["rating"], d["original"]),
                         ("Test Ignore", "2026-09-21", 4, "Very patient."))
        d = wa.parse_review("只有文字。\n第二行。", self.TODAY)
        self.assertEqual((d["author"], d["original"]), ("", "只有文字。\n第二行。"))
        self.assertTrue(d["warnings"])                            # 沒有時間要提醒
        d = wa.parse_review("測試甲\n3 則評論\n2 個月前\n很好。\n更多\n讚\n測試乙\n10 則評論\n1 年前\n也不錯。", self.TODAY)
        self.assertEqual((d["posted"], d["original"]), ("2026-08-21", "很好。"))
        self.assertEqual(len(d["warnings"]), 2)                   # 沒展開、貼了兩則
        self.assertEqual(wa.parse_review("測試\n2026年10月3日\n好。", self.TODAY)["posted"], "2026-10-03")
        self.assertEqual(wa.parse_review("測試\n去年\n好。", self.TODAY)["posted"], "2025-10-21")

    def test_review_draft(self):
        d = wa.review_draft(self.PASTE, today=self.TODAY)
        self.assertEqual(d["person"], "測試***")
        self.assertEqual([f["text"] for f in d["flags"]], ["價格實在，最專業！"])
        self.assertEqual(d["suggested"], "店員很有耐心，解說很清楚。……")
        d = wa.review_draft(original="服務很好。")
        self.assertEqual((d["flags"], d["suggested"]), ([], "服務很好。"))
        with self.assertRaises(wa.Invalid):
            wa.review_draft("長" * (wa.PASTE_MAX + 1))

    def test_clean_quote(self):
        c = self.quote(url="https://maps.app.goo.gl/abc", posted="2026-10-01")
        x = c["extra"]
        self.assertEqual((x["format"], x["person"], x["rating"], x["consent"]), ("quote", "測試***", 5, False))
        self.assertEqual(c["title"], "Google 地圖｜測試***｜2026-10")     # 自動產生，只給後台看
        self.assertEqual(c["body_md"], "老闆很細心。……服務很好。")
        self.assertRegex(x["hash"], r"^[0-9a-f]{16}$")
        self.assertEqual(self.quote(person="王小姐")["extra"]["person"], "王小姐")
        self.assertEqual(self.quote(body_md="")["body_md"], "老闆很細心。價格實在。服務很好。")   # 空白＝照登
        # 同一則評論（只差空白）雜湊一樣，用來擋重複轉貼
        self.assertEqual(self.quote(original="老闆很細心。\n價格實在。 服務很好。")["extra"]["hash"], x["hash"])

    def test_clean_quote_rules(self):
        for bad in ({"platform": "ptt"}, {"rating": 0}, {"rating": 6}, {"original": ""},
                    {"author": "", "person": ""}, {"url": "https://evil.example.com/maps"},
                    {"url": "maps.app.goo.gl/x"}, {"posted": "2026-12-01"}, {"posted": "昨天"},
                    {"body_md": "老闆超細心。"}):
            with self.assertRaises(wa.Invalid, msg=bad):
                self.quote(**bad)
        # 門市心得卡：沒有星等、沒有連結
        c = self.quote(platform="store", rating=None, url="https://x.y", author="", person="王小姐")
        self.assertEqual((c["extra"]["rating"], c["extra"]["url"]), (None, ""))
        self.assertEqual(self.quote(platform="other", url="https://www.ptt.cc/x")["extra"]["url"], "https://www.ptt.cc/x")

    def test_quote_publish_checks(self):
        with self.assertRaises(wa.Invalid):                       # 沒同意不能發佈
            self.quote(status="published")
        with self.assertRaises(wa.Invalid):                       # 提到價格不能發佈（草稿可以存）
            self.quote(status="published", consent=True, body_md="")
        self.assertEqual(self.quote(body_md="")["status"], "draft")
        c = self.quote(status="published", consent=True, original="保證聽得清楚。服務很好。", body_md="")
        self.assertEqual(c["warnings"], ["「保證」可能被當成療效或誇大宣傳"])
        self.assertFalse(self.quote(status="published", consent=True).get("warnings"))

    def test_share_links(self):
        self.assertEqual(wa.clean_share_links({"google": " https://maps.app.goo.gl/x ", "store": "https://x.y",
                                               "other": "https://x.y", "facebook": ""}),
                         {"google": "https://maps.app.goo.gl/x"})
        for bad in ({"google": "https://evil.example.com"}, {"facebook": "facebook.com/x"}):
            with self.assertRaises(wa.Invalid):
                wa.clean_share_links(bad)

    def test_map_embed(self):
        src = "https://www.google.com/maps/embed?pb=!1m18!1m12!1m3!1d3614.9!2d121.51!3d25.03!3m3!1m2!1s0x3442a9%3A0x1!2z!5e0!3m2!1szh-TW!2stw"
        self.assertEqual(wa.map_embed_src(src), src)
        code = f'<iframe src="{src}" width="600" style="border:0;" allowfullscreen="" loading="lazy"></iframe>'
        self.assertEqual(wa.map_embed_src(code), src)                     # 整段 HTML 貼上也可以
        self.assertEqual(wa.map_embed_src("  "), "")
        for bad in ("https://www.google.com/maps/place/x", "https://evil.example/maps/embed?pb=!1m1",
                    "https://www.google.com/maps/embed?pb=!1m1\"><script>", '<iframe src="javascript:alert(1)">'):
            with self.assertRaises(wa.Invalid):
                wa.map_embed_src(bad)
        self.assertEqual(wa.clean_share_links({"map": src})["map"], src)
        self.assertNotIn("map", wa.clean_share_links({"map": ""}))


class SiteUrl(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(wa.clean_site_url(" HTTPS://AP888.DuckDNS.org:5177/ "), "https://ap888.duckdns.org:5177")
        self.assertEqual(wa.clean_site_url("https://www.example.com.tw"), "https://www.example.com.tw")
        for bad in ("", "ap888.duckdns.org", "https://a.b/care", "javascript:alert(1)", "https://a.b:5177?x=1",
                    "https://a b.c", "ftp://a.b"):
            with self.assertRaises(wa.Invalid):
                wa.clean_site_url(bad)

    def test_get_with_fallback(self):
        class Cur:
            def __init__(self, row): self.row = row
            def execute(self, *a): pass
            def fetchone(self): return self.row
        self.assertEqual(wa.get_site_url(Cur({"v": '{"base_url": "https://a.b:5177"}'}), "http://x/"), "https://a.b:5177")
        self.assertEqual(wa.get_site_url(Cur(None), "http://192.168.1.88:8006/"), "http://192.168.1.88:8006")
        self.assertEqual(wa.get_site_url(Cur({"v": '{"base_url": "壞掉"}'}), "http://x"), "http://x")


class Images(unittest.TestCase):
    def test_png_to_webp(self):
        out, name = wa.process_image(img_bytes("PNG", (3000, 1500)))
        self.assertRegex(name, r"^[0-9a-f]{16}\.webp$")
        im = Image.open(io.BytesIO(out))
        self.assertEqual(im.format, "WEBP")
        self.assertEqual(max(im.size), 1600)                   # 縮到 1600px

    def test_exif_rotated_and_stripped(self):
        out, _ = wa.process_image(img_bytes("JPEG", (40, 30), exif_rotate=True))
        im = Image.open(io.BytesIO(out))
        self.assertEqual(im.size, (30, 40))                    # 直拍照片轉正
        self.assertFalse(im.getexif())                         # EXIF（含 GPS）不留

    def test_alpha_kept(self):
        out, _ = wa.process_image(img_bytes("PNG", (20, 20), mode="RGBA"))
        self.assertEqual(Image.open(io.BytesIO(out)).mode, "RGBA")

    def test_rejects_non_image(self):
        for data in (b"", b"not an image", b"%PDF-1.4 ..."):
            with self.assertRaises(wa.Invalid):
                wa.process_image(data)

    def test_save_dedup_and_paths(self):
        with tempfile.TemporaryDirectory() as d:
            a = wa.save_image(Path(d), img_bytes())
            b = wa.save_image(Path(d), img_bytes())
            self.assertEqual(a, b)
            self.assertEqual(len(list(Path(d).iterdir())), 1)
            self.assertEqual(wa.image_path(Path(d), a), Path(d) / a)
        for bad in ("../etc/passwd", "x.webp", "0123456789abcdef.png", ""):
            self.assertIsNone(wa.image_path(Path("/tmp"), bad))

    def test_thumb(self):
        for size, want in (((3000, 1500), (960, 480)), ((1200, 1600), (720, 960)), ((2400, 1600), (720, 480)),
                           ((100, 80), (100, 80))):
            out, _ = wa.process_image(img_bytes("PNG", size))
            im = Image.open(io.BytesIO(wa.make_thumb(out)))
            self.assertEqual((im.format, im.size), ("WEBP", want))        # 剛好蓋滿 720×480、小圖不放大

    def test_save_with_thumb_and_remove(self):
        class Cur:
            n = 0
            def execute(self, *a): pass
            def fetchone(self): return {"n": self.n}
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            name = wa.save_image(d, img_bytes("PNG", (800, 600)), thumb=True)
            self.assertTrue(wa.thumb_path(d, name).exists())
            self.assertEqual(wa.thumb_path(d, name), d / "c" / name)
            wa.thumb_path(d, name).unlink()
            self.assertTrue(wa.ensure_thumb(d, name))                   # 舊圖補做卡片圖
            self.assertTrue(wa.thumb_path(d, name).exists())
            self.assertFalse(wa.ensure_thumb(d, "0123456789abcdef.webp"))
            cur = Cur(); cur.n = 1
            wa.remove_if_unused(cur, d, name)                           # 還有人用：不刪
            self.assertTrue((d / name).exists())
            cur.n = 0
            wa.remove_if_unused(cur, d, name)                           # 原圖、卡片圖一起刪
            self.assertFalse((d / name).exists() or wa.thumb_path(d, name).exists())
        self.assertIsNone(wa.thumb_path(Path("/tmp"), "../x.webp"))



class Keywords(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(wa.clean_keywords("助聽器保養, #耳鳴、 台北  助聽器\n耳鳴"), ["助聽器保養", "耳鳴", "台北 助聽器"])
        self.assertEqual(wa.clean_keywords(["a", "", "a"]), ["a"])
        self.assertEqual(wa.clean_keywords(None), [])
        for bad in ("特價", "最好的助聽器", "x" * 21, "、".join(str(i) for i in range(11))):
            with self.assertRaises(wa.Invalid):
                wa.clean_keywords(bad)

    def test_in_post(self):
        a = wa.clean_post("article", {"title": "t", "category": "聽力保健", "keywords": "耳鳴、保養"})
        self.assertEqual(a["extra"]["keywords"], ["耳鳴", "保養"])
        c = wa.clean_post("care", {"title": "t", "keywords": ""})
        self.assertEqual(c["extra"]["keywords"], [])


class BatchReviews(unittest.TestCase):
    TODAY = __import__("datetime").date(2026, 11, 8)
    PASTE = """評論
5.0
151 則評論
排序
王小明
在地嚮導 · 52 則評論 · 120 張相片
3 週前 新
店員很有耐心。
讚
分享
業主回應
2 週前
謝謝您！
陳美麗
3 則評論
2 個月前
環境乾淨。
讚
林大同
1 則評論
1 年前
服務很好"""

    def test_split(self):
        got = [wa.parse_review(c, self.TODAY) for c in wa.split_reviews(self.PASTE, self.TODAY)]
        real = [(d["author"], d["posted"], d["original"], d["owner_reply"]) for d in got if d["posted"]]
        self.assertEqual(real, [("王小明", "2026-10-18", "店員很有耐心。", "謝謝您！"),
                                ("陳美麗", "2026-09-08", "環境乾淨。", ""),
                                ("林大同", "2025-11-08", "服務很好", "")])

    def test_date_with_badge(self):
        self.assertEqual(wa.review_date("3 週前 新", self.TODAY).isoformat(), "2026-10-18")
        self.assertEqual(wa.review_date("2 個月前・在 Google 上", self.TODAY).isoformat(), "2026-09-08")
        self.assertIsNone(wa.review_date("3 週前我來過", self.TODAY))

    def test_batch_marks_duplicates(self):
        class Cur:
            def __init__(self):
                self.q = None
            def execute(self, sql, args=()):
                self.q = args
            def fetchone(self):
                return {"id": 99} if self.q and self.q[0] == __import__("hashlib").sha256("環境乾淨。".encode()).hexdigest()[:16] else None
        items = wa.batch_reviews(Cur(), self.PASTE, self.TODAY)
        self.assertEqual([x["person"] for x in items], ["王**", "陳**", "林**"])
        self.assertEqual([x["duplicate"] for x in items], [None, 99, None])
        self.assertEqual({x["rating"] for x in items}, {5})
        with self.assertRaises(wa.Invalid):
            wa.batch_reviews(Cur(), "x" * (wa.BATCH_MAX + 1), self.TODAY)

    def test_create_unique_slugs(self):
        saved = []
        with mock.patch.object(wa, "save_post", side_effect=lambda cur, clean, actor: saved.append(clean) or len(saved)):
            r = wa.create_share_drafts(None, [{"author": "王小明", "original": "很好"}, {"author": "陳美麗", "original": "很棒"},
                                              {"author": "", "original": ""}], "john", "")
        self.assertEqual(r["created"], [1, 2])
        self.assertEqual(len(r["skipped"]), 1)                                  # 沒有原文的跳過
        self.assertEqual(len({c["slug"] for c in saved}), 2)
        self.assertTrue(all(c["status"] == "draft" and not c["extra"]["consent"] for c in saved))
        self.assertEqual([c["extra"]["person"] for c in saved], ["王**", "陳**"])

    def test_create_keeps_review_id_photos_year(self):
        saved = []
        with mock.patch.object(wa, "save_post", side_effect=lambda cur, clean, actor: saved.append(clean) or len(saved)):
            r = wa.create_share_drafts(None, [
                {"author": "王小明", "original": "很好", "posted": "2025-11-08", "posted_prec": "year",
                 "review_id": "ChZDSUhNMG9nS0VJQ0FnSUR0ZXN0", "photos": 2},
                {"author": "陳美麗", "original": "很棒", "review_id": "壞掉的代號", "photos": "x"}], "john", "")
        self.assertEqual(r["with_photos"], [1])
        x = saved[0]["extra"]
        self.assertEqual((x["review_id"], x["review_photos"], x["posted_prec"]), ("ChZDSUhNMG9nS0VJQ0FnSUR0ZXN0", 2, "year"))
        self.assertFalse({"review_id", "review_photos", "posted_prec"} & set(saved[1]["extra"]))   # 格式不對、沒日期的不存


# 照 Google 地圖 2026-11 評論區的結構做的假資料（人名、內容都是編的）
REVIEW_HTML = """<div class="m6QErb" aria-label="測試門市">
<div class="jftiEf fontBodyMedium " aria-label="王小明" data-review-id="ChdDSUhNMG9nS0VJQ0FnSUN0ZXN0MQ">
 <div data-review-id="ChdDSUhNMG9nS0VJQ0FnSUN0ZXN0MQ">
  <button class="WEBjve" data-review-id="ChdDSUhNMG9nS0VJQ0FnSUN0ZXN0MQ" aria-label="王小明的相片"><img class="NBa7we"></button>
  <button class="al6Kxe"><div class="d4r55 fontTitleMedium">王小明</div><div class="RfnDt ">在地嚮導・52 則評論</div></button>
  <div class="DU9Pgb"><span class="kvMYJc" role="img" aria-label="5 顆星"><span class="hCCjke"></span></span>
   <span class="rsqaWe">1 年前</span></div>
  <div class="MyEned"><span class="wiI7pd">店員很有耐心&amp;細心。<br>下次還會再來</span></div>
  <div class="KtCyie"><button class="Tya61d" style="background-image: url(&quot;https://x.example/a&quot;);"></button>
   <button class="Tya61d" style="background-image: url(&quot;https://x.example/b&quot;);"></button></div>
  <span class="pkWtMe">1</span><span class="Hzvxle">按讚</span>
  <div class="CDe7pd"><span class="fontTitleSmall">業主回應</span><span class="DZSIDd">1 年前</span>
   <div class="wiI7pd">謝謝您的支持！</div></div>
 </div>
</div>
<div class="jftiEf fontBodyMedium " aria-label="陳美麗" data-review-id="ChdDSUhNMG9nS0VJQ0FnSUN0ZXN0Mg">
  <button class="al6Kxe"><div class="d4r55">陳美麗</div><div class="RfnDt ">3 則評論</div></button>
  <span class="kvMYJc" role="img" aria-label="4 顆星"></span><span class="rsqaWe">上次編輯：3 週前</span>
  <div class="MyEned"><span class="wiI7pd">環境乾淨?</span></div>
</div>
<div class="jftiEf" aria-label="林大同" data-review-id="ChdDSUhNMG9nS0VJQ0FnSUN0ZXN0Mw">
  <div class="d4r55">林大同</div><span class="kvMYJc" aria-label="5 顆星"></span><span class="rsqaWe">2 個月前</span>
</div></div>"""


class ReviewHtml(unittest.TestCase):
    TODAY = BatchReviews.TODAY

    def test_parse(self):
        self.assertTrue(wa.is_review_html(REVIEW_HTML))
        self.assertFalse(wa.is_review_html(BatchReviews.PASTE))
        got = wa.reviews_from_html(REVIEW_HTML, self.TODAY)
        self.assertEqual([(d["author"], d["rating"], d["posted"], d["posted_prec"]) for d in got],
                         [("王小明", 5, "2025-11-08", "year"), ("陳美麗", 4, "2026-10-18", ""), ("林大同", 5, "2026-09-08", "")])
        a = got[0]
        self.assertEqual(a["original"], "店員很有耐心&細心。\n下次還會再來")       # 業主回應、按讚數不混進原文
        self.assertEqual(a["owner_reply"], "謝謝您的支持！")
        self.assertEqual((a["photos"], a["review_id"]), (2, "ChdDSUhNMG9nS0VJQ0FnSUN0ZXN0MQ"))
        self.assertTrue(any("「?」" in w for w in got[1]["warnings"]))          # Big5 存檔掉字的提醒
        self.assertEqual(got[2]["original"], "")                               # 只有星星

    def test_batch_uses_html_and_review_id(self):
        class Cur:
            def execute(self, sql, args=()):
                self.q = args
            def fetchone(self):                                                 # 代號相同＝轉貼過（原文掉字也認得）
                return {"id": 120} if self.q[1] == "ChdDSUhNMG9nS0VJQ0FnSUN0ZXN0Mg" else None
        items = wa.batch_reviews(Cur(), REVIEW_HTML, self.TODAY)
        self.assertEqual([(x["person"], x["duplicate"], x["photos"]) for x in items], [("王**", None, 2), ("陳**", 120, 0)])
        self.assertEqual(items[0]["review_id"], "ChdDSUhNMG9nS0VJQ0FnSUN0ZXN0MQ")

    def test_decode(self):
        self.assertEqual(wa.decode_paste("評論".encode("utf-8")), "評論")
        self.assertEqual(wa.decode_paste("\ufeff評論".encode("utf-8")), "評論")
        self.assertEqual(wa.decode_paste("評論".encode("utf-16")), "評論")
        self.assertEqual(wa.decode_paste("睿聲助聽器".encode("cp950")), "睿聲助聽器")    # 記事本存的 ANSI
        self.assertEqual(wa.review_date("上次編輯：1 年前", self.TODAY).isoformat(), "2025-11-08")


if __name__ == "__main__":
    unittest.main()
