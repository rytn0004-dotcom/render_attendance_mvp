from __future__ import annotations

import asyncio
import csv
import io
import os
import secrets
import hmac
import hashlib
import base64
from datetime import date, datetime, timedelta
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

import qrcode
import requests
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Form, HTTPException, Request, Depends, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

LINE_MODE = os.getenv("LINE_MODE", "simulation").lower()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()
LINE_ADMIN_USER_ID = os.getenv("LINE_ADMIN_USER_ID", "").strip()
LINE_LOGIN_CHANNEL_ID = os.getenv("LINE_LOGIN_CHANNEL_ID", "").strip()
LIFF_ID = os.getenv("LIFF_ID", "").strip()
BINDING_LINK_MINUTES = int(os.getenv("BINDING_LINK_MINUTES", "60"))
DEFAULT_LATE_GRACE_MINUTES = int(os.getenv("LATE_GRACE_MINUTES", "10"))
DEFAULT_CHECKOUT_GRACE_MINUTES = int(os.getenv("CHECKOUT_GRACE_MINUTES", "15"))
CHECKIN_EARLY_MINUTES = int(os.getenv("CHECKIN_EARLY_MINUTES", "60"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "test1234")
DEVICE_COOKIE_NAME = os.getenv("DEVICE_COOKIE_NAME", "attendance_device_token")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

app = FastAPI(title="Attendance Test MVP v0.4.1")
security = HTTPBasic()

WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

# 內部仍使用英文代碼，畫面與匯出資料優先顯示中文。
STATUS_LABELS = {
    "checked_in": "已到班",
    "late": "遲到",
    "completed": "已完成",
    "absent": "未到班",
}
NOTIFICATION_TYPE_LABELS = {
    "check_in": "到班通知",
    "check_out": "離班通知",
    "late": "遲到通知",
    "absent": "未到班通知",
    "missing_checkout": "未離班通知",
    "manual_late": "手動遲到通知",
    "manual_absent": "手動未到通知",
    "manual_missing_checkout": "手動未離班通知",
    "invalid_schedule": "無課程掃描通知",
    "line_test": "LINE 連線測試",
}
NOTIFY_STATUS_LABELS = {
    "sent": "已發送",
    "simulated": "模擬發送",
    "failed": "發送失敗",
}
LINE_MODE_LABELS = {
    "simulation": "模擬模式",
    "live": "正式發送",
}

TEMPLATE_TYPE_LABELS = {
    "check_in": "到班通知",
    "check_out": "離開教室通知",
    "late": "遲到通知",
    "absent": "未到班通知",
    "missing_checkout": "未離班通知",
}

DEFAULT_NOTIFICATION_TEMPLATES = {
    "check_in": "{greeting}，{student_name}到教室了😊",
    "check_out": "{greeting}，{student_name}離開教室了，{thanks}😊",
    "late": "⚠️ 學生遲到\n\n{student_name}\n課程：{course_name}\n原定：{scheduled_start}\n實際到班：{check_in_time}\n遲到：{late_minutes} 分鐘",
    "absent": "🔴 學生未到班\n\n{student_name}\n課程：{course_name}\n原定：{scheduled_start}\n截至 {now_time} 尚未完成到班簽到。",
    "missing_checkout": "🔴 未完成離班簽到\n\n{student_name}\n課程：{course_name}\n到班：{check_in_time}\n原定下課：{scheduled_end}\n截至 {now_time} 尚未完成離班簽到。",
}

def status_label(value: str | None) -> str:
    return STATUS_LABELS.get(value or "", value or "")

def notification_type_label(value: str | None) -> str:
    return NOTIFICATION_TYPE_LABELS.get(value or "", value or "")

def notify_status_label(value: str | None) -> str:
    return NOTIFY_STATUS_LABELS.get(value or "", value or "")


def qr_data_uri(text: str) -> str:
    """Generate an inline QR image for admin pages without exposing a separate QR route."""
    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


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
                CREATE TABLE IF NOT EXISTS line_binding_codes (
                    id BIGSERIAL PRIMARY KEY,
                    student_id BIGINT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                    code TEXT NOT NULL UNIQUE,
                    expires_at TIMESTAMP NOT NULL,
                    used_at TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS student_line_bindings (
                    id BIGSERIAL PRIMARY KEY,
                    student_id BIGINT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                    line_user_id TEXT NOT NULL,
                    display_name TEXT,
                    relation TEXT NOT NULL DEFAULT '家長/監護人',
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    bound_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    bound_by TEXT NOT NULL DEFAULT 'LIFF'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_student_line_binding
                    ON student_line_bindings(student_id, line_user_id) WHERE active=TRUE;
                CREATE INDEX IF NOT EXISTS idx_student_line_user
                    ON student_line_bindings(line_user_id) WHERE active=TRUE;
                CREATE TABLE IF NOT EXISTS line_bind_tokens (
                    id BIGSERIAL PRIMARY KEY,
                    student_id BIGINT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    token_value TEXT,
                    expires_at TIMESTAMP NOT NULL,
                    used_at TIMESTAMP,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    last_used_at TIMESTAMP,
                    created_by TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_line_bind_tokens_student
                    ON line_bind_tokens(student_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS line_message_logs (
                    id BIGSERIAL PRIMARY KEY,
                    event_id TEXT,
                    direction TEXT NOT NULL,
                    message_type TEXT NOT NULL,
                    line_user_id TEXT,
                    student_id BIGINT REFERENCES students(id),
                    message TEXT,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS checkin_devices (
                    id BIGSERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS device_pair_tokens (
                    id BIGSERIAL PRIMARY KEY,
                    device_id BIGINT NOT NULL REFERENCES checkin_devices(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL UNIQUE,
                    expires_at TIMESTAMP NOT NULL,
                    used_at TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_device_pair_tokens_lookup ON device_pair_tokens(token_hash, expires_at);
                CREATE TABLE IF NOT EXISTS notification_templates (
                    id BIGSERIAL PRIMARY KEY,
                    notification_type TEXT NOT NULL,
                    student_id BIGINT REFERENCES students(id) ON DELETE CASCADE,
                    template_text TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'permanent',
                    remaining_uses INTEGER,
                    expires_at TIMESTAMP,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    note TEXT,
                    created_by TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_notification_templates_lookup
                    ON notification_templates(notification_type, student_id, active, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_line_messages_created ON line_message_logs(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_line_messages_user ON line_message_logs(line_user_id);
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
            cur.execute("ALTER TABLE line_bind_tokens ADD COLUMN IF NOT EXISTS token_value TEXT")
            cur.execute("ALTER TABLE line_bind_tokens ADD COLUMN IF NOT EXISTS use_count INTEGER NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE line_bind_tokens ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMP")
            cur.execute(
                "UPDATE courses SET late_grace_minutes=%s WHERE late_grace_minutes IS NULL",
                (DEFAULT_LATE_GRACE_MINUTES,),
            )
            cur.execute(
                "UPDATE courses SET checkout_grace_minutes=%s WHERE checkout_grace_minutes IS NULL",
                (DEFAULT_CHECKOUT_GRACE_MINUTES,),
            )
            seed_demo_data(cur)
            seed_notification_templates(cur)
        conn.commit()


def seed_demo_data(cur) -> None:
    demo = [
        ("STU-000001", "王小明", "18:00", "19:30"),
        ("STU-000002", "林小華", "18:30", "20:00"),
        ("STU-000003", "陳小美", "19:00", "20:30"),
        ("STU-000004", "采璇", "19:00", "20:30"),
    ]
    for code, name, start, end in demo:
        cur.execute("SELECT id FROM students WHERE student_code=%s", (code,))
        row = cur.fetchone()
        if not row:
            cur.execute(
                "INSERT INTO students(student_code, name, qr_token) VALUES (%s,%s,%s) RETURNING id",
                (code, name, secrets.token_urlsafe(18)),
            )
            row = cur.fetchone()
        student_id = row["id"]
        cur.execute("SELECT COUNT(*) AS c FROM courses WHERE student_id=%s", (student_id,))
        if cur.fetchone()["c"] == 0:
            for weekday in range(7):
                cur.execute(
                    """INSERT INTO courses(student_id, course_name, teacher_name, weekday, start_time, end_time, late_grace_minutes, checkout_grace_minutes)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (student_id, "測試課程", "測試老師", weekday, start, end, DEFAULT_LATE_GRACE_MINUTES, DEFAULT_CHECKOUT_GRACE_MINUTES),
                )


def seed_notification_templates(cur) -> None:
    for notification_type, template_text in DEFAULT_NOTIFICATION_TEMPLATES.items():
        cur.execute(
            "SELECT id FROM notification_templates WHERE notification_type=%s AND student_id IS NULL AND active=TRUE LIMIT 1",
            (notification_type,),
        )
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO notification_templates(notification_type,student_id,template_text,mode,active,created_by) VALUES (%s,NULL,%s,'default',TRUE,'system')",
                (notification_type, template_text),
            )


def get_effective_template(cur, notification_type: str, student_id: int | None = None) -> dict[str, Any] | None:
    now = now_local()
    if student_id is not None:
        cur.execute(
            """SELECT * FROM notification_templates
               WHERE notification_type=%s AND student_id=%s AND active=TRUE
                 AND (expires_at IS NULL OR expires_at>%s)
                 AND (remaining_uses IS NULL OR remaining_uses>0)
               ORDER BY created_at DESC LIMIT 1""",
            (notification_type, student_id, now),
        )
        row = cur.fetchone()
        if row:
            return row
    cur.execute(
        """SELECT * FROM notification_templates
           WHERE notification_type=%s AND student_id IS NULL AND active=TRUE
             AND (expires_at IS NULL OR expires_at>%s)
             AND (remaining_uses IS NULL OR remaining_uses>0)
           ORDER BY created_at DESC LIMIT 1""",
        (notification_type, now),
    )
    return cur.fetchone()


def _recipient_words(relation: str | None) -> tuple[str, str]:
    relation = (relation or "").strip()
    if "媽媽" in relation or relation.lower() == "mother":
        return "媽媽您好", "感謝媽媽"
    if "爸爸" in relation or relation.lower() == "father":
        return "爸爸您好", "感謝爸爸"
    if "奶奶" in relation:
        return "奶奶您好", "感謝奶奶"
    if "爺爺" in relation:
        return "爺爺您好", "感謝爺爺"
    if "外婆" in relation:
        return "外婆您好", "感謝外婆"
    if "外公" in relation:
        return "外公您好", "感謝外公"
    return "家長您好", "感謝您"


def render_notification_template(template_text: str, student: dict[str, Any], course: dict[str, Any] | None = None,
                                  relation: str | None = None, check_in_time: datetime | None = None,
                                  check_out_time: datetime | None = None, when: datetime | None = None,
                                  late_minutes: int = 0) -> str:
    when = when or now_local()
    greeting, thanks = _recipient_words(relation)
    values = {
        "greeting": greeting,
        "thanks": thanks,
        "student_name": student.get("name") or "",
        "student_code": student.get("student_code") or "",
        "course_name": (course or {}).get("course_name") or "",
        "teacher_name": (course or {}).get("teacher_name") or "",
        "scheduled_start": (course or {}).get("effective_start") or (course or {}).get("start_time") or "",
        "scheduled_end": (course or {}).get("effective_end") or (course or {}).get("end_time") or "",
        "check_in_time": check_in_time.strftime("%H:%M") if check_in_time else "",
        "check_out_time": check_out_time.strftime("%H:%M") if check_out_time else "",
        "now_time": when.strftime("%H:%M"),
        "late_minutes": str(late_minutes),
    }
    try:
        return template_text.format(**values)
    except KeyError:
        # 範本有未知變數時，不讓簽到流程整體失敗。
        return template_text


def consume_one_time_template(cur, template_row: dict[str, Any] | None) -> None:
    if not template_row or template_row.get("student_id") is None or not template_row.get("active"):
        return
    if template_row.get("mode") != "once":
        return
    if template_row.get("remaining_uses") is None or template_row.get("remaining_uses") <= 1:
        cur.execute("UPDATE notification_templates SET remaining_uses=0,active=FALSE,updated_at=%s WHERE id=%s", (now_local(), template_row["id"]))
    else:
        cur.execute("UPDATE notification_templates SET remaining_uses=remaining_uses-1,updated_at=%s WHERE id=%s", (now_local(), template_row["id"]))


def active_line_bindings(cur, student_id: int, legacy_line_user_id: str | None = None) -> list[dict[str, Any]]:
    cur.execute(
        "SELECT id,line_user_id,display_name,relation FROM student_line_bindings WHERE student_id=%s AND active=TRUE ORDER BY id",
        (student_id,),
    )
    rows = cur.fetchall()
    if legacy_line_user_id and not any(r["line_user_id"] == legacy_line_user_id for r in rows):
        rows.append({"id": None, "line_user_id": legacy_line_user_id, "display_name": None, "relation": "家長/監護人"})
    return rows


def send_student_template_line(cur, attendance_id: int | None, student: dict[str, Any], course: dict[str, Any] | None,
                               notification_type: str, check_in_time: datetime | None = None,
                               check_out_time: datetime | None = None, when: datetime | None = None,
                               late_minutes: int = 0, legacy_line_user_id: str | None = None) -> bool:
    bindings = active_line_bindings(cur, student["id"], legacy_line_user_id)
    if not bindings:
        default_msg = render_notification_template(DEFAULT_NOTIFICATION_TEMPLATES[notification_type], student, course, None, check_in_time, check_out_time, when, late_minutes)
        log_notification(cur, attendance_id, notification_type, None, default_msg, LINE_MODE, "failed", "學生尚未綁定 LINE")
        return False
    template_row = get_effective_template(cur, notification_type, student["id"])
    template_text = template_row["template_text"] if template_row else DEFAULT_NOTIFICATION_TEMPLATES[notification_type]
    results = []
    for binding in bindings:
        msg = render_notification_template(template_text, student, course, binding.get("relation"), check_in_time, check_out_time, when, late_minutes)
        results.append(send_line(cur, attendance_id, binding["line_user_id"], msg, notification_type))
    if any(results):
        consume_one_time_template(cur, template_row)
    return bool(results) and all(results)


def hash_device_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_device_pairing(name: str) -> tuple[str, datetime, int]:
    device_raw_token = secrets.token_urlsafe(32)
    pair_token = secrets.token_urlsafe(24)
    expires = now_local() + timedelta(minutes=30)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO checkin_devices(name,token_hash,active) VALUES (%s,%s,TRUE) RETURNING id",
                (name.strip() or "教室設備", hash_device_token(device_raw_token)),
            )
            device_id = cur.fetchone()["id"]
            cur.execute(
                "INSERT INTO device_pair_tokens(device_id,token_hash,expires_at) VALUES (%s,%s,%s)",
                (device_id, hash_device_token(pair_token), expires),
            )
            conn.commit()
    return pair_token, expires, device_id


def require_checkin_device(request: Request) -> dict[str, Any]:
    raw = request.cookies.get(DEVICE_COOKIE_NAME)
    if not raw:
        raise HTTPException(status_code=403, detail="此頁面只能由已授權的教室簽到設備使用。請先在管理後台建立設備配對。")
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,name,active FROM checkin_devices WHERE token_hash=%s", (hash_device_token(raw),))
            device = cur.fetchone()
            if not device or not device["active"]:
                raise HTTPException(status_code=403, detail="此設備尚未授權或已停用，請聯絡管理員重新配對。")
            cur.execute("UPDATE checkin_devices SET last_used_at=%s WHERE id=%s", (now_local(), device["id"]))
            conn.commit()
            return device


def pair_device(pair_token: str) -> tuple[str, str]:
    device_raw_token = secrets.token_urlsafe(32)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT p.id,p.device_id,d.name,d.active AS device_active
                   FROM device_pair_tokens p JOIN checkin_devices d ON d.id=p.device_id
                   WHERE p.token_hash=%s AND p.used_at IS NULL AND p.expires_at>%s""",
                (hash_device_token(pair_token), now_local()),
            )
            pair = cur.fetchone()
            if not pair or not pair["device_active"]:
                raise HTTPException(status_code=400, detail="設備配對連結無效、已使用或已過期。請由管理員重新產生。")
            cur.execute("UPDATE checkin_devices SET token_hash=%s,active=TRUE WHERE id=%s", (hash_device_token(device_raw_token), pair["device_id"]))
            cur.execute("UPDATE device_pair_tokens SET used_at=%s WHERE id=%s", (now_local(), pair["id"]))
            conn.commit()
            return device_raw_token, pair["name"]


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


def log_line_message(cur, event_id: str | None, direction: str, message_type: str,
                     line_user_id: str | None, student_id: int | None, message: str | None,
                     status: str, error: str | None = None) -> None:
    cur.execute(
        """INSERT INTO line_message_logs
           (event_id,direction,message_type,line_user_id,student_id,message,status,error_message)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (event_id, direction, message_type, line_user_id, student_id, message, status, error),
    )


def verify_line_signature(body: bytes, signature: str | None) -> bool:
    if not LINE_CHANNEL_SECRET or not signature:
        return False
    digest = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def line_profile(line_user_id: str) -> str:
    if not LINE_CHANNEL_ACCESS_TOKEN:
        return ""
    try:
        r = requests.get(
            f"https://api.line.me/v2/bot/profile/{line_user_id}",
            headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
            timeout=10,
        )
        if r.ok:
            return (r.json().get("displayName") or "").strip()
    except requests.RequestException:
        pass
    return ""


def reply_line(reply_token: str, message: str, event_id: str | None, line_user_id: str | None, student_id: int | None = None) -> bool:
    with db_conn() as conn:
        with conn.cursor() as cur:
            if not LINE_CHANNEL_ACCESS_TOKEN:
                log_line_message(cur, event_id, "outbound", "reply", line_user_id, student_id, message, "failed", "LINE_CHANNEL_ACCESS_TOKEN 未設定")
                conn.commit()
                return False
            try:
                response = requests.post(
                    "https://api.line.me/v2/bot/message/reply",
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
                    json={"replyToken": reply_token, "messages": [{"type": "text", "text": message}]},
                    timeout=10,
                )
                if 200 <= response.status_code < 300:
                    log_line_message(cur, event_id, "outbound", "reply", line_user_id, student_id, message, "sent")
                    conn.commit()
                    return True
                log_line_message(cur, event_id, "outbound", "reply", line_user_id, student_id, message, "failed", f"HTTP {response.status_code}: {response.text[:500]}")
                conn.commit()
                return False
            except requests.RequestException as exc:
                log_line_message(cur, event_id, "outbound", "reply", line_user_id, student_id, message, "failed", str(exc))
                conn.commit()
                return False


def make_binding_code() -> str:
    # 保留舊版函式，供資料庫相容；正式綁定改用一次性連結。
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


def hash_bind_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def make_binding_token() -> str:
    return secrets.token_urlsafe(32)


def create_binding_token(student_id: int, created_by: str = "admin") -> tuple[str, datetime]:
    token = make_binding_token()
    expires = now_local() + timedelta(minutes=BINDING_LINK_MINUTES)
    now = now_local()
    with db_conn() as conn:
        with conn.cursor() as cur:
            # 同一學生只保留最新一條有效邀請連結；但這一條連結在有效期限內可以讓多位家長使用。
            cur.execute(
                "UPDATE line_bind_tokens SET expires_at=%s WHERE student_id=%s AND used_at IS NULL AND expires_at>%s",
                (now, student_id, now),
            )
            cur.execute(
                "INSERT INTO line_bind_tokens(student_id,token_hash,token_value,expires_at,use_count,last_used_at,created_by) VALUES (%s,%s,%s,%s,0,NULL,%s)",
                (student_id, hash_bind_token(token), token, expires, created_by),
            )
            conn.commit()
    return token, expires


def get_or_create_binding_token(student_id: int, created_by: str = "system") -> tuple[str, datetime, bool]:
    now = now_local()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT token_value,expires_at FROM line_bind_tokens WHERE student_id=%s AND used_at IS NULL AND expires_at>%s AND token_value IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                (student_id, now),
            )
            row = cur.fetchone()
    if row and row["token_value"]:
        return row["token_value"], row["expires_at"], False
    token, expires = create_binding_token(student_id, created_by)
    return token, expires, True

def binding_link(token: str) -> str:
    if not LIFF_ID:
        return ""
    # 透過 LIFF URL 開啟，讓家長在 LINE 內完成 LINE 身分驗證。
    return f"https://liff.line.me/{LIFF_ID}/bind?token={token}"


def active_line_recipients(cur, student_id: int, legacy_line_user_id: str | None = None) -> list[str]:
    cur.execute(
        "SELECT DISTINCT line_user_id FROM student_line_bindings WHERE student_id=%s AND active=TRUE ORDER BY id",
        (student_id,),
    )
    ids = [r["line_user_id"] for r in cur.fetchall() if r["line_user_id"]]
    if legacy_line_user_id and legacy_line_user_id not in ids:
        ids.append(legacy_line_user_id)
    return ids


def send_student_line(cur, attendance_id: int | None, student_id: int, message: str, notification_type: str, legacy_line_user_id: str | None = None) -> bool:
    recipients = active_line_recipients(cur, student_id, legacy_line_user_id)
    if not recipients:
        # 沒有收件人仍留一筆記錄，方便後台知道為何沒發出去。
        log_notification(cur, attendance_id, notification_type, None, message, LINE_MODE, "failed", "學生尚未綁定 LINE")
        return False
    results = []
    for rid in recipients:
        results.append(send_line(cur, attendance_id, rid, message, notification_type))
    return all(results)


def verify_liff_id_token(id_token: str, expected_user_id: str | None = None) -> dict[str, Any]:
    if not LINE_LOGIN_CHANNEL_ID:
        raise HTTPException(500, "尚未設定 LINE_LOGIN_CHANNEL_ID")
    if not id_token:
        raise HTTPException(400, "缺少 LINE ID Token")
    try:
        response = requests.post(
            "https://api.line.me/oauth2/v2.1/verify",
            data={"id_token": id_token, "client_id": LINE_LOGIN_CHANNEL_ID},
            timeout=10,
        )
    except requests.RequestException as exc:
        raise HTTPException(502, f"LINE ID Token 驗證失敗：{exc}") from exc
    if not response.ok:
        raise HTTPException(401, "LINE ID Token 無效或已過期")
    data = response.json()
    user_id = data.get("sub")
    if not user_id:
        raise HTTPException(401, "LINE 驗證結果缺少使用者 ID")
    if expected_user_id and user_id != expected_user_id:
        raise HTTPException(401, "LINE 使用者身分不一致")
    return data


def binding_token_info(cur, token: str) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT bt.*, s.name, s.student_code, s.active AS student_active
        FROM line_bind_tokens bt
        JOIN students s ON s.id=bt.student_id
        WHERE bt.token_hash=%s AND bt.used_at IS NULL AND bt.expires_at>%s
        """,
        (hash_bind_token(token), now_local()),
    )
    return cur.fetchone()


def bind_liff_user(token: str, id_token: str, relation: str = "家長/監護人") -> dict[str, Any]:
    profile = verify_liff_id_token(id_token)
    line_user_id = profile["sub"]
    display_name = (profile.get("name") or "").strip()[:100] or None
    now = now_local()
    with db_conn() as conn:
        with conn.cursor() as cur:
            row = binding_token_info(cur, token)
            if not row:
                raise HTTPException(400, "綁定連結無效或已過期。請請管理員重新產生連結。")
            cur.execute(
                "SELECT id FROM student_line_bindings WHERE student_id=%s AND line_user_id=%s AND active=TRUE",
                (row["student_id"], line_user_id),
            )
            existing = cur.fetchone()
            # 同一條連結 1 小時內可讓多位家長使用；同一 LINE 也可綁多位孩子。
            if existing:
                cur.execute(
                    "UPDATE line_bind_tokens SET use_count=use_count+1,last_used_at=%s WHERE id=%s",
                    (now, row["id"]),
                )
                conn.commit()
                return {"student_id": row["student_id"], "student_name": row["name"], "already": True, "line_user_id": line_user_id}
            cur.execute(
                "INSERT INTO student_line_bindings(student_id,line_user_id,display_name,relation,active,bound_at,bound_by) VALUES (%s,%s,%s,%s,TRUE,%s,'LIFF')",
                (row["student_id"], line_user_id, display_name, relation.strip() or "家長/監護人", now),
            )
            cur.execute("UPDATE students SET line_user_id=COALESCE(line_user_id,%s) WHERE id=%s", (line_user_id, row["student_id"]))
            cur.execute(
                "UPDATE line_bind_tokens SET use_count=use_count+1,last_used_at=%s WHERE id=%s",
                (now, row["id"]),
            )
            conn.commit()
    return {"student_id": row["student_id"], "student_name": row["name"], "already": False, "line_user_id": line_user_id}


def unbind_line_binding(binding_id: int) -> tuple[bool, str]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT b.student_id,b.line_user_id,s.name
                FROM student_line_bindings b JOIN students s ON s.id=b.student_id
                WHERE b.id=%s AND b.active=TRUE
                """,
                (binding_id,),
            )
            row = cur.fetchone()
            if not row:
                return False, "找不到有效的綁定。"
            cur.execute("UPDATE student_line_bindings SET active=FALSE WHERE id=%s", (binding_id,))
            # 若舊相容欄位就是被解除的帳號，清除；若還有其他綁定，改放其他有效帳號。
            cur.execute("SELECT line_user_id FROM student_line_bindings WHERE student_id=%s AND active=TRUE ORDER BY id LIMIT 1", (row["student_id"],))
            other = cur.fetchone()
            cur.execute("UPDATE students SET line_user_id=%s WHERE id=%s", (other["line_user_id"] if other else None, row["student_id"]))
            conn.commit()
            return True, row["name"]


async def process_line_event(event: dict[str, Any]) -> None:
    event_id = event.get("webhookEventId")
    source = event.get("source") or {}
    line_user_id = source.get("userId")
    event_type = event.get("type")
    if event_type == "follow":
        msg = "👋 歡迎加入官方帳號！\n\n請使用管理員提供的「家長綁定連結」完成學生綁定。\n\n綁定完成後，學生到班與離班通知會自動送到此 LINE。"
        if LINE_MODE == "live" and event.get("replyToken"):
            reply_line(event["replyToken"], msg, event_id, line_user_id)
        return
    if event_type != "message":
        return
    message = event.get("message") or {}
    msg_type = message.get("type", "unknown")
    text = message.get("text", "").strip() if msg_type == "text" else ""
    student_id = None
    with db_conn() as conn:
        with conn.cursor() as cur:
            if line_user_id:
                cur.execute(
                    "SELECT student_id FROM student_line_bindings WHERE line_user_id=%s AND active=TRUE ORDER BY id LIMIT 1",
                    (line_user_id,),
                )
                row = cur.fetchone()
                if row:
                    student_id = row["student_id"]
                else:
                    cur.execute("SELECT id FROM students WHERE line_user_id=%s", (line_user_id,))
                    row = cur.fetchone()
                    student_id = row["id"] if row else None
            log_line_message(cur, event_id, "inbound", msg_type, line_user_id, student_id, text or f"[{msg_type}]", "received")
            conn.commit()
    if not line_user_id or not event.get("replyToken"):
        return
    if text in {"測試", "測試通知"}:
        reply = "✅ 已收到你的測試訊息。\nLINE 內外訊息連線正常。"
    elif text in {"我的ID", "查ID"}:
        reply = f"你的 LINE 使用者 ID：\n{line_user_id}\n\n內測用途，請勿公開貼出。"
    else:
        reply = "✅ 已收到訊息。\n請使用管理員提供的「家長綁定連結」完成學生綁定；管理員可在後台解除綁定。"
    if LINE_MODE == "live":
        reply_line(event["replyToken"], reply, event_id, line_user_id, student_id)

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
        log_line_message(cur, None, "outbound", "push", recipient, None, message, "simulated")
        return True
    if not recipient:
        log_notification(cur, attendance_id, notification_type, None, message, "live", "failed", "未設定 LINE User ID")
        log_line_message(cur, None, "outbound", "push", None, None, message, "failed", "未設定 LINE User ID")
        return False
    if not LINE_CHANNEL_ACCESS_TOKEN:
        log_notification(cur, attendance_id, notification_type, recipient, message, "live", "failed", "LINE_CHANNEL_ACCESS_TOKEN 未設定")
        log_line_message(cur, None, "outbound", "push", recipient, None, message, "failed", "LINE_CHANNEL_ACCESS_TOKEN 未設定")
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
            log_line_message(cur, None, "outbound", "push", recipient, None, message, "sent")
            return True
        log_notification(cur, attendance_id, notification_type, recipient, message, "live", "failed", f"HTTP {response.status_code}: {response.text[:500]}")
        log_line_message(cur, None, "outbound", "push", recipient, None, message, "failed", f"HTTP {response.status_code}")
        return False
    except requests.RequestException as exc:
        log_notification(cur, attendance_id, notification_type, recipient, message, "live", "failed", str(exc))
        log_line_message(cur, None, "outbound", "push", recipient, None, message, "failed", str(exc))
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
                    send_student_template_line(cur, attendance_id, student, course, "check_out", check_in_time=attendance["check_in_time"], check_out_time=when, when=when, legacy_line_user_id=student["line_user_id"])
                    conn.commit()
                    return {"kind": "check_out", "student": student["name"], "course": course["course_name"], "time": iso(when), "check_in": iso(attendance["check_in_time"])}

            # 到班通知（含原本先未到、後來補打卡的情況）
            cur.execute("SELECT * FROM attendance WHERE id=%s", (attendance_id,))
            final_a = cur.fetchone()
            send_student_template_line(cur, attendance_id, student, course, "check_in", check_in_time=final_a["check_in_time"], when=final_a["check_in_time"], legacy_line_user_id=student["line_user_id"])
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
    return "<div class='nav'><a class='btn btn2' href='/admin'>今日出勤</a><a class='btn btn2' href='/admin/courses'>課程時間</a><a class='btn btn2' href='/admin/line'>LINE 綁定 / 測試</a><a class='btn btn2' href='/admin/devices'>教室設備</a><a class='btn btn2' href='/admin/templates'>通知範本</a><a class='btn btn2' href='/admin/export.csv'>匯出 CSV</a></div>"


@app.get("/", response_class=HTMLResponse)
def root(_: str = Depends(admin_auth)):
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def dashboard(_: str = Depends(admin_auth)):
    checks = run_all_checks()
    today = now_local().date()
    body_parts = [
        f"<div class='top'><div><h1>出勤測試系統 V0.4.1</h1><div class='muted'>Render 隔離測試站｜LINE：{escape(LINE_MODE_LABELS.get(LINE_MODE, LINE_MODE))}</div></div>{admin_nav()}</div>",
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
        with db_conn() as c2:
            with c2.cursor() as ccur:
                ccur.execute(
                    "SELECT id,line_user_id,display_name,relation,bound_at FROM student_line_bindings WHERE student_id=%s AND active=TRUE ORDER BY id",
                    (s["id"],),
                )
                binds = ccur.fetchall()
        bind_text = "<br>".join(
            f"{escape(b['display_name'] or 'LINE 使用者')}｜{escape(b['relation'])}｜{escape(b['line_user_id'])} <form style='display:inline' method='post' action='/admin/student/{s['id']}/binding/{b['id']}/unbind'><button class='btn btn2 mini'>解除</button></form>"
            for b in binds
        ) or "尚未綁定"
        test_btn = f"<form method='post' action='/admin/test-line/{s['id']}'><button>測試 LINE</button></form>" if binds or s['line_user_id'] else "尚未綁定"
        qr_rows.append(
            f"<tr><td>{escape(s['name'])}</td><td>{escape(s['student_code'])}</td>"
            f"<td><img class='qr' src='/qr/{escape(s['student_code'])}.png'></td>"
            f"<td>{bind_text}</td>"
            f"<td><form method='post' action='/admin/student/{s['id']}/binding-link'><button class='btn btn2 mini'>產生家長綁定連結</button></form></td>"
            f"<td>{test_btn}</td></tr>"
        )
    body_parts.append("<section><h2>學生 QR / 家長 LINE</h2><p class='muted mini'>學生的 QR 只負責出勤。家長 LINE 透過一次性綁定連結與學生建立關聯；一位學生可綁定多位家長，同一個 LINE 也可綁定多位孩子。解除綁定只由管理員操作。</p><table><tr><th>學生</th><th>編號</th><th>學生 QR</th><th>已綁定 LINE</th><th>綁定連結</th><th>測試</th></tr>" + "".join(qr_rows) + "</table></section>")

    roster_rows = []
    for r in roster:
        a = r["attendance"]
        late = a["late_minutes"] if a else 0
        if a and a["check_out_time"]:
            status = "<span class='green'>✅ 已完成</span>"
        elif a and a["status"] == "absent" and not a["check_in_time"]:
            status = "<span class='red'>🔴 未到班</span>"
        elif late > r["late_grace_minutes"]:
            status = "<span class='orange'>🟠 遲到</span>"
        elif a and a["check_in_time"]:
            status = "<span class='gray'>✅ 已到班</span>"
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
        logs.append(f"<div style='padding:8px 0;border-bottom:1px solid #eee'><b>{n['created_at']:%m-%d %H:%M:%S}</b>｜{escape(notification_type_label(n['notification_type']))}｜{escape(notify_status_label(n['status']))}<br>{escape(n['message'])}{('<br><span class=muted>錯誤：'+escape(n['error_message'])+'</span>') if n['error_message'] else ''}</div>")
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
    <p>狀態 <select name='status'>{''.join(f"<option value='{x}' {'selected' if a['status']==x else ''}>{STATUS_LABELS[x]}</option>" for x in ['checked_in','late','completed','absent'])}</select>
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


@app.get("/admin/templates", response_class=HTMLResponse)
def admin_templates(_: str = Depends(admin_auth)):
    body=[f"<div class='top'><div><h1>LINE 通知範本</h1><div class='muted'>預設範本 + 個別學生覆寫。一次性覆寫送出成功後會自動恢復預設；期限型覆寫到期後自動恢復預設。</div></div>{admin_nav()}</div>"]
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,notification_type,template_text FROM notification_templates WHERE student_id IS NULL AND active=TRUE ORDER BY id")
            defaults=cur.fetchall()
            cur.execute("SELECT id,student_code,name FROM students WHERE active=TRUE ORDER BY name")
            students=cur.fetchall()
            cur.execute("""SELECT nt.id,nt.notification_type,nt.template_text,nt.mode,nt.remaining_uses,nt.expires_at,nt.note,s.student_code,s.name
                         FROM notification_templates nt JOIN students s ON s.id=nt.student_id
                         WHERE nt.student_id IS NOT NULL AND nt.active=TRUE
                         ORDER BY s.name,nt.notification_type,nt.created_at DESC""")
            overrides=cur.fetchall()
    body.append("<section><h2>預設範本</h2><p class='muted'>可使用變數：{greeting}、{thanks}、{student_name}、{course_name}、{teacher_name}、{scheduled_start}、{scheduled_end}、{check_in_time}、{check_out_time}、{now_time}、{late_minutes}。</p><div style='overflow:auto'><table><tr><th>通知</th><th>範本內容</th><th>操作</th></tr>")
    for r in defaults:
        body.append(f"<tr><td>{escape(TEMPLATE_TYPE_LABELS.get(r['notification_type'],r['notification_type']))}</td><td><form method='post' action='/admin/template/default'><input type='hidden' name='notification_type' value='{escape(r['notification_type'])}'><textarea name='template_text' rows='4' style='min-width:520px'>{escape(r['template_text'])}</textarea></td><td><button>儲存預設範本</button></form></td></tr>")
    body.append("</table></div></section>")
    student_options=''.join(f"<option value='{s['id']}'>{escape(s['name'])}｜{escape(s['student_code'])}</option>" for s in students)
    type_options=''.join(f"<option value='{k}'>{escape(v)}</option>" for k,v in TEMPLATE_TYPE_LABELS.items())
    body.append(f"<section><h2>新增／修改個別學生範本</h2><p class='muted'>適合臨時通知。例如：媽媽您好，采璇說日記回去想，先離開教室了，感謝媽媽😊。選「一次性」後，第一次成功送出就自動回復預設範本。</p><form method='post' action='/admin/template/student'><div class='grid'><label>學生<br><select name='student_id'>{student_options}</select></label><label>通知類型<br><select name='notification_type'>{type_options}</select></label><label>套用方式<br><select name='mode'><option value='permanent'>永久個別</option><option value='once'>一次性（送出成功後自動恢復）</option><option value='until'>期限內</option></select></label><label>剩餘次數<br><input type='number' name='remaining_uses' value='1' min='1'></label></div><p>範本內容</p><textarea name='template_text' rows='5' style='width:100%' placeholder='例如：{greeting}，{student_name}說日記回去想，先離開教室了，{thanks}😊'></textarea><p>期限（選填，格式 YYYY-MM-DD HH:MM） <input class='wide' name='expires_at' placeholder='例如 2026-09-20 20:00'>　備註 <input class='wide' name='note' placeholder='例如：今天臨時通知'></p><button>儲存個別範本</button></form></section>")
    rows=[]
    for r in overrides:
        expires = r['expires_at'].strftime('%Y-%m-%d %H:%M') if r['expires_at'] else ''
        mode_label={'permanent':'永久個別','once':'一次性','until':'期限內'}.get(r['mode'],r['mode'])
        rows.append(f"<tr><td>{escape(r['name'])}<br><span class='muted mini'>{escape(r['student_code'])}</span></td><td>{escape(TEMPLATE_TYPE_LABELS.get(r['notification_type'],r['notification_type']))}</td><td style='white-space:pre-wrap'>{escape(r['template_text'])}</td><td>{mode_label}<br>剩餘：{r['remaining_uses'] if r['remaining_uses'] is not None else '不限'}<br>有效至：{expires or '不限'}</td><td><form method='post' action='/admin/template/{r['id']}/restore'><button class='btn btn2 mini'>恢復預設</button></form></td></tr>")
    body.append("<section><h2>目前有效的個別範本</h2><div style='overflow:auto'><table><tr><th>學生</th><th>通知</th><th>內容</th><th>規則</th><th>操作</th></tr>"+''.join(rows)+"</table></div></section>")
    body.append("<section><h2>Excel 管理</h2><p>下載 CSV 後可直接用 Excel 修改。欄位支援：通知類型、學生編號、訊息範本、套用方式、剩餘次數、有效至、啟用、備註。學生編號留白代表修改預設範本。修改後請另存為「CSV UTF-8」再上傳。</p><p><a class='btn btn2' href='/admin/templates/export.csv'>下載通知範本 CSV</a></p><form method='post' action='/admin/templates/import.csv' enctype='multipart/form-data'><input type='file' name='file' accept='.csv,text/csv' required> <button>匯入通知範本 CSV</button></form></section>")
    return page("LINE 通知範本", ''.join(body))


@app.post("/admin/template/default")
def save_default_template(notification_type: str=Form(...), template_text: str=Form(...), _: str=Depends(admin_auth)):
    if notification_type not in DEFAULT_NOTIFICATION_TEMPLATES:
        raise HTTPException(400,"不支援的通知類型")
    if not template_text.strip():
        raise HTTPException(400,"範本內容不可空白")
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE notification_templates SET active=FALSE,updated_at=%s WHERE notification_type=%s AND student_id IS NULL AND active=TRUE", (now_local(),notification_type))
            cur.execute("INSERT INTO notification_templates(notification_type,student_id,template_text,mode,active,created_by) VALUES (%s,NULL,%s,'default',TRUE,'admin')", (notification_type,template_text.strip()))
            conn.commit()
    return RedirectResponse("/admin/templates", status_code=303)


def parse_template_expiry(value: str) -> datetime | None:
    value=value.strip()
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d %H:%M")


@app.post("/admin/template/student")
def save_student_template(student_id:int=Form(...), notification_type:str=Form(...), template_text:str=Form(...), mode:str=Form(...), remaining_uses:int=Form(1), expires_at:str=Form(""), note:str=Form(""), _: str=Depends(admin_auth)):
    if notification_type not in DEFAULT_NOTIFICATION_TEMPLATES:
        raise HTTPException(400,"不支援的通知類型")
    if mode not in {"permanent","once","until"}:
        raise HTTPException(400,"不支援的套用方式")
    if not template_text.strip():
        raise HTTPException(400,"範本內容不可空白")
    exp=parse_template_expiry(expires_at) if expires_at.strip() else None
    if mode=="until" and not exp:
        raise HTTPException(400,"期限內範本需要填寫有效期限")
    uses=max(1,remaining_uses) if mode=="once" else None
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE notification_templates SET active=FALSE,updated_at=%s WHERE notification_type=%s AND student_id=%s AND active=TRUE", (now_local(),notification_type,student_id))
            cur.execute("""INSERT INTO notification_templates(notification_type,student_id,template_text,mode,remaining_uses,expires_at,active,note,created_by)
                         VALUES (%s,%s,%s,%s,%s,%s,TRUE,%s,'admin')""", (notification_type,student_id,template_text.strip(),mode,uses,exp,note.strip() or None))
            conn.commit()
    return RedirectResponse("/admin/templates",status_code=303)


@app.post("/admin/template/{template_id}/restore")
def restore_student_template(template_id:int, _: str=Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE notification_templates SET active=FALSE,updated_at=%s WHERE id=%s AND student_id IS NOT NULL", (now_local(),template_id))
            conn.commit()
    return RedirectResponse("/admin/templates",status_code=303)


@app.get("/admin/templates/export.csv")
def export_notification_templates(_: str=Depends(admin_auth)):
    buf=io.StringIO(); buf.write("\ufeff"); writer=csv.writer(buf)
    writer.writerow(["通知類型","學生編號","學生姓名","訊息範本","套用方式","剩餘次數","有效至","啟用","備註"])
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT nt.notification_type,s.student_code,s.name,nt.template_text,nt.mode,nt.remaining_uses,nt.expires_at,nt.active,nt.note
                         FROM notification_templates nt LEFT JOIN students s ON s.id=nt.student_id
                         WHERE nt.active=TRUE ORDER BY s.name NULLS FIRST,nt.notification_type""")
            for r in cur.fetchall():
                mode_label={'default':'預設','permanent':'永久個別','once':'一次性','until':'期限內'}.get(r['mode'],r['mode'])
                writer.writerow([TEMPLATE_TYPE_LABELS.get(r['notification_type'],r['notification_type']),r['student_code'] or '',r['name'] or '',r['template_text'],mode_label,r['remaining_uses'] if r['remaining_uses'] is not None else '',r['expires_at'].strftime('%Y-%m-%d %H:%M') if r['expires_at'] else '', '是' if r['active'] else '否', r['note'] or ''])
    return StreamingResponse(io.BytesIO(buf.getvalue().encode('utf-8-sig')),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename=notification_templates.csv'})


@app.post("/admin/templates/import.csv")
async def import_notification_templates(file:UploadFile=File(...), _: str=Depends(admin_auth)):
    raw=await file.read()
    text=raw.decode('utf-8-sig')
    reader=csv.DictReader(io.StringIO(text))
    required={"通知類型","學生編號","訊息範本","套用方式"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        raise HTTPException(400,"CSV 欄位不足：至少需要 通知類型、學生編號、訊息範本、套用方式")
    type_map={v:k for k,v in TEMPLATE_TYPE_LABELS.items()}
    mode_map={"預設":"default","永久個別":"permanent","一次性":"once","期限內":"until"}
    true_set={"是","啟用","1","true","TRUE","yes","Y","y"}
    imported=0
    with db_conn() as conn:
        with conn.cursor() as cur:
            for raw_row in reader:
                row={str(k).strip():str(v).strip() if v is not None else '' for k,v in raw_row.items()}
                ntype=type_map.get(row.get("通知類型"), row.get("通知類型"))
                if ntype not in DEFAULT_NOTIFICATION_TEMPLATES or not row.get("訊息範本"):
                    continue
                mode=mode_map.get(row.get("套用方式"),row.get("套用方式","永久個別"))
                if mode not in {"default","permanent","once","until"}:
                    continue
                active=row.get("啟用","是") in true_set
                student_id=None
                student_code=row.get("學生編號","")
                if student_code:
                    cur.execute("SELECT id FROM students WHERE student_code=%s AND active=TRUE", (student_code,))
                    st=cur.fetchone()
                    if not st:
                        continue
                    student_id=st["id"]
                if not active:
                    # 停用目前同類型覆寫，達到手動恢復預設。
                    cur.execute("UPDATE notification_templates SET active=FALSE,updated_at=%s WHERE notification_type=%s AND student_id IS NOT DISTINCT FROM %s AND active=TRUE", (now_local(),ntype,student_id))
                    imported += 1
                    continue
                exp=parse_template_expiry(row.get("有效至","")) if row.get("有效至") else None
                if mode=="until" and not exp:
                    continue
                uses=int(row.get("剩餘次數") or 1) if mode=="once" else None
                cur.execute("UPDATE notification_templates SET active=FALSE,updated_at=%s WHERE notification_type=%s AND student_id IS NOT DISTINCT FROM %s AND active=TRUE", (now_local(),ntype,student_id))
                cur.execute("""INSERT INTO notification_templates(notification_type,student_id,template_text,mode,remaining_uses,expires_at,active,note,created_by)
                             VALUES (%s,%s,%s,%s,%s,%s,TRUE,%s,'EXCEL')""", (ntype,student_id,row["訊息範本"],mode,max(1,uses) if mode=="once" else None,exp,row.get("備註") or None))
                imported += 1
            conn.commit()
    return RedirectResponse("/admin/templates",status_code=303)


@app.get("/admin/line", response_class=HTMLResponse)
def admin_line(_: str = Depends(admin_auth)):
    body=[f"<div class='top'><div><h1>LINE 綁定 / 測試</h1><div class='muted'>第一次綁定由系統建立 1 小時有效的家長連結；同一學生可綁定多位家長，同一個 LINE 也可綁定多位學生。</div></div>{admin_nav()}</div>"]
    webhook_url=f"{public_base_url()}/webhook/line"
    liff_endpoint=f"{public_base_url()}/liff/bind"
    body.append(
        f"<section><h2>LINE 設定</h2><p>Webhook URL：<code>{escape(webhook_url)}</code></p>"
        f"<p>LIFF Endpoint URL：<code>{escape(liff_endpoint)}</code></p>"
        f"<p>LIFF ID：<b>{'已設定' if LIFF_ID else '尚未設定'}</b>｜LINE Login Channel ID：<b>{'已設定' if LINE_LOGIN_CHANNEL_ID else '尚未設定'}</b></p>"
        f"<p>目前 LINE 模式：<b>{escape(LINE_MODE_LABELS.get(LINE_MODE, LINE_MODE))}</b>｜家長綁定連結：<b>{BINDING_LINK_MINUTES} 分鐘</b></p>"
        f"<div class='alert'>第一次綁定：系統會自動為每位學生建立 1 小時有效的家長綁定連結，管理員把連結傳給家長即可。這條連結在有效時間內可讓多位家長綁定同一學生；同一位家長也可以使用不同學生的連結，把多位孩子綁到同一個 LINE。重新綁定或新增綁定人，後續由管理員維護即可。</div>"
        f"<p><a class='btn btn2' href='/admin/line/export.csv'>下載 LINE 綁定表（Excel 可直接開啟的 CSV）</a></p></section>"
    )
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,name,student_code FROM students WHERE active=TRUE ORDER BY name")
            students=cur.fetchall()
            bindings=[]
            for s in students:
                # 自動建立第一次綁定連結；若已有仍有效連結，get_or_create_binding_token 會建立新連結，因此只在需要顯示時生成。
                token, expires, _ = get_or_create_binding_token(s["id"], "admin_auto")
                cur.execute(
                    "SELECT id,line_user_id,display_name,relation,bound_at FROM student_line_bindings WHERE student_id=%s AND active=TRUE ORDER BY id",
                    (s["id"],),
                )
                binds=cur.fetchall()
                link=binding_link(token)
                bindings.append((s, binds, link, expires))
            cur.execute("SELECT * FROM line_message_logs ORDER BY id DESC LIMIT 50")
            rows=cur.fetchall()

    cards=[]
    for s, binds, link, expires in bindings:
        bind_text = "<br>".join(
            f"{escape(b['display_name'] or 'LINE 使用者')}｜{escape(b['relation'])}｜<span class='mini'>{escape(b['line_user_id'])}</span> <form style='display:inline' method='post' action='/admin/student/{s['id']}/binding/{b['id']}/unbind'><button class='btn btn2 mini'>解除</button></form>"
            for b in binds
        ) or "尚未綁定"
        test_btn = f"<form method='post' action='/admin/test-line/{s['id']}'><button>測試 LINE</button></form>" if binds or s.get('line_user_id') else "尚未綁定"
        if link:
            qr_src = qr_data_uri(link)
            linkbox = (
                f"<div style='display:flex;gap:12px;align-items:center;flex-wrap:wrap'>"
                f"<img class='qr' src='{qr_src}' alt='家長綁定 QR'>"
                f"<div style='flex:1;min-width:260px'><textarea rows='2' style='width:100%' readonly>{escape(link)}</textarea>"
                f"<div class='mini muted'>有效至 {expires:%Y-%m-%d %H:%M}（台灣時間）</div>"
                f"<form method='post' action='/admin/student/{s['id']}/binding-link'><button class='btn btn2 mini'>重新產生連結＋QR</button></form></div></div>"
            )
        else:
            linkbox = "尚未設定 LIFF_ID"
        cards.append(
            f"<tr><td>{escape(s['name'])}<br><span class='muted mini'>{escape(s['student_code'])}</span></td>"
            f"<td>{bind_text}</td><td>{linkbox}</td><td>{test_btn}</td></tr>"
        )
    body.append("<section><h2>學生與家長 LINE 綁定</h2><p class='muted'>開啟本頁時，若學生沒有有效的綁定連結，系統會自動建立一條 1 小時有效連結。家長使用後，連結在期限內仍可供其他家長使用。</p><div style='overflow:auto'><table><tr><th>學生</th><th>目前綁定人</th><th>第一次／新增家長連結</th><th>測試</th></tr>" + "".join(cards) + "</table></div></section>")

    table="<section><h2>最近 LINE 訊息</h2><table><tr><th>時間</th><th>方向</th><th>類型</th><th>LINE User ID</th><th>訊息</th><th>狀態</th></tr>"
    for r in rows:
        dlabel="收到" if r['direction']=='inbound' else "送出"
        mtype={"push":"主動推送","reply":"回覆","text":"文字"}.get(r['message_type'],r['message_type'])
        slabel={"received":"已收到","sent":"已送出","simulated":"模擬送出","failed":"失敗"}.get(r['status'],r['status'])
        table += f"<tr><td>{r['created_at']}</td><td>{dlabel}</td><td>{mtype}</td><td class='mini'>{escape(r['line_user_id'] or '')}</td><td>{escape(r['message'] or '')}</td><td>{slabel}</td></tr>"
    table += "</table></section>"
    body.append(table)
    body.append("<section><h2>用 Excel 管理綁定</h2><p>把上方的 CSV 下載後，用 Excel 修改「LINE User ID、關係、啟用」等欄位，另存為 CSV UTF-8，再由下方按鈕上傳。可用來重新綁定或新增同一學生的其他家長。</p><form method='post' action='/admin/line/import.csv' enctype='multipart/form-data'><input type='file' name='file' accept='.csv,text/csv' required> <button>匯入綁定 CSV</button></form><p class='mini muted'>欄位：學生編號、學生姓名、LINE 顯示名稱、LINE User ID、關係、啟用。匯入時空白的 LINE User ID 會略過。</p></section>")
    return page("LINE 綁定 / 測試", "".join(body))


@app.post("/admin/student/{student_id}/binding-link", response_class=HTMLResponse)
def generate_binding_link(student_id:int, admin_user: str=Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,name,student_code FROM students WHERE id=%s AND active=TRUE", (student_id,))
            student=cur.fetchone()
    if not student:
        raise HTTPException(404,"找不到學生")
    token, expires = create_binding_token(student_id, admin_user)
    link = binding_link(token)
    if not link:
        body = f"<section><h1>無法產生綁定連結</h1><div class='alert danger'>尚未設定 LIFF_ID。請先在 Render Environment Variables 設定 LIFF_ID。</div><a class='btn btn2' href='/admin/line'>返回 LINE 綁定</a></section>"
    else:
        qr_src = qr_data_uri(link)
        body = (
            f"<section><h1>家長綁定連結</h1><p>學生：<b>{escape(student['name'])}</b>（{escape(student['student_code'])}）</p>"
            f"<p>有效期限：<b>{expires:%Y-%m-%d %H:%M}</b>（台灣時間）</p>"
            f"<div style='display:flex;gap:18px;align-items:center;flex-wrap:wrap;margin:18px 0'>"
            f"<img class='qr' src='{qr_src}' alt='家長綁定 QR' style='width:180px;height:180px'>"
            f"<div style='flex:1;min-width:280px'><p>家長可以直接掃描 QR，也可以點擊下方連結。此連結在有效期限內可供多位家長使用；若一個 LINE 有多位孩子，請分別使用各學生的連結。</p>"
            f"<textarea rows='4' style='width:100%' readonly>{escape(link)}</textarea></div></div>"
            f"<p><a class='btn' href='{escape(link)}' target='_blank'>開啟綁定頁</a> <a class='btn btn2' href='/admin/line'>返回 LINE 綁定</a></p></section>"
        )
    return page("家長綁定連結", body)


@app.post("/admin/student/{student_id}/binding/{binding_id}/unbind")
def admin_unbind(student_id: int, binding_id: int, _: str=Depends(admin_auth)):
    ok, name = unbind_line_binding(binding_id)
    if not ok:
        raise HTTPException(404, name)
    return RedirectResponse("/admin/line", status_code=303)


@app.post("/admin/test-line/{student_id}")
def test_line(student_id:int, _: str=Depends(admin_auth)):
    now=now_local()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM students WHERE id=%s AND active=TRUE", (student_id,))
            s=cur.fetchone()
            if not s:
                raise HTTPException(404,"找不到學生")
            msg=f"🔔 LINE 連線測試\n學生：{s['name']}\n時間：{now:%H:%M:%S}\n這是一則測試通知。"
            send_student_line(cur,None,s['id'],msg,"line_test",s['line_user_id'])
            conn.commit()
    return RedirectResponse("/admin/line", status_code=303)


# 保留管理員手動輸入 ID 的相容端點，但正式介面不提供此操作。
@app.post("/admin/student/{student_id}/line")
def update_student_line(student_id: int, line_user_id: str = Form(""), _: str = Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE students SET line_user_id=%s WHERE id=%s", (line_user_id.strip() or None, student_id))
            if cur.rowcount == 0:
                raise HTTPException(404, "找不到學生")
            conn.commit()
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/line/export.csv")
def export_line_bindings(_: str = Depends(admin_auth)):
    buf = io.StringIO(); buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow(["學生編號","學生姓名","LINE 顯示名稱","LINE User ID","關係","啟用"] )
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.student_code,s.name,b.display_name,b.line_user_id,b.relation,b.active FROM student_line_bindings b JOIN students s ON s.id=b.student_id ORDER BY s.name,b.id"
            )
            for r in cur.fetchall():
                writer.writerow([r["student_code"],r["name"],r["display_name"] or "",r["line_user_id"],r["relation"],"是" if r["active"] else "否"])
    data=buf.getvalue().encode("utf-8-sig")
    return StreamingResponse(io.BytesIO(data), media_type="text/csv; charset=utf-8", headers={"Content-Disposition":"attachment; filename=line_bindings.csv"})


@app.post("/admin/line/import.csv")
async def import_line_bindings(file: UploadFile = File(...), _: str = Depends(admin_auth)):
    raw = await file.read()
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    required = {"學生編號","LINE User ID"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        raise HTTPException(400, "CSV 欄位不完整，至少需要：學生編號、LINE User ID")
    allowed_true = {"是","啟用","1","true","TRUE","yes","Y","y"}
    imported=0
    with db_conn() as conn:
        with conn.cursor() as cur:
            for raw_row in reader:
                row={str(k).strip(): (str(v).strip() if v is not None else "") for k,v in raw_row.items()}
                student_code=row.get("學生編號", "")
                line_user_id=row.get("LINE User ID", "")
                if not student_code or not line_user_id:
                    continue
                cur.execute("SELECT id,name FROM students WHERE student_code=%s AND active=TRUE", (student_code,))
                student=cur.fetchone()
                if not student:
                    continue
                display_name=row.get("LINE 顯示名稱") or None
                relation=row.get("關係") or "家長/監護人"
                active=row.get("啟用", "是") in allowed_true
                cur.execute("SELECT id FROM student_line_bindings WHERE student_id=%s AND line_user_id=%s", (student["id"], line_user_id))
                b=cur.fetchone()
                if b:
                    cur.execute("UPDATE student_line_bindings SET display_name=%s,relation=%s,active=%s WHERE id=%s", (display_name,relation,active,b["id"]))
                else:
                    cur.execute("INSERT INTO student_line_bindings(student_id,line_user_id,display_name,relation,active,bound_at,bound_by) VALUES (%s,%s,%s,%s,%s,%s,'EXCEL')", (student["id"],line_user_id,display_name,relation,active,now_local()))
                # 每次匯入後重新整理 students.line_user_id 相容欄位，避免停用後仍誤發通知。
                cur.execute(
                    "SELECT line_user_id FROM student_line_bindings WHERE student_id=%s AND active=TRUE ORDER BY id LIMIT 1",
                    (student["id"],),
                )
                primary = cur.fetchone()
                cur.execute("UPDATE students SET line_user_id=%s WHERE id=%s", (primary["line_user_id"] if primary else None, student["id"]))
                imported += 1
            conn.commit()
    return RedirectResponse("/admin/line", status_code=303)


@app.get("/bind/{token}")
def bind_redirect(token: str):
    if not LIFF_ID:
        return page("家長綁定", "<section><h1>尚未完成設定</h1><p>這個測試站尚未設定 LIFF。請聯絡管理員。</p></section>")
    return RedirectResponse(binding_link(token), status_code=307)


@app.get("/liff/bind", response_class=HTMLResponse)
def liff_bind_page(request: Request):
    html = f"""<!doctype html><html lang='zh-Hant'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>家長綁定</title><script src='https://static.line-scdn.net/liff/edge/2/sdk.js'></script><style>body{{font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f6f7fb;margin:0;padding:18px;color:#1f2937}}.box{{max-width:520px;margin:40px auto;background:white;border-radius:18px;padding:24px;box-shadow:0 4px 20px #0001}}button{{border:0;border-radius:10px;padding:12px 18px;background:#111827;color:white;font-size:16px}}.muted{{color:#6b7280}}.ok{{background:#ecfdf5;padding:14px;border-radius:12px}}.warn{{background:#fff7ed;padding:14px;border-radius:12px}}</style></head><body><div class='box'><h1>家長綁定</h1><div id='app'>正在確認 LINE 身分…</div></div><script>
const LIFF_ID={LIFF_ID!r};
function bindingToken(){{
  const p=new URLSearchParams(location.search); if(p.get('token')) return p.get('token');
  const state=p.get('liff.state'); if(state){{ try{{ const u=new URL(state, location.origin); return u.searchParams.get('token') || ''; }}catch(e){{}} }}
  return '';
}}
const token=bindingToken();
async function postJSON(url,payload){{ const r=await fetch(url,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(payload)}}); const d=await r.json().catch(()=>({{detail:'伺服器回應無法讀取'}})); if(!r.ok) throw new Error(d.detail||'操作失敗'); return d; }}
async function main(){{
  const root=document.getElementById('app');
  if(!token){{ root.innerHTML='<div class="warn">找不到有效的綁定連結。</div>'; return; }}
  try{{
    await liff.init({{liffId:LIFF_ID}});
    if(!liff.isLoggedIn()){{ liff.login({{redirectUri:location.href}}); return; }}
    const idToken=liff.getIDToken();
    if(!idToken) throw new Error('無法取得 LINE ID Token，請重新開啟連結。');
    const info=await postJSON('/api/liff/binding-preview',{{token:token,id_token:idToken}});
    root.innerHTML=`<p>請確認您要綁定的學生：</p><h2>${{info.student_name}}</h2><p class="muted">學生編號：${{info.student_code}}</p><p>這個 LINE 帳號將接收該學生的到班與離班通知。此連結在有效期限內仍可供其他家長使用。</p><button id="confirm">確認綁定</button>`;
    document.getElementById('confirm').onclick=async()=>{{
      const b=document.getElementById('confirm'); b.disabled=true; b.textContent='綁定中…';
      try{{ const result=await postJSON('/api/liff/bind',{{token:token,id_token:idToken,relation:'家長/監護人'}}); root.innerHTML=`<div class="ok"><h2>✅ 綁定成功</h2><p>學生：${{result.student_name}}</p><p>之後會接收到到班與離班通知。</p><p class="muted">這條連結在有效期限內仍可讓其他家長完成綁定。</p></div>`; }}catch(e){{ b.disabled=false; b.textContent='確認綁定'; root.innerHTML+=`<div class="warn">${{e.message}}</div>`; }}
    }};
  }}catch(e){{ root.innerHTML=`<div class="warn">${{e.message}}</div>`; }}
}}
main();</script></body></html>"""
    return HTMLResponse(html)


@app.post("/api/liff/binding-preview")
async def liff_binding_preview(payload: dict[str, Any]):
    token = str(payload.get("token") or "").strip()
    id_token = str(payload.get("id_token") or "").strip()
    verify_liff_id_token(id_token)
    with db_conn() as conn:
        with conn.cursor() as cur:
            row = binding_token_info(cur, token)
            if not row:
                raise HTTPException(400, "綁定連結無效、已使用或已過期。")
            return {"student_name": row["name"], "student_code": row["student_code"]}


@app.post("/api/liff/bind")
async def liff_bind(payload: dict[str, Any]):
    token = str(payload.get("token") or "").strip()
    id_token = str(payload.get("id_token") or "").strip()
    relation = str(payload.get("relation") or "家長/監護人").strip()[:50]
    return bind_liff_user(token, id_token, relation)


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
def scan(token: str, request: Request):
    require_checkin_device(request)
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


@app.get("/admin/devices", response_class=HTMLResponse)
def admin_devices(_: str = Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,name,active,created_at,last_used_at FROM checkin_devices ORDER BY id DESC")
            devices = cur.fetchall()
    rows=[]
    for d in devices:
        status = "啟用" if d["active"] else "停用"
        label = "停用" if d["active"] else "啟用"
        rows.append(f"<tr><td>{escape(d['name'])}</td><td>{status}</td><td>{d['created_at']}</td><td>{d['last_used_at'] or '-'}</td><td><form method='post' action='/admin/device/{d['id']}/toggle'><button class='btn btn2 mini'>{label}</button></form></td></tr>")
    body = f"<div class='top'><div><h1>教室簽到設備</h1><div class='muted'>只有已配對的手機／電腦瀏覽器才能完成學生 QR 簽到。</div></div>{admin_nav()}</div>"
    body += "<section><h2>建立設備配對</h2><form method='post' action='/admin/devices/new'><label>設備名稱 <input class='wide' name='name' value='教室手機' required></label> <button>產生 30 分鐘配對連結＋QR</button></form><p class='mini muted'>這裡就是設備配對連結的管理位置。管理員產生後，教室手機直接掃 QR 即可，不需要另外填 Render 環境變數或手動輸入 code；配對成功後這條連結立即失效。</p></section>"
    body += "<section><h2>已授權設備</h2><table><tr><th>設備</th><th>狀態</th><th>建立時間</th><th>最後使用</th><th>操作</th></tr>"+"".join(rows)+"</table></section>"
    return page("教室簽到設備", body)


@app.post("/admin/devices/new", response_class=HTMLResponse)
def admin_new_device(name: str = Form("教室手機"), _: str = Depends(admin_auth)):
    pair_token, expires, _device_id = create_device_pairing(name)
    link = f"{public_base_url()}/device/pair?code={pair_token}"
    qr_src = qr_data_uri(link)
    body = (
        f"<section><h1>設備配對</h1><p>設備：<b>{escape(name.strip() or '教室手機')}</b></p>"
        f"<p>有效至：<b>{expires:%Y-%m-%d %H:%M}</b>（台灣時間）</p>"
        f"<div style='display:flex;gap:18px;align-items:center;flex-wrap:wrap;margin:18px 0'>"
        f"<img class='qr' src='{qr_src}' alt='設備配對 QR' style='width:180px;height:180px'>"
        f"<div style='flex:1;min-width:280px'><p><b>設備配對 code 不需要另外填到 Render。</b><br>它就在下方連結的 <code>code=...</code> 中。教室手機直接掃左側 QR 即可完成配對；成功後此 code 立即失效。</p>"
        f"<textarea rows='3' style='width:100%' readonly>{escape(link)}</textarea></div></div>"
        f"<p><a class='btn' href='{escape(link)}' target='_blank'>在本機開啟配對</a> <a class='btn btn2' href='/admin/devices'>返回</a></p></section>"
    )
    return page("設備配對", body)


@app.post("/admin/device/{device_id}/toggle")
def toggle_device(device_id: int, _: str = Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE checkin_devices SET active=NOT active WHERE id=%s", (device_id,))
            conn.commit()
    return RedirectResponse("/admin/devices", status_code=303)


@app.get("/device/pair", response_class=HTMLResponse)
def device_pair(code: str):
    raw_token, name = pair_device(code)
    response = RedirectResponse("/device", status_code=303)
    response.set_cookie(DEVICE_COOKIE_NAME, raw_token, httponly=True, secure=True, samesite="lax", max_age=60*60*24*30)
    return response


@app.get("/device", response_class=HTMLResponse)
def device_home(request: Request):
    device = require_checkin_device(request)
    body = f"<section style='max-width:680px;margin:40px auto;text-align:center'><h1>教室簽到設備</h1><p>設備：<b>{escape(device['name'])}</b></p><div class='alert success'>✅ 本機已授權</div><p>現在可以用手機內建相機掃描學生 QR。請固定使用目前這個瀏覽器。</p><p class='mini muted'>如果清除 Cookie、換瀏覽器或換手機，需要重新配對。</p><p><a class='btn btn2' href='/admin/devices'>管理員設備頁</a></p></section>"
    return page("教室簽到設備", body)


@app.get("/admin/export.csv")
def export_csv(_: str = Depends(admin_auth)):
    buf = io.StringIO(); buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow(["紀錄ID","日期","學生編號","學生姓名","課程","老師","預定開始","預定結束","到班","離班","狀態","狀態代碼","遲到分鐘","校正備註","校正時間","校正者"])
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
                writer.writerow([r[k] for k in ["id","date","student_code","name","course_name","teacher_name","start_time","end_time","check_in_time","check_out_time"]] + [status_label(r["status"]), r["status"], r["late_minutes"], r["manual_note"], r["adjusted_at"], r["adjusted_by"]])
    data = buf.getvalue().encode("utf-8-sig")
    return StreamingResponse(io.BytesIO(data), media_type="text/csv; charset=utf-8", headers={"Content-Disposition":"attachment; filename=attendance_test_export.csv"})


@app.post("/webhook/line")
async def line_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("x-line-signature")
    if LINE_MODE == "live":
        if not verify_line_signature(body, signature):
            raise HTTPException(status_code=400, detail="LINE webhook signature 驗證失敗")
    try:
        payload = __import__("json").loads(body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="無效 JSON")
    events = payload.get("events") or []
    if LINE_MODE == "live":
        await asyncio.gather(*(process_line_event(e) for e in events))
    return JSONResponse({"ok": True, "events": len(events)})


@app.get("/health")
def health():
    return JSONResponse({"ok": True, "line_mode": LINE_MODE, "database": "postgres", "version": "0.4.0"})
