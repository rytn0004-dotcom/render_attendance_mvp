# Render 出勤系統 V0.4.6｜實際課程驅動版

這版是接續 V0.4.4 內測的架構調整：

- **出勤唯一課程來源 = 主總表「實際課程」**。
- 固定課表 → 已確認調課 → 實際課程的原有流程維持在主系統。
- 「課程提醒」維持原系統獨立發送，**不會反過來決定出勤時間**。
- Render 每次由管理員上傳目前總表 `.xlsx` 後，同步「實際課程」「出勤學生」「出勤LINE綁定」。LINE 正式 Push 則由 `LINE_MODE=live` 與 Messaging API Token 控制。
- 學生 QR 為固定識別碼；教室手機／設備仍需先配對。
- 家長 LINE 綁定採「一列＝一位學生＋一位家長」，每一位可獨立開關提醒。
- 今日臨時校正仍可在 Render「實際課程」頁面進行，僅影響指定日期，不改總表。

## 總表同步工作表

必需／建議包含：

1. `實際課程`
2. `出勤學生`
3. `出勤LINE綁定`

相容舊 V4 的 `聯絡人`：如果 `出勤LINE綁定` 沒有可用資料，系統會回退從 `聯絡人` 的「身分=家長」與「學生姓名/關聯」拆分 LINE 綁定。

### 實際課程時間

系統支援下列時間格式：

- `18:00-19:30`
- `18:00～19:30`
- `18:00 ~ 19:30`
- 分欄：`開始時間` + `下課時間`

如果只提供單一開始時間，仍可記錄到班／離班；但**無法自動判斷「未離班」**，後台會標示缺少下課時間。

## LINE 正式發送設定

這版已將 Render Blueprint 的 `LINE_MODE` 預設為 `live`，但**不會把任何密鑰寫進 ZIP**。部署／更新到現有 Render 服務後，請在 Environment 補齊：

- `LINE_MODE=live`
- `LINE_CHANNEL_ACCESS_TOKEN`：Messaging API Channel Access Token
- `LINE_CHANNEL_SECRET`：Messaging API Channel Secret
- `LINE_ADMIN_USER_ID`：要接收管理員異常／測試通知的 LINE User ID

`LINE_LOGIN_CHANNEL_ID` 與 `LIFF_ID` 仍可保留；它們不是 Push Message 的 Access Token。

如果 `LINE_MODE=live` 但上述任一必要值缺少，後台 `/admin/line` 會直接顯示缺少哪些設定，不會把「正式發送」誤顯示成已可用。

## Render

Build Command：

```text
pip install -r requirements.txt
```

Start Command：

```text
uvicorn app:app --host 0.0.0.0 --port $PORT
```

LINE／資料庫環境變數沿用目前 V0.4.x，不需要重新建立 PostgreSQL。

## 建議測試流程

1. Render → `/admin/line` 上傳最新版總表。
2. 查看同步結果中的「實際課程」筆數。
3. Render → `/admin/courses` 查看今天的實際課程。
4. 如有臨時調課，可在指定日期按「儲存今天校正」。
5. 教室手機完成設備配對後，掃學生固定 QR。
6. 出勤系統只會使用今天 `實際課程` 的課程資料做判斷。
7. 家長通知則依 `出勤LINE綁定` 與個別「家長提醒」設定逐一 Push。

## 重要資料原則

- `實際課程` = 出勤排課真實來源
- `出勤學生` = 學生固定簽到識別與通知開關
- `出勤LINE綁定` = 學生與每一位家長 LINE User ID 的關係
- `課程提醒` = 原系統獨立通知佇列，不作為出勤時間來源


## V0.4.8 修正
- 自動未到班／未離班通知加入 PostgreSQL row lock 與唯一鍵併發保護，避免背景檢查重複 LINE Push。
- 管理首頁重新整理不再直接觸發自動通知；背景檢查每 60 秒執行。
- 修正 PostgreSQL `SELECT DISTINCT ... ORDER BY id` 測試 LINE 查詢錯誤。
