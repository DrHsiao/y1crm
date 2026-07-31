/* line_chat.js — LINE 客服對話頁（AI 自動回覆紀錄 + 人工接手）
   依賴 common.js：$/esc/api/DF/setStatus/openModal/initHeader

   ⚠ 這頁只看與接手，不做回覆：店員回覆一律在 LINE 官方帳號 App
     （後台人工回覆不佔推播額度；LINE 也不會把店員回覆送進 webhook）。
   ⚠ 草稿模式（config [ai] auto_reply=false）已退場，本頁不做草稿採用/改寫/捨棄；
     歷史草稿仍會在訊息串中以淡色標示，只供查閱。 */
"use strict";

const POLL_MS = 20000;
let convs = [];
let curUid = null;
let pollTimer = null;

const SRC_LABEL = { ai: "🤖 AI", ai_draft: "📝 草稿（未採用）", staff: "店員", system: "系統" };

// ── 對話清單 ────────────────────────────────────────────────
async function loadConvs() {
  try {
    const r = await api("/api/line/conversations");
    convs = r.rows;
    renderAiState(r);
    renderConvs();
    if (curUid) renderHead();
  } catch (e) { setStatus(e.message, "danger"); }
}

function renderAiState(r) {
  const el = $("lc-ai-state");
  if (!r.ai_enabled) {
    el.className = "lc-state off";
    el.textContent = "AI 客服已停用";
  } else if (r.auto_reply) {
    el.className = "lc-state on";
    el.textContent = "🤖 AI 自動回覆中";
  } else {
    el.className = "lc-state warn";
    el.textContent = "AI 草稿模式（不會自動回客戶）";
  }
}

function convName(c) {
  return c.customer_name || c.display_name || c.line_user_id.slice(0, 12) + "…";
}

function renderConvs() {
  const box = $("lc-list");
  const keep = box.scrollTop;          // 20 秒輪詢重繪，別把店員的捲動位置歸零
  $("lc-count").textContent = `（${convs.length} 則對話）`;
  box.innerHTML = convs.length ? convs.map(c => `
    <button type="button" class="lc-item${c.line_user_id === curUid ? " on" : ""}"
            onclick="openConv('${esc(c.line_user_id)}')">
      <span class="lc-ava">${c.picture_url
        ? `<img src="${esc(c.picture_url)}" alt="" referrerpolicy="no-referrer">`
        : esc(convName(c).slice(0, 1))}</span>
      <span class="lc-item-main">
        <span class="lc-item-top">
          <b>${esc(convName(c))}</b>
          ${c.mode === "human" ? '<span class="lc-tag human">人工</span>' : ""}
        </span>
        <span class="lc-last">${esc(c.last_text ?? "") || "—"}</span>
      </span>
      <span class="lc-time">${esc(DF.fromISODateTime(c.last_msg_at ?? ""))}</span>
    </button>`).join("")
    : `<p class="hint" style="padding:1rem;">目前沒有 LINE 對話紀錄。</p>`;
  box.scrollTop = keep;
}

// ── 訊息串 ──────────────────────────────────────────────────
async function openConv(uid) {
  curUid = uid;
  $("lc-wrap").classList.add("show-pane");   // 手機：切到訊息串
  renderConvs();
  renderHead();
  await loadMessages();
}

function backToList() {
  $("lc-wrap").classList.remove("show-pane");
  curUid = null;
  renderConvs();
  $("lc-head").innerHTML = "";
  $("lc-thread").innerHTML = `<p class="hint" style="padding:1rem;">請從左側挑一則對話。</p>`;
}

function renderHead() {
  const c = convs.find(x => x.line_user_id === curUid);
  if (!c) return;
  const human = c.mode === "human";
  $("lc-head").innerHTML = `
    <button type="button" class="btn sm lc-back" onclick="backToList()">← 對話清單</button>
    <div class="lc-head-main">
      <b>${esc(convName(c))}</b>
      ${c.customer_id
        ? `<a class="lh" href="/customers#detail/${c.customer_id}">開啟客戶資料</a>`
        : `<span class="lh">未綁定客戶</span>`}
    </div>
    <div class="lc-head-state">
      ${human
        ? `<span class="lc-tag human">🙋 人工接手中</span>
           <span class="hint">${esc(c.assigned_to || "系統轉入")}${
             c.human_until ? `‧${esc(DF.fromISODateTime(c.human_until))} 自動交還` : ""}${
             c.handoff_reason ? `‧原因 ${esc(c.handoff_reason)}` : ""}</span>`
        : `<span class="lc-tag bot">🤖 AI 回覆中</span>`}
      <button type="button" class="btn sm" onclick="setMode('${human ? "bot" : "human"}')">
        ${human ? "交還 AI" : "我來接手"}</button>
    </div>`;
}

async function loadMessages() {
  if (!curUid) return;
  const box = $("lc-thread");
  // 捲到底才自動跟隨，避免店員往回看歷史時被輪詢拉走
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  try {
    const r = await api(`/api/line/conversations/${encodeURIComponent(curUid)}/messages`);
    box.innerHTML = r.rows.length ? r.rows.map(msgHtml).join("")
      : `<p class="hint" style="padding:1rem;">這則對話還沒有訊息。</p>`;
    if (atBottom) box.scrollTop = box.scrollHeight;
  } catch (e) { setStatus(e.message, "danger"); }
}

function msgHtml(m) {
  const out = m.direction === "out";
  const draft = m.source === "ai_draft";
  const failed = m.source === "ai" && m.status === "discarded";
  const img = m.has_media && (m.mime || "").startsWith("image/");
  return `
    <div class="lc-msg ${out ? "out" : "in"}${draft ? " draft" : ""}">
      <div class="lc-bubble">
        ${SRC_LABEL[m.source] ? `<span class="lc-src">${SRC_LABEL[m.source]}</span>` : ""}
        ${img ? `<img class="lc-img" src="/api/line/media/${m.id}" alt="客戶傳來的圖片"
                     onclick="zoomImg(${m.id})" loading="lazy">`
              : `<span class="lc-text">${esc(m.text)}</span>`}
        ${failed ? `<span class="lc-fail">⚠ 這則回覆送不出去（reply token 逾時或 LINE API 失敗），客戶沒收到</span>` : ""}
        <span class="lc-meta">${esc(DF.fromISODateTime(m.created_at ?? ""))}${
          m.meta ? `‧${esc(m.meta)}` : ""}</span>
      </div>
    </div>`;
}

function zoomImg(mid) {
  openModal("客戶傳來的圖片",
    `<a href="/api/line/media/${mid}" target="_blank" rel="noopener">
       <img src="/api/line/media/${mid}" alt="" style="max-width:100%;border-radius:6px;">
     </a>
     <p class="hint">點圖可另開原尺寸。</p>`);
}

// ── 接手／交還 ──────────────────────────────────────────────
async function setMode(mode) {
  if (!curUid) return;
  if (mode === "human" && !confirm("接手後 AI 會停止自動回覆這位客戶，請記得到 LINE 官方帳號 App 回覆。要接手嗎？")) return;
  try {
    await api(`/api/line/conversations/${encodeURIComponent(curUid)}/mode`,
      { method: "PUT", body: JSON.stringify({ mode }) });
    setStatus(mode === "human" ? "已接手，AI 不會再自動回覆這位客戶。" : "已交還 AI。", "success");
    await loadConvs();
  } catch (e) { setStatus(e.message, "danger"); }
}

// ── 輪詢（分頁在背景就跳過，省弱機資源）──────────────────────
function poll() {
  if (document.hidden) return;
  loadConvs();
  if (curUid) loadMessages();
}

// 開機
initHeader()
  .then(() => loadConvs())
  .then(() => { pollTimer = setInterval(poll, POLL_MS); })
  .catch(() => {});
