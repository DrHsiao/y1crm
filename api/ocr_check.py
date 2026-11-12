"""客戶資料表 OCR 的合理性檢查（2026-11-13 john：OCR 把複姓「張廖」的「廖」讀成「聲」；「維持思考關閉，用輔助參數試試」）。

原則：**只標記、不修改**。模型照字形抄下來的值原封不動，看起來不合理的欄位加進 uncertain，
前端標黃請店員確認。不讓模型（或程式）自己「修正」——開思考實測會把寫對的「臺北」改成「臺中」。
"""
import re
from datetime import date
from typing import Dict, Optional

# 台灣常見複姓（含雙姓）。四個字的姓名前兩字不在這裡 → 很可能是複姓第二字讀錯（例：張廖→張聲）
COMPOUND = {"張簡", "張廖", "簡廖", "范姜", "歐陽", "司馬", "諸葛", "上官", "司徒", "夏侯", "東方", "皇甫",
            "端木", "公孫", "長孫", "慕容", "令狐", "鍾離", "宇文", "尉遲", "南宮", "西門", "呼延", "獨孤",
            "軒轅", "澹台", "公羊", "左丘", "東郭", "百里"}

# 常見單姓（百家姓＋台灣常見）。不在清單只會標黃，不會擋
SINGLE = set(
    "陳林黃張李王吳劉蔡楊許鄭謝郭洪曾邱廖賴周徐蘇葉莊呂江何蕭羅高潘簡朱鍾彭游詹胡施沈余趙盧梁顏柯孫魏翁戴范"
    "宋方鄧杜傅侯曹溫薛丁馬蔣唐卓藍馮姚石董紀歐程連古汪湯姜田康鄒白涂尤巫韓龔嚴袁鐘黎金阮陸倪夏童邵柳錢葛章"
    "盛管華段雷賀易喬孔毛史齊闕甘辜粘鄞塗饒甯卜包祝左崔邢任尹熊武牛楚談殷宮萬符樂喻季聶鮑費竇霍樊鄔敖冉冷單"
    "練麥伍戚岳衛黨封邴鄢鄺繆覃莫賈苗閻凌秦景解屈項湛晏鄂荊祁遊仇舒申文蒲向臧支虞侍翟艾闞姬酈雲杭時皮卞元顧"
    "孟平和穆禹狄米貝明計伏成茅龐閔席麻強路婁危梅刁駱昝經房裘干應宗宣賁郁諸吉鈕嵇滑裴榮荀羊於惠甄麴家芮羿儲"
    "靳汲糜松井富烏焦巴弓牧隗山谷車宓蓬全郗班仰秋仲伊寧欒暴鈄厲戎祖束龍幸司韶郜薊薄印宿懷邰從索咸籍藺屠蒙池"
    "陰鬱胥能蒼雙聞莘譚貢勞逄扶堵宰雍卻璩桑桂濮壽通邊扈燕冀郟浦尚農別柴瞿充慕茹習宦魚容慎戈庾終暨居衡步都耿"
    "滿弘匡國寇廣祿東殳沃利蔚越夔隆師鞏厙晁勾融訾辛那空毋沙乜養鞠須豐巢關蒯相查後紅竺權逯蓋益桓公鄒涂巫賴凃"
    "温黄郑谢陈刘吕许叶庄罗萧钟游詹卢颜严龚蓝邓苏杨赖")

_ID_LETTER = dict(zip("ABCDEFGHJKLMNPQRSTUVXYWZIO",
                      [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35]))


def id_checksum_ok(s: str) -> bool:
    """身分證／新式居留證（第二碼 1、2、8、9）檢查碼。"""
    s = (s or "").strip().upper()
    if not re.fullmatch(r"[A-Z][1289]\d{8}", s):
        return False
    n = _ID_LETTER[s[0]]
    total = n // 10 + (n % 10) * 9 + sum(int(d) * w for d, w in zip(s[1:9], range(8, 0, -1))) + int(s[9])
    return total % 10 == 0


def _digits(v: str) -> str:
    return re.sub(r"\D", "", v or "")


def check(f: Dict[str, str], today: Optional[date] = None) -> Dict[str, str]:
    """回傳 {欄位: 不合理的原因}。f 是 ocr_customer 整理後的欄位（日期已轉 ISO）。"""
    today = today or date.today()
    bad: Dict[str, str] = {}

    name = re.sub(r"\s", "", f.get("name") or "")
    if name:
        if not 2 <= len(name) <= 5:
            bad["name"] = "姓名長度不尋常"
        elif name[:2] in COMPOUND:
            pass
        elif len(name) >= 4 and not (name[0] in SINGLE and name[1] in SINGLE):
            # 前兩字都是單姓＝冠夫姓（陳林秀英），長輩客戶很常見，不算錯
            bad["name"] = "四個字但前兩字不是常見複姓（可能是複姓第二字讀錯，例如張廖→張聲）"
        elif name[0] not in SINGLE:
            bad["name"] = "姓氏不在常見姓氏清單"

    nid = (f.get("national_id") or "").strip().upper()
    if nid:
        if not id_checksum_ok(nid):
            bad["national_id"] = "身分證字號格式或檢查碼不對（可能有一碼讀錯）"
        elif f.get("gender") in ("M", "F") and nid[1] in "12" and {"1": "M", "2": "F"}[nid[1]] != f["gender"]:
            bad["gender"] = bad["national_id"] = "性別和身分證第二碼對不上"

    for k, lo in (("birth_date", 110), ("registration_date", None)):
        v = f.get(k)
        if not v:
            continue
        try:
            d = date.fromisoformat(v)
        except ValueError:
            continue
        if d > today:
            bad[k] = "日期在未來"
        elif lo and (today.year - d.year) > lo:
            bad[k] = f"年齡超過 {lo} 歲"
        elif k == "registration_date" and d.year < 1990:
            bad[k] = "建檔日期太早"

    mob = _digits(f.get("phone_mobile"))
    if f.get("phone_mobile") and not re.fullmatch(r"09\d{8}", mob):
        bad["phone_mobile"] = "手機應為 09 開頭共 10 碼"
    home = _digits(f.get("phone_home"))
    if f.get("phone_home") and not re.fullmatch(r"0\d{7,9}", home):
        bad["phone_home"] = "市話號碼位數不對"
    cp = f.get("contact_phone") or ""
    if cp and cp not in ("同上", "同 上") and not re.fullmatch(r"09\d{8}|0\d{7,9}", _digits(cp)):
        bad["contact_phone"] = "聯絡人電話位數不對"
    em = (f.get("email") or "").strip()
    if em and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", em):
        bad["email"] = "Email 格式不對"
    return bad
