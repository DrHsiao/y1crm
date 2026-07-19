/* board.js — 店面狀態看板（電視牆）。獨立於 common.js（免登入、無 header）。
   節奏：時鐘每 250ms、資料每 30s 輪詢、清單每 8s 自動翻頁捲動、每日 04:00 整頁重載。 */
"use strict";

const $ = id => document.getElementById(id);
const esc = s => (s ?? "").toString()
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
const K = new URLSearchParams(location.search).get("k") || "";
const WD = ["週日", "週一", "週二", "週三", "週四", "週五", "週六"];
const TAG_COLOR = { "預約": "--teal", "回訪": "--blue", "保養": "--green",
                    "電池": "--orange", "調機": "--purple", "門市": "--purple",
                    "聯繫": "--red", "試聽": "--blue" };

let lastOk = 0, reloaded = false;

// ── 時鐘（用戶端時間；也兼看板「活著」的指示）────────────────
function tick() {
  const n = new Date();
  const p = v => String(v).padStart(2, "0");
  $("clock").textContent = `${p(n.getHours())}:${p(n.getMinutes())}`;
  $("clock-s").textContent = p(n.getSeconds());
  $("date-roc").textContent = `民國 ${n.getFullYear() - 1911} 年 ${n.getMonth() + 1} 月 ${n.getDate()} 日`;
  $("date-wd").textContent = WD[n.getDay()];
  $("live").classList.toggle("off", Date.now() - lastOk > 70000);   // >70s 沒成功＝斷線紅燈
  // 每日 04:00 整頁重載：清記憶體、更新 ?v= 版本
  if (n.getHours() === 4 && n.getMinutes() === 0) {
    if (!reloaded) { reloaded = true; location.reload(); }
  } else reloaded = false;
}
setInterval(tick, 250); tick();

// ── 資料輪詢與渲染 ───────────────────────────────────────────
function tagHtml(t) {
  const v = TAG_COLOR[t];
  return `<span class="tag"${v ? ` style="background:var(${v})"` : ""}>${esc(t)}</span>`;
}

function render(d) {
  // 公告跑馬燈（無公告時清空；速度依長度）
  const marq = (d.announcements || []).join("　◆　");
  $("marq-inner").textContent = marq ? `📢 ${marq}` : "";
  $("marquee").style.setProperty("--marq-dur", Math.max(18, marq.length * 0.55) + "s");

  // 今日行程
  const sc = d.schedule || [];
  $("sched-cnt").textContent = sc.length ? `（${sc.length}）` : "";
  $("sched-list").innerHTML = sc.length ? sc.map(s => `
    <div class="srow${s.done ? " done" : ""}${s.new ? " new" : ""}">
      <span class="t${s.time ? "" : " allday"}">${s.time || "全天"}</span>
      ${tagHtml(s.tag)}<span class="tx">${esc(s.text)}</span>
      ${s.new ? `<span class="badge-new">新</span>` : ""}
    </div>`).join("") : `<div class="empty">今日尚無排程 🎉</div>`;

  // LINE 新預約
  const ln = d.line_new || [];
  $("line-cnt").textContent = ln.length ? `（${ln.length}）` : "";
  $("line-list").innerHTML = ln.length ? ln.map(b => `
    <div class="crow">
      <span class="who">${esc(b.name)}</span>
      <span class="sub">${esc(b.when)}　${esc(b.phone)}${b.note ? "　" + esc(b.note) : ""}</span>
      <span class="right">${esc(b.created)}</span>
    </div>`).join("") : `<div class="empty">目前沒有待處理的預約</div>`;

  // 今日待辦・逾期
  const td = d.todos || [];
  $("todo-list").innerHTML = td.length ? td.map(t => `
    <div class="crow${t.overdue ? " overdue" : ""}">
      <span class="who">${esc(t.name)}</span>
      <span class="sub">${esc(t.cat)}${t.text ? "・" + esc(t.text) : ""}</span>
      <span class="right">${t.overdue ? "⚠ " : ""}${esc(t.due)}</span>
    </div>`).join("") : `<div class="empty">今日待辦已清空 ✅</div>`;

  // 壽星
  const bd = d.birthdays || [];
  $("bday-list").innerHTML = bd.length ? bd.map(b => `
    <div class="crow">
      <span class="who">${esc(b.name)}</span>
      <span class="right">${esc(b.label)}</span>
    </div>`).join("") : `<div class="empty">近 7 天沒有壽星</div>`;

  // 保固即將到期
  const wr = d.warranty || [];
  $("warr-list").innerHTML = wr.length ? wr.map(w => `
    <div class="crow${w.days <= 7 ? " soon" : ""}">
      <span class="who">${esc(w.name)}</span>
      <span class="sub">${esc(w.product)}</span>
      <span class="right">${esc(w.end)}（${w.days}天）</span>
    </div>`).join("") : `<div class="empty">30 天內沒有到期保固</div>`;

  renderMonth(d.month || {}, d.today?.iso);
}

// 迷你月曆：週日開頭（沿用行事曆慣例）、今日紅框、右下角當日筆數
function renderMonth(m, todayIso) {
  if (!m.year) return;
  $("month-title").textContent = `🗓️ 民國 ${m.year - 1911} 年 ${m.month} 月`;
  const first = new Date(m.year, m.month - 1, 1);
  const days = new Date(m.year, m.month, 0).getDate();
  let html = ["日", "一", "二", "三", "四", "五", "六"].map(w => `<div class="mh">${w}</div>`).join("");
  for (let i = 0; i < first.getDay(); i++) html += `<div class="md out"></div>`;
  for (let d = 1; d <= days; d++) {
    const iso = `${m.year}-${String(m.month).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    const n = (m.counts || {})[iso];
    html += `<div class="md${iso === todayIso ? " today" : ""}">${d}${n ? `<span class="n">${n}</span>` : ""}</div>`;
  }
  $("month-grid").innerHTML = html;
}

async function load() {
  try {
    const res = await fetch(`/api/board?k=${encodeURIComponent(K)}`, { cache: "no-store" });
    if (res.status === 403) {
      $("err").textContent = "看板金鑰不正確：請以 /board?k=<金鑰> 開啟";
      $("err").classList.remove("hidden");
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    render(await res.json());
    lastOk = Date.now();
    $("err").classList.add("hidden");
  } catch { /* 斷線：保留畫面上最後資料，live 燈轉紅 */ }
}
setInterval(load, 30000); load();

// ── 清單自動翻頁（無觸控）：內容超高時每 8 秒往下捲一頁、到底回頂 ──
setInterval(() => {
  document.querySelectorAll(".roll").forEach(el => {
    if (el.scrollHeight <= el.clientHeight + 8) return;
    const next = el.scrollTop + el.clientHeight;
    el.scrollTop = next >= el.scrollHeight - 8 ? 0 : next;
  });
}, 8000);
