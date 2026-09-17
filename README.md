# Attendance Test MVP — Render 隔離測試版

這一份與原本系統完全分開，專門測試「手機 QR → 到班/離班 → LINE → 出勤紀錄」。不需要在本機安裝 Python；部署到 Render 即可。

## 架構

- Render Web Service：簽到頁、管理頁、QR、LINE API
- Render Postgres：測試期間儲存學生與出勤紀錄
- Excel：使用 `/attendance_test_data.xlsx` 作為獨立測試資料/紀錄模板；網站另提供 CSV 匯出，可直接用 Excel 開啟

## Render 部署

1. 建一個新的 GitHub repository，將本資料夾內容上傳。
2. Render Dashboard → New → Blueprint。
3. 選擇這個 GitHub repo，Render 會依 `render.yaml` 建立一個獨立 Web Service 與一個獨立 Postgres。
4. 第一次建立 Blueprint 時填入 LINE_CHANNEL_ACCESS_TOKEN 與 LINE_ADMIN_USER_ID；測試初期可先留空，LINE_MODE 維持 simulation。
5. Deploy 完成後開啟 Render 提供的 `onrender.com` 網址。

Render 官方目前支援 Python Web Service 與 Blueprint，Web Service 需監聽 Render 的 `PORT`；Blueprint 可在同一個 `render.yaml` 建立 Web Service 與 Postgres，並用 `fromDatabase` 傳入連線字串。

## 測試方式

1. 進入管理頁，確認 3 個示範學生。
2. 用手機相機掃王小明 QR。
3. 第一次掃描＝到班，系統建立紀錄。
4. 10 分鐘內再次掃描＝防誤刷，不會被判成離班。
5. 超過 10 分鐘再掃＝離班，系統建立離班紀錄。
6. 若到班超過預定時間 10 分鐘，會產生遲到通知。
7. 課程結束超過 15 分鐘仍沒有離班時，管理頁每分鐘會檢查一次並通知管理者。

## LINE

初期維持：

`LINE_MODE=simulation`

這只會寫入通知紀錄，不會真的發 LINE。

確認掃描邏輯穩定後，改成：

`LINE_MODE=live`

再填：

- `LINE_CHANNEL_ACCESS_TOKEN`
- `LINE_ADMIN_USER_ID`

學生自己的 `line_user_id` 可在管理頁填入。

## Excel

`attendance_test_data.xlsx` 是獨立的測試用工作簿，不會碰原本的 Excel。網站的「匯出 CSV」可下載目前出勤，Excel 可以直接開啟。

注意：Render Free Postgres 適合短期測試；目前 Free Postgres 會在建立後 30 天到期，而且沒有備份。正式上線時應改用付費 Postgres 或其他持久化資料庫。Render Free Web Service 也會在一段時間無流量後 spin down，因此本測試站不應當正式生產系統。
