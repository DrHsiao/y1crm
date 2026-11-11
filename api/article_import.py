"""
article_import.py — 聽力專欄「從網址翻譯文章」。

John 2026-11-11：科技網站、各大助聽器製造商官網的助聽器知識，翻譯後轉貼到聽力專欄，給出處；
圖片複製回來、標很小的出處文字（總公司代理各大製造商的產品，圖片授權沒問題）。

流程（背景執行，status 看進度）：
  店員貼原文網址 → 抓網頁（只連公開網址，擋內網位址）→ 找出內文（標題、段落、清單、表格、圖片）
  → 本機 AI（573 純文字）分段翻成台灣繁體中文；價格、購買、找門市預約的句子省略；中文原文只轉成台灣繁體用語
  → 圖片轉存到官網 media/uploads（只縮尺寸、轉 WebP，不改內容），內文寫成 ![說明](網址 "圖片來源：出處")
  → 建一篇「草稿」：出處記在 extra.source（官網文章上方顯示「本文翻譯整理自…」並連到原文），
    封面用原文的主圖（extra.cover_src="source"，官網封面下方小字標出處）。店員看過、改好再發佈。
網站擋程式讀取（403、驗證頁）不繞過：請店員在瀏覽器選取全文複製、貼到對話框（html），一樣會帶圖。

本檔是純規則（網址檢查、內文擷取、分段、組 Markdown）＋I/O（抓網頁、呼叫本機 AI、存檔），規則有單元測試。
"""
from __future__ import annotations

import io
import ipaddress
import json
import logging
import re
import socket
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin, urlparse

from PIL import Image

import config
from api import fb_drafts, web_admin, wording

log = logging.getLogger("y1crm.article_import")

UA = "Mozilla/5.0 (compatible; y1crm-article-import/1.0; +https://y1ear.com)"   # 照實說是程式，不假裝瀏覽器
PAGE_MAX = 4 * 1024 * 1024
IMG_MAX = 12 * 1024 * 1024
FETCH_TIMEOUT = 20
IMAGES_MAX = 12                  # 一篇最多帶幾張圖
IMG_MIN_W, IMG_MIN_H = 200, 100  # 比這小的多半是圖示、按鈕
CHUNK_CHARS = 1500               # 一次翻多少字（原文）：573 一段約 5～20 秒，LINE 客服 AI 共用這張卡，不要一次塞太多
SOURCE_MAX = 60000               # 原文太長（整本型錄）就拒收
PASTE_MAX = 2_000_000
MODES = {"translate": "翻譯整理", "repost": "轉載"}


class Blocked(Exception):
    """網頁抓不到（網站擋程式、找不到、不是網頁）：訊息直接給店員看。"""


# ── 網址 ─────────────────────────────────────────────────────

def check_url(url: str) -> str:
    """只收 http(s)、一般 port 的公開網址；回傳去掉頭尾空白的網址。不合格丟 web_admin.Invalid。"""
    url = (url or "").strip()
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname or not web_admin.URL_RE.match(url):
        raise web_admin.Invalid("請貼完整的原文網址（https:// 開頭）。")
    if p.port not in (None, 80, 443) or p.username or p.password:
        raise web_admin.Invalid("這個網址的格式不支援，請貼瀏覽器網址列上的網址。")
    if len(url) > web_admin.LIMITS["url"]:
        raise web_admin.Invalid(f"網址太長了（最多 {web_admin.LIMITS['url']} 個字）。")
    return url


def public_host(host: str) -> bool:
    """主機名稱解析出來的位址全部是公開網路才連（不讓這個功能被拿來連本機、內網的服務）。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    ips = {i[4][0] for i in infos}
    return bool(ips) and all(ipaddress.ip_address(ip.split("%")[0]).is_global for ip in ips)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        p = urlparse(newurl)
        if p.scheme not in ("http", "https") or p.port not in (None, 80, 443) or not public_host(p.hostname or ""):
            raise Blocked("原文網址轉到不能連的位址，請改貼原文內容。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirect)


def safe_url(url: str) -> str:
    """網址裡的空白、中文先編碼（Starkey 的圖片檔名有空白；urllib 不收）；已經編碼過的不重複編。"""
    return quote(url.strip(), safe=":/?#[]@!$&'()*+,;=%~")


def fetch(url: str, limit: int, accept: str) -> Tuple[bytes, str, str]:
    """抓一個公開網址：回 (內容, Content-Type, 最後的網址)。抓不到丟 Blocked。"""
    url = safe_url(url)
    p = urlparse(url)
    if p.scheme not in ("http", "https") or p.port not in (None, 80, 443) or not public_host(p.hostname or ""):
        raise Blocked("這個網址連不到（不是公開網站）。")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept,
                                               "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"})
    try:
        with _opener.open(req, timeout=FETCH_TIMEOUT) as r:
            data = r.read(limit + 1)
            ctype = r.headers.get("Content-Type") or ""
            final = r.geturl()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 429, 503):
            raise Blocked(f"這個網站不讓程式讀取（HTTP {e.code}）。請在瀏覽器打開原文，選取全文複製，貼到下面的方框。")
        raise Blocked(f"打不開這個網址（HTTP {e.code}）。")
    except (urllib.error.URLError, OSError) as e:
        raise Blocked(f"連不上這個網站（{getattr(e, 'reason', e)}）。")
    except ValueError as e:                     # http.client.InvalidURL 這類網址格式問題
        raise Blocked(f"這個網址的格式有問題（{e}）。")
    if len(data) > limit:
        raise Blocked("檔案太大，不像是一篇文章。")
    return data, ctype, final


def decode_html(data: bytes, ctype: str) -> str:
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I) or re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", data[:4096], re.I)
    enc = m.group(1).decode() if m and isinstance(m.group(1), bytes) else (m.group(1) if m else "utf-8")
    try:
        return data.decode(enc, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


# ── 內文擷取 ─────────────────────────────────────────────────
#   不靠外部套件：自己把 HTML 建成樹 → 去掉選單、頁尾、分享鈕這類 → 找「包含八成以上段落文字」最深的那一層當內文
#   → 依序轉成區塊（h／p／li／quote／table／img）。

_VOID = {"img", "br", "hr", "input", "meta", "link", "source", "wbr", "area", "base", "col", "embed", "param", "track"}
_SKIP = {"script", "style", "noscript", "template", "svg", "iframe", "form", "button", "select", "textarea", "nav",
         "footer", "aside", "dialog", "object", "embed", "canvas", "video", "audio", "input", "label", "head", "math"}
_NOISE_RE = re.compile(r"(?:^|[-_])(?:nav|navbar|navigation|menu|megamenu|breadcrumbs?|cookies?|consent|onetrust|"
                       r"share|sharing|social|related|newsletter|subscribe|modal|popup|sidebar|footer|comments?|"
                       r"pagination|skip|visually-hidden|sr-only|screen-reader|advert|ads?|promo|banner-cta)"
                       r"(?:$|[-_])", re.I)
_BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "figcaption", "dt", "dd", "pre", "table",
           "ul", "ol", "dl", "figure", "div", "section", "article", "main", "header", "tr", "td", "th", "hr",
           "picture", "img", "details", "summary"}
# 內文到這裡結束（後面是延伸閱讀、找門市、客服這類）
_STOP_RE = re.compile(r"^(related (articles|posts|content|reading)|you (may|might) also like|more articles|"
                      r"read more|recommended( for you)?|share this( article)?|(do you )?(still )?need (more )?(help|support)|"
                      r"ready to take the (first|next) step.*|try our (free )?online hearing test.*|"
                      r"find (a|your) (clinic|store|hearing care professional|hearing aid provider).*|"
                      r"book (an|your) appointment.*|contact (us|support)|join our (community|newsletter|mailing list).*|"
                      r"(subscribe|sign up) (to|for) our (blog|newsletter).*|"
                      r"延伸閱讀|相關文章|推薦閱讀|你可能也喜歡|更多文章)[\s?？.。!！:：]*$", re.I)
_CJK_RE = re.compile(r"[一-鿿]")
_KANA_RE = re.compile(r"[぀-ヿ]")


# 按鈕字（「Learn more」「Take the test」）不是內文；「…click to read more」是延伸閱讀的卡片：連同卡片的標題、圖一起拿掉
_CTA_RE = re.compile(r"^(learn more|read more|find out more|discover more|see more|more info|shop now|buy now|"
                     r"take the (free )?(online )?(hearing )?test|book (an|your) appointment|find a (clinic|provider|"
                     r"hearing care professional|store)( near you)?|contact us|sign up|subscribe|download( now)?|"
                     r"get started|watch (the )?video|(visit|use|try) our (shop|store|clinic|provider|location) ?"
                     r"(finder|locator)\b.*|了解更多|閱讀更多|立即預約|立即購買|線上預約|看更多)[\s.:：!！>›→]*$", re.I)
_TEASER_RE = re.compile(r"(…|\.{2,})\s*(click to )?(read|learn) more[\s.!>›→]*$", re.I)    # 延伸閱讀卡片的「…看更多」


class Node:
    __slots__ = ("tag", "attrs", "kids", "parent")

    def __init__(self, tag: str, attrs: Dict[str, str], parent: Optional["Node"]):
        self.tag, self.attrs, self.kids, self.parent = tag, attrs, [], parent

    def text(self) -> str:
        out = []
        for k in self.kids:
            out.append(k if isinstance(k, str) else ("" if k.tag in _SKIP else k.text()))
        return "".join(out)

    def iter(self):
        yield self
        for k in self.kids:
            if isinstance(k, Node):
                yield from k.iter()


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag in ("p", "li", "dt", "dd") and self.cur.tag == tag:      # <p>、<li> 沒關就開下一個
            self.cur = self.cur.parent or self.root
        n = Node(tag, a, self.cur)
        self.cur.kids.append(n)
        if tag not in _VOID:
            self.cur = n

    def handle_startendtag(self, tag, attrs):
        self.cur.kids.append(Node(tag, {k: (v or "") for k, v in attrs}, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.kids.append(data)


def parse(html_text: str) -> Node:
    t = _Tree()
    t.feed(html_text)
    t.close()
    return t.root


def _noise(n: Node) -> bool:
    if n.tag in _SKIP:
        return True
    a = n.attrs
    if "hidden" in a or a.get("aria-hidden") == "true" or re.search(r"display\s*:\s*none", a.get("style", ""), re.I):
        return True
    if a.get("itemprop") == "articleBody":         # 標明是內文的就是內文（Starkey 的內文 class 叫 social）
        return False
    if a.get("role") in ("navigation", "banner", "contentinfo", "dialog", "complementary", "search"):
        return True
    toks = (a.get("class", "") + " " + a.get("id", "")).split()
    return any(_NOISE_RE.search(t) for t in toks)


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def meta_info(root: Node) -> Dict[str, str]:
    """<title>、og:title、og:site_name、og:image、<html lang>。"""
    out = {"title": "", "og_title": "", "site": "", "og_image": "", "lang": ""}
    for n in root.iter():
        if n.tag == "html" and not out["lang"]:
            out["lang"] = n.attrs.get("lang", "")
        elif n.tag == "title" and not out["title"]:
            out["title"] = _squash(n.text())
        elif n.tag == "meta":
            prop = (n.attrs.get("property") or n.attrs.get("name") or "").lower()
            val = _squash(n.attrs.get("content", ""))
            key = {"og:title": "og_title", "og:site_name": "site", "og:image": "og_image",
                   "twitter:image": "og_image"}.get(prop)
            if key and val and not out[key]:
                out[key] = val
    return out


def _weight(s: str) -> int:
    """段落的份量：中文一個字算三個英文字母。"""
    return len(s) + 2 * len(_CJK_RE.findall(s))


_PARA = ("p", "li", "blockquote", "dd", "td")


def main_node(root: Node) -> Node:
    """包含八成以上「像段落的文字」最深的那個元素（選單、頁尾、側欄不算）。"""
    score: Dict[int, int] = {}
    total = 0

    def walk(n: Node, ok: bool) -> int:
        ok = ok and not _noise(n)
        s = 0
        if n.tag in _PARA:
            w = _weight(_squash(n.text())) if ok else 0
            s = w if w >= 60 else 0
        else:
            loose = _weight(_squash("".join(k for k in n.kids if isinstance(k, str)))) if ok else 0
            s = loose if loose >= 60 else 0
            for k in n.kids:
                if isinstance(k, Node):
                    s += walk(k, ok)
        score[id(n)] = s
        return s

    total = walk(root, True)
    if total == 0:
        body = next((n for n in root.iter() if n.tag == "body"), root)
        return body
    n = root
    while True:
        nxt = next((k for k in n.kids if isinstance(k, Node) and k.tag not in _PARA
                    and score.get(id(k), 0) >= total * 0.8), None)
        if nxt is None:
            return n
        n = nxt


def _img_src(n: Node) -> str:
    """<img> 的圖片網址：延遲載入的 data-src、srcset 取最大的。"""
    a = n.attrs
    best, best_w = "", -1
    for key in ("srcset", "data-srcset"):
        for part in (a.get(key) or "").split(","):
            bits = part.strip().split()
            if not bits:
                continue
            w = 0
            if len(bits) > 1:
                m = re.match(r"(\d+(?:\.\d+)?)([wx])", bits[1])
                w = int(float(m.group(1)) * (1 if m and m.group(2) == "w" else 1000)) if m else 0
            if w > best_w:
                best, best_w = bits[0], w
    for key in ("data-src", "data-lazy-src", "data-original", "src"):
        v = (a.get(key) or "").strip()
        if v and not v.startswith("data:") and not best:
            best = v
    return best


def _junk_img(src: str, n: Node) -> bool:
    low = src.lower().split("?")[0]
    if not src or low.endswith(".svg") or src.startswith("data:"):
        return True
    if re.search(r"(logo|icon|sprite|avatar|badge|pixel|spacer|tracking|flag[-_])", low):
        return True
    if re.search(r"(?:^|[-_])(background|bg)(?:$|[-_])", n.attrs.get("class", ""), re.I):   # 橫幅底圖（裝飾用）
        return True
    for k, lim in (("width", IMG_MIN_W), ("height", IMG_MIN_H)):
        v = re.match(r"\d+", n.attrs.get(k, "") or "")
        if v and int(v.group()) < lim:
            return True
    return False


def _inline(n: Node, skip: Tuple[str, ...] = ()) -> str:
    """區塊裡的文字：粗體留 **，連結只留文字，<br> 當空白。skip＝不要的子元素（清單項目裡的子清單）。"""
    out = []
    for k in n.kids:
        if isinstance(k, str):
            out.append(k)
        elif k.tag in _SKIP or _noise(k) or k.tag == "img" or k.tag in skip:
            continue
        elif k.tag == "br":
            out.append(" ")
        elif k.tag in ("strong", "b"):
            t = _squash(_inline(k))
            out.append(f" **{t}** " if t else "")
        else:
            out.append(_inline(k))
    return re.sub(r"\s+", " ", "".join(out))


def _link_only(n: Node) -> bool:
    """整段只有一個短連結（「Learn more about hearing loss」「Try a free online hearing test」這種按鈕）。"""
    t = _squash(n.text())
    if not t or len(t) > 80:
        return False
    links = [g for g in n.iter() if g.tag == "a"]
    return len(links) == 1 and _squash(links[0].text()) == t


def _toggle(n: Node) -> bool:
    """常見問題的折疊按鈕（按鈕文字就是問題）。"""
    a = n.attrs
    return n.tag == "button" and ("aria-expanded" in a or "aria-controls" in a or "collapse" in
                                  (a.get("data-toggle", "") + a.get("data-bs-toggle", "")) or "accordion" in a.get("class", ""))


def _md_escape_cell(s: str) -> str:
    return s.replace("|", "／")


def blocks(container: Node, base_url: str, title: str = "") -> List[Dict[str, Any]]:
    """內文 → 區塊清單：{"t": "h2"|"h3"|"p"|"li"|"quote"|"table"|"img", "text"|"rows"|"src"/"alt", "lvl"}。"""
    out: List[Dict[str, Any]] = []
    stop = False
    titled = False

    def add(b: Dict[str, Any]) -> None:
        txt = b.get("text")
        if txt is not None:
            txt = _squash(txt).replace("** **", " ").strip()
            if not txt or txt in ("**", "****"):
                return
            b["text"] = txt
        if out and out[-1] == b:
            return
        out.append(b)

    def img(n: Node, caption: str = "") -> None:
        src = _img_src(n)
        if _junk_img(src, n):
            return
        add({"t": "img", "src": urljoin(base_url, src), "alt": _squash(n.attrs.get("alt", "")) or caption[:120]})

    def walk(n: Node, lvl: int = 0, ordered: bool = False) -> None:
        nonlocal stop, titled
        buf: List[str] = []

        def flush() -> None:
            t = _squash("".join(buf))
            buf.clear()
            if t:
                add({"t": "p", "text": t})

        for k in n.kids:
            if stop:
                return
            if isinstance(k, str):
                buf.append(k)
                continue
            if _toggle(k):
                flush()
                add({"t": "h3", "text": _squash(k.text())})
                continue
            if _noise(k):
                continue
            tag = k.tag
            if tag not in _BLOCKS and tag not in ("br",):
                if any(isinstance(g, Node) and g.tag in _BLOCKS for g in k.iter() if g is not k):
                    flush()
                    walk(k, lvl, ordered)        # <span> 包著區塊（排版用的）：當成容器
                else:
                    if tag in ("strong", "b"):
                        t = _squash(_inline(k))
                        buf.append(f" **{t}** " if t else "")
                    else:
                        buf.append(_inline(k))
                    for g in k.iter():
                        if g.tag == "img":
                            flush()
                            img(g)
                continue
            flush()
            if tag == "br" or tag == "hr":
                continue
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                t = _squash(_inline(k)).replace("**", "")
                if _STOP_RE.match(t):
                    stop = True
                    return
                if not t or len(re.findall(r"[│|｜]", t)) >= 2:          # 頁內目錄「清潔 │ 收納 │ 常見錯誤」
                    continue
                if not titled and t.lower() == _squash(title).lower():   # 文章標題本身（上面已經有了）
                    titled = True
                    continue
                add({"t": "h2" if tag in ("h1", "h2") else "h3", "text": t})
            elif tag in ("p", "dt", "dd", "summary", "figcaption", "pre"):
                for g in k.iter():
                    if g.tag == "img":
                        img(g)
                if tag == "p" and _link_only(k):
                    continue
                t = _inline(k)
                if tag == "dt":
                    t = f"**{_squash(t)}**" if _squash(t) else ""
                add({"t": "p", "text": t})
            elif tag == "li":
                add({"t": "li", "text": _inline(k, ("ul", "ol")), "lvl": lvl, "ordered": ordered})
                for g in k.kids:
                    if isinstance(g, Node) and g.tag in ("ul", "ol"):
                        walk(g, lvl + 1, g.tag == "ol")
            elif tag in ("ul", "ol"):
                walk(k, lvl, tag == "ol")
            elif tag == "blockquote":
                t = _squash(_inline(k)) if not any(isinstance(g, Node) and g.tag == "p" for g in k.kids) \
                    else " ".join(_squash(_inline(g)) for g in k.kids if isinstance(g, Node) and g.tag == "p")
                add({"t": "quote", "text": t})
            elif tag == "table":
                rows = []
                for tr in k.iter():
                    if tr.tag == "tr":
                        cells = [_md_escape_cell(_squash(_inline(c)).replace("**", "")) for c in tr.kids
                                 if isinstance(c, Node) and c.tag in ("td", "th")]
                        if any(cells):
                            rows.append(cells)
                if rows and max(len(r) for r in rows) <= 6:
                    add({"t": "table", "rows": rows})
                elif rows:
                    for r in rows:
                        add({"t": "p", "text": "；".join(c for c in r if c)})
            elif tag == "figure":
                cap = next((g for g in k.iter() if g.tag == "figcaption"), None)
                cap_t = _squash(_inline(cap)) if cap else ""
                for g in k.iter():
                    if g.tag == "img":
                        img(g, cap_t)
                        break
                if cap_t:
                    add({"t": "p", "text": cap_t})
            elif tag == "img":
                img(k)
            elif tag == "picture":
                g = next((x for x in k.iter() if x.tag == "img"), None)
                if g is not None:
                    img(g)
            else:                                 # div、section、article…：容器
                walk(k, lvl, ordered)
        flush()

    walk(container)
    return drop_ctas(out)


def drop_ctas(bl: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for b in bl:
        t = b.get("text") or ""
        if b["t"] in ("p", "h2", "h3") and _CTA_RE.match(t.replace("**", "").strip()):
            continue
        if b["t"] == "p" and _TEASER_RE.search(t):
            while out and out[-1]["t"] not in ("h2", "h3"):
                out.pop()
            if out:
                out.pop()
            if out and out[-1]["t"] == "img":
                out.pop()
            continue
        out.append(b)
    while out and out[-1]["t"] in ("h2", "h3", "img"):        # 最後只剩標題或圖（卡片拿掉後）的不要
        out.pop()
    return out


def _visible(n: Node) -> bool:
    while n is not None:
        if _noise(n):
            return False
        n = n.parent
    return True


def page_title(meta: Dict[str, str], root: Node, container: Node) -> str:
    """文章標題：og:title（去掉「｜網站名」；網頁給社群分享用的，最準），再來內文裡的 <h1>、整頁第一個看得到的 <h1>、<title>。
    （有些網站一頁有好幾個 <h1>，藏在分頁標籤裡的也算，所以 <h1> 排後面）"""
    og = re.split(r"\s+[|｜–—-]\s+", meta.get("og_title") or "")[0].strip()
    if og and og.lower() != (meta.get("site") or "").lower():
        return og
    for box in (container, root):
        h1 = next((n for n in box.iter() if n.tag == "h1" and _visible(n) and _squash(n.text())), None)
        if h1 is not None:
            return _squash(h1.text())
    return re.split(r"\s+[|｜–—-]\s+", meta.get("title") or "")[0].strip()


def clean_title(t: str) -> str:
    """標題後面的「[2026]」這種年份拿掉（搜尋引擎用的，不是標題的一部分）。"""
    return re.sub(r"\s*[\[(（【]\s*20\d\d\s*[\])）】]\s*", " ", t or "").strip()


def is_chinese(text: str) -> bool:
    """原文是不是中文（日文有假名，不算）。"""
    if _KANA_RE.search(text[:3000]):
        return False
    sample = re.sub(r"\s", "", text[:3000])
    return bool(sample) and len(_CJK_RE.findall(sample)) / len(sample) > 0.3


def site_name(meta: Dict[str, str], url: str) -> str:
    s = meta.get("site") or ""
    if not s:
        host = (urlparse(url).hostname or "").lower()
        s = re.sub(r"^(www|m)\.", "", host)
    return s[:web_admin.LIMITS["source_site"]]


def extract(html_text: str, base_url: str) -> Dict[str, Any]:
    """網頁 → {title, site, lang, og_image, blocks}。"""
    root = parse(html_text)
    meta = meta_info(root)
    box = main_node(root)
    title = clean_title(page_title(meta, root, box))
    bl = blocks(box, base_url, title)
    if not title:                         # 貼上的片段沒有標題：用第一個小標，或第一段的開頭
        title = next((b["text"] for b in bl if b["t"] in ("h2", "h3")), "") or next(
            (re.split(r"(?<=[.!?。！？])\s", b["text"])[0][:80] for b in bl if b["t"] == "p"), "")
    og = urljoin(base_url, meta["og_image"]) if meta.get("og_image") else ""
    return {"title": title, "site": site_name(meta, base_url), "lang": meta.get("lang", ""),
            "og_image": og if og.startswith(("http://", "https://")) else "", "blocks": bl}


# ── 分段翻譯 ─────────────────────────────────────────────────

def block_md(b: Dict[str, Any], img_tag: str = "") -> str:
    t = b["t"]
    if t == "img":
        return img_tag
    if t == "h2":
        return "## " + b["text"]
    if t == "h3":
        return "### " + b["text"]
    if t == "li":
        return "  " * b.get("lvl", 0) + ("1. " if b.get("ordered") else "- ") + b["text"]
    if t == "quote":
        return "> " + b["text"]
    if t == "table":
        rows = b["rows"]
        n = max(len(r) for r in rows)
        rows = [r + [""] * (n - len(r)) for r in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * n]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(lines)
    return b["text"]


def join_md(lines: List[Tuple[str, str]]) -> str:
    """(區塊種類, Markdown) → 整篇：清單項目之間不空行，其他空一行。"""
    out: List[str] = []
    prev = ""
    for t, s in lines:
        if not s.strip():
            continue
        if out:
            out.append("\n" if t == "li" and prev == "li" else "\n\n")
        out.append(s)
        prev = t
    return "".join(out).strip()


def chunks(bl: List[Dict[str, Any]], limit: int = CHUNK_CHARS) -> List[List[int]]:
    """區塊依序分組（每組原文不超過 limit 字；表格、單一區塊太長就自己一組）。回區塊的索引。"""
    groups: List[List[int]] = []
    cur: List[int] = []
    size = 0
    for i, b in enumerate(bl):
        n = len(block_md(b, "[[圖]]"))
        if cur and size + n > limit:
            groups.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += n
    if cur:
        groups.append(cur)
    return groups


SYSTEM = """你是專業翻譯。把使用者給的文章段落翻成台灣繁體中文，讀者是台灣的長輩和家屬。
規則：
1. 忠實翻譯，不增加原文沒有的內容，不加評論或說明。
2. 保留 Markdown 格式（## 標題、### 小標、- 清單、1. 編號、> 引言、**粗體**、表格），一行原文對一行譯文。
3. [[圖1]] 這類標記原樣保留，自己一行。
4. 台灣用語：hearing aid＝助聽器（絕對不要寫成耳機）、hearing loss＝聽力損失、audiologist＝聽力師、
   hearing care professional＝聽力專業人員、ENT＝耳鼻喉科醫師、tinnitus＝耳鳴、rechargeable＝充電式、
   receiver＝受話器、earmold＝耳模、dome＝耳塞、behind-the-ear (BTE)＝耳掛型、receiver-in-canal (RIC)＝耳道受話器型、
   in-the-ear (ITE)＝耳內型、wax guard／wax filter＝防耳垢濾網、app＝App、Bluetooth＝藍牙、software＝軟體、video＝影片、
   user＝使用者（不要寫「用戶」）、contact＝聯絡（不要寫「聯繫」）、wear＝配戴（不要寫「佩戴」）、tips＝小提醒。
5. 品牌、產品型號保留原文（例如 ReSound、Phonak、Oticon、Signia）。
6. 提到價格、費用、優惠、購買、尋找門市、預約的句子整句省略。
7. 稱呼讀者一律用「您」（不要用「你」）。
8. 只輸出譯文，不要加任何前言或說明。"""

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_PH_RE = re.compile(r"\[\[\s*圖\s*(\d+)\s*\]\]")


def chunk_source(bl: List[Dict[str, Any]], idx: List[int]) -> Tuple[str, List[int]]:
    """一組區塊 → 給 AI 的 Markdown（圖片換成 [[圖n]]）；回 (文字, 這組有哪些圖的區塊索引)。"""
    lines, imgs = [], []
    for i in idx:
        b = bl[i]
        if b["t"] == "img":
            imgs.append(i)
            lines.append((b["t"], f"[[圖{i}]]"))
        else:
            lines.append((b["t"], block_md(b)))
    return join_md(lines), imgs


def restore_images(text: str, imgs: List[int]) -> str:
    """譯文裡的 [[圖n]]：AI 弄丟的補在這段最後；不是這段的拿掉。"""
    text = _THINK_RE.sub("", text or "").strip()
    seen = {int(m.group(1)) for m in _PH_RE.finditer(text)}
    text = _PH_RE.sub(lambda m: f"[[圖{m.group(1)}]]" if int(m.group(1)) in imgs else "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    lost = [i for i in imgs if i not in seen]
    if lost:
        text = text.rstrip() + "".join(f"\n\n[[圖{i}]]" for i in lost)
    return text


def translate_chunk(src: str, imgs: List[int], chat: Callable[..., str]) -> str:
    """翻一段；空的或短得不像翻譯（被摘要掉）就再要一次。"""
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": src}]
    best = ""
    plain = _PH_RE.sub("", src)
    for attempt in range(2):
        raw = chat(msgs, 240, temperature=0.2, num_predict=min(4000, 400 + len(src) * 2))
        out = restore_images(raw, imgs)
        if len(_PH_RE.sub("", out).strip()) >= min(len(plain.strip()) * 0.25, 40):
            return out
        best = best or out
        msgs += [{"role": "assistant", "content": raw},
                 {"role": "user", "content": "請完整翻譯每一句，不要摘要或省略（價格、購買、找門市的句子除外），重新給我整段譯文。"}]
    return best


# 簡體字：本機 AI 偶爾冒出來，那一行用 OpenCC 轉成台灣繁體用語（「台」這種台灣也通用的字不算）
_TW_OK = set("台了里面干后系只制准于云才几出斗冲范借卜丑")
_cc: Dict[str, Any] = {}


def _opencc(kind: str):
    if kind not in _cc:
        from opencc import OpenCC
        _cc[kind] = OpenCC(kind)
    return _cc[kind]


def has_simplified(line: str) -> bool:
    t = _opencc("s2t").convert(line)
    if len(t) != len(line):
        return t != line
    return any(a != b and a not in _TW_OK for a, b in zip(line, t))


# 繁體字、但是大陸的說法：換成台灣說法（只放不會換錯意思的）
TW_WORDS = {"用戶": "使用者", "聯繫": "聯絡", "佩戴": "配戴", "小貼士": "小提醒", "屏幕": "螢幕", "兼容": "相容",
            "默認": "預設", "激活": "啟用", "視頻": "影片", "軟件": "軟體", "質量": "品質", "網絡": "網路", "信息": "訊息",
            "感覺神經性": "感音神經性"}
_TW_RE = re.compile("|".join(map(re.escape, TW_WORDS)))


# 清單開頭的粗體小標直接接內文（「- **簡約的室內設計**隨著…」）：補全形冒號
_LABEL_RE = re.compile(r"^(\s*(?:[-*]|\d+\.) \*\*[^*\n]*[^*\s，。、；：！？:])\*\*(?=[一-鿿])", re.M)
_CJK_PUNCT = "，。、；：！？「」『』（）"


def _punct(c: str) -> bool:
    return unicodedata.category(c)[0] in "PS"


def _cjk_side(c: str) -> bool:
    return bool(_CJK_RE.match(c)) or c in _CJK_PUNCT


def _tidy_bold(line: str) -> str:
    """一行裡的粗體整理成 Markdown 認得的樣子：句尾標點、「」（）移到粗體外面，中文旁邊不留空格。
    （「**重點：**接著」「**「X」**的」這種，粗體貼著標點又接中文，Markdown 不當粗體，畫面會出現 **）"""
    parts = line.split("**")
    if len(parts) < 3 or len(parts) % 2 == 0:
        return line
    out = parts[0]
    for i in range(1, len(parts), 2):
        b, nxt = parts[i].strip(), parts[i + 1]
        if not b:
            out += "**" + parts[i] + "**" + nxt
            continue
        m = re.search(r"[，。、；：！？]+$", b)
        tail = m.group() if m else ""
        b = b[:len(b) - len(tail)] if tail and len(tail) < len(b) else b
        tail = tail if b != parts[i].strip() else ""
        lead = close = ""
        if len(b) > 2 and b[0] in "「『（" and b[-1] in "」』）":
            lead, b, close = b[0], b[1:-1], b[-1]
        if out.endswith(" ") and len(out) > 1 and _cjk_side(out[-2]) and (lead or not _punct(b[0])):
            out = out[:-1]
        if nxt.startswith(" ") and len(nxt) > 1 and _cjk_side(nxt[1]) and (close or tail or not _punct(b[-1])):
            nxt = nxt[1:]
        out += f"{lead}**{b}**{close}{tail}" + nxt
    return out


def fix_zh(text: str) -> str:
    lines = (_opencc("s2twp").convert(ln).replace("臺", "台").rstrip() if has_simplified(ln) else ln.rstrip()
             for ln in (text or "").split("\n"))
    t = _TW_RE.sub(lambda m: TW_WORDS[m.group()], "\n".join(lines))
    t = _LABEL_RE.sub(r"\1**：", t)
    return "\n".join(_tidy_bold(ln) for ln in t.split("\n"))


# 翻完還留著的「找門市、輸入郵遞區號、預約」句子（原廠網站的導購）整句拿掉；前面接的短問句（「還沒有嗎？」）一起拿掉。
# 「附近的診所／醫院」不算（突發性聽損「立刻到附近的診所」這種就醫提醒要留著）
_FINDER_RE = re.compile(r"郵遞區號|門市搜尋|搜尋門市|門市查詢|附近的(聽力)?(專業人員|門市)|線上預約|立即預約|"
                        r"預約(聽力)?(檢查|測試|評估|諮詢)")


def drop_finder(text: str) -> str:
    out = []
    for line in text.split("\n"):
        if line.startswith(("![", "#")) or not _FINDER_RE.search(line):
            out.append(line)
            continue
        keep: List[str] = []
        for sent in re.findall(r"[^。！？!?]+[。！？!?]*", line):
            if _FINDER_RE.search(sent):
                if keep and len(keep[-1].replace("*", "").strip()) <= 8 and keep[-1].rstrip().endswith(("？", "?")):
                    keep.pop()
                continue
            keep.append(sent)
        line = "".join(keep).strip()
        if line and line not in ("-", "*"):
            out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def polite(text: str) -> str:
    """譯文稱呼讀者用「您」（模型偶爾會換回「你」；「你們」不動）。"""
    return re.sub(r"你(?!們)", "您", text)


def translate_meta(title: str, alts: List[str], chat: Callable[..., str]) -> Tuple[str, List[str]]:
    """標題和圖片說明一起翻（JSON）；格式不對：標題用原文、說明留空。"""
    prompt = ("把下面 JSON 的文字翻成台灣繁體中文（hearing aid＝助聽器），品牌型號保留原文，只回同樣結構的 JSON。"
              "title 要像台灣的中文文章標題：自然、簡潔，不要逐字直譯（不要出現「一個」「的指南」這類翻譯腔）：\n"
              + json.dumps({"title": title, "alts": alts}, ensure_ascii=False))
    try:
        d = json.loads(chat([{"role": "user", "content": prompt}], 120, json_mode=True, temperature=0.2,
                            num_predict=1200))
    except (ValueError, TypeError):
        d = {}
    t = fix_zh(_squash(str(d.get("title") or ""))) if isinstance(d, dict) else ""
    a = d.get("alts") if isinstance(d, dict) else None
    a = [fix_zh(_squash(str(x)))[:120] for x in a] if isinstance(a, list) and len(a) == len(alts) else [""] * len(alts)
    return (t or title)[:web_admin.LIMITS["title"]], a


_IMG_MD_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
PROMO_WORDS = ("免費", "優惠", "限時", "促銷", "贈送", "買一送一")      # 同粉專貼文的規定：不談價格、優惠、免費


# 譯文裡留著的大寫開頭英文字多半是原廠品牌、型號（Genesis AI、Omega AI+）；單位、機構縮寫、小寫的醫學名詞不算
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+\-]*(?:[ \t]+[A-Za-z0-9][A-Za-z0-9+\-]*)*")
_LATIN_OK = {"db", "hz", "khz", "hl", "spl", "cdc", "who", "nih", "nidcd", "app", "apps", "bluetooth", "line"}


# 中文後面括號附的英文原名（美國疾病管制與預防中心（Centers for …））、引號裡的圖表標示（「Frequency (Hz)」）不算
_GLOSS_RE = re.compile(r"[（「][A-Za-z0-9 .,'+/()\-]+[）」]")


def brand_words(s: str) -> List[str]:
    out = []
    for m in _LATIN_RE.findall(_GLOSS_RE.sub("", s)):
        if any(len(w) >= 3 and w[0].isupper() and w.lower() not in _LATIN_OK for w in m.split()) and m not in out:
            out.append(m)
    return out


def warnings_of(md: str) -> List[str]:
    """價格、療效／最高級用語、原廠品牌型號：列出那一句給店員改（官網規定不談價格、不寫療效、不替別家產品宣傳）。"""
    out = []
    for sent in re.findall(r"[^。！？!?\n]+[。！？!?]?", _IMG_MD_RE.sub("", md)):
        s = sent.strip()
        if not s:
            continue
        p = wording.price_word(s)
        c = wording.claim_words(s) + [w for w in PROMO_WORDS if w in s]
        b = brand_words(s.replace("**", ""))
        if p or c or b:
            why = ([p] if p else []) + c + ([f"品牌／型號？{'、'.join(b)}"] if b else [])
            out.append(f"「{s[:60]}{'…' if len(s) > 60 else ''}」（{'、'.join(why)}）")
    return out[:20]


def _md_text(s: str) -> str:
    return re.sub(r"[\[\]]", "", s or "").strip()


def image_md(name: str, alt: str, credit: str) -> str:
    credit = credit.replace('"', "'")
    return f'![{_md_text(alt)}](/media/uploads/{name} "{credit}")'


def credit_text(site: str) -> str:
    return f"圖片來源：{site}"


# ── 整篇：抓 → 擷取 → 翻譯 → 存圖 → 建草稿 ─────────────────────

_lock = threading.Lock()
status: Dict[str, Any] = {"running": False}


def _step(msg: str, **kw) -> None:
    status.update(step=msg, **kw)


def _save_img(media: Path, url: str, thumb: bool = False) -> Optional[str]:
    """原文圖片 → 官網 media/uploads（縮到 1600px、轉 WebP；太小的圖示不要）。失敗回 None。"""
    try:
        data, _, _ = fetch(url, IMG_MAX, "image/avif;q=0,image/webp,image/png,image/jpeg,image/*;q=0.8")
        out, name = web_admin.process_image(data)
        with Image.open(io.BytesIO(out)) as im:
            w, h = im.size
        if w < IMG_MIN_W or h < IMG_MIN_H or w / h > 6 or h / w > 6:
            return None
        return web_admin.store_image(media, out, name, thumb)
    except (Blocked, web_admin.Invalid, OSError) as e:
        log.info("原文圖片沒帶回來 %s：%s", url, e)
        return None


def build(url: str, html_text: Optional[str], site: str, category: str, with_images: bool, actor: str,
          chat: Optional[Callable[..., str]] = None, conn: Optional[Callable[[], Any]] = None,
          media: Optional[Path] = None) -> Dict[str, Any]:
    """整篇做成專欄草稿（同步執行，給背景執行緒用）。回 {post_id, title, warnings, images, skipped}。"""
    chat = chat or fb_drafts._chat
    conn = conn or fb_drafts._conn
    media = media or Path(config.WEB["media_dir"])
    if html_text is None:
        _step("讀取原文網頁…")
        data, ctype, final = fetch(url, PAGE_MAX, "text/html,application/xhtml+xml")
        if "html" not in ctype.lower() and not data.lstrip()[:200].lower().startswith((b"<!doctype", b"<html")):
            raise Blocked("這個網址不是網頁（可能是 PDF 或圖片），請改貼原文內容。")
        html_text, base = decode_html(data, ctype), final
    else:
        base = url
    page = extract(html_text, base)
    bl = page["blocks"]
    text_len = sum(len(b.get("text") or "") for b in bl)
    if sum(_weight(b.get("text") or "") for b in bl) < 80:
        raise Blocked("找不到文章內文（可能要登入、或內容是程式載入的）。請在瀏覽器選取全文複製，貼到下面的方框。")
    if text_len > SOURCE_MAX:
        raise Blocked(f"原文太長（約 {text_len} 字），請只複製要翻的那一段貼上。")
    site = (site or page["site"]).strip()[:web_admin.LIMITS["source_site"]]
    zh = is_chinese("\n".join(b.get("text") or "" for b in bl))

    # 圖片：先存起來（存不了的就不放）；原文最前面的大圖（或 og:image）當封面
    imgs: Dict[int, str] = {}
    cover = None
    skipped = 0
    if with_images:
        first_text = next((i for i, b in enumerate(bl) if b["t"] in ("p", "li")), len(bl))
        todo = [i for i, b in enumerate(bl) if b["t"] == "img"][:IMAGES_MAX]
        skipped += sum(1 for b in bl if b["t"] == "img") - len(todo)
        for n, i in enumerate(todo, 1):
            _step(f"帶回圖片 {n}/{len(todo)}…")
            name = _save_img(media, bl[i]["src"])
            if name and name not in imgs.values():
                imgs[i] = name
            elif not name:
                skipped += 1
        hero = next((i for i in sorted(imgs) if i < first_text), None)
        if hero is not None:
            cover = imgs.pop(hero)
            web_admin.ensure_thumb(media, cover)
        elif page["og_image"]:
            cover = _save_img(media, page["og_image"], thumb=True)
            if cover in imgs.values():                      # 主圖在內文裡：封面就用它，內文那張拿掉
                imgs = {i: n for i, n in imgs.items() if n != cover}
    keep = [i for i, b in enumerate(bl) if b["t"] != "img" or i in imgs]
    bl2 = [bl[i] for i in keep]
    idx_map = {old: new for new, old in enumerate(keep)}
    imgs = {idx_map[i]: n for i, n in imgs.items()}

    # 翻譯（中文原文不送 AI：只轉成台灣繁體用語）
    credit = credit_text(site)
    if zh:
        _step("轉成台灣繁體用語…")
        title = fix_zh(page["title"])[:web_admin.LIMITS["title"]]
        body = join_md([(b["t"], image_md(imgs[i], b.get("alt", ""), credit) if b["t"] == "img" else fix_zh(block_md(b)))
                        for i, b in enumerate(bl2)])
    else:
        groups = chunks(bl2)
        parts = []
        for n, g in enumerate(groups, 1):
            _step(f"翻譯第 {n}/{len(groups)} 段…", done=n - 1, total=len(groups))
            src, gi = chunk_source(bl2, g)
            parts.append(drop_finder(polite(fix_zh(translate_chunk(src, gi, chat)))))
        _step("翻譯標題和圖片說明…", done=len(groups), total=len(groups))
        order = sorted(imgs)
        title, alts = translate_meta(page["title"], [bl2[i].get("alt", "") for i in order], chat)
        title = polite(title)
        alt_of = dict(zip(order, alts))
        body = "\n\n".join(parts)
        body = _PH_RE.sub(lambda m: image_md(imgs[int(m.group(1))], alt_of.get(int(m.group(1)), ""), credit)
                          if int(m.group(1)) in imgs else "", body)
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if not title:
        raise RuntimeError("翻不出標題")

    _step("擬摘要和搜尋關鍵字…")
    try:
        seo = fb_drafts.seo_meta(_IMG_MD_RE.sub("", body), category)
    except Exception as e:  # noqa: BLE001 — 摘要擬不出來不影響草稿
        log.warning("翻譯文章的摘要沒擬出來：%s", e)
        seo = {}
    summary = polite(seo.get("summary") or "")[:web_admin.LIMITS["summary"]]
    warns = warnings_of(title + "\n" + summary + "\n" + body)

    _step("建立草稿…")
    with conn() as db, db.cursor() as cur:
        slug = fb_drafts._free_slug(cur, seo["slug"]) if seo.get("slug") else ""
        clean = web_admin.clean_post("article", {
            "kind": "article", "status": "draft", "title": title, "summary": summary, "category": category,
            "keywords": seo.get("keywords") or [], "slug": slug, "body_md": body[:web_admin.LIMITS["body_md"]],
            "source_url": url, "source_title": page["title"][:web_admin.LIMITS["source_title"]], "source_site": site,
            "source_mode": "repost" if zh else "translate"})
        pid = web_admin.save_post(cur, clean, actor)
        if cover:
            cur.execute(f"UPDATE {web_admin.TABLE} SET cover=%s, extra=JSON_SET(COALESCE(extra, JSON_OBJECT()), "
                        "'$.cover_src', 'source', '$.cover_at', %s, '$.cover_credit', CAST(%s AS JSON)) WHERE id=%s",
                        (cover, time.strftime("%Y-%m-%d %H:%M"),
                         json.dumps({"source": site, "url": url}, ensure_ascii=False), pid))
    log.info("翻譯文章 %s：#%s「%s」圖 %d 張（沒帶回 %d）提醒 %d", actor, pid, title, len(imgs) + bool(cover),
             skipped, len(warns))
    return {"post_id": pid, "title": title, "warnings": warns, "images": len(imgs) + bool(cover),
            "skipped": skipped, "mode": "repost" if zh else "translate"}


def start(url: str, html_text: Optional[str], site: str, category: str, with_images: bool, actor: str) -> bool:
    """背景翻譯一篇；已經有一篇在翻就回 False。參數先用 check_url 等檢查過。"""
    if not _lock.acquire(blocking=False):
        return False

    def _run() -> None:
        try:
            r = build(url, html_text, site, category, with_images, actor)
            status.update(running=False, step="完成", result=r, error=None, finished_at=time.strftime("%H:%M"))
        except (Blocked, web_admin.Invalid) as e:
            status.update(running=False, step="", result=None, error=str(e), blocked=isinstance(e, Blocked),
                          finished_at=time.strftime("%H:%M"))
        except Exception:  # noqa: BLE001
            log.exception("翻譯文章失敗 %s", url)
            status.update(running=False, step="", result=None, error="翻譯時出錯了（本機 AI 可能忙線或沒回應），請稍後再試。",
                          blocked=False, finished_at=time.strftime("%H:%M"))
        finally:
            _lock.release()

    status.clear()
    status.update(running=True, url=url, step="準備中…", done=0, total=0, result=None, error=None, blocked=False,
                  by=actor, started_at=time.strftime("%H:%M"))
    threading.Thread(target=_run, daemon=True, name="article-import").start()
    return True
