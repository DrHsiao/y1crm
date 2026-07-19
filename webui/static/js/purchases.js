/* purchases.js — 交易紀錄頁（建檔/編輯 與 查詢結果 為頁內兩個 view）
   依賴 common.js：$/esc/api/DF/dfToISO/attachDateMask/setSeg/getSeg/openModal/closeModal/
                  setStatus/hideStatus/codeLabel/ensureCodes/initHeader */
"use strict";

const TX_TYPE = { purchase: "購買", rental: "租借", repair: "維修", return: "退貨" };
const EAR = { left: "左耳", right: "右耳", both: "雙耳" };
const PAY = { unpaid: "未付", partial: "部分付款", paid: "已付清" };
const PUR_DATE_IDS = ["transaction_date", "warranty_start_date", "warranty_end_date"];
const PUR_TEXT_IDS = ["serial_number", "quantity", "unit_price", "notes"];

let purchaseRows = [], purchaseEditId = null;
let purFiltered = [], pfPage = 1;
let puCustomer = null;   // {id, label}
let puProduct = null;    // {id, label}
let allProducts = [];    // 商品選擇器用（一次載入）

function showView(name) {
  for (const v of ["purchases", "pur-results"]) $("view-" + v).classList.add("hidden");
  $("view-" + name).classList.remove("hidden");
}

// ── 三態/多態選鈕 ────────────────────────────────────────────
for (const segId of ["seg-pu-type", "seg-pu-ear", "seg-pu-pay"]) {
  $(segId).querySelectorAll("button").forEach(btn => {
    btn.onclick = () => setSeg(segId, btn.dataset.v);
  });
}

// ── 客戶選擇器（查 /api/customers/query）──────────────────────
function openCustomerPicker() {
  openModal("選擇客戶", `
    <div class="field"><input id="cp-q" placeholder="輸入姓名／電話／客戶編號" autocomplete="off"></div>
    <div id="cp-results" class="table-wrap" style="max-height:50vh; overflow:auto;"></div>`);
  const q = $("cp-q");
  q.focus();
  let timer = null;
  q.oninput = () => { clearTimeout(timer); timer = setTimeout(() => runCustomerSearch(q.value), 250); };
  q.onkeydown = e => { if (e.key === "Enter") { clearTimeout(timer); runCustomerSearch(q.value); } };
}

async function runCustomerSearch(term) {
  term = (term || "").trim();
  if (!term) { $("cp-results").innerHTML = `<p class="hint">請輸入查詢條件。</p>`; return; }
  // 姓名/手機/編號任一命中：分欄各查一次再合併（後端為 AND 查詢）
  const seen = new Map();
  for (const field of ["name", "phone_mobile", "customer_code"]) {
    try {
      const r = await api("/api/customers/query", { method: "POST", body: JSON.stringify({ [field]: term }) });
      for (const c of r.rows) if (!seen.has(c.id)) seen.set(c.id, c);
    } catch { /* 忽略單欄失敗 */ }
  }
  const rows = [...seen.values()];
  $("cp-results").innerHTML = rows.length ? `<table><tbody>${rows.map(c => `
    <tr onclick="pickCustomer(${c.id}, '${esc(c.name)}', '${esc(c.customer_code || "")}')">
      <td>${esc(c.customer_code) || "—"}</td><td>${esc(c.name)}</td>
      <td>${esc(c.phone_mobile) || ""}</td></tr>`).join("")}</tbody></table>`
    : `<p class="hint">查無符合的客戶。</p>`;
}

function pickCustomer(id, name, code) {
  puCustomer = { id, label: code ? `${name}（${code}）` : name };
  $("pu-customer").value = puCustomer.label;
  closeModal();
}

// ── 商品選擇器（用已載入的 allProducts 就地過濾）──────────────
function openProductPicker() {
  openModal("選擇商品", `
    <div class="field"><input id="pp-q" placeholder="輸入品牌／型號" autocomplete="off"></div>
    <div id="pp-results" class="table-wrap" style="max-height:50vh; overflow:auto;"></div>`);
  const q = $("pp-q");
  q.focus();
  q.oninput = () => renderProductPicker(q.value);
  renderProductPicker("");
}

function renderProductPicker(term) {
  term = (term || "").trim().toLowerCase();
  const rows = allProducts.filter(p => !term ||
    `${p.brand} ${p.model_name} ${p.spec || ""}`.toLowerCase().includes(term)).slice(0, 100);
  $("pp-results").innerHTML = rows.length ? `<table><tbody>${rows.map(p => {
    const label = `${p.brand} ${p.model_name}${p.spec ? " " + p.spec : ""}`;
    return `<tr onclick="pickProduct(${p.id}, '${esc(label)}')">
      <td>${esc(codeLabel("product_category", p.category))}</td><td>${esc(label)}</td>
      <td>${p.is_active ? "" : "停售"}</td></tr>`;
  }).join("")}</tbody></table>` : `<p class="hint">查無符合的商品。</p>`;
}

function pickProduct(id, label) {
  puProduct = { id, label };
  $("pu-product").value = label;
  closeModal();
}

function clearPurProduct() { puProduct = null; $("pu-product").value = ""; }

// ── 建檔資料 ─────────────────────────────────────────────────
function purchaseFormData() {
  const d = {};
  for (const f of PUR_TEXT_IDS) d[f] = $("pu-" + f).value;
  for (const f of PUR_DATE_IDS) d[f] = dfToISO($("pu-" + f).value, f);   // 民國→西元；空值回空字串
  d.transaction_type = getSeg("seg-pu-type");
  d.ear_side = getSeg("seg-pu-ear");
  d.payment_status = getSeg("seg-pu-pay");
  d.customer_id = puCustomer ? puCustomer.id : "";
  d.product_id = puProduct ? puProduct.id : "";
  return d;
}

function editPurchase(id) {
  const p = purchaseRows.find(x => x.id === id);
  if (!p) return;
  purchaseEditId = id;
  for (const f of PUR_TEXT_IDS) $("pu-" + f).value = p[f] ?? "";
  for (const f of PUR_DATE_IDS) $("pu-" + f).value = DF.fromISO(p[f] ?? "");
  setSeg("seg-pu-type", p.transaction_type || "purchase");
  setSeg("seg-pu-ear", p.ear_side || "");
  setSeg("seg-pu-pay", p.payment_status || "unpaid");
  puCustomer = { id: p.customer_id, label: p.customer_code ? `${p.customer_name}（${p.customer_code}）` : p.customer_name };
  $("pu-customer").value = puCustomer.label;
  if (p.product_id) { puProduct = { id: p.product_id, label: `${p.brand} ${p.model_name}` }; $("pu-product").value = puProduct.label; }
  else clearPurProduct();
  $("pur-form-title").textContent = `編輯交易：${p.customer_name}`;
  $("pur-meta").textContent = p.updated_at ? `最後更新 ${DF.fromISODateTime(p.updated_at)}` : "";
  $("btn-pur-save").textContent = "儲存變更";
  $("btn-pur-del").classList.remove("hidden");
  showView("purchases");
  window.scrollTo(0, 0);
}

function clearPurchaseForm() {
  purchaseEditId = null;
  for (const f of PUR_TEXT_IDS) $("pu-" + f).value = "";
  for (const f of PUR_DATE_IDS) $("pu-" + f).value = "";
  setSeg("seg-pu-type", "purchase"); setSeg("seg-pu-ear", ""); setSeg("seg-pu-pay", "unpaid");
  puCustomer = null; $("pu-customer").value = "";
  clearPurProduct();
  $("pur-form-title").textContent = "新增交易";
  $("pur-meta").textContent = "";
  $("btn-pur-save").textContent = "建立交易";
  $("btn-pur-del").classList.add("hidden");
}

async function savePurchase() {
  hideStatus();
  if (!puCustomer) { setStatus("請先選擇客戶。", "danger"); return; }
  if (!$("pu-transaction_date").value.trim()) { setStatus("「交易日期」為必填。", "danger"); return; }
  let d;
  try { d = purchaseFormData(); }
  catch (e) { setStatus(e.message, "danger"); return; }   // 日期格式錯誤
  try {
    if (purchaseEditId === null) {
      await api("/api/purchases", { method: "POST", body: JSON.stringify(d) });
      setStatus("交易已建立。", "success");
      clearPurchaseForm();
    } else {
      await api(`/api/purchases/${purchaseEditId}`, { method: "PUT", body: JSON.stringify(d) });
      setStatus("已儲存變更。", "success");
    }
    await loadPurchases();
  } catch (e) { setStatus(e.message, "danger"); }
}

async function deletePurchase() {
  if (purchaseEditId === null) return;
  if (!confirm("確定刪除這筆交易？")) return;
  try {
    await api(`/api/purchases/${purchaseEditId}`, { method: "DELETE" });
    setStatus("交易已刪除。", "success");
    clearPurchaseForm();
    await loadPurchases();
  } catch (e) { setStatus(e.message, "danger"); }
}

// ── 查詢結果（前端過濾＋分頁）─────────────────────────────────
async function loadPurchases() {
  const r = await api("/api/purchases");
  purchaseRows = r.rows;
  if (!$("view-pur-results").classList.contains("hidden")) renderPurchasePage();
}

function searchPurchases() {
  pfPage = 1;
  renderPurchasePage();
  showView("pur-results");
  window.scrollTo(0, 0);
}

function purGoPage(delta) {
  const size = +$("pur-page-size").value;
  const pages = Math.max(1, Math.ceil(purFiltered.length / size));
  pfPage = Math.min(pages, Math.max(1, pfPage + delta));
  renderPurchasePage();
}

function renderPurchasePage() {
  const cq = $("puf-customer").value.trim().toLowerCase();
  const pq = $("puf-product").value.trim().toLowerCase();
  const tq = $("puf-type").value, yq = $("puf-pay").value;
  const has = (v, q) => !q || String(v || "").toLowerCase().includes(q);
  purFiltered = purchaseRows.filter(p =>
    has(`${p.customer_name} ${p.customer_code || ""}`, cq) &&
    has(`${p.brand || ""} ${p.model_name || ""}`, pq) &&
    (!tq || p.transaction_type === tq) && (!yq || p.payment_status === yq));

  const size = +$("pur-page-size").value;
  const total = purFiltered.length;
  const pages = Math.max(1, Math.ceil(total / size));
  if (pfPage > pages) pfPage = pages;
  const start = (pfPage - 1) * size;
  const rows = purFiltered.slice(start, start + size);
  $("pur-count").textContent = `（${total} 筆）`;
  $("pur-body").innerHTML = rows.map(p => `
    <tr onclick="editPurchase(${p.id})">
      <td>${esc(DF.fromISO(p.transaction_date)) || "—"}</td>
      <td>${esc(p.customer_name)}${p.customer_code ? `<br><span class="hint">${esc(p.customer_code)}</span>` : ""}</td>
      <td>${p.product_id ? esc(`${p.brand} ${p.model_name}`) : "—"}</td>
      <td>${TX_TYPE[p.transaction_type] || esc(p.transaction_type)}</td>
      <td>${p.quantity ?? 1}</td>
      <td class="price-col">${p.unit_price != null ? Number(p.unit_price).toLocaleString() : "—"}</td>
      <td>${PAY[p.payment_status] || esc(p.payment_status)}</td>
      <td>${esc(DF.fromISO(p.warranty_end_date ?? "")) || "—"}</td>
    </tr>`).join("") || `<tr><td colspan="8" class="hint">查無符合條件的交易。</td></tr>`;
  $("pur-pager").classList.toggle("hidden", pages <= 1);
  $("pur-page-info").textContent = `第 ${pfPage} / ${pages} 頁`;
  $("pur-prev").disabled = pfPage <= 1;
  $("pur-next").disabled = pfPage >= pages;
}

// ── 開機 ─────────────────────────────────────────────────────
for (const f of PUR_DATE_IDS) { $("pu-" + f).placeholder = DF.placeholder; attachDateMask($("pu-" + f)); }
document.querySelectorAll(".df-hint").forEach(el => el.textContent = `（${DF.hint}）`);

initHeader().then(async () => {
  await ensureCodes();
  try { allProducts = (await api("/api/products")).rows; } catch { allProducts = []; }
  await loadPurchases();
}).catch(e => { if (e.message !== "未登入") setStatus(e.message, "danger"); });
