/* common.js — 全站共用（layout 頁面載入；登入頁自成一體不載本檔）
   內容：$/esc/api、民國年日期過濾器 DF、日期補斜線、toast、modal、
        主題切換、漢堡選單、header 使用者/權限、登出。
   原則：API/DB 一律西元 ISO，民國年只在輸入解析與畫面顯示兩個邊界。 */
"use strict";

const $ = id => document.getElementById(id);
const esc = s => (s ?? "").toString()
  .replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");
const today = () => new Date().toISOString().slice(0, 10);
const isoDate = d =>
  `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
const WEEKDAYS = ["週日","週一","週二","週三","週四","週五","週六"];

// ── API（401 → 導向登入頁，帶回跳位置）───────────────────────
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...opts,
  });
  if (res.status === 401) {
    location.href = "/login?next=" + encodeURIComponent(location.pathname + location.hash);
    throw new Error("未登入");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

// ══ 日期過濾器（民國年模組）══════════════════════════════════
// ★ 要移除民國年：把下方 const DF = ROC_FILTER 改成 GREGORIAN_FILTER 即可。
function _parseDateInput(s, rocYear) {
  s = (s ?? "").trim();
  if (!s) return "";
  const m = s.match(/^(\d{1,4})[\/\-.](\d{1,2})[\/\-.](\d{1,2})$/);
  if (!m) return null;
  let y = +m[1];
  if (rocYear && y < 1000) y += 1911;   // 民國 → 西元；輸入 4 位數視為已是西元
  if (y < 1900 || y > 2200) return null;
  const mo = +m[2], dd = +m[3];
  const d = new Date(y, mo - 1, dd);
  if (d.getFullYear() !== y || d.getMonth() !== mo - 1 || d.getDate() !== dd) return null;
  return `${y}-${String(mo).padStart(2,"0")}-${String(dd).padStart(2,"0")}`;
}
const ROC_FILTER = {
  hint: "民國年",
  placeholder: "民國年，如 114/07/11",
  yearLen: 3,
  tryToISO: s => _parseDateInput(s, true),
  fromISO(iso) {
    if (!iso) return "";
    const [y, mo, dd] = iso.split("-");
    return `${+y - 1911}/${mo}/${dd}`;
  },
  fromISODateTime(s) {
    if (!s) return "";
    const i = s.indexOf(" ");
    return i < 0 ? this.fromISO(s) : this.fromISO(s.slice(0, i)) + s.slice(i);
  },
  yearLabel: y => `民國 ${y - 1911} 年`,
};
const GREGORIAN_FILTER = {
  hint: "西元",
  placeholder: "西元，如 2026/07/11",
  yearLen: 4,
  tryToISO: s => _parseDateInput(s, false),
  fromISO: iso => iso ?? "",
  fromISODateTime: s => s ?? "",
  yearLabel: y => `${y} 年`,
};
const DF = ROC_FILTER;   // ← 政權更換時改用 GREGORIAN_FILTER

function dfToISO(v, label) {
  const iso = DF.tryToISO(v);
  if (iso === null) throw new Error(`「${label}」格式不正確，請輸入${DF.placeholder}。`);
  return iso;
}
function dateLabel(dIso) {
  const d = new Date(dIso + "T00:00:00");
  return `${DF.fromISO(dIso)}（${WEEKDAYS[d.getDay()]}）`;
}

// 日期輸入自動補斜線：年(DF.yearLen 碼)/月(2)/日(2)
function maskDate(v) {
  const L = DF.yearLen;
  const dg = (v || "").replace(/\D/g, "").slice(0, L + 4);
  if (dg.length <= L) return dg;
  if (dg.length <= L + 2) return dg.slice(0, L) + "/" + dg.slice(L);
  return dg.slice(0, L) + "/" + dg.slice(L, L + 2) + "/" + dg.slice(L + 2);
}
function attachDateMask(el) {
  if (!el || el._masked) return;
  el._masked = true;
  el.addEventListener("input", () => {
    const atEnd = el.selectionStart === el.value.length;
    el.value = maskDate(el.value);
    if (atEnd) el.setSelectionRange(el.value.length, el.value.length);
  });
}

// ── 訊息浮層（#toast，15 秒自動收合）────────────────────────
let toastTimer = null;
function setStatus(text, css) {
  $("toast-text").textContent = text;
  $("toast").className = `banner ${css}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(hideStatus, 15000);
}
function hideStatus() { $("toast").className = "banner hidden"; clearTimeout(toastTimer); }
function setNote(id, text) {
  $(id).textContent = text || "";
  $(id).classList.toggle("hidden", !text);
}

// ── 對話框（單一 overlay，關閉即清空內容釋放記憶體）──────────
function openModal(title, bodyHtml) {
  $("modal-title").textContent = title;
  $("modal-body").innerHTML = bodyHtml;
  $("modal-overlay").classList.remove("hidden");
}
function closeModal() {
  $("modal-overlay").classList.add("hidden");
  $("modal-body").innerHTML = "";   // 弱機守則：關閉即銷毀內容
}
$("modal-overlay").addEventListener("click", e => {
  if (e.target === $("modal-overlay")) closeModal();
});
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

// ── 三態選鈕（各頁的 .seg 都通用）────────────────────────────
function setSeg(segId, v) {
  $(segId).dataset.value = v;
  $(segId).querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.v === v));
}
const getSeg = segId => $(segId).dataset.value;

// ── 主題切換（使用者選單內的項目）────────────────────────────
function currentTheme() {
  return document.documentElement.dataset.theme ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}
function refreshThemeBtn() {
  $("theme-toggle").innerHTML = currentTheme() === "dark"
    ? '<span class="um-ico">☀️</span>切換淺色主題'
    : '<span class="um-ico">🌙</span>切換深色主題';
}
$("theme-toggle").onclick = () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("y1crm-theme", next); } catch {}
  refreshThemeBtn();   // 選單保持開啟，立即看到切換效果
};
refreshThemeBtn();

// ── 使用者頭像選單（Google 式：點頭像開合、點外面關閉）────────
function toggleUserMenu(open) {
  const menu = $("user-menu");
  const want = open ?? menu.classList.contains("hidden");
  menu.classList.toggle("hidden", !want);
  $("avatar-btn").setAttribute("aria-expanded", want ? "true" : "false");
}
$("avatar-btn").addEventListener("click", e => { e.stopPropagation(); toggleUserMenu(); });
document.addEventListener("click", e => {
  if (!$("user-menu").classList.contains("hidden") &&
      !$("user-menu").contains(e.target)) toggleUserMenu(false);
});
document.addEventListener("keydown", e => { if (e.key === "Escape") toggleUserMenu(false); });

// ── 手機漢堡選單 ─────────────────────────────────────────────
function closeNav() {
  $("topbar").classList.remove("nav-open");
  $("nav-toggle").setAttribute("aria-expanded", "false");
  document.body.classList.remove("nav-locked");
}
$("nav-toggle").addEventListener("click", () => {
  const open = $("topbar").classList.toggle("nav-open");
  $("nav-toggle").setAttribute("aria-expanded", open ? "true" : "false");
  document.body.classList.toggle("nav-locked", open);   // 抽屜開啟時鎖底層捲動
  // 手機展開選單時預設全組展開（多層一目瞭然；點群組標題可個別收合）
  if (open) $("topnav").querySelectorAll(".nav-group").forEach(g => g.classList.add("open"));
});
$("nav-backdrop") && $("nav-backdrop").addEventListener("click", closeNav);

// ── 登出 ─────────────────────────────────────────────────────
async function logout() {
  try { await api("/api/logout", { method: "POST" }); } catch {}
  location.href = "/login";
}

// ── 影像縮圖（上傳前在瀏覽器縮小：最長邊 maxEdge px、JPEG 85%）──
async function shrinkImage(file, maxEdge = 1600) {
  const MAX = maxEdge;
  const bmp = await createImageBitmap(file, { imageOrientation: "from-image" }).catch(() => null);
  if (!bmp) return file;   // 瀏覽器解不開就原樣上傳，交給伺服器大小上限把關
  const scale = Math.min(1, MAX / Math.max(bmp.width, bmp.height));
  if (scale === 1 && file.type === "image/jpeg" && file.size < 800 * 1024) return file;
  const cv = document.createElement("canvas");
  cv.width = Math.round(bmp.width * scale);
  cv.height = Math.round(bmp.height * scale);
  cv.getContext("2d").drawImage(bmp, 0, 0, cv.width, cv.height);
  const blob = await new Promise(r => cv.toBlob(r, "image/jpeg", .85));
  return blob || file;
}

// ── 代碼表（下拉選單/標籤共用；各頁自取，導頁即釋放）─────────
let CODES = null;
async function ensureCodes() { if (!CODES) CODES = await api("/api/codes"); return CODES; }
const codeLabel = (cat, key) =>
  ((CODES && CODES[cat]) || []).find(c => c.key === key)?.value ?? (key || "—");
function fillSelect(id, cat, blankLabel = "—") {
  $(id).innerHTML = `<option value="">${blankLabel}</option>` +
    (CODES[cat] || []).map(c => `<option value="${c.key}">${esc(c.value)}</option>`).join("");
}

// ── header：載入使用者與權限（每頁進入時執行）────────────────
function _setAvatar(imgId, initId, uid, realname) {
  const img = $(imgId), init = $(initId);
  init.textContent = (realname || "?").trim().charAt(0);
  img.onerror = () => { img.classList.add("hidden"); init.classList.remove("hidden"); };
  img.onload = () => { img.classList.remove("hidden"); init.classList.add("hidden"); };
  img.src = `/api/staff/${uid}/photo?t=${Date.now()}`;   // 404（無照片）→ 退回姓名首字
}

async function initHeader() {
  const me = await api("/api/me");   // 401 會自動導 /login
  $("hdr-company").textContent = me.company_name || "y1crm";
  $("hdr-branch").textContent = me.branch_name || "";
  // 使用者選單：頭像（無照片→姓名首字）＋ 姓名 ＋ 權限
  _setAvatar("avatar-img", "avatar-initial", me.uid, me.realname);
  _setAvatar("um-img", "um-initial", me.uid, me.realname);
  $("um-name").textContent = me.realname || me.username;
  $("um-level").textContent = me.level;
  ensureCodes().then(() =>       // 權限顯示中文（非同步補上，不擋頁面）
    { $("um-level").textContent = codeLabel("level", me.level); }).catch(() => {});
  document.body.classList.toggle("can-price", (me.caps || []).includes("view_price"));
  renderNav(me.nav || []);   // 選單樹（menus 表驅動，後端已依權限過濾）
  return me;
}

// ── 導覽選單（多層）：桌機下拉、手機手風琴 ────────────────────
function _navHasActive(n) {
  return n.path === location.pathname || (n.children || []).some(_navHasActive);
}
function _navNode(n) {
  const label = `${n.icon ? n.icon + " " : ""}${esc(n.title)}`;
  if (!n.children)
    return `<a class="nav-leaf${n.path === location.pathname ? " on" : ""}" href="${n.path}">${label}</a>`;
  return `<div class="nav-group"><button type="button" class="nav-parent${_navHasActive(n) ? " on" : ""}"
    >${label}<span class="nav-chev">▾</span></button>
    <div class="nav-sub">${n.children.map(_navNode).join("")}</div></div>`;
}
function _navCloseAll() {
  $("topnav").querySelectorAll(".nav-group.open").forEach(g => g.classList.remove("open"));
}
function renderNav(nodes) {
  // 抽屜標頭只在手機側滑時顯示（CSS 控制），桌機自動隱藏
  $("topnav").innerHTML =
    `<div class="nav-drawer-head"><span>選單</span>
       <button type="button" class="nav-close" onclick="closeNav()" title="關閉選單">✕</button></div>` +
    nodes.map(_navNode).join("");
  $("topnav").querySelectorAll(".nav-parent").forEach(btn => {
    btn.onclick = e => {
      e.stopPropagation();
      const g = btn.closest(".nav-group");
      const opening = !g.classList.contains("open");
      if (!matchMedia("(max-width: 760px)").matches) _navCloseAll();  // 桌機一次只開一組
      g.classList.toggle("open", opening);
    };
  });
}
document.addEventListener("click", e => {
  // 點外收合下拉群組。排除三處：選單本身、漢堡鈕（開啟動作會冒泡到這裡，
  // 不排除會把剛展開的群組立刻收回＝手機抽屜「一點就縮」）、遮罩（只關抽屜不動群組）。
  if (!$("topnav").contains(e.target) && !$("nav-toggle").contains(e.target) &&
      e.target.id !== "nav-backdrop") _navCloseAll();
});
document.addEventListener("keydown", e => { if (e.key === "Escape") { _navCloseAll(); closeNav(); } });
