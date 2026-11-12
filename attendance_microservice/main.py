import sys
import os
import json
import asyncio
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any

# Ensure attendance_microservice directory is in sys.path before local imports
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from fastapi import FastAPI, HTTPException, Depends, Request, WebSocket, WebSocketDisconnect, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from storage.ledger import init_db
from core.authentication import (
    init_default_faculty,
    verify_password,
    create_access_token,
    get_current_faculty,
    CONFIG
)
from core.session_manager import SessionManager
from core.hotspot_monitor import HotspotMonitor
from core.network_scanner import NetworkScanner

UI_DIR = os.path.join(BASE_DIR, "ui")
EXPORTS_DIR = os.path.join(BASE_DIR, "exports")
os.makedirs(EXPORTS_DIR, exist_ok=True)

session_manager = SessionManager(CONFIG)
hotspot_monitor = HotspotMonitor(CONFIG)
network_scanner = NetworkScanner()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize SQLite database & default faculty credentials
    await init_db()
    await init_default_faculty()
    
    # Start background ticker for session countdown & student state machine
    session_manager.ticker_task = asyncio.create_task(session_manager.run_ticker())
    yield
    if session_manager.ticker_task:
        session_manager.ticker_task.cancel()

app = FastAPI(title="Campus Mind - Wi-Fi Smart Attendance Microservice", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────── Pydantic Models ───────────────────

class LoginRequest(BaseModel):
    faculty_id: str
    password: str

class StartSessionRequest(BaseModel):
    subject: str = Field(..., min_length=1, max_length=100)
    session_duration_minutes: int = Field(default=45, ge=5, le=300)
    disconnect_grace_seconds: int = Field(default=30, ge=10, le=300)
    hotspot_ssid: Optional[str] = None

class JoinSessionRequest(BaseModel):
    student_id: str = Field(..., min_length=1, max_length=50)
    display_name: str = Field(..., min_length=1, max_length=100)

class SettingsUpdateRequest(BaseModel):
    session_duration_minutes: Optional[int] = Field(None, ge=5, le=300)
    disconnect_grace_seconds: Optional[int] = Field(None, ge=5, le=300)
    require_hotspot_on: Optional[bool] = None
    localhost_bypass: Optional[bool] = None
    mock_mode: Optional[bool] = None
    present_threshold_pct: Optional[int] = Field(None, ge=1, le=100)
    partial_threshold_pct: Optional[int] = Field(None, ge=1, le=100)
    default_faculty_name: Optional[str] = None

class EditSessionRequest(BaseModel):
    subject: Optional[str] = Field(None, min_length=1, max_length=100)
    faculty_name: Optional[str] = Field(None, min_length=1, max_length=100)
    session_duration_minutes: Optional[int] = Field(None, ge=1, le=600)

class DeleteAllConfirmRequest(BaseModel):
    confirm: Optional[str] = None

class HeartbeatRequest(BaseModel):
    student_id: str

# ─────────────────── UI Page Endpoints ───────────────────

@app.get("/")
async def root():
    return RedirectResponse(url="/faculty")

@app.get("/login")
@app.get("/login/")
async def login_page():
    return FileResponse(os.path.join(UI_DIR, "login.html"))

@app.get("/faculty")
@app.get("/faculty/create")
@app.get("/faculty/live")
@app.get("/faculty/settings")
async def faculty_page():
    return FileResponse(os.path.join(UI_DIR, "faculty.html"))

@app.get("/history")
async def history_page():
    return FileResponse(os.path.join(UI_DIR, "history.html"))

@app.get("/student")
@app.get("/student/join")
@app.get("/student/dashboard")
async def student_page():
    return FileResponse(os.path.join(UI_DIR, "student.html"))

@app.get("/ui/shared.css")
async def shared_css():
    return FileResponse(os.path.join(UI_DIR, "shared.css"), media_type="text/css")

# ─────────────────── Auth Endpoints ───────────────────

@app.post("/api/auth/login")
async def api_login(req: LoginRequest, response: Response):
    import aiosqlite
    from storage.ledger import get_db
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM faculty_users WHERE faculty_id = ?", (req.faculty_id,))
        user = await cur.fetchone()
        if not user or not verify_password(user["password_hash"], req.password):
            raise HTTPException(status_code=401, detail="Invalid Faculty ID or Password")

        token = create_access_token({"sub": user["faculty_id"], "name": user["name"]})
        response.set_cookie(
            key="faculty_token",
            value=token,
            httponly=True,
            samesite="lax",
            max_age=CONFIG.get("JWT_EXPIRE_MINUTES", 480) * 60
        )
        return {
            "token": token,
            "faculty_id": user["faculty_id"],
            "name": user["name"]
        }

@app.get("/api/auth/me")
async def api_me(current_faculty: dict = Depends(get_current_faculty)):
    return current_faculty

@app.post("/api/auth/logout")
async def api_logout(response: Response):
    response.delete_cookie(key="faculty_token")
    return {"message": "Logged out successfully"}

# ─────────────────── Session Management Endpoints ───────────────────

@app.post("/api/session/start")
async def api_start_session(req: StartSessionRequest, current_faculty: dict = Depends(get_current_faculty)):
    try:
        session = await session_manager.start_session(
            subject=req.subject,
            faculty_id=current_faculty["faculty_id"],
            duration_minutes=req.session_duration_minutes,
            grace_seconds=req.disconnect_grace_seconds,
            hotspot_ssid=req.hotspot_ssid
        )
        return session
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/session/{session_id}/end")
async def api_end_session(session_id: int, current_faculty: dict = Depends(get_current_faculty)):
    try:
        res = await session_manager.end_session(session_id)
        return res
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/api/session/{session_id}/live")
async def api_live_session(session_id: int):
    try:
        return await session_manager.get_live_session_data(session_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/api/sessions/active")
async def api_active_session():
    active = await session_manager.get_active_session()
    if not active:
        return {"active": False}
    try:
        live_info = await session_manager.get_live_session_data(active["session_id"])
        return {"active": True, "session": live_info}
    except Exception:
        return {"active": True, "session": active}

@app.get("/api/sessions")
async def api_list_sessions():
    return await session_manager.get_all_sessions()

@app.get("/api/config/public")
async def api_public_config():
    return {
        "host_system_name": CONFIG.get("HOST_SYSTEM_NAME", "Campus Mind"),
        "require_hotspot_on": CONFIG.get("REQUIRE_HOTSPOT_ON", True),
        "mock_mode": CONFIG.get("MOCK_MODE", False),
        "session_duration_minutes": CONFIG.get("SESSION_DURATION_MINUTES", 45),
        "disconnect_grace_seconds": CONFIG.get("DISCONNECT_GRACE_SECONDS", 30)
    }

# ─────────────────── Settings Endpoints ───────────────────

@app.get("/api/settings")
async def api_get_settings(current_faculty: dict = Depends(get_current_faculty)):
    return {
        "faculty_id": current_faculty.get("faculty_id", ""),
        "faculty_name": current_faculty.get("name", ""),
        "session_duration_minutes": CONFIG.get("SESSION_DURATION_MINUTES", 45),
        "disconnect_grace_seconds": CONFIG.get("DISCONNECT_GRACE_SECONDS", 30),
        "require_hotspot_on": CONFIG.get("REQUIRE_HOTSPOT_ON", True),
        "localhost_bypass": CONFIG.get("LOCALHOST_BYPASS", True),
        "mock_mode": CONFIG.get("MOCK_MODE", False),
        "present_threshold_pct": CONFIG.get("PRESENT_THRESHOLD_PCT", 75),
        "partial_threshold_pct": CONFIG.get("PARTIAL_THRESHOLD_PCT", 25)
    }

@app.post("/api/settings")
async def api_save_settings(req: SettingsUpdateRequest, current_faculty: dict = Depends(get_current_faculty)):
    if req.session_duration_minutes is not None:
        CONFIG["SESSION_DURATION_MINUTES"] = req.session_duration_minutes
    if req.disconnect_grace_seconds is not None:
        CONFIG["DISCONNECT_GRACE_SECONDS"] = req.disconnect_grace_seconds
    if req.require_hotspot_on is not None:
        CONFIG["REQUIRE_HOTSPOT_ON"] = req.require_hotspot_on
    if req.localhost_bypass is not None:
        CONFIG["LOCALHOST_BYPASS"] = req.localhost_bypass
    if req.mock_mode is not None:
        CONFIG["MOCK_MODE"] = req.mock_mode
    if req.present_threshold_pct is not None:
        CONFIG["PRESENT_THRESHOLD_PCT"] = req.present_threshold_pct
    if req.partial_threshold_pct is not None:
        CONFIG["PARTIAL_THRESHOLD_PCT"] = req.partial_threshold_pct
    
    updated_faculty_name = current_faculty.get("name", "")
    if req.default_faculty_name and req.default_faculty_name.strip():
        new_name = req.default_faculty_name.strip()
        import aiosqlite
        from storage.ledger import get_db
        async with get_db() as db:
            await db.execute(
                "UPDATE faculty_users SET name = ? WHERE faculty_id = ?",
                (new_name, current_faculty["faculty_id"])
            )
            await db.commit()
        CONFIG["DEFAULT_FACULTY_NAME"] = new_name
        updated_faculty_name = new_name

    # Persist updated configuration to integration_config.json
    config_file = os.path.join(BASE_DIR, "integration_config.json")
    try:
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, indent=2)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to persist configuration: {str(e)}")

    session_manager.config = CONFIG
    hotspot_monitor.config = CONFIG

    return {
        "status": "success",
        "message": "Settings saved successfully",
        "settings": {
            "faculty_id": current_faculty.get("faculty_id", ""),
            "faculty_name": updated_faculty_name,
            "session_duration_minutes": CONFIG.get("SESSION_DURATION_MINUTES", 45),
            "disconnect_grace_seconds": CONFIG.get("DISCONNECT_GRACE_SECONDS", 30),
            "require_hotspot_on": CONFIG.get("REQUIRE_HOTSPOT_ON", True),
            "localhost_bypass": CONFIG.get("LOCALHOST_BYPASS", True),
            "mock_mode": CONFIG.get("MOCK_MODE", False),
            "present_threshold_pct": CONFIG.get("PRESENT_THRESHOLD_PCT", 75),
            "partial_threshold_pct": CONFIG.get("PARTIAL_THRESHOLD_PCT", 25)
        }
    }

@app.get("/api/sessions/{session_id}")
async def api_get_session(session_id: int):
    try:
        return await session_manager.get_session_by_id(session_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/api/sessions/{session_id}/export")
async def api_export_session(session_id: int, format: str = "json"):
    reports = await session_manager.export_session_reports(session_id)
    fmt = format.lower()
    if fmt == "csv":
        return FileResponse(reports["csv"], media_type="text/csv", filename=f"session_{session_id}_report.csv")
    return FileResponse(reports["json"], media_type="application/json", filename=f"session_{session_id}_report.json")

# ─────────────────── Session Edit & Delete Endpoints ───────────────────

async def get_optional_faculty(request: Request):
    try:
        auth_header = request.headers.get("Authorization")
        token = None
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1]
        elif request.cookies.get("faculty_token"):
            token = request.cookies.get("faculty_token")

        if not token:
            return None
        from core.authentication import decode_access_token
        return decode_access_token(token)
    except Exception:
        return None

@app.put("/api/sessions/{session_id}")
@app.patch("/api/sessions/{session_id}")
@app.post("/api/sessions/{session_id}/edit")
async def api_edit_session(
    session_id: int,
    req: EditSessionRequest,
    current_faculty: Optional[dict] = Depends(get_optional_faculty)
):
    try:
        updated = await session_manager.update_session(
            session_id=session_id,
            subject=req.subject,
            faculty_name=req.faculty_name,
            session_duration_minutes=req.session_duration_minutes
        )
        return {
            "status": "success",
            "message": f"Session #{session_id} updated successfully.",
            "session": updated
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update session: {str(e)}")

@app.delete("/api/sessions/{session_id}")
@app.post("/api/sessions/{session_id}/delete")
async def api_delete_session(
    session_id: int,
    current_faculty: Optional[dict] = Depends(get_optional_faculty)
):
    try:
        await session_manager.delete_session(session_id)
        return {
            "status": "success",
            "message": f"Session #{session_id} and related records deleted successfully."
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete session: {str(e)}")

@app.delete("/api/sessions")
@app.post("/api/sessions/delete-all")
async def api_delete_all_sessions(
    req: Optional[DeleteAllConfirmRequest] = None,
    current_faculty: Optional[dict] = Depends(get_optional_faculty)
):
    try:
        count = await session_manager.delete_all_sessions()
        return {
            "status": "success",
            "message": f"All {count} session(s) and historical records have been deleted.",
            "deleted_count": count
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete history: {str(e)}")

# ─────────────────── Student & Network Endpoints ───────────────────

@app.post("/api/session/{session_id}/join")
async def api_join_session(session_id: int, req: JoinSessionRequest, request: Request):
    client_ip = request.client.host if request.client else "127.0.0.1"
    try:
        return await session_manager.join_student(
            session_id=session_id,
            student_id=req.student_id.strip().upper(),
            display_name=req.display_name.strip(),
            device_ip=client_ip
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/session/{session_id}/heartbeat")
async def api_heartbeat(session_id: int, req: HeartbeatRequest):
    try:
        return await session_manager.record_heartbeat(session_id, req.student_id.strip().upper())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.post("/api/session/{session_id}/student/{student_id}/disconnect")
async def api_student_disconnect(session_id: int, student_id: str, req: dict = None):
    status = (req or {}).get("status", "PAUSED")
    return await session_manager.record_disconnect(session_id, student_id.strip().upper(), status=status)

@app.post("/api/session/{session_id}/student/{student_id}/reconnect")
async def api_student_reconnect(session_id: int, student_id: str, req: dict = None):
    status = (req or {}).get("status", "RECONNECTED")
    return await session_manager.record_reconnect(session_id, student_id.strip().upper(), status=status)

@app.get("/api/session/{session_id}/student/{student_id}/status")
async def api_student_status(session_id: int, student_id: str):
    try:
        return await session_manager.get_student_status(session_id, student_id.strip().upper())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/api/hotspot/status")
async def api_hotspot_status(response: Response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    status_info = hotspot_monitor.detect_status()
    # Add connected ARP devices count if active
    if status_info.get("hotspot_active"):
        devices = network_scanner.scan_arp_table()
        status_info["arp_devices"] = devices
    else:
        status_info["arp_devices"] = []
    return status_info

# ─────────────────── WebSockets ───────────────────

@app.websocket("/ws/faculty")
async def ws_faculty_endpoint(websocket: WebSocket):
    await websocket.accept()
    session_manager.ws_faculty_listeners.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in session_manager.ws_faculty_listeners:
            session_manager.ws_faculty_listeners.remove(websocket)

@app.websocket("/ws/student/{session_id}/{student_id}")
async def ws_student_endpoint(websocket: WebSocket, session_id: int, student_id: str):
    await websocket.accept()
    key = f"{session_id}_{student_id.strip().upper()}"
    session_manager.ws_student_listeners.setdefault(key, []).append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if key in session_manager.ws_student_listeners and websocket in session_manager.ws_student_listeners[key]:
            session_manager.ws_student_listeners[key].remove(websocket)
