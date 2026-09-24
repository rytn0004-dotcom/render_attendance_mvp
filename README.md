# Render 出勤系統 V0.5.2｜完整篩查修正版

本版本以 V0.5.1 full-audit 為基礎，針對目前 Render `/admin/templates` 持續 500、Excel「實際課程」同步、LINE 測試與自動異常提醒做完整檢查與修正。

## 本版重點
- `/admin/templates` 開啟時自動修復舊版 `notification_templates` 表結構。
- 即使資料庫版本很舊，也會補齊必要欄位，不刪除既有範本。
- 增加 `/admin/diagnostics`，可直接檢查資料表、欄位、筆數與 LINE 設定是否存在。
- `/health` 顯示 V0.5.2 與 LINE 設定是否存在，並禁止快取，方便確認 Render 是否真的跑到最新 build。
- 全站管理頁未處理例外會顯示可診斷頁面與錯誤編號，不再只出現黑底 `Internal Server Error`。
- `實際課程` Excel parser 支援正式欄位 `Course ID／課程日期／上課時間／學生／課程／老師`，並支援 `下課時間（出勤用）` 在上一列標題區的格式。
- 若某日期 Excel 有部分資料格式錯誤，不會先把該日期舊實際課程全部停用，避免半份資料覆蓋。
- 出勤課程查詢只接受 `source='實際課程'`，demo/舊 legacy 課程不再混入。
- 自動未到班／未離班檢查加入 PostgreSQL advisory lock、row lock 與失敗重試冷卻，避免多 worker 或 LINE 暫時失敗造成刷屏。
- `SEED_DEMO_DATA` 預設 false，不會在正式環境自動生成測試學生與測試課程。
- 通知範本變數改為安全替換，未知變數不會讓出勤流程崩潰。
- 正式 LINE Push 設定名稱維持 `LINE_CHANNEL_ACCESS_TOKEN`、`LINE_CHANNEL_SECRET`、`LINE_ADMIN_USER_ID`。

## Render 更新
Build Command：`pip install -r requirements.txt`
Start Command：`uvicorn app:app --host 0.0.0.0 --port $PORT`

請使用目前 Render 服務既有的 `DATABASE_URL`，不要建立第二個 PostgreSQL。

部署後請依序檢查：
1. `/health` 必須顯示 `version=0.5.2`。
2. `/admin/diagnostics` 檢查 `notification_templates` 與其他資料表欄位是否完整。
3. `/admin/templates` 可以正常開啟。
4. `/admin/line` 的「測試 LINE」保持正常。
5. 上傳最新版 V2.8.3 總表後，確認「實際課程」不是 0 筆。

不要把任何 LINE Token/Secret 寫進 ZIP。

## 紀錄保留政策
- `line_message_logs` 與 `notification_logs` 不自動清除。
- 管理員可從 `/admin/logs` 個別刪除或全部刪除。
- 個別刪除需二次確認；全部刪除需輸入指定確認文字。
- 刪除只影響管理後台紀錄，不會撤回 LINE 已送出的訊息，也不會刪除出勤、學生、課程、LINE 綁定或通知範本。
