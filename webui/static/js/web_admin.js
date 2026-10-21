/* web_admin.js — 官網管理頁（選單權限 web）：編輯官網 y1web 的關於韻譯／聽力專欄／好評分享。
   資料存在 y1web 資料庫；官網快取 60 秒，存檔後最多一分鐘出現在官網。
   編輯區放在頁面上（不用對話框：對話框按 Esc 或點外面就會關掉，長文章容易誤丟）。
   依賴 common.js：$/esc/api/DF/dfToISO/attachDateMask/setStatus/setSeg/getSeg/shrinkImage/initHeader */
"use strict";

let META = null;
let rows = [];
let cur = null;        // 編輯中的那筆（新增時 { id: null }）
let dirty = false;
let pickFor = null;    // "cover" 或 "body"

const KIND_HINT = {
  about: "「關於韻譯」頁由上而下顯示「發佈」的段落（依排序）。每段可以放一張圖。",
  article: "「聽力專欄」新的在前。發佈日期在未來的，到那天才會出現。",
  share: "「好評分享」只會顯示「發佈」而且勾了「已取得當事人同意」的。請勿張貼別家業者或網路上的評價。",
};
const kind = () => getSeg("wa-kind");
const statusTag = r => r.status === "published"
  ? '<span class="code-badge">發佈</span>' : '<span class="lh">草稿</span>';
const coverTag = r => r.cover ? "🖼" : "";

// ── 列表 ─────────────────────────────────────────────────────
async function loadList() {
  const k = kind();
  const r = await api(`/api/web/posts?kind=${k}`);
  rows = r.rows;
  $("wa-count").textContent = `（${rows.length} 筆）`;
  $("wa-hint").textContent = KIND_HINT[k];
  const site = META.site_url + { about: "/about", article: "/column", share: "/share" }[k];
  $("wa-site").href = site;
  const head = {
    about: "<th>排序</th><th>英文小標</th><th>標題</th><th>狀態</th><th>圖</th><th>最後修改</th>",
    article: "<th>發佈日期</th><th>分類</th><th>標題</th><th>狀態</th><th>圖</th><th>最後修改</th>",
    share: "<th>發佈日期</th><th>分享人</th><th>標題</th><th>同意</th><th>狀態</th><th>最後修改</th>",
  }[k];
  $("wa-head").innerHTML = `<tr>${head}</tr>`;
  const modified = r => `${esc(DF.fromISODateTime(r.updated_at))}<br><span class="lh">${esc(r.updated_by || "")}</span>`;
  $("wa-body").innerHTML = rows.map(r => {
    const cells = {
      about: `<td>${r.sort}</td><td>${esc(r.extra.en || "")}</td><td>${esc(r.title)}</td>
              <td>${statusTag(r)}</td><td>${coverTag(r)}</td><td>${modified(r)}</td>`,
      article: `<td>${esc(DF.fromISO(r.publish_date)) || "—"}</td><td>${esc(r.category || "")}</td><td>${esc(r.title)}</td>
              <td>${statusTag(r)}</td><td>${coverTag(r)}</td><td>${modified(r)}</td>`,
      share: `<td>${esc(DF.fromISO(r.publish_date)) || "—"}</td><td>${esc(r.extra.person || "")}</td><td>${esc(r.title)}</td>
              <td>${r.extra.consent ? "✓" : '<span class="lh">未勾</span>'}</td><td>${statusTag(r)}</td><td>${modified(r)}</td>`,
    }[k];
    return `<tr onclick="waEdit(${r.id})" style="cursor:pointer;">${cells}</tr>`;
  }).join("") || `<tr><td colspan="6" class="hint">還沒有內容，按「＋ 新增」開始。</td></tr>`;
}

// ── 編輯 ─────────────────────────────────────────────────────
function showKindFields(k) {
  document.querySelectorAll("#wa-editor .wa-only").forEach(el =>
    el.classList.toggle("hidden", !el.dataset.kinds.split(" ").includes(k)));
}

function renderCover() {
  const box = $("wa-cover");
  if (!cur.id) { box.innerHTML = '<div class="field-info">先儲存，再上傳封面圖。</div>'; return; }
  box.innerHTML = (cur.cover
    ? `<img class="wa-cover-img" src="/api/web/media/${esc(cur.cover)}" alt="">` : '<div class="field-info">沒有封面圖。</div>')
    + `<div style="display:flex; gap:.5rem; margin-top:.5rem;">
         <button type="button" class="btn sm outline" onclick="waPick('cover')">📷 ${cur.cover ? "更換" : "上傳"}</button>
         ${cur.cover ? '<button type="button" class="btn sm outline" onclick="waRemoveCover()">移除</button>' : ""}
       </div>`;
}

async function waEdit(id) {
  if (!(await confirmLeave())) return;
  const k = kind();
  cur = id ? { ...rows.find(r => r.id === id) } : { id: null, kind: k, status: "draft", extra: {}, sort: (rows.length + 1) * 10 };
  showKindFields(k);
  $("wa-ed-title").textContent = (id ? "編輯：" : "新增：") + META.kinds[k];
  $("wa-title").value = cur.title || "";
  $("wa-en").value = cur.extra.en || "";
  $("wa-category").innerHTML = '<option value="">請選擇</option>' +
    META.categories.map(c => `<option${c === cur.category ? " selected" : ""}>${esc(c)}</option>`).join("");
  $("wa-person").value = cur.extra.person || "";
  $("wa-model").value = cur.extra.model || "";
  $("wa-hearing").value = cur.extra.hearing || "";
  $("wa-consent").checked = !!cur.extra.consent;
  $("wa-summary").value = cur.summary || "";
  $("wa-body-md").value = cur.body_md || "";
  setSeg("wa-status", cur.status || "draft");
  $("wa-date").value = DF.fromISO(cur.publish_date || "");
  $("wa-sort").value = cur.sort ?? 0;
  $("wa-slug").value = cur.slug || "";
  $("wa-del").classList.toggle("hidden", !cur.id);
  $("wa-meta").textContent = cur.id ? `最後修改：${DF.fromISODateTime(cur.updated_at)}　${cur.updated_by || ""}` : "";
  $("wa-err").classList.add("hidden");
  renderCover();
  $("wa-list-card").classList.add("hidden");
  $("wa-editor").classList.remove("hidden");
  window.scrollTo(0, 0);
  dirty = false;
}

async function confirmLeave() {
  return !dirty || confirm("內容還沒儲存，確定要離開嗎？");
}

async function waClose() {
  if (!(await confirmLeave())) return;
  dirty = false;
  $("wa-editor").classList.add("hidden");
  $("wa-list-card").classList.remove("hidden");
  await loadList();
}

function showErr(msg) { const el = $("wa-err"); el.textContent = msg; el.classList.remove("hidden"); }

function collect() {
  const k = cur.kind || kind();
  const body = {
    kind: k, title: $("wa-title").value.trim(), summary: $("wa-summary").value.trim(),
    body_md: $("wa-body-md").value, status: getSeg("wa-status"), slug: $("wa-slug").value.trim(),
    sort: $("wa-sort").value || 0, en: $("wa-en").value.trim(), category: $("wa-category").value,
    person: $("wa-person").value.trim(), model: $("wa-model").value.trim(), hearing: $("wa-hearing").value.trim(),
    consent: $("wa-consent").checked,
  };
  body.publish_date = dfToISO($("wa-date").value, "發佈日期");
  return body;
}

async function waSave() {
  let body;
  try { body = collect(); } catch (e) { showErr(e.message); return; }
  if (!body.title) { showErr("請填寫標題。"); return; }
  try {
    const r = cur.id
      ? await api(`/api/web/posts/${cur.id}`, { method: "PUT", body: JSON.stringify(body) })
      : await api("/api/web/posts", { method: "POST", body: JSON.stringify(body) });
    dirty = false;
    setStatus(body.status === "published" ? "已儲存並發佈，約一分鐘內出現在官網。" : "已儲存為草稿。", "success");
    await loadList();
    await waEdit(r.id);     // 留在編輯畫面（新增後就可以上傳封面圖）
  } catch (e) { showErr(e.message); }
}

async function waDelete() {
  if (!cur.id || !confirm(`確定刪除「${cur.title}」？刪除後無法復原。`)) return;
  try {
    await api(`/api/web/posts/${cur.id}`, { method: "DELETE" });
    dirty = false;
    setStatus("已刪除。", "success");
    await waClose();
  } catch (e) { showErr(e.message); }
}

// ── 內文小工具 ───────────────────────────────────────────────
function waInsert(text, selStart, selEnd) {
  const ta = $("wa-body-md");
  const s = ta.selectionStart, e = ta.selectionEnd;
  ta.value = ta.value.slice(0, s) + text + ta.value.slice(e);
  ta.focus();
  ta.setSelectionRange(s + selStart, s + selEnd);
  dirty = true;
}
function waWrap(before, after, placeholder) {
  const ta = $("wa-body-md");
  const sel = ta.value.slice(ta.selectionStart, ta.selectionEnd) || placeholder;
  waInsert(before + sel + after, before.length, before.length + sel.length);
}
function waLine(prefix, placeholder) {
  const ta = $("wa-body-md");
  const sel = ta.value.slice(ta.selectionStart, ta.selectionEnd) || placeholder;
  const atLineStart = ta.selectionStart === 0 || ta.value[ta.selectionStart - 1] === "\n";
  const lead = atLineStart ? "" : "\n";
  waInsert(lead + prefix + sel, lead.length + prefix.length, lead.length + prefix.length + sel.length);
}

// ── 圖片：封面（存到這筆）、內文插圖（回傳網址插進內文）──────────────
function waPick(target) { pickFor = target; $("wa-file").click(); }

async function uploadImage(path, file, method) {
  const blob = await shrinkImage(file, 2000);
  const res = await fetch(path, { method, credentials: "same-origin",
    headers: { "Content-Type": blob.type || "image/jpeg" }, body: blob });
  if (res.status === 401) { location.href = "/login?next=/web-admin"; throw new Error("未登入"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

$("wa-file").addEventListener("change", async e => {
  const file = e.target.files[0]; e.target.value = "";
  if (!file) return;
  try {
    if (pickFor === "cover") {
      const r = await uploadImage(`/api/web/posts/${cur.id}/cover`, file, "PUT");
      cur.cover = r.cover;
      renderCover();
      setStatus("封面圖已更新。", "success");
    } else {
      const r = await uploadImage("/api/web/images", file, "POST");
      const alt = prompt("圖片說明（給看不到圖的人，例如：助聽器充電座）", "") || "圖片";
      waInsert(`\n![${alt}](${r.url})\n`, 0, 0);
      setStatus("圖片已插入內文，記得按「儲存」。", "success");
    }
  } catch (err) { setStatus("圖片上傳失敗：" + err.message, "danger"); }
});

async function waRemoveCover() {
  if (!confirm("移除封面圖？")) return;
  try {
    await api(`/api/web/posts/${cur.id}/cover`, { method: "DELETE" });
    cur.cover = null;
    renderCover();
  } catch (e) { setStatus(e.message, "danger"); }
}

// ── 開機 ─────────────────────────────────────────────────────
$("wa-kind").querySelectorAll("button").forEach(b => b.addEventListener("click", async () => {
  setSeg("wa-kind", b.dataset.v);
  await loadList();
}));
$("wa-status").querySelectorAll("button").forEach(b => b.addEventListener("click", () => {
  setSeg("wa-status", b.dataset.v); dirty = true;
}));
document.querySelectorAll("#wa-editor input, #wa-editor textarea, #wa-editor select").forEach(el =>
  el.addEventListener("input", () => { dirty = true; }));
window.addEventListener("beforeunload", e => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
attachDateMask($("wa-date"));
$("wa-date").placeholder = DF.placeholder;
$("wa-date-hint").textContent = DF.hint;

initHeader().then(async me => {
  if (!(me.menus || []).includes("web")) { location.href = "/calendar"; return; }
  META = await api("/api/web/meta");
  const k = new URLSearchParams(location.search).get("k");
  setSeg("wa-kind", META.kinds[k] ? k : "about");
  await loadList();
}).catch(() => {});
