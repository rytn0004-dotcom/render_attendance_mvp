# 出勤簽到測試系統 V0.2

這一版是 Render 隔離測試用，與原本 LINE／課務系統分開。

## 新增功能
- 後台「課程時間」：可修改永久週課表。
- 「今日校正」：只修改指定日期的開始／結束時間，不改掉整個週課表；今日校正會直接影響今天的簽到、遲到與未離班判斷。
- 每堂課可設定「遲到門檻」與「未離班通知延遲」。
- 後台「校正」：可補登／修改實際到班、離班時間、狀態與備註，並留下校正者與校正時間。
- 自動未到通知：課程開始時間＋遲到門檻後，仍沒有到班紀錄時建立未到紀錄並通知管理者。
- 自動遲到通知：學生掃描後若超過該課程遲到門檻，通知管理者。
- 自動未離班通知：課程結束時間＋未離班延遲後，仍無離班時間時通知管理者。
- 手動通知：後台可單獨發送遲到、未到、未離班通知。
- LINE 仍可維持 simulation 模式做大量測試；切成 live 後才真的呼叫 LINE Messaging API。
- 無需 templates 資料夾，首頁與掃描結果頁直接由 app.py 產生。

## Render
Build Command:
`pip install -r requirements.txt`

Start Command:
`uvicorn app:app --host 0.0.0.0 --port $PORT`

需要環境變數：
- DATABASE_URL：由 Render PostgreSQL 提供
- LINE_MODE：simulation / live
- LINE_CHANNEL_ACCESS_TOKEN：live 才需要
- LINE_ADMIN_USER_ID：管理者 LINE User ID
- ADMIN_USER / ADMIN_PASSWORD：後台 Basic Auth

## 管理後台
根網址 `/` 或 `/admin` 會要求管理員登入。
預設測試帳密：
- 使用者：admin
- 密碼：test1234

正式測試前請在 Render Environment Variables 改掉 ADMIN_PASSWORD。

## 自動通知觸發方式
程式每 60 秒在背景檢查一次，管理頁也會在每次載入時檢查一次。
Render Free 如果服務進入休眠，休眠期間不會執行背景檢查；正式環境再改成外部排程或 Render Cron/其他常駐服務。
