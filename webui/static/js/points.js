/* points.js — 會員集點（選單權限 customers；加點／扣點店員都可以；作廢／恢復要經理以上）。
   依賴 common.js：$/esc/api/DF/setStatus/setSeg/getSeg/openModal/closeModal/initHeader */
"use strict";

let ptData = { members: [], fb_people: [], rows: [], can_edit: false, can_adjust: false, kinds: [] };
let ptOpen = null;            // 對話框裡正在看的：{type:"member", m} 或 {type:"fb", p}

const ptWhen = s => esc(DF.fromISODateTime(s || ""));
const ptPostUrl = id => `https://www.facebook.com/${encodeURIComponent(id)}`;
const ptSigned = n => n > 0 ? `+${n}` : n < 0 ? `−${-n}` : "0";
const ptNum = n => n ? ptSigned(n) : '<span class="lh">0</span>';
// 原因欄的提示（會員在官網看得到原因）
const PT_ADJ_HINT = { hearing_store: "例如：來店打卡", redeem: "例如：兌換 助聽器電池一盒（周年活動）",
                      add: "例如：推薦朋友來店", sub: "例如：重複登記，扣回" };

function ptCustomer(cid, name) {
  return cid ? `<a href="/customers#detail/${cid}">${esc(name || "")}</a>` : '<span class="lh">還沒連結</span>';
}

function ptStatus(r) {
  const cls = r.status === "counted" ? "" : ' class="lh"';
  const why = r.status === "void" && r.void_reason ? `<div class="lh">${esc(r.void_by || "")}：${esc(r.void_reason)}</div>` : "";
  return `<span${cls}>${esc(r.status_label)}</span>${why}`;
}

function ptOps(r) {
  if (!ptData.can_edit || (r.kind !== "fb" && r.kind !== "ledger")) return "";
  if (r.status === "counted") return `<button type="button" class="btn sm outline" onclick="ptVoid('${r.kind}', ${r.id})">作廢</button>`;
  if (r.status === "void") return `<button type="button" class="btn sm outline" onclick="ptRestore('${r.kind}', ${r.id})">恢復</button>`;
  return "";
}

function ptPoints(r) {
  if (r.status === "void") return `<s class="lh">${ptSigned(r.orig)}</s>`;
  return r.points ? `<b>${ptSigned(r.points)}</b>` : '<span class="lh">0</span>';
}

function ptRecordRow(r, withWho) {
  const by = r.by ? `<div class="lh">登記：${esc(r.by)}</div>` : "";
  return `<tr>
    <td style="white-space:nowrap;">${ptWhen(r.at)}</td>
    ${withWho ? `<td>${esc(r.who)}</td>` : ""}
    <td>${esc(r.source_label)}</td><td>${esc(r.detail || "")}${by}</td><td>${ptStatus(r)}</td>
    <td style="white-space:nowrap; text-align:right;">${ptPoints(r)}</td>
    <td style="white-space:nowrap;">${ptOps(r)}</td></tr>`;
}

function ptMemberRow(m) {
  const tags = [`會員編號 ${m.id}`].concat(m.fb_linked ? ["已綁 Facebook"] : []).join("・");
  return `<tr>
    <td>${esc(m.display_name || "會員")}<div class="lh">${tags}</div></td>
    <td>${ptCustomer(m.customer_id, m.customer_name)}</td>
    <td>${ptNum(m.practice + m.streak)}</td><td>${ptNum(m.hearing_online)}</td><td>${ptNum(m.hearing_store)}</td><td>${ptNum(m.fb)}</td>
    <td>${ptNum(m.other)}</td><td><b>${ptSigned(m.total)}</b></td>
    <td style="white-space:nowrap;">${ptStoreBtn(m)}<button type="button" class="btn sm${ptData.can_adjust ? " outline" : ""}" onclick="ptOpenMember(${m.id})">${ptData.can_adjust ? "加減點／紀錄" : "看紀錄"}</button></td></tr>`;
}

// ── 來店打卡一鍵加點（11-20 john：門市聽檢改成「來店打卡」——客人 Facebook 粉專打卡給店員看，店員一鍵加點；按完跳 Google、Facebook 兩個 QR）──
const ptStoreKind = () => ptData.kinds.find(k => k.key === "hearing_store");

function ptStoreBtn(m) {
  const k = ptData.can_adjust && ptStoreKind();
  return k ? `<button type="button" class="btn sm" style="margin-right:.35rem;" onclick="ptStoreQuick(${m.id}, this)">來店打卡 +${k.default}</button>` : "";
}

function ptShareQr(title, sub) {
  return `<figure style="margin:0; text-align:center; flex:1; min-width:12rem;">
      <img src="/static/img/share_qr_${title === "Google 地圖" ? "google" : "facebook"}.svg?v=1120" alt="${esc(title)} QR Code"
           style="width:min(15rem, 70vw); height:auto; image-rendering:pixelated; border:1px solid var(--line); border-radius:.5rem;">
      <figcaption style="margin-top:.4rem;"><b style="font-size:1.15rem;">${esc(title)}</b><div class="lh">${esc(sub)}</div></figcaption>
    </figure>`;
}

async function ptStoreQuick(id, btn) {
  const m = ptData.members.find(x => x.id === id);
  const k = ptStoreKind();
  if (!m || !k) return;
  const d = new Date();
  btn.disabled = true;
  try {
    const r = await ptAdjustSend(m, { kind: k.key, points: k.default, reason: `來店打卡（${d.getMonth() + 1}/${d.getDate()}）` });
    if (!r) return;                         // 同月第二次、店員按取消
    ptOpen = null;
    openModal(`已幫「${m.display_name || "會員"}」加 ${k.default} 點`,
      `<div style="text-align:center; margin-bottom:1rem;">來店打卡 +${k.default} 點，目前 <b style="font-size:1.25rem;">${r.balance}</b> 點。
         <div class="lh">把畫面轉給客人，用手機掃描：</div></div>
       <div style="display:flex; gap:1.5rem; flex-wrap:wrap; justify-content:center;">
         ${ptShareQr("Google 地圖", "歡迎留下評價、收藏門市")}${ptShareQr("Facebook 粉專", "按讚追蹤、打卡")}
       </div>
       <p class="hint" style="margin-top:1rem;">按讚、評價請客人自由決定；點數已經加好，跟有沒有評價無關。按錯了：用「加減點／紀錄」的「其他扣點」扣回、寫原因。</p>`);
    document.querySelector("#modal-overlay .modal-card").style.width = "min(760px, 96vw)";
    setStatus(`已幫 ${m.display_name || "會員"} 加 ${k.default} 點（來店打卡），目前 ${r.balance} 點。`, "success");
    await ptLoad();
  } catch (e) {
    setStatus(e.message, "danger");
  } finally {
    btn.disabled = false;
  }
}

// 搜尋：會員編號、稱呼、門市客人姓名、手機（手機去掉 - 和空白比）
function ptMatch(m, q) {
  if (!q) return true;
  const hay = [m.id, m.display_name, m.customer_name, m.customer_id, (m.phone || "").replace(/\D/g, "")]
    .map(x => String(x ?? "")).join(" ").toLowerCase();
  return q.toLowerCase().split(/\s+/).filter(Boolean).every(w => hay.includes(w.replace(/-/g, "")));
}

function ptFbMember(m) {
  if (!m) return '<span class="lh">未綁定</span>';
  return `${esc(m.display_name || "會員")}<div class="lh">門市客人：${ptCustomer(m.customer_id, m.customer_name)}</div>`;
}

function ptFbRow(p, i) {
  return `<tr>
    <td>${esc(p.person_name || "（沒有名字）")}</td><td>${ptFbMember(p.member)}</td>
    <td><b>${p.points}</b></td><td>${p.counted}</td><td>${p.comments}</td>
    <td style="white-space:nowrap;">${ptWhen(p.last_at)}</td>
    <td><button type="button" class="btn sm" onclick="ptOpenFb(${i})">看紀錄</button></td></tr>`;
}

function ptRender() {
  const view = getSeg("pt-view");
  ["members", "fb", "rows"].forEach(v => $(`pt-${v}-wrap`).classList.toggle("hidden", view !== v));
  const total = ptData.members.reduce((s, m) => s + m.total, 0);
  $("pt-count").textContent = view === "members" ? `（${ptData.members.length} 位會員，共 ${total} 點）`
    : view === "fb" ? `（${ptData.fb_people.length} 位留言者）` : `（最近 ${ptData.rows.length} 筆）`;
  $("pt-q-wrap").classList.toggle("hidden", view !== "members");
  const q = $("pt-q").value.trim();
  const shown = ptData.members.filter(m => ptMatch(m, q));
  $("pt-members").innerHTML = shown.map(ptMemberRow).join("")
    || `<tr><td colspan="9" class="hint">${q ? "找不到符合的會員。" : "還沒有官網會員。"}</td></tr>`;
  $("pt-fb").innerHTML = ptData.fb_people.map(ptFbRow).join("")
    || '<tr><td colspan="7" class="hint">還沒有客人在粉專留言。</td></tr>';
  $("pt-rows").innerHTML = ptData.rows.map(r => ptRecordRow(r, true)).join("")
    || '<tr><td colspan="7" class="hint">還沒有紀錄。</td></tr>';
}

async function ptLoad() {
  ptData = await api("/api/points");
  $("pt-dev").classList.toggle("hidden", !ptData.dev_mode);
  ptRender();
}

function ptAdjForm() {
  if (!ptData.can_adjust) return "";
  const opts = ptData.kinds.map(k => `<option value="${k.key}">${esc(k.label)}</option>`).join("");
  return `<div style="border:1px solid var(--line); border-radius:.5rem; padding:.7rem .8rem .2rem; margin:.7rem 0;">
    <div class="modal-row">
      <div class="modal-field"><label>加點／扣點</label><select id="pt-adj-kind" onchange="ptAdjKind()">${opts}</select></div>
      <div class="modal-field" style="max-width:8rem;"><label>點數</label>
        <input id="pt-adj-n" type="number" min="1" max="${ptData.max_adjust}" inputmode="numeric"></div>
    </div>
    <div class="modal-field"><label>原因 <span style="color:var(--brand)">*</span><span class="lh">會員在官網看得到這段原因</span></label>
      <input id="pt-adj-reason" maxlength="${ptData.note_max}"></div>
    <div id="pt-adj-err" class="banner danger hidden" style="margin:.4rem 0"></div>
    <div class="modal-field"><button class="btn" type="button" id="pt-adj-btn" onclick="ptAdjust()">加點</button></div>
  </div>`;
}

function ptAdjKind() {
  const k = ptData.kinds.find(x => x.key === $("pt-adj-kind").value);
  $("pt-adj-n").value = k.default || "";
  $("pt-adj-reason").placeholder = PT_ADJ_HINT[k.key] || "";
  $("pt-adj-btn").textContent = k.sign > 0 ? "加點" : "扣點";
}

function ptAdjErr(msg) {
  $("pt-adj-err").textContent = msg;
  $("pt-adj-err").classList.toggle("hidden", !msg);
}

async function ptAdjustSend(m, body) {
  const r = await api(`/api/points/member/${m.id}/adjust`, { method: "POST", body: JSON.stringify(body) });
  if (r.need_confirm) {
    if (!confirm(r.need_confirm)) return null;
    return ptAdjustSend(m, { ...body, confirm: true });
  }
  return r;
}

async function ptAdjust() {
  const m = ptOpen && ptOpen.m;
  const k = ptData.kinds.find(x => x.key === $("pt-adj-kind").value);
  const n = parseInt($("pt-adj-n").value, 10);
  const reason = $("pt-adj-reason").value.trim();
  if (!(n >= 1 && n <= ptData.max_adjust)) { ptAdjErr(`點數要在 1～${ptData.max_adjust} 之間。`); return; }
  if (!reason) { ptAdjErr("請寫原因（會員在官網看得到）。"); return; }
  ptAdjErr("");
  const verb = k.sign > 0 ? "加" : "扣";
  if (!confirm(`幫「${m.display_name || "會員"}」${verb} ${n} 點\n原因：${reason}\n\n確定嗎？`)) return;
  $("pt-adj-btn").disabled = true;
  try {
    const r = await ptAdjustSend(m, { kind: k.key, points: n, reason });
    if (r) await ptAfterChange(`已${verb} ${n} 點，${m.display_name || "會員"}目前 ${r.balance} 點。`);
  } catch (e) {
    ptAdjErr(e.message);
  } finally {
    if ($("pt-adj-btn")) $("pt-adj-btn").disabled = false;
  }
}

function ptModal(title, head, rows) {
  const body = `${head}
    <div class="table-wrap" style="margin-top:.6rem;"><table>
      <thead><tr><th>時間</th><th>項目</th><th>內容／原因</th><th>狀態</th><th style="text-align:right;">點數</th><th>操作</th></tr></thead>
      <tbody>${rows.map(r => ptRecordRow(r, false)).join("") || '<tr><td colspan="6" class="hint">還沒有紀錄。</td></tr>'}</tbody>
    </table></div>`;
  openModal(title, body);
  // 這一頁的對話框要放整張表（不要表格自己的橫向拉桿）
  document.querySelector("#modal-overlay .modal-card").style.width = "min(1000px, 96vw)";
}

async function ptShow() {
  if (!ptOpen) return;
  if (ptOpen.type === "member") {
    const m = ptOpen.m;
    const r = await api(`/api/points/member/${m.id}`);
    const parts = [["每日聽練", m.practice], ["聽練連續 7 天", m.streak], ["線上聽力測驗", m.hearing_online], ["來店打卡", m.hearing_store],
                   ["粉專活動", m.fb], ["兌換贈品", m.redeem], ["點數調整", m.adjust]]
      .filter(([, n]) => n).map(([t, n]) => `${t} ${ptSigned(n)}`).join("、");
    ptModal(`${m.display_name || "會員"} 的集點紀錄`,
      `<div>會員編號 ${m.id}　門市客人：${ptCustomer(m.customer_id, m.customer_name)}${m.fb_linked ? "　已綁 Facebook" : ""}</div>
       <div>目前 <b style="font-size:1.15rem;">${r.balance}</b> 點${parts ? `（${parts}）` : ""}</div>
       ${ptAdjForm()}`,
      r.rows);
    if (ptData.can_adjust) ptAdjKind();
  } else {
    const p = ptOpen.p;
    const r = await api(`/api/fb/points?person=${encodeURIComponent(p.person_id)}`);
    const rows = r.rows.map(x => ({ ...x, kind: "fb", at: x.created_at, source_label: "粉專活動",
      detail: (x.post_title || "粉專貼文") + (x.excerpt ? `：${x.excerpt}` : "") }));
    ptModal(`${p.person_name || "留言者"} 的粉專留言`,
      `<div>官網會員：${ptFbMember(p.member)}</div>
       <div>累積點數：<b>${p.points}</b>（計分的貼文 ${p.counted} 則，全部留言 ${p.comments} 則）</div>`, rows);
  }
}

async function ptOpenMember(id) {
  ptOpen = { type: "member", m: ptData.members.find(m => m.id === id) };
  try { await ptShow(); } catch (e) { setStatus(e.message, "danger"); }
}

async function ptOpenFb(i) {
  ptOpen = { type: "fb", p: ptData.fb_people[i] };
  try { await ptShow(); } catch (e) { setStatus(e.message, "danger"); }
}

async function ptAfterChange(msg) {
  setStatus(msg, "success");
  const modalOn = !$("modal-overlay").classList.contains("hidden");
  await ptLoad();
  if (modalOn && ptOpen) {
    if (ptOpen.type === "member") ptOpen.m = ptData.members.find(m => m.id === ptOpen.m.id) || ptOpen.m;
    else ptOpen.p = ptData.fb_people.find(p => p.person_id === ptOpen.p.person_id) || ptOpen.p;
    await ptShow();
  }
}

const ptActionUrl = (kind, id, action) => kind === "fb" ? `/api/fb/points/${id}/${action}` : `/api/points/ledger/${id}/${action}`;

async function ptVoid(kind, id) {
  const reason = prompt("作廢這筆點數的原因（例如：洗版、重複帳號、測試）：", "");
  if (reason === null) return;
  if (!reason.trim()) { setStatus("請寫作廢原因。", "danger"); return; }
  try {
    await api(ptActionUrl(kind, id, "void"), { method: "POST", body: JSON.stringify({ reason }) });
    await ptAfterChange("已作廢。");
  } catch (e) { setStatus(e.message, "danger"); }
}

async function ptRestore(kind, id) {
  if (!confirm("恢復這筆點數？")) return;
  try {
    const r = await api(ptActionUrl(kind, id, "restore"), { method: "POST", body: "{}" });
    await ptAfterChange(r.status === "counted" ? "已恢復計分。" : "已恢復；這則貼文他已經有別的留言在計分，所以這筆不另外加點。");
  } catch (e) { setStatus(e.message, "danger"); }
}

$("pt-q").addEventListener("input", ptRender);

$("pt-view").querySelectorAll("button").forEach(b => b.addEventListener("click", () => {
  setSeg("pt-view", b.dataset.v);
  history.replaceState(null, "", b.dataset.v === "members" ? location.pathname : `#${b.dataset.v}`);
  ptRender();
}));

initHeader().then(async me => {
  if (!(me.menus || []).includes("customers")) { location.href = "/calendar"; return; }
  const v = location.hash.slice(1);
  setSeg("pt-view", ["fb", "rows"].includes(v) ? v : "members");
  await ptLoad();
}).catch(e => setStatus(e.message || "載入失敗", "danger"));
