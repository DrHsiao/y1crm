/* fb_posts.js — 粉專貼文：本機 AI 每天寫的草稿，勾選後發到粉專、同步到官網聽力專欄（選單權限 web）。
   依賴 common.js：$/esc/api/isoDate/dateLabel/DF/setStatus/initHeader

   John 2026-11-08：每天 06:00 寫好、預設勾選，10:30 前門市沒取消勾選就自動發（伺服器端 fb_drafts.auto_publish）。
   所以「要發」的勾選存在伺服器（picked），一改就存；有紅色錯誤或黃色提醒的不會自動發。
   改字：輸入時只記在 dirty，離開欄位（change）就存；按發佈、多寫一則之前會先全部存好。
   配圖（api/post_art.py）：每日草稿寫好就從圖庫挑一張真實照片（John 11-08：好評分享以外都用真實照片），
   有圖的發成相片貼文、同步專欄時當封面。挑圖不用 GPU，按「換一張」馬上換好。 */
"use strict";

let info = { pending: [], recent: [], categories: {}, column_categories: [], auto: {} };
const dirty = new Map();          // 草稿 id → 還沒存的 {message?, link?, seo?}

const PICK_CATS = ["own", "news", "care", "health", "family", "ask", "season", "web", "tool"];
const STATUS_LABEL = { published: "已發佈", dev: "開發模式（沒有真的發）" };

const byId = id => info.pending.find(d => d.id === id);
const pickedIds = () => info.pending.filter(d => d.picked).map(d => d.id);

// ── 畫面 ────────────────────────────────────────────────────
function renderState() {
  const el = $("fp-state");
  const a = info.auto || {};
  const hh = String(a.hour).padStart(2, "0");
  if (info.dev_mode) {
    el.className = "lc-state warn";
    el.textContent = "開發模式：還沒接上粉專，發佈不會真的送出";
  } else if (a.enabled && a.publish_at) {
    el.className = "lc-state on";
    el.textContent = `每天 ${hh}:00 寫 ${a.count} 則・${a.publish_at} 自動發勾著的`;
  } else if (a.enabled) {
    el.className = "lc-state on";
    el.textContent = `每天 ${hh}:00 寫 ${a.count} 則・手動發佈`;
  } else {
    el.className = "lc-state off";
    el.textContent = "自動寫稿已關閉";
  }
  $("fp-auto-note").innerHTML = a.publish_at
    ? `每天 ${String(a.hour).padStart(2, "0")}:00 寫好、預設勾選；<b>${esc(a.publish_at)} 前沒有取消勾選的，會自動發到粉專</b>
       （勾了「同步到聽力專欄」的也會一起放上官網）。不想發的請取消勾選；有紅色或黃色提醒的不會自動發，要看過再手動發。`
    : "勾選要發的貼文，按「發佈勾選的」才會送到粉專。";
}

function flagsHtml(d) {
  return [
    ...(d.check_errors || []).map(t => `<li class="price">${esc(t)}</li>`),
    ...(d.check_warnings || []).map(t => `<li class="claim">${esc(t)}</li>`),
    d.error ? `<li class="price">上次發佈：${esc(d.error)}</li>` : "",
  ].join("");
}

function autoTag(d) {
  if (d.auto) return `<span class="fp-auto on">⏰ ${esc(d.auto_note)}</span>`;
  return d.auto_note ? `<span class="fp-auto">${esc(d.auto_note)}</span>` : "";
}

function seoHtml(d) {
  const s = d.seo || {};
  const cats = (info.column_categories || []).map(c =>
    `<option${c === s.category ? " selected" : ""}>${esc(c)}</option>`).join("");
  const ev = `oninput="fpEditSeo(${d.id})" onchange="fpSave(${d.id})"`;
  return `<div class="fp-web">
    <label class="fp-pick"><input type="checkbox" ${d.web_sync ? "checked" : ""} onchange="fpFlag(${d.id}, 'web_sync', this.checked)">
      同步到官網「聽力專欄」</label>
    <details>
      <summary>官網標題：${esc(s.title || "（未填）")}</summary>
      <div class="fp-seo" id="fp-seo-${d.id}">
        <label>官網標題<input data-f="title" maxlength="40" value="${esc(s.title || "")}" ${ev}></label>
        <label>專欄分類<select data-f="category" ${ev}>${cats}</select></label>
        <label class="wide">搜尋關鍵字<span class="hint">（Google 搜尋用，逗號或頓號分開）</span>
          <input data-f="keywords" maxlength="300" value="${esc((s.keywords || []).join("、"))}" ${ev}></label>
        <label class="wide">摘要<span class="hint">（出現在 Google 搜尋結果）</span>
          <textarea data-f="summary" rows="2" maxlength="120" ${ev}>${esc(s.summary || "")}</textarea></label>
        <input type="hidden" data-f="slug" value="${esc(s.slug || "")}">
      </div>
    </details>
  </div>`;
}

function imgHtml(d, sent = false) {
  const m = d.image_meta || {};
  const stock = m.src === "stock";            // 圖庫的真實照片（api/stock_art.py）；ai＝以前畫的，發佈前會換成照片
  const btn = `<button type="button" class="btn sm ghost" onclick="fpArt(${d.id}, ${!!d.image_url})">📷 ${d.image_url ? "換一張" : "挑一張"}</button>`;
  const what = stock ? `圖庫照片：${m.scene || ""}` : "以前 AI 畫的插圖（發佈前會換成真實照片）";
  if (sent) {
    const shown = d.card_url || d.image_url;      // 發出去的是圖卡（有的話）
    return shown ? `<a href="${esc(shown)}" target="_blank" rel="noopener" title="${esc(what)}"><img class="fp-thumb${d.card_url ? " card" : ""}" src="${esc(shown)}" alt="${d.card_url ? "圖卡" : stock ? "圖庫照片" : "AI 插圖"}"></a>${btn}` : btn;
  }
  const c = d.card_meta || {};
  const card = d.image_url ? `<div class="fp-card">
      <span class="hint"><b>🖼 圖卡</b>（照片＋大字標題，發到粉專的是這張）：</span>
      <label>大字標題 <input id="fp-ct-${d.id}" maxlength="18" value="${esc(c.title || "")}" placeholder="例：電視越開越大聲？"></label>
      <label>第二行 <input id="fp-cs-${d.id}" maxlength="22" value="${esc(c.sub || "")}" placeholder="例：先試試這兩個方法"></label>
      <span class="fp-img-btns">
        <button type="button" class="btn sm" onclick="fpCard(${d.id}, false)">更新圖卡</button>
        <button type="button" class="btn sm ghost" onclick="fpCard(${d.id}, true)">🤖 請 AI 重擬標題</button>
      </span>
      ${d.card_url ? "" : '<span class="hint">還沒做圖卡：發佈時會自動做。</span>'}
    </div>` : "";
  return `<div class="fp-img" id="fp-img-${d.id}">
    ${d.card_url
      ? `<a href="${esc(d.card_url)}" target="_blank" rel="noopener"><img class="card" src="${esc(d.card_url)}" alt="圖卡"></a>`
      : d.image_url
      ? `<a href="${esc(d.image_url)}" target="_blank" rel="noopener"><img src="${esc(d.image_url)}" alt=""></a>`
      : '<div class="fp-noimg">沒有配圖：會發成純文字貼文' + (d.link ? "（有附連結的話顯示連結預覽）" : "") + "</div>"}
    <div class="fp-img-side">
      ${d.image_url ? `<span class="hint">${stock
        ? `<b>📷 圖庫照片</b>：${esc(m.scene || "")}（${esc((m.credit || {}).source || "")}）`
        : "<b>🎨 以前 AI 畫的插圖</b>：發佈前會自動換成圖庫的真實照片"}。發成「相片＋文字」貼文${d.link ? "，附的連結會放進文字裡" : ""}；勾了同步的，專欄封面用原照片（不加字）。</span>` : ""}
      <span class="fp-img-btns">${btn}${d.image_url ? `<button type="button" class="btn sm ghost" onclick="fpNoImage(${d.id})">不要圖</button>` : ""}</span>
      ${card}
    </div>
  </div>`;
}

function itemHtml(d) {
  const src = d.source_url
    ? `<span class="hint">來源：<a href="${esc(d.source_url)}" target="_blank" rel="noopener">${esc(d.source_name || "原文")}</a></span>` : "";
  const who = d.created_by ? `${esc(d.created_by)} 請 AI 寫的` : "每日自動";
  return `<article class="fp-item${d.picked ? " on" : ""}" id="fp-${d.id}">
    <div class="fp-head">
      <label class="fp-pick"><input type="checkbox" ${d.picked ? "checked" : ""} onchange="fpFlag(${d.id}, 'picked', this.checked)"> 要發</label>
      <span class="bk-tag">${esc(d.category_label)}</span>
      <b class="fp-topic">${esc(d.topic)}</b>
      ${src}
      <span class="hint fp-who">${who}</span>
    </div>
    <div class="fp-autos" id="fp-auto-${d.id}">${autoTag(d)}</div>
    ${imgHtml(d)}
    <textarea rows="9" maxlength="5000" oninput="fpEdit(${d.id}, 'message', this.value)" onchange="fpSave(${d.id})">${esc(d.message)}</textarea>
    <div class="fp-link">
      <span class="hint">附連結</span>
      <input value="${esc(d.link || "")}" placeholder="不附連結" oninput="fpEdit(${d.id}, 'link', this.value)" onchange="fpSave(${d.id})">
      ${d.link ? `<a href="${esc(d.link)}" target="_blank" rel="noopener">開啟</a>` : ""}
    </div>
    ${seoHtml(d)}
    <ul class="wa-flags" id="fp-flags-${d.id}">${flagsHtml(d)}</ul>
    <div class="fp-actions">
      <span class="hint" id="fp-len-${d.id}">${d.message.length} 字</span>
      <button type="button" class="btn sm ghost" onclick="fpRegen(${d.id}, this)">換一則</button>
      <button type="button" class="btn sm ghost" onclick="fpDiscard(${d.id})">不要</button>
    </div>
  </article>`;
}

function render() {
  const days = {};
  info.pending.forEach(d => (days[d.day] ||= []).push(d));
  const now = isoDate(new Date());
  $("fp-list").innerHTML = Object.keys(days).sort().reverse().map(day =>
    `<h4 class="fp-day">${esc(dateLabel(day))}${day === now ? "　今天" : ""}</h4>` + days[day].map(itemHtml).join("")
  ).join("") || `<p class="hint fp-empty">目前沒有草稿。${info.auto.enabled
    ? `每天 ${String(info.auto.hour).padStart(2, "0")}:00 後會自動寫好；` : ""}也可以用上面的「多寫一則」。</p>`;
  $("fp-count").textContent = info.pending.length ? `（${info.pending.length} 則待選）` : "";
  renderPicked();
  renderRecent();
}

function renderPicked() {
  const n = pickedIds().length;
  $("fp-picked").textContent = n ? `已勾選 ${n} 則` : "還沒勾選";
  $("fp-publish").disabled = !n;
}

function renderRecent() {
  const fbUrl = id => `https://www.facebook.com/${encodeURIComponent(id)}`;
  $("fp-recent").innerHTML = info.recent.length ? info.recent.map(d => `
    <div class="fp-sent">
      ${imgHtml(d, true)}
      <span class="hint">${esc(DF.fromISODateTime(d.published_at || ""))}</span>
      <span class="bk-tag">${esc(d.category_label)}</span>
      <span class="fp-topic">${esc(d.topic)}</span>
      <span class="hint">${esc(STATUS_LABEL[d.status] || d.status)}${d.published_by ? `・${esc(d.published_by)}` : ""}</span>
      ${d.fb_post_id ? `<a href="${fbUrl(d.fb_post_id)}" target="_blank" rel="noopener">看貼文</a>` : ""}
      ${d.web_url ? `<a href="${esc(d.web_url)}" target="_blank" rel="noopener">聽力專欄</a>` : ""}
      ${d.error ? `<span class="fp-err">${esc(d.error)}</span>` : ""}
    </div>`).join("") : '<p class="hint">還沒有發佈過。</p>';
}

function replaceItem(d) {
  const i = info.pending.findIndex(x => x.id === d.id);
  if (i >= 0) info.pending[i] = d;
  const el = $(`fp-${d.id}`);
  if (el) el.outerHTML = itemHtml(d);
}

function refreshFlags(d) {          // 只換提醒和自動發標示，不重畫（免得打斷正在改的欄位）
  const i = info.pending.findIndex(x => x.id === d.id);
  if (i >= 0) info.pending[i] = d;
  $(`fp-flags-${d.id}`).innerHTML = flagsHtml(d);
  $(`fp-auto-${d.id}`).innerHTML = autoTag(d);
  $(`fp-${d.id}`)?.classList.toggle("on", d.picked);
  renderPicked();
}

// ── 編輯 ────────────────────────────────────────────────────
async function fpFlag(id, field, on) {
  try {
    refreshFlags(await api(`/api/fb/post-drafts/${id}`, { method: "PUT", body: JSON.stringify({ [field]: on }) }));
  } catch (e) {
    setStatus(`沒有存到：${e.message}`, "danger");
    await load();
  }
}

function fpEdit(id, field, value) {
  dirty.set(id, { ...(dirty.get(id) || {}), [field]: value });
  if (field === "message") $(`fp-len-${id}`).textContent = `${value.trim().length} 字（未存）`;
}

function fpEditSeo(id) {
  const seo = {};
  $(`fp-seo-${id}`).querySelectorAll("[data-f]").forEach(el => { seo[el.dataset.f] = el.value.trim(); });
  fpEdit(id, "seo", seo);
}

async function fpSave(id) {
  const body = dirty.get(id);
  if (!body) return true;
  dirty.delete(id);
  try {
    const d = await api(`/api/fb/post-drafts/${id}`, { method: "PUT", body: JSON.stringify(body) });
    refreshFlags(d);
    $(`fp-len-${id}`).textContent = `${d.message.length} 字`;
    return true;
  } catch (e) {
    dirty.set(id, { ...body, ...(dirty.get(id) || {}) });
    setStatus(`沒有存到：${e.message}`, "danger");
    return false;
  }
}

async function flushAll() {
  let ok = true;
  for (const id of [...dirty.keys()]) ok = (await fpSave(id)) && ok;
  return ok;
}

// ── AI 寫稿 ─────────────────────────────────────────────────
async function fpWrite() {
  const cat = $("fp-cat").value, brief = $("fp-brief").value.trim();
  if (cat === "own" && !brief) { setStatus("「自己出題」請寫下想寫的題目或重點。", "warning"); $("fp-brief").focus(); return; }
  if (!(await flushAll())) return;
  const btn = $("fp-write");
  btn.disabled = true;
  btn.textContent = "AI 寫作中…";
  try {
    const d = await api("/api/fb/post-drafts", { method: "POST", body: JSON.stringify({ category: cat, brief }) });
    info.pending.unshift(d);
    $("fp-brief").value = "";
    render();
    $(`fp-${d.id}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    setStatus("寫好了，請看過再決定要不要發（自己請 AI 寫的不會自動發）。", "success");
  } catch (e) { setStatus(e.message, "danger"); }
  btn.disabled = false;
  btn.textContent = "寫一則";
}

async function fpRegen(id, btn) {
  if (!confirm("讓 AI 換一個寫法？目前的內容（包括你改過的字和官網標題）會被換掉。")) return;
  dirty.delete(id);
  btn.disabled = true;
  btn.textContent = "重寫中…";
  try {
    replaceItem(await api(`/api/fb/post-drafts/${id}/regenerate`, { method: "POST" }));
  } catch (e) {
    setStatus(e.message, "danger");
    btn.disabled = false;
    btn.textContent = "換一則";
  }
}

async function fpDiscard(id) {
  if (!confirm("不要這一則？（不會發，同一個題目 60 天內不會再出）")) return;
  try {
    await api(`/api/fb/post-drafts/${id}`, { method: "DELETE" });
    dirty.delete(id);
    info.pending = info.pending.filter(d => d.id !== id);
    render();
  } catch (e) { setStatus(e.message, "danger"); }
}

// ── 配圖 ────────────────────────────────────────────────────
async function fpArt(id, again) {
  const sent = !byId(id);
  if (sent && !confirm("換一張圖庫照片？粉專上已發的貼文不會變（Facebook 不讓程式改圖），同步的專欄封面會換成新的。")) return;
  try {
    const d = await api(`/api/fb/post-drafts/${id}/art`, { method: "POST", body: JSON.stringify({ force: again }) });
    if (sent) await load();
    else replaceItem(d);
    setStatus(`換成：${(d.image_meta || {}).scene || "圖庫照片"}`, "success");
  } catch (e) { setStatus(e.message, "danger"); }
}

async function fpCard(id, ai) {
  const body = ai ? { ai: true }
    : { title: (document.getElementById(`fp-ct-${id}`) || {}).value || "", sub: (document.getElementById(`fp-cs-${id}`) || {}).value || "" };
  setStatus(ai ? "本機 AI 擬標題中…（約 10 秒）" : "圖卡更新中…", "");
  try {
    replaceItem(await api(`/api/fb/post-drafts/${id}/card`, { method: "PUT", body: JSON.stringify(body) }));
    setStatus("圖卡更新好了", "success");
  } catch (e) { setStatus(e.message, "danger"); }
}

async function fpNoImage(id) {
  if (!confirm("拿掉這則的配圖？（發成純文字貼文；有附連結的話顯示連結預覽）")) return;
  try { replaceItem(await api(`/api/fb/post-drafts/${id}/image`, { method: "DELETE" })); }
  catch (e) { setStatus(e.message, "danger"); }
}

// ── 發佈 ────────────────────────────────────────────────────
async function fpPublish(force = false) {
  if (!(await flushAll())) return;
  const ids = pickedIds();
  if (!ids.length) return;
  if (!force) {
    const many = ids.length > 1 ? "\n\n一次發好幾則，粉專上會同時出現。" : "";
    const where = info.dev_mode ? "（開發模式：不會真的送出）" : "發出去所有人都看得到。";
    const web = info.pending.some(d => d.picked && d.web_sync) ? "\n勾了「同步到聽力專欄」的也會放上官網。" : "";
    if (!confirm(`現在就把勾選的 ${ids.length} 則發到粉專嗎？\n${where}${web}${many}`)) return;
  }
  const btn = $("fp-publish");
  btn.disabled = true;
  btn.textContent = "發佈中…";
  try {
    const r = await api("/api/fb/post-drafts/publish", { method: "POST", body: JSON.stringify({ ids, force }) });
    if (r.errors) {
      for (const [id, errs] of Object.entries(r.errors)) {
        const d = byId(+id);
        if (d) { d.check_errors = errs; $(`fp-flags-${id}`).innerHTML = flagsHtml(d); }
      }
      setStatus("有幾則還不能發，請看紅色的提醒。勾選的都還沒發出。", "danger");
      return;
    }
    if (r.warnings) {
      const lines = Object.entries(r.warnings).map(([id, ws]) => `・${byId(+id)?.topic || id}：${ws.join("、")}`).join("\n");
      if (confirm(`這些字眼可能被當成療效、促銷或廣告：\n${lines}\n\n確定照現在的內容發佈嗎？`)) {
        btn.textContent = "發佈勾選的";
        return fpPublish(true);
      }
      return;
    }
    const n = s => r.results.filter(x => x.status === s).length;
    const failed = r.results.filter(x => x.status === "failed");
    const webErr = r.results.filter(x => x.web_error);
    await load();
    if (failed.length) setStatus(`發佈 ${n("published") + n("dev")} 則，${failed.length} 則失敗：${failed[0].error}`, "danger");
    else if (webErr.length) setStatus(webErr[0].web_error, "danger");
    else setStatus(n("dev") ? `開發模式：${n("dev")} 則沒有真的送出（已記錄）。` : `已發到粉專 ${n("published")} 則。`, "success");
  } catch (e) {
    setStatus(e.message, "danger");
  } finally {
    btn.textContent = "發佈勾選的";
    renderPicked();
  }
}

// ── 載入 ────────────────────────────────────────────────────
async function load() {
  info = await api("/api/fb/post-drafts");
  renderState();
  render();
}

$("fp-cat").addEventListener("change", () => {
  const own = $("fp-cat").value === "own";
  $("fp-new-hint").textContent = own
    ? "寫下題目或重點，AI 照著寫；本機 AI 寫一則約半分鐘。"
    : "題目可以留空，由 AI 從這個類別挑；寫了就照你的題目、用這個類別的寫法。";
});

initHeader().then(async me => {
  if (!(me.menus || []).includes("web")) { location.href = "/calendar"; return; }
  try {
    await load();
    $("fp-cat").innerHTML = PICK_CATS.map(c => `<option value="${c}">${esc(info.categories[c] || c)}</option>`).join("");
  } catch (e) { setStatus(e.message, "danger"); }
}).catch(() => {});

// 停在頁面上過了自動發佈的時間：每 5 分鐘重新整理一次（沒有正在改的才刷新）
setInterval(() => { if (!dirty.size && !document.querySelector(".fp-item :focus")) load().catch(() => {}); }, 300000);
window.addEventListener("beforeunload", e => { if (dirty.size) { flushAll(); e.preventDefault(); } });
