# y1crm — 助聽器門市 CRM（睿聲助聽器‧中正門市）

2026-07-11 自 Blazor Server 版轉型而來。原 .NET 專案完整封存於 `archive/y1crm-blazor/`，
資料庫沿用本機 MySQL `crm1`（表持續擴充）。

## 架構

| 項目 | 內容 |
|---|---|
| 後端 | FastAPI（`api/app.py`）+ pymysql 直連本機 `crm1`；LINE 相關獨立在 `api/line_bot.py`、客服 AI 在 `api/ai_chat.py` |
| 前端 | **MPA**：一個主功能一頁，Jinja2 `webui/templates/` 繼承 `layout.html`，程式在 `webui/static/js/`。無框架、無建置步驟 |
| 入口 | `run.py`，port **8004**（TWII=8002、TWIII=8003，互不衝突） |
| venv | `./venv`（獨立建立，**不共用** TWII 的 venv） |
| 認證 | `/api/login` bcrypt 驗證 + HMAC 簽章 session cookie（12 小時），密鑰存 `.session_secret` |
| 組態 | `config.py` 三層覆寫：內建預設 < `config.toml` < 環境變數 `Y1CRM_<區段>_<鍵>`。區段：`[storage] [db] [ocr] [server] [line] [ai] [board]` |

**前端為何是 MPA**：門市用的是 4G 記憶體 Android 平板。切換主功能＝瀏覽器導頁＝JS heap 歸零。
功能內的子檢視才留在頁內切換。舊的單檔 SPA `webui/index.html` 已於 2026-07-13 移除
（留底 `archive/webui-spa/`）。

## 啟動 / 停止

已是 systemd 服務（開機自啟）：

```bash
sudo systemctl restart y1crm
sudo systemctl status y1crm
journalctl -u y1crm -f
```

`start.sh` / `stop.sh`（nohup 版）僅保留作手動備用，平時不用。

## 全站慣例（新功能必須遵守）

- **日期一律走民國年過濾器**：使用者輸入經 `dfToISO()` 轉西元後才送 API，顯示經 `DF.fromISO()`。
  **API 與資料庫永遠只存西元 ISO（YYYY-MM-DD）**。切換點是 `common.js` 的 `const DF = ROC_FILTER`，
  改成 `GREGORIAN_FILTER` 即全站切回西元。
- **DB 不存 BLOB**：所有上傳檔落磁碟 `config.STORAGE.base_dir`（`/mnt/raid1/y1crm_uploads`），
  **DB 只存相對路徑** → 換碟只改 base_dir 一行。serving 一律經需登入的端點對映 id→路徑，
  **資料夾絕不可設成 static**（會外洩全客戶附件）。備份要同時涵蓋 DB 與 base_dir。
- **新表一律指定 `utf8mb4_unicode_ci`**（MySQL 8 預設的 `utf8mb4_0900_ai_ci` 與既有表 JOIN 會噴 1267）。
- 機密只放 `config.toml`（已在 `.gitignore`），樣板 `config.example.toml` 才進版控。

## 介面

- 亮色主題：**紅（#c8102e）× 白**；暗色主題：**紅（#d5253f）× 黑**
- 預設跟隨系統深淺色，使用者選單可手動切換（記在 localStorage）
- 表單為 ERP 式雙欄格線，窄螢幕（≤760px）自動收成單欄；導覽在手機為側滑抽屜
- 導覽選單由 `menus` 資料表驅動（支援多層），改選單＝改資料表，不必動程式

## 權限

- `menu_permissions`（menu_key × level）決定選單可見性；`_current_user` 每次請求從 DB 重讀 level
  （改權限即時生效、舊 session 無法越權）
- `_CAPS` 的 `view_price` 限 manager/admin：`_mask_row` 遮蔽價格欄位，寫入端也丟棄無權限者送來的價格欄
- level：admin / manager / sales / user

## 功能

**客戶**
- 建檔／查詢共用同一份表單，任一欄位皆可當查詢條件；建檔只需姓名
- 客戶編號留空自動產生 `C{yyyyMMdd}-NNN`；重複客戶偵測（姓名／手機／身分證）
- 軟性檢核不擋存檔：身分證格式與檢查碼、手機 09 開頭 10 碼、Email 格式
- 客戶照片（多張，前端縮至 1600px）、紙本表單影像、待辦事項（含多檔附件）、交易紀錄內嵌
- **紙本表單 OCR 匯入**：拍照／選檔 → 本機 qwen3-vl 辨識 → 自動填表，沒把握的欄位黃底標示。
  GPU 隨選熱機（「載入模型」鈕）＋閒置 30 分鐘自動休眠

**行事曆**（品牌 logo 即入口）
- FullCalendar v6（自架於 `static/vendor/`，「前端零外掛」原則的唯一例外）
- 四資料源合併：回訪提醒／客戶待辦／個人事件／門市事件。個人事件只有擁有者看得到，門市事件全店共見共編

**商品／交易**
- products 為型錄主檔（SKU、品牌、型號、規格、助聽器專屬 ha_* 欄位、補助款別等），可掛多個任意類型附件
- 現有 434 筆商品由 `scripts/dm_import.py` 自 DM.pdf 型錄匯入
- purchases 交易紀錄：獨立頁與客戶明細內嵌兩種入口

**LINE**（`api/line_bot.py`）
- 客戶綁定：店員在 CRM 從好友名單點選配對（客戶端零輸入），亦保留自助輸入手機號路徑
- 圖文選單六格：預約／查詢預約／FAQ／最新活動／門市資訊／真人客服
- 線上預約：客戶輸入「預約」取得一次性連結 → 公開表單頁 `/lp/booking` → 進 `booking_requests`
  → 店員在 `/bookings` 處理
- 預約提醒：前一天 `reminder_hour` 自動推播，附一顆「確認前往」按鈕；`reminded_at` 為冪等鎖
- **客服 AI**（`api/ai_chat.py`）：本機推論、個資不出區網。輸入與輸出雙層禁區防護
  （價格／醫療／政治／金融／色情／犯罪），命中價格或醫療即轉人工。
  客戶傳來的圖片交給另一張卡的視覺模型判讀
- **對話頁 `/line-chat`**：對話清單＋訊息串＋圖片預覽＋人工接手／交還。
  **只看不回**——店員回覆一律在 LINE 官方帳號 App（不佔推播額度，且 LINE 不會把店員回覆送進 webhook）

**其他**
- 人員管理 `/staff`（僅 admin）：帳號 CRUD、人員照片、防鎖死（不能刪自己／改自己權限／刪掉最後一位 admin）
- 店面狀態看板 `/board?k=<金鑰>`：免登入電視牆，伺服器端遮罩姓名與電話、不顯示金額

## 對外網路

公網入口 `ap888.duckdns.org`（反向代理在 **.26 那台 Windows**，不在本機）。
LINE 只吃 443 且不可帶自訂 port，故 IIS 上以 ARR 規則只放行兩條路徑到 `.88:8004`：
`/api/line/webhook` 與 `/lp/*`。CRM 本體走 :5175。

⚠ 路由器**沒有 hairpin NAT**：店內 WiFi 連不到 `ap888.duckdns.org`，
店內手機測試要關 WiFi 走行動網路，真實客戶不受影響。

## 資料表（crm1）

| 用途 | 表 |
|---|---|
| 主檔 | `customers`、`users`、`app_settings`、`codes` |
| 選單／權限 | `menus`、`menu_permissions` |
| 客戶周邊 | `customer_photos`、`customer_todos`、`customer_todo_attachments`、`follow_up_reminders`、`subsidy_applications` |
| 商品／交易 | `products`、`product_attachments`、`purchases` |
| 行事曆 | `calendar_events` |
| LINE | `line_followers`、`line_conversations`、`line_messages`、`booking_requests`、`booking_tokens` |

`subsidy_applications`、`follow_up_reminders` 目前還沒有管理 UI（行事曆為唯讀顯示提醒）。

⚠ 本專案沒有 migration／seed 檔，建表與 `menus`、`menu_permissions`、`codes` 的內容都是
直接對 DB 操作的。換機器部署時這些要手動補。

紙本表單參考：`doc/form1.jpg`。
