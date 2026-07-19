# y1crm — 助聽器門市 CRM（TWIII 式架構）

2026-07-11 自 Blazor Server 版轉型而來。原 .NET 專案完整封存於 `archive/y1crm-blazor/`，
資料庫 schema 與資料（本機 MySQL `crm1`）完全不變。

## 架構（比照 TWII/TWIII）

| 項目 | 內容 |
|---|---|
| 後端 | FastAPI（`api/app.py`）+ pymysql 直連本機 `crm1` |
| 前端 | 單檔 `webui/index.html`（無框架，比照 TWIII） |
| 入口 | `run.py`，port **8004**（TWII=8002、TWIII=8003，互不衝突） |
| venv | `./venv`（獨立建立，**不共用** TWII 的 venv） |
| 認證 | `/api/login` bcrypt 驗證 + HMAC 簽章 session cookie（12 小時），密鑰存 `.session_secret` |

## 啟動 / 停止

```bash
./start.sh   # nohup 背景執行，日誌 y1crm.log
./stop.sh
```

## 介面（企業識別配色）

- 亮色主題：**紅（#c8102e）× 白** — 紅色頂列、白底卡片、紅色區段標題與主按鈕
- 暗色主題：**紅（#d5253f）× 黑** — 黑底、暗卡片、紅色重點
- 預設跟隨系統深淺色；頂列「深色/淺色」按鈕可手動切換（記在 localStorage）
- 表單為 ERP 式雙欄格線，窄螢幕（≤760px）自動收成單欄

## 功能（與 Blazor 版對齊）

- 登入（users 表 bcrypt；舊明碼帳號登入成功後自動升級為 bcrypt）
- **行事曆（登入後首頁）**：`follow_up_reminders` 月曆檢視，五種提醒類型色塊
  （回診/保養/電池更換/調機/其他），已完成淡化加刪除線、已取消不顯示，
  單日超過 3 筆顯示「還有 N 筆」，點提醒開明細（可直接跳客戶資料）；
  API：`GET /api/calendar?start&end`
- 客戶建檔/查詢共用表單：任一欄位皆可當查詢條件；建檔只需姓名
- 客戶編號/姓名/身分證字號/出生年月日 欄位旁各有「查詢」鈕（只以該欄位單獨查詢；
  底部「查詢」= 所有已填欄位 AND；明細編輯模式下自動隱藏）
- 客戶編號留空自動產生 `C{yyyyMMdd}-NNN`
- 重複客戶偵測（姓名/手機/身分證失焦觸發；建檔遇疑似重複需按兩次確認)
- 軟性檢核（不擋存檔）：身分證格式/檢查碼/性別交叉、手機 09 開頭 10 碼、Email 格式
- 出生年月日顯示民國年與年齡；「同戶籍地址」一鍵複製
- 查詢結果 0 筆提示、1 筆直接開明細、多筆列表（上限 200）
- 明細編輯：儲存、建檔/更新時間與修改者顯示
- 唯一索引衝突轉為友善訊息（身分證重複、客戶編號重複）

## 資料表（crm1，沿用）

customers（主檔）、users（帳號）、app_settings（公司/門市名稱）、
codes / products / purchases / subsidy_applications / follow_up_reminders（尚未有 UI，保留待開發）。

紙本表單參考：`doc/form1.jpg`。
