# 出勤系統 V0.4.1｜LINE／學生／教室設備測試版

## V0.4.0 教室設備安全限制
- 學生 QR 只代表「學生身分」，不包含教室設備權限。
- `/scan/{token}` 現在要求瀏覽器必須先完成「教室設備配對」。
- 管理員可在 `/admin/devices` 建立教室設備配對連結；配對連結 30 分鐘有效，成功配對後立即失效。
- 配對會在該手機／電腦瀏覽器建立授權 Cookie；之後只能由同一瀏覽器掃學生 QR 才會被接受。
- 若清除 Cookie、改用其他瀏覽器或換設備，需要重新配對。
- 因此學生把 QR 圖片傳給外部人士，對方沒有已授權的教室設備 Cookie 時，直接開啟 `/scan/...` 會被拒絕。
- 測試學生已加入：`STU-000004｜采璇`，每日示範課程 `19:00-20:30`。


本版本延續 V0.2 的出勤功能，將家長 LINE 綁定改為「管理員產生一次性 LIFF 連結 → 家長在 LINE 開啟 → LINE 身分驗證 → 確認學生 → 建立綁定」。

## 綁定規則
- 每個家長綁定連結 1 小時有效；有效期間可供多位家長使用，並不會因第一位家長成功綁定就立即失效。
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

## V0.4.0 測試方式
1. 管理員開啟 `/admin/devices`。
2. 建立「教室手機」設備，取得 30 分鐘配對連結。
3. 在教室要用來掃卡的手機瀏覽器開啟配對連結。
4. 配對後，在同一手機／同一瀏覽器使用內建相機掃「采璇」的學生 QR。
5. 若換另一支未配對手機直接掃相同 QR，應顯示「未授權教室設備」並拒絕簽到。
6. 若要撤銷某台設備，管理員在 `/admin/devices` 停用即可。

> 注意：此安全機制是「設備配對」而不是 GPS 地理圍籬。正式使用時建議把掃描設備固定放在教室／櫃台。

## V0.4.1 後台 QR 改進
- LINE 綁定後台的「重新產生連結」現在會在連結旁直接顯示對應 QR Code，家長可直接掃描。
- 設備配對後台的「建立設備配對」也會直接顯示配對 QR Code。
- 設備配對 code 不需要填入 Render Environment Variables；它只是臨時配對連結中的 `code` 參數，掃描後配對成功即失效。
- 設備配對連結目前仍為 30 分鐘有效。
