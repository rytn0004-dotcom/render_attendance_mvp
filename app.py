from __future__ import annotations

import asyncio
import csv
import json
import time
import io
import zipfile
import xml.etree.ElementTree as ET
import os
import secrets
import hmac
import hashlib
import re
import base64
import logging
from functools import lru_cache
from datetime import date, datetime, timedelta
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

import qrcode
import requests
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Form, HTTPException, Request, Depends, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

APP_VERSION = "0.7.0"
logger = logging.getLogger("attendance")
LINE_MODE = os.getenv("LINE_MODE", "live").strip().lower()
if LINE_MODE in {"production", "prod", "正式"}:
    LINE_MODE = "live"
elif LINE_MODE in {"mock", "test", "testing", "模擬"}:
    LINE_MODE = "simulation"
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
SEED_DEMO_DATA = os.getenv("SEED_DEMO_DATA", "false").strip().lower() in {"1", "true", "yes", "y", "是", "啟用"}
NOTIFICATION_RETRY_COOLDOWN_MINUTES = max(1, int(os.getenv("NOTIFICATION_RETRY_COOLDOWN_MINUTES", "15")))

# 主總表自動同步：正式環境建議使用 Google Drive 的私有檔案 + Service Account 讀取。
MASTER_SYNC_ENABLED = os.getenv("MASTER_SYNC_ENABLED", "true").strip().lower() in {"1", "true", "yes", "y", "是", "啟用"}
MASTER_SYNC_PROVIDER_RAW = os.getenv("MASTER_SYNC_PROVIDER", "google_drive").strip().lower()
# "google_sheets" is a user-facing alias; the implementation reads through
# Google Drive and supports both native Google Sheets and private XLSX files.
MASTER_SYNC_PROVIDER = "google_drive" if MASTER_SYNC_PROVIDER_RAW == "google_sheets" else MASTER_SYNC_PROVIDER_RAW
MASTER_SYNC_INTERVAL_MINUTES = max(5, int(os.getenv("MASTER_SYNC_INTERVAL_MINUTES", "10")))
MASTER_SYNC_ON_STARTUP = os.getenv("MASTER_SYNC_ON_STARTUP", "true").strip().lower() in {"1", "true", "yes", "y", "是", "啟用"}
MASTER_SYNC_MAX_MB = max(1, int(os.getenv("MASTER_SYNC_MAX_MB", "20")))
GOOGLE_DRIVE_FILE_ID = os.getenv("GOOGLE_DRIVE_FILE_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON_BASE64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_BASE64", "").strip()
MASTER_SYNC_LOCK_KEY = "attendance-master-excel-sync-v1"

RUNTIME_SETTINGS = {
    "出勤模組": "啟用",
    "出勤課程來源": "實際課程",
    "遲到門檻（分鐘）": DEFAULT_LATE_GRACE_MINUTES if "DEFAULT_LATE_GRACE_MINUTES" in globals() else 10,
    "未到班通知（分鐘）": DEFAULT_LATE_GRACE_MINUTES,
    "未離班通知（分鐘）": DEFAULT_CHECKOUT_GRACE_MINUTES,
    "提前簽到（分鐘）": CHECKIN_EARLY_MINUTES if "CHECKIN_EARLY_MINUTES" in globals() else 60,
    "重複掃描保護（分鐘）": 10,
    "教室設備限制": "啟用",
    "學生 QR": "固定",
}

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

app = FastAPI(title=f"Attendance Test MVP v{APP_VERSION}")
security = HTTPBasic()

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    error_id = secrets.token_hex(6)
    logger.exception("Unhandled exception [%s] path=%s", error_id, request.url.path)
    if request.url.path.startswith("/admin"):
        detail = escape(str(exc)[:900])
        return HTMLResponse(
            page("系統錯誤", f"<section><h1>系統發生錯誤</h1><div class='alert danger'>❌ {detail}</div><p>錯誤編號：<code>{error_id}</code></p><p>程式版本：<b>V{APP_VERSION}</b></p><p><a class='btn btn2' href='/admin/diagnostics'>開啟系統診斷</a> <a class='btn btn2' href='/health'>檢查版本</a></p></section>"),
            status_code=500,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse({"ok": False, "error": "internal_server_error", "error_id": error_id}, status_code=500)

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
    "disabled": "已關閉",
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


def ensure_column(cur, table: str, column: str, definition: str) -> None:
    """Ensure one known schema column exists. Table/column names are hard-coded callers only."""
    cur.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name=%s AND column_name=%s",
        (table, column),
    )
    if cur.fetchone() is None:
        cur.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')


def ensure_notification_template_schema(cur) -> None:
    """Repair/initialize notification_templates without deleting existing data."""
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_templates (
            id BIGSERIAL PRIMARY KEY,
            notification_type TEXT NOT NULL,
            student_id BIGINT,
            template_text TEXT NOT NULL DEFAULT '',
            mode TEXT NOT NULL DEFAULT 'permanent',
            remaining_uses INTEGER,
            expires_at TIMESTAMP,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            note TEXT,
            created_by TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    columns = {
        "id": "BIGSERIAL",
        "notification_type": "TEXT DEFAULT 'check_in'",
        "student_id": "BIGINT",
        "template_text": "TEXT NOT NULL DEFAULT ''",
        "mode": "TEXT NOT NULL DEFAULT 'permanent'",
        "remaining_uses": "INTEGER",
        "expires_at": "TIMESTAMP",
        "active": "BOOLEAN NOT NULL DEFAULT TRUE",
        "note": "TEXT",
        "created_by": "TEXT",
        "created_at": "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "updated_at": "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
    }
    for column, definition in columns.items():
        cur.execute(f'ALTER TABLE notification_templates ADD COLUMN IF NOT EXISTS "{column}" {definition}')
    cur.execute("UPDATE notification_templates SET mode='permanent' WHERE mode IS NULL")
    cur.execute("UPDATE notification_templates SET active=TRUE WHERE active IS NULL")
    cur.execute("UPDATE notification_templates SET template_text='' WHERE template_text IS NULL")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_notification_templates_lookup ON notification_templates(notification_type, student_id, active, created_at DESC)")


def ensure_core_compat_schema(cur) -> None:
    """Non-destructive migrations for schemas created by V0.1-V0.5."""
    table_columns = {
        "students": {
            "parent_notify_enabled": "BOOLEAN NOT NULL DEFAULT TRUE",
            "checkin_enabled": "BOOLEAN NOT NULL DEFAULT TRUE",
            "notify_checkin_enabled": "BOOLEAN NOT NULL DEFAULT TRUE",
            "notify_checkout_enabled": "BOOLEAN NOT NULL DEFAULT TRUE",
            "notify_exception_enabled": "BOOLEAN NOT NULL DEFAULT TRUE",
        },
        "student_line_bindings": {
            "notify_enabled": "BOOLEAN NOT NULL DEFAULT TRUE",
        },
        "courses": {
            "late_grace_minutes": "INTEGER NOT NULL DEFAULT 10",
            "checkout_grace_minutes": "INTEGER NOT NULL DEFAULT 15",
            "actual_course_id": "TEXT",
            "course_date": "DATE",
            "source": "TEXT NOT NULL DEFAULT 'legacy'",
            "source_note": "TEXT",
        },
        "attendance": {
            "late_notified_at": "TIMESTAMP",
            "absent_notified_at": "TIMESTAMP",
            "missing_checkout_notified_at": "TIMESTAMP",
            "last_scan_time": "TIMESTAMP",
            "manual_note": "TEXT",
            "adjusted_at": "TIMESTAMP",
            "adjusted_by": "TEXT",
            "last_absent_attempt_at": "TIMESTAMP",
            "last_missing_checkout_attempt_at": "TIMESTAMP",
        },
        "line_bind_tokens": {
            "token_value": "TEXT",
            "use_count": "INTEGER NOT NULL DEFAULT 0",
            "last_used_at": "TIMESTAMP",
        },
    }
    for table, columns in table_columns.items():
        for column, definition in columns.items():
            cur.execute(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{column}" {definition}')
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_courses_actual_course_id ON courses(actual_course_id) WHERE actual_course_id IS NOT NULL")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_courses_actual_date_student ON courses(course_date, student_id, active)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_attendance_date ON attendance(date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_attendance_open ON attendance(date, check_out_time)")


def db_schema_summary(cur) -> dict:
    wanted = {
        "notification_templates": ["id","notification_type","student_id","template_text","mode","remaining_uses","expires_at","active","note","created_by","created_at","updated_at"],
        "students": ["id","student_code","name","qr_token","active","parent_notify_enabled"],
        "courses": ["id","student_id","course_name","start_time","end_time","actual_course_id","course_date","source","active"],
        "attendance": ["id","student_id","course_id","date","check_in_time","check_out_time","status","late_notified_at","absent_notified_at","missing_checkout_notified_at"],
        "student_line_bindings": ["id","student_id","line_user_id","active","notify_enabled"],
    }
    result = {}
    for table, columns in wanted.items():
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s", (table,))
        existing = {str(r["column_name"]) for r in cur.fetchall()}
        result[table] = {column: (column in existing) for column in columns}
    return result


def load_runtime_settings(cur) -> None:
    global RUNTIME_SETTINGS
    try:
        cur.execute("SELECT key,value FROM runtime_settings")
        rows = cur.fetchall()
    except Exception:
        return
    data = dict(RUNTIME_SETTINGS)
    for row in rows:
        key = str(row["key"]).strip()
        value = str(row["value"]).strip()
        if key:
            data[key] = value
    RUNTIME_SETTINGS = data


def setting_int(key: str, fallback: int) -> int:
    try:
        return max(0, int(str(RUNTIME_SETTINGS.get(key, fallback)).strip()))
    except (TypeError, ValueError):
        return fallback


def setting_enabled(key: str, fallback: bool = True) -> bool:
    value = str(RUNTIME_SETTINGS.get(key, "啟用" if fallback else "停用")).strip()
    return value in {"啟用", "是", "1", "true", "TRUE", "yes", "Y", "y"}


def sync_runtime_settings(cur, rows: list[dict[str, Any]]) -> None:
    allowed = {
        "出勤模組", "出勤課程來源", "遲到門檻（分鐘）", "未到班通知（分鐘）",
        "未離班通知（分鐘）", "提前簽到（分鐘）", "重複掃描保護（分鐘）",
        "教室設備限制", "學生 QR", "LINE 家長提醒", "LINE 管理員提醒", "LINE 資料來源", "總表同步方式", "老師出勤",
    }
    for raw in rows:
        key = str(raw.get("設定項目", "")).strip()
        value = str(raw.get("目前值", "")).strip()
        if key not in allowed or value == "":
            continue
        cur.execute(
            "INSERT INTO runtime_settings(key,value,updated_at) VALUES (%s,%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at",
            (key, value, now_local()),
        )


def sync_default_templates_from_excel(cur, rows: list[dict[str, Any]]) -> int:
    imported = 0
    type_map = {v: k for k, v in TEMPLATE_TYPE_LABELS.items()}
    for raw in rows:
        label = str(raw.get("通知類型", "")).strip()
        ntype = type_map.get(label, label if label in DEFAULT_NOTIFICATION_TEMPLATES else "")
        text = str(raw.get("預設訊息範本", "")).strip()
        if not ntype or not text:
            continue
        active = _truthy_binding(raw.get("啟用"), True)
        cur.execute("UPDATE notification_templates SET active=FALSE,updated_at=%s WHERE notification_type=%s AND student_id IS NULL AND active=TRUE", (now_local(), ntype))
        if active:
            cur.execute(
                "INSERT INTO notification_templates(notification_type,student_id,template_text,mode,active,created_by,updated_at) "
                "VALUES (%s,NULL,%s,'default',TRUE,'EXCEL',%s)",
                (ntype, text, now_local()),
            )
        imported += 1
    return imported


def sync_individual_templates_from_excel(cur, rows: list[dict[str, Any]]) -> int:
    imported = 0
    type_map = {v: k for k, v in TEMPLATE_TYPE_LABELS.items()}
    mode_map = {"永久個別": "permanent", "一次性": "once", "期限內": "until", "預設": "default"}
    for raw in rows:
        code = str(raw.get("學生編號", "")).strip()
        label = str(raw.get("通知類型", "")).strip()
        text = str(raw.get("個別訊息範本", "")).strip()
        if not code or not text:
            continue
        ntype = type_map.get(label, label if label in DEFAULT_NOTIFICATION_TEMPLATES else "")
        if not ntype:
            continue
        cur.execute("SELECT id FROM students WHERE student_code=%s AND active=TRUE", (code,))
        st = cur.fetchone()
        if not st:
            continue
        student_id = st["id"]
        mode = mode_map.get(str(raw.get("套用方式", "")).strip(), "permanent")
        if mode not in {"permanent", "once", "until", "default"}:
            continue
        active = _truthy_binding(raw.get("啟用"), True)
        exp = None
        expiry = str(raw.get("有效至", "")).strip()
        if expiry:
            try:
                exp = parse_template_expiry(expiry)
            except ValueError:
                continue
        uses_text = str(raw.get("剩餘次數", "")).strip()
        try:
            uses = max(1, int(uses_text)) if mode == "once" and uses_text else (1 if mode == "once" else None)
        except ValueError:
            continue
        cur.execute("UPDATE notification_templates SET active=FALSE,updated_at=%s WHERE notification_type=%s AND student_id=%s AND active=TRUE", (now_local(), ntype, student_id))
        if active:
            cur.execute(
                "INSERT INTO notification_templates(notification_type,student_id,template_text,mode,remaining_uses,expires_at,active,note,created_by,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,TRUE,%s,'EXCEL',%s)",
                (ntype, student_id, text, mode, uses, exp, str(raw.get("備註", "")).strip() or None, now_local()),
            )
        imported += 1
    return imported


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
                    parent_notify_enabled BOOLEAN NOT NULL DEFAULT TRUE,
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
                    last_absent_attempt_at TIMESTAMP,
                    last_missing_checkout_attempt_at TIMESTAMP,
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
                    notify_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    bound_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    bound_by TEXT NOT NULL DEFAULT 'LIFF'
                );
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
                CREATE TABLE IF NOT EXISTS master_sync_state (
                    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
                    provider TEXT NOT NULL DEFAULT 'google_drive',
                    source_key TEXT,
                    source_name TEXT,
                    remote_modified_at TEXT,
                    remote_checksum TEXT,
                    last_checked_at TIMESTAMP,
                    last_synced_at TIMESTAMP,
                    last_success BOOLEAN NOT NULL DEFAULT FALSE,
                    last_error TEXT,
                    last_trigger TEXT,
                    last_actual_imported INTEGER NOT NULL DEFAULT 0,
                    last_actual_skipped INTEGER NOT NULL DEFAULT 0,
                    last_binding_imported INTEGER NOT NULL DEFAULT 0,
                    last_error_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS notification_templates (
                    id BIGSERIAL PRIMARY KEY,
                    notification_type TEXT NOT NULL,
                    student_id BIGINT REFERENCES students(id) ON DELETE CASCADE,
                    template_text TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL DEFAULT 'permanent',
                    remaining_uses INTEGER,
                    expires_at TIMESTAMP,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    note TEXT,
                    created_by TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS runtime_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            ensure_notification_template_schema(cur)
            ensure_core_compat_schema(cur)
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_student_line_binding ON student_line_bindings(student_id, line_user_id) WHERE active=TRUE")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_student_line_user ON student_line_bindings(line_user_id) WHERE active=TRUE")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_line_bind_tokens_student ON line_bind_tokens(student_id, created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_device_pair_tokens_lookup ON device_pair_tokens(token_hash, expires_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_line_messages_created ON line_message_logs(created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_line_messages_user ON line_message_logs(line_user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_runtime_settings_updated ON runtime_settings(updated_at DESC)")
            cur.execute("INSERT INTO master_sync_state(id,provider,updated_at) VALUES (1,%s,%s) ON CONFLICT(id) DO NOTHING", (MASTER_SYNC_PROVIDER, now_local()))
            cur.execute("UPDATE courses SET late_grace_minutes=%s WHERE late_grace_minutes IS NULL", (DEFAULT_LATE_GRACE_MINUTES,))
            cur.execute("UPDATE courses SET checkout_grace_minutes=%s WHERE checkout_grace_minutes IS NULL", (DEFAULT_CHECKOUT_GRACE_MINUTES,))
            if SEED_DEMO_DATA:
                seed_demo_data(cur)
            seed_notification_templates(cur)
            for key, value in RUNTIME_SETTINGS.items():
                cur.execute(
                    "INSERT INTO runtime_settings(key,value,updated_at) VALUES (%s,%s,%s) ON CONFLICT(key) DO NOTHING",
                    (key, str(value), now_local()),
                )
            load_runtime_settings(cur)
        conn.commit()


def seed_demo_data(cur) -> None:
    # 測試學生保留，並額外建立「今天」的實際課程資料。
    demo = [
        ("STU-000001", "王小明", "18:00", "19:30"),
        ("STU-000002", "林小華", "18:30", "20:00"),
        ("STU-000003", "陳小美", "19:00", "20:30"),
        ("STU-000004", "采璇", "19:00", "20:30"),
    ]
    work_date = now_local().date()
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
        actual_id = f"DEMO-{work_date:%Y%m%d}-{code}"
        cur.execute("SELECT id FROM courses WHERE actual_course_id=%s", (actual_id,))
        if not cur.fetchone():
            cur.execute(
                """INSERT INTO courses(
                    student_id, course_name, teacher_name, weekday, start_time, end_time,
                    late_grace_minutes, checkout_grace_minutes, active, actual_course_id,
                    course_date, source, source_note
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s,'demo','Demo 測試資料')""",
                (student_id, "測試課程", "測試老師", work_date.weekday(), start, end,
                 DEFAULT_LATE_GRACE_MINUTES, DEFAULT_CHECKOUT_GRACE_MINUTES, actual_id, work_date),
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
    def replace_var(match):
        key = match.group(1)
        return values.get(key, match.group(0))
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", replace_var, str(template_text or ""))


def consume_one_time_template(cur, template_row: dict[str, Any] | None) -> None:
    if not template_row or template_row.get("student_id") is None or not template_row.get("active"):
        return
    if template_row.get("mode") != "once":
        return
    if template_row.get("remaining_uses") is None or template_row.get("remaining_uses") <= 1:
        cur.execute("UPDATE notification_templates SET remaining_uses=0,active=FALSE,updated_at=%s WHERE id=%s", (now_local(), template_row["id"]))
    else:
        cur.execute("UPDATE notification_templates SET remaining_uses=remaining_uses-1,updated_at=%s WHERE id=%s", (now_local(), template_row["id"]))


def configured_admin_line_user_ids() -> list[str]:
    """Read one or more admin LINE User IDs from Render LINE_ADMIN_USER_ID.
    Accepts comma, semicolon, whitespace, or newline separated IDs.
    """
    raw = str(LINE_ADMIN_USER_ID or "").strip()
    ids = [x.strip() for x in re.split(r"[,;\s]+", raw) if x.strip()]
    # Preserve order while removing duplicates.
    return list(dict.fromkeys(ids))


def configured_admin_line_user_id() -> str:
    ids = configured_admin_line_user_ids()
    return ids[0] if ids else ""


def active_line_bindings(cur, student_id: int, legacy_line_user_id: str | None = None) -> list[dict[str, Any]]:
    cur.execute(
        "SELECT id,line_user_id,display_name,relation,notify_enabled FROM student_line_bindings WHERE student_id=%s AND active=TRUE AND notify_enabled=TRUE ORDER BY id",
        (student_id,),
    )
    rows = cur.fetchall()
    if legacy_line_user_id and not any(r["line_user_id"] == legacy_line_user_id for r in rows):
        rows.append({"id": None, "line_user_id": legacy_line_user_id, "display_name": None, "relation": "家長/監護人", "notify_enabled": True})
    return rows


def parent_notification_enabled(student: dict[str, Any], notification_type: str) -> bool:
    if not bool(student.get("parent_notify_enabled", True)):
        return False
    if notification_type == "check_in":
        return bool(student.get("notify_checkin_enabled", True))
    if notification_type == "check_out":
        return bool(student.get("notify_checkout_enabled", True))
    return bool(student.get("notify_exception_enabled", True))


def send_student_template_line(cur, attendance_id: int | None, student: dict[str, Any], course: dict[str, Any] | None,
                               notification_type: str, check_in_time: datetime | None = None,
                               check_out_time: datetime | None = None, when: datetime | None = None,
                               late_minutes: int = 0, legacy_line_user_id: str | None = None) -> bool:
    if not setting_enabled("LINE 家長提醒", True):
        disabled_msg = render_notification_template(
            DEFAULT_NOTIFICATION_TEMPLATES[notification_type], student, course, None,
            check_in_time, check_out_time, when, late_minutes
        )
        log_notification(cur, attendance_id, notification_type, None, disabled_msg, LINE_MODE, "disabled", "全域家長 LINE 通知已由管理員關閉")
        return False
    if not parent_notification_enabled(student, notification_type):
        disabled_msg = render_notification_template(
            DEFAULT_NOTIFICATION_TEMPLATES[notification_type], student, course, None,
            check_in_time, check_out_time, when, late_minutes
        )
        log_notification(cur, attendance_id, notification_type, None, disabled_msg, LINE_MODE, "disabled", "此學生的家長 LINE 通知已由管理員關閉")
        return False
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


def send_admin_template_line(cur, attendance_id: int | None, student: dict[str, Any], course: dict[str, Any] | None,
                              notification_type: str, check_in_time: datetime | None = None,
                              check_out_time: datetime | None = None, when: datetime | None = None,
                              late_minutes: int = 0) -> bool:
    template_row = get_effective_template(cur, notification_type, None)
    template_text = template_row["template_text"] if template_row else DEFAULT_NOTIFICATION_TEMPLATES[notification_type]
    msg = render_notification_template(template_text, student, course, None, check_in_time, check_out_time, when, late_minutes)
    if not setting_enabled("LINE 管理員提醒", True):
        log_notification(cur, attendance_id, notification_type, None, msg, LINE_MODE, "disabled", "全域管理員 LINE 通知已由管理員關閉")
        return False
    recipients = configured_admin_line_user_ids()
    if not recipients:
        log_notification(cur, attendance_id, notification_type, None, msg, LINE_MODE, "failed", "未設定 LINE_ADMIN_USER_ID")
        return False
    results = [send_line(cur, attendance_id, recipient, msg, notification_type) for recipient in recipients]
    return any(results) and all(results)


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


def device_pair_info(pair_token: str) -> dict[str, Any]:
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT p.id,p.device_id,p.expires_at,d.name,d.active AS device_active
                   FROM device_pair_tokens p JOIN checkin_devices d ON d.id=p.device_id
                   WHERE p.token_hash=%s AND p.used_at IS NULL AND p.expires_at>%s""",
                (hash_device_token(pair_token), now_local()),
            )
            pair = cur.fetchone()
    if not pair or not pair["device_active"]:
        raise HTTPException(status_code=400, detail="設備配對連結無效、已使用或已過期。請由管理員重新產生。")
    return pair


def confirm_device_pair(pair_token: str) -> tuple[str, str]:
    device_raw_token = secrets.token_urlsafe(32)
    now = now_local()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT p.id,p.device_id,d.name,d.active AS device_active
                   FROM device_pair_tokens p JOIN checkin_devices d ON d.id=p.device_id
                   WHERE p.token_hash=%s AND p.used_at IS NULL AND p.expires_at>%s
                   FOR UPDATE""",
                (hash_device_token(pair_token), now),
            )
            pair = cur.fetchone()
            if not pair or not pair["device_active"]:
                raise HTTPException(status_code=400, detail="設備配對連結無效、已使用或已過期。請由管理員重新產生。")
            cur.execute("UPDATE checkin_devices SET token_hash=%s,active=TRUE WHERE id=%s", (hash_device_token(device_raw_token), pair["device_id"]))
            cur.execute("UPDATE device_pair_tokens SET used_at=%s WHERE id=%s", (now, pair["id"]))
            conn.commit()
            return device_raw_token, pair["name"]


def get_student(cur, token: str):
    cur.execute("SELECT * FROM students WHERE qr_token=%s AND active=TRUE", (token,))
    return cur.fetchone()


def get_courses_today(cur, student_id: int, work_date: date):
    """出勤唯一課程來源：總表「實際課程」同步進來的日課程。"""
    cur.execute(
        """SELECT * FROM courses
           WHERE student_id=%s AND course_date=%s AND active=TRUE
             AND actual_course_id IS NOT NULL AND COALESCE(source, '實際課程')='實際課程'
           ORDER BY start_time, id""",
        (student_id, work_date),
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
        en = parse_hhmm(c["effective_end"], when.date()) if c.get("effective_end") else st
        early = st - timedelta(minutes=setting_int("提前簽到（分鐘）", CHECKIN_EARLY_MINUTES))
        late_end = en + timedelta(minutes=max(c["checkout_grace_minutes"], 60))
        inside = early <= when <= late_end
        distance = 0 if inside else abs((when - st).total_seconds())
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
        "SELECT line_user_id FROM student_line_bindings WHERE student_id=%s AND active=TRUE GROUP BY line_user_id ORDER BY MIN(id)",
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
                "INSERT INTO student_line_bindings(student_id,line_user_id,display_name,relation,active,notify_enabled,bound_at,bound_by) VALUES (%s,%s,%s,%s,TRUE,TRUE,%s,'LIFF')",
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
            cur.execute("SELECT line_user_id FROM student_line_bindings WHERE student_id=%s AND active=TRUE GROUP BY line_user_id ORDER BY MIN(id) LIMIT 1", (row["student_id"],))
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
            if not bool(student.get("checkin_enabled", True)):
                return {"kind": "error", "student": student["name"], "message": "此學生的簽到功能目前已由管理員關閉。"}
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
                if when < scheduled_start - timedelta(minutes=setting_int("提前簽到（分鐘）", CHECKIN_EARLY_MINUTES)):
                    conn.commit()
                    return {"kind": "too_early", "student": student["name"], "course": course["course_name"], "time": iso(when), "message": f"距離課程開始時間過早，請於課前 {setting_int("提前簽到（分鐘）", CHECKIN_EARLY_MINUTES)} 分鐘內再掃描。"}
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
                    if (when - attendance["check_in_time"]).total_seconds() < setting_int("重複掃描保護（分鐘）", 10) * 60:
                        conn.commit()
                        return {"kind": "duplicate", "student": student["name"], "message": f"已於 {attendance['check_in_time']:%H:%M} 到班，{setting_int("重複掃描保護（分鐘）", 10)} 分鐘內重複掃描不會視為離班。"}
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
                sent = send_admin_template_line(cur, attendance_id, student, course, "late", check_in_time=final_a["check_in_time"], when=when, late_minutes=final_a["late_minutes"])
                if sent:
                    cur.execute("UPDATE attendance SET late_notified_at=%s WHERE id=%s", (when, attendance_id))
            # 補到班後，若先前已被判定未到，保留紀錄但不再重複發未到。
            cur.execute("UPDATE attendance SET absent_notified_at=absent_notified_at WHERE id=%s", (attendance_id,))
            conn.commit()
            return {"kind": "check_in", "student": student["name"], "course": course["course_name"], "time": iso(final_a["check_in_time"]), "late": final_a["late_minutes"]}


def check_scheduled_absences() -> int:
    """Auto absent check. Consolidate due absent alerts into one admin push."""
    now = now_local()
    work_date = now.date()
    pending = []
    changed = 0
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(hashtext('attendance.absent.checker')) AS locked")
            if not cur.fetchone()["locked"]:
                return 0
            cur.execute("SELECT c.*, s.name, s.active AS student_active FROM courses c JOIN students s ON s.id=c.student_id WHERE c.course_date=%s AND c.active=TRUE AND c.actual_course_id IS NOT NULL AND COALESCE(c.source,'實際課程')='實際課程' AND s.active=TRUE ORDER BY c.start_time,c.id", (work_date,))
            threshold = setting_int("未到班通知（分鐘）", DEFAULT_LATE_GRACE_MINUTES)
            for course in cur.fetchall():
                eff = effective_schedule(cur, course, work_date)
                if not eff:
                    continue
                if now < parse_hhmm(eff["effective_start"], work_date) + timedelta(minutes=threshold):
                    continue
                cur.execute("INSERT INTO attendance(student_id,course_id,date,status,updated_at) VALUES (%s,%s,%s,'absent',%s) ON CONFLICT(student_id,course_id,date) DO NOTHING RETURNING id", (course["student_id"],course["id"],work_date,now))
                inserted = cur.fetchone()
                aid = inserted["id"] if inserted else None
                if not aid:
                    cur.execute("SELECT * FROM attendance WHERE student_id=%s AND course_id=%s AND date=%s FOR UPDATE", (course["student_id"],course["id"],work_date))
                    a = cur.fetchone()
                    if not a or a["check_in_time"] or a["absent_notified_at"]:
                        continue
                    if a.get("last_absent_attempt_at") and now-a["last_absent_attempt_at"] < timedelta(minutes=NOTIFICATION_RETRY_COOLDOWN_MINUTES):
                        continue
                    aid = a["id"]
                cur.execute("SELECT * FROM attendance WHERE id=%s FOR UPDATE",(aid,))
                a=cur.fetchone()
                if not a or a["check_in_time"] or a["absent_notified_at"]:
                    continue
                if a.get("last_absent_attempt_at") and now-a["last_absent_attempt_at"] < timedelta(minutes=NOTIFICATION_RETRY_COOLDOWN_MINUTES):
                    continue
                cur.execute("UPDATE attendance SET last_absent_attempt_at=%s,updated_at=%s WHERE id=%s",(now,now,aid))
                msg=absent_admin_message(course["name"],eff.get("course_name") or "未命名課程",eff["effective_start"],now)
                pending.append((aid,"absent",msg))
            if pending:
                if not setting_enabled("LINE 管理員提醒", True):
                    for aid,ntype,msg in pending:
                        log_notification(cur,aid,ntype,None,msg,LINE_MODE,"disabled","全域管理員 LINE 通知已由管理員關閉")
                    conn.commit()
                    return 0
                recipients=configured_admin_line_user_ids()
                batch="📋 今日未到班提醒（%s）\n\n%s" % (now.strftime("%H:%M"),"\n".join("• "+ " ".join(m.splitlines()).replace("🔴 ","",1) for _,_,m in pending))
                if len(batch)>4900: batch=batch[:4860]+"\n…（其餘請查看管理後台）"
                results=[send_line(cur,None,recipient,batch,"absent") for recipient in recipients] if recipients else []
                ok=bool(results) and all(results)
                for aid,ntype,msg in pending:
                    log_notification(cur,aid,ntype,",".join(recipients) if recipients else None,msg,LINE_MODE,"sent" if ok else "failed",None if ok else "管理員彙整提醒發送失敗")
                if ok:
                    for aid,_,_ in pending:
                        cur.execute("UPDATE attendance SET absent_notified_at=%s,status='absent',updated_at=%s WHERE id=%s AND absent_notified_at IS NULL",(now,now,aid))
                        changed += cur.rowcount
            conn.commit()
    return changed


def check_missing_checkout() -> int:
    """Auto missing-checkout check. Consolidate due alerts into one admin push."""
    now=now_local()
    work_date=now.date()
    pending=[]
    changed=0
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(hashtext('attendance.missing_checkout.checker')) AS locked")
            if not cur.fetchone()["locked"]:
                return 0
            cur.execute("SELECT a.*,s.name,c.course_name,c.start_time,c.end_time,c.checkout_grace_minutes FROM attendance a JOIN students s ON s.id=a.student_id JOIN courses c ON c.id=a.course_id WHERE a.date=%s AND a.check_in_time IS NOT NULL AND a.check_out_time IS NULL AND a.missing_checkout_notified_at IS NULL AND c.active=TRUE AND c.actual_course_id IS NOT NULL AND COALESCE(c.source,'實際課程')='實際課程' FOR UPDATE OF a SKIP LOCKED",(work_date,))
            for row in cur.fetchall():
                if row.get("last_missing_checkout_attempt_at") and now-row["last_missing_checkout_attempt_at"] < timedelta(minutes=NOTIFICATION_RETRY_COOLDOWN_MINUTES):
                    continue
                cur.execute("SELECT * FROM courses WHERE id=%s",(row["course_id"],))
                course=cur.fetchone()
                if not course: continue
                eff=effective_schedule(cur,course,work_date)
                if not eff or not eff.get("effective_end") or now < parse_hhmm(eff["effective_end"],work_date)+timedelta(minutes=eff["checkout_grace_minutes"]):
                    continue
                cur.execute("SELECT * FROM attendance WHERE id=%s FOR UPDATE",(row["id"],))
                a=cur.fetchone()
                if not a or a["check_out_time"] or a["missing_checkout_notified_at"]:
                    continue
                if a.get("last_missing_checkout_attempt_at") and now-a["last_missing_checkout_attempt_at"] < timedelta(minutes=NOTIFICATION_RETRY_COOLDOWN_MINUTES):
                    continue
                cur.execute("UPDATE attendance SET last_missing_checkout_attempt_at=%s,updated_at=%s WHERE id=%s",(now,now,row["id"]))
                msg=missing_checkout_message({"name":row["name"],"course_name":eff.get("course_name") or "未命名課程","check_in_time":a["check_in_time"],"effective_end":eff["effective_end"]},now)
                pending.append((row["id"],"missing_checkout",msg))
            if pending:
                if not setting_enabled("LINE 管理員提醒", True):
                    for aid,ntype,msg in pending:
                        log_notification(cur,aid,ntype,None,msg,LINE_MODE,"disabled","全域管理員 LINE 通知已由管理員關閉")
                    conn.commit()
                    return 0
                recipients=configured_admin_line_user_ids()
                batch="📋 今日未離班提醒（%s）\n\n%s" % (now.strftime("%H:%M"),"\n".join("• "+ " ".join(m.splitlines()).replace("🔴 ","",1) for _,_,m in pending))
                if len(batch)>4900: batch=batch[:4860]+"\n…（其餘查看管理後台）"
                results=[send_line(cur,None,recipient,batch,"missing_checkout") for recipient in recipients] if recipients else []
                ok=bool(results) and all(results)
                for aid,ntype,msg in pending:
                    log_notification(cur,aid,ntype,",".join(recipients) if recipients else None,msg,LINE_MODE,"sent" if ok else "failed",None if ok else "管理員彙整提醒發送失敗")
                if ok:
                    for aid,_,_ in pending:
                        cur.execute("UPDATE attendance SET missing_checkout_notified_at=%s,updated_at=%s WHERE id=%s AND missing_checkout_notified_at IS NULL",(now,now,aid))
                        changed += cur.rowcount
            conn.commit()
    return changed


def run_all_checks() -> dict[str, int]:
    result = {"absent": 0, "missing_checkout": 0}
    try:
        result["absent"] = check_scheduled_absences()
    except Exception:
        logger.exception("自動未到班檢查失敗")
    try:
        result["missing_checkout"] = check_missing_checkout()
    except Exception:
        logger.exception("自動未離班檢查失敗")
    return result


async def periodic_checker():
    logger.info("Starting attendance checker v%s; line_mode=%s; demo_seed=%s", APP_VERSION, LINE_MODE, SEED_DEMO_DATA)
    await asyncio.to_thread(run_all_checks)
    while True:
        await asyncio.sleep(60)
        await asyncio.to_thread(run_all_checks)


@app.on_event("startup")
async def startup():
    await asyncio.to_thread(init_db)
    asyncio.create_task(periodic_checker())
    asyncio.create_task(periodic_master_sync())


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
    return "<div class='nav'><a class='btn btn2' href='/admin'>今日出勤</a><a class='btn btn2' href='/admin/courses'>實際課程</a><a class='btn btn2' href='/admin/line'>LINE 綁定 / 測試</a><a class='btn btn2' href='/admin/logs'>訊息紀錄</a><a class='btn btn2' href='/admin/devices'>教室設備</a><a class='btn btn2' href='/admin/templates'>通知範本</a><a class='btn btn2' href='/admin/master-sync'>總表同步</a><a class='btn btn2' href='/admin/export.csv'>匯出 CSV</a></div>"


@app.get("/", response_class=HTMLResponse)
def root(_: str = Depends(admin_auth)):
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def dashboard(_: str = Depends(admin_auth)):
    today = now_local().date()
    with db_conn() as conn:
        with conn.cursor() as cur:
            load_runtime_settings(cur)
            parent_global = setting_enabled("LINE 家長提醒", True)
            admin_global = setting_enabled("LINE 管理員提醒", True)
    notice_class = "success" if parent_global and admin_global else "danger"
    notice_text = "家長＋管理員通知目前均啟用" if parent_global and admin_global else "⚠️ 有通知已關閉，關閉的通知不會發送"
    emergency_panel = (
        f"<section class='alert {notice_class}'><h2>🚨 LINE 緊急通知開關</h2>"
        f"<p><b>{escape(notice_text)}</b></p>"
        f"<div style='display:flex;gap:8px;flex-wrap:wrap'>"
        f"<form method='post' action='/admin/notifications/parent'><button class='btn {'btn2' if parent_global else ''}'>家長通知：{'啟用中（點此關閉）' if parent_global else '已關閉（點此開啟）'}</button></form>"
        f"<form method='post' action='/admin/notifications/admin'><button class='btn {'btn2' if admin_global else ''}'>管理員通知：{'啟用中（點此關閉）' if admin_global else '已關閉（點此開啟）'}</button></form>"
        f"</div>"
        f"<p class='mini muted'>管理員 LINE User ID 請在 Render 的 LINE_ADMIN_USER_ID 設定；可填多位，以逗號、分號、空白或換行分隔。</p></section>"
    )
    body_parts = [
        f"<div class='top'><div><h1>出勤測試系統 V{APP_VERSION}</h1><div class='muted'>Render 隔離測試站｜LINE：{escape(LINE_MODE_LABELS.get(LINE_MODE, LINE_MODE))}</div></div>{admin_nav()}</div>",
        "<script>setInterval(function(){if(!document.hidden){location.reload();}},5000);</script>",
        f"<div class='alert'>今天：{today:%Y-%m-%d}　自動檢查由背景程序每 60 秒執行一次；重新整理此頁面不會重複觸發 LINE 提醒。</div>",
        emergency_panel,
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

            # 一次載入所有有效 LINE 綁定，避免首頁每位學生各開一個 DB connection。
            # 學生數量增加後，這個 N+1 查詢會讓 /admin 明顯變慢。
            cur.execute(
                "SELECT id,student_id,line_user_id,display_name,relation,bound_at "
                "FROM student_line_bindings WHERE active=TRUE ORDER BY student_id,id"
            )
            binding_rows = cur.fetchall()
            bindings_by_student = {}
            for b in binding_rows:
                bindings_by_student.setdefault(b["student_id"], []).append(b)

            cur.execute(
                """
                SELECT c.*, s.name, s.student_code, s.line_user_id
                FROM courses c JOIN students s ON s.id=c.student_id
                WHERE c.course_date=%s AND c.active=TRUE AND c.actual_course_id IS NOT NULL AND COALESCE(c.source,'實際課程')='實際課程'
                ORDER BY c.start_time, s.name
                """,
                (today,),
            )
            roster_raw = cur.fetchall()
            roster = []
            if roster_raw:
                course_ids = [c["id"] for c in roster_raw]
                placeholders = ",".join(["%s"] * len(course_ids))
                cur.execute(
                    f"SELECT * FROM schedule_overrides WHERE work_date=%s AND course_id IN ({placeholders})",
                    (today, *course_ids),
                )
                overrides = {r["course_id"]: r for r in cur.fetchall()}
                cur.execute(
                    f"SELECT * FROM attendance WHERE date=%s AND course_id IN ({placeholders})",
                    (today, *course_ids),
                )
                attendance_by_course = {(a["student_id"], a["course_id"]): a for a in cur.fetchall()}
                for c in roster_raw:
                    ov = overrides.get(c["id"])
                    if ov and ov["cancelled"]:
                        continue
                    item = dict(c)
                    item["effective_start"] = ov["start_time"] if ov and ov["start_time"] else c["start_time"]
                    item["effective_end"] = ov["end_time"] if ov and ov["end_time"] else c["end_time"]
                    item["override_note"] = ov["note"] if ov else ""
                    item["override_id"] = ov["id"] if ov else None
                    item["late_grace_minutes"] = c["late_grace_minutes"]
                    item["checkout_grace_minutes"] = c["checkout_grace_minutes"]
                    item["attendance"] = attendance_by_course.get((c["student_id"], c["id"]))
                    roster.append(item)

            cur.execute("SELECT * FROM notification_logs ORDER BY id DESC LIMIT 30")
            notifications = cur.fetchall()

    body_parts.append(f"<div class='cards'><div class='card'><div class='muted'>學生</div><div class='num'>{student_count}</div></div><div class='card'><div class='muted'>今日紀錄</div><div class='num'>{total_records}</div></div><div class='card'><div class='muted'>遲到</div><div class='num'>{late_count}</div></div><div class='card'><div class='muted'>未到</div><div class='num'>{absent_count}</div></div><div class='card'><div class='muted'>尚未離班</div><div class='num'>{open_count}</div></div></div>")

    qr_rows = []
    for s in students:
        # 綁定資料已在上面的單次查詢中載入，不再為每位學生建立新的 DB connection。
        binds = bindings_by_student.get(s["id"], [])
        bind_text = "<br>".join(
            f"{escape(b['display_name'] or 'LINE 使用者')}｜{escape(b['relation'])}｜{escape(b['line_user_id'])} <form style='display:inline' method='post' action='/admin/student/{s['id']}/binding/{b['id']}/unbind'><button class='btn btn2 mini'>解除</button></form>"
            for b in binds
        ) or "尚未綁定"
        test_btn = f"<form method='post' action='/admin/test-line/{s['id']}'><button>測試 LINE</button></form>" if binds or s['line_user_id'] else "尚未綁定"
        notify_label = "啟用" if s.get("parent_notify_enabled", True) else "已關閉"
        qr_rows.append(
            f"<tr><td>{escape(s['name'])}</td><td>{escape(s['student_code'])}</td>"
            f"<td><img class='qr' src='/qr/{escape(s['student_code'])}.png'><br><span class='mini muted'>QR 固定不變；只有管理員按下重新產生才會更新。</span>"
            f"<form style='margin-top:6px' method='post' action='/admin/student/{s['id']}/qr-regenerate'><button class='btn btn2 mini'>重新產生學生 QR</button></form></td>"
            f"<td>{bind_text}</td>"
            f"<td><span class='{ 'green' if s.get('parent_notify_enabled', True) else 'gray' }'>家長提醒：{notify_label}</span>"
            f"<form style='margin-top:6px' method='post' action='/admin/student/{s['id']}/toggle-parent-notify'><button class='btn btn2 mini'>{'關閉家長提醒' if s.get('parent_notify_enabled', True) else '開啟家長提醒'}</button></form></td>"
            + (
                f"<td>{test_btn}<form style='margin-top:6px' method='post' action='/admin/student/{s['id']}/delete-test' onsubmit='return confirm(&quot;確定刪除測試學生？這會一併刪除其出勤、課程、LINE 綁定與通知範本資料，無法復原。&quot;)'><button class='btn btn2 mini'>刪除測試學生</button></form></td>"
                if str(s.get("student_code") or "").upper().startswith("STU-")
                else f"<td>{test_btn}</td>"
            )        )
    body_parts.append("<section><h2>學生 QR / 家長 LINE</h2><p class='muted mini'>學生 QR 是永久識別碼：日常不會變，只有管理員主動重新產生才會更新。家長 LINE 不由學生手機綁定，而是從既有 LINE 客服／Excel 的 LINE User ID 與學生姓名（或學生編號）建立關聯。</p><table><tr><th>學生</th><th>編號</th><th>學生 QR</th><th>已綁定 LINE</th><th>家長提醒</th><th>測試</th></tr>" + "".join(qr_rows) + "</table></section>")

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


@app.post("/admin/notifications/toggle")
def toggle_global_notifications(_: str = Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            load_runtime_settings(cur)
            parent_on = setting_enabled("LINE 家長提醒", True)
            admin_on = setting_enabled("LINE 管理員提醒", True)
            cur.execute(
                "INSERT INTO runtime_settings(key,value,updated_at) VALUES (%s,%s,%s) "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at",
                ("LINE 家長提醒", "停用" if parent_on else "啟用", now_local()),
            )
            cur.execute(
                "INSERT INTO runtime_settings(key,value,updated_at) VALUES (%s,%s,%s) "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at",
                ("LINE 管理員提醒", "停用" if admin_on else "啟用", now_local()),
            )
            conn.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/notifications/parent")
def toggle_global_parent_notification(_: str = Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            load_runtime_settings(cur)
            current = setting_enabled("LINE 家長提醒", True)
            cur.execute(
                "INSERT INTO runtime_settings(key,value,updated_at) VALUES (%s,%s,%s) "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at",
                ("LINE 家長提醒", "停用" if current else "啟用", now_local()),
            )
            conn.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/notifications/admin")
def toggle_global_admin_notification(_: str = Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            load_runtime_settings(cur)
            current = setting_enabled("LINE 管理員提醒", True)
            cur.execute(
                "INSERT INTO runtime_settings(key,value,updated_at) VALUES (%s,%s,%s) "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at",
                ("LINE 管理員提醒", "停用" if current else "啟用", now_local()),
            )
            conn.commit()
    return RedirectResponse("/admin", status_code=303)



@app.get("/admin/courses", response_class=HTMLResponse)
def admin_courses(_: str = Depends(admin_auth)):
    today = now_local().date()
    body = [
        f"<div class='top'><div><h1>實際課程</h1><div class='muted'>出勤只依據總表「實際課程」同步資料。固定課表／調課請在原本總表處理；這裡只提供今天的臨時校正。</div></div>{admin_nav()}</div>",
        "<section><h2>同步規則</h2><p>上傳總表後，Render 會依「實際課程」的 Course ID＋課程日期＋學生建立/更新出勤課程。若原本課表調課已確認，正式時間以「實際課程」為準；「課程提醒」仍是獨立通知佇列，不會反過來修改出勤時間。</p></section>"
    ]
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.*, s.name, s.student_code
                   FROM courses c JOIN students s ON s.id=c.student_id
                   WHERE c.active=TRUE AND c.actual_course_id IS NOT NULL AND COALESCE(c.source,'實際課程')='實際課程' AND c.course_date >= %s
                   ORDER BY c.course_date,c.start_time,s.name LIMIT 300""",
                (today,),
            )
            rows = cur.fetchall()
            grouped = {}
            for c in rows:
                grouped.setdefault(c["course_date"], []).append(c)
            for d, items in grouped.items():
                body.append(f"<section><h2>{d:%Y-%m-%d}　{WEEKDAYS[d.weekday()]}</h2><div style='overflow:auto'><table><tr><th>學生</th><th>Course ID</th><th>課程</th><th>老師</th><th>來源時間</th><th>今日校正</th></tr>")
                for c in items:
                    cur.execute("SELECT * FROM schedule_overrides WHERE course_id=%s AND work_date=%s", (c["id"], d))
                    ov = cur.fetchone()
                    effective_start = ov["start_time"] if ov and ov["start_time"] else c["start_time"]
                    effective_end = ov["end_time"] if ov and ov["end_time"] else c["end_time"]
                    end_display = effective_end or "未設定"
                    warning = "<br><span class='red mini'>⚠️ 缺少下課時間：無法自動判定未離班</span>" if not effective_end else ""
                    body.append(
                        f"<tr><td><b>{escape(c['name'])}</b><br><span class='mini muted'>{escape(c['student_code'])}</span></td>"
                        f"<td class='mini'>{escape(c['actual_course_id'])}</td>"
                        f"<td>{escape(c['course_name'])}</td><td>{escape(c['teacher_name'] or '')}</td>"
                        f"<td>{escape(effective_start)}-{escape(end_display)}{warning}</td>"
                        f"<td><form method='post' action='/admin/course/{c['id']}/override'>"
                        f"<input type='date' name='work_date' value='{d:%Y-%m-%d}' style='width:145px' readonly> "
                        f"<input type='time' name='start_time' value='{escape(effective_start)}'> "
                        f"<input type='time' name='end_time' value='{escape(effective_end or '')}'> "
                        f"<input class='wide' name='note' value='{escape((ov['note'] or '') if ov else '', quote=True)}' placeholder='臨時校正原因'> "
                        f"<label><input type='checkbox' name='cancelled' value='1' {'checked' if ov and ov['cancelled'] else ''}> 取消</label> "
                        f"<button class='btn btn2 mini'>儲存今天校正</button></form>"
                        f"<div class='mini muted'>目前有效時間：{escape(effective_start)}-{escape(end_display)}</div></td></tr>"
                    )
                body.append("</table></div></section>")
    if len(rows) == 0:
        body.append("<section class='alert'>目前沒有已同步的未來「實際課程」。請到「LINE 綁定 / 測試」上傳最新總表 Excel。</section>")
    return page("實際課程", "".join(body))


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
def save_override(course_id: int, start_time: str = Form(...), end_time: str = Form(...), note: str = Form(""), cancelled: str | None = Form(None), work_date: str = Form(""), _: str = Depends(admin_auth)):
    target_date = datetime.strptime(work_date, "%Y-%m-%d").date() if work_date else now_local().date()
    if end_time.strip():
        if parse_hhmm(start_time, target_date) >= parse_hhmm(end_time, target_date):
            raise HTTPException(400, "結束時間必須晚於開始時間。")
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO schedule_overrides(course_id,work_date,start_time,end_time,cancelled,note)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT(course_id,work_date) DO UPDATE SET start_time=EXCLUDED.start_time,end_time=EXCLUDED.end_time,cancelled=EXCLUDED.cancelled,note=EXCLUDED.note
                """,
                (course_id, target_date, start_time.strip() or None, end_time.strip() or None, bool(cancelled), note.strip() or None),
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
            student = {"id": c["student_id"], "name": c["name"], "line_user_id": None, "parent_notify_enabled": True, "notify_exception_enabled": True}
            if send_admin_template_line(cur, aid, student, eff, "absent", when=now):
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
            student = {"id": a["student_id"], "name": a["name"], "line_user_id": None, "parent_notify_enabled": True, "notify_exception_enabled": True}
            if send_admin_template_line(cur, attendance_id, student, eff, "missing_checkout", check_in_time=a["check_in_time"], when=now):
                cur.execute("UPDATE attendance SET missing_checkout_notified_at=%s,updated_at=%s WHERE id=%s", (now,now,attendance_id))
            conn.commit()
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/templates", response_class=HTMLResponse)
def admin_templates(_: str = Depends(admin_auth)):
    body = [
        f"<div class='top'><div><h1>LINE 通知範本</h1><div class='muted'>系統版本：V{APP_VERSION}｜預設範本 + 個別學生覆寫。一次性覆寫送出成功後自動恢復預設；期限型覆寫到期後自動恢復預設。</div></div>{admin_nav()}</div>"
    ]
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                ensure_notification_template_schema(cur)
                seed_notification_templates(cur)
                conn.commit()
                cur.execute("SELECT id,notification_type,template_text FROM notification_templates WHERE student_id IS NULL AND active=TRUE ORDER BY id")
                defaults = cur.fetchall()
                cur.execute("SELECT id,student_code,name FROM students WHERE active=TRUE ORDER BY name,id")
                students = cur.fetchall()
                cur.execute(
                    """SELECT nt.id,nt.notification_type,nt.template_text,nt.mode,nt.remaining_uses,nt.expires_at,nt.note,s.student_code,s.name
                       FROM notification_templates nt JOIN students s ON s.id=nt.student_id
                       WHERE nt.student_id IS NOT NULL AND nt.active=TRUE
                       ORDER BY s.name,s.id,nt.notification_type,nt.created_at DESC"""
                )
                overrides = cur.fetchall()
    except Exception as exc:
        logger.exception("通知範本頁面失敗")
        detail = escape(str(exc)[:1200])
        return page(
            "LINE 通知範本診斷",
            f"<section><h1>LINE 通知範本無法開啟</h1><div class='alert danger'>❌ {detail}</div>"
            f"<p>程式版本：<b>V{APP_VERSION}</b></p><p>這個版本會在開啟本頁時自動修復通知範本表結構；如果仍失敗，請查看 <a href='/admin/diagnostics'>系統診斷</a> 與 Render Logs。</p>"
            f"<p><a class='btn btn2' href='/admin'>返回管理頁</a> <a class='btn btn2' href='/health'>檢查版本</a> <a class='btn btn2' href='/admin/diagnostics'>系統診斷</a></p></section>"
        )

    body.append("<section><h2>預設範本</h2><p class='muted'>可使用變數：{greeting}、{thanks}、{student_name}、{student_code}、{course_name}、{teacher_name}、{scheduled_start}、{scheduled_end}、{check_in_time}、{check_out_time}、{now_time}、{late_minutes}。未知變數會原樣保留，不會讓整個出勤流程失敗。</p><div style='overflow:auto'><table><tr><th>通知</th><th>範本內容</th><th>操作</th></tr>")
    for r in defaults:
        form_id = f"default_template_{r['id']}"
        body.append(
            f"<tr><td>{escape(TEMPLATE_TYPE_LABELS.get(r['notification_type'],r['notification_type']))}</td>"
            f"<td><textarea form='{form_id}' name='template_text' rows='4' style='min-width:520px'>{escape(r['template_text'])}</textarea></td>"
            f"<td><form id='{form_id}' method='post' action='/admin/template/default'><input type='hidden' name='notification_type' value='{escape(r['notification_type'])}'><button>儲存預設範本</button></form></td></tr>"
        )
    body.append("</table></div></section>")

    student_options = ''.join(f"<option value='{s['id']}'>{escape(s['name'])}｜{escape(s['student_code'])}</option>" for s in students)
    type_options = ''.join(f"<option value='{k}'>{escape(v)}</option>" for k, v in TEMPLATE_TYPE_LABELS.items())
    body.append(
        f"<section><h2>新增／修改個別學生範本</h2><p class='muted'>一次性範本第一次成功送出後自動恢復預設。</p>"
        f"<form method='post' action='/admin/template/student'><div class='grid'>"
        f"<label>學生<br><select name='student_id' required>{student_options}</select></label>"
        f"<label>通知類型<br><select name='notification_type' required>{type_options}</select></label>"
        f"<label>套用方式<br><select name='mode'><option value='permanent'>永久個別</option><option value='once'>一次性（成功送出後自動恢復）</option><option value='until'>期限內</option></select></label>"
        f"<label>剩餘次數<br><input type='number' name='remaining_uses' value='1' min='1'></label></div>"
        f"<p>範本內容</p><textarea name='template_text' rows='5' style='width:100%' required placeholder='例如：{{greeting}}，{{student_name}}說今天先離開教室了，{{thanks}}😊'></textarea>"
        f"<p>期限（選填，格式 YYYY-MM-DD HH:MM） <input class='wide' name='expires_at' placeholder='例如 2026-09-30 20:00'>　備註 <input class='wide' name='note' placeholder='例如：今天臨時通知'></p>"
        f"<button>儲存個別範本</button></form></section>"
    )

    rows = []
    for r in overrides:
        expires = r['expires_at'].strftime('%Y-%m-%d %H:%M') if r['expires_at'] else ''
        mode_label = {'permanent':'永久個別','once':'一次性','until':'期限內','default':'預設'}.get(r['mode'], r['mode'])
        rows.append(
            f"<tr><td>{escape(r['name'])}<br><span class='muted mini'>{escape(r['student_code'])}</span></td>"
            f"<td>{escape(TEMPLATE_TYPE_LABELS.get(r['notification_type'],r['notification_type']))}</td>"
            f"<td style='white-space:pre-wrap'>{escape(r['template_text'])}</td>"
            f"<td>{escape(mode_label)}<br>剩餘：{r['remaining_uses'] if r['remaining_uses'] is not None else '不限'}<br>有效至：{expires or '不限'}</td>"
            f"<td><form method='post' action='/admin/template/{r['id']}/restore'><button class='btn btn2 mini'>恢復預設</button></form></td></tr>"
        )
    body.append("<section><h2>目前有效的個別範本</h2><div style='overflow:auto'><table><tr><th>學生</th><th>通知</th><th>內容</th><th>規則</th><th>操作</th></tr>" + ''.join(rows) + "</table></div></section>")
    body.append("<section><h2>Excel／CSV 管理</h2><p>通知範本仍以總表「出勤通知模板」與「出勤通知個別」為管理來源；此頁可做即時校正。</p><p><a class='btn btn2' href='/admin/templates/export.csv'>下載通知範本 CSV</a></p><form method='post' action='/admin/templates/import.csv' enctype='multipart/form-data'><input type='file' name='file' accept='.csv,text/csv' required> <button>匯入通知範本 CSV</button></form></section>")
    return page("LINE 通知範本", ''.join(body))


@app.post("/admin/template/default")
def save_default_template(notification_type: str=Form(...), template_text: str=Form(...), _: str=Depends(admin_auth)):
    # 自動修復舊版通知範本表結構。
    with db_conn() as conn:
        with conn.cursor() as cur:
            ensure_notification_template_schema(cur)
            conn.commit()
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
    # 自動修復舊版通知範本表結構。
    with db_conn() as conn:
        with conn.cursor() as cur:
            ensure_notification_template_schema(cur)
            conn.commit()
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
    # 自動修復舊版通知範本表結構。
    with db_conn() as conn:
        with conn.cursor() as cur:
            ensure_notification_template_schema(cur)
            conn.commit()
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE notification_templates SET active=FALSE,updated_at=%s WHERE id=%s AND student_id IS NOT NULL", (now_local(),template_id))
            conn.commit()
    return RedirectResponse("/admin/templates",status_code=303)


@app.get("/admin/templates/export.csv")
def export_notification_templates(_: str=Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            ensure_notification_template_schema(cur)
            conn.commit()
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
def admin_line(request: Request, _: str = Depends(admin_auth)):
    body=[f"<div class='top'><div><h1>LINE 綁定 / 測試</h1><div class='muted'>出勤系統直接使用既有 AI 客服／家長服務所蒐集的 LINE User ID，不要求學生本人有 LINE。</div></div>{admin_nav()}</div>"]
    imported = request.query_params.get("imported")
    actual_imported = request.query_params.get("actual_imported")
    actual_skipped = request.query_params.get("actual_skipped")
    errors = request.query_params.get("errors")
    if imported is not None or actual_imported is not None:
        source = "總表 Excel" if request.query_params.get("source") == "xlsx" else "CSV"
        body.append(f"<section class='alert success'>✅ {source}同步完成：LINE 綁定 {escape(imported or '0')} 筆；實際課程 {escape(actual_imported or '0')} 筆。")
        if actual_skipped and actual_skipped != '0':
            body.append(f"<br>⚠️ 實際課程略過 {escape(actual_skipped)} 筆")
        if errors and errors != '0':
            body.append(f"<br>⚠️ 有 {escape(errors)} 筆同步資料需要檢查學生姓名／編號或時間格式。")
        body.append("</section>")
    with db_conn() as conn:
        with conn.cursor() as cur:
            master_state = _get_master_sync_state(cur)
    sync_configured, sync_config_detail = _master_sync_configured()
    sync_badge = "✅ 自動同步正常" if master_state.get("last_success") else ("⚠️ 尚未成功同步" if sync_configured else "⚠️ 尚未完成自動同步設定")
    sync_class = "success" if master_state.get("last_success") else "alert"
    master_sync_status_html = (
        f"<section><h2>主總表同步</h2><div class='alert {sync_class}'><b>{escape(sync_badge)}</b>｜{escape(sync_config_detail)}</div>"
        f"<p>來源：<b>{escape(str(master_state.get('source_name') or '尚未同步'))}</b>｜最後檢查：{escape(str(master_state.get('last_checked_at') or '—'))}｜最後成功：{escape(str(master_state.get('last_synced_at') or '—'))}</p>"
        f"<p>自動檢查每 <b>{MASTER_SYNC_INTERVAL_MINUTES}</b> 分鐘；手動上傳只作為備援。"
        f" <a class='btn btn2' href='/admin/master-sync'>查看同步狀態</a></p></section>"
    )
    body.append(master_sync_status_html)
    webhook_url=f"{public_base_url()}/webhook/line"
    line_missing = []
    if LINE_MODE == "live":
        if not LINE_CHANNEL_ACCESS_TOKEN:
            line_missing.append("LINE_CHANNEL_ACCESS_TOKEN")
        if not LINE_CHANNEL_SECRET:
            line_missing.append("LINE_CHANNEL_SECRET")
        if not LINE_ADMIN_USER_ID:
            line_missing.append("LINE_ADMIN_USER_ID")
    if LINE_MODE == "live" and line_missing:
        line_status = (
            "<div class='alert' style='background:#fff3cd;border-color:#ffe69c'>"
            "⚠️ 已切換為正式發送，但 Render 尚缺少："
            + escape("、".join(line_missing))
            + "。補齊後再按「測試 LINE」。"
            "</div>"
        )
    elif LINE_MODE == "live":
        line_status = "<div class='alert success'>✅ LINE 正式發送設定已就緒，可使用「測試 LINE」確認實際 Push。</div>"
    else:
        line_status = "<div class='alert'>目前為模擬模式；按「測試 LINE」只會寫入測試紀錄，不會真的傳到家長 LINE。正式測試請將 LINE_MODE 設為 <code>live</code>，並補齊 LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET / LINE_ADMIN_USER_ID。</div>"
    body.append(
        f"<section><h2>LINE 連線</h2><p>Webhook URL：<code>{escape(webhook_url)}</code></p>"
        f"<p>目前 LINE 模式：<b>{escape(LINE_MODE_LABELS.get(LINE_MODE, LINE_MODE))}</b></p>"
        f"{line_status}"
        f"<div class='alert'>家長端的 LINE 綁定可以繼續由你原本的 AI 客服處理。這個出勤站不再要求家長另外建立第二套綁定；只要從客服系統／Excel 取得「學生姓名（或學生編號）＋LINE User ID」，匯入這裡即可。</div>"
        f"<p class='mini muted'>LIFF / LINE Login 目前不是出勤簽到的必要條件；未來若要做新的家長自助綁定，再另外啟用即可。</p></section>"
    )
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,name,student_code,parent_notify_enabled FROM students WHERE active=TRUE ORDER BY name")
            students=cur.fetchall()
            rows=[]
            for s in students:
                cur.execute("SELECT id,line_user_id,display_name,relation,bound_at,active,notify_enabled FROM student_line_bindings WHERE student_id=%s ORDER BY id DESC", (s["id"],))
                rows.extend([dict(x, student_id=s["id"], student_name=s["name"], student_code=s["student_code"], parent_notify_enabled=s["parent_notify_enabled"]) for x in cur.fetchall()])
            cur.execute("SELECT * FROM line_message_logs ORDER BY id DESC LIMIT 80")
            logs=cur.fetchall()
    table=["<section><h2>學生與 LINE</h2><p class='muted'>可由學生層級總開關控制，也可對單一家長綁定個別關閉提醒；簽到紀錄本身不會因為關閉提醒而停止。</p><div style='overflow:auto'><table><tr><th>學生</th><th>LINE</th><th>家長提醒</th><th>測試</th></tr>"]
    for s in students:
        binds=[r for r in rows if r["student_id"]==s["id"] and r["active"]]
        count_text = f"共 {len(binds)} 位" if binds else "尚未綁定"
        bind_rows=[]
        for b in binds:
            display = escape(b['display_name'] or 'LINE 使用者')
            relation = escape(b['relation'] or '家長/監護人')
            uid = escape(b['line_user_id'])
            notify_label = "提醒啟用" if b.get("notify_enabled", True) else "提醒關閉"
            notify_btn = (
                f"<form style='display:inline' method='post' action='/admin/student/{s['id']}/binding/{b['id']}/toggle-notify'>"
                + ("<button class='btn btn2 mini'>關閉提醒</button>" if b.get("notify_enabled", True) else "<button class='btn btn2 mini'>開啟提醒</button>")
                + "</form>"
            )
            bind_rows.append(
                f"<div style='padding:7px 0;border-bottom:1px solid #eee'><b>{display}</b>｜{relation}<br>"
                f"<span class='mini'>{uid}</span>｜綁定 {b['bound_at']}｜<span class='mini'>{notify_label}</span> "
                + notify_btn + " "
                + f"<form style='display:inline' method='post' action='/admin/student/{s['id']}/binding/{b['id']}/unbind'><button class='btn btn2 mini'>解除</button></form></div>"
            )
        bind_text = count_text + ("<div>" + "".join(bind_rows) + "</div>" if bind_rows else "")
        test_btn=f"<form method='post' action='/admin/test-line/{s['id']}'><button>測試 LINE</button></form>" if binds or s.get('line_user_id') else "尚未綁定"
        toggle_text = "關閉全部" if s["parent_notify_enabled"] else "開啟全部"
        table.append(f"<tr><td><b>{escape(s['name'])}</b><br><span class='mini muted'>{escape(s['student_code'])}</span></td><td>{bind_text}</td><td><span class='{ 'green' if s['parent_notify_enabled'] else 'gray' }'>{'啟用' if s['parent_notify_enabled'] else '已關閉'}</span><form style='margin-top:6px' method='post' action='/admin/student/{s['id']}/toggle-parent-notify'><button class='btn btn2 mini'>{toggle_text}家長提醒</button></form></td><td>{test_btn}</td></tr>")
    table.append("</table></div></section>")
    body.append("".join(table))

    body.append("<section><h2>總表同步／手動備援</h2><p>正式模式會從 Google Drive 自動同步主總表；這裡的 Excel 上傳保留作為「手動立即更新／緊急備援」。出勤的日期／時間唯一依據是「實際課程」；LINE 綁定使用「出勤LINE綁定」，若該表沒有可用資料才回退讀取「聯絡人」。</p><p><b>自動同步設定：</b>請到「總表同步」確認 Google Drive 檔案與 Service Account 已設定。</p><form method='post' action='/admin/line/import.xlsx' enctype='multipart/form-data'><input type='file' name='file' accept='.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' required> <button>匯入總表 Excel</button></form><form method='post' action='/admin/line/import.csv' enctype='multipart/form-data' style='margin-top:8px'><input type='file' name='file' accept='.csv,text/csv' required> <button class='btn btn2'>匯入 LINE 綁定 CSV</button></form><p><a class='btn btn2' href='/admin/line/export.csv'>下載目前 LINE 綁定 CSV</a></p><p class='mini muted'>出勤同步會讀取「實際課程」、「出勤學生」、「出勤LINE綁定」。實際課程由固定課表＋已確認調課形成；「課程提醒」仍維持原系統獨立發送，不會改變出勤時間。每位家長的「家長提醒」可個別開關。</p></section>")

    log_table=["<section><h2>最近 LINE 訊息</h2><p class='mini muted'>紀錄會永久保留，除非管理員手動刪除；這裡只顯示最新 80 筆。</p><p><a class='btn btn2' href='/admin/logs'>前往訊息紀錄管理</a></p><div style='overflow:auto'><table><tr><th>時間</th><th>方向</th><th>類型</th><th>LINE User ID</th><th>訊息</th><th>狀態</th></tr>"]
    for r in logs:
        dlabel="收到" if r['direction']=='inbound' else "送出"
        mtype={"push":"主動推送","reply":"回覆","text":"文字"}.get(r['message_type'],r['message_type'])
        slabel={"received":"已收到","sent":"已送出","simulated":"模擬送出","failed":"失敗","disabled":"已關閉"}.get(r['status'],r['status'])
        log_table.append(f"<tr><td>{r['created_at']}</td><td>{dlabel}</td><td>{mtype}</td><td class='mini'>{escape(r['line_user_id'] or '')}</td><td>{escape(r['message'] or '')}</td><td>{slabel}</td></tr>")
    log_table.append("</table></div></section>")
    body.append("".join(log_table))
    return page("LINE 綁定 / 測試", "".join(body))


# ---------- Manual log management ----------

def _log_confirm_page(kind: str, action: str, title: str, detail: str, form_action: str) -> str:
    phrase = "刪除全部 LINE 訊息紀錄" if kind == "line_all" else "刪除全部出勤通知紀錄" if kind == "notification_all" else "刪除這筆紀錄"
    extra = ""
    if kind in {"line_all", "notification_all"}:
        extra = (
            f"<p>為避免誤刪，請在下方輸入：<code>{escape(phrase)}</code></p>"
            f"<input class='wide' name='confirm_text' placeholder='{escape(phrase)}' required>"
        )
    else:
        extra = "<label><input type='checkbox' name='confirm' value='yes' required style='width:auto'> 我確認要刪除此筆紀錄。</label>"
    body = (
        f"<section><h1>{escape(title)}</h1>"
        f"<div class='alert danger'>⚠️ {escape(detail)}</div>"
        f"<p class='mini muted'>刪除的是管理後台的紀錄，不會撤回已經發送到 LINE 的訊息，也不會刪除學生、課程、出勤紀錄、LINE 綁定或通知範本。</p>"
        f"<form method='post' action='{escape(form_action)}'>{extra}"
        f"<div style='margin-top:14px'><button type='submit'>確認刪除</button> <a class='btn btn2' href='/admin/logs'>取消</a></div></form></section>"
    )
    return page("確認刪除紀錄", body)


@app.get("/admin/logs", response_class=HTMLResponse)
def admin_logs(_: str = Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM line_message_logs")
            line_count = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM notification_logs")
            notification_count = cur.fetchone()["c"]
            cur.execute("SELECT * FROM line_message_logs ORDER BY id DESC LIMIT 80")
            line_logs = cur.fetchall()
            cur.execute("SELECT * FROM notification_logs ORDER BY id DESC LIMIT 50")
            notification_logs = cur.fetchall()

    body = [
        f"<div class='top'><div><h1>訊息紀錄管理</h1><div class='muted'>V{APP_VERSION}｜紀錄不自動清除，只能由管理員手動刪除。</div></div>{admin_nav()}</div>",
        "<div class='alert'>保留政策：<b>永久保留，直到管理員手動刪除。</b>這裡沒有 30 天自動清理，也不會因為背景程序或重新整理頁面而刪除紀錄。畫面只顯示最近一部分，資料庫仍保留全部紀錄。</div>",
        f"<section><h2>LINE 訊息紀錄</h2><p>資料庫目前共有 <b>{line_count}</b> 筆；畫面顯示最新 80 筆。</p>"
        f"<p><a class='btn btn2' href='/admin/logs/line/delete-all-confirm'>刪除全部 LINE 訊息紀錄</a></p>",
        "<div style='overflow:auto'><table><tr><th>時間</th><th>方向</th><th>類型</th><th>LINE User ID</th><th>訊息</th><th>狀態</th><th>操作</th></tr>"
    ]
    for r in line_logs:
        dlabel = "收到" if r["direction"] == "inbound" else "送出"
        mtype = {"push":"主動推送","reply":"回覆","text":"文字"}.get(r["message_type"], r["message_type"])
        slabel = {"received":"已收到","sent":"已送出","simulated":"模擬送出","failed":"失敗","disabled":"已關閉"}.get(r["status"], r["status"])
        body.append(
            f"<tr><td>{r['created_at']}</td><td>{escape(dlabel)}</td><td>{escape(mtype)}</td>"
            f"<td class='mini'>{escape(r['line_user_id'] or '')}</td><td>{escape(r['message'] or '')}</td>"
            f"<td>{escape(slabel)}</td><td><a class='btn btn2 mini' href='/admin/logs/line/{r['id']}/delete-confirm'>刪除</a></td></tr>"
        )
    body.append("</table></div></section>")

    body.append(
        f"<section><h2>出勤通知紀錄</h2><p>資料庫目前共有 <b>{notification_count}</b> 筆；畫面顯示最新 50 筆。</p>"
        f"<p><a class='btn btn2' href='/admin/logs/notification/delete-all-confirm'>刪除全部出勤通知紀錄</a></p>"
        "<div style='overflow:auto'><table><tr><th>時間</th><th>類型</th><th>學生</th><th>LINE User ID</th><th>狀態</th><th>錯誤</th><th>操作</th></tr>"
    )
    for r in notification_logs:
        body.append(
            f"<tr><td>{r['created_at']}</td><td>{escape(notification_type_label(r['notification_type']))}</td>"
            f"<td>{escape(r.get('student_name') or '')}</td><td class='mini'>{escape(r.get('line_user_id') or '')}</td>"
            f"<td>{escape(notify_status_label(r.get('status')))}</td><td>{escape(r.get('error_message') or '')}</td>"
            f"<td><a class='btn btn2 mini' href='/admin/logs/notification/{r['id']}/delete-confirm'>刪除</a></td></tr>"
        )
    body.append("</table></div></section>")
    body.append(
        "<section><h2>刪除規則</h2><ul><li>單筆刪除：先進入確認頁，再由管理員確認。</li><li>全部刪除：必須再輸入指定文字，避免誤刪。</li><li>刪除只影響管理後台的 log，不會撤回 LINE、刪除出勤、學生、課程、LINE 綁定或通知範本。</li></ul></section>"
    )
    return page("訊息紀錄管理", "".join(body))


@app.get("/admin/logs/line/{log_id}/delete-confirm", response_class=HTMLResponse)
def confirm_delete_line_log(log_id: int, _: str = Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,created_at,line_user_id,message FROM line_message_logs WHERE id=%s", (log_id,))
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, "找不到 LINE 訊息紀錄")
    detail = f"將刪除 {row['created_at']} 的 LINE 訊息紀錄；收件人 {row['line_user_id'] or '-'}。"
    return _log_confirm_page("line_one", "delete", "確認刪除 LINE 訊息紀錄", detail, f"/admin/logs/line/{log_id}/delete")


@app.post("/admin/logs/line/{log_id}/delete")
def delete_line_log(log_id: int, confirm: str = Form(""), _: str = Depends(admin_auth)):
    if confirm != "yes":
        raise HTTPException(400, "請先勾選確認")
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM line_message_logs WHERE id=%s", (log_id,))
            deleted = cur.rowcount
        conn.commit()
    if not deleted:
        raise HTTPException(404, "找不到 LINE 訊息紀錄")
    return RedirectResponse("/admin/logs", status_code=303)


@app.get("/admin/logs/line/delete-all-confirm", response_class=HTMLResponse)
def confirm_delete_all_line_logs(_: str = Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM line_message_logs")
            count = cur.fetchone()["c"]
    detail = f"目前共有 {count} 筆 LINE 訊息紀錄。全部刪除後無法從此系統復原。"
    return _log_confirm_page("line_all", "delete", "確認刪除全部 LINE 訊息紀錄", detail, "/admin/logs/line/delete-all")


@app.post("/admin/logs/line/delete-all")
def delete_all_line_logs(confirm_text: str = Form(""), _: str = Depends(admin_auth)):
    required = "刪除全部 LINE 訊息紀錄"
    if confirm_text.strip() != required:
        raise HTTPException(400, "確認文字不正確，尚未執行刪除")
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM line_message_logs")
            deleted = cur.rowcount
        conn.commit()
    return RedirectResponse("/admin/logs", status_code=303)


@app.get("/admin/logs/notification/{log_id}/delete-confirm", response_class=HTMLResponse)
def confirm_delete_notification_log(log_id: int, _: str = Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,created_at,notification_type,student_name,line_user_id,status FROM notification_logs WHERE id=%s", (log_id,))
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, "找不到出勤通知紀錄")
    detail = f"將刪除 {row['created_at']} 的 {notification_type_label(row['notification_type'])} 紀錄；學生 {row.get('student_name') or '-'}。"
    return _log_confirm_page("notification_one", "delete", "確認刪除出勤通知紀錄", detail, f"/admin/logs/notification/{log_id}/delete")


@app.post("/admin/logs/notification/{log_id}/delete")
def delete_notification_log(log_id: int, confirm: str = Form(""), _: str = Depends(admin_auth)):
    if confirm != "yes":
        raise HTTPException(400, "請先勾選確認")
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM notification_logs WHERE id=%s", (log_id,))
            deleted = cur.rowcount
        conn.commit()
    if not deleted:
        raise HTTPException(404, "找不到出勤通知紀錄")
    return RedirectResponse("/admin/logs", status_code=303)


@app.get("/admin/logs/notification/delete-all-confirm", response_class=HTMLResponse)
def confirm_delete_all_notification_logs(_: str = Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM notification_logs")
            count = cur.fetchone()["c"]
    detail = f"目前共有 {count} 筆出勤通知紀錄。全部刪除後無法從此系統復原。"
    return _log_confirm_page("notification_all", "delete", "確認刪除全部出勤通知紀錄", detail, "/admin/logs/notification/delete-all")


@app.post("/admin/logs/notification/delete-all")
def delete_all_notification_logs(confirm_text: str = Form(""), _: str = Depends(admin_auth)):
    required = "刪除全部出勤通知紀錄"
    if confirm_text.strip() != required:
        raise HTTPException(400, "確認文字不正確，尚未執行刪除")
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM notification_logs")
        conn.commit()
    return RedirectResponse("/admin/logs", status_code=303)


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


@app.post("/admin/test-line/{student_id}", response_class=HTMLResponse)
def test_line(student_id:int, _: str=Depends(admin_auth)):
    now = now_local()
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM students WHERE id=%s AND active=TRUE", (student_id,))
                s = cur.fetchone()
                if not s:
                    raise HTTPException(404, "找不到學生")

                cur.execute(
                    "SELECT line_user_id FROM student_line_bindings WHERE student_id=%s AND active=TRUE GROUP BY line_user_id ORDER BY MIN(id)",
                    (student_id,),
                )
                recipients = [r["line_user_id"] for r in cur.fetchall() if r["line_user_id"]]
                if s.get("line_user_id") and s["line_user_id"] not in recipients:
                    recipients.append(s["line_user_id"])

                if not recipients:
                    body = (
                        f"<section><h1>LINE 測試失敗</h1>"
                        f"<div class='alert danger'>❌ {escape(s['name'])} 目前沒有有效的 LINE User ID。</div>"
                        f"<a class='btn btn2' href='/admin/line'>返回 LINE 綁定</a></section>"
                    )
                    return HTMLResponse(page("LINE 測試失敗", body), status_code=400)

                if LINE_MODE == "live" and not LINE_CHANNEL_ACCESS_TOKEN:
                    body = (
                        f"<section><h1>LINE 尚未設定完成</h1>"
                        f"<div class='alert danger'>❌ Render 尚未設定 LINE_CHANNEL_ACCESS_TOKEN，因此不能進行正式 Push。</div>"
                        f"<p>目前 LINE 模式：<b>正式發送</b></p>"
                        f"<a class='btn btn2' href='/admin/line'>返回 LINE 綁定</a></section>"
                    )
                    return HTMLResponse(page("LINE 尚未設定完成", body), status_code=400)

                msg = f"🔔 LINE 連線測試\n學生：{s['name']}\n時間：{now:%H:%M:%S}\n這是一則測試通知。"
                ok = send_student_line(cur, None, s["id"], msg, "line_test", s.get("line_user_id"))

                # 讀取本次測試最後一筆紀錄，讓管理員直接看到 LINE API 回傳原因。
                cur.execute(
                    "SELECT status,error_message,recipient FROM notification_logs "
                    "WHERE notification_type='line_test' ORDER BY id DESC LIMIT 1"
                )
                log = cur.fetchone()
                conn.commit()

                if ok:
                    detail = "✅ LINE 測試訊息已由系統送出。請立即查看家長 LINE。"
                    if LINE_MODE != "live":
                        detail = "🟡 模擬測試已記錄；目前不是正式 LINE Push。"
                    body = (
                        f"<section><h1>LINE 測試結果</h1>"
                        f"<div class='alert success'>{detail}</div>"
                        f"<p>學生：<b>{escape(s['name'])}</b></p>"
                        f"<p>收件人：<code>{escape(log['recipient'] if log and log['recipient'] else recipients[0])}</code></p>"
                        f"<p>狀態：<b>{escape(log['status'] if log else 'sent')}</b></p>"
                        f"<a class='btn btn2' href='/admin/line'>返回 LINE 綁定</a></section>"
                    )
                    return HTMLResponse(page("LINE 測試結果", body))

                error = (log["error_message"] if log else None) or "LINE Push 未成功，請查看 Render Logs。"
                body = (
                    f"<section><h1>LINE 測試失敗</h1>"
                    f"<div class='alert danger'>❌ {escape(error)}</div>"
                    f"<p>學生：<b>{escape(s['name'])}</b></p>"
                    f"<p>目前模式：<b>{escape(LINE_MODE_LABELS.get(LINE_MODE, LINE_MODE))}</b></p>"
                    f"<a class='btn btn2' href='/admin/line'>返回 LINE 綁定</a></section>"
                )
                return HTMLResponse(page("LINE 測試失敗", body), status_code=502)
    except HTTPException:
        raise
    except Exception as exc:
        # 不再讓按鈕只顯示瀏覽器的 Internal Server Error；直接把可診斷的錯誤留在管理頁。
        detail = str(exc)[:1000]
        body = (
            f"<section><h1>LINE 測試發生系統錯誤</h1>"
            f"<div class='alert danger'>❌ {escape(detail)}</div>"
            f"<p>這代表測試流程本身發生例外，尚未能確認 LINE API 是否成功。請將這段錯誤與 Render Logs 一起提供給我。</p>"
            f"<a class='btn btn2' href='/admin/line'>返回 LINE 綁定</a></section>"
        )
        return HTMLResponse(page("LINE 測試系統錯誤", body), status_code=500)


# 保留管理員手動輸入 ID 的相容端點，但正式介面不提供此操作。
@app.post("/admin/student/{student_id}/toggle-parent-notify")
def toggle_parent_notify(student_id: int, _: str = Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE students SET parent_notify_enabled=NOT parent_notify_enabled WHERE id=%s AND active=TRUE", (student_id,))
            if cur.rowcount == 0:
                raise HTTPException(404, "找不到學生")
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


@app.get("/admin/line/export.csv")
def export_line_bindings(_: str = Depends(admin_auth)):
    buf = io.StringIO(); buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow(["學生編號","學生姓名","LINE 顯示名稱","LINE User ID","關係","啟用","家長提醒"] )
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT s.student_code,s.name,b.display_name,b.line_user_id,b.relation,b.active,b.notify_enabled "
                "FROM student_line_bindings b JOIN students s ON s.id=b.student_id ORDER BY s.name,b.id"
            )
            for r in cur.fetchall():
                writer.writerow([r["student_code"],r["name"],r["display_name"] or "",r["line_user_id"],r["relation"],"是" if r["active"] else "否", "是" if r["notify_enabled"] else "否"])
    data=buf.getvalue().encode("utf-8-sig")
    return StreamingResponse(io.BytesIO(data), media_type="text/csv; charset=utf-8", headers={"Content-Disposition":"attachment; filename=line_bindings.csv"})


def _truthy_binding(value: Any, default: bool = True) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip() in {"是","啟用","1","true","TRUE","yes","Y","y"}


def _normalize_binding_row(raw_row: dict[str, Any]) -> dict[str, str]:
    row = {str(k).strip(): (str(v).strip() if v is not None else "") for k, v in raw_row.items()}
    return {
        "student_code": row.get("學生編號", "") or row.get("學生ID", "") or row.get("student_code", ""),
        "student_name": row.get("學生姓名", "") or row.get("姓名", "") or row.get("name", ""),
        "line_user_id": row.get("LINE User ID", "") or row.get("LINE ID", "") or row.get("LINE_ID", "") or row.get("line_user_id", ""),
        "display_name": row.get("LINE 顯示名稱", "") or row.get("LINE顯示名稱", "") or "",
        "relation": row.get("關係", "") or row.get("家長關係", "") or "家長/監護人",
        "active": "是" if _truthy_binding(row.get("啟用"), True) else "否",
        "notify": "是" if _truthy_binding(row.get("家長提醒"), True) else "否",
    }


def _upsert_students_from_excel(cur, rows: list[dict[str, Any]]) -> set[int]:
    affected: set[int] = set()
    for raw in rows:
        row = {str(k).strip(): (str(v).strip() if v is not None else "") for k, v in raw.items()}
        code = row.get("學生編號", "") or row.get("學生ID", "") or row.get("student_code", "")
        name = row.get("學生姓名", "") or row.get("姓名", "") or row.get("name", "")
        qr_token = row.get("簽到識別碼", "") or row.get("QR Token", "") or row.get("qr_token", "")
        if not code or not name:
            continue
        cur.execute("SELECT id FROM students WHERE student_code=%s", (code,))
        student = cur.fetchone()
        if student:
            sid = student["id"]
            if qr_token:
                cur.execute(
                    "UPDATE students SET name=%s,qr_token=%s,checkin_enabled=%s,notify_checkin_enabled=%s,notify_checkout_enabled=%s,notify_exception_enabled=%s WHERE id=%s",
                    (name, qr_token, _truthy_binding(row.get("簽到啟用"), True), _truthy_binding(row.get("到班通知"), True), _truthy_binding(row.get("離班通知"), True), _truthy_binding(row.get("異常通知"), True), sid),
                )
            else:
                cur.execute(
                    "UPDATE students SET name=%s,checkin_enabled=%s,notify_checkin_enabled=%s,notify_checkout_enabled=%s,notify_exception_enabled=%s WHERE id=%s",
                    (name, _truthy_binding(row.get("簽到啟用"), True), _truthy_binding(row.get("到班通知"), True), _truthy_binding(row.get("離班通知"), True), _truthy_binding(row.get("異常通知"), True), sid),
                )
        else:
            if not qr_token:
                qr_token = f"ATT-{secrets.token_urlsafe(12)}"
            cur.execute(
                "INSERT INTO students(student_code,name,qr_token,checkin_enabled,notify_checkin_enabled,notify_checkout_enabled,notify_exception_enabled) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (code, name, qr_token, _truthy_binding(row.get("簽到啟用"), True), _truthy_binding(row.get("到班通知"), True), _truthy_binding(row.get("離班通知"), True), _truthy_binding(row.get("異常通知"), True)),
            )
            sid = cur.fetchone()["id"]
        affected.add(sid)
    return affected


def _upsert_line_binding_rows(cur, rows: list[dict[str, Any]]) -> tuple[int, set[int]]:
    imported = 0
    affected: set[int] = set()
    for raw_row in rows:
        row = _normalize_binding_row(raw_row)
        if not row["line_user_id"] or (not row["student_code"] and not row["student_name"]):
            continue
        if row["student_code"]:
            cur.execute("SELECT id,name FROM students WHERE student_code=%s AND active=TRUE", (row["student_code"],))
        else:
            cur.execute("SELECT id,name FROM students WHERE name=%s AND active=TRUE ORDER BY id", (row["student_name"],))
        matches = cur.fetchall()
        if len(matches) != 1:
            continue
        student = matches[0]
        active = row["active"] == "是"
        notify = row["notify"] == "是"
        cur.execute("SELECT id FROM student_line_bindings WHERE student_id=%s AND line_user_id=%s", (student["id"], row["line_user_id"]))
        existing = cur.fetchone()
        if existing:
            cur.execute(
                "UPDATE student_line_bindings SET display_name=%s,relation=%s,active=%s,notify_enabled=%s WHERE id=%s",
                (row["display_name"] or None, row["relation"], active, notify, existing["id"]),
            )
        else:
            cur.execute(
                "INSERT INTO student_line_bindings(student_id,line_user_id,display_name,relation,active,notify_enabled,bound_at,bound_by) VALUES (%s,%s,%s,%s,%s,%s,%s,'EXCEL')",
                (student["id"], row["line_user_id"], row["display_name"] or None, row["relation"], active, notify, now_local()),
            )
        affected.add(student["id"])
        imported += 1
    for sid in affected:
        cur.execute(
            "SELECT EXISTS(SELECT 1 FROM student_line_bindings WHERE student_id=%s AND active=TRUE AND notify_enabled=TRUE) AS enabled",
            (sid,),
        )
        enabled = bool(cur.fetchone()["enabled"])
        cur.execute("UPDATE students SET parent_notify_enabled=%s WHERE id=%s", (enabled, sid))
        cur.execute("SELECT line_user_id FROM student_line_bindings WHERE student_id=%s AND active=TRUE GROUP BY line_user_id ORDER BY MIN(id) LIMIT 1", (sid,))
        primary = cur.fetchone()
        cur.execute("UPDATE students SET line_user_id=%s WHERE id=%s", (primary["line_user_id"] if primary else None, sid))
    return imported, affected


def _normalize_excel_time(value: Any) -> str:
    """Normalize Excel numeric/文字時間 to HH:MM."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        number = float(text)
        if 0 <= number < 1:
            total = int(round(number * 24 * 60)) % (24 * 60)
            return f"{total // 60:02d}:{total % 60:02d}"
    except (TypeError, ValueError):
        pass
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", text)
    if m:
        h, mm = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mm <= 59:
            return f"{h:02d}:{mm:02d}"
    return text


def _xlsx_col_index(ref: str) -> int:
    letters = "".join(ch for ch in ref if ch.isalpha())
    n = 0
    for ch in letters.upper():
        n = n * 26 + ord(ch) - 64
    return n - 1


def _xlsx_read_sheet(raw: bytes, target_name: str) -> list[dict[str, str]]:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        ns_main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        ns_rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        ns_pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
        wb_root = ET.fromstring(z.read("xl/workbook.xml"))
        rel_root = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rels = {r.attrib.get("Id"): r.attrib.get("Target") for r in rel_root}
        sheet_target = None
        for sh in wb_root.findall(f"{{{ns_main}}}sheets/{{{ns_main}}}sheet"):
            if sh.attrib.get("name") == target_name:
                rid = sh.attrib.get(f"{{{ns_rel}}}id")
                sheet_target = rels.get(rid)
                break
        if not sheet_target:
            return []
        if sheet_target.startswith("/"):
            sheet_path = sheet_target.lstrip("/")
        elif sheet_target.startswith("xl/"):
            sheet_path = sheet_target
        else:
            sheet_path = "xl/" + sheet_target
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            ss_root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in ss_root.findall(f"{{{ns_main}}}si"):
                shared.append("".join((t.text or "") for t in si.iter(f"{{{ns_main}}}t")))
        root = ET.fromstring(z.read(sheet_path))
        data = root.find(f"{{{ns_main}}}sheetData")
        matrix: list[dict[int,str]] = []
        max_col = -1
        if data is not None:
            for row_el in data.findall(f"{{{ns_main}}}row"):
                row_map: dict[int,str] = {}
                for cell in row_el.findall(f"{{{ns_main}}}c"):
                    ref = cell.attrib.get("r", "")
                    idx = _xlsx_col_index(ref)
                    typ = cell.attrib.get("t")
                    value = ""
                    if typ == "inlineStr":
                        value = "".join((t.text or "") for t in cell.iter(f"{{{ns_main}}}t"))
                    else:
                        v = cell.find(f"{{{ns_main}}}v")
                        value = v.text if v is not None and v.text is not None else ""
                        if typ == "s" and value.isdigit() and int(value) < len(shared):
                            value = shared[int(value)]
                    row_map[idx] = value
                    max_col = max(max_col, idx)
                matrix.append(row_map)
        if not matrix:
            return []
        # 有些 Excel 工作表第一列是標題（例如「聯絡人｜V4 正式資料＋綁定」），
        # 因此不要硬把第 1 列當欄位名稱；改找出包含關鍵欄位的標題列。
        header_idx = 0
        known_headers = {
            "Course ID", "課程日期", "星期", "上課時間", "學生", "課程", "老師", "校區", "來源", "固定課表ID", "調課ID", "調課結果", "備註", "學生編號", "學生姓名", "姓名", "身分", "學生姓名/關聯（可多位）",
            "學生姓名/關聯", "LINE User ID", "LINE ID", "LINE_ID", "關係",
            "家長提醒", "啟用", "簽到識別碼", "下課時間（出勤用）", "開始時間（出勤用）",
            "通知類型", "預設訊息範本", "個別訊息範本", "套用方式", "剩餘次數", "有效至",
            "設定項目", "目前值", "用途", "說明"
        }
        # 不再限制標題只能出現在前 10 列。總表有些工作表在正式欄位前會有較長的說明／標題區，
        # 若硬限制前 10 列，可能把資料列誤當成標題，最後造成例如「出勤學生」讀取 0 筆。
        # 依工作表用途提高標題辨識精準度，並掃描整張工作表。
        preferred_headers = {
            "出勤學生": {"學生編號", "學生姓名", "姓名", "簽到識別碼", "簽到啟用", "到班通知", "離班通知", "異常通知"},
            "出勤LINE綁定": {"學生編號", "學生姓名", "LINE User ID", "LINE ID", "LINE_ID", "關係", "家長提醒", "啟用"},
            "聯絡人": {"姓名", "身分", "LINE User ID", "LINE ID", "學生姓名/關聯（可多位）", "學生姓名/關聯"},
            "實際課程": {"Course ID", "課程日期", "星期", "上課時間", "學生", "課程", "老師", "校區", "來源"},
        }.get(target_name, set())

        best_score = -1
        best_required = 0
        for i, row in enumerate(matrix):
            vals = {str(v).strip() for v in row.values() if str(v).strip()}
            preferred_score = len(vals & preferred_headers) if preferred_headers else 0
            generic_score = len(vals & known_headers)
            # 同分時優先選用途相關欄位較多的列；至少命中 2 個已知欄位才視為標題。
            score = preferred_score * 10 + generic_score
            if generic_score >= 2 and score > best_score:
                best_score = score
                best_required = generic_score
                header_idx = i

        if best_score < 0:
            logger.warning("Excel sheet %s 找不到可辨識的標題列；共讀取 %s 列。", target_name, len(matrix))
            return []

        headers = [matrix[header_idx].get(i, "").strip() for i in range(max_col + 1)]
        # 支援目前總表「實際課程」把「下課時間（出勤用）」放在上一列標題區的情況。
        for prior_row in matrix[:header_idx]:
            for i, value in prior_row.items():
                label = str(value).strip()
                if label in known_headers and (i >= len(headers) or not headers[i]):
                    if i >= len(headers):
                        headers.extend([""] * (i + 1 - len(headers)))
                    headers[i] = label
        out = []
        for row in matrix[header_idx + 1:]:
            obj = {headers[i]: row.get(i, "") for i in range(len(headers)) if headers[i]}
            if any(str(v).strip() for v in obj.values()):
                out.append(obj)
        return out



def _parse_excel_date_value(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # 支援 Excel serial date（1900 date system）與常見文字格式。
    try:
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            serial = float(text)
            if serial > 30000:
                return (date(1899, 12, 30) + timedelta(days=int(serial)))
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _weekday_index(value: Any, fallback: date) -> int:
    text = str(value or "").strip()
    mapping = {"一":0,"二":1,"三":2,"四":3,"五":4,"六":5,"日":6,"天":6,
               "星期一":0,"星期二":1,"星期三":2,"星期四":3,"星期五":4,"星期六":5,"星期日":6,"星期天":6}
    if text in mapping:
        return mapping[text]
    return fallback.weekday()


def _upsert_actual_courses_from_excel(cur, rows: list[dict[str, Any]]) -> tuple[int, int, list[str]]:
    """Import the master-sheet 實際課程 snapshot.

    The master sheet is intentionally allowed to keep only the start time in
    「上課時間」.  If the end time is embedded in 「備註」, e.g.
    「原時段 16:30-19:30」, extract it automatically.

    Group lessons may contain multiple students in one row, e.g.
    「葉依柔、陳翊森」.  Each student gets an independent attendance course,
    while the original Course ID is retained as the base identifier and a
    deterministic student suffix is added only when needed to satisfy the
    unique actual_course_id index.
    """
    imported = 0
    skipped = 0
    errors: list[str] = []
    parsed_rows: list[dict[str, Any]] = []
    dates: set[date] = set()
    invalid_dates: set[date] = set()

    def split_student_names(value: str) -> list[str]:
        text = str(value or "").strip()
        if not text:
            return []
        # GPT整理課表常用「、」；同時容許逗號、頓號及換行。
        parts = re.split(r"[、，,；;／/\\n]+", text)
        return [p.strip() for p in parts if p.strip()]

    def extract_end_time_from_note(note: str) -> tuple[str, str]:
        """Return (start, end) from 原時段/時段 text when present."""
        text = str(note or "")
        # 優先抓「原時段 16:30-19:30」，也容許 ～、至、到、全形符號。
        patterns = [
            r"(?:原時段|原時間|時段|課程時間)\s*[:：]?\s*(\d{1,2}:\d{2})\s*[-~～—–至到]\s*(\d{1,2}:\d{2})",
            r"(\d{1,2}:\d{2})\s*[-~～—–至到]\s*(\d{1,2}:\d{2})",
        ]
        for pattern in patterns:
            m = re.search(pattern, text)
            if m:
                return _normalize_excel_time(m.group(1)), _normalize_excel_time(m.group(2))
        return "", ""

    for i, raw in enumerate(rows, start=2):
        row = {str(k).strip(): (str(v).strip() if v is not None else "") for k, v in raw.items()}
        actual_id = row.get("Course ID", "") or row.get("課程ID", "")
        course_date = _parse_excel_date_value(row.get("課程日期") or row.get("日期"))
        student_text = row.get("學生", "") or row.get("學生姓名", "") or row.get("姓名", "")
        time_text = row.get("上課時間", "") or row.get("課程時間", "") or ""
        note_text = row.get("備註", "") or ""

        start_time = row.get("開始時間（出勤用）", "") or row.get("開始時間", "") or row.get("開始", "") or ""
        end_time = row.get("下課時間（出勤用）", "") or row.get("下課時間", "") or row.get("結束時間", "") or row.get("結束", "") or ""
        start_time = _normalize_excel_time(start_time)
        end_time = _normalize_excel_time(end_time)

        if not start_time and time_text:
            m = re.search(r"(\d{1,2}:\d{2})\s*[-~～—–至到]\s*(\d{1,2}:\d{2})", str(time_text))
            if m:
                start_time, end_time = m.group(1), m.group(2)
            else:
                start_time = _normalize_excel_time(time_text)

        # 目前總表把完整時段放在備註，例如「原時段 17:00-19:00」。
        # 若欄位本身沒有下課時間，就從備註補上；欄位本身有值則優先保留。
        note_start, note_end = extract_end_time_from_note(note_text)
        if not start_time and note_start:
            start_time = note_start
        if not end_time and note_end:
            end_time = note_end

        start_time = _normalize_excel_time(start_time)
        end_time = _normalize_excel_time(end_time)

        student_names = split_student_names(student_text)
        if not actual_id or not course_date or not student_names or not start_time:
            if course_date:
                invalid_dates.add(course_date)
            skipped += 1
            if any(row.values()):
                errors.append(f"第 {i} 列缺少 Course ID／日期／學生／上課時間，已略過。")
            continue

        try:
            parse_hhmm(start_time, course_date)
            if end_time:
                parse_hhmm(end_time, course_date)
        except ValueError:
            if course_date:
                invalid_dates.add(course_date)
            skipped += 1
            errors.append(f"第 {i} 列時間格式錯誤：{start_time}-{end_time}。")
            continue

        # 一列多人課拆成多筆獨立簽到課程。
        for student_index, student_name in enumerate(student_names, start=1):
            student_code = ""
            # 優先使用整列的學生編號；若多人課沒有學生編號，改用姓名唯一對應。
            raw_student_code = row.get("學生編號", "") or row.get("學生ID", "")
            if len(student_names) == 1:
                student_code = raw_student_code

            if student_code:
                cur.execute(
                    "SELECT id,name FROM students WHERE student_code=%s AND active=TRUE",
                    (student_code,),
                )
                matches = cur.fetchall()
            else:
                cur.execute(
                    "SELECT id,name FROM students WHERE name=%s AND active=TRUE ORDER BY id",
                    (student_name,),
                )
                matches = cur.fetchall()

            if len(matches) != 1:
                invalid_dates.add(course_date)
                skipped += 1
                errors.append(
                    f"第 {i} 列學生「{student_name}」無法唯一對應（找到 {len(matches)} 位）。"
                )
                continue

            student_id = matches[0]["id"]
            # 保留原 Course ID；多人課為每位學生產生穩定、可重複同步的子 ID。
            normalized_actual_id = (
                actual_id
                if len(student_names) == 1
                else f"{actual_id}__S{student_id}"
            )

            parsed_rows.append({
                "actual_id": normalized_actual_id,
                "base_actual_id": actual_id,
                "course_date": course_date,
                "student_id": student_id,
                "student_name": student_name,
                "weekday": _weekday_index(row.get("星期"), course_date),
                "start_time": start_time,
                "end_time": end_time,
                "course_name": row.get("課程", "") or "未命名課程",
                "teacher_name": row.get("老師", ""),
                "source_note": "；".join(
                    [
                        x
                        for x in [
                            row.get("來源", ""),
                            row.get("固定課表ID", ""),
                            row.get("調課ID", ""),
                            row.get("調課結果", ""),
                            note_text,
                        ]
                        if x
                    ]
                ),
            })
            dates.add(course_date)

    # 以「實際課程」為該日期唯一來源：該日期原本同步進來的課程先停用，再寫入新快照。
    for d in dates - invalid_dates:
        cur.execute(
            "UPDATE courses SET active=FALSE WHERE actual_course_id IS NOT NULL "
            "AND COALESCE(source,'實際課程')='實際課程' AND course_date=%s",
            (d,),
        )

    for r in parsed_rows:
        cur.execute("SELECT id FROM courses WHERE actual_course_id=%s", (r["actual_id"],))
        existing = cur.fetchone()
        if existing:
            cur.execute(
                """UPDATE courses SET student_id=%s,course_name=%s,teacher_name=%s,weekday=%s,start_time=%s,end_time=%s,
                   late_grace_minutes=%s,checkout_grace_minutes=%s,active=TRUE,course_date=%s,source='實際課程',source_note=%s
                   WHERE id=%s""",
                (
                    r["student_id"], r["course_name"], r["teacher_name"] or None, r["weekday"],
                    r["start_time"], r["end_time"], DEFAULT_LATE_GRACE_MINUTES,
                    DEFAULT_CHECKOUT_GRACE_MINUTES, r["course_date"], r["source_note"] or None,
                    existing["id"],
                ),
            )
        else:
            cur.execute(
                """INSERT INTO courses(student_id,course_name,teacher_name,weekday,start_time,end_time,late_grace_minutes,checkout_grace_minutes,
                   active,actual_course_id,course_date,source,source_note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s,'實際課程',%s)""",
                (
                    r["student_id"], r["course_name"], r["teacher_name"] or None, r["weekday"],
                    r["start_time"], r["end_time"], DEFAULT_LATE_GRACE_MINUTES,
                    DEFAULT_CHECKOUT_GRACE_MINUTES, r["actual_id"], r["course_date"],
                    r["source_note"] or None,
                ),
            )
        imported += 1

    return imported, skipped, errors


# ---------- Master Excel automatic sync ----------

def _decode_service_account_json(raw: str) -> dict[str, Any]:
    """Parse a Service Account credential from JSON, quoted JSON, or Base64.

    Render environment variables are plain strings, so users may accidentally
    paste the JSON into the Base64 variable (or vice versa).  Accept both
    forms and do not expose the credential contents in errors/logs.
    """
    value = (raw or "").strip().lstrip("\ufeff")
    if not value:
        raise ValueError("內容為空")

    def parse_json(text: str) -> dict[str, Any]:
        text = text.strip().lstrip("\ufeff")
        obj = json.loads(text)
        # Accept JSON stored as a JSON string, e.g. quoted/escaped JSON.
        if isinstance(obj, str):
            obj = json.loads(obj)
        if not isinstance(obj, dict):
            raise ValueError("JSON 根節點不是物件")
        required = ("type", "project_id", "private_key", "client_email")
        missing = [key for key in required if not str(obj.get(key) or "").strip()]
        if missing:
            raise ValueError("缺少必要欄位：" + ", ".join(missing))
        if str(obj.get("type")).strip() != "service_account":
            raise ValueError("type 不是 service_account")
        # Some copy/paste paths double-escape private_key newlines.
        if isinstance(obj.get("private_key"), str):
            obj["private_key"] = obj["private_key"].replace("\\n", "\n")
        return obj

    # First try the value exactly as supplied. This supports raw JSON and also
    # catches JSON that was accidentally pasted into the *_BASE64 variable.
    try:
        return parse_json(value)
    except Exception:
        pass

    # Some deployment UIs preserve shell-style escaping such as {\"type\":...}.
    # Try that form before treating the value as Base64.
    try:
        if value.startswith("{") and "\\\"" in value:
            return parse_json(value.replace("\\\"", "\""))
    except Exception:
        pass

    # Then try standard or URL-safe Base64 with optional missing padding.
    try:
        compact = re.sub(r"\s+", "", value).strip('\"\'')
        padded = compact + ("=" * (-len(compact) % 4))
        try:
            decoded_bytes = base64.b64decode(padded.encode("ascii"), validate=True)
        except Exception:
            decoded_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
        decoded = decoded_bytes.decode("utf-8-sig")
        return parse_json(decoded)
    except Exception as exc:
        raise ValueError("不是有效的 Service Account JSON 或 Base64 JSON") from exc


def _master_sync_credential_identity(creds) -> str:
    """Return only the non-secret Service Account identity for diagnostics."""
    return str(getattr(creds, "service_account_email", "") or "")

def _google_response_error(resp, operation: str) -> RuntimeError:
    """Preserve Google's useful error details without exposing credentials."""
    try:
        payload = resp.json()
        err = payload.get("error", payload) if isinstance(payload, dict) else payload
        if isinstance(err, dict):
            code = err.get("code", resp.status_code)
            status = err.get("status", "")
            message = err.get("message", "")
            details = err.get("errors") or err.get("details") or []
            reason = ""
            if isinstance(details, list):
                reasons = []
                for item in details:
                    if isinstance(item, dict) and item.get("reason"):
                        reasons.append(str(item.get("reason")))
                if reasons:
                    reason = "；reason=" + ",".join(dict.fromkeys(reasons))
            detail = f"HTTP {code}" + (f" {status}" if status else "") + (f"：{message}" if message else "") + reason
        else:
            detail = f"HTTP {resp.status_code}：{str(err)[:500]}"
    except Exception:
        detail = f"HTTP {resp.status_code}：{resp.text[:500]}"
    return RuntimeError(f"Google Drive {operation} 失敗：{detail}")

def _master_sync_credentials():
    # Read the current environment at call time as well as the startup snapshot.
    # This makes the function easier to diagnose and avoids relying solely on
    # module-import-time values in long-lived workers.
    candidates = [
        ("GOOGLE_SERVICE_ACCOUNT_JSON", os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()),
        ("GOOGLE_SERVICE_ACCOUNT_JSON_BASE64", os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_BASE64", "").strip()),
        ("GOOGLE_SERVICE_ACCOUNT_JSON", GOOGLE_SERVICE_ACCOUNT_JSON),
        ("GOOGLE_SERVICE_ACCOUNT_JSON_BASE64", GOOGLE_SERVICE_ACCOUNT_JSON_BASE64),
    ]
    seen: set[tuple[str, str]] = set()
    errors: list[str] = []
    for source, value in candidates:
        if not value or (source, value) in seen:
            continue
        seen.add((source, value))
        try:
            info = _decode_service_account_json(value)
            logger.info("Google Service Account credentials parsed from %s", source)
            return service_account.Credentials.from_service_account_info(
                info,
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
            )
        except Exception as exc:
            errors.append(f"{source}: {str(exc)[:180]}")

    if not seen:
        raise RuntimeError("尚未設定 GOOGLE_SERVICE_ACCOUNT_JSON_BASE64（或 GOOGLE_SERVICE_ACCOUNT_JSON）。")
    raise RuntimeError("Google Service Account JSON 無法解析；已嘗試可用的 JSON／Base64 設定，但都不是有效的 Service Account 憑證。")


def _download_master_from_google_drive() -> tuple[bytes, dict[str, str]]:
    if not GOOGLE_DRIVE_FILE_ID:
        raise RuntimeError("尚未設定 GOOGLE_DRIVE_FILE_ID。")
    creds = _master_sync_credentials()
    service_account_email = _master_sync_credential_identity(creds)
    logger.info(
        "Google Drive sync request: service_account=%s file_id=%s",
        service_account_email or "(unknown)",
        GOOGLE_DRIVE_FILE_ID,
    )
    session = AuthorizedSession(creds)
    meta_url = f"https://www.googleapis.com/drive/v3/files/{GOOGLE_DRIVE_FILE_ID}"
    meta_resp = session.get(
        meta_url,
        params={"fields": "id,name,mimeType,modifiedTime,md5Checksum,size,capabilities(canDownload)"},
        timeout=30,
    )
    if not meta_resp.ok:
        raise _google_response_error(meta_resp, "讀取檔案資訊")
    meta = meta_resp.json()
    logger.info(
        "Google Drive file metadata: service_account=%s file_id=%s name=%s mimeType=%s canDownload=%s",
        service_account_email or "(unknown)",
        meta.get("id", GOOGLE_DRIVE_FILE_ID),
        meta.get("name", ""),
        meta.get("mimeType", ""),
        meta.get("capabilities", {}).get("canDownload"),
    )
    if meta.get("capabilities", {}).get("canDownload") is False:
        raise RuntimeError("Google Drive 檔案目前禁止下載。")
    name = str(meta.get("name") or "master.xlsx")
    mime = str(meta.get("mimeType") or "")
    native_sheet_mime = "application/vnd.google-apps.spreadsheet"
    if mime == native_sheet_mime:
        # Google 試算表原生文件：透過 Drive export API 轉成 XLSX，再沿用既有 Excel 解析器。
        export_url = f"https://www.googleapis.com/drive/v3/files/{GOOGLE_DRIVE_FILE_ID}/export"
        download_resp = session.get(
            export_url,
            params={"mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
            timeout=60,
        )
        if not download_resp.ok:
            raise _google_response_error(download_resp, "匯出 Google 試算表")
        raw = download_resp.content
        source_name = f"{name}.xlsx"
    else:
        if not name.lower().endswith(".xlsx"):
            raise RuntimeError(f"Google Drive 指定檔案不是支援的 XLSX 或 Google 試算表：{name}（{mime}）")
        size = int(meta.get("size") or 0)
        if size and size > MASTER_SYNC_MAX_MB * 1024 * 1024:
            raise RuntimeError(f"總表檔案 {size / 1024 / 1024:.1f} MB 超過上限 {MASTER_SYNC_MAX_MB} MB。")
        download_resp = session.get(meta_url, params={"alt": "media"}, timeout=60)
        if not download_resp.ok:
            raise _google_response_error(download_resp, "下載 XLSX")
        raw = download_resp.content
        source_name = name
    if len(raw) > MASTER_SYNC_MAX_MB * 1024 * 1024:
        raise RuntimeError(f"下載後總表超過上限 {MASTER_SYNC_MAX_MB} MB。")
    checksum = str(meta.get("md5Checksum") or hashlib.md5(raw).hexdigest())
    return raw, {
        "provider": "google_drive",
        "source_key": GOOGLE_DRIVE_FILE_ID,
        "source_name": source_name,
        "remote_modified_at": str(meta.get("modifiedTime") or ""),
        "remote_checksum": checksum,
        "mime_type": mime,
    }


def _load_master_workbook_sheets(raw: bytes) -> dict[str, list[dict[str, Any]]]:
    if not raw.startswith(b"PK"):
        raise RuntimeError("主總表不是有效的 .xlsx 檔案。")
    names = {
        "出勤學生": "出勤學生",
        "出勤LINE綁定": "出勤LINE綁定",
        "聯絡人": "聯絡人",
        "實際課程": "實際課程",
        "課程提醒": "課程提醒",
        "出勤設定": "出勤設定",
        "出勤通知模板": "出勤通知模板",
        "出勤通知個別": "出勤通知個別",
    }
    out = {}
    for key, sheet in names.items():
        out[key] = _xlsx_read_sheet(raw, sheet)
    # 目前正式總表沒有「出勤學生」工作表；學生名單直接由「實際課程」建立。
    # 因此「實際課程」是出勤同步的必要來源，其他工作表都是輔助資料。
    if not out["實際課程"]:
        raise RuntimeError("總表沒有可讀取的「實際課程」資料；目前版本以「實際課程」作為出勤學生與課程的主要來源。")
    logger.info(
        "Master workbook sheets loaded: 實際課程=%s, 出勤LINE綁定=%s, 聯絡人=%s, 出勤設定=%s, 出勤通知模板=%s, 出勤通知個別=%s",
        len(out["實際課程"]), len(out["出勤LINE綁定"]), len(out["聯絡人"]),
        len(out["出勤設定"]), len(out["出勤通知模板"]), len(out["出勤通知個別"])
    )
    return out


def _apply_master_sync(cur, sheets: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    student_rows = sheets.get("出勤學生", [])
    binding_rows = sheets.get("出勤LINE綁定", [])
    contact_rows = sheets.get("聯絡人", [])
    actual_course_rows = sheets.get("實際課程", [])
    attendance_settings_rows = sheets.get("出勤設定", [])
    attendance_template_rows = sheets.get("出勤通知模板", [])
    attendance_individual_template_rows = sheets.get("出勤通知個別", [])

    actual_imported = actual_skipped = binding_imported = 0
    errors: list[str] = []
    cur.execute("SELECT COUNT(*) AS c FROM students WHERE active=TRUE")
    student_count_before = cur.fetchone()["c"]

    # 正式總表沒有「出勤學生」工作表：
    # 先從「實際課程」的「學生」欄建立學生，再解析課程。
    # 若未來總表恢復「出勤學生」，仍保留原本的欄位同步邏輯。
    student_source = student_rows
    student_source_name = "出勤學生"
    if not student_source:
        derived_rows = []
        seen_names = set()
        for raw in actual_course_rows:
            name_text = str(raw.get("學生", "") or raw.get("學生姓名", "") or raw.get("姓名", "")).strip()
            for student_name in re.split(r"[、，,；;／/\\n]+", name_text):
                student_name = student_name.strip()
                if not student_name or student_name in seen_names:
                    continue
                seen_names.add(student_name)
                # 「實際課程」沒有學生編號時，建立穩定的系統學生編號。
                # 這樣 _upsert_students_from_excel 不會因為缺少「學生編號」而略過學生，
                # 同時同一姓名每次同步都會得到相同編號，不會重複建立學生。
                stable_code = "ACTUAL-" + hashlib.sha1(student_name.encode("utf-8")).hexdigest()[:12].upper()
                derived_rows.append({
                    "學生姓名": student_name,
                    "學生編號": str(raw.get("學生編號", "") or raw.get("學生ID", "")).strip() or stable_code,
                })
        student_source = derived_rows
        student_source_name = "實際課程→學生"
        logger.info(
            "Master sync: no 出勤學生 sheet; deriving students from 實際課程「學生」欄, derived_unique_students=%s, sample=%s",
            len(derived_rows), [r.get("學生姓名") for r in derived_rows[:20]]
        )

    _upsert_students_from_excel(cur, student_source)
    cur.execute("SELECT COUNT(*) AS c FROM students WHERE active=TRUE")
    student_count_after = cur.fetchone()["c"]
    logger.info(
        "Master sync sheet counts: 出勤學生=%s, student_source=%s, 實際課程=%s, 出勤LINE綁定=%s; active_students_before=%s after=%s",
        len(student_rows), student_source_name, len(actual_course_rows), len(binding_rows), student_count_before, student_count_after
    )
    if actual_course_rows:
        actual_imported, actual_skipped, errors = _upsert_actual_courses_from_excel(cur, actual_course_rows)
        logger.info(
            "Master sync actual-course parse: imported=%s skipped=%s errors=%s sample_errors=%s",
            actual_imported, actual_skipped, len(errors), errors[:20]
        )
        # 自動同步的安全門檻：如果原檔有實際課程資料，但一筆都無法有效解析，不覆蓋目前線上資料。
        if actual_imported == 0:
            detail = "；".join(errors[:12]) if errors else "沒有產生逐列錯誤資訊"
            raise RuntimeError(
                "總表「實際課程」有資料，但本次沒有任何一筆成功解析；"
                f"學生來源 {student_source_name}、讀取 {len(student_source)} 筆、同步後啟用學生 {student_count_after} 筆；"
                f"實際課程讀取 {len(actual_course_rows)} 筆、跳過 {actual_skipped} 筆。"
                f"原因：{detail}"
            )
    if attendance_settings_rows:
        sync_runtime_settings(cur, attendance_settings_rows)
    if attendance_template_rows:
        sync_default_templates_from_excel(cur, attendance_template_rows)
    if attendance_individual_template_rows:
        sync_individual_templates_from_excel(cur, attendance_individual_template_rows)
    if binding_rows:
        binding_imported, _ = _upsert_line_binding_rows(cur, binding_rows)
    if binding_imported == 0 and contact_rows:
        fallback_rows = []
        for r in contact_rows:
            role = str(r.get("身分", "")).strip()
            line_id = str(r.get("LINE User ID", "") or r.get("LINE ID", "") or r.get("LINE_ID", "")).strip()
            person = str(r.get("姓名", "")).strip()
            related = str(r.get("學生姓名/關聯（可多位）", "") or r.get("學生姓名/關聯", "")).strip()
            if role != "家長" or not line_id or not related:
                continue
            for student_name in [x.strip() for x in related.replace("，", "、").split("、") if x.strip()]:
                fallback_rows.append({"學生姓名": student_name, "LINE User ID": line_id, "LINE 顯示名稱": person, "關係": "家長", "啟用": "是", "家長提醒": "是"})
        binding_imported, _ = _upsert_line_binding_rows(cur, fallback_rows)
    load_runtime_settings(cur)
    return {
        "actual_imported": actual_imported,
        "actual_skipped": actual_skipped,
        "binding_imported": binding_imported,
        "errors": errors,
    }


def _get_master_sync_state(cur) -> dict[str, Any]:
    cur.execute("SELECT * FROM master_sync_state WHERE id=1")
    row = cur.fetchone()
    return dict(row) if row else {}


def _master_sync_configured() -> tuple[bool, str]:
    if not MASTER_SYNC_ENABLED:
        return False, "自動同步已關閉（MASTER_SYNC_ENABLED=false）"
    if MASTER_SYNC_PROVIDER != "google_drive":
        return False, f"目前不支援的同步來源：{MASTER_SYNC_PROVIDER}"
    if not GOOGLE_DRIVE_FILE_ID:
        return False, "缺少 GOOGLE_DRIVE_FILE_ID"
    if not (GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_JSON_BASE64):
        return False, "缺少 GOOGLE_SERVICE_ACCOUNT_JSON_BASE64（或 GOOGLE_SERVICE_ACCOUNT_JSON）"
    return True, "已設定"


def master_sync_once(trigger: str = "scheduled") -> dict[str, Any]:
    configured, config_detail = _master_sync_configured()
    result = {"status": "disabled", "detail": config_detail}
    if not configured:
        return result
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS locked", (MASTER_SYNC_LOCK_KEY,))
            if not cur.fetchone()["locked"]:
                return {"status": "locked", "detail": "另一個 worker 正在同步"}
            try:
                cur.execute("UPDATE master_sync_state SET last_checked_at=%s,last_trigger=%s,updated_at=%s WHERE id=1", (now_local(), trigger, now_local()))
                conn.commit()
                try:
                    raw, meta = _download_master_from_google_drive()
                except Exception as exc:
                    cur.execute("UPDATE master_sync_state SET last_success=FALSE,last_error=%s,last_trigger=%s,updated_at=%s WHERE id=1", (str(exc)[:2000], trigger, now_local()))
                    conn.commit()
                    logger.exception("Master Excel sync download failed")
                    return {"status": "failed", "detail": str(exc)[:1000]}

                cur.execute("SELECT remote_modified_at,remote_checksum,last_success FROM master_sync_state WHERE id=1")
                state = cur.fetchone() or {}
                if meta.get("remote_modified_at") and meta.get("remote_checksum") and state.get("remote_modified_at") == meta.get("remote_modified_at") and state.get("remote_checksum") == meta.get("remote_checksum") and state.get("last_success") is True:
                    cur.execute("UPDATE master_sync_state SET last_checked_at=%s,last_trigger=%s,last_error=NULL,updated_at=%s WHERE id=1", (now_local(), trigger, now_local()))
                    conn.commit()
                    return {"status": "unchanged", "source_name": meta.get("source_name"), "detail": "主總表沒有更新"}

                try:
                    sheets = _load_master_workbook_sheets(raw)
                    summary = _apply_master_sync(cur, sheets)
                    cur.execute("""UPDATE master_sync_state SET provider=%s,source_key=%s,source_name=%s,remote_modified_at=%s,remote_checksum=%s,
                        last_checked_at=%s,last_synced_at=%s,last_success=TRUE,last_error=NULL,last_trigger=%s,
                        last_actual_imported=%s,last_actual_skipped=%s,last_binding_imported=%s,last_error_count=%s,updated_at=%s WHERE id=1""",
                        (meta.get("provider"), meta.get("source_key"), meta.get("source_name"), meta.get("remote_modified_at"), meta.get("remote_checksum"),
                         now_local(), now_local(), trigger, summary["actual_imported"], summary["actual_skipped"], summary["binding_imported"], len(summary["errors"]), now_local()))
                    conn.commit()
                    logger.info("Master Excel sync success: %s", summary)
                    return {"status": "synced", "source_name": meta.get("source_name"), **summary}
                except Exception as exc:
                    conn.rollback()
                    cur.execute("UPDATE master_sync_state SET provider=%s,source_key=%s,source_name=%s,remote_modified_at=%s,remote_checksum=%s,last_success=FALSE,last_error=%s,last_trigger=%s,updated_at=%s WHERE id=1",
                                (meta.get("provider"), meta.get("source_key"), meta.get("source_name"), meta.get("remote_modified_at"), meta.get("remote_checksum"), str(exc)[:2000], trigger, now_local()))
                    conn.commit()
                    logger.exception("Master Excel sync apply failed")
                    return {"status": "failed", "source_name": meta.get("source_name"), "detail": str(exc)[:1000]}
            finally:
                try:
                    cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (MASTER_SYNC_LOCK_KEY,))
                except Exception:
                    pass


async def periodic_master_sync():
    if MASTER_SYNC_ENABLED and MASTER_SYNC_ON_STARTUP:
        await asyncio.sleep(5)
        await asyncio.to_thread(master_sync_once, "startup")
    while True:
        await asyncio.sleep(MASTER_SYNC_INTERVAL_MINUTES * 60)
        if MASTER_SYNC_ENABLED:
            await asyncio.to_thread(master_sync_once, "scheduled")


@app.get("/admin/master-sync", response_class=HTMLResponse)
def admin_master_sync(_: str = Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            state = _get_master_sync_state(cur)
    configured, config_detail = _master_sync_configured()
    status_label = {True:"正常", False:"未完成"}.get(bool(state.get("last_success")), "尚未同步") if state else "尚未同步"
    color = "success" if state.get("last_success") else "alert"
    body = [f"<div class='top'><div><h1>主總表自動同步</h1><div class='muted'>V{APP_VERSION}｜主總表放在 Google Drive，Render 自動檢查更新；手動同步保留為備援。</div></div>{admin_nav()}</div>"]
    body.append(f"<section><h2>同步狀態</h2><div class='alert {color}'><b>{escape(status_label)}</b>｜{escape(config_detail)}</div><table>"
                f"<tr><th>同步來源</th><td>{escape(str(state.get('source_name') or '尚未同步'))}</td></tr>"
                f"<tr><th>最後檢查</th><td>{escape(str(state.get('last_checked_at') or '—'))}</td></tr>"
                f"<tr><th>最後成功同步</th><td>{escape(str(state.get('last_synced_at') or '—'))}</td></tr>"
                f"<tr><th>來源最後修改</th><td>{escape(str(state.get('remote_modified_at') or '—'))}</td></tr>"
                f"<tr><th>實際課程</th><td>{escape(str(state.get('last_actual_imported') or 0))} 筆（跳過 {escape(str(state.get('last_actual_skipped') or 0))} 筆）</td></tr>"
                f"<tr><th>LINE 綁定</th><td>{escape(str(state.get('last_binding_imported') or 0))} 筆</td></tr>"
                f"<tr><th>上次錯誤</th><td style='white-space:pre-wrap'>{escape(str(state.get('last_error') or '無'))}</td></tr></table>"
                f"<p><b>自動檢查間隔：</b>{MASTER_SYNC_INTERVAL_MINUTES} 分鐘</p>"
                f"<form method='post' action='/admin/master-sync/now'><button>立即檢查並同步</button></form></section>")
    body.append("<section><h2>安全原則</h2><p>只有通過 Excel 結構與實際課程解析的資料才會寫入；同步失敗會保留上一份可用資料，不會因壞檔直接清空出勤資料。</p><p>Google Drive 檔案不需要公開分享；建議只把這一份總表分享給本服務專用的 Service Account。</p></section>")
    return page("主總表自動同步", ''.join(body))


@app.post("/admin/master-sync/now")
def admin_master_sync_now(_: str = Depends(admin_auth)):
    result = master_sync_once("manual")
    return RedirectResponse(f"/admin/master-sync?status={result.get('status')}", status_code=303)

def sync_master_excel_manual(raw: bytes) -> dict[str, Any]:
    if not raw.startswith(b"PK"):
        raise HTTPException(400, "這不是有效的 .xlsx 檔案。")
    sheets = _load_master_workbook_sheets(raw)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS locked", (MASTER_SYNC_LOCK_KEY,))
            if not cur.fetchone()["locked"]:
                raise HTTPException(409, "目前正有自動同步正在執行，請稍後再試。")
            try:
                summary = _apply_master_sync(cur, sheets)
                cur.execute("""UPDATE master_sync_state SET provider='manual_upload',source_name=%s,last_checked_at=%s,last_synced_at=%s,last_success=TRUE,last_error=NULL,last_trigger='manual_upload',last_actual_imported=%s,last_actual_skipped=%s,last_binding_imported=%s,last_error_count=%s,updated_at=%s WHERE id=1""",
                            ("管理員手動上傳總表", now_local(), now_local(), summary["actual_imported"], summary["actual_skipped"], summary["binding_imported"], len(summary["errors"]), now_local()))
                conn.commit()
                return summary
            finally:
                try:
                    cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (MASTER_SYNC_LOCK_KEY,))
                except Exception:
                    pass


@app.post("/admin/line/import.csv")
async def import_line_bindings(file: UploadFile = File(...), _: str = Depends(admin_auth)):
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "CSV 必須使用 UTF-8 編碼。")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    with db_conn() as conn:
        with conn.cursor() as cur:
            imported, _ = _upsert_line_binding_rows(cur, rows)
            conn.commit()
    return RedirectResponse(f"/admin/line?imported={imported}", status_code=303)


@app.post("/admin/line/import.xlsx")
async def import_line_master_xlsx(file: UploadFile = File(...), _: str = Depends(admin_auth)):
    raw = await file.read()
    try:
        result = sync_master_excel_manual(raw)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"總表同步失敗：{exc}") from exc
    return RedirectResponse(
        f"/admin/line?imported={result['binding_imported']}&actual_imported={result['actual_imported']}&actual_skipped={result['actual_skipped']}&errors={len(result['errors'])}&source=xlsx",
        status_code=303,
    )


@app.post("/admin/student/{student_id}/binding/{binding_id}/toggle-notify")
def toggle_binding_notify(student_id: int, binding_id: int, _: str = Depends(admin_auth)):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE student_line_bindings SET notify_enabled=NOT notify_enabled WHERE id=%s AND student_id=%s", (binding_id, student_id))
            if cur.rowcount == 0:
                raise HTTPException(404, "找不到 LINE 綁定")
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM student_line_bindings WHERE student_id=%s AND active=TRUE AND notify_enabled=TRUE) AS enabled",
                (student_id,),
            )
            enabled = bool(cur.fetchone()["enabled"])
            cur.execute("UPDATE students SET parent_notify_enabled=%s WHERE id=%s", (enabled, student_id))
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


@lru_cache(maxsize=512)
def cached_qr_png(scan_url: str) -> bytes:
    img = qrcode.make(scan_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@app.get("/qr/{student_code}.png")
def qr(student_code: str):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT qr_token FROM students WHERE student_code=%s AND active=TRUE", (student_code,))
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, "找不到學生")
    data = cached_qr_png(f"{public_base_url()}/scan/{row['qr_token']}")
    return StreamingResponse(
        io.BytesIO(data),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=604800, immutable"},
    )

@app.post("/admin/student/{student_id}/delete-test")
def delete_test_student(student_id: int, _: str = Depends(admin_auth)):
    """Hard-delete only test-coded students (STU-*)."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,name,student_code FROM students WHERE id=%s", (student_id,))
            student = cur.fetchone()
            if not student:
                raise HTTPException(404, "找不到學生")
            code = str(student["student_code"] or "").strip().upper()
            if not code.startswith("STU-"):
                raise HTTPException(400, "為避免誤刪正式學生，只允許刪除學生編號以 STU- 開頭的測試資料。")
            cur.execute("DELETE FROM attendance WHERE student_id=%s", (student_id,))
            cur.execute("DELETE FROM schedule_overrides WHERE course_id IN (SELECT id FROM courses WHERE student_id=%s)", (student_id,))
            cur.execute("DELETE FROM courses WHERE student_id=%s", (student_id,))
            cur.execute("DELETE FROM student_line_bindings WHERE student_id=%s", (student_id,))
            try:
                cur.execute("DELETE FROM notification_templates WHERE student_id=%s", (student_id,))
            except Exception:
                pass
            try:
                cur.execute("DELETE FROM line_bind_tokens WHERE student_id=%s", (student_id,))
            except Exception:
                pass
            cur.execute("DELETE FROM students WHERE id=%s", (student_id,))
            conn.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/student/{student_id}/qr-regenerate")
def regenerate_student_qr(student_id: int, _: str = Depends(admin_auth)):
    new_token = secrets.token_urlsafe(18)
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE students SET qr_token=%s WHERE id=%s AND active=TRUE", (new_token, student_id))
            if cur.rowcount == 0:
                raise HTTPException(404, "找不到學生")
            conn.commit()
    return RedirectResponse("/admin", status_code=303)


@app.get("/scan/{token}", response_class=HTMLResponse)
def scan(token: str, request: Request):
    if setting_enabled("教室設備限制", True):
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
    body = f"<div class='top'><div><h1>教室簽到設備</h1><div class='muted'>只有已配對的教室手機（或教室電腦瀏覽器）才能完成學生 QR 簽到。一般家長／學生自己的手機即使拿到 QR，也不能直接簽到。</div></div>{admin_nav()}</div>"
    body += "<section><h2>建立設備配對</h2><form method='post' action='/admin/devices/new'><label>設備名稱 <input class='wide' name='name' value='教室手機' required></label> <button>產生 30 分鐘設備配對連結＋QR</button></form><p class='mini muted'>這裡就是設備配對連結的管理位置。管理員產生後，教室手機直接掃 QR 即可，不需要另外填 Render 環境變數或手動輸入 code；教室手機開啟連結後還要按「確認配對」，確認後這條連結才失效。</p></section>"
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
    pair = device_pair_info(code)
    body = (
        f"<section style='max-width:560px;margin:40px auto;text-align:center'>"
        f"<h1>教室設備配對</h1>"
        f"<p>即將配對設備：<b>{escape(pair['name'])}</b></p>"
        f"<p>此配對連結有效至：<b>{pair['expires_at']:%Y-%m-%d %H:%M}</b>（台灣時間）</p>"
        f"<div class='alert'>請確認現在使用的就是放在教室、專門用來掃學生 QR 的手機。</div>"
        f"<form method='post' action='/device/pair/confirm'><input type='hidden' name='code' value='{escape(code)}'><button>確認配對這支手機</button></form>"
        f"<p class='mini muted'>這一步是為了避免手機瀏覽器或 QR 預覽工具自動開啟連結時，意外消耗配對碼。</p>"
        f"</section>"
    )
    return page("教室設備配對", body)


@app.post("/device/pair/confirm", response_class=HTMLResponse)
def device_pair_confirm(code: str = Form(...)):
    raw_token, name = confirm_device_pair(code.strip())
    response = RedirectResponse("/device", status_code=303)
    response.set_cookie(DEVICE_COOKIE_NAME, raw_token, httponly=True, secure=True, samesite="lax", max_age=60*60*24*30, path="/")
    return response


@app.get("/device", response_class=HTMLResponse)
def device_home(request: Request):
    device = require_checkin_device(request)
    body = f"<section style='max-width:680px;margin:40px auto;text-align:center'><h1>教室簽到設備</h1><p>設備：<b>{escape(device['name'])}</b></p><div class='alert success'>✅ 本機已授權</div><p>現在可以用這支教室手機的內建相機掃描學生 QR。這就是正式的教室簽到設備，不需要電腦。</p><p class='mini muted'>如果清除瀏覽器 Cookie、換瀏覽器或換手機，需要重新配對；只要這支手機的這個瀏覽器保持登入配對即可。</p><p><a class='btn btn2' href='/admin/devices'>管理員設備頁</a></p></section>"
    return page("教室簽到設備", body)


@app.get("/admin/export.csv")
def export_csv(_: str = Depends(admin_auth)):
    buf = io.StringIO(); buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow(["紀錄ID","日期","學生編號","學生姓名","Actual Course ID","課程","老師","預定開始","預定結束","到班","離班","狀態","狀態代碼","遲到分鐘","校正備註","校正時間","校正者"])
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.id,a.date,s.student_code,s.name,c.actual_course_id,c.course_date,c.course_name,c.teacher_name,c.start_time,c.end_time,a.check_in_time,a.check_out_time,a.status,a.late_minutes,a.manual_note,a.adjusted_at,a.adjusted_by
                FROM attendance a JOIN students s ON s.id=a.student_id LEFT JOIN courses c ON c.id=a.course_id
                ORDER BY a.date DESC,a.id DESC
                """
            )
            for r in cur.fetchall():
                writer.writerow([r[k] for k in ["id","date","student_code","name","actual_course_id","course_name","teacher_name","start_time","end_time","check_in_time","check_out_time"]] + [status_label(r["status"]), r["status"], r["late_minutes"], r["manual_note"], r["adjusted_at"], r["adjusted_by"]])
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


@app.get("/admin/diagnostics", response_class=HTMLResponse)
def admin_diagnostics(_: str = Depends(admin_auth)):
    checks = []
    checks.append(("程式版本", f"V{APP_VERSION}"))
    checks.append(("LINE 模式", escape(LINE_MODE_LABELS.get(LINE_MODE, LINE_MODE))))
    checks.append(("LINE Access Token", "已設定" if LINE_CHANNEL_ACCESS_TOKEN else "缺少"))
    checks.append(("LINE Channel Secret", "已設定" if LINE_CHANNEL_SECRET else "缺少"))
    checks.append(("LINE Admin User ID", "已設定" if LINE_ADMIN_USER_ID else "缺少"))
    db_error = None
    schema = {}
    counts = {}
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                schema = db_schema_summary(cur)
                for table in ["students","courses","attendance","student_line_bindings","notification_templates","runtime_settings","line_message_logs","notification_logs","master_sync_state"]:
                    try:
                        cur.execute(f'SELECT COUNT(*) AS c FROM "{table}"')
                        counts[table] = cur.fetchone()["c"]
                    except Exception as exc:
                        counts[table] = f"錯誤：{str(exc)[:160]}"
    except Exception as exc:
        db_error = str(exc)[:1000]
    body = [f"<div class='top'><div><h1>系統診斷</h1><div class='muted'>V{APP_VERSION}｜此頁不顯示任何 Token／Secret 內容</div></div>{admin_nav()}</div>"]
    body.append("<section><h2>執行環境</h2><table><tr><th>項目</th><th>結果</th></tr>" + ''.join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k,v in checks) + "</table></section>")
    if db_error:
        body.append(f"<section><h2>資料庫</h2><div class='alert danger'>❌ {escape(db_error)}</div></section>")
    else:
        body.append("<section><h2>資料表欄位檢查</h2><div style='overflow:auto'><table><tr><th>資料表</th><th>必要欄位</th></tr>" + ''.join(
            f"<tr><td>{escape(t)}</td><td>" + ' '.join(f"<span class={'green' if ok else 'red'}>{escape(c)}：{'OK' if ok else '缺少'}</span>" for c, ok in cols.items()) + "</td></tr>" for t, cols in schema.items()
        ) + "</table></div></section>")
        body.append("<section><h2>資料量</h2><table><tr><th>資料表</th><th>筆數</th></tr>" + ''.join(f"<tr><td>{escape(t)}</td><td>{escape(str(c))}</td></tr>" for t,c in counts.items()) + "</table></section>")
    body.append(f"<section><h2>主總表自動同步</h2><p>啟用：<b>{'是' if MASTER_SYNC_ENABLED else '否'}</b>｜來源：<b>{escape(MASTER_SYNC_PROVIDER)}</b>｜間隔：<b>{MASTER_SYNC_INTERVAL_MINUTES} 分鐘</b></p></section>")
    body.append(f"<section><h2>版本確認</h2><p>部署這個版本後，<code>/health</code> 應該回傳 <b>{APP_VERSION}</b>；若仍看到舊版本或黑底 Internal Server Error，代表目前 Render 服務沒有實際執行這個 build。</p></section>")
    return page("系統診斷", ''.join(body))


@app.get("/health")
def health():
    db_ok = False
    db_error = None
    try:
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
                db_ok = True
    except Exception as exc:
        db_error = str(exc)[:300]
    payload = {"ok": db_ok, "line_mode": LINE_MODE, "database": "postgres", "version": APP_VERSION, "build": f"attendance-v{APP_VERSION}-auto-master-sync", "config": {"line_channel_access_token": bool(LINE_CHANNEL_ACCESS_TOKEN), "line_channel_secret": bool(LINE_CHANNEL_SECRET), "line_admin_user_id": bool(LINE_ADMIN_USER_ID), "master_sync_enabled": MASTER_SYNC_ENABLED, "master_sync_provider": MASTER_SYNC_PROVIDER, "google_drive_file_id": bool(GOOGLE_DRIVE_FILE_ID), "google_service_account": bool(GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_JSON_BASE64)}}
    if db_error:
        payload["database_error"] = db_error
    return JSONResponse(payload, status_code=200 if db_ok else 503, headers={"Cache-Control": "no-store"})
