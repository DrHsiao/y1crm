/* web_admin.js — 官網管理頁（選單權限 web）：編輯官網 y1web 的關於我們／聽力專欄／好評分享／使用小教室／補助小幫手。
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
  about: "「關於我們」頁由上而下顯示「發佈」的段落（依排序）。每段可以放一張圖。",
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
// 圖片來源（John 2026-11-08：要標原圖還是 AI 生成，以後才能批次替換）：extra.cover_src＝ai／upload，舊資料只有 cover_ai
// 「助聽器知識」用圖庫的真實照片（cover_src＝stock，出處在 cover_credit；api/stock_art.py）
const isAiCover = r => r.cover && (r.extra.cover_src === "ai" || r.extra.cover_ai === true);
const isStockCover = r => r.cover && r.extra.cover_src === "stock";
// 翻譯文章帶回來的原文圖片（cover_src＝source，出處在 cover_credit.source；api/article_import.py）
const isSourceCover = r => r.cover && r.extra.cover_src === "source";
// John 11-08：好評分享以外（專欄、小教室）一律用圖庫的真實照片，不畫 AI 圖
const isStockKind = r => r.kind === "article" || r.kind === "care";
const coverTag = r => !r.cover ? "" : isAiCover(r) ? '<span title="AI 生成">🎨 AI</span>'
  : isStockCover(r) ? '<span title="圖庫照片">📷 圖庫</span>'
  : isSourceCover(r) ? '<span title="翻譯文章的原文圖片">🔗 原文</span>' : '<span title="原圖（店員上傳）">🖼 原圖</span>';
const COVER_KEYS = ["cover_ai", "cover_src", "cover_model", "cover_at", "cover_prompt", "cover_people", "cover_credit"];
// 文章短影音（api/article_video.py 做的，extra.video 是物件；使用小教室的 extra.video 是表單上的影片連結字串）
const hasVideo = r => !!(r.extra && r.extra.video && typeof r.extra.video === "object" && r.extra.video.file);
const videoTag = r => !hasVideo(r) ? "" : r.extra.video.youtube?.ok ? ' <span title="官網嵌 YouTube">▶ YouTube</span>'
  : r.extra.video.youtube ? ' <span title="YouTube 連結有問題，官網暫時播本站的影片">▶ ⚠</span>'
  : ' <span title="有影片，還沒貼 YouTube 網址">▶ 影片</span>';
const picksTag = (codes, labels, none) => (codes || []).map(c => esc(labels[c] || c)).join("、") || `<span class="lh">${none}</span>`;

// ── 列表 ─────────────────────────────────────────────────────
async function loadList() {
  const k = kind();
  const r = await api(`/api/web/posts?kind=${k}`);
  rows = r.rows;
  const ai = rows.filter(isAiCover).length, stock = rows.filter(isStockCover).length, src = rows.filter(isSourceCover).length;
  const orig = rows.filter(r => r.cover && !isAiCover(r) && !isStockCover(r) && !isSourceCover(r)).length;
  $("wa-count").textContent = `（${rows.length} 筆${ai || orig || stock || src
    ? `；圖片：AI 生成 ${ai}、${stock ? `圖庫照片 ${stock}、` : ""}${src ? `原文圖片 ${src}、` : ""}原圖 ${orig}` : ""}）`;
  $("wa-hint").textContent = KIND_HINT[k];
  $("wa-site").href = META.site_url + SITE_PATH[k];
  $("wa-qr").href = META.site_url + "/care/qr";
  $("wa-qr").classList.toggle("hidden", k !== "care");
  $("wa-links").classList.toggle("hidden", k !== "share");
  const artKind = ART_KINDS.includes(k);
  $("wa-share-tools").classList.toggle("hidden", k !== "share" && !artKind);
  document.querySelectorAll(".wa-share-only").forEach(el => el.classList.toggle("hidden", k !== "share"));
  document.querySelectorAll(".wa-article-only").forEach(el => el.classList.toggle("hidden", k !== "article"));
  const noPic = rows.filter(r => (k === "share" ? r.extra.format === "quote" && !r.extra.review_photos : artKind) && !r.cover).length;
  $("wa-art-btn").classList.toggle("hidden", !((k === "share" && META.art) || artKind));
  $("wa-art-btn").textContent = k === "share" ? `🎨 沒有圖片的畫示意插圖（${noPic} 則）` : `📷 沒有封面的挑圖庫照片（${noPic} 篇）`;
  $("wa-art-btn").disabled = !noPic;
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
              <td>${statusTag(r)}</td><td>${coverTag(r)}${videoTag(r)}</td><td>${modified(r)}</td>`,
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
  return `<span class="lh">${esc(p ? p.label : "")}${r.extra.rating ? " " + "★".repeat(r.extra.rating) : ""} ${coverTag(r)}</span><br>`
    + esc(text.length > 40 ? text.slice(0, 40) + "…" : text);
}

function renderShareLinks() {
  const links = META.share_links || {};
  const parts = Object.entries(META.share_platforms).filter(([, p]) => p.all)
    .map(([k, p]) => `${esc(p.label)}：${links[k] ? `<a href="${esc(links[k])}" target="_blank" rel="noopener">${esc(links[k])}</a>` : "未設定"}`);
  parts.push(`彈窗地圖：${links.map ? "已貼嵌入碼" : "用門市名稱搜尋"}`);
  $("wa-links-v").innerHTML = parts.join("　");
}

// ── 聽力專欄：從網址翻譯文章（api/article_import.py，背景執行，這裡輪詢進度）─────────────
//   本機 AI 翻成台灣繁體中文，圖片帶回官網、圖下方標「圖片來源」，文章上方寫出處。做好是草稿。
//   網站擋程式讀取時：店員在瀏覽器複製原文貼上（剪貼簿的 HTML 只送到伺服器解析，這頁不顯示它）。
let trHtml = "";
let trTimer = null;

function waTranslate() {
  trHtml = "";
  openModal("從網址翻譯文章", `
    <p class="hint">貼上科技網站或助聽器廠商官網的文章網址：本機 AI 翻成台灣繁體中文，圖片帶回官網並在圖下方標「圖片來源」，
      文章上方寫出處、連到原文。價格、購買、找門市預約的句子會省略。中文的原文只轉成台灣用語（標「轉載」）。<br>
      做好是<b>草稿</b>：請看過譯文、改好再發佈。一篇約 1～3 分鐘。</p>
    <div class="modal-field"><label>原文網址 <span class="req">*</span></label>
      <input id="tr-url" maxlength="300" placeholder="https://…" autocapitalize="none" spellcheck="false"></div>
    <div class="modal-row">
      <div class="modal-field"><label>出處名稱<span class="lh">空白＝用網站名稱</span></label>
        <input id="tr-site" maxlength="60" placeholder="例如 ReSound"></div>
      <div class="modal-field"><label>專欄分類</label>
        <select id="tr-cat">${META.categories.map(c => `<option>${esc(c)}</option>`).join("")}</select></div>
    </div>
    <label class="wa-check"><input type="checkbox" id="tr-img" checked> 圖片一起帶回來</label>
    <details id="tr-paste-box" style="margin-top:.6rem;"><summary>網站擋程式讀取時：改貼原文內容</summary>
      <p class="hint">在電腦瀏覽器打開原文，從標題選到文章最後一個字，複製後貼在下面（圖片會跟著帶過來）。原文網址還是要填。</p>
      <textarea id="tr-paste" rows="6" placeholder="貼上原文…"></textarea>
    </details>
    <div class="wa-actions"><button type="button" class="btn" id="tr-go" onclick="waTrStart()">開始翻譯</button></div>
    <div id="tr-out"></div>`);
  $("tr-paste").addEventListener("paste", e => {
    const h = e.clipboardData && e.clipboardData.getData("text/html");
    trHtml = h || "";                       // 有 HTML 就送 HTML（帶得到圖片）；方框裡只顯示文字
  });
  $("tr-paste").addEventListener("input", () => { if (!$("tr-paste").value.trim()) trHtml = ""; });
  waTrPoll();                               // 已經有一篇在翻的話顯示進度
}

function trPasteHtml() {
  const t = $("tr-paste").value.trim();
  if (!t) return "";
  if (trHtml) return trHtml;
  return t.split(/\n\s*\n|\n/).map(x => x.trim()).filter(Boolean).map(x => `<p>${esc(x)}</p>`).join("");
}

async function waTrStart() {
  const url = $("tr-url").value.trim();
  if (!url) { $("tr-out").innerHTML = '<div class="banner danger">請貼原文網址。</div>'; return; }
  $("tr-go").disabled = true;
  try {
    await api("/api/web/translate", { method: "POST", body: JSON.stringify({
      url, site: $("tr-site").value.trim(), category: $("tr-cat").value, images: $("tr-img").checked,
      html: trPasteHtml() }) });
    waTrPoll();
  } catch (e) {
    $("tr-go").disabled = false;
    $("tr-out").innerHTML = `<div class="banner danger">${esc(e.message)}</div>`;
  }
}

async function waTrPoll() {
  clearTimeout(trTimer);
  if (!$("tr-out")) return;                 // 對話框關掉了
  let s;
  try { s = await api("/api/web/translate"); } catch (e) { return; }
  if (!$("tr-out")) return;
  if (s.running) {
    $("tr-go").disabled = true;
    const bar = s.total ? `（${s.done}/${s.total}）` : "";
    $("tr-out").innerHTML = `<div class="banner">⏳ ${esc(s.step || "處理中…")}${bar}<br><span class="lh">${esc(s.url || "")}</span></div>`;
    trTimer = setTimeout(waTrPoll, 2000);
    return;
  }
  $("tr-go").disabled = false;
  if (s.error) {
    $("tr-out").innerHTML = `<div class="banner danger">${esc(s.error)}</div>`;
    if (s.blocked) $("tr-paste-box").open = true;
  } else if (s.result) {
    const r = s.result;
    const warn = (r.warnings || []).length
      ? `<div class="banner warning" style="margin-top:.5rem;">請看一下這幾句（官網不談價格、不寫療效或「最好」這類用語；原廠品牌、型號的介紹建議刪掉）：<br>・${r.warnings.map(esc).join("<br>・")}</div>` : "";
    $("tr-out").innerHTML = `<div class="banner success">已建立草稿「${esc(r.title)}」（${r.mode === "repost" ? "轉載" : "翻譯整理"}；`
      + `圖片 ${r.images} 張${r.skipped ? `，${r.skipped} 張沒帶回來` : ""}）。</div>${warn}
      <div class="wa-actions"><button type="button" class="btn" onclick="waTrOpen(${r.post_id})">打開草稿</button></div>`;
    if (kind() === "article") loadList();
  }
}

async function waTrOpen(id) {
  closeModal();
  setSeg("wa-kind", "article");
  await loadList();
  await waEdit(id);
}

// ── 一次貼上多則 Google 評論 → 草稿；沒圖片的畫示意插圖 ─────────────
//   拆解規則在 api/web_admin.split_reviews／batch_reviews；畫圖在 api/review_art.py（背景執行，這裡輪詢進度）。
let batchItems = [];

function waBatch() {
  openModal("一次貼上多則 Google 評論", `
    <p class="hint">在電腦版 Google 地圖打開門市的評論：<b>先按「排序 → 評分最高」</b>，往下捲到要的評論都出現，
      有「更多」的先按開，再把整片評論選起來複製，貼到下面。直接複製文字時星星複製不到，一律當 5 顆星，請只勾五星好評。<br>
      也可以貼評論區的 HTML（開發人員工具「複製 outerHTML」），或選存好的檔案：星等、照片都認得出來。</p>
    <textarea id="wb-text" rows="9" class="wa-md" placeholder="貼上整片評論…"></textarea>
    <div class="wa-actions">
      <label class="btn sm outline">📄 選檔案<input type="file" id="wb-file" accept=".txt,.html,.htm,text/plain,text/html" hidden onchange="waBatchFile(this)"></label>
      <button type="button" class="btn" onclick="waBatchParse()">拆解</button></div>
    <div id="wb-out"></div>`);
}

// 存成檔案的評論區：UTF-8，或 Windows 記事本存的「ANSI」（Big5）。Big5 存不下的表情符號會變成「?」
async function waBatchFile(input) {
  const f = input.files[0];
  if (!f) return;
  const buf = await f.arrayBuffer();
  let text;
  try { text = new TextDecoder("utf-8", { fatal: true }).decode(buf); }
  catch (e) { text = new TextDecoder("big5").decode(buf); }
  $("wb-text").value = text.replace(/^\uFEFF/, "");
  waBatchParse();
}

async function waBatchParse() {
  let r;
  try {
    r = await api("/api/web/share/batch", { method: "POST", body: JSON.stringify({ text: $("wb-text").value }) });
  } catch (e) { $("wb-out").innerHTML = `<div class="banner danger">${esc(e.message)}</div>`; return; }
  batchItems = r.items;
  if (!batchItems.length) {
    $("wb-out").innerHTML = '<div class="banner warning">沒拆出評論。請確認貼的內容有評論帳號、「幾週前」這類時間和評論文字。</div>';
    return;
  }
  const rowsHtml = batchItems.map((x, i) => {
    const notes = [x.duplicate ? `已經轉貼過（#${x.duplicate}）` : "",
                   x.flags.length ? `有 ${x.flags.length} 句提到價格或療效，已建議節錄` : "",
                   ...(x.warnings || [])].filter(Boolean);
    const pics = x.photos ? `<div class="lh">📷 附了 ${x.photos} 張照片：不畫示意插圖，問過後可上傳照片</div>` : "";
    const text = x.suggested.replace(/\s+/g, " ");
    return `<tr><td><input type="checkbox" id="wb-${i}"${x.duplicate ? "" : " checked"}></td>
      <td>${esc(x.person)}<div class="lh">${esc(DF.fromISO(x.posted))}</div></td>
      <td>${esc(text.length > 80 ? text.slice(0, 80) + "…" : text)}${pics}${notes.length ? `<div class="lh" style="color:var(--danger)">${esc(notes.join("；"))}</div>` : ""}</td></tr>`;
  }).join("");
  $("wb-out").innerHTML = `
    <p class="hint">拆出 ${batchItems.length} 則（Google 地圖的連結用「看全部評論」那個：${r.url ? esc(r.url) : "還沒設定"}）。
      ${batchItems.some(x => (x.warnings || []).some(w => w.includes("「?」")))
        ? "有幾則出現「?」：檔案是用記事本的 ANSI 存的，表情符號會遺失。請改存成 UTF-8 再選一次，或直接貼上。" : ""}</p>
    <div class="table-wrap" style="max-height:45vh; overflow:auto;"><table>
      <thead><tr><th>建</th><th>官網顯示</th><th>內容（提到價格的句子已刪節）</th></tr></thead><tbody>${rowsHtml}</tbody></table></div>
    ${r.art ? `<label class="wa-check" style="margin:.6rem 0;"><input type="checkbox" id="wb-art" checked>
      建好後，沒有圖片的自動畫示意插圖（依評論文意畫虛構人物；官網畫面上不標示，後台標成 AI 生成）</label>` : ""}
    <p class="hint">會建成<b>草稿</b>：發佈前請先問過評論者，在每則勾「已取得當事人同意」再發佈。</p>
    <div class="wa-actions"><button type="button" class="btn" onclick="waBatchCreate()">建立勾選的草稿</button></div>`;
}

async function waBatchCreate() {
  const items = batchItems.filter((x, i) => $(`wb-${i}`).checked).map(x => ({
    author: x.author, posted: x.posted, rating: x.rating, original: x.original, person: x.person, shown: x.suggested,
    review_id: x.review_id, posted_prec: x.posted_prec, photos: x.photos }));
  if (!items.length) { setStatus("請勾選要建立的評論。", "warning"); return; }
  try {
    const r = await api("/api/web/share/batch/create", { method: "POST",
      body: JSON.stringify({ items, illustrate: !!$("wb-art")?.checked }) });
    closeModal();
    const skip = r.skipped.length ? `，跳過 ${r.skipped.length} 則（${r.skipped[0].person}：${r.skipped[0].reason}）` : "";
    setStatus(`已建立 ${r.created.length} 則草稿${skip}。${r.art_started ? "正在畫示意插圖，" : ""}發佈前請先問過評論者、勾「已取得當事人同意」。`, "success");
    await loadList();
    if (r.art_started) waArtPoll();
  } catch (e) { setStatus(e.message, "danger"); }
}

async function waImportFb() {
  if (!confirm("把粉專上的推薦（正面、有文字的）匯入成草稿？轉貼過的會自動跳過。\n" +
               "Facebook 不給推薦者的名字，會先寫「臉書用戶」，問同意時再改成對方的稱呼。")) return;
  try {
    const r = await api("/api/web/share/import-fb", { method: "POST", body: JSON.stringify({ illustrate: !!META.art }) });
    const skip = r.skipped.length ? `，${r.skipped.length} 則已經轉貼過或不合格` : "";
    setStatus(`粉專有 ${r.found} 則推薦：建立 ${r.created.length} 則草稿${skip}。${r.art_started ? "正在畫示意插圖。" : ""}`, "success");
    await loadList();
    if (r.art_started) waArtPoll();
  } catch (e) { setStatus(e.message, "danger"); }
}

// 聽力專欄、使用小教室的封面也可以請本機 AI 依文章內容畫（api/post_art.py）；好評轉貼是示意插圖（api/review_art.py）
const ART_KINDS = ["article", "care"];

async function waArt(ids, force) {
  const k = ids && cur ? cur.kind || kind() : kind();          // 編輯中按的：那一筆的類別；列表上的：目前分頁
  const share = k === "share";
  if (!ids && !confirm(share ? "幫所有沒有圖片的評論轉貼畫示意插圖？（依評論文意畫虛構人物，一張約 20 秒）"
                              : "幫所有沒有封面的文章從圖庫挑真實照片？（依內容比對主題；自己放的封面不會動）")) return;
  try {
    if (share) {                                    // 示意插圖要用顯卡畫：背景執行，輪詢進度
      await api("/api/web/share/illustrate", { method: "POST", body: JSON.stringify({ ids, force }) });
      return waArtPoll();
    }
    const r = await api("/api/web/illustrate", { method: "POST", body: JSON.stringify({ ids, force, kind: k }) });
    await waArtDone(r.errors || [], `換好了（${r.done} 張圖庫照片）。`);   // 挑圖庫照片馬上好
  } catch (e) { setStatus(e.message, "danger"); }
}

let artTimer = null;
async function waArtPoll() {
  clearTimeout(artTimer);
  let s;
  try { s = await api("/api/web/share/illustrate"); } catch (e) { return; }
  $("wa-art-state").textContent = s.running ? `🎨 畫圖中…（${s.done}/${s.total || "?"}）`
    : s.finished_at ? `🎨 ${s.finished_at} 畫好 ${s.done} 張${s.errors.length ? "；" + s.errors.join("、") : ""}` : "";
  if (s.running) { artTimer = setTimeout(waArtPoll, 4000); return; }
  if (!s.finished_at) return;
  await waArtDone(s.errors, "插圖畫好了。");
}

async function waArtDone(errors, ok) {
  if (cur && cur.id) {                            // 編輯中：只更新圖片，不動正在改的欄位
    const fresh = (await api(`/api/web/posts?kind=${cur.kind || kind()}`)).rows.find(r => r.id === cur.id);
    if (fresh) {
      cur.cover = fresh.cover;
      for (const k of COVER_KEYS) cur.extra[k] = fresh.extra[k];
      renderCover();
    }
    setStatus(errors.length ? errors.join("、") : ok, errors.length ? "danger" : "success");
  } else if (kind() === "share" || ART_KINDS.includes(kind())) {
    await loadList();
    if (errors.length) setStatus(errors.join("、"), "danger");
  }
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
  const x = cur.extra || {};
  const stockArt = isStockKind(cur);                                     // 專欄、小教室：從圖庫挑，不畫 AI 圖
  const canArt = (isQuote() || ART_KINDS.includes(cur.kind)) && (META.art || stockArt)
    && (!cur.cover || isAiCover(cur) || isStockCover(cur));                // 原圖不會被蓋掉
  box.innerHTML = (cur.cover
    ? `<img class="wa-cover-img" src="/api/web/media/${esc(cur.cover)}" alt="">` : `<div class="field-info">${none}</div>`)
    + (isQuote() && x.review_photos && !cur.cover ? `<div class="field-info">📷 這則評論在 Google 上附了 ${x.review_photos} 張照片：問過評論者後，可以把照片存下來按「上傳」。</div>` : "")
    + (cur.cover && isStockCover(cur) ? `<div class="field-info"><b>📷 圖庫照片</b>：${esc((x.cover_credit || {}).title || "")}`
        + `（${esc([(x.cover_credit || {}).author, (x.cover_credit || {}).source].filter(Boolean).join("／"))}，`
        + `${esc((x.cover_credit || {}).license || "")}${(x.cover_credit || {}).credit_required ? "，官網會標出處" : ""}）。`
        + `專欄和小教室用真實照片，不畫 AI 圖。</div>` : "")
    + (cur.cover && isSourceCover(cur) ? `<div class="field-info"><b>🔗 原文圖片</b>：翻譯文章時從原文帶回來的，`
        + `官網封面下方會標「圖片來源：${esc((x.cover_credit || {}).source || "")}」。</div>` : "")
    + (cur.cover && !isStockCover(cur) && !isSourceCover(cur) ? `<div class="field-info">${isAiCover(cur)
        ? `<b>🎨 AI 生成</b>（${esc(x.cover_model || "")}${x.cover_at ? `，${esc(DF.fromISODateTime(x.cover_at))}` : ""}）：`
          + (isQuote() ? `依評論文意畫的虛構人物${x.cover_people ? `（${esc(x.cover_people)}）` : ""}。官網畫面上不標示，只寫在圖片替代文字。`
                       : `依文章內容畫的插圖${x.cover_people ? `：${esc(x.cover_people)}` : ""}。`)
        : `<b>🖼 原圖</b>（店員上傳${x.cover_at ? `，${esc(DF.fromISODateTime(x.cover_at))}` : ""}）：不會被自動換掉。`}</div>` : "")
    + `<div style="display:flex; flex-wrap:wrap; gap:.5rem; margin-top:.5rem;">
         <button type="button" class="btn sm outline" onclick="waPick('cover')">🖼 ${cur.cover ? "換成自己的照片" : "上傳自己的照片"}</button>
         ${cur.cover ? '<button type="button" class="btn sm outline" onclick="waRemoveCover()">移除</button>' : ""}
         ${canArt ? (stockArt
           ? `<button type="button" class="btn sm outline" onclick="waArt([cur.id], ${!!cur.cover})">📷 ${cur.cover ? "換一張圖庫照片" : "挑一張圖庫照片"}</button>`
           : `<button type="button" class="btn sm outline" onclick="waArt([cur.id], ${!!isAiCover(cur)})">🎨 ${isAiCover(cur) ? "重畫" : isQuote() ? "畫示意插圖" : "畫插圖"}</button>`) : ""}
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
  $("wa-keywords").value = (cur.extra.keywords || []).join("、");
  const src = cur.extra.source || {};
  $("wa-src-url").value = src.url || "";
  $("wa-src-site").value = src.site || "";
  $("wa-src-title").value = src.title || "";
  $("wa-src-mode").value = src.mode || "translate";
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
  $("wa-yt").classList.toggle("hidden", !(cur.id && hasVideo(cur)));
  $("wa-mkvid").classList.toggle("hidden", !(cur.id && (cur.kind || k) === "article"));
  $("wa-vfb").classList.toggle("hidden", !(cur.id && hasVideo(cur)));
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
    types: checked("wa-types"), video: $("wa-video").value.trim(), keywords: $("wa-keywords").value.trim(),
    when: checked("wa-when"), agency: $("wa-agency").value.trim(), link: $("wa-link").value.trim(),
    source_url: $("wa-src-url").value.trim(), source_site: $("wa-src-site").value.trim(),
    source_title: $("wa-src-title").value.trim(), source_mode: $("wa-src-mode").value,
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
  const hist = fbHistItems(r.history);
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

// 粉專分享紀錄（連結貼文、影片都記在 fb_posts）
const fbHistItems = rows => rows.map(h => `<li>${esc(DF.fromISODateTime(h.created_at))} ${esc(h.created_by || "")}：${
  h.status === "sent"
    ? `已發佈 <a href="https://www.facebook.com/${encodeURIComponent(h.fb_post_id)}" target="_blank" rel="noopener">看貼文 ↗</a>`
    : h.status === "dev" ? "開發模式（沒有真的貼出去）"
    : h.status === "deleted" ? `已從粉專刪除${h.error ? `（${esc(h.error)}）` : ""}` : `失敗：${esc(h.error || "")}`}</li>`).join("");

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

// ── 影片送 YouTube（api/video_youtube.py）────────────────────────
//   API 稽核通過前在 YouTube 工作室手動上傳：這裡下載影片、複製標題／說明／標籤，上傳後把網址貼回來。
//   網址查過（門市頻道、公開、允許嵌入）官網才嵌 YouTube，不然照舊播本站的影片；之後每 6 小時自動重查。
const ytField = (id, label, value, max, rows) => `<div class="modal-field">
    <label for="${id}">${label}<span class="yt-count" id="${id}-n">${value.length}／${max}</span></label>
    ${rows ? `<textarea id="${id}" rows="${rows}" maxlength="${max}" oninput="ytCount('${id}', ${max})">${esc(value)}</textarea>`
           : `<input id="${id}" value="${esc(value)}" maxlength="${max}" oninput="ytCount('${id}', ${max})">`}
    <button type="button" class="btn sm outline" onclick="ytCopy('${id}', this)">複製</button></div>`;
const ytCount = (id, max) => { $(`${id}-n`).textContent = `${$(id).value.length}／${max}`; };

async function ytCopy(id, btn) {
  const el = $(id);
  try { await navigator.clipboard.writeText(el.value); }       // 只有 https／localhost 才有 clipboard
  catch { el.select(); document.execCommand("copy"); }
  const t = btn.textContent;
  btn.textContent = "✓ 已複製";
  setTimeout(() => { btn.textContent = t; }, 1500);
}

function ytShowLink(l) {
  if (hasVideo(cur)) { if (l) cur.extra.video.youtube = l; else delete cur.extra.video.youtube; }
  const watch = l && `https://www.youtube.com/watch?v=${encodeURIComponent(l.id)}`;
  $("yt-link").innerHTML = !l ? '<p class="hint">還沒貼 YouTube 網址：官網目前播本站的影片。</p>'
    : l.ok ? `<p class="banner success">✓ 官網嵌入 YouTube：<a href="${watch}" target="_blank" rel="noopener">${esc(l.title || l.id)} ↗</a>`
      + `${l.note ? `<br>${esc(l.note)}` : ""}<br><small>最後檢查：${esc(DF.fromISODateTime(l.checked_at))}</small></p>`
    : `<p class="banner warning">⚠ <a href="${watch}" target="_blank" rel="noopener">${esc(l.title || l.id)}</a>：${esc(l.problem)}<br>`
      + `官網暫時改播本站的影片；在 YouTube 改好後按「重新檢查」。</p>`;
  $("yt-link-btns").innerHTML = l ? `<button type="button" class="btn sm" onclick="waYtSync(this)">⟳ 用上面的說明和標籤更新 YouTube</button>
      <button type="button" class="btn sm outline" onclick="waYtCheck(this)">重新檢查</button>
      <button type="button" class="btn sm ghost" onclick="waYtRemove()">拿掉連結</button>
      ${l.synced_at ? `<span class="hint">說明、標籤最後更新：${esc(DF.fromISODateTime(l.synced_at))} ${esc(l.synced_by || "")}</span>` : ""}` : "";
  $("yt-url").placeholder = l ? "換成另一支：貼新的網址" : "https://youtu.be/…";
}

async function waYouTube() {
  if (!cur.id) return;
  let r;
  try { r = await api(`/api/web/posts/${cur.id}/youtube`); } catch (e) { showErr(e.message); return; }
  const p = r.pack, base = `/api/web/posts/${cur.id}/video`;
  openModal("影片送 YouTube", `<div class="fb-share yt-pack">
    <video controls preload="metadata" src="${base}.mp4" poster="${base}.jpg"></video>
    <div class="wa-actions">
      <a class="btn sm outline" href="${base}.mp4?dl=1">⬇ 下載影片（${Math.round(r.video.sec || 0)} 秒）</a>
      <a class="btn sm outline" href="${base}.jpg?dl=1">⬇ 下載縮圖</a>
      <a class="btn sm outline" href="https://studio.youtube.com/" target="_blank" rel="noopener">YouTube 工作室 ↗</a>
    </div>
    ${r.video.tall ? `<div class="wa-actions"><span class="hint">直式 9:16 版（Reels、Shorts）：</span>
      <a class="btn sm outline" href="${base}.mp4?v=tall&dl=1">⬇ 下載直式影片</a>
      <a class="btn sm outline" href="${base}.jpg?v=tall&dl=1">⬇ 下載直式封面</a></div>
      <div class="yt-row"><input id="yt-short" placeholder="${r.video.tall.link ? "換一支 Shorts：貼新的網址" : "直式上傳成 Shorts 後，網址貼這裡（官網不嵌直的，只記錄）"}" autocapitalize="none" spellcheck="false">
        <button type="button" class="btn sm outline" onclick="waYtShort()">儲存 Shorts 網址</button></div>
      <div id="yt-short-st" class="hint">${ytShortHtml(r.video.tall.link)}</div>` : ""}
    ${r.authorized ? "" : '<p class="banner warning">YouTube 還沒授權：網址貼回來時沒辦法確認，請先找管理者。</p>'}
    <ol class="hint yt-steps">
      <li>YouTube 工作室 →「建立」→「上傳影片」，選剛下載的影片。</li>
      <li>標題、說明分別按「複製」貼上；「顯示更多」→「標籤」貼整串標籤；縮圖選「上傳檔案」用剛下載的縮圖。</li>
      <li>觀眾選「否，這不是為兒童打造的內容」；「顯示更多」→「授權」確認勾了「允許嵌入」。</li>
      <li>瀏覽權限選「公開」，發布後把影片網址貼到最下面。</li>
    </ol>
    ${ytField("yt-title", "標題", p.title, 100)}
    ${ytField("yt-desc", "說明", p.description, 5000, 9)}
    ${ytField("yt-tags", "標籤（整串貼到「標籤」欄，會自動分開）", p.tags_text, 500)}
    <h4>YouTube 網址</h4>
    <div id="yt-link"></div>
    <div class="yt-row"><input id="yt-url" autocapitalize="none" spellcheck="false" onkeydown="if (event.key === 'Enter') waYtSave()">
      <button type="button" class="btn sm" id="yt-save" onclick="waYtSave()">儲存網址</button></div>
    <p id="yt-err" class="banner danger hidden"></p>
    <div class="wa-actions" id="yt-link-btns"></div>
    ${r.published ? "" : '<p class="hint">這篇還是草稿，官網上看不到；網址可以先存，文章發佈後就會出現。</p>'}
    <details><summary>頻道預設說明欄範本</summary>
      <p class="hint">貼到 YouTube 工作室 → 設定 → 上傳預設設定 →「說明」。單篇的說明從上面複製，會整段取代它。</p>
      ${ytField("yt-tpl", "範本", r.template, 5000, 8)}</details>
  </div>`);
  ytShowLink(r.link);
}

// 直式（Shorts）：只記錄、每 6 小時一起重查；官網嵌的是橫式那支
function ytShortHtml(l) {
  if (!l) return "";
  const a = `<a href="https://youtube.com/shorts/${encodeURIComponent(l.id)}" target="_blank" rel="noopener">${esc(l.title || l.id)} ↗</a>`;
  return l.ok ? `✓ Shorts：${a}` : `⚠ Shorts：${a}：${esc(l.problem || "")}`;
}

async function waYtShort() {
  const url = $("yt-short").value.trim();
  if (!url) return;
  try {
    const r = await api(`/api/web/posts/${cur.id}/youtube`, { method: "PUT", body: JSON.stringify({ url, which: "tall" }) });
    $("yt-short").value = "";
    $("yt-short-st").innerHTML = ytShortHtml(r.link);
  } catch (e) { $("yt-err").textContent = e.message; $("yt-err").classList.remove("hidden"); }
}

async function waYtSave() {
  const url = $("yt-url").value.trim();
  if (!url) return;
  $("yt-save").disabled = true;
  $("yt-err").classList.add("hidden");
  try {
    const r = await api(`/api/web/posts/${cur.id}/youtube`, { method: "PUT", body: JSON.stringify({ url }) });
    $("yt-url").value = "";
    ytShowLink(r.link);
  } catch (e) {
    $("yt-err").textContent = e.message;
    $("yt-err").classList.remove("hidden");
  } finally { $("yt-save").disabled = false; }
}

// 標題、類別不動（店員在 YouTube 工作室自己定的）；說明和標籤用視窗裡的（可以先改再按）
async function waYtSync(btn) {
  if (!confirm("把上面的「說明」和「標籤」寫到 YouTube 上這支影片？（YouTube 上原本的說明和標籤會被取代，標題不變）")) return;
  btn.disabled = true;
  $("yt-err").classList.add("hidden");
  try {
    const r = await api(`/api/web/posts/${cur.id}/youtube/sync`, { method: "POST",
      body: JSON.stringify({ description: $("yt-desc").value, tags: $("yt-tags").value }) });
    const l = { ...cur.extra.video.youtube, synced_at: r.synced_at, synced_by: r.synced_by };
    ytShowLink(l);
  } catch (e) {
    $("yt-err").textContent = e.message; $("yt-err").classList.remove("hidden"); btn.disabled = false;
  }
}

async function waYtCheck(btn) {
  btn.disabled = true;
  try { ytShowLink((await api(`/api/web/posts/${cur.id}/youtube/check`, { method: "POST" })).link); }
  catch (e) { $("yt-err").textContent = e.message; $("yt-err").classList.remove("hidden"); btn.disabled = false; }
}

async function waYtRemove() {
  if (!confirm("拿掉 YouTube 連結？官網會改播本站的影片（YouTube 上的影片不會被刪）。")) return;
  try { await api(`/api/web/posts/${cur.id}/youtube`, { method: "DELETE" }); ytShowLink(null); }
  catch (e) { $("yt-err").textContent = e.message; $("yt-err").classList.remove("hidden"); }
}

// ── 做影片（api/motion_video.py、api/video_jobs.py）────────────────────
//   本機 AI 擬分鏡 → 店員改 → 背景產生（約 5 分鐘，可以先關視窗）→ 預覽橫式、直式 → 放上官網
let VP = null;           // { plan, photos, kinds, header, job, has_video }
let vpTimer = null;

async function waMakeVideo() {
  if (!cur.id) return;
  if (dirty && !confirm("文章還沒儲存。影片會用官網上目前的版本，要繼續嗎？")) return;
  try { VP = await api(`/api/web/posts/${cur.id}/video-plan`); } catch (e) { showErr(e.message); return; }
  VP.plan = VP.plan || { title: [], header: VP.header, lines: [] };
  openModal("做影片", `<div class="fb-share vid-pack">
    <p class="hint">① 請 AI 擬分鏡（或自己填）→ ② 改旁白、畫面、照片 → ③ 產生影片（約 5 分鐘，可以先關掉視窗）→ ④ 預覽滿意再「放上官網」。
      旁白會送到微軟的語音服務念出來（只送這些字）。</p>
    <div class="wa-actions"><button type="button" class="btn sm outline" id="vp-ai" onclick="vpAi()">🤖 請 AI 擬分鏡</button>
      <span class="hint" id="vp-ai-st"></span></div>
    <div class="modal-field"><label>片頭大字（影片第一格、粉專封面都是它：要讓人滑到就想停下來；每行 14 字內）</label>
      <div class="vp-two"><input id="vp-t1" maxlength="14" placeholder="例：助聽器嗶嗶叫？">
        <input id="vp-t2" maxlength="14" placeholder="例：先檢查這兩件事"></div></div>
    <div id="vp-rows"></div>
    <div class="wa-actions"><button type="button" class="btn sm outline" onclick="vpAdd()">＋ 加一段</button></div>
    <p id="vp-err" class="banner danger hidden"></p>
    <div class="wa-actions"><button type="button" class="btn sm outline" onclick="vpSave()">儲存分鏡</button>
      <button type="button" class="btn sm" id="vp-go" onclick="vpRender()">🎬 產生影片（約 5 分鐘）</button></div>
    <div id="vp-job"></div></div>`);
  vpFill();
  vpShowJob(VP.job);
}

function vpFill() {
  $("vp-t1").value = VP.plan.title?.[0] || "";
  $("vp-t2").value = VP.plan.title?.[1] || "";
  vpRows();
}

function vpRows() {
  const photoOpts = sel => VP.photos.map(p => `<option value="${esc(p.id)}"${p.id === sel ? " selected" : ""}>${esc(p.title)}</option>`).join("");
  const labelKinds = ["photo", "points", "feedback", "domes"];
  $("vp-rows").innerHTML = VP.plan.lines.map((ln, i) => {
    const kind = ln.kind || "photo";
    const kindOpts = Object.entries(VP.kinds).map(([k, l]) => `<option value="${k}"${k === kind ? " selected" : ""}>${esc(l)}</option>`).join("");
    const ph = VP.photos.find(p => p.id === ln.photo);
    return `<div class="vp-row">
      <div class="vp-head"><b>${i + 1}</b><select onchange="vpSet(${i},'kind',this.value)">${kindOpts}</select>
        <select onchange="vpSet(${i},'photo',this.value)" title="照片（片頭、圖解當模糊的底）">${photoOpts(ln.photo)}</select>
        ${ph ? `<img src="/api/web/media/${esc(ph.file)}" alt="">` : ""}
        <span class="vp-btns"><button type="button" onclick="vpMove(${i},-1)" title="上移">↑</button><button type="button" onclick="vpMove(${i},1)" title="下移">↓</button><button type="button" onclick="vpDel(${i})" title="刪掉這段">✕</button></span></div>
      <textarea rows="2" maxlength="70" placeholder="旁白（念出來的話，70 字內）" oninput="vpSet(${i},'say',this.value)">${esc(ln.say || "")}</textarea>
      ${labelKinds.includes(kind) ? `<input maxlength="14" placeholder="畫面上的重點標籤（14 字內，可空白）" value="${esc(ln.label || "")}" oninput="vpSet(${i},'label',this.value)">` : ""}
      ${kind === "points" ? `<input placeholder="重點清單：2～4 點，用「、」分開（每點 14 字內）" value="${esc((ln.items || []).join("、"))}" oninput="vpSet(${i},'items',this.value.split(/[、,，]/).map(x => x.trim()).filter(Boolean))">` : ""}
      ${kind === "end" ? `<div class="vp-two"><input maxlength="16" placeholder="片尾大字" value="${esc((ln.cta || [])[0] || "")}" oninput="vpCta(${i},0,this.value)">
        <input maxlength="16" placeholder="片尾小字" value="${esc((ln.cta || [])[1] || "")}" oninput="vpCta(${i},1,this.value)"></div>` : ""}
    </div>`;
  }).join("") || '<p class="hint">還沒有分鏡：按「請 AI 擬分鏡」，或「加一段」自己填。第一段要是片頭、最後一段要是片尾。</p>';
}

function vpSet(i, k, v) { VP.plan.lines[i][k] = v; if (k === "kind" || k === "photo") vpRows(); }
function vpCta(i, j, v) { const ln = VP.plan.lines[i]; ln.cta = ln.cta || []; ln.cta[j] = v; }
function vpMove(i, d) {
  const L = VP.plan.lines, j = i + d;
  if (j < 0 || j >= L.length) return;
  [L[i], L[j]] = [L[j], L[i]];
  vpRows();
}
function vpDel(i) { VP.plan.lines.splice(i, 1); vpRows(); }
function vpAdd() {
  VP.plan.lines.splice(Math.max(0, VP.plan.lines.length - 1), 0, { kind: "photo", say: "", label: "", photo: VP.photos[0]?.id });
  vpRows();
}
function vpCollect() {
  VP.plan.title = [$("vp-t1").value.trim(), $("vp-t2").value.trim()].filter(Boolean);
  VP.plan.header = VP.plan.header || VP.header;
  VP.plan.lines.forEach(ln => { if (ln.cta) ln.cta = ln.cta.filter(x => x && x.trim()); });
  return VP.plan;
}
function vpErr(msg) { const el = $("vp-err"); el.textContent = msg || ""; el.classList.toggle("hidden", !msg); }

async function vpAi() {
  if (VP.plan.lines.length && !confirm("用 AI 擬的分鏡取代目前的內容？")) return;
  $("vp-ai").disabled = true;
  $("vp-ai-st").textContent = "本機 AI 擬稿中，約 30 秒～2 分鐘……";
  try {
    const r = await api(`/api/web/posts/${cur.id}/video-plan/ai`, { method: "POST" });
    VP.plan = r.plan;
    vpFill();
    $("vp-ai-st").textContent = r.source === "ai" ? "AI 擬好了：請看過、修改後再產生（AI 會漏重點、用字也要確認）。"
      : `AI 擬的不能用（${r.problem || "格式不對"}），先用文章的條列整理，請修改。`;
  } catch (e) { $("vp-ai-st").textContent = e.message; }
  finally { if ($("vp-ai")) $("vp-ai").disabled = false; }
}

async function vpSave() {
  vpErr("");
  try {
    const r = await api(`/api/web/posts/${cur.id}/video-plan`, { method: "PUT", body: JSON.stringify({ plan: vpCollect() }) });
    VP.plan = r.plan;
    vpFill();
    $("vp-ai-st").textContent = "分鏡已儲存。";
  } catch (e) { vpErr(e.message); }
}

async function vpRender() {
  vpErr("");
  if (VP.job && VP.job.state === "done" && !confirm("上一支還沒放上官網，重新產生會另外做一支。要繼續嗎？")) return;
  $("vp-go").disabled = true;
  try {
    vpShowJob(await api(`/api/web/posts/${cur.id}/video-job`, { method: "POST", body: JSON.stringify({ plan: vpCollect() }) }));
  } catch (e) { vpErr(e.message); $("vp-go").disabled = false; }
}

function vpShowJob(j) {
  VP.job = j;
  clearTimeout(vpTimer);
  const box = $("vp-job"), go = $("vp-go");
  if (!box) return;
  if (!j) { box.innerHTML = ""; if (go) go.disabled = false; return; }
  const pct = Math.round((j.progress || 0) * 100);
  if (j.state === "queued" || j.state === "running") {
    if (go) go.disabled = true;
    box.innerHTML = `<div class="vp-prog"><div style="width:${pct}%"></div></div>
      <p class="hint">產生中：${esc(j.step || "")}（${pct}%）。可以先關掉視窗，等一下再按「🎬 做影片」回來看。</p>`;
    vpTimer = setTimeout(async () => {
      if (!$("vp-job")) return;                       // 視窗關了就不再問
      try { vpShowJob(await api(`/api/web/video-jobs/${j.id}`)); } catch { vpShowJob(null); }
    }, 3000);
    return;
  }
  if (go) go.disabled = false;
  if (j.state === "failed") {
    box.innerHTML = `<p class="banner danger">影片沒做成：${esc(j.error || "")}</p>
      <div class="wa-actions"><button type="button" class="btn sm ghost" onclick="vpDiscard(true)">清掉這筆</button></div>`;
    return;
  }
  const b = `/api/web/video-jobs/${encodeURIComponent(j.id)}`;
  box.innerHTML = `<h4>預覽（${Math.round(j.sec || 0)} 秒，${esc(j.updated || "")} 做好）</h4>
    <div class="vp-previews"><video controls preload="metadata" src="${b}/wide.mp4" poster="${b}/wide.jpg"></video>
      <video class="tall" controls preload="metadata" src="${b}/tall.mp4" poster="${b}/tall.jpg"></video></div>
    <div class="wa-actions"><button type="button" class="btn sm" onclick="vpInstall()">✅ 放上官網</button>
      <button type="button" class="btn sm ghost" onclick="vpDiscard()">捨棄這支</button></div>
    <p class="hint">左邊橫式給官網、YouTube；右邊直式給粉專 Reels。${VP.has_video
      ? "放上官網會換掉這篇原本的影片（YouTube 連結也會拿掉，要重傳）。" : ""}</p>`;
}

async function vpInstall() {
  if (!confirm("把這支影片放上官網？")) return;
  try {
    const r = await api(`/api/web/video-jobs/${encodeURIComponent(VP.job.id)}/install`, { method: "POST" });
    cur.extra.video = r.video;
    VP.has_video = true;
    VP.job = null;
    $("vp-job").innerHTML = '<p class="banner success">已放上官網（約一分鐘內出現）。接著可以按「📘 影片發粉專」。</p>';
    $("wa-vfb").classList.remove("hidden");
    $("wa-yt").classList.remove("hidden");
  } catch (e) { vpErr(e.message); }
}

async function vpDiscard(quiet) {
  if (!quiet && !confirm("捨棄這支影片？")) return;
  try { await api(`/api/web/video-jobs/${encodeURIComponent(VP.job.id)}`, { method: "DELETE" }); vpShowJob(null); }
  catch (e) { vpErr(e.message); }
}

// ── 影片發粉專（直式＝Reels，建議；第一格就是封面）──────────────────
async function waVideoFb() {
  if (!cur.id) return;
  let r;
  try { r = await api(`/api/web/posts/${cur.id}/video-fb`); } catch (e) { showErr(e.message); return; }
  const hist = fbHistItems(r.history);
  openModal("影片發粉專", `<div class="fb-share">
    ${r.dev_mode ? '<p class="banner warning">開發模式：還沒接上粉專，按「發佈」只會記錄，不會真的貼出去。</p>' : ""}
    ${r.published ? "" : '<p class="banner warning">這篇還是草稿：請先把文章改成「發佈」並儲存，貼文裡的連結才看得到。</p>'}
    <div class="modal-field"><label>版本</label>
      <label class="vp-radio"><input type="radio" name="vfb-which" value="tall"${r.tall ? " checked" : " disabled"}> 直式（Reels，建議）</label>
      <label class="vp-radio"><input type="radio" name="vfb-which" value="wide"${r.tall ? "" : " checked"}> 橫式（一般影片）</label></div>
    <div class="modal-field"><label>貼文內容（第一行最重要：要讓人滑到就想停下來）</label>
      <textarea id="vfb-msg" rows="11" style="width:100%;">${esc(r.message)}</textarea></div>
    <p class="hint">影片的第一格就是封面。不可以提到價格；「最好」「保證」「改善聽力」這類用語會先提醒你再確認一次。</p>
    <div class="wa-actions">
      <button type="button" class="btn" id="vfb-go" onclick="waVideoFbPost(false)">發佈到粉專</button>
      <button type="button" class="btn ghost" onclick="closeModal()">取消</button>
    </div>
    ${hist ? `<h4 style="margin-top:1rem;">粉專紀錄</h4><ul class="hint">${hist}</ul>` : ""}</div>`);
}

async function waVideoFbPost(force) {
  const which = document.querySelector('input[name="vfb-which"]:checked')?.value || "tall";
  if (!force && !confirm("確定要把影片發到粉專嗎？發出去大家就看得到。")) return;
  const btn = $("vfb-go");
  btn.disabled = true;
  btn.textContent = "上傳中（約 1 分鐘）……";
  try {
    const r = await api(`/api/web/posts/${cur.id}/video-fb`,
      { method: "POST", body: JSON.stringify({ which, message: $("vfb-msg").value.trim(), force }) });
    if (!r.ok && r.warnings) {
      if (confirm("請再確認一次用語：\n・" + r.warnings.join("\n・") + "\n\n確定照原文發佈嗎？")) return waVideoFbPost(true);
      btn.disabled = false;
      btn.textContent = "發佈到粉專";
      return;
    }
    closeModal();
    setStatus(r.status === "sent" ? `已發到粉專：${r.url}` : "開發模式：已記錄，但沒有真的貼到粉專。",
      r.status === "sent" ? "success" : "warning");
  } catch (e) {
    setStatus(e.message, "danger");
    if ($("vfb-go")) { $("vfb-go").disabled = false; $("vfb-go").textContent = "發佈到粉專"; }
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
  if (META.art) waArtPoll();                     // 有一批正在畫的話顯示進度
}).catch(() => {});
