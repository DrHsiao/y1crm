/* customers.js — 客戶資料頁（建檔/查詢/明細 + OCR + 待辦 + 照片 + 表單影像）
   依賴 common.js：$/esc/api/DF/dfToISO/maskDate/attachDateMask/today/setStatus/hideStatus/
                  setNote/openModal/closeModal/setSeg/getSeg/CODES/ensureCodes/codeLabel/initHeader
   頁內兩個 view：form（表單）/ results（查詢結果）；hash #detail/<id> 直開明細。 */
"use strict";

const TEXT_FIELDS = ["customer_code","name","national_id","phone_mobile","phone_home","email",
  "contact_name","contact_phone","household_city","household_district","household_village",
  "household_detail","mailing_city","mailing_district","mailing_village","mailing_detail","notes"];
const DATE_FIELDS = ["birth_date","registration_date","consent_date"];

let detailId = null;      // null=建檔/查詢模式，數字=明細編輯模式
let confirmCreate = false; // 有疑似重複時需按兩次建檔

// 頁內 view 切換（本頁只有表單/查詢結果兩個）
function showView(name) {
  for (const v of ["form","results"]) $("view-"+v).classList.add("hidden");
  $("view-"+name).classList.remove("hidden");
}

// 401 時導回登入頁（raw fetch 路徑用；api() 已內建）
function showLogin() {
  location.href = "/login?next=" + encodeURIComponent(location.pathname + location.hash);
}

// 日期欄位的佔位符與提示文字跟著過濾器走，並掛上自動補斜線
for (const f of DATE_FIELDS) { $("f-" + f).placeholder = DF.placeholder; attachDateMask($("f-" + f)); }
document.querySelectorAll(".df-hint").forEach(el => el.textContent = `（${DF.hint}）`);

// ── 三態選鈕綁定（本頁的 seg；選「已簽署」自動帶今天）─────────
for (const segId of ["seg-gender","seg-consent","seg-subsidy","seg-identity"]) {
  $(segId).querySelectorAll("button").forEach(btn => {
    btn.onclick = () => {
      setSeg(segId, btn.dataset.v);
      if (segId === "seg-consent" && btn.dataset.v === "y" && !$("f-consent_date").value)
        $("f-consent_date").value = DF.fromISO(today());
    };
  });
}

// ── 明細頁手風琴：點區塊頭展開/收合 ─────────────────────────
function toggleDsec(ev, head) {
  ev.stopPropagation();
  head.closest(".dsection").classList.toggle("collapsed");
}
// 進入明細時的預設：各區塊全部收合（含客戶資料）
function applyAccordionDefaults() {
  document.querySelectorAll("#view-form .dsection").forEach(el => el.classList.add("collapsed"));
}

// ── 表單資料 ─────────────────────────────────────────────────
function formData() {
  const d = {};
  for (const f of TEXT_FIELDS) d[f] = $("f-"+f).value;
  for (const f of DATE_FIELDS) d[f] = dfToISO($("f-"+f).value, FIELD_LABELS[f]);  // 民國→西元
  d.gender = getSeg("seg-gender");
  d.consent_signed = getSeg("seg-consent");
  d.subsidy_applied = getSeg("seg-subsidy");
  d.identity = getSeg("seg-identity");
  return d;
}

function fillForm(row) {
  for (const f of TEXT_FIELDS) $("f-"+f).value = row[f] ?? "";
  for (const f of DATE_FIELDS) $("f-"+f).value = DF.fromISO(row[f] ?? "");  // 西元→民國
  setSeg("seg-gender", row.gender ?? "");
  setSeg("seg-consent", row.consent_signed == null ? "" : (row.consent_signed ? "y" : "n"));
  setSeg("seg-subsidy", row.subsidy_applied == null ? "" : (row.subsidy_applied ? "y" : "n"));
  setSeg("seg-identity", row.identity ?? "");
  refreshSoftChecks();
}

function clearForm() {
  fillForm({});
  setSeg("seg-gender",""); setSeg("seg-consent",""); setSeg("seg-subsidy",""); setSeg("seg-identity","");
  hideDup(); hideStatus(); confirmCreate = false;
  clearOcrMarks(); ocrPendingPhoto = null;
  refreshSoftChecks();
}

// ── 建檔 / 查詢 / 明細 ──────────────────────────────────────
function enterEntryMode() {
  detailId = null;
  clearForm();
  clearPhotos();
  clearTodos();
  clearCustTx();
  $("formimg-view").innerHTML = ""; setNote("formimg-status", "");
  $("view-form").classList.remove("mode-detail");
  $("form-head-entry").classList.remove("hidden");
  $("form-head-detail").classList.add("hidden");
  $("actions-entry").classList.remove("hidden");
  $("actions-detail").classList.add("hidden");
  $("detail-meta").classList.add("hidden");
  $("created-banner").classList.add("hidden");
  refreshOcrState();
}

async function openDetail(id, created = false) {
  try {
    const row = await api(`/api/customers/${id}`);
    detailId = id;
    fillForm(row);
    applyDetailHeader(row);
    renderFormImage(row);
    loadPhotos();
    loadTodos();
    loadCustTx();
    applyAccordionDefaults();
    hideDup(); hideStatus(); confirmCreate = false;
    $("view-form").classList.add("mode-detail");
    $("form-head-entry").classList.add("hidden");
    $("form-head-detail").classList.remove("hidden");
    $("actions-entry").classList.add("hidden");
    $("actions-detail").classList.remove("hidden");
    $("created-banner").classList.toggle("hidden", !created);
    showView("form");
    window.scrollTo(0, 0);
  } catch (e) { setStatus(e.message, "danger"); }
}

function applyDetailHeader(row) {
  $("detail-name").textContent = row.name;
  $("detail-code").textContent = row.customer_code;
  renderLineStatus(row);
  $("detail-meta").textContent =
    `建檔時間：${DF.fromISODateTime(row.created_at ?? "")}‧最後更新：${DF.fromISODateTime(row.updated_at ?? "")}` +
    (row.updated_by ? `（修改者：${row.updated_by}）` : "");
  $("detail-meta").classList.remove("hidden");
}

function backToSearch() { enterEntryMode(); showView("form"); window.scrollTo(0, 0); }

// ── LINE 綁定（店員配對制：客戶只需加好友，店員在這裡點綁定）──
function renderLineStatus(row) {
  const el = $("detail-line");
  if (row.line_user_id) {
    el.innerHTML = `<span class="line-badge on">LINE 已綁定${row.line_display_name ? "‧" + esc(row.line_display_name) : ""}</span>
      <button type="button" class="btn sm outline" onclick="unbindLine()">解綁</button>`;
  } else {
    el.innerHTML = `<span class="line-badge off">LINE 未綁定</span>
      <button type="button" class="btn sm outline" onclick="openLineBindModal()">綁定 LINE</button>`;
  }
}

async function openLineBindModal() {
  let rows = [];
  try { rows = (await api("/api/line/followers")).rows; }
  catch (e) { setStatus(e.message, "danger"); return; }
  const list = rows.length ? rows.map(f => `
    <div class="lf-item">
      ${f.picture_url ? `<img src="${esc(f.picture_url)}" alt="">` : `<span class="lf-noimg">👤</span>`}
      <span class="lf-name">${esc(f.display_name || "（未提供暱稱）")}</span>
      <span class="lf-time">${esc(DF.fromISODateTime(f.last_seen_at || f.followed_at || ""))}</span>
      <button type="button" class="btn sm" onclick="bindLine('${esc(f.line_user_id)}')">綁定</button>
    </div>`).join("")
    : `<div class="hint">目前沒有待綁定的新好友。<br>
       請客戶先用 LINE 加入官方帳號（ID：<b>@021tpjrz</b> 或掃門市 QR code），
       加入後這份名單就會出現他的暱稱。</div>`;
  openModal("綁定 LINE 好友", `
    <div class="hint" style="margin-bottom:.5rem;">點「綁定」把下列 LINE 好友對應到目前這位客戶（${esc($("detail-name").textContent)}）。</div>
    ${list}
    <div class="modal-actions"><button class="btn ghost" onclick="closeModal()">關閉</button></div>`);
}

async function bindLine(uid) {
  try {
    await api(`/api/customers/${detailId}/line-bind`, {
      method: "POST", body: JSON.stringify({ line_user_id: uid }) });
    closeModal();
    await openDetail(detailId);
    setStatus("LINE 綁定成功。之後預約與提醒會推播到這個帳號。", "success");
  } catch (e) { setStatus(e.message, "danger"); }
}

async function unbindLine() {
  if (!confirm("解除這位客戶的 LINE 綁定？解除後將無法收到推播提醒。")) return;
  try {
    await api(`/api/customers/${detailId}/line-bind`, { method: "DELETE" });
    await openDetail(detailId);
    setStatus("已解除 LINE 綁定。", "success");
  } catch (e) { setStatus(e.message, "danger"); }
}

async function createCustomer() {
  hideStatus();
  let d;
  try { d = formData(); } catch (e) { setStatus(e.message, "danger"); return; }
  if (!d.name.trim()) {
    setStatus("請至少輸入「姓名」後再建檔，其餘欄位可於建檔後補登。", "danger");
    return;
  }
  await refreshDup();
  const dups = $("dup-list").children.length;
  if (dups > 0 && !confirmCreate) {
    confirmCreate = true;
    setStatus(`發現 ${dups} 筆可能重複的客戶（上方清單可點開確認）。確定不是同一人，請再按一次「建檔」。`, "warning");
    return;
  }
  setBusy(true);
  try {
    const r = await api("/api/customers", { method: "POST", body: JSON.stringify(d) });
    await openDetail(r.id, true);
    if (ocrPendingPhoto) {   // OCR 匯入/拍照的原稿 → 存為「原始表單影像」單一份（非多檔畫廊）
      try {
        const id = await putFormImage(ocrPendingPhoto.file);
        renderFormImage({ form_image_id: id });
      } catch { /* 留底失敗不影響建檔 */ }
      ocrPendingPhoto = null;
    }
  } catch (e) { setStatus(e.message, "danger"); }
  finally { setBusy(false); }
}

let lastQueryCond = {};   // 翻頁時重送同一組條件
let pagerPage = 1;

async function runQuery(cond, page = 1) {
  hideStatus(); setBusy(true);
  try {
    const r = await api("/api/customers/query",
      { method: "POST", body: JSON.stringify({ ...cond, page }) });
    if (r.total === 0) {
      setStatus("查無符合條件的客戶。若要新增，確認姓名後按「建檔」即可。", "info");
      return;
    }
    if (r.total === 1) { await openDetail(r.rows[0].id); return; }
    lastQueryCond = cond;
    pagerPage = r.page;
    $("results-count").textContent = `共 ${r.total} 筆符合`;
    $("pager-info").textContent = `第 ${r.page} / ${r.pages} 頁`;
    $("pager-prev").disabled = r.page <= 1;
    $("pager-next").disabled = r.page >= r.pages;
    $("pager").classList.toggle("hidden", r.pages <= 1);
    renderResults(r.rows);
    showView("results");
    window.scrollTo(0, 0);
  } catch (e) { setStatus(e.message, "danger"); }
  finally { setBusy(false); }
}

function pagerGo(d) { runQuery(lastQueryCond, pagerPage + d); }

function queryCustomers() {
  try { return runQuery(formData()); }
  catch (e) { setStatus(e.message, "danger"); }
}

// 欄位旁查詢鈕：只用該欄位單獨當條件（底部「查詢」= 全部欄位 AND）
const FIELD_LABELS = {
  customer_code: "客戶編號", name: "姓名",
  national_id: "身分證字號", birth_date: "出生年月日",
  registration_date: "建檔日期", consent_date: "同意書簽署日期",
};
function queryByField(f) {
  let v = $("f-" + f).value.trim();
  if (v && DATE_FIELDS.includes(f)) {   // 查詢條件同樣民國→西元後才送 API
    try { v = dfToISO(v, FIELD_LABELS[f]); }
    catch (e) { setStatus(e.message, "danger"); return; }
  }
  return runQuery(v ? { [f]: v } : {});  // 空白＝不設條件，列出全部（每頁 10 筆）
}

async function saveCustomer() {
  hideStatus();
  let d;
  try { d = formData(); } catch (e) { setStatus(e.message, "danger"); return; }
  if (!d.name.trim()) { setStatus("姓名不可空白。", "danger"); return; }
  if (!d.customer_code.trim()) { setStatus("客戶編號不可空白。", "danger"); return; }
  setBusy(true);
  try {
    const row = await api(`/api/customers/${detailId}`, { method: "PUT", body: JSON.stringify(d) });
    applyDetailHeader(row);
    $("created-banner").classList.add("hidden");
    setStatus("已儲存。", "success");
  } catch (e) { setStatus(e.message, "danger"); }
  finally { setBusy(false); }
}

function ageText(birth) {
  if (!birth) return "—";
  const b = new Date(birth + "T00:00:00"), now = new Date();
  let age = now.getFullYear() - b.getFullYear();
  const m = now.getMonth() - b.getMonth();
  if (m < 0 || (m === 0 && now.getDate() < b.getDate())) age--;
  return `${age} 歲`;
}

function renderResults(rows) {
  $("results-body").innerHTML = rows.map(c => `
    <tr onclick="openDetail(${c.id})">
      <td>${esc(c.customer_code)}</td>
      <td>${esc(c.name)}</td>
      <td>${c.gender === "M" ? "男" : c.gender === "F" ? "女" : "—"}</td>
      <td>${ageText(c.birth_date)}</td>
      <td>${esc(c.phone_mobile) || "—"}</td>
      <td>${esc(c.phone_home) || "—"}</td>
      <td class="addr">${esc([c.mailing_city,c.mailing_district,c.mailing_detail].filter(Boolean).join("")) || "—"}</td>
      <td>${esc(DF.fromISO(c.registration_date ?? "")) || "—"}</td>
    </tr>`).join("");
}

// ── 客戶照片（多張上傳/檢視/刪除；僅明細編輯模式）────────────
async function loadPhotos() {
  if (detailId === null) return;
  try {
    const r = await api(`/api/customers/${detailId}/photos`);
    renderPhotos(r.rows);
  } catch { /* 401 已導回登入 */ }
}

function renderPhotos(rows) {
  // 有照片＝展開照片區（拉到最上方後的預設）；無照片維持收起
  document.querySelector(".photo-section").classList.toggle("collapsed", rows.length === 0);
  $("photo-count").textContent = rows.length ? `（${rows.length} 張）` : "";
  $("photo-grid").innerHTML = rows.length ? rows.map(p => `
    <div class="photo-item">
      <img src="/api/photos/${p.id}" alt="客戶照片" loading="lazy"
           title="${esc(p.filename ?? "")}‧${esc(DF.fromISODateTime(p.created_at ?? ""))}${p.created_by ? "‧" + esc(p.created_by) : ""}"
           onclick="showPhoto(${p.id})">
      <button type="button" class="photo-del" title="刪除這張照片" onclick="deletePhoto(${p.id})">✕</button>
    </div>`).join("")
    : `<div class="photo-empty">尚無照片，按「上傳照片」新增（可一次選多張）。</div>`;
}

function clearPhotos() {
  $("photo-grid").innerHTML = "";
  $("photo-count").textContent = "";
  setNote("photo-status", "");
}

function showPhoto(pid) {
  openModal("客戶照片", `
    <img src="/api/photos/${pid}" style="max-width:100%; border-radius:6px;">
    <div class="modal-actions"><button class="btn" onclick="closeModal()">關閉</button></div>`);
}

async function deletePhoto(pid) {
  if (!confirm("確定刪除這張照片？刪除後無法復原。")) return;
  try {
    await api(`/api/photos/${pid}`, { method: "DELETE" });
    await loadPhotos();
  } catch (e) { setStatus(e.message, "danger"); }
}

// （shrinkImage 已移至 common.js 供各頁共用）

async function uploadPhoto(file) {
  const body = await shrinkImage(file);
  const res = await fetch(`/api/customers/${detailId}/photos?filename=${encodeURIComponent(file.name)}`, {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": body.type || "image/jpeg" }, body });
  if (res.status === 401) { showLogin(); throw new Error("未登入"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

async function handlePhotoFiles(files) {
  if (!files.length || detailId === null) return;
  let done = 0;
  const errs = [];
  setNote("photo-status", `上傳中…（0/${files.length}）`);
  for (const f of files) {
    try { await uploadPhoto(f); done++; }
    catch (err) { errs.push(`${f.name}：${err.message}`); }
    setNote("photo-status", `上傳中…（${done}/${files.length}）`);
  }
  setNote("photo-status", errs.length ? `完成 ${done} 張；失敗 ${errs.length} 張 — ${errs.join("、")}` : "");
  await loadPhotos();
}

for (const inputId of ["photo-file", "photo-camera"]) {
  $(inputId).addEventListener("change", e => {
    const files = [...e.target.files];
    e.target.value = "";   // 清空才能連續拍照/重選同一檔
    handlePhotoFiles(files);
  });
}

// ── 原始表單影像（單一份；連結存 customers.form_image_id）────
function renderFormImage(row) {
  const id = row && row.form_image_id;
  $("formimg-view").innerHTML = id
    ? `<img src="/api/photos/${id}?t=${Date.now()}" alt="原始表單影像" loading="lazy"
           title="點擊放大檢視" onclick="showPhoto(${id})">`
    : `<div class="formimg-empty">尚無原始表單影像，可按「上傳／回補」加入（僅保留最新一份）。</div>`;
}

// 縮到 1600 後 PUT 到單一份端點；回傳新影像 id
async function putFormImage(file) {
  const blob = await shrinkImage(file);   // 預設 1600，維持清晰
  const res = await fetch(`/api/customers/${detailId}/form-image?filename=${encodeURIComponent(file.name || "客戶資料表.jpg")}`, {
    method: "PUT", credentials: "same-origin",
    headers: { "Content-Type": blob.type || "image/jpeg" }, body: blob });
  if (res.status === 401) { showLogin(); throw new Error("未登入"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data.id;
}

// 明細模式：使用者上傳／回補原始表單影像
async function uploadFormImage(file) {
  if (detailId === null) return;
  setNote("formimg-status", "上傳中…");
  try {
    const id = await putFormImage(file);
    renderFormImage({ form_image_id: id });
    setNote("formimg-status", "");
    loadPhotos();   // 舊份已刪、畫廊排除規則，重整以防殘影
  } catch (e) { setNote("formimg-status", "上傳失敗：" + e.message); }
}

// 原始表單影像：拍照/上傳（單一檔，回補即替換）
for (const inputId of ["formimg-file", "formimg-camera"]) {
  $(inputId).addEventListener("change", e => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) uploadFormImage(file);
  });
}

// ── 客戶待辦事項 ─────────────────────────────────────────────
let todoEditId = null, todoAttTarget = null;
const TODO_PRIORITIES = [["", "—"], ["high", "高"], ["normal", "中"], ["low", "低"]];

async function loadTodos() {
  if (detailId === null) return;
  try {
    await ensureCodes();   // 分類要顯示中文
    const r = await api(`/api/customers/${detailId}/todos`);
    renderTodos(r.rows);
  } catch { /* 401 已導回登入 */ }
}

function clearTodos() { $("todo-list").innerHTML = ""; $("todo-count").textContent = ""; }

function renderTodos(rows) {
  const undone = rows.filter(t => !t.done_at).length;
  $("todo-count").textContent = rows.length ? `（未完成 ${undone} / 共 ${rows.length}）` : "";
  $("todo-list").innerHTML = rows.length ? rows.map(todoItemHtml).join("")
    : `<div class="todo-empty">尚無待辦，按「＋ 新增待辦」加入。</div>`;
}

function todoItemHtml(t) {
  const done = !!t.done_at;
  const overdue = !done && t.due_at && t.due_at.slice(0, 16) < nowLocalMin();
  const cls = "todo-item" + (done ? " done" : "") + (overdue ? " overdue" : "");
  const cat = t.category ? `<span class="todo-badge cat">${esc(codeLabel("todo_category", t.category))}</span>` : "";
  const pri = t.priority === "high" ? `<span class="todo-badge pri-high">高</span>`
            : t.priority === "low" ? `<span class="todo-badge">低</span>` : "";
  const due = t.due_at ? `${overdue ? '<span class="todo-badge overdue">逾期</span>' : ""}⏰ ${esc(DF.fromISODateTime(t.due_at))}` : "";
  const doneInfo = done ? `✔ ${esc(DF.fromISODateTime(t.done_at))}${t.done_by ? "‧" + esc(t.done_by) : ""}` : "";
  const atts = (t.attachments || []).map(a => todoAttHtml(a)).join("");
  return `<div class="${cls}">
    <div class="todo-top">
      ${cat}${pri}
      <span class="todo-desc">${esc(t.description)}</span>
      <div class="todo-acts">
        <button title="${done ? "取消完成" : "標記完成"}" onclick="toggleTodoDone(${t.id}, ${!done})">${done ? "↩︎" : "✔ 完成"}</button>
        <button title="編輯" onclick="openTodoModal(${t.id})">✎</button>
        <button title="加附件" onclick="pickTodoAttachment(${t.id})">📎</button>
        <button title="刪除" onclick="deleteTodoItem(${t.id})">✕</button>
      </div>
    </div>
    <div class="todo-meta">
      ${due ? `<span>${due}</span>` : ""}
      ${doneInfo ? `<span>${doneInfo}</span>` : ""}
      <span>建立 ${esc(DF.fromISODateTime(t.created_at))}${t.created_by ? "‧" + esc(t.created_by) : ""}</span>
    </div>
    ${atts ? `<div class="todo-atts">${atts}</div>` : ""}
  </div>`;
}

function todoAttHtml(a) {
  const isPdf = (a.mime || "").includes("pdf");
  const inner = isPdf
    ? `<div class="pdf" onclick="window.open('/api/todo-attachments/${a.id}')">PDF</div>`
    : `<img src="/api/todo-attachments/${a.id}" loading="lazy" onclick="window.open('/api/todo-attachments/${a.id}')">`;
  return `<div class="todo-att">${inner}<button class="att-del" title="刪除附件" onclick="delTodoAttachment(${a.id})">✕</button></div>`;
}

// 本地時間 "YYYY-MM-DD HH:MM"（比較逾期用）
function nowLocalMin() {
  const d = new Date(), p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function openTodoModal(tid) {
  await ensureCodes();
  todoEditId = tid || null;
  let t = { category: "", description: "", priority: "", due_at: "" };
  if (tid) {
    const list = await api(`/api/customers/${detailId}/todos`);
    t = list.rows.find(x => x.id === tid) || t;
  }
  const catOpts = `<option value="">—</option>` +
    (CODES["todo_category"] || []).map(c => `<option value="${c.key}"${c.key===t.category?" selected":""}>${esc(c.value)}</option>`).join("");
  const priOpts = TODO_PRIORITIES.map(([k, v]) => `<option value="${k}"${k===(t.priority||"")?" selected":""}>${v}</option>`).join("");
  let dueDate = "", dueTime = "";
  if (t.due_at) { const s = t.due_at.slice(0, 16); dueDate = DF.fromISO(s.slice(0,10)); dueTime = s.slice(11,16); }
  openModal(tid ? "編輯待辦" : "新增待辦", `
    <div class="modal-field"><label>說明 <span style="color:var(--brand)">*</span></label>
      <textarea id="td-desc" rows="3">${esc(t.description)}</textarea></div>
    <div class="modal-row">
      <div class="modal-field"><label>分類</label><select id="td-cat">${catOpts}</select></div>
      <div class="modal-field"><label>優先度</label><select id="td-pri">${priOpts}</select></div>
    </div>
    <div class="modal-row">
      <div class="modal-field"><label>時間（${DF.hint}，可空）</label>
        <input id="td-date" placeholder="${DF.placeholder}" value="${dueDate}"></div>
      <div class="modal-field"><label>時刻</label><input id="td-time" type="time" value="${dueTime}"></div>
    </div>
    <div id="td-err" class="banner danger hidden" style="margin:.4rem 0"></div>
    <div class="modal-actions">
      <button class="btn" onclick="saveTodo()">${tid ? "儲存" : "新增"}</button>
      <button class="btn outline" onclick="closeModal()">取消</button>
    </div>`);
  attachDateMask($("td-date"));   // 待辦時間欄也自動補斜線
}

async function saveTodo() {
  const desc = $("td-desc").value.trim();
  if (!desc) { showTdErr("請填寫說明。"); return; }
  let due_at = null;
  const dv = $("td-date").value.trim();
  if (dv) {
    const iso = DF.tryToISO(dv);
    if (iso === null) { showTdErr(`時間日期格式不正確，請輸入${DF.placeholder}。`); return; }
    due_at = iso + "T" + ($("td-time").value || "00:00");
  }
  const body = { description: desc, category: $("td-cat").value || null,
                 priority: $("td-pri").value || null, due_at };
  try {
    if (todoEditId) await api(`/api/todos/${todoEditId}`, { method: "PUT", body: JSON.stringify(body) });
    else await api(`/api/customers/${detailId}/todos`, { method: "POST", body: JSON.stringify(body) });
    closeModal();
    await loadTodos();   // 後端以 created_at 遞減回傳，新筆自動在第 1 筆
  } catch (e) { showTdErr(e.message); }
}
function showTdErr(msg) { const el = $("td-err"); el.textContent = msg; el.classList.remove("hidden"); }

async function toggleTodoDone(tid, done) {
  try { await api(`/api/todos/${tid}`, { method: "PUT", body: JSON.stringify({ done }) }); await loadTodos(); }
  catch (e) { setStatus(e.message, "danger"); }
}

async function deleteTodoItem(tid) {
  if (!confirm("確定刪除這筆待辦？其附件也會一併移除。")) return;
  try { await api(`/api/todos/${tid}`, { method: "DELETE" }); await loadTodos(); }
  catch (e) { setStatus(e.message, "danger"); }
}

function pickTodoAttachment(tid) { todoAttTarget = tid; $("todo-att-file").click(); }

async function uploadTodoAttachment(tid, file) {
  try {
    const isImg = (file.type || "").startsWith("image/");
    const body = isImg ? await shrinkImage(file) : file;   // 圖片縮 1600；PDF 原樣
    const res = await fetch(`/api/todos/${tid}/attachments?filename=${encodeURIComponent(file.name)}`, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": body.type || file.type || "application/octet-stream" }, body });
    if (res.status === 401) { showLogin(); return; }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    await loadTodos();
  } catch (e) { setStatus("附件上傳失敗：" + e.message, "danger"); }
}

async function delTodoAttachment(aid) {
  if (!confirm("刪除這個附件？")) return;
  try { await api(`/api/todo-attachments/${aid}`, { method: "DELETE" }); await loadTodos(); }
  catch (e) { setStatus(e.message, "danger"); }
}

$("todo-att-file").addEventListener("change", e => {
  const file = e.target.files[0]; e.target.value = "";
  if (file && todoAttTarget) uploadTodoAttachment(todoAttTarget, file);
});

// ── 紙本表單 OCR 匯入（本機 GPU 辨識 → 自動填表 → 人工校對）──
let ocrPendingPhoto = null;   // 辨識用的原稿；建檔成功後自動存入原始表單影像
// OCR 上傳影像的長邊上限（px）。2026-07-13 實測：換 Instruct 模型後推論僅 ~13s，
// 影像大小對速度幾乎無影響（prefill 本就 <2s），但對手寫準確度有影響（1280 曾把
// 姓名多讀一字）。故維持 1600 拚準確度；速度靠模型（見 app.py _OCR_MODEL）而非縮圖。
const OCR_MAX_EDGE = 1600;

let ocrTimerId = null, ocrBusy = false;
window.addEventListener("beforeunload", e => {
  if (ocrBusy) { e.preventDefault(); e.returnValue = ""; }  // 辨識中關閉/重整前警告
});
function showOcrOverlay() {
  ocrBusy = true;
  const t0 = Date.now();
  const timer = $("ocr-timer"), hint = $("ocr-hint");
  const tick = () => {
    const s = Math.floor((Date.now() - t0) / 1000);
    timer.textContent = `已處理 ${s} 秒`;
    // 分段安撫文字：手寫多、圖大時較久
    if (s >= 45) hint.innerHTML = "手寫內容較多，仍在辨識中，請再稍候…<br>處理期間請勿離開或關閉本頁。";
  };
  tick();
  ocrTimerId = setInterval(tick, 1000);
  $("ocr-hint").innerHTML = "正在讀取表單內容，完成後會自動填入欄位。<br>處理期間請勿離開或關閉本頁。";
  $("ocr-overlay").classList.remove("hidden");
}
function hideOcrOverlay() {
  ocrBusy = false;
  clearInterval(ocrTimerId); ocrTimerId = null;
  $("ocr-overlay").classList.add("hidden");
}

async function handleOcrFile(file) {
  hideStatus();
  const btns = [$("ocr-camera-btn"), $("ocr-file-btn")];
  btns.forEach(b => { b.disabled = true; });
  $("ocr-camera-btn").textContent = "辨識中…";
  showOcrOverlay();
  try {
    const blob = await shrinkImage(file, OCR_MAX_EDGE);
    const res = await fetch("/api/customers/ocr", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": blob.type || "image/jpeg" }, body: blob });
    if (res.status === 401) { showLogin(); throw new Error("未登入"); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    const n = applyOcr(data);
    // 存檔留原始檔（清晰），OCR 只用上面的縮小版
    ocrPendingPhoto = { file, name: file.name || "客戶資料表.jpg" };
    const unc = (data.uncertain || []).length;
    setNote("ocr-status", "");
    setStatus(`辨識完成，已填入 ${n} 個欄位` +
      (unc ? `；其中 ${unc} 個欄位辨識沒把握（黃底），請對照原稿核對` : "") +
      "。確認無誤後按「建檔」，原稿照片會自動存入客戶資料。", "success");
  } catch (e) {
    setNote("ocr-status", "");
    setStatus("辨識失敗：" + e.message, "danger");
  } finally {
    hideOcrOverlay();
    btns.forEach(b => { b.disabled = false; });
    $("ocr-camera-btn").textContent = "📷 拍照匯入";
  }
}

function applyOcr(data) {
  const f = data.fields || {};
  let n = 0;
  clearOcrMarks();
  for (const k of TEXT_FIELDS) if (f[k] != null && k !== "notes") { $("f-"+k).value = f[k]; n++; }
  for (const k of DATE_FIELDS) if (f[k]) { $("f-"+k).value = DF.fromISO(f[k]); n++; }  // API 回西元 ISO → 民國顯示
  if (f.gender) { setSeg("seg-gender", f.gender); n++; }
  if (f.subsidy_applied) { setSeg("seg-subsidy", f.subsidy_applied); n++; }
  for (const k of data.uncertain || []) $("f-"+k)?.classList.add("ocr-uncertain");
  refreshSoftChecks();
  refreshDup();
  return n;
}

function clearOcrMarks() {
  document.querySelectorAll(".ocr-uncertain").forEach(el => el.classList.remove("ocr-uncertain"));
}

// ── OCR GPU 載入狀態機（未熱機前鎖住拍照/匯入，載入完成才解鎖）──────
let ocrPollTimer = null;

function setOcrState(state, extra) {
  const load = $("ocr-load-btn"), cam = $("ocr-camera-btn"), file = $("ocr-file-btn");
  const importable = state === "ready";
  file.disabled = !importable;
  cam.disabled = !importable;
  // 已載入＝按鈕整顆隱藏（不佔位）；只有未載入/載入中才顯示。
  // 伺服器休眠卸載後，每分鐘的 refreshOcrState 會回報 idle → 按鈕自動重現。
  load.classList.toggle("hidden", state === "ready");
  if (state === "loading") {
    load.disabled = true;
    load.textContent = "載入中…" + (extra?.elapsed ? ` ${extra.elapsed}s` : "");
    setNote("ocr-status", "GPU 熱機中，約需 2–4 分鐘（載入模型到顯示卡），完成後即可拍照/匯入。");
  } else if (state === "ready") {
    if ($("ocr-status").textContent.startsWith("GPU 熱機")) setNote("ocr-status", "");
  } else {  // idle / error
    load.disabled = false;
    load.textContent = "🔌 載入模型";
    if (state === "error") setStatus("模型載入失敗：" + (extra?.error || "請重試"), "danger");
  }
}

async function warmupOcr() {
  try {
    const r = await api("/api/ocr/warmup", { method: "POST" });
    if (r.state === "ready") { setOcrState("ready"); return; }
    setOcrState("loading");
    pollOcrStatus();
  } catch (e) { setStatus(e.message, "danger"); }
}

async function pollOcrStatus() {
  clearTimeout(ocrPollTimer);
  let s;
  try { s = await api("/api/ocr/status"); } catch { return; }
  setOcrState(s.state, s);
  if (s.state === "loading") ocrPollTimer = setTimeout(pollOcrStatus, 4000);
}

// 進入建檔模式時同步一次狀態（別的分頁/前一次已載入就直接解鎖）
async function refreshOcrState() {
  try {
    const s = await api("/api/ocr/status");
    setOcrState(s.state === "ready" ? "ready" : "idle");
    if (s.state === "loading") { setOcrState("loading"); pollOcrStatus(); }
  } catch {}
}

// 伺服器休眠後「已載入」自動退回「載入模型」並鎖回拍照/匯入（每分鐘同步）
setInterval(() => {
  if (ocrBusy || ocrPollTimer) return;                 // 辨識中/載入輪詢中不打擾
  if ($("view-form").classList.contains("hidden")) return; // 在查詢結果頁不查
  refreshOcrState();
}, 60000);

for (const inputId of ["ocr-camera", "ocr-file"]) {
  $(inputId).addEventListener("change", e => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) handleOcrFile(file);
  });
}
// 使用者修改欄位即解除黃框（已人工確認）
document.querySelectorAll("#view-form input, #view-form textarea").forEach(el =>
  el.addEventListener("input", () => el.classList.remove("ocr-uncertain")));

// 桌機（滑鼠為主要指標）沒有相機可拍，隱藏拍照鈕；手機/平板顯示
if (!matchMedia("(pointer: coarse)").matches) {
  $("ocr-camera-btn").classList.add("hidden");
  $("photo-camera-btn").classList.add("hidden");
  $("formimg-camera-btn").classList.add("hidden");
}

// ── 重複客戶偵測（姓名/手機/身分證 失焦時觸發）──────────────
async function refreshDup() {
  confirmCreate = false;
  if (detailId !== null) return;  // 明細編輯模式不偵測
  // 直接讀三個文字欄位（不經 formData：日期欄可能尚在輸入中、還不是合法格式）
  const name = $("f-name").value, phone_mobile = $("f-phone_mobile").value,
        national_id = $("f-national_id").value;
  if (!name.trim() && !phone_mobile.trim() && !national_id.trim()) { hideDup(); return; }
  try {
    const r = await api("/api/customers/dup", { method: "POST",
      body: JSON.stringify({ name, phone_mobile, national_id }) });
    if (r.rows.length === 0) { hideDup(); return; }
    $("dup-count").textContent = r.rows.length;
    $("dup-list").innerHTML = r.rows.map(x => `
      <li><a href="javascript:openDetail(${x.id})">${x.name}</a>
        <span class="dup-meta">${x.customer_code}${x.phone_mobile ? "‧"+x.phone_mobile : ""}${x.birth_date ? "‧"+DF.fromISO(x.birth_date) : ""}</span>
      </li>`).join("");
    $("dup-panel").classList.remove("hidden");
  } catch { /* 離線等狀況不擋輸入 */ }
}
function hideDup() { $("dup-panel").classList.add("hidden"); $("dup-list").innerHTML = ""; }

for (const f of ["name","phone_mobile","national_id"])
  $("f-"+f).addEventListener("blur", refreshDup);

// ── 軟性檢核：只提醒、不擋存檔（同 Blazor 版規則）───────────
const LETTER_VALUES = [10,11,12,13,14,15,16,17,34,18,19,20,21,
                       22,35,23,24,25,26,27,28,29,32,30,31,33];
function nidChecksum(id) {
  const letter = LETTER_VALUES[id.charCodeAt(0) - 65];
  let sum = Math.floor(letter / 10) + (letter % 10) * 9;
  const w = [8,7,6,5,4,3,2,1,1];
  for (let i = 0; i < 9; i++) sum += (id.charCodeAt(i + 1) - 48) * w[i];
  return sum % 10 === 0;
}
function nidWarning() {
  const id = $("f-national_id").value.trim().toUpperCase();
  if (!id) return null;
  if (!/^[A-Z][1289][0-9]{8}$/.test(id))
    return "格式不符：應為 1 個大寫字母 + 9 碼數字（新式居留證同格式）。仍可儲存。";
  if (!nidChecksum(id)) return "檢查碼不正確，請再核對一次。仍可儲存。";
  const idMale = id[1] === "1" || id[1] === "8";
  const g = getSeg("seg-gender");
  if (g === "M" && !idMale) return "身分證第 2 碼顯示為女性，與「性別：男」不一致。";
  if (g === "F" && idMale) return "身分證第 2 碼顯示為男性，與「性別：女」不一致。";
  return null;
}
function mobileWarning() {
  const m = $("f-phone_mobile").value.trim();
  if (!m || /^09\d{8}$/.test(m.replaceAll("-",""))) return null;
  return "台灣手機通常是 09 開頭共 10 碼，請確認。仍可儲存（市話請填市話欄）。";
}
function emailWarning() {
  const e = $("f-email").value.trim();
  if (!e || /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(e)) return null;
  return "Email 格式看起來不完整，請確認。仍可儲存。";
}
function birthInfo() {
  const v = $("f-birth_date").value;
  if (!v.trim()) return null;
  const iso = DF.tryToISO(v);   // 即時回饋：民國輸入→西元確認
  if (iso === null) return `日期格式不正確，請輸入${DF.placeholder}`;
  const b = new Date(iso + "T00:00:00"), now = new Date();
  let age = now.getFullYear() - b.getFullYear();
  const m = now.getMonth() - b.getMonth();
  if (m < 0 || (m === 0 && now.getDate() < b.getDate())) age--;
  return `西元 ${b.getFullYear()} 年‧現年 ${age} 歲`;
}
function refreshSoftChecks() {
  setNote("warn-nid", nidWarning());
  setNote("warn-mobile", mobileWarning());
  setNote("warn-email", emailWarning());
  setNote("birth-info", birthInfo());
}
for (const f of ["national_id","phone_mobile","email","birth_date"])
  $("f-"+f).addEventListener("input", refreshSoftChecks);
$("seg-gender").addEventListener("click", refreshSoftChecks);

// ── 其他 ─────────────────────────────────────────────────────
function copyHousehold() {
  for (const p of ["city","district","village","detail"])
    $("f-mailing_"+p).value = $("f-household_"+p).value;
}
function copyMailing() {
  for (const p of ["city","district","village","detail"])
    $("f-household_"+p).value = $("f-mailing_"+p).value;
}
function setBusy(b) {
  for (const id of ["btn-create","btn-query","btn-save"]) $(id).disabled = b;
  document.querySelectorAll(".fq-btn").forEach(el => el.disabled = b);
}

// ── 開機：header → 建檔模式；hash #detail/<id> 直開明細 ─────
// ══ 交易紀錄（客戶明細內嵌；與 /purchases 共用同組 API）══════════
const TX_TYPE = { purchase: "購買", rental: "租借", repair: "維修", return: "退貨" };
const TX_EAR = { left: "左耳", right: "右耳", both: "雙耳" };
const TX_PAY = { unpaid: "未付", partial: "部分付款", paid: "已付清" };
let txMenuOK = false, txRows = [], txProducts = null;

function clearCustTx() {
  $("tx-list").innerHTML = ""; $("tx-count").textContent = "";
  document.querySelector(".tx-section").classList.add("hidden");
}

async function loadCustTx() {
  if (!txMenuOK || detailId === null) return;
  document.querySelector(".tx-section").classList.remove("hidden");
  try {
    const r = await api(`/api/purchases?customer_id=${detailId}`);
    txRows = r.rows;
    renderCustTx();
  } catch (e) { $("tx-list").innerHTML = `<p class="hint">${esc(e.message)}</p>`; }
}

function renderCustTx() {
  $("tx-count").textContent = txRows.length ? `（${txRows.length}）` : "";
  $("tx-list").innerHTML = txRows.length ? `<div class="table-wrap"><table>
    <thead><tr><th>交易日</th><th>商品</th><th>類型</th><th>數量</th>
      <th class="price-col">單價</th><th>付款</th><th>保固到期</th></tr></thead>
    <tbody>${txRows.map(p => `
      <tr onclick="openTxModal(${p.id})" style="cursor:pointer;">
        <td>${esc(DF.fromISO(p.transaction_date)) || "—"}</td>
        <td>${p.product_id ? esc(`${p.brand} ${p.model_name}`) : "—"}</td>
        <td>${TX_TYPE[p.transaction_type] || esc(p.transaction_type)}</td>
        <td>${p.quantity ?? 1}</td>
        <td class="price-col">${p.unit_price != null ? Number(p.unit_price).toLocaleString() : "—"}</td>
        <td>${TX_PAY[p.payment_status] || esc(p.payment_status)}</td>
        <td>${esc(DF.fromISO(p.warranty_end_date ?? "")) || "—"}</td>
      </tr>`).join("")}</tbody></table></div>`
    : `<p class="hint">尚無交易紀錄，按「新增交易」建立第一筆。</p>`;
}

async function ensureTxProducts() {
  if (txProducts) return txProducts;
  try { txProducts = (await api("/api/products")).rows; } catch { txProducts = []; }
  return txProducts;
}

async function openTxModal(txId) {
  await ensureTxProducts();
  const p = txId ? txRows.find(x => x.id === txId) : null;
  const sel = (id, map, cur, blank) =>
    `<select id="${id}">${blank ? `<option value="">${blank}</option>` : ""}` +
    Object.entries(map).map(([k, v]) => `<option value="${k}"${cur === k ? " selected" : ""}>${v}</option>`).join("") + `</select>`;
  const prodOpts = `<option value="">未指定商品</option>` + txProducts.map(pr =>
    `<option value="${pr.id}"${p && p.product_id === pr.id ? " selected" : ""}>${esc(pr.brand + " " + pr.model_name)}</option>`).join("");
  openModal(txId ? "編輯交易" : "新增交易", `
    <div class="form-grid">
      <div class="field wide"><label for="tx-product_id">商品</label><select id="tx-product_id">${prodOpts}</select></div>
      <div class="field"><label for="tx-transaction_type">交易類型</label>${sel("tx-transaction_type", TX_TYPE, p ? p.transaction_type : "purchase")}</div>
      <div class="field"><label for="tx-ear_side">配戴耳別</label>${sel("tx-ear_side", TX_EAR, p ? p.ear_side : "", "—")}</div>
      <div class="field"><label for="tx-serial_number">機身序號</label><input id="tx-serial_number" maxlength="50" value="${p ? esc(p.serial_number) : ""}"></div>
      <div class="field"><label for="tx-quantity">數量</label><input id="tx-quantity" inputmode="numeric" maxlength="4" value="${p ? (p.quantity ?? 1) : 1}"></div>
      <div class="field price-col"><label for="tx-unit_price">成交單價</label><input id="tx-unit_price" inputmode="decimal" value="${p && p.unit_price != null ? p.unit_price : ""}"></div>
      <div class="field"><label for="tx-payment_status">付款狀態</label>${sel("tx-payment_status", TX_PAY, p ? p.payment_status : "unpaid")}</div>
      <div class="field"><label for="tx-transaction_date">交易日期<span class="lh">（${DF.hint}）</span></label><input id="tx-transaction_date" maxlength="10" value="${p ? DF.fromISO(p.transaction_date) : DF.fromISO(today())}"></div>
      <div class="field"><label for="tx-warranty_start_date">保固起始日<span class="lh">（${DF.hint}）</span></label><input id="tx-warranty_start_date" maxlength="10" value="${p ? DF.fromISO(p.warranty_start_date ?? "") : ""}"></div>
      <div class="field"><label for="tx-warranty_end_date">保固到期日<span class="lh">（${DF.hint}）</span></label><input id="tx-warranty_end_date" maxlength="10" value="${p ? DF.fromISO(p.warranty_end_date ?? "") : ""}"></div>
      <div class="field wide"><label for="tx-notes">備註</label><textarea id="tx-notes" rows="2">${p ? esc(p.notes) : ""}</textarea></div>
    </div>
    <div class="modal-actions">
      ${txId ? `<button class="btn ghost" onclick="deleteTx(${txId})">刪除</button>` : ""}
      <button class="btn" onclick="saveTx(${txId || 0})">${txId ? "儲存變更" : "建立交易"}</button>
      <button class="btn outline" onclick="closeModal()">取消</button>
    </div>`);
  for (const f of ["transaction_date", "warranty_start_date", "warranty_end_date"]) attachDateMask($("tx-" + f));
}

function txModalData() {
  const d = {
    product_id: $("tx-product_id").value,
    transaction_type: $("tx-transaction_type").value,
    ear_side: $("tx-ear_side").value,
    serial_number: $("tx-serial_number").value,
    quantity: $("tx-quantity").value,
    unit_price: $("tx-unit_price").value,
    payment_status: $("tx-payment_status").value,
    notes: $("tx-notes").value,
  };
  for (const f of ["transaction_date", "warranty_start_date", "warranty_end_date"])
    d[f] = dfToISO($("tx-" + f).value, f);
  return d;
}

async function saveTx(txId) {
  if (!$("tx-transaction_date").value.trim()) { setStatus("「交易日期」為必填。", "danger"); return; }
  let d;
  try { d = txModalData(); } catch (e) { setStatus(e.message, "danger"); return; }
  try {
    if (txId) await api(`/api/purchases/${txId}`, { method: "PUT", body: JSON.stringify(d) });
    else { d.customer_id = detailId; await api("/api/purchases", { method: "POST", body: JSON.stringify(d) }); }
    closeModal();
    setStatus("交易已儲存。", "success");
    await loadCustTx();
  } catch (e) { setStatus(e.message, "danger"); }
}

async function deleteTx(txId) {
  if (!confirm("確定刪除這筆交易？")) return;
  try {
    await api(`/api/purchases/${txId}`, { method: "DELETE" });
    closeModal();
    setStatus("交易已刪除。", "success");
    await loadCustTx();
  } catch (e) { setStatus(e.message, "danger"); }
}

initHeader().then(me => {
  txMenuOK = (me.menus || []).includes("purchases");
  enterEntryMode();
  const m = location.hash.match(/^#detail\/(\d+)$/);
  if (m) openDetail(+m[1]);
}).catch(() => {});
