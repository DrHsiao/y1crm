# y1crm — 助聽器門市 CRM

一間助聽器門市自己用的客戶管理系統：客戶建檔、回訪提醒、行事曆、商品與交易紀錄，
加上 LINE 官方帳號整合（綁定、線上預約、預約提醒、客服 AI）。
門市的平板、手機、電腦都用瀏覽器開，不必安裝 App。

> 本頁是簡介。完整的架構、全站慣例、權限、資料表與對外網路設定見 **[`doc/README.md`](doc/README.md)**。

## 功能一覽

| 模組 | 內容 |
|---|---|
| 客戶 | 建檔與查詢共用一份表單（任一欄可當查詢條件）、重複客戶偵測、身分證與手機格式檢核、照片、待辦與附件、交易紀錄 |
| 紙本表單 OCR | 拍下紙本客戶資料表 → 本機視覺模型辨識 → 自動填表，沒把握的欄位黃底標示 |
| 行事曆 | 回訪提醒、客戶待辦、個人事件、門市事件四種來源合併在一個行事曆 |
| 商品／交易 | 型錄主檔（品牌、型號、規格、補助款別）與附件；交易紀錄可從客戶頁直接看 |
| LINE | 客戶綁定、圖文選單、線上預約、前一天自動提醒、對話頁（店員只看、回覆在 LINE 官方帳號 App） |
| 客服 AI | 本機模型自動回覆；價格、醫療、政治等禁區雙層防護，碰到價格或醫療就轉真人 |
| 人員與權限 | 四個層級（admin／manager／sales／user）；選單與價格欄位依權限顯示 |
| 店面看板 | 免登入的電視牆頁面，伺服器端遮罩姓名與電話、不顯示金額 |

**個資不出門市**：OCR、客服 AI、圖片判讀都跑在本機顯卡上的模型（ollama），不呼叫雲端 AI。

## 技術架構

| 項目 | 內容 |
|---|---|
| 後端 | Python 3.12、FastAPI、PyMySQL（DBUtils 連線池）、bcrypt |
| 前端 | 多頁式（MPA）：一個主功能一頁，Jinja2 樣板＋原生 JavaScript，無框架、無建置步驟 |
| 資料庫 | MySQL 8（`utf8mb4_unicode_ci`）；上傳檔放磁碟，資料庫只存相對路徑 |
| AI | 本機 ollama：視覺模型做 OCR 與圖片判讀、語言模型做客服回覆 |
| 部署 | systemd 服務 `y1crm`，`run.py` 啟動（port 8004） |

為什麼是多頁式：門市用的是 4 GB 記憶體的 Android 平板，換頁就把瀏覽器記憶體歸零，比單頁應用穩定。

## 目錄

| 路徑 | 內容 |
|---|---|
| `api/` | `app.py` 主程式與 API、`line_bot.py` LINE、`ai_chat.py` 客服 AI、`dbpool.py` 連線池 |
| `webui/` | `templates/` 各頁樣板（繼承 `layout.html`）、`static/` 程式與樣式 |
| `config.py` | 設定三層覆寫：內建預設 < `config.toml` < 環境變數 `Y1CRM_<區段>_<鍵>` |
| `config.example.toml` | 設定樣板（真正的 `config.toml` 含機密，不進版控） |
| `scripts/` | `seed_menus.py` 導覽選單種子、`dm_import.py` 型錄匯入、`line_richmenu_setup.py` LINE 圖文選單 |
| [`scripts/ops/`](scripts/ops/README.md) | 主機健康檢查與每日摘要（Telegram 告警，只在狀態改變時推） |
| [`scripts/backup/`](scripts/backup/README.md) | 每日備份（資料與 MySQL → 另一顆實體碟） |
| [`scripts/gpu/`](scripts/gpu/README.md) | GPU 溫控與 ollama 熱機／看門狗 |
| `doc/` | 詳細說明 [`doc/README.md`](doc/README.md) |
| `archive/` | 2026-07 以前的 .NET Blazor 版原始碼（留底） |

## 啟動

```bash
cp config.example.toml config.toml        # 填資料庫、儲存路徑、LINE、AI 等設定
python3 -m venv venv
./venv/bin/pip install fastapi uvicorn jinja2 pymysql DBUtils bcrypt pillow   # 型錄匯入另需 pymupdf
./venv/bin/python run.py                  # http://<主機>:8004
```

正式環境用 systemd：`sudo systemctl restart y1crm`、`journalctl -u y1crm -f`。

⚠ 資料表沒有 migration 機制；換機器部署時，導覽選單用 `scripts/seed_menus.py` 補齊（可重跑、預設只新增不覆蓋），
其餘資料表結構要從既有資料庫匯出。詳見 [`doc/README.md`](doc/README.md)〈資料表〉。

## 版本歷程

- 2026-07-11：由 .NET Blazor Server 版改寫為 FastAPI＋MPA（舊版封存在 `archive/y1crm-blazor/`）
- 2026-07：紙本表單 OCR、行事曆、LINE 綁定與預約、預約提醒、客服 AI、對話頁
- 2026-08～09：GPU／ollama 維運腳本、連線池、主機健康檢查與 Telegram 告警、每日備份
