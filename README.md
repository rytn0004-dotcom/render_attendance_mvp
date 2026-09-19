# 出勤系統 V0.3.0｜LINE 與學生綁定測試版

本版本延續 V0.2 的出勤功能，將家長 LINE 綁定改為「管理員產生一次性 LIFF 連結 → 家長在 LINE 開啟 → LINE 身分驗證 → 確認學生 → 建立綁定」。

## 綁定規則
- 每個綁定連結 30 分鐘有效、使用一次後失效。
- 同一學生可綁定多個 LINE 使用者。
- 同一個 LINE 使用者可綁定多位學生。
- 家長端不提供「解除綁定」；解除綁定只由管理員後台操作。
- QR 簽到與 LINE 身分完全分離：學生只需要持有自己的 QR 卡，不需要手機或 LINE。

## LINE Developers 設定
1. Messaging API Channel 與 LINE Login Channel 請建立在同一個 Provider。
2. 在 LINE Login Channel 建立 LIFF App，Endpoint URL 填：`https://你的Render網域/liff/bind`。
3. LIFF Scope 至少勾選 `openid`，才能取得 ID Token。
4. Render Environment Variables：
   - `LINE_MODE=live`
   - `LINE_CHANNEL_ACCESS_TOKEN`
   - `LINE_CHANNEL_SECRET`
   - `LINE_ADMIN_USER_ID`
   - `LINE_LOGIN_CHANNEL_ID`
   - `LIFF_ID`
   - `ADMIN_PASSWORD`
5. Messaging API Webhook URL：`https://你的Render網域/webhook/line`，並開啟 Webhook。

## 測試流程
1. 管理員進 `/admin`。
2. 在「學生 QR / 家長 LINE」對指定學生按「產生家長綁定連結」。
3. 把產生的 LIFF 連結傳給自己的 LINE 測試帳號。
4. 在 LINE 開啟連結，看到學生姓名後按「確認綁定」。
5. 回到 `/admin/line`，應看到新的綁定紀錄。
6. 在後台按「測試 LINE」，確認 Push 到正確的 LINE。
7. 最後再測 QR 到班 / 離班，確認出勤通知送到剛綁定的 LINE。

## 安全
- 伺服器不信任前端直接傳來的 LINE User ID，而是把 LIFF `id_token` 送到 LINE 的 Verify ID token endpoint 取得 `sub`（LINE User ID）。
- Webhook 仍驗證 `x-line-signature`。
- 綁定 token 只在資料庫儲存 SHA-256 雜湊值。


## V0.3.1 綁定規則
- 系統自動為每位學生建立 1 小時有效的家長綁定連結。
- 同一條連結在有效期內可供多位家長使用。
- 同一個 LINE 使用者可透過不同學生連結綁定多位孩子。
- 家長端不提供解除綁定；解除／新增綁定由管理員處理。
- 管理員可從「LINE 綁定 / 測試」下載 CSV，用 Excel 修改後另存 UTF-8 CSV，再上傳回 Render。
- LINE User ID 由 LINE 平台產生，伺服器透過 LIFF ID Token 驗證後保存。

## V0.3.2 通知範本
- 到班預設：`{greeting}，{student_name}到教室了😊`
- 離開教室預設：`{greeting}，{student_name}離開教室了，{thanks}😊`
- `{greeting}` 會依家長關係自動變成「媽媽您好／爸爸您好／家長您好」等；`{thanks}` 會依關係變成「感謝媽媽／感謝爸爸／感謝您」。
- 管理員可在 `/admin/templates` 修改預設範本，也可建立「個別學生範本」。
- 個別範本支援「永久個別」、「一次性（成功送出後自動恢復預設）」與「期限內」。
- Excel 可透過「通知範本 CSV」下載／修改／匯入，學生編號留白代表修改預設範本。
- 建議範本內容例：`{greeting}，{student_name}說日記回去想，先離開教室了，{thanks}😊`；設定成「一次性」後，成功送出這次離開教室通知即自動恢復預設離開教室訊息。
