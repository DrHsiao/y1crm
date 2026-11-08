/* web_admin.js — 官網管理頁（選單權限 web）：編輯官網 y1web 的關於韻譯／聽力專欄／好評分享／使用小教室／補助小幫手。
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
  share: "「好評分享」只會顯示「發佈」而且勾了「已取得當事人同意」的。評論轉貼只能用本門市的評論，原文照登或刪節，不能改寫；請勿張貼別家業者的評價。",
  care: "「使用小教室」依排序顯示，交機時可以把 QR 貼紙貼在助聽器盒子上，客人在家掃了就看得到。",
  subsidy: "「補助小幫手」：民眾回答幾個問題，官網列出可能適用的補助並連到官方網站。規定常變動，發佈前請先向主管機關確認；不要寫金額。",
};
const SITE_PATH = { about: "/about", article: "/column", share: "/share", care: "/care", subsidy: "/subsidy" };
const FB_KINDS = ["article", "care"];   // 有自己網頁的才能分享到粉專（同 api/fb_page.SHARE_PATHS）
const kind = () => getSeg("wa-kind");
const isQuote = () => (cur?.kind || kind()) === "share" && getSeg("wa-format") === "quote";
// 官網顯示的評論帳號：末 3 個字換成 *，不到 4 個字留第一個字（同 api/web_admin.mask_name）
const maskName = n => { n = (n || "").trim(); const k = Math.min(3, n.length - 1); return k > 0 ? n.slice(0, -k) + "*".repeat(k) : n; };
const statusTag = r => r.status === "published"
  ? '<span class="code-badge">發佈</span>' : '<span class="lh">草稿</span>';
const coverTag = r => r.cover ? "🖼" : "";
const picksTag = (codes, labels, none) => (codes || []).map(c => esc(labels[c] || c)).join("、") || `<span class="lh">${none}</span>`;

// ── 列表 ─────────────────────────────────────────────────────
async function loadList() {
  const k = kind();
  const r = await api(`/api/web/posts?kind=${k}`);
  rows = r.rows;
  $("wa-count").textContent = `（${rows.length} 筆）`;
  $("wa-hint").textContent = KIND_HINT[k];
  $("wa-site").href = META.site_url + SITE_PATH[k];
  $("wa-qr").href = META.site_url + "/care/qr";
  $("wa-qr").classList.toggle("hidden", k !== "care");
  $("wa-links").classList.toggle("hidden", k !== "share");
  const head = {
    about: "<th>排序</th><th>英文小標</th><th>標題</th><th>狀態</th><th>圖</th><th>最後修改</th>",
    article: "<th>發佈日期</th><th>分類</th><th>標題</th><th>狀態</th><th>圖</th><th>最後修改</th>",
    share: "<th>發佈日期</th><th>分享人</th><th>標題</th><th>同意</th><th>狀態</th><th>最後修改</th>",
    care: "<th>排序</th><th>標題</th><th>適用</th><th>狀態</th><th>圖</th><th>最後修改</th>",
    subsidy: "<th>排序</th><th>標題</th><th>適用條件</th><th>受理單位</th><th>狀態</th><th>最後修改</th>",
  }[k];
  $("wa-head").innerHTML = `<tr>${head}</tr>`;
  const modified = r => `${esc(DF.fromISODateTime(r.updated_at))}<br><span class="lh">${esc(r.updated_by || "")}</span>`;
  $("wa-body").innerHTML = rows.map(r => {
    const cells = {
      about: `<td>${r.sort}</td><td>${esc(r.extra.en || "")}</td><td>${esc(r.title)}</td>
              <td>${statusTag(r)}</td><td>${coverTag(r)}</td><td>${modified(r)}</td>`,
      article: `<td>${esc(DF.fromISO(r.publish_date)) || "—"}</td><td>${esc(r.category || "")}</td><td>${esc(r.title)}</td>
              <td>${statusTag(r)}</td><td>${coverTag(r)}</td><td>${modified(r)}</td>`,
      share: `<td>${esc(DF.fromISO(r.publish_date)) || "—"}</td><td>${esc(r.extra.person || "")}</td><td>${shareCell(r)}</td>
              <td>${r.extra.consent ? "✓" : '<span class="lh">未勾</span>'}</td><td>${statusTag(r)}</td><td>${modified(r)}</td>`,
      care: `<td>${r.sort}</td><td>${esc(r.title)}</td><td>${picksTag(r.extra.types, META.care_types, "全部")}</td>
              <td>${statusTag(r)}</td><td>${coverTag(r)}</td><td>${modified(r)}</td>`,
      subsidy: `<td>${r.sort}</td><td>${esc(r.title)}</td><td>${picksTag(r.extra.when, META.subsidy_conditions, "一般資訊")}</td>
              <td>${esc(r.extra.agency || "")}</td><td>${statusTag(r)}</td><td>${modified(r)}</td>`,
    }[k];
    return `<tr onclick="waEdit(${r.id})" style="cursor:pointer;">${cells}</tr>`;
  }).join("") || `<tr><td colspan="6" class="hint">還沒有內容，按「＋ 新增」開始。</td></tr>`;
}

function shareCell(r) {
  if (r.extra.format !== "quote") return esc(r.title);
  const p = META.share_platforms[r.extra.platform];
  const text = (r.body_md || "").replace(/\s+/g, " ");
  return `<span class="lh">${esc(p ? p.label : "")}${r.extra.rating ? " " + "★".repeat(r.extra.rating) : ""}</span><br>`
    + esc(text.length > 40 ? text.slice(0, 40) + "…" : text);
}

function renderShareLinks() {
  const links = META.share_links || {};
  const parts = Object.entries(META.share_platforms).filter(([, p]) => p.all)
    .map(([k, p]) => `${esc(p.label)}：${links[k] ? `<a href="${esc(links[k])}" target="_blank" rel="noopener">${esc(links[k])}</a>` : "未設定"}`);
  parts.push(`彈窗地圖：${links.map ? "已貼嵌入碼" : "用門市名稱搜尋"}`);
  $("wa-links-v").innerHTML = parts.join("　");
}

// ── 編輯 ─────────────────────────────────────────────────────
function showKindFields(k) {
  const eff = k === "share" && getSeg("wa-format") === "quote" ? "quote" : k;    // 評論轉貼另有一組欄位
  document.querySelectorAll("#wa-editor .wa-only").forEach(el =>
    el.classList.toggle("hidden", !el.dataset.kinds.split(" ").includes(eff)));
  if (eff === "quote") showPlatformFields();
  $("wa-cover-label").innerHTML = eff === "quote"
    ? '圖片<span class="lh">選填一張：評論附的照片、心得卡照片。官網放在卡片上方（裁成 3:2），點開看完整的圖</span>' : "封面圖";
  if (cur) renderCover();
}

// 原文連結怎麼取得：Google 評論頁不准別的網站嵌入，官網用彈出視窗開；Facebook 貼文官網會直接嵌進彈窗（要公開）
const URL_HINT = {
  google: "選填：在那則評論按「分享評論」→「複製連結」。官網點開時用另一個小視窗開原文（Google 不准嵌入）",
  facebook: "選填：點貼文上的時間 → 複製網址列（facebook.com/…/posts/…）。公開的貼文官網會直接顯示在彈窗裡",
};
function showPlatformFields() {
  const k = $("wa-platform").value, p = META.share_platforms[k] || {};
  $("wa-rating-f").classList.toggle("hidden", !p.stars);
  $("wa-url-f").classList.toggle("hidden", p.link === "none");
  $("wa-url-hint").textContent = URL_HINT[k] || "選填：原文的網址";
  $("wa-url").placeholder = k === "facebook" ? "https://www.facebook.com/…/posts/…" : k === "google" ? "https://maps.app.goo.gl/…" : "https://…";
}

function renderChecks(id, labels, picked) {
  $(id).innerHTML = Object.entries(labels).map(([v, t]) =>
    `<label><input type="checkbox" value="${esc(v)}"${(picked || []).includes(v) ? " checked" : ""}> ${esc(t)}</label>`).join("");
  $(id).querySelectorAll("input").forEach(el => el.addEventListener("change", () => { dirty = true; }));
}
const checked = id => [...$(id).querySelectorAll("input:checked")].map(el => el.value);

function renderCover() {
  const box = $("wa-cover");
  const what = isQuote() ? "圖片" : "封面圖";
  if (!cur.id) { box.innerHTML = `<div class="field-info">先儲存，再上傳${what}。</div>`; return; }
  const p = isQuote() && META.share_platforms[$("wa-platform").value];
  const none = p ? `沒有圖片，官網會顯示「${esc(p.label)}」的標誌。` : "沒有封面圖。";
  box.innerHTML = (cur.cover
    ? `<img class="wa-cover-img" src="/api/web/media/${esc(cur.cover)}" alt="">` : `<div class="field-info">${none}</div>`)
    + `<div style="display:flex; gap:.5rem; margin-top:.5rem;">
         <button type="button" class="btn sm outline" onclick="waPick('cover')">📷 ${cur.cover ? "更換" : "上傳"}</button>
         ${cur.cover ? '<button type="button" class="btn sm outline" onclick="waRemoveCover()">移除</button>' : ""}
       </div>`;
}

async function waEdit(id) {
  if (!(await confirmLeave())) return;
  const k = kind();
  cur = id ? { ...rows.find(r => r.id === id) } : { id: null, kind: k, status: "draft", extra: {}, sort: (rows.length + 1) * 10 };
  const x = cur.extra;
  setSeg("wa-format", k === "share" && (id ? x.format !== "quote" : false) ? "story" : "quote");   // 新增時預設評論轉貼
  $("wa-platform").innerHTML = Object.entries(META.share_platforms)
    .map(([v, p]) => `<option value="${esc(v)}"${v === (x.platform || "google") ? " selected" : ""}>${esc(p.label)}</option>`).join("");
  $("wa-rating").value = String(x.rating || 5);
  $("wa-posted").value = DF.fromISO(x.posted || "");
  $("wa-author").value = x.author || "";
  $("wa-qperson").value = x.format === "quote" && x.person !== maskName(x.author) ? (x.person || "") : "";
  $("wa-qperson").placeholder = maskName(x.author);
  $("wa-url").value = x.url || "";
  $("wa-original").value = x.original || "";
  $("wa-shown").value = x.format === "quote" && cur.body_md !== x.original ? (cur.body_md || "") : "";
  $("wa-paste").value = "";
  $("wa-parse-msg").innerHTML = "";
  $("wa-flags").innerHTML = "";
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
  renderChecks("wa-types", META.care_types, cur.extra.types);
  renderChecks("wa-when", META.subsidy_conditions, cur.extra.when);
  $("wa-video").value = cur.extra.video || "";
  $("wa-agency").value = cur.extra.agency || "";
  $("wa-link").value = cur.extra.link || "";
  $("wa-summary").value = cur.summary || "";
  $("wa-body-md").value = cur.body_md || "";
  setSeg("wa-status", cur.status || "draft");
  $("wa-date").value = DF.fromISO(cur.publish_date || "");
  $("wa-sort").value = cur.sort ?? 0;
  $("wa-slug").value = cur.slug || "";
  $("wa-del").classList.toggle("hidden", !cur.id);
  $("wa-fb").classList.toggle("hidden", !(cur.id && FB_KINDS.includes(cur.kind || k)));
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
  if (isQuote()) {
    const body = {
      kind: k, format: "quote", status: getSeg("wa-status"), consent: $("wa-consent").checked,
      platform: $("wa-platform").value, url: $("wa-url").value.trim(), rating: $("wa-rating").value,
      author: $("wa-author").value.trim(), person: $("wa-qperson").value.trim(),
      original: $("wa-original").value.trim(), body_md: $("wa-shown").value.trim(), slug: cur.slug || "",
    };
    body.posted = dfToISO($("wa-posted").value, "評論日期");
    body.publish_date = dfToISO($("wa-date").value, "發佈日期");
    return body;
  }
  const body = {
    format: k === "share" ? "story" : undefined,
    kind: k, title: $("wa-title").value.trim(), summary: $("wa-summary").value.trim(),
    body_md: $("wa-body-md").value, status: getSeg("wa-status"), slug: $("wa-slug").value.trim(),
    sort: $("wa-sort").value || 0, en: $("wa-en").value.trim(), category: $("wa-category").value,
    person: $("wa-person").value.trim(), model: $("wa-model").value.trim(), hearing: $("wa-hearing").value.trim(),
    consent: $("wa-consent").checked,
    types: checked("wa-types"), video: $("wa-video").value.trim(),
    when: checked("wa-when"), agency: $("wa-agency").value.trim(), link: $("wa-link").value.trim(),
  };
  body.publish_date = dfToISO($("wa-date").value, "發佈日期");
  return body;
}

async function waSave(force) {
  let body;
  try { body = collect(); } catch (e) { showErr(e.message); return; }
  if (!body.title && body.format !== "quote") { showErr("請填寫標題。"); return; }
  body.force = !!force;
  try {
    const r = cur.id
      ? await api(`/api/web/posts/${cur.id}`, { method: "PUT", body: JSON.stringify(body) })
      : await api("/api/web/posts", { method: "POST", body: JSON.stringify(body) });
    if (!r.ok && r.warnings) {      // 評論轉貼有療效／誇大用語：確認後再存一次
      if (confirm("請再確認一次用語：\n・" + r.warnings.join("\n・") + "\n\n確定照這樣發佈嗎？（也可以用節錄刪掉那一句）")) return waSave(true);
      return;
    }
    dirty = false;
    setStatus(body.status === "published" ? "已儲存並發佈，約一分鐘內出現在官網。" : "已儲存為草稿。", "success");
    await loadList();
    await waEdit(r.id);     // 留在編輯畫面（新增後就可以上傳封面圖）
  } catch (e) { showErr(e.message); }
}

// ── 評論轉貼：拆解整段評論、檢查用語（api/web_admin.review_draft，不存檔）──────────
function renderFlags(d) {
  const flags = d.flags || [];
  $("wa-flags").innerHTML = flags.length ? `<ul class="wa-flags">${flags.map(f => f.price
    ? `<li class="price">不能發佈（提到價格「${esc(f.price)}」）：${esc(f.text)}</li>`
    : `<li class="claim">要確認（${f.claims.map(w => "「" + esc(w) + "」").join("")}）：${esc(f.text)}</li>`).join("")}</ul>
    <div class="wa-tools"><button type="button" class="btn sm outline" id="wa-use-sug">用建議的節錄（刪掉上面這幾句）</button>
      <button type="button" class="btn sm ghost" id="wa-use-orig">原文照登</button></div>`
    : '<p class="hint">用語檢查：沒有發現價格或療效用語。</p>';
  if (!flags.length) return;
  $("wa-use-sug").onclick = () => { $("wa-shown").value = d.suggested; dirty = true; };
  $("wa-use-orig").onclick = () => { $("wa-shown").value = ""; dirty = true; };
}

async function waParse() {
  const text = $("wa-paste").value;
  if (!text.trim()) { showErr("請先貼上評論。"); return; }
  let d;
  try { d = await api("/api/web/share/draft", { method: "POST", body: JSON.stringify({ text }) }); }
  catch (e) { showErr(e.message); return; }
  $("wa-err").classList.add("hidden");
  if (d.author) $("wa-author").value = d.author;
  $("wa-qperson").value = "";
  $("wa-qperson").placeholder = d.person || "";
  if (d.posted) $("wa-posted").value = DF.fromISO(d.posted);
  if (d.rating) $("wa-rating").value = String(d.rating);
  $("wa-original").value = d.original || "";
  $("wa-shown").value = d.flags.length ? d.suggested : "";
  const notes = [...new Set(d.warnings)];
  notes.push(d.rating ? `星等：${d.rating} 顆（從複製的內容找到）` : "星等複製不到，請對照 Google 地圖選星等。");
  notes.push(d.owner_reply ? "Google 上已經有業主回應。" : "這則還沒有業主回應，記得到 Google 回覆。");
  $("wa-parse-msg").innerHTML = `<ul class="hint">${notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>`;
  renderFlags(d);
  dirty = true;
}

async function waCheck() {
  const original = $("wa-original").value.trim();
  if (!original) { showErr("請先填評論原文。"); return; }
  try { renderFlags(await api("/api/web/share/draft", { method: "POST", body: JSON.stringify({ original }) })); }
  catch (e) { showErr(e.message); }
}

async function waShareLinks() {
  const links = { ...(META.share_links || {}) };
  for (const [k, p] of Object.entries(META.share_platforms)) {
    if (!p.all) continue;
    const v = prompt(`${p.label}「看全部評論」的連結（空白＝不顯示）。\n` +
      "Google 地圖：打開門市 → 分享 → 複製連結。", links[k] || "");
    if (v === null) return;
    links[k] = v.trim();
  }
  const m = prompt("點開評論時，視窗裡顯示的 Google 地圖（門市位置和 Google 上的評分）。選填，空白＝用門市名稱搜尋。\n" +
    "要指定的話：Google 地圖打開門市 → 分享 → 嵌入地圖 → 複製 HTML，整段貼上。", links.map || "");
  if (m === null) return;
  links.map = m.trim();
  try {
    const r = await api("/api/web/share-links", { method: "PUT", body: JSON.stringify(links) });
    META.share_links = r.share_links;
    renderShareLinks();
    setStatus("已更新，約一分鐘內出現在官網。", "success");
  } catch (e) { setStatus(e.message, "danger"); }
}

// ── 分享到 Facebook 粉專（api/fb_page.py；價格字眼一律擋、療效用語先提醒）──────
async function waShareFb() {
  if (!cur.id) return;
  if (dirty && !confirm("內容還沒儲存。分享出去的會是官網上目前的版本，要繼續嗎？")) return;
  let r;
  try { r = await api(`/api/web/posts/${cur.id}/facebook`); } catch (e) { showErr(e.message); return; }
  const hist = r.history.map(h => `<li>${esc(DF.fromISODateTime(h.created_at))} ${esc(h.created_by || "")}：${
    h.status === "sent"
      ? `已發佈 <a href="https://www.facebook.com/${encodeURIComponent(h.fb_post_id)}" target="_blank" rel="noopener">看貼文 ↗</a>`
      : h.status === "dev" ? "開發模式（沒有真的貼出去）" : `失敗：${esc(h.error || "")}`}</li>`).join("");
  openModal("分享到 Facebook 粉專", r.reason ? `<p>${esc(r.reason)}</p>` : `<div class="fb-share">
    ${r.dev_mode ? '<p class="banner warning">開發模式：還沒接上粉專，按「發佈」只會記錄，不會真的貼出去。</p>' : ""}
    <div class="modal-field"><label>貼文內容</label>
      <textarea id="fb-msg" rows="6" style="width:100%;">${esc(r.text)}</textarea></div>
    <p class="hint">Facebook 會自動帶出官網這篇的標題、摘要和封面圖：<br>
      <a href="${esc(r.link)}" target="_blank" rel="noopener">${esc(r.link)}</a></p>
    <p class="hint">不可以提到價格；「最好」「保證」「改善聽力」這類用語會先提醒你再確認一次。</p>
    <div class="wa-actions">
      <button type="button" class="btn" id="fb-go" onclick="waFbPost(false)">發佈到粉專</button>
      <button type="button" class="btn ghost" onclick="closeModal()">取消</button>
    </div>
    ${hist ? `<h4 style="margin-top:1rem;">分享紀錄</h4><ul class="hint">${hist}</ul>` : ""}</div>`);
}

async function waFbPost(force) {
  const message = $("fb-msg").value.trim();
  $("fb-go").disabled = true;
  try {
    const r = await api(`/api/web/posts/${cur.id}/facebook`,
      { method: "POST", body: JSON.stringify({ message, force }) });
    if (!r.ok && r.warnings) {
      if (confirm("請再確認一次用語：\n・" + r.warnings.join("\n・") + "\n\n確定照原文發佈嗎？")) return waFbPost(true);
      $("fb-go").disabled = false;
      return;
    }
    closeModal();
    setStatus(r.status === "sent" ? "已發佈到粉專。" : "開發模式：已記錄，但沒有真的貼到粉專。",
      r.status === "sent" ? "success" : "warning");
  } catch (e) {
    setStatus(e.message, "danger");
    if ($("fb-go")) $("fb-go").disabled = false;
  }
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

// ── 官網網址（存一個地方：官網和 y1crm 都讀這個）──────────────────────
async function waSiteUrl() {
  const v = prompt("官網對外網址（例如 https://y1ear.com，不要加路徑）。\n" +
    "改了之後，官網的分享連結、QR 貼紙、sitemap 和保養提醒訊息約一分鐘內都會換成新網址。", META.site_url);
  if (v === null || v.trim() === META.site_url) return;
  try {
    const r = await api("/api/web/site", { method: "PUT", body: JSON.stringify({ base_url: v.trim() }) });
    META.site_url = r.site_url;
    $("wa-site-url").textContent = r.site_url;
    setStatus("官網網址已更新：已經印好的 QR 貼紙還是舊網址，記得重印。", "success");
    await loadList();
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
$("wa-format").querySelectorAll("button").forEach(b => b.addEventListener("click", () => {
  setSeg("wa-format", b.dataset.v); showKindFields(cur.kind || kind()); dirty = true;
}));
$("wa-platform").addEventListener("change", () => { showPlatformFields(); renderCover(); dirty = true; });
$("wa-author").addEventListener("input", () => { $("wa-qperson").placeholder = maskName($("wa-author").value); });
document.querySelectorAll("#wa-editor input, #wa-editor textarea, #wa-editor select").forEach(el =>
  el.addEventListener("input", () => { dirty = true; }));
window.addEventListener("beforeunload", e => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
attachDateMask($("wa-date"));
$("wa-date").placeholder = DF.placeholder;
$("wa-date-hint").textContent = DF.hint;
attachDateMask($("wa-posted"));
$("wa-posted").placeholder = DF.placeholder;
$("wa-posted-hint").textContent = DF.hint;

initHeader().then(async me => {
  if (!(me.menus || []).includes("web")) { location.href = "/calendar"; return; }
  META = await api("/api/web/meta");
  $("wa-site-url").textContent = META.site_url;
  renderShareLinks();
  const k = new URLSearchParams(location.search).get("k");
  setSeg("wa-kind", META.kinds[k] ? k : "about");
  await loadList();
}).catch(() => {});
