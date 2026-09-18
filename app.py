from __future__ import annotations

import io
import os
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any

import requests
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import psycopg
from psycopg.rows import dict_row
import qrcode

LINE_MODE = os.getenv("LINE_MODE", "simulation").lower()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_ADMIN_USER_ID = os.getenv("LINE_ADMIN_USER_ID", "").strip()
LATE_GRACE_MINUTES = int(os.getenv("LATE_GRACE_MINUTES", "10"))
CHECKOUT_GRACE_MINUTES = int(os.getenv("CHECKOUT_GRACE_MINUTES", "15"))
CHECKIN_EARLY_MINUTES = int(os.getenv("CHECKIN_EARLY_MINUTES", "60"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
os.makedirs(TEMPLATE_DIR, exist_ok=True)

# Render 部署時若 templates/ 沒有被一起上傳，啟動時自動補齊。
# 這讓測試版不再依賴 GitHub 是否正確保留 templates 資料夾。
DASHBOARD_TEMPLATE = r"""
<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>出勤測試系統</title>
<style>
body{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f6f7fb;margin:0;color:#1f2937}
.wrap{max-width:1180px;margin:0 auto;padding:22px}.top{display:flex;justify-content:space-between;gap:16px;align-items:center;flex-wrap:wrap}
h1{margin:0 0 4px}.muted{color:#6b7280}.cards{display:flex;gap:12px;flex-wrap:wrap;margin:18px 0}.card{background:#fff;padding:15px 18px;border-radius:12px;box-shadow:0 2px 10px #0000000b;min-width:130px}.num{font-size:28px;font-weight:700}
section{background:#fff;border-radius:14px;padding:18px;margin-top:16px;box-shadow:0 2px 10px #0000000b} table{width:100%;border-collapse:collapse}th,td{padding:10px 8px;border-bottom:1px solid #eee;text-align:left;font-size:14px}th{background:#fafafa}
.badge{padding:4px 8px;border-radius:999px;font-size:12px}.green{background:#dcfce7}.orange{background:#ffedd5}.red{background:#fee2e2}.gray{background:#f3f4f6}
.btn{display:inline-block;border:0;border-radius:8px;padding:8px 12px;cursor:pointer;background:#111827;color:#fff;text-decoration:none}.btn2{background:#e5e7eb;color:#111827}.mini{font-size:12px}
input{padding:7px 8px;border:1px solid #d1d5db;border-radius:7px;width:150px}.qr{width:90px;height:90px;border:1px solid #eee}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.log{max-height:380px;overflow:auto;white-space:pre-wrap}.notice{padding:10px;border-radius:9px;background:#fff7ed}
@media(max-width:800px){.grid{grid-template-columns:1fr}.wrap{padding:12px}th,td{font-size:12px}.qr{width:72px;height:72px}}
</style>
<script>
async function checkMissing(){try{await fetch('/admin/check-missing',{method:'POST'});location.reload()}catch(e){console.log(e)}}
setInterval(checkMissing,60000);
</script>
</head>
<body><div class="wrap">
<div class="top"><div><h1>出勤測試系統</h1><div class="muted">Render 隔離測試站｜LINE：{{ line_mode }}</div></div>
<div><a class="btn btn2" href="/admin/export.csv">匯出 Excel 可開啟的 CSV</a> <button class="btn" onclick="checkMissing()">立即檢查未離班</button></div></div>
<div class="cards">
<div class="card"><div class="muted">今日紀錄</div><div class="num">{{ stats.today }}</div></div>
<div class="card"><div class="muted">遲到</div><div class="num">{{ stats.late }}</div></div>
<div class="card"><div class="muted">尚未離班</div><div class="num">{{ stats.open }}</div></div>
<div class="card"><div class="muted">遲到門檻</div><div class="num">{{ late_grace }}分</div></div>
</div>
<section><h2>測試學生 QR</h2><p class="muted mini">手機掃描 QR 後會直接進入該學生的簽到頁。第一版先用示範資料測試。</p>
<table><tr><th>學生</th><th>編號</th><th>QR</th><th>LINE User ID</th><th></th></tr>
{% for s in students %}<tr><td>{{ s.name }}</td><td>{{ s.student_code }}</td><td><img class="qr" src="/qr/{{ s.student_code }}.png"></td><td><form method="post" action="/admin/student/{{ s.id }}/line"><input name="line_user_id" value="{{ s.line_user_id or '' }}" placeholder="Uxxxx"><button class="btn" type="submit">儲存</button></form></td><td class="mini">掃一次＝到班<br>10分鐘內重掃不離班</td></tr>{% endfor %}
</table></section>
<div class="grid"><section><h2>今日出勤</h2><div style="overflow:auto"><table><tr><th>學生</th><th>課程</th><th>預定</th><th>到班</th><th>離班</th><th>狀態</th><th></th></tr>
{% for r in rows %}<tr><td>{{ r.name }}<br><span class="muted mini">{{ r.student_code }}</span></td><td>{{ r.course_name or '' }}</td><td>{{ r.start_time or '' }}-{{ r.end_time or '' }}</td><td>{{ r.check_in_time.strftime('%H:%M:%S') if r.check_in_time else '-' }}</td><td>{{ r.check_out_time.strftime('%H:%M:%S') if r.check_out_time else '-' }}</td><td>{% if r.check_out_time %}<span class="badge green">完成</span>{% elif r.late_minutes > late_grace %}<span class="badge orange">遲到</span>{% else %}<span class="badge gray">到班</span>{% endif %}</td><td><form method="post" action="/admin/resend/{{ r.id }}"><button class="btn btn2 mini" type="submit">重送 LINE</button></form></td></tr>{% endfor %}</table></div></section>
<section><h2>LINE / 通知紀錄</h2><div class="log">{% for n in notifications %}{{ n.created_at.strftime('%Y-%m-%d %H:%M:%S') }}｜{{ n.notification_type }}｜{{ n.status }}
{{ n.message }}
{% if n.error_message %}錯誤：{{ n.error_message }}
{% endif %}--------------------
{% endfor %}</div></section></div>
</div></body></html>
"""

SCAN_RESULT_TEMPLATE = r"""
<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>簽到結果</title>
<style>body{font-family:system-ui,sans-serif;background:#f6f7fb;margin:0}.box{max-width:520px;margin:48px auto;background:#fff;border-radius:16px;padding:28px;box-shadow:0 4px 20px #0001;text-align:center}.ok{font-size:28px}.sub{color:#6b7280}.btn{display:inline-block;margin-top:22px;background:#111827;color:#fff;padding:10px 16px;border-radius:9px;text-decoration:none}</style></head>
<body><div class="box">
{% if kind == 'check_in' %}<div class="ok">✅ 到班完成</div><h1>{{ student }}</h1><p>{{ course }}</p><p>{{ time[11:16] }}{% if late > 0 %}<br><strong>遲到 {{ late }} 分鐘</strong>{% endif %}</p>
{% elif kind == 'check_out' %}<div class="ok">👋 離班完成</div><h1>{{ student }}</h1><p>{{ course }}</p><p>到班：{{ check_in[11:16] }}<br>離班：{{ time[11:16] }}</p>
{% elif kind == 'duplicate' %}<div class="ok">ℹ️ 已有紀錄</div><h1>{{ student }}</h1><p>{{ message }}</p>
{% else %}<div class="ok">⚠️ {{ message }}</div><h1>{{ student }}</h1>
{% endif %}<a class="btn" href="/">返回管理頁</a></div></body></html>
"""

# 啟動時自動建立模板檔案；就算 GitHub 沒有 templates/ 也能正常運作。
for _name, _content in (("dashboard.html", DASHBOARD_TEMPLATE), ("scan_result.html", SCAN_RESULT_TEMPLATE)):
    _path = os.path.join(TEMPLATE_DIR, _name)
    if not os.path.exists(_path):
        with open(_path, "w", encoding="utf-8") as _f:
            _f.write(_content)

templates = Jinja2Templates(directory=TEMPLATE_DIR)
app = FastAPI(title="Attendance Test MVP")


def now_local() -> datetime:
    # Store Taiwan local time as a naive timestamp because DB columns are TIMESTAMP.
    return datetime.now(ZoneInfo("Asia/Taipei")).replace(microsecond=0, tzinfo=None)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def public_base_url() -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    if RENDER_EXTERNAL_HOSTNAME:
        return f"https://{RENDER_EXTERNAL_HOSTNAME}"
    return "http://localhost:8000"


def db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL 未設定。請在 Render 連接 Postgres。")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def parse_hhmm(value: str, date_str: str) -> datetime:
    return datetime.strptime(f"{date_str} {value}", "%Y-%m-%d %H:%M")


def init_db() -> None:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS students (
                    id BIGSERIAL PRIMARY KEY,
                    student_code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    qr_token TEXT NOT NULL UNIQUE,
                    line_user_id TEXT,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS courses (
                    id BIGSERIAL PRIMARY KEY,
                    student_id BIGINT NOT NULL REFERENCES students(id),
                    course_name TEXT NOT NULL,
                    teacher_name TEXT,
                    weekday INTEGER NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT TRUE
                );
                CREATE TABLE IF NOT EXISTS attendance (
                    id BIGSERIAL PRIMARY KEY,
                    student_id BIGINT NOT NULL REFERENCES students(id),
                    course_id BIGINT REFERENCES courses(id),
                    date DATE NOT NULL,
                    check_in_time TIMESTAMP,
                    check_out_time TIMESTAMP,
                    status TEXT NOT NULL DEFAULT 'checked_in',
                    late_minutes INTEGER NOT NULL DEFAULT 0,
                    late_notified_at TIMESTAMP,
                    missing_checkout_notified_at TIMESTAMP,
                    last_scan_time TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(student_id, course_id, date)
                );
                CREATE TABLE IF NOT EXISTS notification_logs (
                    id BIGSERIAL PRIMARY KEY,
                    attendance_id BIGINT REFERENCES attendance(id),
                    notification_type TEXT NOT NULL,
                    recipient TEXT,
                    message TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    sent_at TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_attendance_date ON attendance(date);
                CREATE INDEX IF NOT EXISTS idx_attendance_open ON attendance(date, check_out_time);
                """
            )
            seed_demo_data(cur)
        conn.commit()


def seed_demo_data(cur) -> None:
    cur.execute("SELECT COUNT(*) AS c FROM students")
    if cur.fetchone()["c"]:
        return
    demo = [
        ("STU-000001", "王小明"),
        ("STU-000002", "林小華"),
        ("STU-000003", "陳小美"),
    ]
    for code, name in demo:
        cur.execute(
            "INSERT INTO students(student_code, name, qr_token) VALUES (%s, %s, %s)",
            (code, name, secrets.token_urlsafe(18)),
        )
    cur.execute("SELECT id FROM students ORDER BY id")
    students = cur.fetchall()
    for idx, row in enumerate(students):
        start = ["18:00", "18:30", "19:00"][idx % 3]
        end = ["19:30", "20:00", "20:30"][idx % 3]
        for weekday in range(7):
            cur.execute(
                """
                INSERT INTO courses(student_id, course_name, teacher_name, weekday, start_time, end_time)
                VALUES (%s,%s,%s,%s,%s,%s)
                """,
                (row["id"], "測試課程", "測試老師", weekday, start, end),
            )


def get_today_course(cur, student_id: int, when: datetime):
    cur.execute(
        """
        SELECT * FROM courses
        WHERE student_id=%s AND weekday=%s AND active=TRUE
        ORDER BY start_time
        LIMIT 1
        """,
        (student_id, when.weekday()),
    )
    return cur.fetchone()


def log_notification(cur, attendance_id: int | None, notification_type: str,
                     recipient: str | None, message: str, mode: str,
                     status: str, error: str | None = None) -> None:
    sent = now_local() if status == "sent" else None
    cur.execute(
        """
        INSERT INTO notification_logs
        (attendance_id, notification_type, recipient, message, mode, status, error_message, sent_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (attendance_id, notification_type, recipient, message, mode, status, error, sent),
    )


def send_line(cur, attendance_id: int | None, recipient: str | None,
              message: str, notification_type: str) -> bool:
    if not recipient:
        log_notification(cur, attendance_id, notification_type, None, message, LINE_MODE, "skipped", "未設定 LINE User ID")
        return False
    if LINE_MODE != "live":
        log_notification(cur, attendance_id, notification_type, recipient, message, "simulation", "sent")
        return True
    if not LINE_CHANNEL_ACCESS_TOKEN:
        log_notification(cur, attendance_id, notification_type, recipient, message, "live", "failed", "LINE_CHANNEL_ACCESS_TOKEN 未設定")
        return False
    try:
        response = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            },
            json={"to": recipient, "messages": [{"type": "text", "text": message}]},
            timeout=10,
        )
        if 200 <= response.status_code < 300:
            log_notification(cur, attendance_id, notification_type, recipient, message, "live", "sent")
            return True
        log_notification(cur, attendance_id, notification_type, recipient, message, "live", "failed",
                         f"HTTP {response.status_code}: {response.text[:500]}")
        return False
    except requests.RequestException as exc:
        log_notification(cur, attendance_id, notification_type, recipient, message, "live", "failed", str(exc))
        return False


def checkin_message(student, course, when: datetime) -> str:
    return f"🟢 到班通知\n{student['name']} 已於 {when:%H:%M} 到班。\n課程：{course['course_name']}\n老師：{course['teacher_name'] or '未設定'}"


def checkout_message(student, course, check_in_time, when: datetime) -> str:
    return f"🔵 離班通知\n{student['name']} 已於 {when:%H:%M} 離開。\n課程：{course['course_name']}\n到班：{check_in_time:%H:%M}\n離班：{when:%H:%M}"


def scan_student(token: str) -> dict[str, Any]:
    when = now_local()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM students WHERE qr_token=%s AND active=TRUE", (token,))
            student = cur.fetchone()
            if not student:
                raise HTTPException(status_code=404, detail="無效 QR Code")
            course = get_today_course(cur, student["id"], when)
            if not course:
                msg = f"⚠️ 無課程簽到\n學生：{student['name']}\n時間：{when:%H:%M}"
                send_line(cur, None, LINE_ADMIN_USER_ID, msg, "invalid_schedule")
                conn.commit()
                return {"kind": "error", "student": student["name"], "message": "今天沒有安排課程，已記錄並通知管理者。"}

            date_str = when.date()
            cur.execute(
                "SELECT * FROM attendance WHERE student_id=%s AND course_id=%s AND date=%s",
                (student["id"], course["id"], date_str),
            )
            attendance = cur.fetchone()
            scheduled_start = parse_hhmm(course["start_time"], date_str.strftime("%Y-%m-%d"))

            if attendance is None:
                if when < scheduled_start - timedelta(minutes=CHECKIN_EARLY_MINUTES):
                    conn.commit()
                    return {"kind": "too_early", "student": student["name"], "course": course["course_name"], "time": iso(when), "message": f"距離課程開始時間過早，請於課前 {CHECKIN_EARLY_MINUTES} 分鐘內再掃描。"}
                late = max(0, int((when - scheduled_start).total_seconds() // 60))
                status = "late" if late > LATE_GRACE_MINUTES else "checked_in"
                cur.execute(
                    """
                    INSERT INTO attendance
                    (student_id,course_id,date,check_in_time,status,late_minutes,last_scan_time,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                    """,
                    (student["id"], course["id"], date_str, when, status, late, when, when),
                )
                attendance_id = cur.fetchone()["id"]
                send_line(cur, attendance_id, student["line_user_id"], checkin_message(student, course, when), "check_in")
                if late > LATE_GRACE_MINUTES:
                    admin_msg = (
                        f"⚠️ 學生遲到\n\n{student['name']}\n課程：{course['course_name']}\n"
                        f"原定：{course['start_time']}\n實際到班：{when:%H:%M}\n遲到：{late} 分鐘"
                    )
                    sent = send_line(cur, attendance_id, LINE_ADMIN_USER_ID, admin_msg, "late")
                    if sent:
                        cur.execute("UPDATE attendance SET late_notified_at=%s WHERE id=%s", (when, attendance_id))
                conn.commit()
                return {"kind": "check_in", "student": student["name"], "course": course["course_name"], "time": iso(when), "late": late}

            if attendance["check_out_time"]:
                conn.commit()
                return {"kind": "duplicate", "student": student["name"], "message": f"今天已完成到班與離班，離班時間：{attendance['check_out_time']:%H:%M}"}

            if when < scheduled_start:
                conn.commit()
                return {"kind": "duplicate", "student": student["name"], "message": f"已於 {attendance['check_in_time']:%H:%M} 到班，但目前尚未到課程開始時間，不會視為離班。"}

            if attendance["check_in_time"] and (when - attendance["check_in_time"]).total_seconds() < 600:
                conn.commit()
                return {"kind": "duplicate", "student": student["name"], "message": f"已於 {attendance['check_in_time']:%H:%M} 到班，10 分鐘內重複掃描不會視為離班。"}

            cur.execute(
                "UPDATE attendance SET check_out_time=%s,status='completed',last_scan_time=%s,updated_at=%s WHERE id=%s",
                (when, when, when, attendance["id"]),
            )
            send_line(cur, attendance["id"], student["line_user_id"], checkout_message(student, course, attendance["check_in_time"], when), "check_out")
            conn.commit()
            return {"kind": "check_out", "student": student["name"], "course": course["course_name"], "time": iso(when), "check_in": iso(attendance["check_in_time"])}


def check_missing_checkout() -> int:
    now = now_local()
    date_str = now.date()
    changed = 0
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.*, s.name, c.course_name, c.end_time
                FROM attendance a
                JOIN students s ON s.id=a.student_id
                JOIN courses c ON c.id=a.course_id
                WHERE a.date=%s AND a.check_in_time IS NOT NULL
                  AND a.check_out_time IS NULL AND a.missing_checkout_notified_at IS NULL
                """,
                (date_str,),
            )
            rows = cur.fetchall()
            for row in rows:
                end_dt = parse_hhmm(row["end_time"], date_str.strftime("%Y-%m-%d"))
                if now >= end_dt + timedelta(minutes=CHECKOUT_GRACE_MINUTES):
                    msg = (
                        f"🔴 未完成離班簽到\n\n{row['name']}\n課程：{row['course_name']}\n"
                        f"到班：{row['check_in_time']:%H:%M}\n原定下課：{row['end_time']}\n截至 {now:%H:%M} 尚未完成離班簽到。"
                    )
                    sent = send_line(cur, row["id"], LINE_ADMIN_USER_ID, msg, "missing_checkout")
                    if sent:
                        cur.execute("UPDATE attendance SET missing_checkout_notified_at=%s, updated_at=%s WHERE id=%s", (now, now, row["id"]))
                    changed += 1
            conn.commit()
    return changed


def render_dashboard(request: Request):
    # Also run checker opportunistically so the dashboard can trigger late notifications.
    try:
        check_missing_checkout()
    except Exception:
        pass
    today = now_local().date()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.*, s.student_code, s.name, c.course_name, c.teacher_name, c.start_time, c.end_time
                FROM attendance a
                JOIN students s ON s.id=a.student_id
                LEFT JOIN courses c ON c.id=a.course_id
                WHERE a.date=%s
                ORDER BY COALESCE(a.check_in_time, a.created_at) DESC
                """,
                (today,),
            )
            rows = cur.fetchall()
            cur.execute("SELECT * FROM students WHERE active=TRUE ORDER BY id")
            students = cur.fetchall()
            cur.execute("SELECT * FROM notification_logs ORDER BY id DESC LIMIT 50")
            notifications = cur.fetchall()
    stats = {
        "today": len(rows),
        "late": sum(1 for r in rows if r["late_minutes"] > LATE_GRACE_MINUTES),
        "open": sum(1 for r in rows if r["check_in_time"] and not r["check_out_time"]),
    }
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "rows": rows,
            "students": students,
            "notifications": notifications,
            "stats": stats,
            "line_mode": LINE_MODE,
            "late_grace": LATE_GRACE_MINUTES,
            "checkout_grace": CHECKOUT_GRACE_MINUTES,
            "public_base_url": public_base_url(),
        },
    )


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return render_dashboard(request)


@app.head("/")
def dashboard_head():
    return None


@app.get("/scan/{token}", response_class=HTMLResponse)
def scan(token: str, request: Request):
    result = scan_student(token)
    return templates.TemplateResponse(
        request=request,
        name="scan_result.html",
        context=result,
    )


@app.get("/qr/{student_code}.png")
def qr(student_code: str):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT qr_token FROM students WHERE student_code=%s AND active=TRUE", (student_code,))
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, "找不到學生")
    img = qrcode.make(f"{public_base_url()}/scan/{row['qr_token']}")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.post("/admin/resend/{attendance_id}")
def resend(attendance_id: int):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.*, s.name, s.line_user_id, c.course_name
                FROM attendance a JOIN students s ON s.id=a.student_id
                LEFT JOIN courses c ON c.id=a.course_id
                WHERE a.id=%s
                """,
                (attendance_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "找不到紀錄")
            if row["check_out_time"]:
                msg = f"🔵 離班通知\n{row['name']} 已於 {row['check_out_time']:%H:%M} 離開。\n課程：{row['course_name']}"
                send_line(cur, attendance_id, row["line_user_id"], msg, "manual_check_out")
            else:
                msg = f"🟢 到班通知\n{row['name']} 已於 {row['check_in_time']:%H:%M} 到班。\n課程：{row['course_name']}"
                send_line(cur, attendance_id, row["line_user_id"], msg, "manual_check_in")
            conn.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/admin/student/{student_id}/line")
def update_student_line(student_id: int, line_user_id: str = Form("")):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE students SET line_user_id=%s WHERE id=%s", (line_user_id.strip() or None, student_id))
            if cur.rowcount == 0:
                raise HTTPException(404, "找不到學生")
            conn.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/admin/check-missing")
def admin_check_missing():
    count = check_missing_checkout()
    return JSONResponse({"ok": True, "notified": count})


@app.get("/admin/export.csv")
def export_csv():
    # Excel can open this UTF-8 BOM CSV directly. Kept separate from the user's existing workbook.
    import csv
    import io as _io
    buf = _io.StringIO()
    buf.write("\\ufeff")
    writer = csv.writer(buf)
    writer.writerow(["紀錄ID", "日期", "學生編號", "學生姓名", "課程", "老師", "預定開始", "預定結束", "到班", "離班", "狀態", "遲到分鐘"])
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.id,a.date,s.student_code,s.name,c.course_name,c.teacher_name,c.start_time,c.end_time,
                       a.check_in_time,a.check_out_time,a.status,a.late_minutes
                FROM attendance a JOIN students s ON s.id=a.student_id
                LEFT JOIN courses c ON c.id=a.course_id
                ORDER BY a.date DESC,a.id DESC
                """
            )
            for r in cur.fetchall():
                writer.writerow([
                    r["id"], r["date"], r["student_code"], r["name"], r["course_name"], r["teacher_name"],
                    r["start_time"], r["end_time"], r["check_in_time"], r["check_out_time"], r["status"], r["late_minutes"]
                ])
    data = buf.getvalue().encode("utf-8-sig")
    return StreamingResponse(io.BytesIO(data), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=attendance_test_export.csv"})


@app.get("/health")
def health():
    return JSONResponse({"ok": True, "line_mode": LINE_MODE, "database": "postgres"})
