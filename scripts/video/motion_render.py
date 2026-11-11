#!/usr/bin/env python
"""
motion_render.py — 文章動態影片的畫面和聲音（y1crm api/motion_video.py 準備好 spec.json 之後呼叫）。
John 2026-11-08：「做生動一點的動態影片（不要純投影片的）」。要在有 torch 的環境跑（借 config.IMAGEGEN.python），
呼叫端用 CUDA_VISIBLE_DEVICES 指定好卡 0002（573 不做影像）。

    python -I motion_render.py spec.json

- 照片：Depth Anything V2 Small（Apache-2.0）估每張照片的深度 → GPU 照深度做視差運鏡：近的動得多、遠的動得少，
  看起來像攝影機在移動。畫面本身沒有用 AI 生成或改人物（真實照片、不用標 AI）。
- 圖解：片頭的嗶嗶聲、回饋音怎麼來、耳塞尺寸，都是程式畫的向量動畫（兩倍大畫再縮小，邊緣才平滑）。
- 字幕：照語音服務給的每個詞的時間，一個詞一個詞跳出來，正在講的詞是金色。
- 聲音：旁白＋程式合成的音效（嗶嗶、轉場的咻、圖示出現的啵），沒有外部音樂。
- 輸出 out_dir/wide.mp4（16:9，官網、YouTube）、tall.mp4（9:16，Reels、Shorts）和同名 .jpg 封面。
  第一格就是片頭畫面（Facebook Reels 拿第一格當封面，11-08 #42 從黑畫面淡入，封面一片黑）。
"""
import json
import math
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image, ImageDraw, ImageFilter, ImageFont

SR = 44100
MAX_WARP = 0.25          # 視差造成的局部變形上限：0.25＝一個像素最多被拉成 1.25 個、或和隔壁錯開 0.25 個（見 Shot.amount）
DEPTH_SMOOTH = 20        # 深度圖平滑的視窗＝照片寬 / DEPTH_SMOOTH：輪廓的深度差分散開，同樣的變形上限可以留比較多視差（11-09 從 60 改）
GOLD, WHITE = (255, 214, 102), (255, 255, 255)
BLUE, ORANGE, RED, GREEN = (86, 170, 255), (255, 150, 60), (235, 70, 60), (70, 200, 120)
SKIN, SKIN_D = (246, 214, 196), (205, 150, 128)
SPEC: dict = {}
_fonts: dict = {}


def font(size: int) -> ImageFont.FreeTypeFont:
    if size not in _fonts:
        _fonts[size] = ImageFont.truetype(SPEC["font"], size, index=SPEC["font_index"])
    return _fonts[size]


def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def ease_out(x):
    x = clamp(x)
    return 1 - (1 - x) ** 3


def ease_in_out(x):
    x = clamp(x)
    return x * x * (3 - 2 * x)


def back_out(x):
    """彈出：衝過頭一點再回來。"""
    x = clamp(x)
    c = 1.70158
    return 1 + (c + 1) * (x - 1) ** 3 + c * (x - 1) ** 2


def wrap(text, f, width):
    lines, cur = [], ""
    for ch in text:
        if f.getlength(cur + ch) > width and cur:
            if ch in "，。、！？；：」）":
                cur, ch = cur[:-1], cur[-1] + ch
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    return lines + ([cur] if cur else [])


# ── 版面 ─────────────────────────────────────────────────────
#   wide：圖解的舞台在中間 820 見方，字幕在下方；tall：上方標題，舞台 940 見方，字幕在 Reels 下方文字之上、不到右側按鈕
LAYOUT = {
    "wide": {"size": (1920, 1080), "stage": (550, 30, 820), "photo": (0, 0, 1920, 1080),
             "sub": {"top": 1040, "anchor": "bottom", "width": 1500, "font": 54, "lh": 76, "rows": 2},
             "label": (60, 56), "header": None},
    "tall": {"size": (1080, 1920), "stage": (70, 430, 940), "photo": (0, 350, 1080, 900),   # 舞台往下讓出左上的標籤
             "sub": {"top": 1290, "bottom": 1540, "anchor": "top", "width": 780, "font": 54, "lh": 76, "rows": 4},
             "label": (40, 380), "header": (215, 292)},
}


# ── GPU：照片＋深度 → 運鏡 ─────────────────────────────────────
class Shot:
    """一張照片（或模糊的底圖）放在 GPU 上；render() 依時間做推近／平移，有深度的話近景遠景位移不同（視差）。"""

    def __init__(self, img: Image.Image, dev, depth=None):
        self.w0, self.h0 = img.size
        self.img = torch.from_numpy(np.asarray(img.convert("RGB"))).to(dev).permute(2, 0, 1)[None].float().div(255)
        self.depth = depth
        self.dev = dev
        self._grid = {}
        self._amount = {}

    def _base(self, w, h):
        if (w, h) not in self._grid:
            ys, xs = torch.meshgrid(torch.linspace(0, 1, h, device=self.dev), torch.linspace(0, 1, w, device=self.dev),
                                    indexing="ij")
            self._grid[(w, h)] = (xs, ys)
        return self._grid[(w, h)]

    def crop(self, w, h, focus, z):
        """輸出 w×h、放大 z 倍時，來源照片上（0–1 座標）看得到的範圍：中心和寬高。"""
        ar_s, ar_o = self.w0 / self.h0, w / h
        cw, ch = (1.0, ar_s / ar_o) if ar_o > ar_s else (ar_o / ar_s, 1.0)
        cw, ch = cw / z, ch / z
        cx = clamp(focus[0], cw / 2, 1 - cw / 2)
        cy = clamp(focus[1], ch / 2, 1 - ch / 2)
        return cx, cy, cw, ch

    def camera(self, t, move):
        """t＝這段的進度 0–1 → (放大倍數, 左右移動 -1–1)。"""
        e = ease_in_out(t)
        return {"in": (1.05 + 0.13 * e, 0.0), "out": (1.18 - 0.13 * e, 0.0),
                "right": (1.12, -1 + 2 * e), "left": (1.12, 1 - 2 * e)}.get(move, (1.05 + 0.08 * e, 0.0))

    def _sample(self, w, h, t, focus, move, strength, amount):
        """輸出每個像素取照片上哪一點（0–1 座標）。amount＝視差打幾折（0＝單純推近／平移）。"""
        z, s = self.camera(t, move)
        cx, cy, cw, ch = self.crop(w, h, focus, z)
        xs, ys = self._base(w, h)
        gx = cx + (xs - 0.5) * cw
        gy = cy + (ys - 0.5) * ch
        if self.depth is not None and amount > 0:
            grid0 = torch.stack([gx * 2 - 1, gy * 2 - 1], -1)[None]
            d = F.grid_sample(self.depth, grid0, mode="bilinear", padding_mode="border", align_corners=False)[0, 0]
            fx, fy = focus
            df = F.grid_sample(self.depth, torch.tensor([[[[fx * 2 - 1, fy * 2 - 1]]]], device=self.dev),
                               mode="bilinear", align_corners=False).item()
            rel = (d - df) * amount                              # 比焦點近是正的
            gx = gx - strength * s * rel * cw                     # 左右移動：近的移得多
            k = 0.55 * (z - 1.05) * rel                           # 推近：近的放大得多
            gx = cx + (gx - cx) / (1 + k)
            gy = cy + (gy - cy) / (1 + k)
        return gx, gy, (cx, cy, cw, ch)

    def amount(self, w, h, focus, move, strength):
        """這個鏡頭的視差要打幾折。人物輪廓（臉、手）前後景深度差很多，位移差太大會把輪廓拉長、拖影
        （11-09 #106 試做：阿伯的臉、驗配師的手指被拉花）。整段運鏡抽幾個時間點、縮小算畫面每一處的局部變形
        （拉伸、壓扁、錯位），最大的超過 MAX_WARP 就照比例把視差調小；整段同一個折數，畫面才不會忽強忽弱。"""
        key = (w, h, tuple(focus), move, strength)
        if key not in self._amount:
            sw, sh = max(32, w // 4), max(32, h // 4)
            worst = 0.0
            for t in (0.0, 0.25, 0.5, 0.75, 1.0):
                gx, gy, (_, _, cw, ch) = self._sample(sw, sh, t, focus, move, strength, 1.0)
                ux, uy = cw / (sw - 1), ch / (sh - 1)           # 沒有視差時，相鄰像素在照片上差多少
                parts = ((gx[:, 1:] - gx[:, :-1]) / ux - 1, (gy[1:] - gy[:-1]) / uy - 1,   # 拉伸／壓扁
                         (gx[1:] - gx[:-1]) / ux, (gy[:, 1:] - gy[:, :-1]) / uy)            # 錯位
                worst = max([worst] + [p.abs().max().item() for p in parts])
            self._amount[key] = min(1.0, MAX_WARP / worst) if worst > 0 else 1.0
            print(json.dumps({"event": "warp", "w": w, "move": move, "worst": round(worst, 3),
                              "amount": round(self._amount[key], 3)}), file=sys.stderr, flush=True)
        return self._amount[key]

    def render(self, w, h, t, focus, move, strength=0.045):
        a = self.amount(w, h, focus, move, strength) if self.depth is not None else 0.0
        gx, gy, (cx, cy, cw, ch) = self._sample(w, h, t, focus, move, strength, a)
        grid = torch.stack([gx * 2 - 1, gy * 2 - 1], -1)[None]
        out = F.grid_sample(self.img, grid, mode="bilinear", padding_mode="border", align_corners=False)
        arr = out[0].clamp(0, 1).mul(255).byte().permute(1, 2, 0).contiguous().cpu().numpy()
        return Image.fromarray(arr, "RGB"), (cx, cy, cw, ch)


def to_out(pt, view, w, h):
    """來源照片上的一點 → 輸出畫面座標（不算視差；標記都放在焦點附近，視差幾乎是 0）。"""
    cx, cy, cw, ch = view
    return ((pt[0] - cx) / cw + 0.5) * w, ((pt[1] - cy) / ch + 0.5) * h


def load_depth_model(dev):
    """只載模型。前處理自己做（照 preprocessor_config.json）：transformers 的影像前處理要 torchvision，
    借用的環境沒有裝，也不去動別人的環境。"""
    from transformers import AutoModelForDepthEstimation
    return None, AutoModelForDepthEstimation.from_pretrained(SPEC["depth_model"]).to(dev).eval()


def depth_input(img: Image.Image, dev):
    """DPT 前處理：短邊縮到 518（保持比例、14 的倍數）、bicubic、0–1、ImageNet 平均／標準差。"""
    w, h = img.size
    sc = 518 / min(w, h)
    nw, nh = max(14, round(w * sc / 14) * 14), max(14, round(h * sc / 14) * 14)
    x = np.asarray(img.convert("RGB").resize((nw, nh), Image.BICUBIC), np.float32) / 255
    x = (x - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
    return torch.from_numpy(x).permute(2, 0, 1)[None].to(dev)


def estimate_depth(img: Image.Image, proc, model, dev):
    """相對深度 0（遠）–1（近），平滑過（邊緣不撕裂），形狀 1×1×H×W、在 GPU 上。"""
    with torch.no_grad():
        pd = model(pixel_values=depth_input(img, dev)).predicted_depth
    d = F.interpolate(pd[:, None], size=(img.size[1], img.size[0]), mode="bicubic", align_corners=False)
    flat = d.flatten()[::53]
    lo, hi = torch.quantile(flat.float(), 0.02), torch.quantile(flat.float(), 0.98)
    d = ((d - lo) / (hi - lo + 1e-6)).clamp(0, 1)
    k = max(5, img.size[0] // DEPTH_SMOOTH) | 1
    for _ in range(3):
        d = F.avg_pool2d(d, k, stride=1, padding=k // 2, count_include_pad=False)
    return d


def backdrop_img(img: Image.Image) -> Image.Image:
    small = img.convert("RGB").resize((img.size[0] // 4, img.size[1] // 4), Image.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(10))
    small = Image.blend(small, Image.new("RGB", small.size, (18, 26, 40)), 0.58)
    return small.resize(img.size, Image.BILINEAR)


# ── 圖解（舞台座標 0–1，兩倍大畫）─────────────────────────────
class Stage:
    def __init__(self, side):
        self.S = side * 2
        self.im = Image.new("RGBA", (self.S, self.S), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.im)

    def P(self, x, y):
        return x * self.S, y * self.S

    def L(self, v):
        return v * self.S

    def done(self, side):
        return self.im.resize((side, side), Image.LANCZOS)

    def text(self, x, y, s, size, fill, anchor="mm", alpha=1.0, stroke=0):
        if alpha <= 0:
            return
        f = font(int(self.L(size)))
        col = fill + (int(255 * alpha),)
        self.d.text(self.P(x, y), s, font=f, fill=col, anchor=anchor,
                    stroke_width=int(self.L(stroke)), stroke_fill=(20, 26, 36, int(255 * alpha)))

    def text_fit(self, x, y, s, size, fill, maxw, anchor="mm", alpha=1.0, stroke=0):
        """字太長就縮小到 maxw（舞台寬度的比例）以內。"""
        w = font(int(self.L(size))).getlength(s) / self.S
        self.text(x, y, s, size * min(1.0, maxw / w) if w else size, fill, anchor, alpha, stroke)

    def arc(self, x, y, r, a0, a1, col, w, alpha=1.0):
        if alpha <= 0 or r <= 0:
            return
        X, Y, R = *self.P(x, y), self.L(r)
        self.d.arc((X - R, Y - R, X + R, Y + R), a0, a1, fill=col + (int(255 * alpha),), width=max(1, int(self.L(w))))

    def circle(self, x, y, r, fill=None, outline=None, w=0.0, alpha=1.0):
        X, Y, R = *self.P(x, y), self.L(r)
        a = int(255 * alpha)
        self.d.ellipse((X - R, Y - R, X + R, Y + R), fill=fill + (a,) if fill else None,
                       outline=outline + (a,) if outline else None, width=max(0, int(self.L(w))))

    def ellipse(self, x, y, rx, ry, fill=None, outline=None, w=0.0, alpha=1.0):
        X, Y = self.P(x, y)
        a = int(255 * alpha)
        self.d.ellipse((X - self.L(rx), Y - self.L(ry), X + self.L(rx), Y + self.L(ry)),
                       fill=fill + (a,) if fill else None, outline=outline + (a,) if outline else None,
                       width=max(0, int(self.L(w))))

    def line(self, pts, col, w, alpha=1.0):
        self.d.line([self.P(*p) for p in pts], fill=col + (int(255 * alpha),), width=max(1, int(self.L(w))),
                    joint="curve")

    def poly(self, pts, fill, outline=None, w=0.0, alpha=1.0):
        a = int(255 * alpha)
        self.d.polygon([self.P(*p) for p in pts], fill=fill + (a,), outline=outline + (a,) if outline else None,
                       width=max(0, int(self.L(w))))

    def rrect(self, x0, y0, x1, y1, r, fill, outline=None, w=0.0, alpha=1.0):
        a = int(255 * alpha)
        self.d.rounded_rectangle((*self.P(x0, y0), *self.P(x1, y1)), radius=self.L(r), fill=fill + (a,),
                                 outline=outline + (a,) if outline else None, width=max(0, int(self.L(w))))


def bezier(p0, p1, p2, n=24):
    return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
             (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1]) for t in (i / n for i in range(n + 1))]


def capsule(cx, cy, length, r, ang, n=14):
    """膠囊形（耳掛型助聽器的機身）的多邊形頂點。"""
    pts = []
    ca, sa = math.cos(ang), math.sin(ang)
    for end, sgn in ((length / 2, 1), (-length / 2, -1)):
        for i in range(n + 1):
            a = -math.pi / 2 + math.pi * i / n
            px, py = end + sgn * r * math.cos(a), sgn * r * math.sin(a)
            pts.append((cx + px * ca - py * sa, cy + px * sa + py * ca))
    return pts


def hearing_aid(st: Stage, x, y, sc, wobble=0.0, alpha=1.0):
    """耳掛型助聽器圖示：機身＋細線＋耳塞。回傳耳塞（出聲音的地方）和麥克風的位置。"""
    ang = math.radians(-58 + wobble)
    body = capsule(x, y, 0.26 * sc, 0.055 * sc, ang)
    st.poly(body, (222, 226, 232), (120, 130, 142), 0.006 * sc, alpha)
    top = (x + 0.13 * sc * math.cos(ang), y + 0.13 * sc * math.sin(ang))
    mic = (x + 0.09 * sc * math.cos(ang), y + 0.09 * sc * math.sin(ang))
    st.circle(*mic, 0.012 * sc, (70, 80, 92), alpha=alpha)
    dome = (x + 0.24 * sc, y + 0.17 * sc)
    st.line(bezier(top, (x + 0.30 * sc, y - 0.20 * sc), dome), (150, 160, 172), 0.012 * sc, alpha)
    st.circle(*dome, 0.035 * sc, (90, 150, 220), (40, 90, 160), 0.005 * sc, alpha)
    return dome, mic


def bubble(st: Stage, x, y, s, txt, pop, col=RED):
    """「嗶！」對話框，pop＝0–1 彈出進度。"""
    k = back_out(pop)
    if k <= 0.01:
        return
    w, h = 0.16 * s * k, 0.09 * s * k
    st.rrect(x - w / 2, y - h / 2, x + w / 2, y + h / 2, 0.03 * s * k, WHITE, col, 0.006 * s)
    st.poly([(x - 0.02 * s * k, y + h / 2 - 0.002), (x - 0.045 * s * k, y + h / 2 + 0.035 * s * k),
             (x + 0.01 * s * k, y + h / 2 - 0.002)], WHITE)
    st.text(x, y, txt, 0.055 * s * k, col)


def word_time(sc, needle, default):
    """這段旁白裡某個詞開始的時間（秒，從這段開頭算）；找不到用 default。"""
    for t0, _, w in sc["words"]:
        if needle in w:
            return sc["lead"] + t0
    return default


def draw_title(st: Stage, sc, t):
    beeps = [b for kind, b in sc["sfx"] if kind == "beep2"]
    shake = sum(math.sin((t - b) * 70) * 4 * max(0, 1 - (t - b) / 0.35) for b in beeps if 0 <= t - b)
    dome, _ = hearing_aid(st, 0.36, 0.30, 1.0, shake)
    for b in beeps:                                    # 每次嗶：耳塞往外擴散的聲波
        for k in range(3):
            p = (t - b - k * 0.09) / 0.7
            if 0 <= p <= 1:
                st.arc(dome[0], dome[1], 0.06 + 0.22 * p, -40, 40, RED, 0.016, 1 - p)
    for i, b in enumerate(beeps):
        bubble(st, 0.70 + 0.07 * (i % 2), 0.12 + 0.14 * (i % 2), 1.0, "嗶！", (t - b) / 0.25)
    # 開頭的問句第一格就要看得到（Reels 拿第一格當封面；John 11-08：開頭要有足夠吸引人的字眼），只做一點點往上飄
    for i, row in enumerate(SPEC["title"]):
        drift = (1 - ease_out(t / 0.8)) * 0.015
        st.text(0.5, 0.66 + i * 0.135 + drift, row, 0.105 if i == 0 else 0.085, WHITE if i == 0 else GOLD, stroke=0.006)


def draw_feedback(st: Stage, sc, t):
    """回饋音：耳塞送進耳道的聲音（藍）從縫隙漏出來（橘）→ 被麥克風收進去 → 嗶（紅）。"""
    t_leak = word_time(sc, "漏", sc["dur"] * 0.35)
    t_beep = word_time(sc, "嗶", sc["dur"] * 0.8)
    ap = ease_out(t / 0.4)
    # 耳朵輪廓、耳道
    st.arc(0.40, 0.55, 0.30, 100, 260, SKIN_D, 0.035, ap)
    st.rrect(0.42, 0.48, 0.93, 0.62, 0.07, SKIN, SKIN_D, 0.006, ap)
    st.ellipse(0.925, 0.55, 0.018, 0.07, (238, 186, 166), SKIN_D, 0.004, ap)
    st.text(0.70, 0.69, "耳道", 0.055, WHITE, alpha=ap, stroke=0.004)
    # 助聽器：機身在耳後（左上），細線接到耳道口的耳塞
    body = capsule(0.17, 0.26, 0.22, 0.05, math.radians(-70))
    st.poly(body, (222, 226, 232), (120, 130, 142), 0.006, ap)
    mic = (0.205, 0.155)
    hot = clamp((t - t_beep) / 0.2) if t > t_beep else 0
    st.circle(*mic, 0.016 + 0.01 * hot * (0.5 + 0.5 * math.sin(t * 40)), RED if hot else (70, 80, 92), alpha=ap)
    st.line(bezier((0.215, 0.16), (0.46, 0.10), (0.455, 0.53)), (150, 160, 172), 0.01, ap)
    st.rrect(0.43, 0.515, 0.48, 0.585, 0.02, (90, 150, 220), (40, 90, 160), 0.004, ap)
    st.text(0.21, 0.06, "麥克風", 0.055, WHITE, alpha=ap * ease_out((t - 0.3) / 0.4), stroke=0.004)
    # 送進耳道的聲音（一直有）
    for k in range(5):
        p = ((t * 0.9 + k / 5) % 1)
        st.arc(0.47 + 0.42 * p, 0.55, 0.05, -45, 45, BLUE, 0.012, ap * (1 - p) * 0.9)
    # 從縫隙漏出來，沿著曲線回到麥克風
    if t > t_leak:
        path = bezier((0.43, 0.49), (0.36, 0.18), mic, 40)
        speed = 0.55 if t < t_beep else 1.1
        for k in range(6):
            p = ((t - t_leak) * speed - k / 6)
            if p < 0:
                continue
            p %= 1
            x, y = path[int(p * (len(path) - 1))]
            col = RED if t > t_beep else ORANGE
            st.circle(x, y, 0.016, col, alpha=min(1, (t - t_leak) / 0.3) * (1 - 0.3 * p))
        st.text(0.27, 0.41, "漏出來", 0.055, ORANGE, alpha=ease_out((t - t_leak) / 0.4), stroke=0.004)
    if t > t_beep:
        bubble(st, 0.43, 0.24, 1.25, "嗶～！", (t - t_beep) / 0.3)


def draw_domes(st: Stage, sc, t):
    """耳塞尺寸：太小（縫隙漏聲）、剛好（打勾）、太大（擠歪、縫隙漏聲）。"""
    t_small = word_time(sc, "小", 0.6)
    t_big = word_time(sc, "大", 1.4)
    t_leak = word_time(sc, "漏", sc["dur"] * 0.6)
    cols = [(0.2, "太小", ORANGE, t_small), (0.5, "剛好", GREEN, t_leak + 0.5), (0.8, "太大", ORANGE, t_big)]
    for i, (x, name, col, t_on) in enumerate(cols):
        p = back_out((t - 0.2 - i * 0.22) / 0.45)
        if p <= 0.01:
            continue
        y = 0.40
        st.circle(x, y, 0.135 * p, (60, 44, 40), SKIN_D, 0.014)          # 耳道口
        if i == 0:
            st.circle(x, y, 0.072 * p, (120, 170, 230), (40, 90, 160), 0.007)
        elif i == 1:
            st.circle(x, y, 0.126 * p, (120, 170, 230), (40, 90, 160), 0.007)
        else:
            st.ellipse(x, y + 0.01, 0.175 * p, 0.09 * p, (120, 170, 230), (40, 90, 160), 0.007)
            st.line([(x - 0.13 * p, y + 0.01), (x + 0.13 * p, y + 0.01)], (40, 90, 160), 0.007)
        q = ease_out((t - t_on) / 0.4)
        st.text(x, 0.66, name, 0.08, col, alpha=min(p, 1) * (0.35 + 0.65 * q), stroke=0.005)
        if i != 1 and t > t_leak:                   # 縫隙漏聲
            for k in range(3):
                r = ((t - t_leak) * 0.8 + k / 3) % 1
                st.arc(x, y, 0.13 + 0.12 * r, 200, 340, ORANGE, 0.01, 1 - r)
                st.arc(x, y, 0.13 + 0.12 * r, 20, 160, ORANGE, 0.01, (1 - r) * 0.6)
        if i == 1 and t > t_leak + 0.5:              # 打勾：右上角的綠色圓章，勾一筆一筆畫出來
            c = ease_out((t - t_leak - 0.5) / 0.5)
            bx, by = x + 0.10, y - 0.11
            st.circle(bx, by, 0.055 * back_out(c * 1.4), GREEN)
            pts = [(bx - 0.028, by + 0.002), (bx - 0.006, by + 0.024), (bx + 0.03, by - 0.022)]
            seg = c * 2
            line = [pts[0], (pts[0][0] + (pts[1][0] - pts[0][0]) * min(1, seg), pts[0][1] + (pts[1][1] - pts[0][1]) * min(1, seg))]
            if seg > 1:
                line.append((pts[1][0] + (pts[2][0] - pts[1][0]) * (seg - 1), pts[1][1] + (pts[2][1] - pts[1][1]) * (seg - 1)))
            st.line(line, WHITE, 0.013)


def fit(text, size, maxw):
    """字太長就縮小到 maxw 像素以內。"""
    f = font(size)
    w = f.getlength(text)
    return font(max(16, int(size * maxw / w))) if w > maxw else f


# 通用片頭、重點清單直接畫在整個畫面上（不擠在圖解的方形舞台裡），橫式、直式各有自己的大小
HOOK = {"wide": {"y": 440, "f1": 128, "f2": 80, "maxw": 1640}, "tall": {"y": 760, "f1": 104, "f2": 68, "maxw": 960}}
POINTS = {"wide": {"x0": 360, "x1": 1560, "yc": 470, "h": 122, "gap": 156, "font": 58},
          "tall": {"x0": 60, "x1": 1020, "yc": 830, "h": 114, "gap": 144, "font": 50}}


def draw_hook(d, fmt, sc, t):
    """通用片頭：照片運鏡（變暗）上的大字問句。第一格就看得到（Reels 拿第一格當封面），底線慢慢拉長。"""
    W, g = LAYOUT[fmt]["size"][0], HOOK[fmt]
    rows = SPEC["title"]
    y = g["y"] - (len(rows) - 1) * 50 + (1 - ease_out(t / 0.8)) * 14
    for i, row in enumerate(rows):
        f = fit(row, g["f1"] if i == 0 else g["f2"], g["maxw"])
        d.text((W / 2, y), row, font=f, fill=(WHITE if i == 0 else GOLD) + (255,), anchor="mm",
               stroke_width=5 if i == 0 else 3, stroke_fill=(20, 26, 36))
        if i == 0:
            uw = (0.3 + 0.5 * ease_out(t / 0.8)) * f.getlength(row)
            uy = y + f.size * 0.66
            d.rounded_rectangle((W / 2 - uw / 2, uy, W / 2 + uw / 2, uy + 12), 6, fill=GOLD + (255,))
            y += f.size * 1.45
        else:
            y += f.size * 1.3
    d.text((W / 2, y + 20), SPEC["brand"], font=font(42 if fmt == "wide" else 36),
           fill=WHITE + (int(255 * ease_out((t - 0.3) / 0.5)),), anchor="mm", stroke_width=2, stroke_fill=(20, 26, 36))


def point_times(sc):
    """重點清單：第 i 點什麼時候出現（照旁白的進度平均分配，從這段開頭算）。"""
    items = sc.get("items") or []
    words = sc.get("words") or []
    speech = words[-1][1] if words else max(1.0, sc["dur"] - sc["lead"] - 1)
    return [sc["lead"] + speech * 0.88 * i / max(1, len(items)) for i in range(len(items))]


def draw_points(d, fmt, sc, t):
    """重點清單：白色卡片一條一條從右邊彈進來，前面是金色編號。"""
    g = POINTS[fmt]
    items = sc.get("items") or []
    y0 = g["yc"] - g["gap"] * (len(items) - 1) / 2
    for i, (txt, t_on) in enumerate(zip(items, point_times(sc))):
        p = back_out((t - t_on) / 0.45)
        if p <= 0.01:
            continue
        dx, y, h = (1 - min(1.0, p)) * 520, y0 + i * g["gap"], g["h"]
        d.rounded_rectangle((g["x0"] + dx, y - h / 2, g["x1"] + dx, y + h / 2), int(h / 2.2),
                            fill=(255, 255, 255, int(242 * min(1.0, p))))
        cx, r = g["x0"] + dx + h * 0.56, h * 0.33
        d.ellipse((cx - r, y - r, cx + r, y + r), fill=GOLD + (255,))
        d.text((cx, y), str(i + 1), font=font(int(g["font"] * 0.95)), fill=(29, 29, 29, 255), anchor="mm")
        d.text((cx + r + 28, y), txt, font=fit(txt, g["font"], g["x1"] - g["x0"] - h * 1.4), fill=(29, 29, 29, 255),
               anchor="lm")


GRAPHICS = {"title": draw_title, "feedback": draw_feedback, "domes": draw_domes}


# ── 疊字：字幕、重點標籤、上方標題、照片上的圈和箭頭、片尾 ──────────
def _layout_sub(sc, sub):
    """旁白排版：每個字屬於哪個詞（語音服務給的詞沒有標點，標點跟著前一個詞）。"""
    say, words = sc["say"], sc["words"]
    owner = [None] * len(say)
    idx = 0                                       # 依序把每個詞對到原文
    for k, (_, _, w) in enumerate(words):
        j = say.find(w, idx)
        if j < 0:
            continue
        for m in range(j, j + len(w)):
            owner[m] = k
        idx = j + len(w)
    last = 0
    for i in range(len(owner)):                   # 標點、對不到的字：跟著前一個詞
        if owner[i] is None:
            owner[i] = last
        last = owner[i]
    f = font(sub["font"])
    rows = wrap(say, f, sub["width"])[:sub["rows"]]
    out, i = [], 0
    for row in rows:
        out.append([(ch, owner[i + k] if i + k < len(owner) else last) for k, ch in enumerate(row)])
        i += len(row)
    return out


def draw_sub(d, fmt, sc, t, cache):
    sub = LAYOUT[fmt]["sub"]
    if "sub" not in cache:
        cache["sub"] = _layout_sub(sc, sub)
    rows = cache["sub"]
    words = sc["words"]
    if not words:
        return
    tv = t - sc["lead"]                       # 旁白開始後的時間
    if tv < words[0][0] - 0.06:
        return
    f, lh = font(sub["font"]), sub["lh"]
    W = LAYOUT[fmt]["size"][0]
    need = 44 + lh * len(rows)
    if sub["anchor"] == "bottom":
        top = sub["top"] - need
    else:
        top = sub["top"] if sub["top"] + need <= sub["bottom"] else sub["bottom"] - need
    box_a = ease_out((tv - words[0][0] + 0.06) / 0.14)
    widest = max(f.getlength("".join(c for c, _ in r)) for r in rows)
    bw = min(sub["width"], widest) + 80
    d.rounded_rectangle((W / 2 - bw / 2, top, W / 2 + bw / 2, top + need), 22, fill=(0, 0, 0, int(165 * box_a)))
    for ri, row in enumerate(rows):
        x = W / 2 - f.getlength("".join(c for c, _ in row)) / 2
        y = top + 22 + ri * lh
        for ch, k in row:
            t0, t1, _ = words[min(k, len(words) - 1)]
            p = clamp((tv - t0 + 0.06) / 0.14)
            if p > 0:
                cur = t0 - 0.06 <= tv < t1 + 0.12
                col = GOLD if cur else WHITE
                d.text((x, y + (1 - ease_out(p)) * 10), ch, font=f, fill=col + (int(255 * p),))
            x += f.getlength(ch)


def draw_label(d, fmt, sc, t):
    if not sc.get("label"):
        return
    x, y = LAYOUT[fmt]["label"]
    p = ease_out((t - 0.15) / 0.35)
    if p <= 0:
        return
    fl = font(44 if fmt == "wide" else 42)
    tw = fl.getlength(sc["label"])
    x0 = x - (1 - p) * (tw + 120)
    d.rounded_rectangle((x0, y, x0 + tw + 56, y + 78), 18, fill=(0, 0, 0, int(170 * p)))
    d.rounded_rectangle((x0, y, x0 + 10, y + 78), 5, fill=GOLD + (int(255 * p),))
    d.text((x0 + 30, y + 14), sc["label"], font=fl, fill=GOLD + (int(255 * p),))


def draw_header(d, fmt):
    h = LAYOUT[fmt]["header"]
    if not h:
        return
    W = LAYOUT[fmt]["size"][0]
    d.text((W / 2, h[0]), SPEC["header"], font=font(58), fill=WHITE, anchor="ma", stroke_width=2,
           stroke_fill=(20, 26, 36))
    d.text((W / 2, h[1]), SPEC["brand"], font=font(34), fill=GOLD, anchor="ma")


def draw_marks(d, sc, t, pt_out, scale):
    """照片上的標記：ring＝脈動的圈；arrow＝往目標推的箭頭。"""
    if sc.get("ring") and pt_out:
        x, y = pt_out
        p = ease_out((t - 0.5) / 0.5)
        r = sc["ring"][2] * scale * (1 + 0.08 * math.sin(t * 5))
        for k, a in ((0, 1.0), (1, 0.5)):
            rr = r * (1 + 0.25 * k + 0.25 * ((t * 0.8) % 1) * k)
            d.ellipse((x - rr, y - rr, x + rr, y + rr), outline=GOLD + (int(255 * p * a * (1 - 0.6 * k)),), width=8 - 3 * k)
    if sc.get("arrow") and pt_out:
        x, y = pt_out
        p = ease_out((t - 0.4) / 0.5)
        if p <= 0:
            return
        push = 0.5 + 0.5 * math.sin(t * 4)
        L = 0.12 * scale
        dx, dy = 0.7071, 0.7071                     # 從右下往左上推
        tip = (x + dx * (L * 0.25 + 18 * push), y + dy * (L * 0.25 + 18 * push))
        tail = (tip[0] + dx * L, tip[1] + dy * L)
        col = GOLD + (int(255 * p),)
        d.line((tail, tip), fill=col, width=14)
        hw = 34
        d.polygon([tip, (tip[0] + dx * hw - dy * hw * 0.8, tip[1] + dy * hw + dx * hw * 0.8),
                   (tip[0] + dx * hw + dy * hw * 0.8, tip[1] + dy * hw - dx * hw * 0.8)], fill=col)


def draw_end(d, fmt, sc, t):
    W, H = LAYOUT[fmt]["size"]
    cta, buttons = sc.get("cta") or [], sc.get("buttons") or []
    if fmt == "wide":
        cx, y0, bw = 1380, 210, 820
        d.rectangle((900, 0, W, H), fill=(14, 20, 32, int(150 * ease_out(t / 0.5))))
    else:
        cx, y0, bw = W / 2, 420, 880
        d.rounded_rectangle((60, 360, W - 60, 1240), 36, fill=(14, 20, 32, int(165 * ease_out(t / 0.5))))
    y = y0
    for i, row in enumerate(cta):
        p = ease_out((t - 0.2 - i * 0.25) / 0.5)
        f = font(76 if i == 0 else 50)
        d.text((cx, y + (1 - p) * 30), row, font=f, fill=(WHITE if i == 0 else GOLD) + (int(255 * p),), anchor="ma",
               stroke_width=2 if i == 0 else 0, stroke_fill=(20, 26, 36))
        y += 110 if i == 0 else 90
    y += 40
    for i, (txt, path) in enumerate(buttons):
        p = back_out((t - 0.9 - i * 0.3) / 0.45)
        if p > 0.01:
            h = 150 * p
            col = (220, 170, 16) if i == 0 else (6, 199, 85)
            d.rounded_rectangle((cx - bw / 2 * p, y + 75 - h / 2, cx + bw / 2 * p, y + 75 + h / 2), int(75 * p),
                                fill=col + (240,))
            if p > 0.6:
                a = int(255 * clamp((p - 0.6) / 0.4))
                ink = ((29, 29, 29) if i == 0 else WHITE) + (a,)
                d.text((cx, y + 22), txt, font=font(50), fill=ink, anchor="ma")
                d.text((cx, y + 88), SPEC["host"] + path, font=font(36), fill=ink, anchor="ma")
        y += 180                                    # 還沒彈出來的按鈕也要佔位，店名才不會跳
    d.text((cx, y + 30), SPEC["brand"], font=font(36), fill=GOLD + (int(255 * ease_out((t - 1.6) / 0.5)),), anchor="ma")


# ── 一格畫面 ──────────────────────────────────────────────────
def frame(fmt, sc, t, shots, cache):
    W, H = LAYOUT[fmt]["size"]
    kind, u = sc["kind"], t / sc["dur"]
    sh, back = shots[sc["photo"]], shots["back:" + sc["photo"]]
    pt_out, scale = None, W
    hook = kind == "title" and sc.get("icon") != "beep"       # 通用片頭：照片運鏡＋大字（嗶嗶聲那篇用助聽器圖示）
    if kind in ("photo", "end") or hook:
        if fmt == "wide":
            base, view = sh.render(W, H, u, sc["focus"], sc.get("move", "in"))
            if sc.get("ring") or sc.get("arrow"):
                pt_out = to_out((sc.get("ring") or sc["arrow"])[:2], view, W, H)
            scale = W / view[2]                          # 照片寬度 1 在畫面上幾個像素（圈的半徑跟著放大）
        else:
            px, py, pw, ph = LAYOUT[fmt]["photo"]
            base, _ = back.render(W, H, u, sc["focus"], "in")
            pic, view = sh.render(pw, ph, u, sc["focus"], sc.get("move", "in"))
            base.paste(pic, (px, py))
            if sc.get("ring") or sc.get("arrow"):
                ox, oy = to_out((sc.get("ring") or sc["arrow"])[:2], view, pw, ph)
                pt_out = (ox + px, oy + py)
            scale = pw / view[2]
    else:
        base, _ = back.render(W, H, u, (0.5, 0.5), "in")
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if hook:
        d.rectangle((0, 0, W, H), fill=(10, 16, 26, 125))     # 照片變暗，字才看得清楚
    if kind in GRAPHICS and not hook:
        sx, sy, side = LAYOUT[fmt]["stage"]
        st = Stage(side)
        GRAPHICS[kind](st, sc, t)
        layer.alpha_composite(st.done(side), (sx, sy))
    if hook:
        draw_hook(d, fmt, sc, t)
    if kind == "points":
        draw_points(d, fmt, sc, t)
    if kind == "end":
        draw_end(d, fmt, sc, t)
    if kind != "title" and kind != "end":
        draw_header(d, fmt)
    draw_marks(d, sc, t, pt_out, scale)
    draw_label(d, fmt, sc, t)
    draw_sub(d, fmt, sc, t, cache)
    out = base.convert("RGBA")
    out.alpha_composite(layer)
    return out.convert("RGB")


# ── 聲音 ─────────────────────────────────────────────────────
def tone(freq, sec, amp, sr=SR):
    n = int(sec * sr)
    t = np.arange(n) / sr
    env = np.minimum(1, np.minimum(t / 0.006, (sec - t) / 0.012))
    return (np.sin(2 * np.pi * freq * t) * env * amp).astype(np.float32)


def sfx(kind):
    if kind == "beep2":                              # 助聽器的嗶嗶聲（兩聲）
        b = tone(2700, 0.09, 0.16)
        return np.concatenate([b, np.zeros(int(0.07 * SR), np.float32), b])
    if kind == "pop":
        n = int(0.07 * SR)
        t = np.arange(n) / SR
        f = 500 + 900 * t / 0.07
        return (np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 45) * 0.12).astype(np.float32)
    if kind == "whoosh":                             # 轉場：濾波的雜訊，先強後弱
        n = int(0.42 * SR)
        rng = np.random.default_rng(7)
        x = rng.standard_normal(n).astype(np.float32)
        out = np.zeros(n, np.float32)
        y = 0.0
        for i in range(n):
            a = 0.02 + 0.25 * math.sin(math.pi * i / n)
            y += a * (x[i] - y)
            out[i] = y
        env = np.sin(np.linspace(0, math.pi, n)) ** 2
        return out * env * 0.32
    raise ValueError(kind)


def read_wav(path):
    with wave.open(path, "rb") as w:
        assert w.getframerate() == SR and w.getsampwidth() == 2
        data = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
        return data.reshape(-1, w.getnchannels()).mean(axis=1)


def mix_audio(scenes, path):
    total = int(round(sum(s["dur"] for s in scenes) * SR))
    buf = np.zeros(total + SR, np.float32)
    start = 0.0
    for i, sc in enumerate(scenes):
        v = read_wav(sc["voice"])
        o = int(round((start + sc["lead"]) * SR))
        buf[o:o + len(v)] += v
        for kind, at in sc["sfx"]:
            s = sfx(kind)
            o = int(round((start + at) * SR))
            buf[o:o + len(s)] += s
        start += sc["dur"]
        if i < len(scenes) - 1:                      # 轉場的咻：在換段前一點
            s = sfx("whoosh")
            o = max(0, int(round((start - 0.28) * SR)))
            buf[o:o + len(s)] += s
    buf = buf[:total]
    peak = float(np.abs(buf).max()) or 1.0
    if peak > 0.97:
        buf *= 0.97 / peak
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((buf * 32767).astype(np.int16).tobytes())


# ── 組起來 ────────────────────────────────────────────────────
def encode_proc(fmt, audio, out):
    W, H = LAYOUT[fmt]["size"]
    fps = SPEC["fps"]
    return subprocess.Popen(
        [SPEC["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-", "-i", audio,
         "-map", "0:v", "-map", "1:a", "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
         "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-profile:v", "high", "-level", "4.0",
         "-r", str(fps), "-g", str(fps * 2), "-colorspace", "bt709", "-color_primaries", "bt709",
         "-color_trc", "bt709", "-color_range", "tv", "-c:a", "aac", "-b:a", "128k", "-ar", str(SR), "-ac", "1",
         "-movflags", "+faststart", "-shortest", out], stdin=subprocess.PIPE)


def render_format(fmt, scenes, shots, audio, out_dir, only=None):
    fps = SPEC["fps"]
    proc = encode_proc(fmt, audio, str(out_dir / f"{fmt}.mp4"))
    N = int(0.3 * fps)                                # 轉場格數
    poster_done = False
    for i, sc in enumerate(scenes):
        n = int(round(sc["dur"] * fps))
        cache, nxt = {}, None
        for f in range(n):
            t = f / fps
            img = frame(fmt, sc, t, shots, cache)
            if i < len(scenes) - 1 and f >= n - N:   # 轉場：疊到下一段的第一格，帶一點放大
                if nxt is None:
                    nxt = frame(fmt, scenes[i + 1], 0.0, shots, {})
                a = ease_in_out((f - (n - N) + 1) / N)
                z = 1 + 0.04 * a
                W, H = img.size
                zi = img.resize((int(W * z), int(H * z)), Image.BILINEAR).crop(
                    (int(W * (z - 1) / 2), int(H * (z - 1) / 2), int(W * (z - 1) / 2) + W, int(H * (z - 1) / 2) + H))
                img = Image.blend(zi, nxt, a)
            if not poster_done and i == 0 and t >= sc.get("poster_at", 1.4):
                img.save(out_dir / f"{fmt}.jpg", quality=90)
                poster_done = True
            proc.stdin.write(img.tobytes())
        print(json.dumps({"fmt": fmt, "scene": i, "frames": n}), flush=True)
    proc.stdin.close()
    if proc.wait():
        raise RuntimeError(f"ffmpeg 失敗（{fmt}）")


def main(spec_path):
    SPEC.update(json.loads(Path(spec_path).read_text(encoding="utf-8")))
    out_dir = Path(SPEC["out_dir"])
    dev = torch.device("cuda")
    proc, model = load_depth_model(dev)
    shots = {}
    for sc in SPEC["scenes"]:
        p = sc["photo"]
        if p in shots:
            continue
        img = Image.open(p).convert("RGB")
        shots[p] = Shot(img, dev, estimate_depth(img, proc, model, dev))
        shots["back:" + p] = Shot(backdrop_img(img), dev)
    del model
    torch.cuda.empty_cache()
    print(json.dumps({"event": "depth", "photos": len(shots) // 2}), flush=True)
    audio = str(out_dir / "mix.wav")
    mix_audio(SPEC["scenes"], audio)
    for fmt in SPEC["formats"]:
        render_format(fmt, SPEC["scenes"], shots, audio, out_dir)
    print(json.dumps({"event": "done"}), flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
