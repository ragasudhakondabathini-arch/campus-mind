# Data Field Mapping

## Database Tables

### 1. `sessions`
| Field | Type | Description |
|---|---|---|
| `session_id` | INTEGER PRIMARY KEY | Unique session identifier |
| `faculty_id` | TEXT | Faculty identifier (e.g. `faculty01`) |
| `subject` | TEXT | Class course name |
| `start_time` | REAL | Epoch timestamp when session began |
| `end_time` | REAL | Epoch timestamp when session finalized |
| `planned_duration` | INTEGER | Scheduled duration in minutes |
| `actual_duration` | REAL | Total elapsed seconds |
| `session_duration_minutes` | INTEGER | Active duration configured by faculty |
| `disconnect_grace_seconds` | INTEGER | Time before attendance timer pauses |
| `hotspot_ssid` | TEXT | Wi-Fi network SSID |
| `total_joined` | INTEGER | Count of distinct students enrolled |
| `created_at` | TEXT | ISO-8601 creation string |
| `is_active` | INTEGER | 1 if active, 0 if finalized |

### 2. `session_attendees`
| Field | Type | Description |
|---|---|---|
| `row_id` | INTEGER PRIMARY KEY | Internal identifier |
| `session_id` | INTEGER | Foreign key to `sessions` |
| `student_id` | TEXT | Unique student roll number |
| `display_name` | TEXT | Full name of student |
| `device_mac` | TEXT | Client hardware address |
| `device_ip` | TEXT | Client network IP |
| `join_time` | REAL | Epoch timestamp of first check-in |
| `leave_time` | REAL | Epoch timestamp of departure / finish |
| `total_minutes` | REAL | Accumulated active minutes |
| `total_seconds` | INTEGER | Accumulated active seconds |
| `current_status` | TEXT | `JOINED`, `PAUSED`, or `DISCONNECTED` |
| `final_status` | TEXT | `PRESENT` (>=75%), `PARTIAL` (25%-75%), or `DROPPED` (<25%) |
| `last_seen` | REAL | Timestamp of last heartbeat ping |

### 3. `connection_events`
| Field | Type | Description |
|---|---|---|
| `event_id` | INTEGER PRIMARY KEY | Event sequence ID |
| `session_id` | INTEGER | Foreign key to `sessions` |
| `student_id` | TEXT | Affected student |
| `event_type` | TEXT | `JOINED`, `PAUSED`, `RESUMED`, `DISCONNECTED`, `FINALIZED` |
| `timestamp` | REAL | Epoch timestamp |
| `details` | TEXT | Informational context |

