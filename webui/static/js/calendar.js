/* calendar.js — 行事曆頁（FullCalendar v6 自架 + zh-tw 語系）
   資料源：回訪提醒(reminder)／客戶待辦(todo)／個人(personal)／門市(store)事件。
   手機預設「日」檢視、平板/電腦「月」檢視，工具列可切月/週/日。
   標題以民國年顯示（datesSet 覆寫）；事件分類代碼取自 codes(cal_category)。
   依賴 common.js：$/esc/api/DF/dateLabel/isoDate/today/attachDateMask/openModal/closeModal/
                  setSeg/getSeg/CODES/ensureCodes/codeLabel/initHeader/setStatus */
"use strict";

// 回訪提醒型別/顏色沿用舊系統
const REMINDER_TYPES = {
  follow_up:           { label: "回診",     color: "#1d6fd8" },
  maintenance:         { label: "保養",     color: "#188554" },
  battery_replacement: { label: "電池更換", color: "#c05600" },
  tuning:              { label: "調機",     color: "#6f42c1" },
  other:               { label: "其他",     color: "#5c636a" },
};
const rType = t => REMINDER_TYPES[t] || REMINDER_TYPES.other;
// 其餘資料源以「來源」配色（分類名稱顯示在標題/彈窗）
const SOURCE_COLORS = { todo: "#dc2626", personal: "#0d9488", store: "#7c3aed" };

let fcRows = {};   // "kind-id" → 原始列（彈窗顯示用）

// ── FullCalendar 初始化 ──────────────────────────────────────
const isPhone = matchMedia("(max-width: 760px)").matches;

function rowToEvent(row) {
  const key = row.kind + "-" + row.id;
  fcRows[key] = row;
  if (row.kind === "reminder") {
    const t = rType(row.reminder_type);
    return { id: key, title: `${t.label}‧${row.customer_name}`,
             start: row.scheduled_date, allDay: true,
             backgroundColor: t.color, borderColor: t.color,
             extendedProps: { done: row.status === "completed" } };
  }
  if (row.kind === "todo") {
    const catL = row.category ? codeLabel("todo_category", row.category) : "待辦";
    return { id: key, title: `${catL}‧${row.customer_name}`,
             start: row.due_at.replace(" ", "T"),
             backgroundColor: SOURCE_COLORS.todo, borderColor: SOURCE_COLORS.todo,
             extendedProps: { done: !!row.done_at } };
  }
  // 個人/門市事件
  const color = SOURCE_COLORS[row.scope] || SOURCE_COLORS.personal;
  return { id: key, title: row.title,
           start: row.start_at.replace(" ", "T"),
           end: row.end_at ? row.end_at.replace(" ", "T") : null,
           allDay: !!row.all_day,
           backgroundColor: color, borderColor: color,
           extendedProps: {} };
}

async function fetchEvents(info, success, failure) {
  try {
    await ensureCodes();   // 待辦/事件分類要顯示中文
    const r = await api(`/api/calendar?start=${isoDate(info.start)}&end=${isoDate(info.end)}`);
    fcRows = {};
    success(r.rows.map(rowToEvent));
  } catch (e) { failure(e); }
}

// 標題改民國年（zh-tw 語系原生為西元）
function applyRocTitle(info) {
  const el = document.querySelector(".fc-toolbar-title");
  if (!el) return;
  const v = info.view, s = v.currentStart;
  let txt;
  if (v.type === "dayGridMonth") {
    txt = `${DF.yearLabel(s.getFullYear())} ${s.getMonth() + 1} 月`;
  } else if (v.type === "timeGridDay") {
    txt = dateLabel(isoDate(s));
  } else {   // 週檢視：起–迄
    const e2 = new Date(v.currentEnd); e2.setDate(e2.getDate() - 1);
    txt = `${DF.fromISO(isoDate(s))} – ${DF.fromISO(isoDate(e2))}`;
  }
  el.textContent = txt;
}

const fc = new FullCalendar.Calendar($("fc"), {
  locale: "zh-tw",
  firstDay: 0,                        // 沿用舊版：週日開頭
  initialView: isPhone ? "timeGridDay" : "dayGridMonth",
  headerToolbar: { left: "prev,next today", center: "title",
                   right: "dayGridMonth,timeGridWeek,timeGridDay" },
  // 新增事件改用右下角圓形浮動鈕（calendar.html 的 .fab），工具列不再放「＋新增」
  // 按鈕文字用 zh-tw 語系原生（月/週/天/今天），避免與格內日期的「日」重複
  dayCellContent: arg => arg.dayNumberText.replace("日", ""),  // 月檢視數字去「日」（1日→1）
  height: "auto",
  dayMaxEventRows: 4,                 // 月檢視每格最多 4 列，其餘收合為「還有 N 筆」
  nowIndicator: true,
  scrollTime: "08:00:00",             // 週/日檢視預設捲到早上 8 點
  events: fetchEvents,
  eventClick: onEventClick,
  dateClick: info => {                // 點空白格直接新增（帶入日期/時刻）
    openEventModal(null, { date: info.dateStr.slice(0, 10),
                           time: info.allDay ? "" : info.dateStr.slice(11, 16) });
  },
  datesSet: applyRocTitle,
  eventDidMount: info => {            // 已完成的提醒/待辦淡化＋刪除線
    if (info.event.extendedProps.done) info.el.classList.add("fc-ev-done");
  },
});

// ── 事件點擊 → 各類型彈窗 ────────────────────────────────────
function onEventClick(info) {
  const row = fcRows[info.event.id];
  if (!row) return;
  if (row.kind === "reminder") showReminder(row);
  else if (row.kind === "todo") showTodo(row);
  else openEventModal(row);
}

function showReminder(row) {
  const t = rType(row.reminder_type);
  openModal(`${t.label}提醒`, `
    <div><strong>客戶：</strong>${esc(row.customer_name)}</div>
    <div><strong>電話：</strong>${esc(row.phone) || "—"}</div>
    <div><strong>日期：</strong>${dateLabel(row.scheduled_date)}</div>
    <div><strong>狀態：</strong>${row.status === "completed" ? "已完成" : "待處理"}</div>
    ${row.notes ? `<div><strong>備註：</strong>${esc(row.notes)}</div>` : ""}
    <div class="modal-actions">
      <button class="btn outline" onclick="location.href='/customers#detail/${row.customer_id}'">開啟客戶資料</button>
      <button class="btn ghost" onclick="closeModal()">關閉</button>
    </div>`);
}

function showTodo(row) {
  const catL = row.category ? codeLabel("todo_category", row.category) : "—";
  openModal("客戶待辦", `
    <div><strong>客戶：</strong>${esc(row.customer_name)}</div>
    <div><strong>分類：</strong>${esc(catL)}</div>
    <div><strong>時間：</strong>${esc(DF.fromISODateTime(row.due_at))}</div>
    <div><strong>狀態：</strong>${row.done_at ? "已完成 " + esc(DF.fromISODateTime(row.done_at)) : "待處理"}</div>
    <div><strong>說明：</strong>${esc(row.description)}</div>
    <div class="modal-actions">
      <button class="btn outline" onclick="location.href='/customers#detail/${row.customer_id}'">開啟客戶資料</button>
      <button class="btn ghost" onclick="closeModal()">關閉</button>
    </div>`);
}

// ── 個人/門市事件：新增/編輯/刪除 ────────────────────────────
async function openEventModal(row, preset) {
  await ensureCodes();
  const isEdit = !!row;
  const catOpts = `<option value="">—</option>` +
    (CODES.cal_category || []).map(c =>
      `<option value="${c.key}"${isEdit && c.key === row.category ? " selected" : ""}>${esc(c.value)}</option>`).join("");
  let d, t1 = "", t2 = "";
  if (isEdit) {
    d = DF.fromISO(row.start_at.slice(0, 10));
    t1 = row.all_day ? "" : row.start_at.slice(11, 16);
    t2 = row.end_at ? row.end_at.slice(11, 16) : "";
  } else {
    d = DF.fromISO(preset?.date || today());
    t1 = preset?.time || "";
  }
  openModal(isEdit ? "編輯行事曆" : "新增行事曆", `
    <div class="modal-row">
      <div class="modal-field"><label>類型</label>
        <div class="seg" id="ev-scope" data-value="">
          <button data-v="personal">個人</button><button data-v="store">門市</button>
        </div></div>
      <div class="modal-field"><label>分類</label><select id="ev-cat">${catOpts}</select></div>
    </div>
    <div class="modal-field"><label>標題 <span style="color:var(--brand)">*</span></label>
      <input id="ev-title" maxlength="200" value="${esc(isEdit ? row.title : "")}"></div>
    <div class="modal-row">
      <div class="modal-field"><label>日期（${DF.hint}）</label>
        <input id="ev-date" placeholder="${DF.placeholder}" value="${d}"></div>
      <div class="modal-field"><label>開始<span class="lh">（空＝全天）</span></label>
        <input id="ev-time" type="time" value="${t1}"></div>
      <div class="modal-field"><label>結束</label><input id="ev-time2" type="time" value="${t2}"></div>
    </div>
    <div class="modal-field"><label>說明</label>
      <textarea id="ev-desc" rows="2">${esc(isEdit ? (row.description || "") : "")}</textarea></div>
    <div id="ev-err" class="banner danger hidden" style="margin:.4rem 0"></div>
    <div class="modal-actions">
      ${isEdit ? `<button class="btn outline" onclick="deleteEvent(${row.id})">刪除</button>` : ""}
      <button class="btn" onclick="saveEvent(${isEdit ? row.id : "null"})">${isEdit ? "儲存" : "新增"}</button>
      <button class="btn ghost" onclick="closeModal()">取消</button>
    </div>`);
  attachDateMask($("ev-date"));
  $("ev-scope").querySelectorAll("button").forEach(b =>
    b.onclick = () => setSeg("ev-scope", b.dataset.v));
  setSeg("ev-scope", isEdit ? row.scope : "personal");
}

function showEvErr(msg) { const el = $("ev-err"); el.textContent = msg; el.classList.remove("hidden"); }

async function saveEvent(id) {
  const title = $("ev-title").value.trim();
  if (!title) { showEvErr("請填寫標題。"); return; }
  const iso = DF.tryToISO($("ev-date").value.trim());
  if (!iso) { showEvErr(`日期格式不正確，請輸入${DF.placeholder}。`); return; }
  const t1 = $("ev-time").value, t2 = $("ev-time2").value;
  const body = {
    scope: getSeg("ev-scope") || "personal",
    category: $("ev-cat").value || null,
    title,
    description: $("ev-desc").value.trim() || null,
    start_at: iso + "T" + (t1 || "00:00"),
    end_at: t2 ? iso + "T" + t2 : null,
    all_day: !t1,
  };
  try {
    if (id) await api(`/api/events/${id}`, { method: "PUT", body: JSON.stringify(body) });
    else await api("/api/events", { method: "POST", body: JSON.stringify(body) });
    closeModal();
    fc.refetchEvents();
  } catch (e) { showEvErr(e.message); }
}

async function deleteEvent(id) {
  if (!confirm("確定刪除這筆行事曆事件？")) return;
  try {
    await api(`/api/events/${id}`, { method: "DELETE" });
    closeModal();
    fc.refetchEvents();
  } catch (e) { showEvErr(e.message); }
}

// ── 開機：header 使用者/權限 → 渲染行事曆 ───────────────────
initHeader().then(() => fc.render()).catch(() => {});
