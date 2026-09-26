# Render 出勤系統 V0.6.0｜主總表自動同步版

本版本在 V0.5.x 基礎上加入「主總表自動同步」。正式運作時不必每次手動把總表上傳到 Render；Render 會從 Google Drive 讀取指定的私有 XLSX，依排程檢查檔案是否更新，更新才同步到 PostgreSQL。

## 新增功能
- Google Drive 私有 XLSX 自動同步，預設每 10 分鐘檢查。
- 啟動時可先同步一次（`MASTER_SYNC_ON_STARTUP=true`）。
- 使用 Google Drive `fileId` + Service Account；檔案不需要公開。
- 先取得檔案版本／checksum，未更新就不重匯。
- 同步失敗保留上一份可用資料，不直接清空目前出勤資料。
- PostgreSQL advisory lock 防止多 worker 同時同步。
- `/admin/master-sync` 可查看最後檢查、最後成功、來源版本、課程／LINE 綁定數量與錯誤。
- `立即檢查並同步` 是手動備援，不取代自動同步。
- 原本 `/admin/line` 的 Excel 上傳仍保留，作為緊急備援。

## Google Drive 設定
1. 在 Google Cloud 建立 Service Account。
2. 取得 Service Account 的 email。
3. 將你的正式主總表 XLSX 只分享給這個 Service Account，權限給 Viewer 即可。
4. 取得該檔案的 `File ID`。
5. 在 Render 設定：
   - `MASTER_SYNC_ENABLED=true`
   - `MASTER_SYNC_PROVIDER=google_drive`
   - `MASTER_SYNC_INTERVAL_MINUTES=10`
   - `GOOGLE_DRIVE_FILE_ID=<你的檔案ID>`
   - `GOOGLE_SERVICE_ACCOUNT_JSON_BASE64=<Service Account JSON 的 Base64>`

Base64 產生方式（Windows PowerShell）：
`[Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\path\service-account.json"))`

產生後只把結果貼到 Render 的 `GOOGLE_SERVICE_ACCOUNT_JSON_BASE64`，不要把 JSON 檔或私鑰放進 ZIP。

## 正式資料流
你的 V2.8.3 主總表 → Google Drive → Render 自動檢查 → 驗證 XLSX → 同步「實際課程／出勤學生／出勤LINE綁定／出勤通知模板／出勤設定」→ PostgreSQL → QR 出勤與 LINE。

## 安全規則
- 不把 Token、Secret 或 Service Account JSON 放進 ZIP。
- 同步來源驗證失敗或整份「實際課程」無法解析時，不覆蓋目前可用資料。
- 建議只分享主總表給本服務專用 Service Account，不要設為「知道連結的任何人可查看」。

## Render
Build Command：`pip install -r requirements.txt`
Start Command：`uvicorn app:app --host 0.0.0.0 --port $PORT`
Health Check：`/health`

部署後建議先檢查：
1. `/health` → `version=0.6.0`。
2. `/admin/master-sync` → 顯示自動同步設定。
3. 第一次同步成功後，查看「實際課程」是否有未來課程。
4. LINE 測試維持正常。

## 紀錄保留政策

- `line_message_logs` 與 `notification_logs` 不自動清除。
- 管理員可從 `/admin/logs` 個別刪除或全部刪除。
- 個別刪除需二次確認；全部刪除需輸入指定確認文字。
- 刪除只影響管理後台紀錄，不會撤回 LINE 已送出的訊息，也不會刪除出勤、學生、課程、LINE 綁定或通知範本。
