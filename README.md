# Render 出勤系統 V0.8.1｜平日自動 K 書出勤（待測試）

本版本在 V0.5.x 基礎上加入「主總表自動同步」。正式運作時不必每次手動把總表上傳到 Render；Render 會從 Google Drive 讀取指定的私有 XLSX，依排程檢查檔案是否更新，更新才同步到 PostgreSQL。

## 新增功能
- Google Drive 私有 XLSX／Google 試算表自動同步，預設每 10 分鐘檢查。
- 學生姓名標準化：陳翊森→翊森、依柔→葉依柔。
- 生物團班對應翊森與葉依柔；如來源省略學生姓名，依此規則產生兩人的獨立簽到課程。
- 即使來源將「生物團班」誤填在學生姓名欄，也會展開為翊森與葉依柔；相同學生／日期／課程／老師／時段的重複列會在同步時去重，避免重複點名。
- 「雙翁」拆分為褕安與褕嫺，兩人各自建立課程／簽到紀錄，家庭 LINE 綁定別名可對應兩位學生。
- 「昀瑄國文體驗課」解析為學生昀瑄、課程「國文體驗課」。
- 啟動時可先同步一次（`MASTER_SYNC_ON_STARTUP=true`）。
- 使用 Google Drive `fileId` + Service Account；檔案不需要公開。
- V0.6.1 修正 Service Account 憑證解析：JSON／Base64 可互相 fallback，也兼容誤貼格式與 `google_sheets` 設定別名。
- 先取得檔案版本／checksum，未更新就不重匯。
- 同步失敗保留上一份可用資料，不直接清空目前出勤資料。
- PostgreSQL advisory lock 防止多 worker 同時同步。
- 新增「明日簽到表」唯讀預覽頁，可提前核對明日有效實際課程、教師、時段及明日校正備註；不建立出勤紀錄、不發送 LINE 通知、不修改 QR。
- `/admin/master-sync` 可查看最後檢查、最後成功、來源版本、課程／LINE 綁定數量與錯誤。
- `立即檢查並同步` 是手動備援，不取代自動同步。
- 原本 `/admin/line` 的 Excel 上傳仍保留，作為緊急備援。

## Google Drive 設定
1. 在 Google Cloud 建立 Service Account。
2. 取得 Service Account 的 email。
3. 將你的正式主總表 XLSX 或 Google 試算表只分享給這個 Service Account，權限給 Viewer 即可。
4. 取得該檔案的 `File ID`（Google 試算表網址 `/spreadsheets/d/` 後面的那段）。
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
你的 V2.8.3 主總表 → Google Drive／Google 試算表 → Render 自動檢查 → 匯出/驗證 XLSX → 同步「實際課程／出勤學生／出勤LINE綁定／出勤通知模板／出勤設定」→ PostgreSQL → QR 出勤與 LINE。

## 安全規則
- 不把 Token、Secret 或 Service Account JSON 放進 ZIP。
- 同步來源驗證失敗或整份「實際課程」無法解析時，不覆蓋目前可用資料。
- 建議只分享主總表給本服務專用 Service Account，不要設為「知道連結的任何人可查看」。

## Render
Build Command：`pip install -r requirements.txt`
Start Command：`uvicorn app:app --host 0.0.0.0 --port $PORT`
Health Check：`/health`

部署後建議先檢查：
1. `/health` → `version=0.6.1`。
2. `/admin/master-sync` → 顯示自動同步設定。
3. 第一次同步成功後，查看「實際課程」是否有未來課程。
4. LINE 測試維持正常。

## 紀錄保留政策

- `line_message_logs` 與 `notification_logs` 不自動清除。
- 管理員可從 `/admin/logs` 個別刪除或全部刪除。
- 個別刪除需二次確認；全部刪除需輸入指定確認文字。
- 刪除只影響管理後台紀錄，不會撤回 LINE 已送出的訊息，也不會刪除出勤、學生、課程、LINE 綁定或通知範本。


## V0.8.0：K 書出勤（功能分支，尚未合併正式版）

- 讀取 Google 總表中的「K書學生」與「K書出勤安排」兩張工作表。
- 不讀取、不修改原本的「拉K」週次紀錄表；不會依該表推定學生哪天應到。
- 一般 K 書以「平日固定」為預設：在「K書學生」每位學生填一次預計到班、預計離班時間，系統自動產生星期一至星期五的每日簽到課程，不必為每週五天建立五筆排班。
- 「K書出勤安排」主要用於例外：某天不來時新增「單日停用」，填指定日期並把「安排啟用」設為「是」；將開關改為「否」即可恢復該日。也支援特殊單日、每週或平日時段覆寫。
- 新增管理頁 `/admin/kbook`，顯示今日安排、實際到班／離班、平日預設排班、單日例外與同步診斷。
- 「未離班處理」預設為「只顯示後台」，不會只因超過預計離班時間就自動推播；家長到班／離班通知預設關閉，除非明確設定開啟。
- 學生配對優先使用「系統學生編號（對應出勤學生）」；若未填編號，先找唯一同名學生；若完全找不到既有姓名，才建立新的系統學生與固定 QR。若同名有多筆、填入的編號不存在或已停用，該列會顯示同步警告，不會建立重複學生。既有 K書設定ID 會沿用同一學生與 QR。
- 「K書學生」新增欄位：`平日固定K書`、`預計到班時間`、`預計離班時間`。平日固定預設開啟，但時間未填妥時不會猜測時段，會顯示同步警告並略過每日簽到表。既有試算表需要補上這三個欄位。開始日期當天起生效；停用日期當天起不再排班。停用學生／排班時保留歷史簽到資料與固定 QR。
- 本版本仍需在獨立測試資料庫與測試總表驗證；通過測試前不應合併至 `main` 或直接部署正式服務。
