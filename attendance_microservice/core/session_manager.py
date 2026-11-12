import os
import csv
import json
import time
import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import urllib.request
import aiosqlite

from storage.ledger import get_db
from core.pause_resume_engine import PauseResumeEngine
from core.mock_provider import MockProvider
from core.hotspot_monitor import HotspotMonitor

logger = logging.getLogger("SessionManager")

class SessionManager:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.engine = PauseResumeEngine(
            present_pct=config.get("PRESENT_THRESHOLD_PCT", 75),
            partial_pct=config.get("PARTIAL_THRESHOLD_PCT", 25)
        )
        self.hotspot_monitor = HotspotMonitor(config)
        self.mock_provider: Optional[MockProvider] = None
        self.active_session_id: Optional[int] = None
        self.ticker_task: Optional[asyncio.Task] = None
        self.ws_faculty_listeners: List[Any] = []
        self.ws_student_listeners: Dict[str, List[Any]] = {}
        self.last_tick_time: float = time.time()

    def refresh_config(self) -> Dict[str, Any]:
        try:
            config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "integration_config.json")
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    updated = json.load(f)
                    self.config.update(updated)
                    self.hotspot_monitor.config = self.config
        except Exception as e:
            logger.warning(f"Failed to reload config: {e}")
        return self.config

    async def get_active_session(self) -> Optional[Dict[str, Any]]:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
                SELECT s.*, COALESCE(s.faculty_name, f.name, s.faculty_id) as faculty_name 
                FROM sessions s 
                LEFT JOIN faculty_users f ON s.faculty_id = f.faculty_id 
                WHERE s.is_active = 1 
                ORDER BY s.session_id DESC LIMIT 1
            """)
            row = await cur.fetchone()
            if not row:
                self.active_session_id = None
                return None
            sess = dict(row)
            self.active_session_id = sess["session_id"]
            now = time.time()
            elapsed = now - sess["start_time"]
            total_duration_sec = sess["session_duration_minutes"] * 60
            sess["remaining_seconds"] = max(0, int(total_duration_sec - elapsed))
            sess["elapsed_seconds"] = int(elapsed)
            if not sess.get("faculty_name"):
                sess["faculty_name"] = sess["faculty_id"]
            return sess

    async def start_session(
        self,
        subject: str,
        faculty_id: str,
        duration_minutes: int,
        grace_seconds: int,
        hotspot_ssid: Optional[str] = None
    ) -> Dict[str, Any]:
        if self.config.get("REQUIRE_HOTSPOT_ON", True) and not self.config.get("MOCK_MODE", False):
            hotspot_diag = self.hotspot_monitor.detect_status()
            if not hotspot_diag.get("hotspot_active", False):
                raise ValueError("Cannot start session: Windows Mobile Hotspot is OFF. Please turn ON your laptop Mobile Hotspot in Windows Settings before starting.")

        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT session_id FROM sessions WHERE is_active = 1")
            active_rows = await cur.fetchall()
            for r in active_rows:
                await self.end_session(r["session_id"])

            now = time.time()
            created_at = datetime.now(timezone.utc).isoformat()

            cur_f = await db.execute("SELECT name FROM faculty_users WHERE faculty_id = ?", (faculty_id,))
            f_row = await cur_f.fetchone()
            faculty_name = f_row["name"] if f_row and f_row["name"] else faculty_id

            try:
                await db.execute("ALTER TABLE sessions ADD COLUMN faculty_name TEXT")
            except Exception:
                pass
            
            cur = await db.execute("""
                INSERT INTO sessions (
                    faculty_id, faculty_name, subject, start_time, planned_duration,
                    session_duration_minutes, disconnect_grace_seconds,
                    hotspot_ssid, created_at, is_active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """, (faculty_id, faculty_name, subject, now, duration_minutes, duration_minutes, grace_seconds, hotspot_ssid or "Classroom-WiFi", created_at))
            session_id = cur.lastrowid
            await db.commit()

        self.active_session_id = session_id
        self.last_tick_time = time.time()

        self.refresh_config()
        if self.config.get("MOCK_MODE", False):
            self.mock_provider = MockProvider(
                count_min=self.config.get("MOCK_STUDENT_MIN", 15),
                count_max=self.config.get("MOCK_STUDENT_MAX", 25)
            )
        else:
            self.mock_provider = None

        return await self.get_session_by_id(session_id)

    async def end_session(self, session_id: int) -> Dict[str, Any]:
        now = time.time()
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
            sess = await cur.fetchone()
            if not sess:
                raise ValueError("Session not found")

            actual_duration = round(now - sess["start_time"], 1)

            cur_att = await db.execute("SELECT * FROM session_attendees WHERE session_id = ?", (session_id,))
            attendees = await cur_att.fetchall()

            if self.mock_provider:
                mock_times = self.mock_provider.get_final_attendance(sess["session_duration_minutes"])
                for att in attendees:
                    st_id = att["student_id"]
                    if st_id in mock_times:
                        total_s = mock_times[st_id]
                        total_m = round(total_s / 60.0, 2)
                        await db.execute(
                            "UPDATE session_attendees SET total_seconds = ?, total_minutes = ? WHERE row_id = ?",
                            (total_s, total_m, att["row_id"])
                        )
                await db.commit()
                # Re-fetch attendees with finalized attended times
                cur_att = await db.execute("SELECT * FROM session_attendees WHERE session_id = ?", (session_id,))
                attendees = await cur_att.fetchall()

            for att in attendees:
                final_status, pct = self.engine.calculate_final_status(
                    att["total_seconds"], sess["session_duration_minutes"]
                )
                await db.execute(
                    "UPDATE session_attendees SET leave_time = ?, final_status = ?, current_status = ? WHERE row_id = ?",
                    (now, final_status, final_status, att["row_id"])
                )
                await db.execute(
                    "INSERT INTO connection_events (session_id, student_id, event_type, timestamp, details) VALUES (?, ?, ?, ?, ?)",
                    (session_id, att["student_id"], "FINALIZED", now, f"Final Status: {final_status} ({pct}%)")
                )

            await db.execute(
                "UPDATE sessions SET is_active = 0, end_time = ?, actual_duration = ? WHERE session_id = ?",
                (now, actual_duration, session_id)
            )
            await db.commit()

        if self.active_session_id == session_id:
            self.active_session_id = None
            self.mock_provider = None

        export_res = await self.export_session_reports(session_id)

        webhook_url = self.config.get("TARGET_WEBHOOK_URL")
        if webhook_url:
            asyncio.create_task(self.send_webhook_notification(webhook_url, session_id))

        await self.broadcast_faculty({"type": "SESSION_ENDED", "session_id": session_id})
        return {"message": "Session ended successfully", "session_id": session_id, "exports": export_res}

    async def join_student(self, session_id: int, student_id: str, display_name: str, device_ip: str = "127.0.0.1", device_mac: str = None) -> Dict[str, Any]:
        # Remote validation if VERIFY_USER_ENDPOINT is configured
        verify_endpoint = self.config.get("VERIFY_USER_ENDPOINT")
        if verify_endpoint and str(verify_endpoint).strip():
            try:
                verify_payload = json.dumps({
                    "student_id": student_id,
                    "session_id": session_id,
                    "display_name": display_name
                }).encode("utf-8")
                req = urllib.request.Request(
                    str(verify_endpoint).strip(),
                    data=verify_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    if resp.status not in (200, 201, 204):
                        raise ValueError(f"Student verification rejected by host system (status {resp.status})")
            except Exception as e:
                if isinstance(e, ValueError):
                    raise e
                logger.warning(f"Verify user endpoint check failed: {e}")
                raise ValueError(f"Student verification failed with host system: {e}")

        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM sessions WHERE session_id = ? AND is_active = 1", (session_id,))
            sess = await cur.fetchone()
            if not sess:
                raise ValueError("No active session with this ID")

            now = time.time()
            cur_att = await db.execute("SELECT * FROM session_attendees WHERE session_id = ? AND student_id = ?", (session_id, student_id))
            existing = await cur_att.fetchone()

            if existing:
                await db.execute("""
                    UPDATE session_attendees 
                    SET last_seen = ?, current_status = 'JOINED', device_ip = COALESCE(?, device_ip)
                    WHERE session_id = ? AND student_id = ?
                """, (now, device_ip, session_id, student_id))
                await db.execute(
                    "INSERT INTO connection_events (session_id, student_id, event_type, timestamp, details) VALUES (?, ?, 'RECONNECTED', ?, ?)",
                    (session_id, student_id, now, f"Re-joined from IP {device_ip}")
                )
            else:
                await db.execute("""
                    INSERT INTO session_attendees (
                        session_id, student_id, display_name, device_mac, device_ip,
                        join_time, total_minutes, total_seconds, current_status, final_status, last_seen
                    ) VALUES (?, ?, ?, ?, ?, ?, 0.0, 0, 'JOINED', 'PENDING', ?)
                """, (session_id, student_id, display_name, device_mac or "N/A", device_ip, now, now))
                
                await db.execute("UPDATE sessions SET total_joined = total_joined + 1 WHERE session_id = ?", (session_id,))
                
                await db.execute(
                    "INSERT INTO connection_events (session_id, student_id, event_type, timestamp, details) VALUES (?, ?, 'JOINED', ?, ?)",
                    (session_id, student_id, now, f"First join from IP {device_ip}")
                )

            await db.commit()

        return await self.get_student_status(session_id, student_id)

    async def record_heartbeat(self, session_id: int, student_id: str) -> Dict[str, Any]:
        now = time.time()
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM session_attendees WHERE session_id = ? AND student_id = ?",
                (session_id, student_id)
            )
            att = await cur.fetchone()
            if not att:
                raise ValueError("Attendee record not found")

            new_status = att["current_status"]
            if new_status in ("PAUSED", "DISCONNECTED", "DROPPED"):
                new_status = "RECONNECTED"
                await db.execute(
                    "INSERT INTO connection_events (session_id, student_id, event_type, timestamp, details) VALUES (?, ?, 'RECONNECTED', ?, ?)",
                    (session_id, student_id, now, "Heartbeat received, student reconnected")
                )

            await db.execute(
                "UPDATE session_attendees SET last_seen = ?, current_status = ? WHERE session_id = ? AND student_id = ?",
                (now, new_status, session_id, student_id)
            )
            await db.commit()

        return {"ok": True, "status": new_status, "timestamp": now}

    async def record_disconnect(self, session_id: int, student_id: str, status: str = "PAUSED") -> Dict[str, Any]:
        now = time.time()
        target_status = status if status in ("PAUSED", "DROPPED", "DISCONNECTED") else "PAUSED"
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM session_attendees WHERE session_id = ? AND student_id = ?",
                (session_id, student_id)
            )
            att = await cur.fetchone()
            if not att:
                return {"ok": False}

            await db.execute(
                "UPDATE session_attendees SET current_status = ? WHERE session_id = ? AND student_id = ?",
                (target_status, session_id, student_id)
            )
            await db.execute(
                "INSERT INTO connection_events (session_id, student_id, event_type, timestamp, details) VALUES (?, ?, ?, ?, ?)",
                (session_id, student_id, target_status, now, f"Student connection transitioned to {target_status}")
            )
            await db.commit()

        return {"ok": True, "status": target_status, "timestamp": now}

    async def record_reconnect(self, session_id: int, student_id: str, status: str = "RECONNECTED") -> Dict[str, Any]:
        now = time.time()
        target_status = status if status in ("RECONNECTED", "JOINED") else "RECONNECTED"
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM session_attendees WHERE session_id = ? AND student_id = ?",
                (session_id, student_id)
            )
            att = await cur.fetchone()
            if not att:
                return {"ok": False}

            await db.execute(
                "UPDATE session_attendees SET last_seen = ?, current_status = ? WHERE session_id = ? AND student_id = ?",
                (now, target_status, session_id, student_id)
            )
            await db.execute(
                "INSERT INTO connection_events (session_id, student_id, event_type, timestamp, details) VALUES (?, ?, 'RESUMED', ?, ?)",
                (session_id, student_id, now, f"Student reconnected ({target_status})")
            )
            await db.commit()

        return {"ok": True, "status": target_status, "timestamp": now}

    async def get_student_status(self, session_id: int, student_id: str) -> Dict[str, Any]:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
            sess = await cur.fetchone()
            if not sess:
                raise ValueError("Session not found")

            cur_att = await db.execute(
                "SELECT * FROM session_attendees WHERE session_id = ? AND student_id = ?",
                (session_id, student_id)
            )
            att = await cur_att.fetchone()
            if not att:
                raise ValueError("Student not found in this session")

            now = time.time()
            elapsed = now - sess["start_time"]
            total_duration_sec = sess["session_duration_minutes"] * 60
            remaining_sec = max(0, int(total_duration_sec - elapsed))
            
            pct = min(100.0, round((att["total_seconds"] / max(total_duration_sec, 1)) * 100.0, 1))

            cur_fac = await db.execute("SELECT name FROM faculty_users WHERE faculty_id = ?", (sess["faculty_id"],))
            fac_row = await cur_fac.fetchone()
            faculty_name = fac_row["name"] if fac_row and fac_row["name"] else sess["faculty_id"]

            return {
                "session_id": session_id,
                "subject": sess["subject"],
                "faculty_id": sess["faculty_id"],
                "faculty_name": faculty_name,
                "student_id": student_id,
                "display_name": att["display_name"],
                "current_status": att["current_status"],
                "final_status": att["final_status"],
                "attended_seconds": att["total_seconds"],
                "attended_minutes": att["total_minutes"],
                "attendance_percentage": pct,
                "session_duration_minutes": sess["session_duration_minutes"],
                "remaining_seconds": remaining_sec,
                "disconnect_grace_seconds": sess["disconnect_grace_seconds"],
                "is_session_active": bool(sess["is_active"])
            }

    async def get_live_session_data(self, session_id: int) -> Dict[str, Any]:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
            sess = await cur.fetchone()
            if not sess:
                raise ValueError("Session not found")

            cur_att = await db.execute("SELECT * FROM session_attendees WHERE session_id = ? ORDER BY row_id ASC", (session_id,))
            attendees = [dict(r) for r in await cur_att.fetchall()]

            cur_fac = await db.execute("SELECT name FROM faculty_users WHERE faculty_id = ?", (sess["faculty_id"],))
            fac_row = await cur_fac.fetchone()
            faculty_name = fac_row["name"] if fac_row and fac_row["name"] else sess["faculty_id"]

            now = time.time()
            elapsed = max(int(now - sess["start_time"]), 1)
            duration_sec = max(sess["session_duration_minutes"] * 60, 60)
            remaining = max(0, int(duration_sec - elapsed))

            # Total unique joined students
            total_joined = len(attendees)
            present_count = 0
            partial_count = 0
            dropped_count = 0

            # Calculate provisional attendance during active session
            for a in attendees:
                attended_sec = a["total_seconds"]
                curr_status = a.get("current_status", "JOINED")
                join_time = a.get("join_time", sess["start_time"])
                time_since_join = max(int(now - join_time), 1)

                max_possible_sec = attended_sec + remaining
                max_possible_pct = (max_possible_sec / duration_sec) * 100.0
                active_ratio_pct = (attended_sec / time_since_join) * 100.0

                if curr_status == "DROPPED" or max_possible_pct < self.config.get("PARTIAL_THRESHOLD_PCT", 25):
                    prov = "DROPPED"
                    dropped_count += 1
                elif max_possible_pct < self.config.get("PRESENT_THRESHOLD_PCT", 75) or active_ratio_pct < self.config.get("PARTIAL_THRESHOLD_PCT", 25):
                    prov = "PARTIAL"
                    partial_count += 1
                elif curr_status == "PAUSED" and active_ratio_pct < self.config.get("PRESENT_THRESHOLD_PCT", 75):
                    prov = "PARTIAL"
                    partial_count += 1
                else:
                    prov = "PRESENT"
                    present_count += 1

                a["provisional_status"] = prov

            return {
                "session_id": session_id,
                "subject": sess["subject"],
                "faculty_id": sess["faculty_id"],
                "faculty_name": faculty_name,
                "start_time": sess["start_time"],
                "is_active": bool(sess["is_active"]),
                "session_duration_minutes": sess["session_duration_minutes"],
                "disconnect_grace_seconds": sess["disconnect_grace_seconds"],
                "remaining_seconds": remaining,
                "elapsed_seconds": int(elapsed),
                "total_joined": total_joined,
                "present_count": present_count,
                "partial_count": partial_count,
                "dropped_count": dropped_count,
                "students": attendees,
                "is_mock": bool(self.mock_provider is not None)
            }

    async def get_all_sessions(self) -> List[Dict[str, Any]]:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
                SELECT s.*, COALESCE(s.faculty_name, f.name, s.faculty_id) as faculty_name 
                FROM sessions s 
                LEFT JOIN faculty_users f ON s.faculty_id = f.faculty_id 
                ORDER BY s.session_id DESC
            """)
            sessions = [dict(r) for r in await cur.fetchall()]

            for s in sessions:
                sid = s["session_id"]
                actual_sec = s.get("actual_duration") or 0
                planned_sec = max(s["planned_duration"] * 60, 1)
                basis_sec = min(planned_sec, max(int(actual_sec), 1)) if actual_sec > 0 else planned_sec
                cur_att = await db.execute("SELECT * FROM session_attendees WHERE session_id = ?", (sid,))
                attendees = [dict(a) for a in await cur_att.fetchall()]
                
                total = len(attendees)
                present = 0
                partial = 0
                dropped = 0

                for a in attendees:
                    st = a.get("final_status")
                    if st == "PRESENT":
                        present += 1
                    elif st == "PARTIAL":
                        partial += 1
                    elif st == "DROPPED":
                        dropped += 1
                    else:
                        pct = (a["total_seconds"] / basis_sec) * 100.0
                        if pct >= self.config.get("PRESENT_THRESHOLD_PCT", 75):
                            present += 1
                        elif pct >= self.config.get("PARTIAL_THRESHOLD_PCT", 25):
                            partial += 1
                        else:
                            dropped += 1

                s["total_students"] = total
                s["present_count"] = present
                s["partial_count"] = partial
                s["dropped_count"] = dropped

            return sessions

    async def get_session_by_id(self, session_id: int) -> Dict[str, Any]:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("""
                SELECT s.*, COALESCE(s.faculty_name, f.name, s.faculty_id) as faculty_name 
                FROM sessions s 
                LEFT JOIN faculty_users f ON s.faculty_id = f.faculty_id 
                WHERE s.session_id = ?
            """, (session_id,))
            sess = await cur.fetchone()
            if not sess:
                raise ValueError("Session not found")
            return dict(sess)

    async def update_session(
        self,
        session_id: int,
        subject: Optional[str] = None,
        faculty_name: Optional[str] = None,
        session_duration_minutes: Optional[int] = None
    ) -> Dict[str, Any]:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
            sess = await cur.fetchone()
            if not sess:
                raise ValueError(f"Session #{session_id} not found")

            updates = []
            params = []
            if subject is not None and subject.strip():
                updates.append("subject = ?")
                params.append(subject.strip())
            if session_duration_minutes is not None and session_duration_minutes > 0:
                updates.append("session_duration_minutes = ?")
                params.append(int(session_duration_minutes))
                updates.append("planned_duration = ?")
                params.append(int(session_duration_minutes))
            if faculty_name is not None and faculty_name.strip():
                try:
                    await db.execute("ALTER TABLE sessions ADD COLUMN faculty_name TEXT")
                except Exception:
                    pass
                updates.append("faculty_name = ?")
                params.append(faculty_name.strip())
                if sess["faculty_id"]:
                    await db.execute(
                        "UPDATE faculty_users SET name = ? WHERE faculty_id = ?",
                        (faculty_name.strip(), sess["faculty_id"])
                    )

            if updates:
                params.append(session_id)
                await db.execute(f"UPDATE sessions SET {', '.join(updates)} WHERE session_id = ?", params)
                await db.commit()

        return await self.get_session_by_id(session_id)

    async def delete_session(self, session_id: int) -> bool:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT session_id FROM sessions WHERE session_id = ?", (session_id,))
            if not await cur.fetchone():
                raise ValueError(f"Session #{session_id} not found")

            if self.active_session_id == session_id:
                self.active_session_id = None
                self.mock_provider = None

            await db.execute("DELETE FROM connection_events WHERE session_id = ?", (session_id,))
            await db.execute("DELETE FROM session_attendees WHERE session_id = ?", (session_id,))
            await db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            await db.commit()

        export_folder = self.config.get("EXPORT_FOLDER", "./exports")
        for ext in ["csv", "json"]:
            path = os.path.join(export_folder, f"session_{session_id}_report.{ext}")
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass

        return True

    async def delete_all_sessions(self) -> int:
        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT count(*) as cnt FROM sessions")
            row = await cur.fetchone()
            count = row["cnt"] if row else 0

            self.active_session_id = None
            self.mock_provider = None

            await db.execute("DELETE FROM connection_events")
            await db.execute("DELETE FROM session_attendees")
            await db.execute("DELETE FROM sessions")
            try:
                await db.execute("DELETE FROM sqlite_sequence WHERE name IN ('sessions', 'session_attendees', 'connection_events')")
            except Exception:
                pass
            await db.commit()

        export_folder = self.config.get("EXPORT_FOLDER", "./exports")
        if os.path.exists(export_folder):
            for fname in os.listdir(export_folder):
                if fname.startswith("session_") and (fname.endswith(".csv") or fname.endswith(".json")):
                    try:
                        os.remove(os.path.join(export_folder, fname))
                    except Exception:
                        pass

        return count

    async def export_session_reports(self, session_id: int) -> Dict[str, str]:
        export_folder = self.config.get("EXPORT_FOLDER", "./exports")
        os.makedirs(export_folder, exist_ok=True)

        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
            sess = dict(await cur.fetchone())

            cur_att = await db.execute("SELECT * FROM session_attendees WHERE session_id = ? ORDER BY row_id ASC", (session_id,))
            attendees = [dict(r) for r in await cur_att.fetchall()]

            cur_ev = await db.execute("SELECT * FROM connection_events WHERE session_id = ? ORDER BY timestamp ASC", (session_id,))
            events = [dict(r) for r in await cur_ev.fetchall()]

        # Map per-student event history (Blueprint Section 12 requirement)
        events_by_student = {}
        for ev in events:
            sid = ev["student_id"]
            ev_time = datetime.fromtimestamp(ev["timestamp"], timezone.utc).isoformat() if isinstance(ev["timestamp"], (int, float)) else str(ev["timestamp"])
            events_by_student.setdefault(sid, []).append({
                "type": ev["event_type"].lower(),
                "time": ev_time,
                "details": ev.get("details")
            })

        attendees_formatted = []
        for a in attendees:
            join_iso = datetime.fromtimestamp(a["join_time"], timezone.utc).isoformat() if a.get("join_time") else None
            leave_iso = datetime.fromtimestamp(a["leave_time"], timezone.utc).isoformat() if a.get("leave_time") else None
            tot_min = a.get("total_minutes")
            if tot_min is None or tot_min == 0.0:
                tot_min = round((a.get("total_seconds") or 0) / 60.0, 2)

            attendees_formatted.append({
                "student_id": a["student_id"],
                "display_name": a["display_name"],
                "device_ip": a.get("device_ip", "127.0.0.1"),
                "device_mac": a.get("device_mac", "N/A"),
                "join_time": join_iso,
                "leave_time": leave_iso,
                "total_seconds": a.get("total_seconds", 0),
                "total_minutes": tot_min,
                "final_status": a.get("final_status") or "PENDING",
                "events": events_by_student.get(a["student_id"], [])
            })

        start_dt = datetime.fromtimestamp(sess["start_time"], timezone.utc)
        date_stamp = start_dt.strftime("%Y%m%d_%H%M")

        json_data = {
            "session_id": str(sess["session_id"]),
            "host_system": self.config.get("HOST_SYSTEM_NAME", "Campus Mind"),
            "faculty_id": sess["faculty_id"],
            "subject": sess["subject"],
            "start_time": start_dt.isoformat(),
            "end_time": datetime.fromtimestamp(sess["end_time"], timezone.utc).isoformat() if sess.get("end_time") else None,
            "planned_duration": sess["planned_duration"],
            "actual_duration": sess["actual_duration"],
            "hotspot_ssid": sess["hotspot_ssid"],
            "attendees": attendees_formatted,
            # Dual compatibility keys:
            "session_information": {
                "session_id": sess["session_id"],
                "faculty_id": sess["faculty_id"],
                "subject": sess["subject"],
                "start_time": start_dt.isoformat(),
                "end_time": datetime.fromtimestamp(sess["end_time"], timezone.utc).isoformat() if sess.get("end_time") else None,
                "session_duration_minutes": sess["session_duration_minutes"],
                "disconnect_grace_seconds": sess["disconnect_grace_seconds"],
                "total_joined": sess["total_joined"]
            },
            "student_attendance_records": attendees,
            "connection_event_history": events
        }

        # Write session report JSON
        json_path = os.path.join(export_folder, f"session_{session_id}_report.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=2)

        # Also write timestamped filename as specified in Blueprint Section 11/12
        date_json_path = os.path.join(export_folder, f"session_{date_stamp}.json")
        with open(date_json_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=2)

        # Write flat CSV for Excel-based review
        csv_path = os.path.join(export_folder, f"session_{session_id}_report.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Session ID", "Host System", "Subject", "Faculty", "Student ID", "Student Name",
                "Device IP", "Device MAC", "Attended (Seconds)", "Attended (Minutes)", "Final Status", "Event Count"
            ])
            for a in attendees_formatted:
                writer.writerow([
                    sess["session_id"],
                    self.config.get("HOST_SYSTEM_NAME", "Campus Mind"),
                    sess["subject"],
                    sess["faculty_id"],
                    a["student_id"],
                    a["display_name"],
                    a["device_ip"],
                    a["device_mac"],
                    a["total_seconds"],
                    a["total_minutes"],
                    a["final_status"],
                    len(a["events"])
                ])

        date_csv_path = os.path.join(export_folder, f"session_{date_stamp}.csv")
        with open(date_csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Session ID", "Host System", "Subject", "Faculty", "Student ID", "Student Name",
                "Device IP", "Device MAC", "Attended (Seconds)", "Attended (Minutes)", "Final Status", "Event Count"
            ])
            for a in attendees_formatted:
                writer.writerow([
                    sess["session_id"],
                    self.config.get("HOST_SYSTEM_NAME", "Campus Mind"),
                    sess["subject"],
                    sess["faculty_id"],
                    a["student_id"],
                    a["display_name"],
                    a["device_ip"],
                    a["device_mac"],
                    a["total_seconds"],
                    a["total_minutes"],
                    a["final_status"],
                    len(a["events"])
                ])

        return {"json": json_path, "csv": csv_path, "date_json": date_json_path, "date_csv": date_csv_path}

    async def send_webhook_notification(self, url: str, session_id: int):
        try:
            live = await self.get_live_session_data(session_id)
            payload = json.dumps(live).encode("utf-8")
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            logger.debug(f"Webhook dispatch failed: {e}")

    async def broadcast_faculty(self, message: dict):
        dead = []
        for ws in self.ws_faculty_listeners:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.ws_faculty_listeners:
                self.ws_faculty_listeners.remove(ws)

    async def broadcast_student(self, session_id: int, student_id: str, message: dict):
        key = f"{session_id}_{student_id}"
        dead = []
        for ws in self.ws_student_listeners.get(key, []):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if key in self.ws_student_listeners and ws in self.ws_student_listeners[key]:
                self.ws_student_listeners[key].remove(ws)

    async def run_ticker(self):
        while True:
            await asyncio.sleep(1)
            try:
                now = time.time()
                dt = now - self.last_tick_time
                self.last_tick_time = now

                active_sess = await self.get_active_session()
                if not active_sess:
                    continue

                sid = active_sess["session_id"]
                duration_sec = active_sess["session_duration_minutes"] * 60
                elapsed = now - active_sess["start_time"]

                if elapsed >= duration_sec:
                    await self.end_session(sid)
                    continue

                self.refresh_config()
                if self.config.get("MOCK_MODE", False):
                    if self.mock_provider is None:
                        self.mock_provider = MockProvider(
                            count_min=self.config.get("MOCK_STUDENT_MIN", 15),
                            count_max=self.config.get("MOCK_STUDENT_MAX", 25)
                        )
                else:
                    self.mock_provider = None

                if self.mock_provider:
                    actions = self.mock_provider.simulate_tick(elapsed)
                    for act in actions:
                        if act["action"] == "join":
                            s = act["student"]
                            await self.join_student(sid, s["student_id"], s["name"], s["ip"], s["mac"])
                        elif act["action"] == "heartbeat":
                            try:
                                await self.record_heartbeat(sid, act["student_id"])
                            except Exception:
                                pass
                        elif act["action"] == "disconnect":
                            try:
                                await self.record_disconnect(sid, act["student_id"], act.get("status", "PAUSED"))
                            except Exception:
                                pass
                        elif act["action"] == "reconnect":
                            try:
                                await self.record_reconnect(sid, act["student_id"], act.get("status", "RECONNECTED"))
                            except Exception:
                                pass

                updated_attendees = await self.engine.process_tick(
                    session_id=sid,
                    grace_seconds=active_sess["disconnect_grace_seconds"],
                    elapsed_since_last_tick=dt
                )

                live_data = await self.get_live_session_data(sid)

                await self.broadcast_faculty({
                    "type": "TICK",
                    "data": live_data
                })

                for att in updated_attendees:
                    st_id = att["student_id"]
                    key = f"{sid}_{st_id}"
                    if key in self.ws_student_listeners and self.ws_student_listeners[key]:
                        pct = min(100.0, round((att["total_seconds"] / max(duration_sec, 1)) * 100.0, 1))
                        await self.broadcast_student(sid, st_id, {
                            "type": "STUDENT_TICK",
                            "data": {
                                "current_status": att["current_status"],
                                "attended_seconds": att["total_seconds"],
                                "attended_minutes": att["total_minutes"],
                                "attendance_percentage": pct,
                                "remaining_seconds": live_data["remaining_seconds"]
                            }
                        })

            except Exception as e:
                logger.error(f"Error in ticker loop: {e}", exc_info=True)
