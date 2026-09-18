from __future__ import annotations

import asyncio
import csv
import io
import os
import secrets
from datetime import date, datetime, timedelta
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

import qrcode
import requests
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Form, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

LINE_MODE = os.getenv("LINE_MODE", "simulation").lower()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_ADMIN_USER_ID = os.getenv("LINE_ADMIN_USER_ID", "").strip()
DEFAULT_LATE_GRACE_MINUTES = int(os.getenv("LATE_GRACE_MINUTES", "10"))
DEFAULT_CHECKOUT_GRACE_MINUTES = int(os.getenv("CHECKOUT_GRACE_MINUTES", "15"))
CHECKIN_EARLY_MINUTES = int(os.getenv("CHECKIN_EARLY_MINUTES", "60"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "test1234")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

app = FastAPI(title="Attendance Test MVP v0.2")
security = HTTPBasic()

WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def now_local() -> datetime:
    return datetime.now(ZoneInfo("Asia/Taipei")).replace(microsecond=0, tzinfo=None)


def iso(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


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


def admin_auth(credentials: HTTPBasicCredentials = Depends(security)):
    ok_user = secrets.compare_digest(credentials.username, ADMIN_USER)
    ok_pass = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (ok_user and ok_pass):
        from fastapi.responses import Response
        raise HTTPException(
            status_code=401,
            detail="需要管理員登入",
            headers={"WWW-Authenticate": "Basic realm=Attendance Admin"},
        )
    return credentials.username


def parse_hhmm(value: str, date_value: date) -> datetime:
    return datetime.strptime(f"{date_value:%Y-%m-%d} {value}", "%Y-%m-%d %H:%M")


def parse_form_datetime(value: str | None, date_value: date) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(f"{date_value:%Y-%m-%d} {value}", "%Y-%m-%d %H:%M")


def effective_schedule(cur, course: dict[str, Any], work_date: date) -> dict[str, Any] | None:
    cur.execute(
        "SELECT * FROM schedule_overrides WHERE course_id=%s AND work_date=%s",
        (course["id"], work_date),
    )
    ov = cur.fetchone()
    if ov and ov["cancelled"]:
        return None
    result = dict(course)
    result["effective_start"] = ov["start_time"] if ov and ov["start_time"] else course["start_time"]
    result["effective_end"] = ov["end_time"] if ov and ov["end_time"] else course["end_time"]
    result["override_id"] = ov["id"] if ov else None
    result["override_note"] = ov["note"] if ov else ""
    result["late_grace_minutes"] = course["late_grace_minutes"]
    result["checkout_grace_minutes"] = course["checkout_grace_minutes"]
    return result


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
                    late_grace_minutes INTEGER NOT NULL DEFAULT 10,
                    checkout_grace_minutes INTEGER NOT NULL DEFAULT 15,
                    active BOOLEAN NOT NULL DEFAULT TRUE
                );
                CREATE TABLE IF NOT EXISTS schedule_overrides (
                    id BIGSERIAL PRIMARY KEY,
                    course_id BIGINT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
                    work_date DATE NOT NULL,
                    start_time TEXT,
                    end_time TEXT,
                    cancelled BOOLEAN NOT NULL DEFAULT FALSE,
                    note TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(course_id, work_date)
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
                    absent_notified_at TIMESTAMP,
                    missing_checkout_notified_at TIMESTAMP,
                    last_scan_time TIMESTAMP,
                    manual_note TEXT,
                    adjusted_at TIMESTAMP,
                    adjusted_by TEXT,
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
            # Migration for databases created by earlier V0.1 builds.
            cur.execute("ALTER TABLE courses ADD COLUMN IF NOT EXISTS late_grace_minutes INTEGER NOT NULL DEFAULT 10")
            cur.execute("ALTER TABLE courses ADD COLUMN IF NOT EXISTS checkout_grace_minutes INTEGER NOT NULL DEFAULT 15")
            cur.execute("ALTER TABLE attendance ADD COLUMN IF NOT EXISTS absent_notified_at TIMESTAMP")
            cur.execute("ALTER TABLE attendance ADD COLUMN IF NOT EXISTS manual_note TEXT")
            cur.execute("ALTER TABLE attendance ADD COLUMN IF NOT EXISTS adjusted_at TIMESTAMP")
            cur.execute("ALTER TABLE attendance ADD COLUMN IF NOT EXISTS adjusted_by TEXT")
            cur.execute(
                "UPDATE courses SET late_grace_minutes=%s WHERE late_grace_minutes IS NULL",
                (DEFAULT_LATE_GRACE_MINUTES,),
            )
            cur.execute(
                "UPDATE courses SET checkout_grace_minutes=%s WHERE checkout_grace_minutes IS NULL",
                (DEFAULT_CHECKOUT_GRACE_MINUTES,),
            )
            seed_demo_data(cur)
        conn.commit()


def seed_demo_data(cur) -> None:
    cur.execute("SELECT COUNT(*) AS c FROM students")
    if cur.fetchone()["c"]:
        return
    demo = [("STU-000001", "王小明"), ("STU-000002", "林小華"), ("STU-000003", "陳小美")]
    for code, name in demo:
        cur.execute(
            "INSERT INTO students(student_code, name, qr_token) VALUES (%s,%s,%s)",
            (code, name, secrets.token_urlsafe(18)),
        )
    cur.execute("SELECT id FROM students ORDER BY id")
    students = cur.fetchall()
    times = [("18:00", "19:30"), ("18:30", "20:00"), ("19:00", "20:30")]
    for idx, row in enumerate(students):
        start, end = times[idx % len(times)]
        # 每天都 seed，讓測試任何一天都能直接掃。
        for weekday in range(7):
            cur.execute(
                """
                INSERT INTO courses(student_id, course_name, teacher_name, weekday, start_time, end_time, late_grace_minutes, checkout_grace_minutes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (row["id"], "測試課程", "測試老師", weekday, start, end, DEFAULT_LATE_GRACE_MINUTES, DEFAULT_CHECKOUT_GRACE_MINUTES),
            )


def get_student(cur, token: str):
    cur.execute("SELECT * FROM students WHERE qr_token=%s AND active=TRUE", (token,))
    return cur.fetchone()


def get_courses_today(cur, student_id: int, work_date: date):
    cur.execute(
        "SELECT * FROM courses WHERE student_id=%s AND weekday=%s AND active=TRUE ORDER BY start_time",
        (student_id, work_date.weekday()),
    )
    raw = cur.fetchall()
    result = []
    for course in raw:
        eff = effective_schedule(cur, course, work_date)
        if eff:
            result.append(eff)
    return result


def choose_course(courses: list[dict[str, Any]], when: datetime, existing_attendance: dict[int, dict[str, Any]] | None = None):
    if not courses:
        return None
    # 先優先已經有未離班紀錄的課程。
    if existing_attendance:
        for c in courses:
            a = existing_attendance.get(c["id"])
            if a and not a["check_out_time"]:
                return c
    scored = []
    for c in courses:
        st = parse_hhmm(c["effective_start"], when.date())
        en = parse_hhmm(c["effective_end"], when.date())
        early = st - timedelta(minutes=CHECKIN_EARLY_MINUTES)
        late_end = en + timedelta(minutes=max(c["checkout_grace_minutes"], 60))
        inside = early <= when <= late_end
        distance = 0 if inside else min(abs((when - st).total_seconds()), abs((when - en).total_seconds()))
        scored.append((0 if inside else 1, distance, st, c))
    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    return scored[0][3]


def log_notification(cur, attendance_id: int | None, notification_type: str,
                     recipient: str | None, message: str, mode: str,
                     status: str, error: str | None = None) -> None:
    cur.execute(
        """
        INSERT INTO notification_logs
        (attendance_id, notification_type, recipient, message, mode, status, error_message, sent_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (attendance_id, notification_type, recipient, message, mode, status, error, now_local() if status in {"sent", "simulated"} else None),
    )


def send_line(cur, attendance_id: int | None, recipient: str | None,
              message: str, notification_type: str) -> bool:
    # Simulation 直接記錄成功，不要求真的有 recipient，方便大量測試。
    if LINE_MODE != "live":
        log_notification(cur, attendance_id, notification_type, recipient, message, "simulation", "simulated")
        return True
    if not recipient:
        log_notification(cur, attendance_id, notification_type, None, message, "live", "failed", "未設定 LINE User ID")
        return False
    if not LINE_CHANNEL_ACCESS_TOKEN:
        log_notification(cur, attendance_id, notification_type, recipient, message, "live", "failed", "LINE_CHANNEL_ACCESS_TOKEN 未設定")
        return False
    try:
        response = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
            json={"to": recipient, "messages": [{"type": "text", "text": message}]},
            timeout=10,
        )
        if 200 <= response.status_code < 300:
            log_notification(cur, attendance_id, notification_type, recipient, message, "live", "sent")
            return True
        log_notification(cur, attendance_id, notification_type, recipient, message, "live", "failed", f"HTTP {response.status_code}: {response.text[:500]}")
        return False
    except requests.RequestException as exc:
        log_notification(cur, attendance_id, notification_type, recipient, message, "live", "failed", str(exc))
        return False


def checkin_message(student, course, when: datetime) -> str:
    return f"🟢 到班通知\n{student['name']} 已於 {when:%H:%M} 到班。\n課程：{course['course_name']}\n老師：{course['teacher_name'] or '未設定'}"


def checkout_message(student, course, check_in_time, when: datetime) -> str:
    return f"🔵 離班通知\n{student['name']} 已於 {when:%H:%M} 離開。\n課程：{course['course_name']}\n到班：{check_in_time:%H:%M}\n離班：{when:%H:%M}"


def late_admin_message(student_name: str, course_name: str, scheduled_start: str, actual: datetime, late: int) -> str:
    return f"⚠️ 學生遲到\n\n{student_name}\n課程：{course_name}\n原定：{scheduled_start}\n實際到班：{actual:%H:%M}\n遲到：{late} 分鐘"


def absent_admin_message(student_name: str, course_name: str, scheduled_start: str, now: datetime) -> str:
    return f"🔴 學生未到班\n\n{student_name}\n課程：{course_name}\n原定：{scheduled_start}\n截至 {now:%H:%M} 尚未完成到班簽到。"


def missing_checkout_message(row: dict[str, Any], now: datetime) -> str:
    return f"🔴 未完成離班簽到\n\n{row['name']}\n課程：{row['course_name']}\n到班：{row['check_in_time']:%H:%M}\n原定下課：{row['effective_end']}\n截至 {now:%H:%M} 尚未完成離班簽到。"


def scan_student(token: str) -> dict[str, Any]:
    when = now_local()
    work_date = when.date()
    with db_conn() as conn:
        with conn.cursor() as cur:
            student = get_student(cur, token)
            if not student:
                raise HTTPException(status_code=404, detail="無效 QR Code")
            courses = get_courses_today(cur, student["id"], work_date)
            if not courses:
                msg = f"⚠️ 無課程簽到\n學生：{student['name']}\n時間：{when:%H:%M}"
                send_line(cur, None, LINE_ADMIN_USER_ID, msg, "invalid_schedule")
                conn.commit()
                return {"kind": "error", "student": student["name"], "message": "今天沒有安排課程，已記錄並通知管理者。"}

            existing = {}
            cur.execute("SELECT * FROM attendance WHERE student_id=%s AND date=%s", (student["id"], work_date))
            for a in cur.fetchall():
                existing[a["course_id"]] = a
            course = choose_course(courses, when, existing)
            if not course:
                return {"kind": "error", "student": student["name"], "message": "今天沒有可用課程。"}

            cur.execute(
                "SELECT * FROM attendance WHERE student_id=%s AND course_id=%s AND date=%s",
                (student["id"], course["id"], work_date),
            )
            attendance = cur.fetchone()
            scheduled_start = parse_hhmm(course["effective_start"], work_date)

            if attendance is None:
                if when < scheduled_start - timedelta(minutes=CHECKIN_EARLY_MINUTES):
                    conn.commit()
                    return {"kind": "too_early", "student": student["name"], "course": course["course_name"], "time": iso(when), "message": f"距離課程開始時間過早，請於課前 {CHECKIN_EARLY_MINUTES} 分鐘內再掃描。"}
                late = max(0, int((when - scheduled_start).total_seconds() // 60))
                status = "late" if late > course["late_grace_minutes"] else "checked_in"
                cur.execute(
                    """
                    INSERT INTO attendance
                    (student_id,course_id,date,check_in_time,status,late_minutes,last_scan_time,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
                    """,
                    (student["id"], course["id"], work_date, when, status, late, when, when),
                )
                attendance_id = cur.fetchone()["id"]
            else:
                attendance_id = attendance["id"]
                if attendance["check_out_time"]:
                    conn.commit()
                    return {"kind": "duplicate", "student": student["name"], "message": f"今天已完成到班與離班，離班時間：{attendance['check_out_time']:%H:%M}"}
                if attendance["check_in_time"] is None:
                    late = max(0, int((when - scheduled_start).total_seconds() // 60))
                    status = "late" if late > course["late_grace_minutes"] else "checked_in"
                    cur.execute(
                        """
                        UPDATE attendance SET check_in_time=%s,status=%s,late_minutes=%s,last_scan_time=%s,updated_at=%s
                        WHERE id=%s
                        """,
                        (when, status, late, when, when, attendance_id),
                    )
                else:
                    if when < scheduled_start:
                        conn.commit()
                        return {"kind": "duplicate", "student": student["name"], "message": f"已於 {attendance['check_in_time']:%H:%M} 到班，但目前尚未到課程開始時間，不會視為離班。"}
                    if (when - attendance["check_in_time"]).total_seconds() < 600:
                        conn.commit()
                        return {"kind": "duplicate", "student": student["name"], "message": f"已於 {attendance['check_in_time']:%H:%M} 到班，10 分鐘內重複掃描不會視為離班。"}
                    cur.execute(
                        "UPDATE attendance SET check_out_time=%s,status='completed',last_scan_time=%s,updated_at=%s WHERE id=%s",
                        (when, when, when, attendance_id),
                    )
                    send_line(cur, attendance_id, student["line_user_id"], checkout_message(student, course, attendance["check_in_time"], when), "check_out")
                    conn.commit()
                    return {"kind": "check_out", "student": student["name"], "course": course["course_name"], "time": iso(when), "check_in": iso(attendance["check_in_time"])}

            # 到班通知（含原本先未到、後來補打卡的情況）
            cur.execute("SELECT * FROM attendance WHERE id=%s", (attendance_id,))
            final_a = cur.fetchone()
            send_line(cur, attendance_id, student["line_user_id"], checkin_message(student, course, final_a["check_in_time"]), "check_in")
            if final_a["late_minutes"] > course["late_grace_minutes"] and not final_a["late_notified_at"]:
                admin_msg = late_admin_message(student["name"], course["course_name"], course["effective_start"], final_a["check_in_time"], final_a["late_minutes"])
                sent = send_line(cur, attendance_id, LINE_ADMIN_USER_ID, admin_msg, "late")
                if sent:
                    cur.execute("UPDATE attendance SET late_notified_at=%s WHERE id=%s", (when, attendance_id))
            # 補到班後，若先前已被判定未到，保留紀錄但不再重複發未到。
            cur.execute("UPDATE attendance SET absent_notified_at=absent_notified_at WHERE id=%s", (attendance_id,))
            conn.commit()
            return {"kind": "check_in", "student": student["name"], "course": course["course_name"], "time": iso(final_a["check_in_time"]), "late": final_a["late_minutes"]}


def check_scheduled_absences() -> int:
    now = now_local()
    work_date = now.date()
    changed = 0
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.*, s.name, s.active AS student_active
                FROM courses c JOIN students s ON s.id=c.student_id
                WHERE c.weekday=%s AND c.active=TRUE AND s.active=TRUE
                ORDER BY c.start_time
                """,
                (work_date.weekday(),),
            )
            courses = cur.fetchall()
            for course in courses:
                eff = effective_schedule(cur, course, work_date)
                if not eff:
                    continue
                start_dt = parse_hhmm(eff["effective_start"], work_date)
                notify_at = start_dt + timedelta(minutes=eff["late_grace_minutes"])
                if now < notify_at:
                    continue
                cur.execute(
                    "SELECT * FROM attendance WHERE student_id=%s AND course_id=%s AND date=%s",
                    (course["student_id"], course["id"], work_date),
                )
                a = cur.fetchone()
                if a and a["check_in_time"]:
                    continue
                if not a:
                    cur.execute(
                        """
                        INSERT INTO attendance (student_id,course_id,date,status,updated_at)
                        VALUES (%s,%s,%s,'absent',%s) RETURNING id
                        """,
                        (course["student_id"], course["id"], work_date, now),
                    )
                    aid = cur.fetchone()["id"]
                else:
                    aid = a["id"]
                    if a["absent_notified_at"]:
                        continue
                msg = absent_admin_message(course["name"], course["course_name"], eff["effective_start"], now)
                sent = send_line(cur, aid, LINE_ADMIN_USER_ID, msg, "absent")
                if sent:
                    cur.execute("UPDATE attendance SET absent_notified_at=%s,status='absent',updated_at=%s WHERE id=%s", (now, now, aid))
                changed += 1
            conn.commit()
    return changed


def check_missing_checkout() -> int:
    now = now_local()
    work_date = now.date()
    changed = 0
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.*, s.name, c.course_name, c.start_time, c.end_time, c.checkout_grace_minutes
                FROM attendance a JOIN students s ON s.id=a.student_id JOIN courses c ON c.id=a.course_id
                WHERE a.date=%s AND a.check_in_time IS NOT NULL AND a.check_out_time IS NULL
                  AND a.missing_checkout_notified_at IS NULL
                """,
                (work_date,),
            )
            rows = cur.fetchall()
            for row in rows:
                cur.execute("SELECT * FROM courses WHERE id=%s", (row["course_id"],))
                course = cur.fetchone()
                if not course:
                    continue
                eff = effective_schedule(cur, course, work_date)
                if not eff:
                    continue
                end_dt = parse_hhmm(eff["effective_end"], work_date)
                if now >= end_dt + timedelta(minutes=eff["checkout_grace_minutes"]):
                    row = dict(row)
                    row["effective_end"] = eff["effective_end"]
                    msg = missing_checkout_message(row, now)
                    sent = send_line(cur, row["id"], LINE_ADMIN_USER_ID, msg, "missing_checkout")
                    if sent:
                        cur.execute("UPDATE attendance SET missing_checkout_notified_at=%s, updated_at=%s WHERE id=%s", (now, now, row["id"]))
                    changed += 1
            conn.commit()
    return changed


def run_all_checks() -> dict[str, int]:
    result = {"absent": 0, "missing_checkout": 0}
    try:
        result["absent"] = check_scheduled_absences()
    except Exception:
        pass
    try:
        result["missing_checkout"] = check_missing_checkout()
    except Exception:
        pass
    return result


async def periodic_checker():
    while True:
        await asyncio.sleep(60)
        await asyncio.to_thread(run_all_checks)


@app.on_event("startup")
async def startup():
    await asyncio.to_thread(init_db)
    asyncio.create_task(periodic_checker())


# ---------- HTML helpers ----------

def page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{escape(title)}</title><style>
body{{font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f6f7fb;margin:0;color:#1f2937}}
.wrap{{max-width:1240px;margin:0 auto;padding:20px}}.top{{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}}
h1,h2{{margin-top:0}}.muted{{color:#6b7280}}.cards{{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}}.card{{background:white;padding:14px 18px;border-radius:12px;box-shadow:0 2px 10px #0000000b;min-width:130px}}.num{{font-size:27px;font-weight:700}}
section{{background:white;border-radius:14px;padding:18px;margin-top:16px;box-shadow:0 2px 10px #0000000b}}table{{width:100%;border-collapse:collapse}}th,td{{padding:9px 8px;border-bottom:1px solid #eee;text-align:left;font-size:13px;vertical-align:top}}th{{background:#fafafa;position:sticky;top:0}}
input,select,textarea{{padding:7px 8px;border:1px solid #d1d5db;border-radius:7px;box-sizing:border-box}}input{{width:130px}}textarea{{width:100%}}button,.btn{{display:inline-block;border:0;border-radius:8px;padding:8px 11px;cursor:pointer;background:#111827;color:white;text-decoration:none}}.btn2{{background:#e5e7eb;color:#111827}}
.green{{background:#dcfce7;padding:4px 7px;border-radius:999px}}.orange{{background:#ffedd5;padding:4px 7px;border-radius:999px}}.red{{background:#fee2e2;padding:4px 7px;border-radius:999px}}.gray{{background:#f3f4f6;padding:4px 7px;border-radius:999px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}.nav a{{margin-right:8px}}.mini{{font-size:12px}}.wide{{min-width:180px}}.qr{{width:95px;height:95px;border:1px solid #eee}}
.alert{{padding:12px;border-radius:10px;background:#fff7ed;margin-bottom:12px}}.danger{{background:#fef2f2}}.success{{background:#ecfdf5}}
@media(max-width:900px){{.grid{{grid-template-columns:1fr}}.wrap{{padding:10px}}th,td{{font-size:12px}}}}
</style></head><body><div class='wrap'>{body}</div></body></html>"""


def admin_nav() -> str:
    return "<div class='nav'><a class='btn btn2' href='/admin'>今日出勤</a><a class='btn btn2' href='/admin/courses'>課程時間</a><a class='btn btn2' href='/admin/export.csv'>匯出 CSV</a></div>"


@app.get("/", response_class=HTMLResponse)
def root(_: str = Depends(admin_auth)):
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def dashboard(_: str = Depends(admin_auth)):
    checks = run_all_checks()
    today = now_local().date()
    body_parts = [
        f"<div class='top'><div><h1>出勤測試系統 V0.2</h1><div class='muted'>Render 隔離測試站｜LINE：{escape(LINE_MODE)}</div></div>{admin_nav()}</div>",
        f"<div class='alert'>今天：{today:%Y-%m-%d}　自動檢查：未到 {checks['absent']} 筆、未離班 {checks['missing_checkout']} 筆。系統每 60 秒會再檢查一次。</div>",
    ]
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM students WHERE active=TRUE")
            student_count = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM attendance WHERE date=%s", (today,))
            total_records = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM attendance WHERE date=%s AND status='late'", (today,))
            late_count = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM attendance WHERE date=%s AND status='absent' AND check_in_time IS NULL", (today,))
            absent_count = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM attendance WHERE date=%s AND check_in_time IS NOT NULL AND check_out_time IS NULL", (today,))
            open_count = cur.fetchone()["c"]

            cur.execute("SELECT * FROM students WHERE active=TRUE ORDER BY id")
            students = cur.fetchall()

            cur.execute(
                """
                SELECT c.*, s.name, s.student_code, s.line_user_id
                FROM courses c JOIN students s ON s.id=c.student_id
                WHERE c.weekday=%s AND c.active=TRUE
                ORDER BY c.start_time, s.name
                """,
                (today.weekday(),),
            )
            roster_raw = cur.fetchall()
            roster = []
            for c in roster_raw:
                eff = effective_schedule(cur, c, today)
                if not eff:
                    continue
                cur.execute("SELECT * FROM attendance WHERE student_id=%s AND course_id=%s AND date=%s", (c["student_id"], c["id"], today))
                a = cur.fetchone()
                item = dict(c)
                item["effective_start"] = eff["effective_start"]
                item["effective_end"] = eff["effective_end"]
                item["override_note"] = eff.get("override_note", "")
                item["attendance"] = a
                roster.append(item)

            cur.execute("SELECT * FROM notification_logs ORDER BY id DESC LIMIT 30")
            notifications = cur.fetchall()

    body_parts.append(f"<div class='cards'><div class='card'><div class='muted'>學生</div><div class='num'>{student_count}</div></div><div class='card'><div class='muted'>今日紀錄</div><div class='num'>{total_records}</div></div><div class='card'><div class='muted'>遲到</div><div class='num'>{late_count}</div></div><div class='card'><div class='muted'>未到</div><div class='num'>{absent_count}</div></div><div class='card'><div class='muted'>尚未離班</div><div class='num'>{open_count}</div></div></div>")

    qr_rows = []
    for s in students:
        qr_rows.append(
            f"<tr><td>{escape(s['name'])}</td><td>{escape(s['student_code'])}</td>"
            f"<td><img class='qr' src='/qr/{escape(s['student_code'])}.png'></td>"
            f"<td><form method='post' action='/admin/student/{s['id']}/line'><input name='line_user_id' value='{escape(s['line_user_id'] or '', quote=True)}' placeholder='LINE User ID'><button>儲存</button></form></td></tr>"
        )
    body_parts.append("<section><h2>學生 QR / LINE</h2><table><tr><th>學生</th><th>編號</th><th>QR</th><th>LINE User ID</th></tr>" + "".join(qr_rows) + "</table></section>")

    roster_rows = []
    for r in roster:
        a = r["attendance"]
        late = a["late_minutes"] if a else 0
        if a and a["check_out_time"]:
            status = "<span class='green'>完成</span>"
        elif a and a["status"] == "absent" and not a["check_in_time"]:
            status = "<span class='red'>未到</span>"
        elif late > r["late_grace_minutes"]:
            status = "<span class='orange'>遲到</span>"
        elif a and a["check_in_time"]:
            status = "<span class='gray'>到班</span>"
        else:
            status = "<span class='gray'>尚未掃描</span>"
        actions = []
        if a:
            actions.append(f"<a class='btn btn2 mini' href='/admin/attendance/{a['id']}'>校正</a>")
            if a["check_in_time"]:
                actions.append(f"<form style='display:inline' method='post' action='/admin/notify-late/{a['id']}'><button class='btn btn2 mini'>手動遲到通知</button></form>")
            if not a["check_in_time"]:
                actions.append(f"<form style='display:inline' method='post' action='/admin/notify-absent/{r['id']}'><button class='btn btn2 mini'>手動未到通知</button></form>")
            if a["check_in_time"] and not a["check_out_time"]:
                actions.append(f"<form style='display:inline' method='post' action='/admin/notify-missing/{a['id']}'><button class='btn btn2 mini'>手動未離班通知</button></form>")
        else:
            actions.append(f"<form style='display:inline' method='post' action='/admin/notify-absent/{r['id']}'><button class='btn btn2 mini'>手動未到通知</button></form>")
        start_note = f"<br><span class='muted mini'>校正：{escape(r['override_note'])}</span>" if r.get("override_note") else ""
        roster_rows.append(
            f"<tr><td>{escape(r['name'])}<br><span class='muted mini'>{escape(r['student_code'])}</span></td>"
            f"<td>{escape(r['course_name'])}<br><span class='muted mini'>{escape(r['teacher_name'] or '')}</span></td>"
            f"<td>{r['effective_start']}-{r['effective_end']}<br><span class='muted mini'>遲到>{r['late_grace_minutes']}分／未離班+{r['checkout_grace_minutes']}分</span>{start_note}</td>"
            f"<td>{a['check_in_time'].strftime('%H:%M:%S') if a and a['check_in_time'] else '-'}</td>"
            f"<td>{a['check_out_time'].strftime('%H:%M:%S') if a and a['check_out_time'] else '-'}</td>"
            f"<td>{status}<br><span class='muted mini'>遲到 {late} 分</span></td><td>{' '.join(actions)}</td></tr>"
        )
    body_parts.append("<section><h2>今日課程 / 出勤</h2><div style='overflow:auto'><table><tr><th>學生</th><th>課程</th><th>實際判定時間</th><th>到班</th><th>離班</th><th>狀態</th><th>操作</th></tr>" + "".join(roster_rows) + "</table></div></section>")

    logs = []
    for n in notifications:
        logs.append(f"<div style='padding:8px 0;border-bottom:1px solid #eee'><b>{n['created_at']:%m-%d %H:%M:%S}</b>｜{escape(n['notification_type'])}｜{escape(n['status'])}<br>{escape(n['message'])}{('<br><span class=muted>錯誤：'+escape(n['error_message'])+'</span>') if n['error_message'] else ''}</div>")
    body_parts.append("<section><h2>最近通知紀錄</h2>" + ("".join(logs) or "尚無通知紀錄") + "</section>")
    return page("出勤測試系統", "".join(body_parts))


@app.get("/admin/courses", response_class=HTMLResponse)
def admin_courses(_: str = Depends(admin_auth)):
    today = now_local().date()
    body = [f"<div class='top'><div><h1>課程時間設定</h1><div class='muted'>永久週課表 + 指定日期校正。今日校正會優先影響今天的簽到判斷。</div></div>{admin_nav()}</div>"]
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.*, s.name, s.student_code
                FROM courses c JOIN students s ON s.id=c.student_id
                WHERE c.active=TRUE
                ORDER BY s.name,c.weekday,c.start_time
                """
            )
            courses = cur.fetchall()
            for c in courses:
                cur.execute("SELECT * FROM schedule_overrides WHERE course_id=%s AND work_date=%s", (c["id"], today))
                ov = cur.fetchone()
                wd = c["weekday"]
                weekday_options = ''.join(
                    f"<option value='{i}' {'selected' if i == wd else ''}>{WEEKDAYS[i]}</option>"
                    for i in range(7)
                )
                body.append(
                    f"<section><h2>{escape(c['name'])}｜{escape(c['course_name'])}</h2>"
                    f"<form method='post' action='/admin/course/{c['id']}'><div class='grid'>"
                    f"<div><label>星期<br><select name='weekday'>{weekday_options}</select></label> "
                    f"<label>開始<br><input type='time' name='start_time' value='{escape(c['start_time'])}'></label> "
                    f"<label>結束<br><input type='time' name='end_time' value='{escape(c['end_time'])}'></label></div>"
                    f"<div><label>遲到門檻(分)<br><input type='number' name='late_grace_minutes' value='{c['late_grace_minutes']}' min='0' max='180'></label> "
                    f"<label>未離班通知(分)<br><input type='number' name='checkout_grace_minutes' value='{c['checkout_grace_minutes']}' min='1' max='240'></label></div></div>"
                    f"<p><label>課程名稱 <input class='wide' name='course_name' value='{escape(c['course_name'], quote=True)}'></label> "
                    f"<label>老師 <input class='wide' name='teacher_name' value='{escape(c['teacher_name'] or '', quote=True)}'></label> <button>儲存永久課表</button></p></form>"
                    f"<hr><h3>今日 ({today:%Y-%m-%d}) 校正</h3>"
                    f"<form method='post' action='/admin/course/{c['id']}/override'><label>今天開始 <input type='time' name='start_time' value='{escape((ov['start_time'] if ov and ov['start_time'] else c['start_time']))}'></label> "
                    f"<label>今天結束 <input type='time' name='end_time' value='{escape((ov['end_time'] if ov and ov['end_time'] else c['end_time']))}'></label> "
                    f"<label>備註 <input class='wide' name='note' value='{escape((ov['note'] if ov else ''), quote=True)}' placeholder='例如：9/18 臨時調課'></label> "
                    f"<button>儲存今日校正</button>"
                    f"<label style='margin-left:8px'><input type='checkbox' name='cancelled' value='1' {'checked' if ov and ov['cancelled'] else ''}> 今日取消</label></form>"
                    f"<p class='mini muted'>目前今日有效時間：<b>{escape((ov['start_time'] if ov and ov['start_time'] else c['start_time']))} - {escape((ov['end_time'] if ov and ov['end_time'] else c['end_time']))}</b>。修改後會直接影響今天的到班、遲到與未離班判斷。</p></section>"
                )
    return page("課程時間設定", "".join(body))


@app.post("/admin/course/{course_id}")
def update_course(course_id: int, weekday: int = Form(...), start_time: str = Form(...), end_time: str = Form(...), late_grace_minutes: int = Form(...), checkout_grace_minutes: int = Form(...), course_name: str = Form(...), teacher_name: str = Form(""), _: str = Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE courses SET weekday=%s,start_time=%s,end_time=%s,late_grace_minutes=%s,checkout_grace_minutes=%s,course_name=%s,teacher_name=%s WHERE id=%s
                """,
                (weekday, start_time, end_time, max(0, late_grace_minutes), max(1, checkout_grace_minutes), course_name.strip(), teacher_name.strip() or None, course_id),
            )
            conn.commit()
    return RedirectResponse("/admin/courses", status_code=303)


@app.post("/admin/course/{course_id}/override")
def save_override(course_id: int, start_time: str = Form(...), end_time: str = Form(...), note: str = Form(""), cancelled: str | None = Form(None), _: str = Depends(admin_auth)):
    today = now_local().date()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO schedule_overrides(course_id,work_date,start_time,end_time,cancelled,note)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT(course_id,work_date) DO UPDATE SET start_time=EXCLUDED.start_time,end_time=EXCLUDED.end_time,cancelled=EXCLUDED.cancelled,note=EXCLUDED.note
                """,
                (course_id, today, start_time.strip() or None, end_time.strip() or None, bool(cancelled), note.strip() or None),
            )
            conn.commit()
    return RedirectResponse("/admin/courses", status_code=303)


@app.get("/admin/attendance/{attendance_id}", response_class=HTMLResponse)
def edit_attendance(attendance_id: int, _: str = Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.*,s.name,s.student_code,c.course_name,c.start_time,c.end_time
                FROM attendance a JOIN students s ON s.id=a.student_id LEFT JOIN courses c ON c.id=a.course_id
                WHERE a.id=%s
                """,
                (attendance_id,),
            )
            a = cur.fetchone()
    if not a:
        raise HTTPException(404, "找不到出勤紀錄")
    body = f"""
    <div class='top'><div><h1>校正出勤</h1><div class='muted'>{escape(a['name'])}｜{escape(a['course_name'] or '')}｜{a['date']}</div></div>{admin_nav()}</div>
    <section><div class='alert'>這裡修改的是「實際出勤時間」。校正不會改掉課表；要改課程時間請到「課程時間」頁面。</div>
    <form method='post' action='/admin/attendance/{attendance_id}/adjust'>
    <p>到班時間（HH:MM）<input type='time' name='check_in' value='{a['check_in_time'].strftime('%H:%M') if a['check_in_time'] else ''}'>
    離班時間（HH:MM）<input type='time' name='check_out' value='{a['check_out_time'].strftime('%H:%M') if a['check_out_time'] else ''}'></p>
    <p>狀態 <select name='status'>{''.join(f"<option value='{x}' {'selected' if a['status']==x else ''}>{x}</option>" for x in ['checked_in','late','completed','absent'])}</select>
    遲到分鐘 <input type='number' name='late_minutes' value='{a['late_minutes']}' min='0'> </p>
    <p>校正備註</p><textarea name='manual_note' rows='4' placeholder='例如：學生忘記掃離班，由櫃台依監視器時間補登。'>{escape(a['manual_note'] or '')}</textarea>
    <p><button>儲存校正</button> <a class='btn btn2' href='/admin'>取消</a></p></form></section>
    """
    return page("校正出勤", body)


@app.post("/admin/attendance/{attendance_id}/adjust")
def adjust_attendance(attendance_id: int, check_in: str = Form(""), check_out: str = Form(""), status: str = Form(...), late_minutes: int = Form(0), manual_note: str = Form(""), user: str = Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT date FROM attendance WHERE id=%s", (attendance_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "找不到紀錄")
            d = row["date"]
            ci = parse_form_datetime(check_in, d)
            co = parse_form_datetime(check_out, d)
            cur.execute(
                """
                UPDATE attendance SET check_in_time=%s,check_out_time=%s,status=%s,late_minutes=%s,manual_note=%s,adjusted_at=%s,adjusted_by=%s,updated_at=%s
                WHERE id=%s
                """,
                (ci, co, status, max(0, late_minutes), manual_note.strip() or None, now_local(), user, now_local(), attendance_id),
            )
            conn.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/notify-late/{attendance_id}")
def manual_late(attendance_id: int, _: str = Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.*,s.name,s.line_user_id,c.course_name,c.start_time
                FROM attendance a JOIN students s ON s.id=a.student_id JOIN courses c ON c.id=a.course_id WHERE a.id=%s
                """,
                (attendance_id,),
            )
            a = cur.fetchone()
            if not a or not a["check_in_time"]:
                raise HTTPException(400, "沒有到班時間，無法發送遲到通知")
            cur.execute("SELECT * FROM courses WHERE id=%s", (a["course_id"],))
            course = cur.fetchone()
            if not course:
                raise HTTPException(404, "找不到課程")
            eff = effective_schedule(cur, course, a["date"])
            msg = late_admin_message(a["name"], a["course_name"], eff["effective_start"], a["check_in_time"], a["late_minutes"])
            send_line(cur, attendance_id, LINE_ADMIN_USER_ID, msg, "manual_late")
            conn.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/notify-absent/{course_id}")
def manual_absent(course_id: int, _: str = Depends(admin_auth)):
    today = now_local().date(); now = now_local()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT c.*,s.name FROM courses c JOIN students s ON s.id=c.student_id WHERE c.id=%s", (course_id,))
            c = cur.fetchone()
            if not c:
                raise HTTPException(404, "找不到課程")
            eff = effective_schedule(cur, c, today)
            if not eff:
                raise HTTPException(400, "今日課程已取消")
            cur.execute("SELECT * FROM attendance WHERE student_id=%s AND course_id=%s AND date=%s", (c["student_id"],course_id,today))
            a = cur.fetchone()
            if not a:
                cur.execute("INSERT INTO attendance(student_id,course_id,date,status,updated_at) VALUES (%s,%s,%s,'absent',%s) RETURNING id", (c["student_id"],course_id,today,now))
                aid = cur.fetchone()["id"]
            else:
                aid = a["id"]
            msg = absent_admin_message(c["name"], c["course_name"], eff["effective_start"], now)
            if send_line(cur, aid, LINE_ADMIN_USER_ID, msg, "manual_absent"):
                cur.execute("UPDATE attendance SET absent_notified_at=%s, status=CASE WHEN check_in_time IS NULL THEN 'absent' ELSE status END, updated_at=%s WHERE id=%s", (now,now,aid))
            conn.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/notify-missing/{attendance_id}")
def manual_missing(attendance_id: int, _: str = Depends(admin_auth)):
    now = now_local()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT a.*,s.name,c.course_name,c.end_time,c.checkout_grace_minutes FROM attendance a JOIN students s ON s.id=a.student_id JOIN courses c ON c.id=a.course_id WHERE a.id=%s", (attendance_id,))
            a = cur.fetchone()
            if not a or not a["check_in_time"] or a["check_out_time"]:
                raise HTTPException(400, "目前沒有可通知的未離班紀錄")
            cur.execute("SELECT * FROM courses WHERE id=%s", (a["course_id"],))
            course = cur.fetchone()
            if not course:
                raise HTTPException(404, "找不到課程")
            eff = effective_schedule(cur, course, now.date())
            if not eff:
                raise HTTPException(400, "今日課程已取消")
            row = dict(a); row["effective_end"] = eff["effective_end"]
            msg = missing_checkout_message(row, now)
            if send_line(cur, attendance_id, LINE_ADMIN_USER_ID, msg, "manual_missing_checkout"):
                cur.execute("UPDATE attendance SET missing_checkout_notified_at=%s,updated_at=%s WHERE id=%s", (now,now,attendance_id))
            conn.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/student/{student_id}/line")
def update_student_line(student_id: int, line_user_id: str = Form(""), _: str = Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE students SET line_user_id=%s WHERE id=%s", (line_user_id.strip() or None, student_id))
            if cur.rowcount == 0:
                raise HTTPException(404, "找不到學生")
            conn.commit()
    return RedirectResponse("/admin", status_code=303)


@app.get("/qr/{student_code}.png")
def qr(student_code: str):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT qr_token FROM students WHERE student_code=%s AND active=TRUE", (student_code,))
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, "找不到學生")
    img = qrcode.make(f"{public_base_url()}/scan/{row['qr_token']}")
    buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.get("/scan/{token}", response_class=HTMLResponse)
def scan(token: str):
    result = scan_student(token)
    kind = result.get("kind")
    if kind == "check_in":
        title = "到班完成"; icon = "✅"; detail = f"{result['time'][11:16]}<br>遲到：{result['late']} 分鐘" if result.get('late', 0) else result['time'][11:16]
    elif kind == "check_out":
        title = "離班完成"; icon = "👋"; detail = f"到班：{result['check_in'][11:16]}<br>離班：{result['time'][11:16]}"
    elif kind == "duplicate":
        title = "已有紀錄"; icon = "ℹ️"; detail = escape(result["message"])
    else:
        title = result.get("message", "系統訊息"); icon = "⚠️"; detail = ""
    body = f"<div style='max-width:540px;margin:60px auto;background:white;border-radius:16px;padding:28px;text-align:center;box-shadow:0 4px 20px #0001'><div style='font-size:30px'>{icon}</div><h1>{escape(title)}</h1><h2>{escape(result.get('student',''))}</h2><p>{escape(result.get('course',''))}</p><p>{detail}</p><p><a class='btn' href='/admin'>返回管理頁</a></p></div>"
    return page("簽到結果", body)


@app.get("/admin/export.csv")
def export_csv(_: str = Depends(admin_auth)):
    buf = io.StringIO(); buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow(["紀錄ID","日期","學生編號","學生姓名","課程","老師","預定開始","預定結束","到班","離班","狀態","遲到分鐘","校正備註","校正時間","校正者"])
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.id,a.date,s.student_code,s.name,c.course_name,c.teacher_name,c.start_time,c.end_time,a.check_in_time,a.check_out_time,a.status,a.late_minutes,a.manual_note,a.adjusted_at,a.adjusted_by
                FROM attendance a JOIN students s ON s.id=a.student_id LEFT JOIN courses c ON c.id=a.course_id
                ORDER BY a.date DESC,a.id DESC
                """
            )
            for r in cur.fetchall():
                writer.writerow([r[k] for k in ["id","date","student_code","name","course_name","teacher_name","start_time","end_time","check_in_time","check_out_time","status","late_minutes","manual_note","adjusted_at","adjusted_by"]])
    data = buf.getvalue().encode("utf-8-sig")
    return StreamingResponse(io.BytesIO(data), media_type="text/csv; charset=utf-8", headers={"Content-Disposition":"attachment; filename=attendance_test_export.csv"})


@app.get("/health")
def health():
    return JSONResponse({"ok": True, "line_mode": LINE_MODE, "database": "postgres", "version": "0.2"})
