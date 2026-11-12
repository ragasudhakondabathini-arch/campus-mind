# Campus Mind Wi-Fi Smart Attendance System – Integration Guide

## Overview
Campus Mind is a standalone attendance microservice that enforces physical classroom attendance boundaries using a Wi-Fi Hotspot perimeter.

## Running the Service
1. Install dependencies:
   ```cmd
   pip install -r requirements.txt
   ```
2. Start the microservice:
   ```cmd
   py -m uvicorn main:app --host 0.0.0.0 --port 8001 --reload
   ```
3. Open in browser:
   - Faculty Portal: `http://localhost:8001/login` (default: `faculty01` / `admin123`)
   - Student Portal: `http://localhost:8001/student`
   - History & Reports: `http://localhost:8001/history`

## Architecture
- **Hotspot Monitor**: Detects Windows Mobile Hotspot state and connected clients.
- **Pause/Resume Engine**: State machine tracking `NOT_JOINED` -> `JOINED` -> `PAUSED` -> `JOINED` -> `FINAL STATUS`.
- **Mock Provider**: When `MOCK_MODE: true` in `integration_config.json`, generates 15–25 simulated student joins, pauses, reconnects and attendance ticks.
- **Reporting**: Exports standard Excel-compatible CSV and structured JSON files upon session completion to `./exports`.

