/* fb_chat.js — 粉專訊息頁（Messenger 私訊＋貼文留言，可直接回覆）
   依賴 common.js：$/esc/api/DF/setStatus/setSeg/getSeg/initHeader

   和 LINE 客服頁不同：粉專的回覆從這裡送（Graph API，不收費），所以有回覆框。
   ⚠ 公開留言的 AI 只寫建議，一定要店員按「送出」；Messenger 依 config [facebook] ai_reply。 */
"use strict";

const POLL_MS = 20000;
let threads = [];
let curId = null;
let draftId = null;      // 店員按「用這段」時記下，送出後那則 AI 建議就標成已處理
let info = {};

const SRC_LABEL = { ai: "🤖 AI", staff: "店員", system: "系統" };
const KIND_ICON = { messenger: "💬", comment: "🗨" };

// ── 對話清單 ────────────────────────────────────────────────
async function loadThreads() {
  try {
    info = await api("/api/fb/threads");
    threads = info.rows;
    renderState();
    renderList();
    if (curId) renderHead();
  } catch (e) { setStatus(e.message, "danger"); }
}

function renderState() {
  const el = $("fc-state");
  if (info.dev_mode) {
    el.className = "lc-state warn";
    el.textContent = "開發模式：還沒接上粉專，回覆不會真的送出";
  } else if (!info.ai_enabled || info.ai_reply === "off") {
    el.className = "lc-state off";
    el.textContent = "私訊 AI 未啟用";
  } else if (info.ai_reply === "auto") {
    el.className = "lc-state on";
    el.textContent = "🤖 私訊 AI 自動回覆中";
  } else {
    el.className = "lc-state on";
    el.textContent = "📝 AI 只寫建議，由店員送出";
  }
}

const threadName = t => t.person_name || (t.kind === "messenger" ? "Messenger 客人" : "留言者");

function renderList() {
  const f = getSeg("fc-filter");
  const list = threads.filter(t => f === "all" || (f === "pending" ? t.pending : t.kind === f));
  const box = $("lc-list");
  const keep = box.scrollTop;
  const pending = threads.filter(t => t.pending).length;
  $("fc-count").textContent = `（${threads.length} 則${pending ? `，${pending} 則待回` : ""}）`;
  box.innerHTML = list.length ? list.map(t => `
    <button type="button" class="lc-item${t.id === curId ? " on" : ""}" onclick="openThread(${t.id})">
      <span class="lc-ava">${KIND_ICON[t.kind]}</span>
      <span class="lc-item-main">
        <span class="lc-item-top"><b>${esc(threadName(t))}</b></span>
        <span class="lc-last">${esc(t.last_text ?? "") || "—"}</span>
        ${t.pending || t.drafts ? `<span class="fc-tags">${
          t.pending ? '<span class="lc-tag human">待回</span>' : ""}${
          t.drafts ? '<span class="lc-tag bot">AI 建議</span>' : ""}</span>` : ""}
      </span>
      <span class="lc-time">${esc(DF.fromISODateTime(t.last_msg_at ?? ""))}</span>
    </button>`).join("")
    : `<p class="hint" style="padding:1rem;">${threads.length ? "沒有符合的對話。" : "目前沒有粉專訊息。"}</p>`;
  box.scrollTop = keep;
}

// ── 訊息串 ──────────────────────────────────────────────────
async function openThread(id) {
  if (curId !== id) { $("fc-text").value = ""; draftId = null; }
  curId = id;
  $("lc-wrap").classList.add("show-pane");
  renderList();
  renderHead();
  await loadMessages();
}

function backToList() {
  $("lc-wrap").classList.remove("show-pane");
  curId = null;
  renderList();
  $("lc-head").innerHTML = "";
  $("fc-reply").classList.add("hidden");
  $("lc-thread").innerHTML = `<p class="hint" style="padding:1rem;">請從左側挑一則對話。</p>`;
}

function renderHead() {
  const t = threads.find(x => x.id === curId);
  if (!t) return;
  const human = t.mode === "human";
  const where = t.kind === "comment"
    ? `<span class="lh">留言在：${t.post_id
        ? `<a href="https://www.facebook.com/${encodeURIComponent(t.post_id)}" target="_blank" rel="noopener">${esc(t.post_title || "粉專貼文")} ↗</a>`
        : "粉專貼文"}</span>`
    : `<span class="lh">Messenger 私訊</span>`;
  const modeCtl = t.kind === "messenger"
    ? (human
      ? `<span class="lc-tag human">🙋 人工接手中</span>
         <span class="hint">${esc(t.assigned_to || "系統轉入")}${
           t.human_until ? `‧${esc(DF.fromISODateTime(t.human_until))} 自動交還` : ""}</span>
         <button type="button" class="btn sm" onclick="setMode('bot')">交還 AI</button>`
      : `<button type="button" class="btn sm" onclick="setMode('human')">我來接手</button>`)
    : "";
  $("lc-head").innerHTML = `
    <button type="button" class="btn sm lc-back" onclick="backToList()">← 對話清單</button>
    <div class="lc-head-main"><b>${KIND_ICON[t.kind]} ${esc(threadName(t))}</b>${where}</div>
    <div class="lc-head-state">
      ${modeCtl}
      <button type="button" class="btn sm ghost" onclick="deleteThread()" title="客人要求刪除資料時使用">刪除紀錄</button>
    </div>`;
  const box = $("fc-reply");
  box.classList.remove("hidden");
  $("fc-text").disabled = $("fc-send").disabled = !t.can_reply;
  $("fc-note").textContent = t.can_reply
    ? (t.kind === "comment" ? "會以粉專身分公開回覆在這則留言底下。" : "")
    : `已超過 ${info.window_hours} 小時，Messenger 不允許再回覆；請改用電話或 LINE 聯絡。`;
}

async function loadMessages() {
  if (!curId) return;
  const box = $("lc-thread");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  try {
    const r = await api(`/api/fb/threads/${curId}/messages`);
    const shown = r.rows.filter(m => m.source !== "ai_draft");
    box.innerHTML = shown.length ? shown.map(msgHtml).join("")
      : `<p class="hint" style="padding:1rem;">這段對話還沒有訊息。</p>`;
    if (atBottom) box.scrollTop = box.scrollHeight;
    renderDraft(r.rows.filter(m => m.source === "ai_draft" && m.status === "new").pop());
  } catch (e) { setStatus(e.message, "danger"); }
}

function msgHtml(m) {
  const out = m.direction === "out";
  const label = m.source === "staff" && m.created_by ? `店員 ${m.created_by}` : SRC_LABEL[m.source];
  const notSent = out && (m.status === "failed" || m.status === "discarded");
  return `
    <div class="lc-msg ${out ? "out" : "in"}">
      <div class="lc-bubble">
        ${label ? `<span class="lc-src">${esc(label)}</span>` : ""}
        <span class="lc-text">${esc(m.text)}</span>
        ${notSent ? `<span class="lc-fail">⚠ 沒有送出${m.meta ? `：${esc(m.meta)}` : ""}</span>` : ""}
        <span class="lc-meta">${esc(DF.fromISODateTime(m.created_at ?? ""))}${
          m.meta && !notSent ? `‧${esc(m.meta)}` : ""}</span>
      </div>
    </div>`;
}

function renderDraft(d) {
  const box = $("fc-draft");
  if (!d) { box.classList.add("hidden"); box.innerHTML = ""; return; }
  box.classList.remove("hidden");
  box.innerHTML = `
    <span class="lc-src">📝 AI 建議（${esc(DF.fromISODateTime(d.created_at ?? ""))}）</span>
    <span class="lc-text">${esc(d.text)}</span>
    <span class="fc-draft-btns">
      <button type="button" class="btn sm outline" onclick="useDraft(${d.id})">用這段</button>
      <button type="button" class="btn sm ghost" onclick="discardDraft(${d.id})">不用</button>
    </span>`;
  box.dataset.text = d.text;
}

function useDraft(id) {
  $("fc-text").value = $("fc-draft").dataset.text || "";
  draftId = id;
  $("fc-text").focus();
}

async function discardDraft(id) {
  try {
    await api(`/api/fb/drafts/${id}`, { method: "PUT" });
    if (draftId === id) draftId = null;
    await refresh();
  } catch (e) { setStatus(e.message, "danger"); }
}

// ── 回覆與處理 ──────────────────────────────────────────────
async function sendReply() {
  const t = threads.find(x => x.id === curId);
  const text = $("fc-text").value.trim();
  if (!t || !text) return;
  if (t.kind === "comment" && !confirm("這是公開回覆，所有人都看得到。確定送出？")) return;
  $("fc-send").disabled = true;
  try {
    const r = await api(`/api/fb/threads/${curId}/reply`,
      { method: "POST", body: JSON.stringify({ text, draft_id: draftId }) });
    $("fc-text").value = "";
    draftId = null;
    setStatus(r.sent ? "已送出。" : "開發模式：已記錄，但沒有真的送到 Facebook。", r.sent ? "success" : "warning");
    await refresh();
  } catch (e) { setStatus(e.message, "danger"); }
  $("fc-send").disabled = false;
}

async function markHandled() {
  if (!curId) return;
  try {
    await api(`/api/fb/threads/${curId}/handled`, { method: "PUT" });
    setStatus("已標為不用回。", "success");
    await loadThreads();
  } catch (e) { setStatus(e.message, "danger"); }
}

async function setMode(mode) {
  if (!curId) return;
  try {
    await api(`/api/fb/threads/${curId}/mode`, { method: "PUT", body: JSON.stringify({ mode }) });
    setStatus(mode === "human" ? "已接手，AI 不會再回這位客人。" : "已交還 AI。", "success");
    await loadThreads();
  } catch (e) { setStatus(e.message, "danger"); }
}

async function deleteThread() {
  const t = threads.find(x => x.id === curId);
  if (!t || !confirm(`刪除「${threadName(t)}」這段對話在本系統的紀錄？\n（Facebook 上的留言或私訊不會被刪除；刪除後無法復原）`)) return;
  try {
    await api(`/api/fb/threads/${curId}`, { method: "DELETE" });
    setStatus("已刪除。", "success");
    backToList();
    await loadThreads();
  } catch (e) { setStatus(e.message, "danger"); }
}

async function refresh() {
  await loadThreads();
  if (curId) await loadMessages();
}

// ── 輪詢（分頁在背景就跳過）──────────────────────────────────
function poll() {
  if (document.hidden) return;
  refresh();
}

$("fc-filter").querySelectorAll("button").forEach(b => b.addEventListener("click", () => {
  setSeg("fc-filter", b.dataset.v);
  renderList();
}));

initHeader()
  .then(() => loadThreads())
  .then(() => { setInterval(poll, POLL_MS); })
  .catch(() => {});
