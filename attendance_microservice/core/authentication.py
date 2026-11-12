import hashlib
import os
import json
import time
from datetime import datetime, timedelta, timezone
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import aiosqlite
from storage.ledger import get_db

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "integration_config.json")
with open(CONFIG_PATH, "r") as f:
    CONFIG = json.load(f)

JWT_SECRET = CONFIG.get("JWT_SECRET")

if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET is missing from configuration")
JWT_ALGORITHM = CONFIG.get("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = CONFIG.get("JWT_EXPIRE_MINUTES", 480)

security = HTTPBearer(auto_error=False)

def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 100000)
    return f"{salt}${key.hex()}"

def verify_password(stored_password_hash: str, provided_password: str) -> bool:
    try:
        salt, key_hex = stored_password_hash.split('$')
        new_key = hashlib.pbkdf2_hmac('sha256', provided_password.encode('utf-8'), salt.encode('utf-8'), 100000)
        return hmac_compare(new_key.hex(), key_hex)
    except Exception:
        return False

def hmac_compare(val1: str, val2: str) -> bool:
    import hmac
    return hmac.compare_digest(val1, val2)

def create_access_token(data: dict, expires_delta: timedelta = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=JWT_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None

async def init_default_faculty():
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM faculty_users WHERE faculty_id = ?", (CONFIG.get("DEFAULT_FACULTY_ID", "faculty01"),))
        row = await cur.fetchone()
        default_name = CONFIG.get("DEFAULT_FACULTY_NAME", "Faculty Member")
        if not row:
            p_hash = hash_password(CONFIG.get("DEFAULT_FACULTY_PASSWORD", "admin123"))
            await db.execute(
                "INSERT INTO faculty_users (faculty_id, name, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (CONFIG.get("DEFAULT_FACULTY_ID", "faculty01"), default_name, p_hash, datetime.now(timezone.utc).isoformat())
            )
            await db.commit()
        elif row["name"] == "Dr. Ananya Sharma":
            await db.execute("UPDATE faculty_users SET name = ? WHERE faculty_id = ?", (default_name, row["faculty_id"]))
            await db.commit()

async def get_current_faculty(credentials: HTTPAuthorizationCredentials = Depends(security), request: Request = None):
    token = None
    if credentials:
        token = credentials.credentials
    elif request:
        token = request.cookies.get("faculty_token")

    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    payload = decode_access_token(token)
    if not payload or "sub" not in payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    faculty_id = payload["sub"]
    async with get_db() as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT faculty_id, name FROM faculty_users WHERE faculty_id = ?", (faculty_id,))
        faculty = await cur.fetchone()
        if not faculty:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Faculty user not found")
        return dict(faculty)
