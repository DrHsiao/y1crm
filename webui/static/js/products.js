/* products.js — 商品管理頁（表單/查詢結果為頁內兩個 view）
   依賴 common.js：$/esc/api/DF/setStatus/hideStatus/setSeg/getSeg/CODES/fillSelect/codeLabel/initHeader */
"use strict";

let productRows = [], productEditId = null;
let prodFiltered = [], prodPage = 1, prodSearched = false;
const PRODUCT_INPUT_IDS = ["product_code","barcode","brand","model_name","spec","unit","supplier",
  "list_price","cost_price","warranty_months","ha_channels","ha_ip_rating","compatible_models","notes"];
const PRODUCT_SELECT_IDS = ["category","ha_form","ha_tech_level","ha_battery","subsidy_class"];

// 頁內 view 切換（本頁只有表單/查詢結果兩個）
function showView(name) {
  for (const v of ["products","prod-results"]) $("view-"+v).classList.add("hidden");
  $("view-"+name).classList.remove("hidden");
}

// 從編輯表單返回查詢結果：保留原查詢結果集（不重新過濾，因查詢/建檔欄位共用、
// 編輯時已被商品值覆蓋），僅離開編輯狀態並重繪既有結果頁。
function backToProdResults() {
  clearProductForm();     // 離開編輯狀態、清空表單、隱藏返回/刪除鈕
  prodSearched = true;    // clearProductForm 會設 false，保留查詢態供 loadProducts 更新
  renderProductPage();    // 用未變動的 prodFiltered 重繪原結果
  showView("prod-results");
  window.scrollTo(0, 0);
}

// ── 三態選鈕綁定（本頁的 seg）────────────────────────────────
for (const segId of ["seg-p-active","seg-p-bt","seg-p-tcoil"]) {
  $(segId).querySelectorAll("button").forEach(btn => {
    btn.onclick = () => setSeg(segId, btn.dataset.v);
  });
}

async function loadProducts() {
  const r = await api("/api/products");
  productRows = r.rows;
  // 進商品頁預設不顯示列表；若已有查詢條件則就地更新結果頁的資料（不切換畫面）
  if (prodSearched) { applyProductFilter(); renderProductPage(); }
}

// 品牌／供應商建議清單（datalist）：來源＝現有商品相異值，可輸入不受限
async function loadBrandSuggestions() {
  try {
    const r = await api("/api/products/brands");
    const fill = (id, arr) => {
      $(id).innerHTML = (arr || []).map(v => `<option value="${esc(v)}">`).join("");
    };
    fill("brand-list", r.brands);
    fill("supplier-list", r.suppliers);
  } catch { /* 401 已導回登入；建議清單非必要，失敗不擋頁面 */ }
}

// 查詢欄位與建檔欄位共用（p-category/p-brand/p-model_name/p-supplier）
// 分類精確、其餘含比對；只計算結果，不負責畫面切換
function applyProductFilter() {
  const cat = $("p-category").value;
  const brand = $("p-brand").value.trim().toLowerCase();
  const model = $("p-model_name").value.trim().toLowerCase();
  const sup = $("p-supplier").value.trim().toLowerCase();
  const has = (v, q) => !q || String(v || "").toLowerCase().includes(q);
  prodFiltered = productRows.filter(p =>
    (!cat || p.category === cat) &&
    has(p.brand, brand) && has(p.model_name, model) && has(p.supplier, sup));
  return prodFiltered.length;
}

// 使用者按「查詢」：空白條件＝列出全部（比照客戶查詢欄位鈕）；有結果才切到結果頁
function searchProducts() {
  prodSearched = true;
  prodPage = 1;
  if (applyProductFilter() === 0) {
    setStatus("查無符合條件的商品。", "info");
    return;
  }
  hideStatus();
  renderProductPage();
  showView("prod-results");
  window.scrollTo(0, 0);
}

function prodGoPage(delta) {
  const size = +$("prod-page-size").value;
  const pages = Math.max(1, Math.ceil(prodFiltered.length / size));
  prodPage = Math.min(pages, Math.max(1, prodPage + delta));
  renderProductPage();
}

function renderProductPage() {
  const size = +$("prod-page-size").value;
  const total = prodFiltered.length;
  const pages = Math.max(1, Math.ceil(total / size));
  if (prodPage > pages) prodPage = pages;
  const start = (prodPage - 1) * size;
  const rows = prodFiltered.slice(start, start + size);
  $("prod-count").textContent = `（${total} 筆）`;
  $("prod-body").innerHTML = rows.map(p => `
    <tr onclick="editProduct(${p.id})"${p.is_active ? "" : ' style="opacity:.55"'}>
      <td>${esc(codeLabel("product_category", p.category))}</td>
      <td>${esc(p.brand)}</td>
      <td>${esc(p.model_name)}</td>
      <td>${esc(p.spec) || "—"}</td>
      <td class="price-col">${p.list_price != null ? Number(p.list_price).toLocaleString() : "—"}</td>
      <td>${p.warranty_months ? p.warranty_months + " 個月" : "—"}</td>
      <td>${p.subsidy_class ? esc(codeLabel("subsidy_class", p.subsidy_class)) : "—"}</td>
      <td>${p.is_active ? "在售" : "停售"}</td>
    </tr>`).join("") || `<tr><td colspan="8" class="hint">查無符合條件的商品。</td></tr>`;
  // 分頁列：只有一頁時隱藏（比照客戶查詢結果）
  $("prod-pager").classList.toggle("hidden", pages <= 1);
  $("prod-page-info").textContent = `第 ${prodPage} / ${pages} 頁`;
  $("prod-prev").disabled = prodPage <= 1;
  $("prod-next").disabled = prodPage >= pages;
}

function toggleHaFields() {
  const isHA = $("p-category").value === "hearing_aid";
  document.querySelectorAll(".ha-only").forEach(el => el.classList.toggle("hidden", !isHA));
}
$("p-category").addEventListener("change", toggleHaFields);

function productFormData() {
  const d = {};
  for (const f of PRODUCT_INPUT_IDS) d[f] = $("p-"+f).value;
  for (const f of PRODUCT_SELECT_IDS) d[f] = $("p-"+f).value;
  d.ha_bluetooth = getSeg("seg-p-bt");
  d.ha_tcoil = getSeg("seg-p-tcoil");
  d.is_active = getSeg("seg-p-active");
  d.ha_fit_levels = [...document.querySelectorAll("#p-fit input:checked")].map(i => i.value).join(",");
  return d;
}

function editProduct(id) {
  const p = productRows.find(x => x.id === id);
  if (!p) return;
  productEditId = id;
  for (const f of PRODUCT_INPUT_IDS) $("p-"+f).value = p[f] ?? "";
  for (const f of PRODUCT_SELECT_IDS) $("p-"+f).value = p[f] ?? "";
  setSeg("seg-p-bt", p.ha_bluetooth == null ? "" : (p.ha_bluetooth ? "y" : "n"));
  setSeg("seg-p-tcoil", p.ha_tcoil == null ? "" : (p.ha_tcoil ? "y" : "n"));
  setSeg("seg-p-active", p.is_active ? "1" : "0");
  const fit = new Set((p.ha_fit_levels || "").split(","));
  document.querySelectorAll("#p-fit input").forEach(i => i.checked = fit.has(i.value));
  $("prod-form-title").textContent = `編輯商品：${p.brand} ${p.model_name}`;
  $("prod-meta").textContent = p.updated_at
    ? `最後更新 ${DF.fromISODateTime(p.updated_at)}${p.updated_by ? "‧" + p.updated_by : ""}` : "";
  $("btn-prod-save").textContent = "儲存變更";
  $("btn-prod-del").classList.remove("hidden");
  $("btn-prod-back").classList.toggle("hidden", !prodSearched);   // 有查詢結果才顯示返回
  $("view-products").classList.add("mode-prod-edit");   // 編輯既有商品時隱藏查詢鈕
  loadProductAttachments(id);                            // 載入該商品的附件
  toggleHaFields();
  showView("products");   // 從結果頁點選 → 切回表單頁編輯
  window.scrollTo(0, 0);
}

function clearProductForm() {
  productEditId = null;
  for (const f of PRODUCT_INPUT_IDS) $("p-"+f).value = "";
  for (const f of PRODUCT_SELECT_IDS) $("p-"+f).value = "";
  setSeg("seg-p-bt", ""); setSeg("seg-p-tcoil", ""); setSeg("seg-p-active", "1");
  document.querySelectorAll("#p-fit input").forEach(i => i.checked = false);
  $("prod-form-title").textContent = "新增商品";
  $("prod-meta").textContent = "";
  $("btn-prod-save").textContent = "建立商品";
  $("btn-prod-del").classList.add("hidden");
  $("btn-prod-back").classList.add("hidden");
  $("view-products").classList.remove("mode-prod-edit");   // 回到新增/查詢模式，顯示查詢鈕
  clearProductAttachments();
  prodSearched = false;
  toggleHaFields();
}

async function saveProduct() {
  hideStatus();
  const d = productFormData();
  if (!d.category || !d.brand.trim() || !d.model_name.trim()) {
    setStatus("「分類」「品牌」「型號」為必填。", "danger");
    return;
  }
  try {
    if (productEditId === null) {
      await api("/api/products", { method: "POST", body: JSON.stringify(d) });
      setStatus("商品已建立。", "success");
      clearProductForm();
    } else {
      await api(`/api/products/${productEditId}`, { method: "PUT", body: JSON.stringify(d) });
      setStatus("已儲存變更。", "success");
    }
    await loadProducts();
    loadBrandSuggestions();   // 可能新增了品牌／供應商，刷新建議清單
  } catch (e) { setStatus(e.message, "danger"); }
}

async function deleteProduct() {
  if (productEditId === null) return;
  if (!confirm("確定刪除這筆商品？已有交易紀錄的商品無法刪除（請改「停售」）。")) return;
  try {
    await api(`/api/products/${productEditId}`, { method: "DELETE" });
    setStatus("商品已刪除。", "success");
    clearProductForm();
    await loadProducts();
  } catch (e) { setStatus(e.message, "danger"); }
}

// ── 商品附件（多檔，任意類型；圖片預覽、其他顯示檔案卡片）──────
const isImageMime = m => (m || "").startsWith("image/");
function fmtSize(n) {
  if (!n && n !== 0) return "";
  return n < 1024 ? n + " B"
       : n < 1048576 ? (n / 1024).toFixed(0) + " KB"
       : (n / 1048576).toFixed(1) + " MB";
}
function fileMeta(a) {
  const ext = ((a.filename || "").split(".").pop() || "").toLowerCase();
  const ico = /pdf/.test(a.mime) || ext === "pdf" ? "📕"
    : /(word|document)/.test(a.mime) || ["doc", "docx"].includes(ext) ? "📘"
    : /(sheet|excel)/.test(a.mime) || ["xls", "xlsx", "csv"].includes(ext) ? "📗"
    : /(zip|compress|rar|7z)/.test(a.mime) || ["zip", "rar", "7z"].includes(ext) ? "🗜️"
    : "📎";
  return { ext, ico };
}

async function loadProductAttachments(pid) {
  try {
    const r = await api(`/api/products/${pid}/attachments`);
    renderProductAttachments(r.rows);
  } catch { /* 401 已導回登入 */ }
}

function clearProductAttachments() {
  $("pa-grid").innerHTML = "";
  $("pa-count").textContent = "";
  setNote("pa-status", "");
}

function renderProductAttachments(rows) {
  $("pa-count").textContent = rows.length ? `（${rows.length}）` : "";
  $("pa-grid").innerHTML = rows.length ? rows.map(a => {
    const url = `/api/product-attachments/${a.id}`;
    const inner = isImageMime(a.mime)
      ? `<div class="pa-thumb" onclick="showProductImage(${a.id})"><img src="${url}" loading="lazy" alt=""></div>`
      : (() => { const m = fileMeta(a);
          return `<a class="pa-file" href="${url}" target="_blank" rel="noopener" title="點擊開啟／下載">
                    <span class="pa-ico">${m.ico}</span><span class="pa-ext">${esc(m.ext) || "檔案"}</span></a>`; })();
    return `<div class="pa-item">
      ${inner}
      <button type="button" class="pa-del" title="刪除附件" onclick="deleteProductAttachment(${a.id})">✕</button>
      <div class="pa-name">${esc(a.filename || "（未命名）")}<br><span class="pa-sz">${fmtSize(a.bytes_size)}</span></div>
    </div>`;
  }).join("") : `<div class="pa-empty">尚無附件，按「上傳附件」加入（說明書、合約、圖片等）。</div>`;
}

function showProductImage(aid) {
  openModal("附件預覽", `
    <img src="/api/product-attachments/${aid}" style="max-width:100%; border-radius:6px;">
    <div class="modal-actions">
      <a class="btn outline" href="/api/product-attachments/${aid}" target="_blank" rel="noopener">開新視窗</a>
      <button class="btn" onclick="closeModal()">關閉</button>
    </div>`);
}

async function uploadProductAttachment(file) {
  if (productEditId === null) return;
  setNote("pa-status", "上傳中…");
  try {
    const res = await fetch(`/api/products/${productEditId}/attachments?filename=${encodeURIComponent(file.name)}`, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": file.type || "application/octet-stream" }, body: file });
    if (res.status === 401) { location.href = "/login?next=/products"; return; }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    setNote("pa-status", "");
    await loadProductAttachments(productEditId);
  } catch (e) { setNote("pa-status", ""); setStatus("附件上傳失敗：" + e.message, "danger"); }
}

async function deleteProductAttachment(aid) {
  if (!confirm("確定刪除這個附件？")) return;
  try {
    await api(`/api/product-attachments/${aid}`, { method: "DELETE" });
    await loadProductAttachments(productEditId);
  } catch (e) { setStatus(e.message, "danger"); }
}

$("pa-file").addEventListener("change", e => {
  const file = e.target.files[0]; e.target.value = "";   // 清空才能連續上傳同一檔
  if (file) uploadProductAttachment(file);
});

// ── 開機：header → 代碼表 → 下拉選單 → 商品清單 ─────────────
initHeader().then(async () => {
  await ensureCodes();
  fillSelect("p-category", "product_category", "請選擇");
  fillSelect("p-ha_form", "ha_form");
  fillSelect("p-ha_tech_level", "ha_tech_level");
  fillSelect("p-ha_battery", "battery_type");
  fillSelect("p-subsidy_class", "subsidy_class");
  $("p-fit").innerHTML = (CODES.identity || []).map(c =>
    `<label><input type="checkbox" value="${c.key}">${esc(c.value)}</label>`).join("");
  toggleHaFields();
  await loadProducts();
  loadBrandSuggestions();   // 品牌／供應商 datalist 建議（非必要，不擋主流程）
}).catch(e => { if (e.message !== "未登入") setStatus(e.message, "danger"); });
