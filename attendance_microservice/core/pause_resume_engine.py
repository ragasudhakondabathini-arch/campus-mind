import time
import aiosqlite
from typing import Dict, Any, Tuple
from storage.ledger import get_db

class PauseResumeEngine:
    def __init__(self, present_pct: int = 75, partial_pct: int = 25):
        self.present_pct = present_pct
        self.partial_pct = partial_pct

    async def record_event(self, session_id: int, student_id: str, event_type: str, details: str = None):
        async with get_db() as db:
            await db.execute(
                "INSERT INTO connection_events (session_id, student_id, event_type, timestamp, details) VALUES (?, ?, ?, ?, ?)",
                (session_id, student_id, event_type, time.time(), details)
            )
            await db.commit()

    async def process_tick(self, session_id: int, grace_seconds: int, elapsed_since_last_tick: float = 1.0) -> Dict[str, Any]:
        now = time.time()
        updated_attendees = []

        async with get_db() as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM session_attendees WHERE session_id = ?", (session_id,))
            rows = await cur.fetchall()

            for row in rows:
                sid = row["student_id"]
                curr_status = row["current_status"]
                last_seen = row["last_seen"]
                time_since_seen = now - last_seen
                total_sec = row["total_seconds"]
                new_status = curr_status
                event_to_record = None

                if curr_status in ("JOINED", "RECONNECTED"):
                    if time_since_seen <= grace_seconds:
                        total_sec += int(elapsed_since_last_tick)
                        # Reconnected student smoothly transitions back to JOINED after maintaining active connection
                        if curr_status == "RECONNECTED" and time_since_seen <= 3:
                            if total_sec % 4 == 0:
                                new_status = "JOINED"
                    else:
                        if time_since_seen <= (grace_seconds * 2.5):
                            new_status = "PAUSED"
                            event_to_record = "PAUSED"
                        else:
                            new_status = "DROPPED"
                            event_to_record = "DROPPED"
                elif curr_status == "PAUSED":
                    if time_since_seen > (grace_seconds * 2.5):
                        new_status = "DROPPED"
                        event_to_record = "DROPPED"
                elif curr_status == "DROPPED":
                    # Stays dropped unless student re-joins or reconnects
                    pass

                total_min = round(total_sec / 60.0, 2)
                await db.execute(
                    """UPDATE session_attendees 
                       SET total_seconds = ?, total_minutes = ?, current_status = ? 
                       WHERE session_id = ? AND student_id = ?""",
                    (total_sec, total_min, new_status, session_id, sid)
                )

                if event_to_record:
                    await db.execute(
                        "INSERT INTO connection_events (session_id, student_id, event_type, timestamp, details) VALUES (?, ?, ?, ?, ?)",
                        (session_id, sid, event_to_record, now, f"Grace limit {grace_seconds}s; idle {int(time_since_seen)}s")
                    )

                updated_attendees.append({
                    "student_id": sid,
                    "display_name": row["display_name"],
                    "current_status": new_status,
                    "total_seconds": total_sec,
                    "total_minutes": total_min,
                    "last_seen": last_seen,
                    "device_ip": row["device_ip"] or "127.0.0.1",
                    "device_mac": row["device_mac"] or "N/A"
                })

            await db.commit()

        return updated_attendees

    def calculate_final_status(self, total_attended_seconds: int, planned_duration_minutes: int) -> Tuple[str, float]:
        planned_seconds = max(planned_duration_minutes * 60, 1)
        percentage = min(100.0, round((total_attended_seconds / planned_seconds) * 100.0, 1))
        
        if percentage >= self.present_pct:
            return "PRESENT", percentage
        elif percentage >= self.partial_pct:
            return "PARTIAL", percentage
        else:
            return "DROPPED", percentage
