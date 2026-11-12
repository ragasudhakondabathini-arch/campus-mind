# Campus Mind Wi-Fi Smart Attendance Management System

A standalone Windows-based attendance system using a Wi-Fi Hotspot perimeter as a classroom boundary.

## Quick Start
```cmd
cd attendance_microservice
py -m uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

## Default Credentials
- **Faculty ID**: `faculty01`
- **Password**: `admin123`

## Portals & URLs
- **Faculty Portal**: [http://localhost:8001/login](http://localhost:8001/login) -> [http://localhost:8001/faculty](http://localhost:8001/faculty)
- **Student Portal**: [http://localhost:8001/student](http://localhost:8001/student)
- **History & Reports**: [http://localhost:8001/history](http://localhost:8001/history)

## Features
- **Adjustable Session Duration & Reconnect Grace Time**: Configured by faculty before launching.
- **Accurate Elapsed-Time State Machine**: Students' timers pause after the grace period expires, and resume when reconnected without penalizing brief interruptions.
- **Automated Attendance Categorization**:
  - `PRESENT`: ≥ 75% attended time
  - `PARTIAL`: 25% – 75% attended time
  - `DROPPED`: < 25% attended time
- **Instant Excel CSV & JSON Export**: Auto-generated upon session completion into `./exports`.
- **Integrated Mock Mode**: Generates 15–25 simulated student interactions for zero-hardware testing.

