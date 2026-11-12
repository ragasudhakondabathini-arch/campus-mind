"""
Campus Mind – Wi-Fi Smart Attendance Management System
FastAPI backend with SQLite and real-time WebSocket updates
"""
import os
import asyncio
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ─────────────────────────── Database ───────────────────────────

DB_PATH = "attendance.db"


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                subject         TEXT NOT NULL,
                faculty_name    TEXT NOT NULL DEFAULT 'Faculty Member',
                duration_min    INTEGER NOT NULL DEFAULT 60,
                grace_sec       INTEGER NOT NULL DEFAULT 90,
                started_at      REAL NOT NULL,
                ended_at        REAL,
                is_active       INTEGER NOT NULL DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS attendance (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id      INTEGER NOT NULL,
                student_id      TEXT NOT NULL,
                student_name    TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'Joined',
                attended_sec    INTEGER NOT NULL DEFAULT 0,
                last_seen       REAL NOT NULL,
                joined_at       REAL NOT NULL,
                UNIQUE(session_id, student_id)
            )
        """)
        await db.commit()


# ─────────────────────────── WebSocket Manager ───────────────────────────

class ConnectionManager:
    def __init__(self):
        self.faculty_clients: list[WebSocket] = []
        self.student_clients: dict[str, list[WebSocket]] = {}  # session_id+student_id → [ws]

    async def connect_faculty(self, ws: WebSocket):
        await ws.accept()
        self.faculty_clients.append(ws)

    def disconnect_faculty(self, ws: WebSocket):
        if ws in self.faculty_clients:
            self.faculty_clients.remove(ws)

    async def connect_student(self, ws: WebSocket, key: str):
        await ws.accept()
        self.student_clients.setdefault(key, []).append(ws)

    def disconnect_student(self, ws: WebSocket, key: str):
        if key in self.student_clients:
            self.student_clients[key] = [c for c in self.student_clients[key] if c != ws]

    async def broadcast_faculty(self, data: dict):
        dead = []
        for ws in self.faculty_clients:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect_faculty(ws)

    async def send_student(self, key: str, data: dict):
        dead = []
        for ws in self.student_clients.get(key, []):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect_student(ws, key)


manager = ConnectionManager()


# ─────────────────────────── Background ticker ───────────────────────────

async def attendance_ticker():
    """Every second: update attended_sec for connected students,
       mark students as Disconnected if last_seen too old,
       end sessions past their duration, broadcast updates."""
    while True:
        await asyncio.sleep(1)
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                now = time.time()

                # Fetch all active sessions
                cursor = await db.execute(
                    "SELECT * FROM sessions WHERE is_active=1"
                )
                sessions = await cursor.fetchall()

                for sess in sessions:
                    sid = sess["id"]
                    duration_sec = sess["duration_min"] * 60
                    grace_sec = sess["grace_sec"]
                    elapsed = now - sess["started_at"]

                    # End session if duration exceeded
                    if elapsed >= duration_sec:
                        await db.execute(
                            "UPDATE sessions SET is_active=0, ended_at=? WHERE id=?",
                            (now, sid)
                        )
                        await db.commit()
                        await manager.broadcast_faculty({"type": "session_ended", "session_id": sid})
                        continue

                    # Fetch attendance rows
                    cur2 = await db.execute(
                        "SELECT * FROM attendance WHERE session_id=?", (sid,)
                    )
                    rows = await cur2.fetchall()

                    updates = []
                    for row in rows:
                        new_status = row["status"]
                        new_attended = row["attended_sec"]
                        time_since = now - row["last_seen"]

                        if row["status"] == "Joined":
                            new_attended += 1
                            if time_since > grace_sec:
                                new_status = "Disconnected"
                        elif row["status"] == "Paused":
                            if time_since <= grace_sec:
                                new_status = "Joined"
                                new_attended += 1
                            elif time_since > grace_sec * 2:
                                new_status = "Disconnected"
                        elif row["status"] == "Disconnected":
                            if time_since <= grace_sec:
                                new_status = "Joined"
                                new_attended += 1

                        updates.append((new_status, new_attended, row["id"]))

                    if updates:
                        await db.executemany(
                            "UPDATE attendance SET status=?, attended_sec=? WHERE id=?",
                            updates
                        )
                        await db.commit()

                    # Broadcast live stats to faculty
                    cur3 = await db.execute(
                        "SELECT * FROM attendance WHERE session_id=?", (sid,)
                    )
                    all_rows = await cur3.fetchall()
                    remaining = max(0, duration_sec - int(elapsed))

                    students_data = [
                        {
                            "student_id": r["student_id"],
                            "student_name": r["student_name"],
                            "status": r["status"],
                            "attended_sec": r["attended_sec"],
                            "last_seen": r["last_seen"],
                        }
                        for r in all_rows
                    ]

                    total = len(students_data)
                    present = sum(1 for s in students_data if s["status"] == "Joined")
                    paused = sum(1 for s in students_data if s["status"] == "Paused")
                    dropped = sum(1 for s in students_data if s["status"] == "Disconnected")

                    await manager.broadcast_faculty({
                        "type": "tick",
                        "session_id": sid,
                        "remaining_sec": remaining,
                        "duration_sec": duration_sec,
                        "grace_sec": grace_sec,
                        "total": total,
                        "present": present,
                        "paused": paused,
                        "dropped": dropped,
                        "students": students_data,
                    })

                    # Broadcast to each student
                    for s in students_data:
                        key = f"{sid}_{s['student_id']}"
                        pct = int((s["attended_sec"] / duration_sec) * 100) if duration_sec > 0 else 0
                        await manager.send_student(key, {
                            "type": "tick",
                            "session_id": sid,
                            "remaining_sec": remaining,
                            "duration_sec": duration_sec,
                            "grace_sec": grace_sec,
                            "attended_sec": s["attended_sec"],
                            "status": s["status"],
                            "attendance_pct": min(100, pct),
                        })

        except Exception as e:
            print(f"[ticker] error: {e}")


# ─────────────────────────── App Lifespan ───────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    asyncio.create_task(attendance_ticker())
    yield


app = FastAPI(title="Campus Mind", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")


# ─────────────────────────── Pydantic Models ───────────────────────────

class CreateSessionRequest(BaseModel):
    subject: str
    faculty_name: str = "Faculty Member"
    duration_min: int = 60
    grace_sec: int = 90


class JoinSessionRequest(BaseModel):
    student_id: str
    student_name: str


class HeartbeatRequest(BaseModel):
    student_id: str
    session_id: int


# ─────────────────────────── REST Endpoints ───────────────────────────

@app.get("/")
async def root():
    return FileResponse("templates/index.html")

MICRO_UI_DIR = os.path.join(os.path.dirname(__file__), "attendance_microservice", "ui")

@app.get("/login")
@app.get("/login/")
async def login_page_route():
    login_path = os.path.join(MICRO_UI_DIR, "login.html")
    if os.path.exists(login_path):
        return FileResponse(login_path)
    return FileResponse("templates/index.html")

@app.get("/faculty")
async def faculty_root_route():
    fac_path = os.path.join(MICRO_UI_DIR, "faculty.html")
    if os.path.exists(fac_path):
        return FileResponse(fac_path)
    return FileResponse("templates/faculty_create.html")

@app.get("/history")
async def history_root_route():
    hist_path = os.path.join(MICRO_UI_DIR, "history.html")
    if os.path.exists(hist_path):
        return FileResponse(hist_path)
    return FileResponse("templates/index.html")

@app.get("/student")
async def student_root_route():
    st_path = os.path.join(MICRO_UI_DIR, "student.html")
    if os.path.exists(st_path):
        return FileResponse(st_path)
    return FileResponse("templates/student_join.html")

@app.get("/ui/shared.css")
async def shared_css_route():
    css_path = os.path.join(MICRO_UI_DIR, "shared.css")
    if os.path.exists(css_path):
        return FileResponse(css_path, media_type="text/css")
    return HTMLResponse("", status_code=404)

@app.get("/faculty/create-session")
async def faculty_create():
    return FileResponse("templates/faculty_create.html")


@app.get("/faculty/live")
async def faculty_live():
    return FileResponse("templates/faculty_live.html")


@app.get("/student/join")
async def student_join():
    return FileResponse("templates/student_join.html")


@app.get("/student/dashboard")
async def student_dashboard():
    return FileResponse("templates/student_dashboard.html")


# ── Sessions ──

@app.post("/api/sessions")
async def create_session(req: CreateSessionRequest):
    # End any existing active session first
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET is_active=0, ended_at=? WHERE is_active=1",
            (time.time(),)
        )
        cursor = await db.execute(
            """INSERT INTO sessions (subject, faculty_name, duration_min, grace_sec, started_at)
               VALUES (?, ?, ?, ?, ?)""",
            (req.subject, req.faculty_name, req.duration_min, req.grace_sec, time.time())
        )
        session_id = cursor.lastrowid
        await db.commit()
    return {"session_id": session_id, "message": "Session started"}


@app.get("/api/sessions/active")
async def get_active_session():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM sessions WHERE is_active=1 ORDER BY started_at DESC LIMIT 1"
        )
        row = await cur.fetchone()
        if not row:
            return {"active": False}
        now = time.time()
        elapsed = now - row["started_at"]
        remaining = max(0, row["duration_min"] * 60 - int(elapsed))
        return {
            "active": True,
            "session_id": row["id"],
            "subject": row["subject"],
            "faculty_name": row["faculty_name"],
            "duration_min": row["duration_min"],
            "grace_sec": row["grace_sec"],
            "started_at": row["started_at"],
            "remaining_sec": remaining,
        }


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM sessions WHERE id=?", (session_id,))
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "Session not found")
        now = time.time()
        elapsed = now - row["started_at"]
        remaining = max(0, row["duration_min"] * 60 - int(elapsed))
        return dict(row) | {"remaining_sec": remaining}


@app.post("/api/sessions/{session_id}/end")
async def end_session(session_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET is_active=0, ended_at=? WHERE id=?",
            (time.time(), session_id)
        )
        await db.commit()
    await manager.broadcast_faculty({"type": "session_ended", "session_id": session_id})
    return {"message": "Session ended"}


@app.get("/api/sessions/{session_id}/attendance")
async def get_attendance(session_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM attendance WHERE session_id=? ORDER BY joined_at",
            (session_id,)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


@app.get("/api/sessions/history/all")
async def get_history():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT 50"
        )
        rows = await cur.fetchall()
        result = []
        for r in rows:
            cur2 = await db.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN status='Joined' THEN 1 ELSE 0 END) as present "
                "FROM attendance WHERE session_id=?",
                (r["id"],)
            )
            stats = await cur2.fetchone()
            result.append(dict(r) | {"total": stats["total"], "present": stats["present"] or 0})
        return result


# ── Attendance ──

@app.post("/api/sessions/{session_id}/join")
async def join_session(session_id: int, req: JoinSessionRequest):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM sessions WHERE id=? AND is_active=1", (session_id,)
        )
        sess = await cur.fetchone()
        if not sess:
            raise HTTPException(400, "No active session found")

        now = time.time()
        await db.execute(
            """INSERT INTO attendance (session_id, student_id, student_name, status, attended_sec, last_seen, joined_at)
               VALUES (?, ?, ?, 'Joined', 0, ?, ?)
               ON CONFLICT(session_id, student_id) DO UPDATE SET
                   status='Joined', last_seen=excluded.last_seen""",
            (session_id, req.student_id, req.student_name, now, now)
        )
        await db.commit()
        elapsed = now - sess["started_at"]
        remaining = max(0, sess["duration_min"] * 60 - int(elapsed))
        return {
            "joined": True,
            "session_id": session_id,
            "subject": sess["subject"],
            "faculty_name": sess["faculty_name"],
            "duration_min": sess["duration_min"],
            "grace_sec": sess["grace_sec"],
            "remaining_sec": remaining,
        }


@app.post("/api/heartbeat")
async def heartbeat(req: HeartbeatRequest):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM attendance WHERE session_id=? AND student_id=?",
            (req.session_id, req.student_id)
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "Attendance record not found")
        now = time.time()
        new_status = "Joined" if row["status"] in ("Paused", "Disconnected") else row["status"]
        await db.execute(
            "UPDATE attendance SET last_seen=?, status=? WHERE session_id=? AND student_id=?",
            (now, new_status, req.session_id, req.student_id)
        )
        await db.commit()
        return {"ok": True, "status": new_status}


@app.get("/api/student/{student_id}/history")
async def student_history(student_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT a.*, s.subject, s.faculty_name, s.duration_min, s.started_at as sess_started
               FROM attendance a
               JOIN sessions s ON a.session_id = s.id
               WHERE a.student_id=?
               ORDER BY a.joined_at DESC""",
            (student_id,)
        )
        rows = await cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["attendance_pct"] = min(100, int(d["attended_sec"] / (d["duration_min"] * 60) * 100)) if d["duration_min"] > 0 else 0
            result.append(d)
        return result


# ─────────────────────────── WebSocket Endpoints ───────────────────────────

@app.websocket("/ws/faculty")
async def ws_faculty(websocket: WebSocket):
    await manager.connect_faculty(websocket)
    try:
        while True:
            await websocket.receive_text()  # keep alive
    except WebSocketDisconnect:
        manager.disconnect_faculty(websocket)


@app.websocket("/ws/student/{session_id}/{student_id}")
async def ws_student(websocket: WebSocket, session_id: int, student_id: str):
    key = f"{session_id}_{student_id}"
    await manager.connect_student(websocket, key)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_student(websocket, key)

