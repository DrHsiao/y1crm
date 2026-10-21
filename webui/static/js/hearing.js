/* hearing.js — 聽檢輔助頁（Noah 匯出檔或 MA42 螢幕照片／PDF → 聽力數值 → 講義規則判讀）
   規則判讀全在後端 api/hearing.py（單一來源、有單元測試）；前端只管輸入、畫聽力圖、顯示結果。
   依賴 common.js：$/esc/api/DF/dfToISO/attachDateMask/setStatus/hideStatus/setNote/
                  setSeg/getSeg/shrinkImage/initHeader */
"use strict";

const FREQS = [250, 500, 750, 1000, 1500, 2000, 3000, 4000, 6000, 8000];
const BC_FREQS = new Set([250, 500, 750, 1000, 1500, 2000, 3000, 4000]);
const KINDS = ["ac", "acm", "bc", "bcm"];
const EAR = { R: "右耳", L: "左耳" };
const LIMITS = { ac: [-10, 120], bc: [-10, 85] };   // MA42 規格書輸出範圍
// [耳, 種類, 列標題]：符號同聽力圖慣例（講義 p.7）
const ROWS = [
  ["R", "ac", "右 氣導 ○"], ["R", "acm", "右 氣導遮蔽 △"], ["R", "bc", "右 骨導 <"], ["R", "bcm", "右 骨導遮蔽 ["],
  ["L", "ac", "左 氣導 ×"], ["L", "acm", "左 氣導遮蔽 □"], ["L", "bc", "左 骨導 >"], ["L", "bcm", "左 骨導遮蔽 ]"],
];
const SEGS = { "seg-mode": "adult", "seg-transducer": "DD45", "seg-first_ear": "R", "seg-talk": "ok" };
const READ_MAX_EDGE = 1600;   // 同表單 OCR：縮圖幾乎不影響速度，但影響準確度

const blankData = () => ({
  thr: { R: { ac: {}, acm: {}, bc: {}, bcm: {} }, L: { ac: {}, acm: {}, bc: {}, bcm: {} } },
  retest1k: {}, tymp: {}, speech: {},
});

let data = blankData();
let testId = null;          // 編輯中的紀錄 id（null＝新檢查）
let customer = null;        // {id, name, customer_code}
let custRows = [];
let savedFiles = [];        // 已存檔的原始檔
let pendingFiles = [];      // 本次讀值用過、儲存時才上傳 {blob, name, mime}
let dirty = false, nrMode = false;

const fLabel = f => f >= 1000 ? `${f / 1000}k` : `${f}`;
const opts = () => Object.fromEntries(Object.keys(SEGS).map(id => [id.slice(4), getSeg(id)]));
const num = v => v == null ? "—" : `${v}`;

// ══ 聽閾格線 ══════════════════════════════════════════════════
function buildGrid() {
  $("grid-head").innerHTML = `<tr><th></th>${FREQS.map(f => `<th>${fLabel(f)}</th>`).join("")}</tr>`;
  $("grid-body").innerHTML = ROWS.map(([ear, kind, label], i) => `
    <tr class="ear-${ear}${i === 4 ? " ht-sep" : ""}"><th class="rowh">${label}</th>${FREQS.map(f =>
      kind.startsWith("bc") && !BC_FREQS.has(f) ? `<td class="na"></td>` :
      `<td><input class="ht-cell" inputmode="numeric" autocomplete="off" data-ear="${ear}"
          data-kind="${kind}" data-f="${f}" aria-label="${label} ${fLabel(f)}"></td>`).join("")}</tr>`).join("");
  document.querySelectorAll(".ht-cell").forEach(el => {
    el.addEventListener("input", () => onCell(el));
    el.addEventListener("click", () => { if (nrMode) toggleNr(el); });
  });
}

function parseLevel(s, kind) {
  s = (s || "").trim().toUpperCase();
  if (!s) return null;
  if (s === "NR" || s === "N") return "NR";
  if (!/^-?\d{1,3}$/.test(s)) return undefined;
  const n = +s, [lo, hi] = LIMITS[kind.slice(0, 2)];
  return n >= lo && n <= hi ? n : undefined;
}

function onCell(el) {
  const { ear, kind, f } = el.dataset;
  el.classList.remove("ocr-uncertain");   // 人改過＝已確認
  refreshConfirmBtn();
  const v = parseLevel(el.value, kind);
  el.classList.toggle("ht-bad", v === undefined);
  if (v == null) delete data.thr[ear][kind][f];
  else data.thr[ear][kind][f] = v;
  changed();
}

function refreshConfirmBtn() {
  $("confirm-btn").classList.toggle("hidden", !document.querySelector(".ht-cell.ocr-uncertain"));
}
function confirmAll() {
  if (!confirm("請確定已逐格對照機器畫面，黃底的數值都正確？")) return;
  document.querySelectorAll(".ht-cell.ocr-uncertain").forEach(el => el.classList.remove("ocr-uncertain"));
  refreshConfirmBtn();
}

function toggleNr(el) {
  el.value = el.value.trim().toUpperCase() === "NR" ? "" : "NR";
  onCell(el);
}

function toggleNrMode() {
  nrMode = !nrMode;
  document.body.classList.toggle("nr-mode", nrMode);
  // 標記模式下格子唯讀：點了只切換 NR，不叫出鍵盤
  document.querySelectorAll(".ht-cell").forEach(el => { el.readOnly = nrMode; });
  $("nr-btn").classList.toggle("outline", !nrMode);
  $("nr-btn").textContent = nrMode ? "NR 標記中（點格子切換）" : "NR 標記";
}

// 1 kHz 重測：只有先測耳要做（講義 p.6）
function syncRetest() {
  const first = getSeg("seg-first_ear");
  $("retest-label").textContent = `${EAR[first]} 1 kHz 重測`;
  $("retest-in").value = data.retest1k[first] ?? "";
  $("retest-in").classList.remove("ht-bad");
}
$("retest-in").addEventListener("input", () => {
  const el = $("retest-in"), first = getSeg("seg-first_ear");
  const v = parseLevel(el.value, "ac");
  el.classList.toggle("ht-bad", v === undefined || v === "NR");
  if (typeof v === "number") data.retest1k[first] = v; else delete data.retest1k[first];
  changed();
});

// ══ 鼓室圖與語音（選填）════════════════════════════════════════
const EXTRA = [   // [欄位, 所屬, 最小, 最大, 整數]
  ["pressure", "tymp", -600, 400, true], ["compliance", "tymp", 0, 9.99, false],
  ["srt", "speech", -10, 120, true], ["wds", "speech", 0, 100, true], ["mcl", "speech", -10, 120, true],
];
function buildExtra() {
  const inp = (ear, k) => `<td><input class="ht-x" id="x-${ear}-${k}" inputmode="decimal" autocomplete="off"></td>`;
  $("extra-body").innerHTML = ["R", "L"].map(ear => `
    <tr class="ear-${ear}"><th class="rowh">${EAR[ear]}</th>
      ${inp(ear, "pressure")}${inp(ear, "compliance")}
      <td><input type="checkbox" id="x-${ear}-np" class="ht-np"></td>
      <td id="x-${ear}-tymp">—</td>
      ${inp(ear, "srt")}${inp(ear, "wds")}${inp(ear, "mcl")}
    </tr>`).join("");
  document.querySelectorAll(".ht-x").forEach(el => el.addEventListener("input", collectExtra));
  document.querySelectorAll(".ht-np").forEach(el => el.addEventListener("change", collectExtra));
}
function collectExtra() {
  for (const ear of ["R", "L"]) {
    const np = $(`x-${ear}-np`).checked;
    const t = { np }, s = {};
    for (const [k, part, lo, hi, isInt] of EXTRA) {
      const el = $(`x-${ear}-${k}`);
      el.disabled = part === "tymp" && np;            // NP（無尖峰）＝壓力與聲順值不適用
      const raw = el.value.trim();
      const n = Number(raw);
      const bad = raw !== "" && !(Number.isFinite(n) && n >= lo && n <= hi && (!isInt || Number.isInteger(n)));
      el.classList.toggle("ht-bad", bad);
      if (raw === "" || bad || el.disabled) continue;
      (part === "tymp" ? t : s)[k] = n;
    }
    if (np || t.pressure != null || t.compliance != null) data.tymp[ear] = t; else delete data.tymp[ear];
    if (Object.keys(s).length) data.speech[ear] = s; else delete data.speech[ear];
  }
  changed();
}
function fillExtra() {
  for (const ear of ["R", "L"]) {
    const t = data.tymp[ear] || {}, s = data.speech[ear] || {};
    $(`x-${ear}-np`).checked = !!t.np;
    for (const [k, part] of EXTRA) {
      const el = $(`x-${ear}-${k}`);
      el.value = (part === "tymp" ? t : s)[k] ?? "";
      el.disabled = part === "tymp" && !!t.np;
      el.classList.remove("ht-bad");
    }
  }
}

function fillAll() {
  document.querySelectorAll(".ht-cell").forEach(el => {
    el.value = data.thr[el.dataset.ear][el.dataset.kind][el.dataset.f] ?? "";
    el.classList.remove("ht-bad", "ocr-uncertain");
  });
  syncRetest();
  fillExtra();
  refreshConfirmBtn();
  drawChart();
  scheduleAssess();
}

function changed() {
  dirty = true;
  drawChart();
  scheduleAssess();
}

// ══ 聽力圖（SVG；只是輔助對照，數值以表格為準）═════════════════
function finalAc(ear, f) {
  const m = data.thr[ear].acm[f];
  return m !== undefined ? m : data.thr[ear].ac[f];
}
function symbol(ear, kind, x, y) {
  const c = `ht-${ear.toLowerCase()}`;
  const X = x + (kind.startsWith("bc") ? (ear === "R" ? -9 : 9) : 0);   // 骨導畫在頻率線外側
  switch (ear + kind) {
    case "Rac":  return `<circle class="${c}" cx="${X}" cy="${y}" r="6"/>`;
    case "Racm": return `<polygon class="${c}" points="${X},${y - 7} ${X - 6.5},${y + 5} ${X + 6.5},${y + 5}"/>`;
    case "Rbc":  return `<polyline class="${c}" points="${X + 3},${y - 6} ${X - 4},${y} ${X + 3},${y + 6}"/>`;
    case "Rbcm": return `<polyline class="${c}" points="${X + 3},${y - 7} ${X - 3},${y - 7} ${X - 3},${y + 7} ${X + 3},${y + 7}"/>`;
    case "Lac":  return `<path class="${c}" d="M${X - 5} ${y - 5}L${X + 5} ${y + 5}M${X + 5} ${y - 5}L${X - 5} ${y + 5}"/>`;
    case "Lacm": return `<rect class="${c}" x="${X - 5.5}" y="${y - 5.5}" width="11" height="11"/>`;
    case "Lbc":  return `<polyline class="${c}" points="${X - 3},${y - 6} ${X + 4},${y} ${X - 3},${y + 6}"/>`;
    case "Lbcm": return `<polyline class="${c}" points="${X - 3},${y - 7} ${X + 3},${y - 7} ${X + 3},${y + 7} ${X - 3},${y + 7}"/>`;
  }
  return "";
}
function nrArrow(ear, kind, x, y) {
  const d = ear === "R" ? -1 : 1;
  const X = x + (kind.startsWith("bc") ? 9 * d : 0) + 4 * d, Y = y + 5;
  const ex = X + 7 * d, ey = Y + 9;
  return `<path class="ht-${ear.toLowerCase()}" d="M${X} ${Y}L${ex} ${ey}M${ex} ${ey}l${-4 * d} 0M${ex} ${ey}l0 -4"/>`;
}
function drawChart() {
  const W = 560, H = 360, L = 40, R = 16, T = 24, B = 10;
  const pw = W - L - R, ph = H - T - B;
  const x = f => L + Math.log2(f / 125) / 6 * pw;          // 125–8000 Hz，對數刻度
  const y = db => T + (db + 10) / 130 * ph;                 // -10–120 dB HL，往下遞增
  let g = "";
  for (let db = -10; db <= 120; db += 10)
    g += `<line class="ht-gl" x1="${L}" x2="${W - R}" y1="${y(db)}" y2="${y(db)}"/>` +
         `<text class="ht-tx" x="${L - 6}" y="${y(db) + 4}" text-anchor="end">${db}</text>`;
  for (const f of [125, 250, 500, 1000, 2000, 4000, 8000])
    g += `<line class="ht-gl" x1="${x(f)}" x2="${x(f)}" y1="${T}" y2="${H - B}"/>` +
         `<text class="ht-tx" x="${x(f)}" y="${T - 8}" text-anchor="middle">${fLabel(f)}</text>`;
  for (const f of [750, 1500, 3000, 6000])
    g += `<line class="ht-gl ht-gl-mid" x1="${x(f)}" x2="${x(f)}" y1="${T}" y2="${H - B}"/>`;
  g += `<line class="ht-ref" x1="${L}" x2="${W - R}" y1="${y(25)}" y2="${y(25)}"/>`;   // 講義 p.7 正常上限
  for (const ear of ["R", "L"]) {
    const pts = FREQS.map(f => [f, finalAc(ear, f)]).filter(([, v]) => typeof v === "number");
    if (pts.length > 1)
      g += `<polyline class="ht-${ear.toLowerCase()} ht-line" points="${pts.map(([f, v]) => `${x(f)},${y(v)}`).join(" ")}"/>`;
    for (const kind of KINDS)
      for (const [f, v] of Object.entries(data.thr[ear][kind])) {
        // NR：畫在該傳導方式的最大輸出處加箭頭（只示意，實際上限依頻率而異）
        const vy = y(v === "NR" ? LIMITS[kind.slice(0, 2)][1] : v);
        g += symbol(ear, kind, x(+f), vy) + (v === "NR" ? nrArrow(ear, kind, x(+f), vy) : "");
      }
  }
  $("ht-chart").innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="聽力圖">${g}</svg>`;
}

// ══ 規則判讀（後端 /api/hearing/assess）════════════════════════
let assessTimer = null, assessSeq = 0;
function scheduleAssess() {
  clearTimeout(assessTimer);
  assessTimer = setTimeout(runAssess, 250);
}
async function runAssess() {
  const seq = ++assessSeq;
  try {
    const r = await api("/api/hearing/assess", { method: "POST", body: JSON.stringify({ data, ...opts() }) });
    if (seq === assessSeq) renderAssess(r);   // 只畫最新一次（快速連打時舊回應作廢）
  } catch (e) { if (seq === assessSeq) setStatus("判讀失敗：" + e.message, "danger"); }
}

function renderAssess(r) {
  const n = r.next;
  let html = "";
  if (r.start) html += `<div class="ht-start">🎯 ${esc(r.start)}</div>`;
  html += n
    ? `<div class="ht-next-main">下一步：<strong>${esc(n.text)}</strong></div>` +
      (n.reason ? `<div class="why">${esc(n.reason)}</div>` : "") +
      (n.kind === "acm" || n.kind === "bcm" ? `<div class="why">遮蔽量依聽力師判斷（講義未定，系統不給數值）</div>` : "")
    : `<div class="ht-next-main">✔ 清單上的項目都已完成</div>`;
  $("ht-next").innerHTML = html;

  const warns = [...r.warnings];
  if (r.retest && !r.retest.ok) warns.unshift(r.retest.text);
  $("ht-warn").innerHTML = (r.retest && r.retest.ok ? `<div class="field-info">✔ ${esc(r.retest.text)}</div>` : "") +
    warns.map(w => `<div class="soft-warn ht-warn">${esc(w)}</div>`).join("");

  const isNext = c => n && c.ear === n.ear && c.kind === n.kind && c.freq === n.freq && c.text === n.text;
  $("ht-checklist").innerHTML = r.checklist.map(c => `
    <li class="${c.done ? "done" : isNext(c) ? "now" : ""}">${c.done ? "✔" : isNext(c) ? "▶" : "○"} ${esc(c.text)}${
      c.reason ? `<span class="why">${esc(c.reason)}</span>` : ""}</li>`).join("");

  // 表格上標出下一步要填的格子
  document.querySelectorAll(".ht-hit").forEach(el => el.classList.remove("ht-hit"));
  if (n) {
    const el = n.kind === "retest" ? $("retest-in")
      : document.querySelector(`.ht-cell[data-ear="${n.ear}"][data-kind="${n.kind}"][data-f="${n.freq}"]`);
    el && el.classList.add("ht-hit");
  }

  $("ht-interp").innerHTML = `<div class="table-wrap"><table class="ht-grid">
    <thead><tr><th></th><th>平均氣導</th><th>平均骨導</th><th>氣骨導差</th><th>程度</th><th>類型</th><th>SRT−PTA</th></tr></thead>
    <tbody>${["R", "L"].map(e => {
      const it = r.interp[e], sp = r.speech[e];
      return `<tr class="ear-${e}"><th class="rowh">${EAR[e]}</th><td>${num(it.pta_ac)}</td><td>${num(it.pta_bc)}</td>
        <td>${num(it.abg)}</td><td>${esc(it.degree || "—")}</td><td>${esc(it.type || "—")}</td>
        <td>${sp ? sp.srt_pta_diff : "—"}</td></tr>` +
        (it.notes.length ? `<tr class="ht-note"><td></td><td colspan="6">${it.notes.map(esc).join("；")}</td></tr>` : "");
    }).join("")}</tbody></table></div>
    <p class="hint">平均＝500／1k／2k 三頻（暫定）；程度與類型依講義 p.7、p.8。規則版本：${esc(r.version)}</p>`;

  for (const e of ["R", "L"]) {
    const t = r.tymp[e];
    $(`x-${e}-tymp`).textContent = t ? `${t.type ? "Type " + t.type + "：" : ""}${t.meaning}` : "—";
  }
  $("ht-pending").innerHTML = r.pending.map(p => `<li>${esc(p)}</li>`).join("");
}

// ══ 拍螢幕／匯入 PDF → 本機模型讀值 ════════════════════════════
let ocrTimerId = null, ocrBusy = false;
function showOcrOverlay() {
  ocrBusy = true;
  const t0 = Date.now();
  const tick = () => { $("ocr-timer").textContent = `已處理 ${Math.floor((Date.now() - t0) / 1000)} 秒`; };
  tick();
  ocrTimerId = setInterval(tick, 1000);
  $("ocr-overlay").classList.remove("hidden");
}
function hideOcrOverlay() {
  ocrBusy = false;
  clearInterval(ocrTimerId); ocrTimerId = null;
  $("ocr-overlay").classList.add("hidden");
}

async function handleReadFile(file) {
  hideStatus();
  const isPdf = file.type === "application/pdf" || /\.pdf$/i.test(file.name || "");
  const btns = [$("read-camera-btn"), $("read-file-btn")];
  btns.forEach(b => { b.disabled = true; });
  showOcrOverlay();
  try {
    const blob = isPdf ? file : await shrinkImage(file, READ_MAX_EDGE);
    const mime = isPdf ? "application/pdf" : (blob.type || "image/jpeg");
    const res = await fetch("/api/hearing/read", {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": mime }, body: blob });
    if (res.status === 401) { location.href = "/login?next=/hearing"; return; }
    const r = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(r.detail || `HTTP ${res.status}`);
    const diff = applyReading(r);
    pendingFiles.push({ blob, mime, name: file.name || (isPdf ? "MA42報告.pdf" : "MA42螢幕.jpg") });
    renderFiles();
    if (!r.count) {
      setStatus("沒有讀到任何聽閾點。請正對螢幕、避開反光重拍，或直接手動輸入。", "warning");
    } else {
      setStatus(`讀到 ${r.count} 個點` + (diff ? `，其中 ${diff} 個與原本的值不同` : "") +
        "，已全部標黃。AI 讀聽力圖常差 5 dB，請逐格對照機器畫面確認後再儲存。", "warning");
    }
  } catch (e) {
    setStatus("讀值失敗：" + e.message, "danger");
  } finally {
    hideOcrOverlay();
    btns.forEach(b => { b.disabled = false; });
  }
}

// 讀到的點覆蓋到表格（沒讀到的格子保留原值）；回傳「與原值不同」的數量。
// 讀到的格子「全部」標黃：合成聽力圖實測約一半差 5 dB，而模型不會自己標出沒把握（見 api/hearing.py）
function applyReading(r) {
  let diff = 0;
  const hit = [];
  for (const ear of ["R", "L"]) for (const kind of KINDS)
    for (const [f, v] of Object.entries(r.thr[ear][kind])) {
      const prev = data.thr[ear][kind][f];
      if (prev !== undefined && prev !== v) diff++;
      data.thr[ear][kind][f] = v;
      hit.push(`.ht-cell[data-ear="${ear}"][data-kind="${kind}"][data-f="${f}"]`);
    }
  const keep = [...document.querySelectorAll(".ht-cell.ocr-uncertain")];   // 前一次讀值還沒確認的
  fillAll();
  keep.forEach(el => el.classList.add("ocr-uncertain"));
  hit.forEach(sel => document.querySelector(sel)?.classList.add("ocr-uncertain"));
  refreshConfirmBtn();
  $("screen-now").textContent = r.current ? `機器螢幕目前設定：${r.current}` : "";
  $("screen-now").classList.toggle("hidden", !r.current);
  dirty = true;
  return diff;
}

for (const id of ["read-camera", "read-file"]) {
  $(id).addEventListener("change", e => {
    const file = e.target.files[0];
    e.target.value = "";
    if (file) handleReadFile(file);
  });
}

// ══ 匯入 Noah 匯出檔（.nhax）════════════════════════════════════
// 解析全在後端 api/noah.py。聽力圖是 MA42 經 Noah 存的精確值，填入後不標黃。
// 選配設定不另存：.nhax 當原始檔跟著紀錄存，重開紀錄時再解析一次。
const NHAX_MIME = "application/x-nhax";
const GAIN_ROWS = [["50", "小聲 50"], ["65", "一般 65"], ["80", "大聲 80"], ["mpo", "最大輸出"]];
let noah = null;            // 目前顯示的解析結果
let noahApplied = null;     // 已填入表格的那份聽力圖 index

async function handleNoahFile(file) {
  hideStatus();
  if (!/\.nhax$/i.test(file.name || "")) {
    setStatus("請選擇 Noah 匯出的客戶檔（副檔名 .nhax）。", "warning"); return;
  }
  $("noah-btn").disabled = true;
  try {
    const res = await fetch("/api/hearing/noah", {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": NHAX_MIME }, body: file });
    if (res.status === 401) { location.href = "/login?next=/hearing"; return; }
    const r = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(r.detail || `HTTP ${res.status}`);
    pendingFiles = pendingFiles.filter(p => p.mime !== NHAX_MIME);   // 只留最後匯入的那份
    pendingFiles.push({ blob: file, mime: NHAX_MIME, name: file.name });
    renderFiles();
    dirty = true;
    noahApplied = null;
    renderNoah(r);
    const n = r.audiograms.length;
    let msg = (n ? `讀到 ${n} 份聽力圖` : "這份 Noah 檔沒有聽力圖") + (r.fitting ? "與助聽器選配紀錄" : "") +
      (n ? "，請在「Noah 匯入」選一份填入表格。" : "。");
    if (!customer && r.patient.name) {
      $("cust-q").value = r.patient.name;
      await searchCustomers();
      msg += "已用 Noah 的客戶姓名查詢，請在上方選擇客戶。";
    }
    setStatus(msg, "success");
  } catch (e) {
    setStatus("Noah 檔匯入失敗：" + e.message, "danger");
  } finally {
    $("noah-btn").disabled = false;
  }
}
$("noah-file").addEventListener("change", e => {
  const file = e.target.files[0];
  e.target.value = "";
  if (file) handleNoahFile(file);
});

async function loadSavedNoah(fid) {
  try { renderNoah(await api(`/api/hearing/files/${fid}/noah`)); }
  catch (e) { setStatus("存檔的 Noah 檔無法解析：" + e.message, "warning"); }
}
function hideNoah() {
  noah = null; noahApplied = null;
  $("noah-card").classList.add("hidden");
}
function closeNoah() { $("noah-card").classList.add("hidden"); }

function renderNoah(r) {
  noah = r;
  const p = r.patient;
  $("noah-patient").textContent = [p.name || "（沒有姓名）", p.birth_date && "生日 " + DF.fromISO(p.birth_date),
    p.no && "Noah 編號 " + p.no].filter(Boolean).join("・");
  renderNoahWarn();
  renderNoahAudiograms();
  renderNoahFitting();
  $("noah-card").classList.remove("hidden");
}
function renderNoahWarn() {
  const warns = [...noah.warnings];
  if (customer && noah.patient.name && customer.name !== noah.patient.name)
    warns.unshift(`Noah 檔的客戶是「${noah.patient.name}」，和目前選的「${customer.name}」不同，請確認沒有選錯人。`);
  $("noah-warn").innerHTML = warns.map(w => `<div class="soft-warn ht-warn">${esc(w)}</div>`).join("");
}

const agSource = a => a.measured ? `${a.source}${a.transducer ? `（${a.transducer}）` : ""} 實測` : `Noah「${a.source}」`;
const dtText = dt => `${DF.fromISO(dt.slice(0, 10))} ${dt.slice(11)}`;

function renderNoahAudiograms() {
  const list = noah.audiograms;
  if (!list.length) { $("noah-audiograms").innerHTML = `<p class="hint">這份檔案沒有聽力圖。</p>`; return; }
  // 建議用最新一份聽檢儀實測的（有遮蔽資訊）；Noah 裡手動輸入的沒有遮蔽，數值也可能打錯
  const measured = list.filter(a => a.measured);
  const rec = (measured.length ? measured : list).at(-1).index;
  // 卡片而不是表格：差異清單長，手機上表格會把「填入」鈕擠出畫面
  $("noah-audiograms").innerHTML = [...list].reverse().map(a => `
    <div class="ht-ag${a.index === noahApplied ? " ht-current" : ""}">
      <div class="ht-ag-head">
        <strong>${esc(dtText(a.datetime))}</strong>
        <span>${esc(agSource(a))}${a.index === rec ? ` <span class="ht-badge">建議</span>` : ""}</span>
        <span class="lh">${a.count} 點${a.masking_info ? "" : "・沒有遮蔽資訊"}</span>
        <button type="button" class="btn sm${a.index === rec ? "" : " outline"}"
          onclick="applyNoahAudiogram(${a.index})">${a.index === noahApplied ? "✔ 已填入" : "填入"}</button>
      </div>
      ${a.diff_prev.length ? `<div class="ht-ag-diff">和前一份不同：${a.diff_prev.map(esc).join("、")}</div>`
        : a.index ? `<div class="ht-ag-diff">和前一份相同</div>` : ""}
      ${a.skipped.length ? `<div class="why">沒填入：${a.skipped.map(esc).join("、")}</div>` : ""}
    </div>`).join("");
}

function applyNoahAudiogram(i) {
  const a = noah.audiograms.find(x => x.index === i);
  const has = ["R", "L"].some(e => KINDS.some(k => Object.keys(data.thr[e][k]).length));
  if (has && !confirm("會用這份聽力圖取代表格裡目前的聽力數值（重測、鼓室圖、語音不受影響），確定？")) return;
  data.thr = JSON.parse(JSON.stringify(a.thr));
  $("f-test_date").value = DF.fromISO(a.date);
  if (a.transducer) setSeg("seg-transducer", a.transducer);
  noahApplied = i;
  fillAll();          // 也清掉先前拍照讀值的黃底：整張表都換成精確值了
  dirty = true;
  renderNoahAudiograms();
  setStatus(`已填入 ${dtText(a.datetime)} ${agSource(a)}的聽力圖（${a.count} 點），檢查日期也改成這一天。`, "success");
}

function renderNoahFitting() {
  const f = noah.fitting;
  let html = "";
  if (f) {
    const devs = ["R", "L"].filter(e => f.devices[e]).map(e => `${EAR[e]} ${esc(f.devices[e].model)}`).join("、");
    const last = Object.values(f.devices).map(d => d.datetime).sort().at(-1);
    html += `<p class="ht-devs">${devs}<span class="lh">${esc(f.software)}・最後選配 ${esc(dtText(last))}</span></p>` +
      f.programs.map(noahProgram).join("") + noahHistory(f.history) +
      `<p class="hint">名稱與刻度文字照 Smart Fit 2.3 中文版畫面；沒對照過的值顯示軟體原始代碼。黃底＝和初始選配值不同。</p>`;
  }
  if (noah.other_fittings.length)
    html += `<div class="soft-warn ht-warn">其他廠牌選配：${noah.other_fittings.map(o =>
      `${esc(o.text)}（${EAR[o.ear]}，${esc(DF.fromISO(o.datetime.slice(0, 10)))}）`).join("；")}。這些軟體的設定是廠商自有格式，讀不出來。</div>`;
  $("noah-fitting").innerHTML = html || `<p class="hint">這份檔案沒有助聽器選配紀錄。</p>`;
}
function noahCell(c) {
  if (!c) return `<td>—</td>`;
  return `<td class="${c.changed ? "ht-chg" : ""}">${esc(c.value)}${
    c.changed && c.init ? `<div class="why">初始 ${esc(c.init)}</div>` : ""}</td>`;
}
function gainHead() {
  return `<thead><tr><th></th>${noah.freqs.map(f => `<th>${fLabel(f)}</th>`).join("")}`;
}
function noahProgram(p) {
  const feats = p.features.map(f => `<tr><th class="rowh">${esc(f.label)}<span class="lh">${esc(f.tab)}</span></th>
    ${noahCell(f.R)}${noahCell(f.L)}</tr>`).join("");
  const gains = ["R", "L"].filter(e => p.gains[e]).map(e => GAIN_ROWS.filter(([k]) => p.gains[e][k]).map(([k, label], j) =>
    `<tr class="ear-${e}${e === "L" && j === 0 ? " ht-sep" : ""}"><th class="rowh">${EAR[e]} ${label}</th>${
      p.gains[e][k].map(v => `<td>${v ?? "—"}</td>`).join("")}</tr>`).join("")).join("");
  return `<div class="ht-prog"><h4>程式 ${esc(p.loc)}：${esc(p.name)}</h4>
    <div class="table-wrap"><table class="ht-grid ht-feat">
      <thead><tr><th></th><th class="ht-r-text">右耳</th><th class="ht-l-text">左耳</th></tr></thead>
      <tbody>${feats}</tbody></table></div>
    <details class="ht-details"><summary>增益與最大輸出（dB，依輸入音量）</summary>
      <div class="table-wrap"><table class="ht-grid">${gainHead()}</tr></thead><tbody>${gains}</tbody></table></div>
    </details></div>`;
}
function noahHistory(hist) {
  if (!hist.length) return "";
  const prev = {};
  const rows = hist.map(h => {
    const before = prev[h.ear];
    prev[h.ear] = h.g65;
    const cells = (h.g65 || []).map((v, i) => {
      const d = before && v != null && before[i] != null ? v - before[i] : 0;
      return `<td>${d >= 3 ? "▲" : d <= -3 ? "▼" : ""}${v ?? "—"}</td>`;
    }).join("");
    return `<tr class="ear-${h.ear}"><th class="rowh">${esc(dtText(h.datetime))} ${EAR[h.ear]}</th>${cells}
      <td class="ht-chglist">${h.changes.map(esc).join("<br>")}</td></tr>`;
  }).reverse().join("");
  return `<details class="ht-details"><summary>調整歷程（${hist.length} 次有變動的存檔）</summary>
    <div class="table-wrap"><table class="ht-grid ht-hist">${gainHead()}<th>功能變更</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    <p class="hint">數字是程式 1、一般音量（65 dB 輸入）的增益；▲▼＝比同一耳上一次存檔高／低 3 dB 以上。</p></details>`;
}

// ── 模型載入狀態（與表單 OCR 共用同一個模型與端點）─────────────
let ocrPollTimer = null;
function setOcrState(state, extra) {
  const ready = state === "ready";
  $("read-camera-btn").disabled = !ready;
  $("read-file-btn").disabled = !ready;
  $("ocr-load-btn").classList.toggle("hidden", ready);
  if (state === "loading") {
    $("ocr-load-btn").disabled = true;
    $("ocr-load-btn").textContent = "載入中…" + (extra?.elapsed ? ` ${extra.elapsed}s` : "");
    setNote("ocr-status", "GPU 熱機中，約需 2–4 分鐘，完成後即可拍螢幕／匯入。");
  } else {
    if (ready) setNote("ocr-status", "");
    $("ocr-load-btn").disabled = false;
    $("ocr-load-btn").textContent = "🔌 載入模型";
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
  ocrPollTimer = null;
  let s;
  try { s = await api("/api/ocr/status"); } catch { return; }
  setOcrState(s.state, s);
  if (s.state === "loading") ocrPollTimer = setTimeout(pollOcrStatus, 4000);
}
async function refreshOcrState() {
  try {
    const s = await api("/api/ocr/status");
    if (s.state === "loading") { setOcrState("loading", s); pollOcrStatus(); }
    else setOcrState(s.state === "ready" ? "ready" : "idle");
  } catch {}
}
setInterval(() => { if (!ocrBusy && !ocrPollTimer) refreshOcrState(); }, 60000);

// ══ 客戶與歷次紀錄 ════════════════════════════════════════════
async function searchCustomers() {
  const q = $("cust-q").value.trim();
  if (!q) { $("cust-q").focus(); return; }
  try {
    custRows = (await api(`/api/hearing/customers?q=${encodeURIComponent(q)}`)).rows;
  } catch (e) { setStatus(e.message, "danger"); return; }
  $("cust-results").innerHTML = custRows.length
    ? custRows.map((c, i) => `<button type="button" onclick="pickCustomer(${i})">
        <strong>${esc(c.name)}</strong> <span class="lh">${esc(c.customer_code)}${
        c.birth_date ? "・" + esc(DF.fromISO(c.birth_date)) : ""}</span></button>`).join("")
    : `<div class="ht-pick-empty">查無符合的客戶</div>`;
  $("cust-results").classList.remove("hidden");
}
$("cust-q").addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); searchCustomers(); } });

function pickCustomer(i) {
  const c = custRows[i];
  setCustomer({ id: c.id, name: c.name, customer_code: c.customer_code });
  $("cust-results").classList.add("hidden");
  $("cust-q").value = "";
  if (testId) dirty = true;   // 編輯中的紀錄改掛到別的客戶，要按儲存才生效
}
function setCustomer(c) {
  customer = c;
  $("cust-picked").innerHTML = c
    ? `已選：<strong>${esc(c.name)}</strong>（${esc(c.customer_code)}）`
    : "";
  $("cust-picked").classList.toggle("hidden", !c);
  if (noah) renderNoahWarn();
  loadHistory();
}

async function loadHistory() {
  if (!customer) { $("ht-history").classList.add("hidden"); return; }
  let rows;
  try { rows = (await api(`/api/hearing/tests?customer_id=${customer.id}`)).rows; }
  catch (e) { setStatus(e.message, "danger"); return; }
  $("history-body").innerHTML = rows.length ? rows.map(t => `
    <tr onclick="openTest(${t.id})" class="${t.id === testId ? "ht-current" : ""}">
      <td>${esc(DF.fromISO(t.test_date))}</td><td>${esc(t.summary || "—")}</td>
      <td>${t.files ? `📎 ${t.files}` : "—"}</td><td>${esc(t.created_by || "—")}</td></tr>`).join("")
    : `<tr><td colspan="4" class="hint">這位客戶還沒有聽檢紀錄</td></tr>`;
  $("ht-history").classList.remove("hidden");
}

const confirmDiscard = () => !dirty || confirm("目前的內容還沒儲存，確定要放棄嗎？");

async function openTest(id) {
  if (id !== testId && !confirmDiscard()) return;
  let r;
  try { r = await api(`/api/hearing/tests/${id}`); } catch (e) { setStatus(e.message, "danger"); return; }
  testId = r.id;
  data = r.data;
  for (const segId of Object.keys(SEGS)) setSeg(segId, r[segId.slice(4)]);
  $("f-test_date").value = DF.fromISO(r.test_date);
  $("f-notes").value = r.notes || "";
  savedFiles = r.files;
  pendingFiles = [];
  $("screen-now").classList.add("hidden");
  $("ht-editing").textContent = `（編輯 ${DF.fromISO(r.test_date)} 的紀錄）`;
  $("del-btn").classList.remove("hidden");
  renderFiles();
  fillAll();
  dirty = false;
  const nh = savedFiles.filter(f => f.mime === NHAX_MIME).at(-1);
  if (nh) { noahApplied = null; loadSavedNoah(nh.id); } else hideNoah();
  if (!customer || customer.id !== r.customer_id)
    setCustomer({ id: r.customer_id, name: r.customer_name, customer_code: r.customer_code });
  else loadHistory();
}

function newTest(force = false) {
  if (!force && !confirmDiscard()) return;
  testId = null;
  data = blankData();
  for (const [id, v] of Object.entries(SEGS)) setSeg(id, v);
  $("f-test_date").value = DF.fromISO(isoDate(new Date()));   // 本地日期（today() 是 UTC）
  $("f-notes").value = "";
  savedFiles = []; pendingFiles = [];
  $("screen-now").classList.add("hidden");
  $("ht-editing").textContent = "（新檢查）";
  $("del-btn").classList.add("hidden");
  hideNoah();
  renderFiles();
  fillAll();
  dirty = false;
  loadHistory();
}

// ══ 原始檔 ════════════════════════════════════════════════════
function renderFiles() {
  const saved = savedFiles.map(f => `<span class="ht-file">
      <a href="/api/hearing/files/${f.id}" target="_blank" rel="noopener">📎 ${esc(f.filename || "檔案")}</a>
      <button type="button" onclick="deleteSavedFile(${f.id})" title="刪除">✕</button></span>`);
  const pend = pendingFiles.map((p, i) => `<span class="ht-file pending">⏳ ${esc(p.name)}（儲存時上傳）
      <button type="button" onclick="dropPending(${i})" title="不保存">✕</button></span>`);
  $("ht-files").innerHTML = [...saved, ...pend].join("") ||
    `<span class="hint">尚無。用上方「匯入 Noah」「拍螢幕」或「照片／PDF」後，原始檔會在儲存時一起保存。</span>`;
}
function dropPending(i) { pendingFiles.splice(i, 1); renderFiles(); }
async function deleteSavedFile(fid) {
  if (!confirm("確定刪除這個原始檔？")) return;
  try {
    await api(`/api/hearing/files/${fid}`, { method: "DELETE" });
    savedFiles = savedFiles.filter(f => f.id !== fid);
    renderFiles();
    loadHistory();
  } catch (e) { setStatus(e.message, "danger"); }
}
async function uploadPending(tid, p) {
  const res = await fetch(`/api/hearing/tests/${tid}/files?filename=${encodeURIComponent(p.name)}`, {
    method: "POST", credentials: "same-origin", headers: { "Content-Type": p.mime }, body: p.blob });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
}

// ══ 儲存／刪除 ════════════════════════════════════════════════
async function saveTest() {
  if (!customer) { setStatus("請先查詢並選擇客戶。", "warning"); $("cust-q").focus(); return; }
  let test_date;
  try { test_date = dfToISO($("f-test_date").value, "檢查日期"); } catch (e) { setStatus(e.message, "danger"); return; }
  if (!test_date) { setStatus("請填寫檢查日期。", "warning"); return; }
  if (document.querySelector(".ht-cell.ht-bad, #retest-in.ht-bad, .ht-x.ht-bad")) {
    setStatus("有格子的數值不合法（紅框），請修正或清空後再儲存。", "danger"); return;
  }
  const unconfirmed = document.querySelectorAll(".ht-cell.ocr-uncertain").length;
  if (unconfirmed && !confirm(`還有 ${unconfirmed} 格 AI 讀值（黃底）沒有對照確認，確定要儲存？`)) return;
  const body = JSON.stringify({ customer_id: customer.id, test_date, notes: $("f-notes").value, data, ...opts() });
  $("save-btn").disabled = true;
  try {
    if (testId) await api(`/api/hearing/tests/${testId}`, { method: "PUT", body });
    else testId = (await api("/api/hearing/tests", { method: "POST", body })).id;
    let failed = 0;
    for (const p of [...pendingFiles]) {
      try { await uploadPending(testId, p); pendingFiles.splice(pendingFiles.indexOf(p), 1); }
      catch { failed++; }
    }
    savedFiles = (await api(`/api/hearing/tests/${testId}`)).files;
    renderFiles();
    dirty = false;
    $("ht-editing").textContent = `（編輯 ${DF.fromISO(test_date)} 的紀錄）`;
    $("del-btn").classList.remove("hidden");
    loadHistory();
    setStatus(failed ? `已儲存，但有 ${failed} 個原始檔上傳失敗，請再按一次「儲存」重試。` : "已儲存。",
      failed ? "warning" : "success");
  } catch (e) {
    setStatus("儲存失敗：" + e.message, "danger");
  } finally {
    $("save-btn").disabled = false;
  }
}

async function deleteTest() {
  if (!testId || !confirm("確定刪除這筆聽檢紀錄？原始檔會一併刪除，無法復原。")) return;
  try {
    await api(`/api/hearing/tests/${testId}`, { method: "DELETE" });
    newTest(true);
    setStatus("已刪除。", "success");
  } catch (e) { setStatus(e.message, "danger"); }
}

// ── 開機 ─────────────────────────────────────────────────────
window.addEventListener("beforeunload", e => {
  if (ocrBusy || dirty) { e.preventDefault(); e.returnValue = ""; }
});
for (const id of Object.keys(SEGS))
  $(id).querySelectorAll("button").forEach(b => b.addEventListener("click", () => {
    setSeg(id, b.dataset.v);
    if (id === "seg-first_ear") syncRetest();
    changed();
  }));
document.querySelectorAll(".df-hint").forEach(el => el.textContent = `（${DF.hint}）`);
attachDateMask($("f-test_date"));
$("f-notes").addEventListener("input", () => { dirty = true; });
$("f-test_date").addEventListener("input", () => { dirty = true; });
// 桌機（滑鼠為主）沒有相機可拍，隱藏拍照鈕
if (!matchMedia("(pointer: coarse)").matches) $("read-camera-btn").classList.add("hidden");

initHeader().then(me => {
  if (!(me.menus || []).includes("hearing")) { location.href = "/calendar"; return; }
  buildGrid();
  buildExtra();
  newTest(true);
  refreshOcrState();
}).catch(() => {});
