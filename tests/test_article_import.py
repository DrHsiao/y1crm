"""聽力專欄「從網址翻譯文章」（api/article_import.py）：網址檢查、內文擷取、分段、翻譯後處理、建草稿。
不連網路、不連資料庫、不跑模型（本機 AI 用假的）。測試資料都是自己寫的假網頁（測試請忽略）。

執行：./venv/bin/python -m unittest tests.test_article_import
"""
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from api import article_import as ai
from api import fb_drafts, web_admin
from tests.test_post_art import Ctx, FakeCur

BASE = "https://www.example.com/hearing/how-hearing-aids-work"

PAGE = """<!doctype html><html lang="en"><head><title>How hearing aids work | Example Hearing</title>
<meta property="og:site_name" content="Example Hearing">
<meta property="og:image" content="/img/hero.jpg"></head><body>
<header class="site-header"><nav><a href="/">Home</a><a href="/shop">Shop</a></nav></header>
<div class="cookie-banner"><p>We use cookies to improve your experience on this website, please accept them all.</p></div>
<div class="hero"><h1>How hearing aids work</h1></div>
<main><article class="post-body overflow-hidden">
  <figure><img src="/img/hero.jpg" alt="A hearing aid" width="1200" height="800"></figure>
  <p>A hearing aid has three basic parts: a <a href="/mic">microphone</a>, an amplifier and a receiver.
     The microphone picks up sound and turns it into a digital signal.</p>
  <img src="/img/logo.png" alt="logo">
  <h2>The <strong>amplifier</strong></h2>
  <p>The amplifier makes the signal stronger, based on the results of your hearing test at the clinic.</p>
  <ul><li>Soft sounds are made louder.<ul><li>Speech first</li></ul></li><li>Loud sounds stay comfortable.</li></ul>
  <figure><picture><img data-src="/img/parts-small.jpg" data-srcset="/img/parts-small.jpg 400w, /img/parts-big.jpg 1600w"></picture>
    <figcaption>The parts of a hearing aid</figcaption></figure>
  <table><tr><th>Type</th><th>Where</th></tr><tr><td>BTE</td><td>Behind the ear</td></tr></table>
  <div class="share-buttons"><p>Share this article with your friends and family on social media today.</p></div>
  <h2>Related articles</h2>
  <p>Ten tips for new hearing aid users that you should not miss reading.</p>
</article></main>
<footer><p>Copyright Example Hearing. All rights reserved. Privacy policy and terms of use.</p></footer>
</body></html>"""


def png(w=800, h=600, color=(90, 120, 150)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


class Url(unittest.TestCase):
    def test_check_url(self):
        self.assertEqual(ai.check_url("  https://www.example.com/a?b=1 "), "https://www.example.com/a?b=1")
        for bad in ("", "ftp://example.com/a", "example.com/a", "https://example.com:8004/a",
                    "https://user:pw@example.com/a", "https://exa mple.com/a"):
            with self.assertRaises(web_admin.Invalid, msg=bad):
                ai.check_url(bad)

    def test_public_host(self):
        def addrs(*ips):
            return mock.patch.object(ai.socket, "getaddrinfo", return_value=[(2, 1, 6, "", (ip, 0)) for ip in ips])
        with addrs("93.184.216.34"):
            self.assertTrue(ai.public_host("example.com"))
        for ips in (("127.0.0.1",), ("192.168.1.26",), ("10.0.0.5",), ("93.184.216.34", "169.254.169.254"), ("::1",)):
            with addrs(*ips):
                self.assertFalse(ai.public_host("x.example"), ips)
        with mock.patch.object(ai.socket, "getaddrinfo", side_effect=OSError):
            self.assertFalse(ai.public_host("nope.example"))

    def test_fetch_refuses_private(self):
        with mock.patch.object(ai, "public_host", return_value=False), \
                mock.patch.object(ai._opener, "open") as op:
            with self.assertRaises(ai.Blocked):
                ai.fetch("http://192.168.1.26:80/", 100, "*/*")
        op.assert_not_called()

    def test_safe_url(self):
        self.assertEqual(ai.safe_url("https://a.com/-/media/Blog 101-Header.jpg?w=1&h=2"),
                         "https://a.com/-/media/Blog%20101-Header.jpg?w=1&h=2")
        self.assertEqual(ai.safe_url("https://a.com/%E8%81%BD/x"), "https://a.com/%E8%81%BD/x")    # 不重複編碼
        self.assertEqual(ai.safe_url("https://a.com/聽力"), "https://a.com/%E8%81%BD%E5%8A%9B")

    def test_decode(self):
        self.assertEqual(ai.decode_html("中文".encode("big5"), "text/html; charset=big5"), "中文")
        self.assertEqual(ai.decode_html('<meta charset="utf-8">中'.encode(), ""), '<meta charset="utf-8">中')


class Extract(unittest.TestCase):
    def test_page(self):
        p = ai.extract(PAGE, BASE)
        self.assertEqual((p["title"], p["site"], p["og_image"]),
                         ("How hearing aids work", "Example Hearing", "https://www.example.com/img/hero.jpg"))
        kinds = [(b["t"], b.get("text") or b.get("src")) for b in p["blocks"]]
        self.assertEqual(kinds[0], ("img", "https://www.example.com/img/hero.jpg"))
        self.assertTrue(kinds[1][1].startswith("A hearing aid has three basic parts: a microphone, an amplifier"))
        self.assertIn(("h2", "The amplifier"), kinds)                       # 標題裡的粗體不留 **
        self.assertIn(("li", "Soft sounds are made louder."), kinds)          # 子清單不混進上一層
        li = [b for b in p["blocks"] if b["t"] == "li"]
        self.assertEqual([(b["text"], b["lvl"]) for b in li],
                         [("Soft sounds are made louder.", 0), ("Speech first", 1), ("Loud sounds stay comfortable.", 0)])
        self.assertIn(("img", "https://www.example.com/img/parts-big.jpg"), kinds)   # srcset 取最大的
        self.assertEqual(next(b for b in p["blocks"] if b.get("src", "").endswith("parts-big.jpg"))["alt"],
                         "The parts of a hearing aid")                     # 沒有 alt：用圖說
        self.assertIn(("p", "The parts of a hearing aid"), kinds)
        self.assertIn({"t": "table", "rows": [["Type", "Where"], ["BTE", "Behind the ear"]]}, p["blocks"])
        text = json.dumps(p["blocks"])
        for junk in ("cookies", "logo.png", "Share this", "Related", "Ten tips", "Copyright", "Shop"):
            self.assertNotIn(junk, text, junk)                              # 選單、橫幅、分享鈕、延伸閱讀、頁尾都不要

    def test_pasted_fragment(self):
        frag = ("<html><body><!--StartFragment--><p>Tinnitus is a sound in your ears that other people cannot hear. "
                "It can be a ringing, buzzing or hissing sound.</p><p>Talk to an audiologist.</p><!--EndFragment-->")
        p = ai.extract(frag, BASE)
        self.assertEqual(p["site"], "example.com")
        self.assertEqual(p["title"], "Tinnitus is a sound in your ears that other people cannot hear.")
        self.assertEqual([b["t"] for b in p["blocks"]], ["p", "p"])

    def test_article_body_and_link_buttons(self):
        page = ('<html><body><main><ul class="recent"><li>Five tips for travel</li><li>Seven signs of hearing loss</li></ul>'
                '<div class="social" itemprop="articleBody"><p>An audiogram is a chart that shows the results of your '
                'hearing test, from low pitches to high pitches.</p><p><a href="/test">Try a free online hearing test</a></p>'
                '<p>Your hearing care professional will walk you through it, one ear at a time.</p></div></main></body></html>')
        p = ai.extract(page, BASE)
        self.assertEqual([b["text"][:12] for b in p["blocks"]], ["An audiogram", "Your hearing"])

    def test_store_finder_line(self):
        page = ('<html><body><article><p>See a hearing professional for a complete hearing test if your hearing '
                'range is not what it used to be.</p><p>Visit our Shop Finder to find a hearing professional near you.</p>'
                '</article></body></html>')
        self.assertEqual(len(ai.extract(page, BASE)["blocks"]), 1)

    def test_faq_accordion_and_teaser_card(self):
        page = ('<html><body><article><img class="hero__background-image" src="/img/banner.jpg">'
                '<p>Conductive hearing loss occurs when sound cannot reach the inner ear at full strength.</p>'
                '<div class="accordion"><button class="accordion__button" data-toggle="collapse" aria-expanded="false">'
                '<span>How is hearing loss diagnosed?</span></button><div class="collapse">A hearing evaluation by '
                'a hearing care professional, including a hearing test and a look at the ear.</div></div>'
                '<div class="card"><img src="/img/audiogram.jpg"><h2>What is an audiogram?</h2><div><span>An audiogram '
                'is a graph of your hearing test results. <em>…click to read more</em></span></div></div></article></body></html>')
        p = ai.extract(page, BASE)
        self.assertEqual([(b["t"], (b.get("text") or b.get("src"))[:20]) for b in p["blocks"]],
                         [("p", "Conductive hearing l"), ("h3", "How is hearing loss "), ("p", "A hearing evaluation")])

    def test_chinese(self):
        self.assertTrue(ai.is_chinese("助聽器的保養很重要，每天都要清潔。Bluetooth"))
        self.assertFalse(ai.is_chinese("Hearing aids need daily care 助聽器"))
        self.assertFalse(ai.is_chinese("補聴器のお手入れは毎日しましょう"))           # 日文


class Translate(unittest.TestCase):
    BL = [{"t": "h2", "text": "Parts"}, {"t": "p", "text": "A" * 900}, {"t": "img", "src": "x", "alt": ""},
          {"t": "li", "text": "one", "lvl": 0}, {"t": "li", "text": "two", "lvl": 1, "ordered": False},
          {"t": "p", "text": "B" * 900}]

    def test_chunks_and_source(self):
        g = ai.chunks(self.BL, 1500)
        self.assertEqual(g, [[0, 1, 2, 3, 4], [5]])
        src, imgs = ai.chunk_source(self.BL, g[0])
        self.assertEqual(imgs, [2])
        self.assertTrue(src.endswith("[[圖2]]\n\n- one\n  - two"))

    def test_restore_images(self):
        self.assertEqual(ai.restore_images("<think>x</think>譯文\n\n[[ 圖 2 ]]\n\n[[圖9]]", [2]), "譯文\n\n[[圖2]]")
        self.assertEqual(ai.restore_images("譯文", [2, 3]), "譯文\n\n[[圖2]]\n\n[[圖3]]")      # AI 弄丟的補回去

    def test_retry_when_summarised(self):
        chat = mock.Mock(side_effect=["短", "完整的譯文" * 20])
        self.assertEqual(ai.translate_chunk("Long English text. " * 20, [], chat), "完整的譯文" * 20)
        self.assertEqual(chat.call_count, 2)

    def test_fix_zh(self):
        self.assertEqual(ai.fix_zh("助听器的软件和视频\n台北的助聽器，先了解一下"), "助聽器的軟體和影片\n台北的助聽器，先了解一下")
        self.assertFalse(ai.has_simplified("台北市中正區，了解助聽器"))
        self.assertEqual(ai.fix_zh("認為 **您的聽力範圍不完美** 嗎？**聽力圖** ，還有 **重點。** 接著 **Hz** test"),
                         "認為**您的聽力範圍不完美**嗎？**聽力圖**，還有**重點**。接著**Hz** test")
        self.assertEqual(ai.fix_zh("下載類似**「SoundPrint」**的 App\n- **< [** ：右耳\n**A** 說：**B**後\n**沒成對 **A**"),
                         "下載類似「**SoundPrint**」的 App\n- **< [** ：右耳\n**A**說：**B**後\n**沒成對 **A**")
        self.assertEqual(ai.polite("你的聽力圖可以告訴你，你們也可以"), "您的聽力圖可以告訴您，你們也可以")
        self.assertEqual(ai.fix_zh("- **簡約的室內設計**隨著趨勢  \n- **純音氣導測試：** 您會聽到\n1. **步驟一**先做"),
                         "- **簡約的室內設計**：隨著趨勢\n- **純音氣導測試**：您會聽到\n1. **步驟一**：先做")
        self.assertEqual(ai.drop_finder("**想了解更多嗎？** 只要問您的聽力專業人員。還沒有嗎？在這裡輸入您的郵遞區號，尋找附近的專業人員。\n\n"
                                        "請使用我們的門市搜尋功能。\n\n## 下一步\n\n下載 App 找到附近較安靜的場所。突然聽不見請立刻到附近的診所。"),
                         "**想了解更多嗎？** 只要問您的聽力專業人員。\n\n## 下一步\n\n下載 App 找到附近較安靜的場所。突然聽不見請立刻到附近的診所。")

    def test_meta(self):
        chat = mock.Mock(return_value=json.dumps({"title": "助听器如何运作", "alts": ["助聽器"]}))
        self.assertEqual(ai.translate_meta("How hearing aids work", ["A hearing aid"], chat), ("助聽器如何運作", ["助聽器"]))
        chat = mock.Mock(return_value="不是 JSON")
        self.assertEqual(ai.translate_meta("How", ["a", "b"], chat), ("How", ["", ""]))

    def test_warnings(self):
        w = ai.warnings_of("## 標題\n\n這台售價 30000 元。\n\n![保證](/media/uploads/a.webp)\n\n戴了保證聽得最清楚！其他正常。")
        self.assertEqual(len(w), 2)
        self.assertIn("售價", w[0])
        self.assertIn("保證、最清楚", w[1].replace("最清楚、保證", "保證、最清楚"))
        w = ai.warnings_of("聽力閾值在 20 dB HL 以內、250 Hz 到 8 kHz（CDC 建議）。老年性聽損（presbycusis）很常見。"
                           "美國疾病管制與預防中心（Centers for Disease Control）指出，橫軸標示「Frequency (Hz)」。"
                           "**Genesis AI** 助聽器會自動調整。")
        self.assertEqual(len(w), 1)
        self.assertIn("品牌／型號？Genesis AI", w[0])

    def test_image_md(self):
        self.assertEqual(ai.image_md("0123456789abcdef.webp", "助聽器[零件]", ai.credit_text('A "B"')),
                         "![助聽器零件](/media/uploads/0123456789abcdef.webp \"圖片來源：A 'B'\")")


class CurWithId(FakeCur):
    lastrowid = 77


class Build(unittest.TestCase):
    def fake_chat(self, msgs, timeout, json_mode=False, **kw):
        if json_mode and "alts" in msgs[-1]["content"]:
            d = json.loads(msgs[-1]["content"].split("\n", 1)[1])
            return json.dumps({"title": "助聽器怎麼運作（測試請忽略）", "alts": ["零件圖" for _ in d["alts"]]},
                              ensure_ascii=False)
        src = msgs[-1]["content"]                 # 「翻譯」＝每行字前面加「譯：」，Markdown 記號照留
        return "\n".join(ln if ln.startswith(("[[", "|")) else re.sub(r"^((?:#+|\s*-|\s*\d+\.|>)\s)?(?=\S)",
                                                                     lambda m: (m.group(1) or "") + "譯：", ln)
                         for ln in src.split("\n"))

    def run_build(self, with_images=True):
        cur = CurWithId()
        imgs = {"https://www.example.com/img/hero.jpg": png(1200, 800, (10, 20, 30)),
                "https://www.example.com/img/parts-big.jpg": png(1600, 1000, (200, 100, 50))}

        def fetch(url, limit, accept):
            if url not in imgs:
                raise ai.Blocked("沒有")
            return imgs[url], "image/png", url
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(ai, "fetch", side_effect=fetch), \
                mock.patch.object(fb_drafts, "seo_meta", return_value={"summary": "測試摘要", "keywords": ["助聽器原理"],
                                                                      "slug": "how-hearing-aids-work"}):
            r = ai.build(BASE, PAGE, "", "助聽器知識", with_images, "測試", chat=self.fake_chat,
                         conn=lambda: Ctx(cur), media=Path(d))
            files = sorted(p.name for p in Path(d).rglob("*.webp"))
        ins = next(a for s, a in cur.sql if s.startswith("INSERT"))
        upd = [a for s, a in cur.sql if s.startswith("UPDATE")]
        return r, ins, upd, files

    def test_draft(self):
        r, ins, upd, files = self.run_build()
        slug, title, summary, body, cat, status, _, _, extra, by = ins[:10]
        self.assertEqual((r["post_id"], title, status, cat, slug, by),
                         (77, "助聽器怎麼運作（測試請忽略）", "draft", "助聽器知識", "how-hearing-aids-work", "測試"))
        x = json.loads(extra)
        self.assertEqual(x["source"], {"url": BASE, "title": "How hearing aids work", "site": "Example Hearing",
                                       "mode": "translate"})
        self.assertEqual(x["keywords"], ["助聽器原理"])
        self.assertIn("譯：A hearing aid has three basic parts", body)
        self.assertIn("## 譯：The amplifier", body)
        self.assertIn('![零件圖](/media/uploads/', body)
        self.assertIn('"圖片來源：Example Hearing")', body)
        self.assertEqual(body.count("![零件圖]"), 1)                           # 最前面的主圖當封面，不在內文重複
        self.assertEqual(r["images"], 2)
        cover, _, credit, pid = upd[-1]
        self.assertEqual((json.loads(credit), pid), ({"source": "Example Hearing", "url": BASE}, 77))
        self.assertIn(cover, files)
        self.assertEqual(len(files), 3)                                       # 兩張圖＋封面的卡片圖

    def test_no_images(self):
        r, ins, upd, files = self.run_build(with_images=False)
        self.assertNotIn("![", ins[3])
        self.assertEqual((r["images"], upd, files), (0, [], []))

    def test_chinese_source_not_sent_to_ai(self):
        page = ("<html><body><article><h1>助听器保养</h1><p>每天用干布擦拭助听器，晚上放进干燥盒。</p>"
                "<p>助聽器不要碰水，洗澡前先拿下來，平常也要定期回門市保養。</p></article></body></html>")
        cur = CurWithId()
        chat = mock.Mock()
        with mock.patch.object(fb_drafts, "seo_meta", return_value={}):
            r = ai.build(BASE, page, "某廠商", "助聽器知識", False, "測試", chat=chat, conn=lambda: Ctx(cur),
                         media=Path("/nonexistent"))
        chat.assert_not_called()
        ins = next(a for s, a in cur.sql if s.startswith("INSERT"))
        self.assertEqual((r["mode"], ins[1]), ("repost", "助聽器保養"))
        self.assertIn("每天用乾布擦拭助聽器", ins[3])
        self.assertEqual(json.loads(ins[8])["source"]["mode"], "repost")

    def test_too_short(self):
        with self.assertRaises(ai.Blocked):
            ai.build(BASE, "<html><body><p>Login</p></body></html>", "", "助聽器知識", False, "測試",
                     chat=mock.Mock(), conn=mock.Mock(), media=Path("/nonexistent"))


class Source(unittest.TestCase):
    def post(self, **kw):
        return web_admin.clean_post("article", {"title": "測試請忽略", "category": "助聽器知識", **kw})

    def test_clean_source(self):
        self.assertNotIn("source", self.post()["extra"])
        x = self.post(source_url="https://www.example.com/a", source_title="Original")["extra"]["source"]
        self.assertEqual(x, {"url": "https://www.example.com/a", "title": "Original", "site": "example.com",
                             "mode": "translate"})
        for bad in ({"source_site": "ReSound"}, {"source_url": "example.com/a"},
                    {"source_url": "https://e.com/a", "source_mode": "copy"}):
            with self.assertRaises(web_admin.Invalid, msg=bad):
                self.post(**bad)


if __name__ == "__main__":
    unittest.main()
