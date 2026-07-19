/* staff.js — 人員管理頁（users；選單權限 staff＝僅 admin）
   密碼不可看只可重設；人員照片單張（回補即替換，檔案落磁碟 staff/）。
   依賴 common.js：$/esc/api/DF/setStatus/openModal/closeModal/shrinkImage/
                  CODES/ensureCodes/codeLabel/initHeader */
"use strict";

let staffRows = [];
let photoTarget = null;   // 待上傳照片的人員 id
let myUid = null;         // 目前登入者（不能刪自己/改自己權限）

async function loadStaff() {
  const r = await api("/api/staff");
  staffRows = r.rows;
  renderStaff();
}

function photoCell(u) {
  return u.has_photo
    ? `<img class="staff-avatar" src="/api/staff/${u.id}/photo?t=${Date.now()}" alt="${esc(u.realname)}"
           onclick="event.stopPropagation(); showStaffPhoto(${u.id})">`
    : `<span class="staff-avatar staff-avatar-empty">👤</span>`;
}

function renderStaff() {
  $("staff-count").textContent = `（${staffRows.length} 位）`;
  $("staff-body").innerHTML = staffRows.map(u => `
    <tr onclick="openStaffModal(${u.id})">
      <td>${photoCell(u)}</td>
      <td>${esc(u.username)}${u.id === myUid ? '<span class="lh">（我）</span>' : ""}</td>
      <td>${esc(u.realname) || "—"}</td>
      <td>${esc(codeLabel("level", u.level))}</td>
      <td>${esc(DF.fromISODateTime(u.cdate ?? "")) || "—"}</td>
      <td>
        <button type="button" class="btn sm outline" onclick="event.stopPropagation(); pickStaffPhoto(${u.id})">📷 照片</button>
        <button type="button" class="btn sm outline" onclick="event.stopPropagation(); deleteStaff(${u.id})">刪除</button>
      </td>
    </tr>`).join("");
}

function showStaffPhoto(uid) {
  openModal("人員照片", `
    <img src="/api/staff/${uid}/photo?t=${Date.now()}" style="max-width:100%; border-radius:6px;">
    <div class="modal-actions">
      <button class="btn outline" onclick="removeStaffPhoto(${uid})">移除照片</button>
      <button class="btn" onclick="closeModal()">關閉</button>
    </div>`);
}

// ── 新增 / 編輯（密碼欄留空＝不變更；不顯示舊密碼）──────────
async function openStaffModal(uid) {
  await ensureCodes();
  const u = uid ? staffRows.find(x => x.id === uid) : null;
  const isEdit = !!u;
  const lvOpts = (CODES.level || []).map(c =>
    `<option value="${c.key}"${isEdit && c.key === u.level ? " selected" : ""}>${esc(c.value)}</option>`).join("");
  openModal(isEdit ? `編輯人員：${esc(u.realname)}` : "新增人員", `
    <div class="modal-row">
      <div class="modal-field"><label>帳號 <span style="color:var(--brand)">*</span></label>
        <input id="st-username" maxlength="45" autocapitalize="none" spellcheck="false"
               value="${esc(isEdit ? u.username : "")}"></div>
      <div class="modal-field"><label>姓名 <span style="color:var(--brand)">*</span></label>
        <input id="st-realname" maxlength="45" value="${esc(isEdit ? u.realname : "")}"></div>
    </div>
    <div class="modal-row">
      <div class="modal-field"><label>權限 <span style="color:var(--brand)">*</span></label>
        <select id="st-level"${isEdit && u.id === myUid ? " disabled title='不能變更自己的權限'" : ""}>${lvOpts}</select></div>
      <div class="modal-field"><label>${isEdit ? "新密碼（留空＝不變更）" : "密碼 *（至少 6 個字元）"}</label>
        <input id="st-password" type="password" autocomplete="new-password"
               placeholder="${isEdit ? "不顯示舊密碼；輸入即重設" : "至少 6 個字元"}"></div>
    </div>
    ${isEdit ? `<div class="modal-field"><label>照片</label>
      <div style="display:flex; gap:.6rem; align-items:center;">
        ${photoCell(u)}
        <button type="button" class="btn sm outline" onclick="pickStaffPhoto(${u.id})">📷 上傳／更換</button>
        ${u.has_photo ? `<button type="button" class="btn sm outline" onclick="removeStaffPhoto(${u.id})">移除</button>` : ""}
      </div></div>` : ""}
    <div id="st-err" class="banner danger hidden" style="margin:.4rem 0"></div>
    <div class="modal-actions">
      <button class="btn" onclick="saveStaff(${isEdit ? u.id : "null"})">${isEdit ? "儲存" : "建立"}</button>
      <button class="btn ghost" onclick="closeModal()">取消</button>
    </div>`);
}

function showStErr(msg) { const el = $("st-err"); el.textContent = msg; el.classList.remove("hidden"); }

async function saveStaff(uid) {
  const body = {
    username: $("st-username").value.trim(),
    realname: $("st-realname").value.trim(),
    level: $("st-level").value,
    password: $("st-password").value,   // 空字串後端視為不變更
  };
  if (!body.username) { showStErr("請填寫帳號。"); return; }
  if (!body.realname) { showStErr("請填寫姓名。"); return; }
  if (!uid && body.password.length < 6) { showStErr("密碼至少 6 個字元。"); return; }
  if (uid && body.password && body.password.length < 6) { showStErr("新密碼至少 6 個字元。"); return; }
  try {
    if (uid) await api(`/api/staff/${uid}`, { method: "PUT", body: JSON.stringify(body) });
    else await api("/api/staff", { method: "POST", body: JSON.stringify(body) });
    closeModal();
    await loadStaff();
    setStatus(uid ? "已儲存。" : "人員已建立。", "success");
  } catch (e) { showStErr(e.message); }
}

async function deleteStaff(uid) {
  const u = staffRows.find(x => x.id === uid);
  if (!u) return;
  if (!confirm(`確定刪除人員「${u.realname}（${u.username}）」？刪除後無法復原。`)) return;
  try {
    await api(`/api/staff/${uid}`, { method: "DELETE" });
    await loadStaff();
    setStatus("已刪除。", "success");
  } catch (e) { setStatus(e.message, "danger"); }
}

// ── 照片：上傳（縮 800px 足夠頭像用）／移除 ─────────────────
function pickStaffPhoto(uid) { photoTarget = uid; $("staff-photo-file").click(); }

async function uploadStaffPhoto(uid, file) {
  try {
    const blob = await shrinkImage(file, 800);
    const res = await fetch(`/api/staff/${uid}/photo`, {
      method: "PUT", credentials: "same-origin",
      headers: { "Content-Type": blob.type || "image/jpeg" }, body: blob });
    if (res.status === 401) { location.href = "/login?next=/staff"; return; }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    closeModal();
    await loadStaff();
    setStatus("照片已更新。", "success");
  } catch (e) { setStatus("照片上傳失敗：" + e.message, "danger"); }
}

async function removeStaffPhoto(uid) {
  if (!confirm("移除這位人員的照片？")) return;
  try {
    await api(`/api/staff/${uid}/photo`, { method: "DELETE" });
    closeModal();
    await loadStaff();
    setStatus("照片已移除。", "success");
  } catch (e) { setStatus(e.message, "danger"); }
}

$("staff-photo-file").addEventListener("change", e => {
  const file = e.target.files[0]; e.target.value = "";
  if (file && photoTarget) uploadStaffPhoto(photoTarget, file);
});

// ── 開機 ─────────────────────────────────────────────────────
initHeader().then(async me => {
  if (!(me.menus || []).includes("staff")) { location.href = "/calendar"; return; }
  myUid = me.uid ?? null;
  await ensureCodes();
  await loadStaff();
}).catch(() => {});
