/* care_reminders.js — 保養提醒名單（選單權限 customers）。目前只列名單、預覽訊息，不送 LINE。
   依賴 common.js：$/esc/api/DF/setStatus/setSeg/getSeg/initHeader */
"use strict";

let crRows = [];

function crRow(r, i) {
  const due = `${esc(DF.fromISO(r.due))}${r.overdue ? '<div class="lh" style="color:var(--danger)">已過期</div>' : ""}`;
  const who = `<a href="/customers#detail/${r.customer_id}">${esc(r.customer_name)}</a><div class="lh">${esc(r.customer_code || "")}　${esc(r.phone || "")}</div>`;
  const model = `${r.model ? esc(r.model) : '<span class="lh">交易未選商品</span>'}<div class="lh">交機 ${esc(DF.fromISO(r.transaction_date))}</div>`;
  const line = r.line ? "✅ 已綁定" : '<span class="lh">未綁定（請打電話）</span>';
  const booked = r.booked ? `📅 ${esc(DF.fromISO(r.booked))}` : '<span class="lh">—</span>';
  return `<tr>
    <td>${due}</td><td>${who}</td><td>${esc(r.label)}</td><td>${model}</td><td>${line}</td><td>${booked}</td>
    <td style="white-space:nowrap;">
      <button type="button" class="btn sm" onclick="crSchedule(${i})">加到行事曆</button>
      <button type="button" class="btn sm outline" onclick="crPreview(${i})">訊息草稿</button>
    </td></tr>
    <tr class="hidden" id="cr-msg-${i}"><td colspan="7"><div class="lh">LINE 訊息草稿（目前不會送出）：</div><pre class="cr-msg">${esc(r.message)}</pre></td></tr>`;
}

async function crLoad() {
  const r = await api(`/api/care-reminders?ahead=${getSeg("cr-ahead")}`);
  crRows = r.rows;
  $("cr-count").textContent = `（${crRows.length} 筆）`;
  $("cr-body").innerHTML = crRows.map(crRow).join("")
    || '<tr><td colspan="7" class="hint">這段期間沒有需要提醒的客人。交易記錄裡要有助聽器的購買或租借，才會算出提醒。</td></tr>';
}

function crPreview(i) { $(`cr-msg-${i}`).classList.toggle("hidden"); }

async function crSchedule(i) {
  const r = crRows[i];
  if (!confirm(`在 ${DF.fromISO(r.due)} 建一筆「${r.label}」回訪提醒給 ${r.customer_name}？`)) return;
  try {
    await api("/api/care-reminders", { method: "POST", body: JSON.stringify({
      customer_id: r.customer_id, purchase_id: r.purchase_id, type: r.type, due: r.due, label: r.label }) });
    setStatus("已加到行事曆。", "success");
    await crLoad();
  } catch (e) { setStatus(e.message, "danger"); }
}

$("cr-ahead").querySelectorAll("button").forEach(b => b.addEventListener("click", async () => {
  setSeg("cr-ahead", b.dataset.v);
  await crLoad();
}));

initHeader().then(async me => {
  if (!(me.menus || []).includes("customers")) { location.href = "/calendar"; return; }
  setSeg("cr-ahead", "30");
  await crLoad();
}).catch(() => {});
