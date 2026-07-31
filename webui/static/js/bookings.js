/* bookings.js — 預約留言收單頁（LINE 線上預約；輕量版，不進行事曆）
   依賴 common.js：$/esc/api/DF/setStatus/setSeg/initHeader */
"use strict";

const WD = ["日", "一", "二", "三", "四", "五", "六"];
let bkStatus = "new";

const ST_LABEL = { new: "🟡 待處理", handled: "✅ 已處理", cancelled: "❌ 已取消" };
// 客戶在 LINE 提醒訊息上按「確認前往」（confirm_status）
const CF_LABEL = { confirmed: "✅ 確認前往" };

function replyCell(b) {
  const cf = CF_LABEL[b.confirm_status];
  if (cf) return `${cf}<span class="lh">‧${esc(DF.fromISODateTime(b.confirmed_at ?? ""))}</span>`;
  if (b.reminded_at) return `<span class="hint">已提醒‧未回覆</span>`;
  return `<span class="hint">—</span>`;
}

function slotLabel(dIso, hour) {
  if (!dIso) return "—";
  const d = new Date(dIso + "T00:00:00");
  return `${DF.fromISO(dIso)}（${WD[d.getDay()]}）${String(hour).padStart(2, "0")}:00`;
}

async function loadBookings() {
  try {
    const r = await api("/api/bookings" + (bkStatus ? `?status=${bkStatus}` : ""));
    renderBookings(r.rows);
  } catch (e) { setStatus(e.message, "danger"); }
}

function renderBookings(rows) {
  $("bk-count").textContent = `（${rows.length} 筆）`;
  $("bk-body").innerHTML = rows.length ? rows.map(b => `
    <tr${b.status !== "new" ? ' style="opacity:.6"' : ""}>
      <td>${esc(DF.fromISODateTime(b.created_at ?? ""))}</td>
      <td>${esc(b.contact_name)}</td>
      <td>${esc(b.phone)}</td>
      <td><b>${slotLabel(b.req_date, b.req_hour)}</b></td>
      <td class="addr">${esc(b.note ?? "") || "—"}</td>
      <td>${b.customer_id
            ? `<a href="/customers#detail/${b.customer_id}">${esc(b.customer_name)}‧${esc(b.customer_code)}</a>`
            : esc(b.line_display_name ?? "") || "—"}</td>
      <td>${replyCell(b)}</td>
      <td>${ST_LABEL[b.status] || esc(b.status)}${b.handled_by ? `<span class="lh">‧${esc(b.handled_by)}</span>` : ""}</td>
      <td>
        ${b.status === "new" ? `
          <button type="button" class="btn sm" onclick="setBooking(${b.id},'handled')">已處理</button>
          <button type="button" class="btn sm outline" onclick="setBooking(${b.id},'cancelled')">取消</button>`
        : `<button type="button" class="btn sm outline" onclick="setBooking(${b.id},'new')">還原待處理</button>`}
        ${b.line_user_id && b.status !== "cancelled"
          ? `<button type="button" class="btn sm outline" onclick="remindBooking(${b.id},${b.reminded_at ? "true" : "false"})">${b.reminded_at ? "重送提醒" : "送提醒"}</button>`
          : ""}
      </td>
    </tr>`).join("")
    : `<tr><td colspan="9" class="hint">目前沒有${bkStatus === "new" ? "待處理的" : ""}預約留言。</td></tr>`;
}

async function setBooking(bid, st) {
  try {
    await api(`/api/bookings/${bid}`, { method: "PUT", body: JSON.stringify({ status: st }) });
    await loadBookings();
  } catch (e) { setStatus(e.message, "danger"); }
}

async function remindBooking(bid, sent) {
  if (sent && !confirm("這筆已送過提醒，重送會再佔用一則推播額度，確定要重送嗎？")) return;
  try {
    const r = await api(`/api/bookings/${bid}/remind`,
      { method: "POST", body: JSON.stringify({ force: !!sent }) });
    setStatus(r.msg || "提醒已送出。", "success");
    await loadBookings();
  } catch (e) { setStatus(e.message, "danger"); }
}

// 狀態頁籤
$("bk-filter").querySelectorAll("button").forEach(btn => {
  btn.onclick = () => { setSeg("bk-filter", btn.dataset.v); bkStatus = btn.dataset.v; loadBookings(); };
});
setSeg("bk-filter", "new");

// 開機
initHeader().then(() => loadBookings()).catch(() => {});
