# Attendance Test MVP - Render

這版特別修正 Render 找不到 templates/dashboard.html 的問題。
HTML 模板已內嵌在 app.py，啟動時會自動建立 templates/，因此 GitHub 不需要上傳 templates 資料夾。

Render：
Build Command: pip install -r requirements.txt
Start Command: uvicorn app:app --host 0.0.0.0 --port $PORT

先保持 LINE_MODE=simulation。
