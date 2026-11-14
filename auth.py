import os
import hmac
import time
import hashlib
import base64
from datetime import datetime
from fastapi import Request, HTTPException, WebSocket, Depends
from sqlalchemy.orm import Session
from database import get_db, User

SECRET_KEY = os.getenv("SECRET_KEY", "change-this-super-secret-key-98765").encode()
COOKIE_NAME = "paas_session"
SESSION_TTL = 60 * 60 * 24 * 7


def create_session_token(username: str) -> str:
    exp = str(int(time.time()) + SESSION_TTL)
    payload = f"{username}:{exp}".encode()
    sig = hmac.new(SECRET_KEY, payload, hashlib.sha256).hexdigest().encode()
    return base64.urlsafe_b64encode(payload + b":" + sig).decode()


def verify_session_token(token: str) -> str | None:
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        username, exp, sig = raw.rsplit(":", 2)
        if int(exp) < time.time():
            return None
        expected_sig = hmac.new(SECRET_KEY, f"{username}:{exp}".encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected_sig):
            return username
    except Exception:
        return None
    return None


def get_client_ip(request: Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    username = verify_session_token(request.cookies.get(COOKIE_NAME))
    if not username:
        raise HTTPException(status_code=401, detail="Unauthorized: Please login first")
    user = db.query(User).filter(User.username == username).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled or missing")
    user.last_seen = datetime.utcnow()
    user.last_ip = get_client_ip(request)
    db.commit()
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def verify_ws_auth(websocket: WebSocket, db: Session) -> User | None:
    username = verify_session_token(websocket.cookies.get(COOKIE_NAME))
    if not username:
        return None
    user = db.query(User).filter(User.username == username, User.is_active == 1).first()
    return user
