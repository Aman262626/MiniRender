import os
import json
import hashlib
import secrets
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, BigInteger, String, Text, DateTime, text
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./render_clone.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()
    return f"{salt}\${dk}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, dk = stored.split("$", 1)
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()
        return secrets.compare_digest(dk, check)
    except Exception:
        return False


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, default="user")
    github_username = Column(String, default="")
    is_active = Column(Integer, default=1)
    last_ip = Column(String, default="")
    last_seen = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ActivityLog(Base):
    __tablename__ = "activity_logs"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, nullable=False)
    action = Column(String, nullable=False)
    details = Column(String, default="")
    ip_address = Column(String, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class TelegramAllowedUser(Base):
    __tablename__ = "telegram_allowed_users"
    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(BigInteger, unique=True, index=True, nullable=False)
    label = Column(String, default="User")
    added_by = Column(String, default="admin")
    created_at = Column(DateTime, default=datetime.utcnow)


class Project(Base):
    __tablename__ = "projects"
    id = Column(Integer, primary_key=True, index=True)
    owner_username = Column(String, default="admin")
    name = Column(String, unique=True, index=True, nullable=False)
    service_type = Column(String, default="web")
    runtime = Column(String, default="python")
    build_command = Column(String, default="")
    start_command = Column(String, default="")
    internal_port = Column(Integer, default=8000)
    host_port = Column(Integer, unique=True, nullable=False)
    custom_domain = Column(String, default="")
    github_repo = Column(String, default="")
    github_branch = Column(String, default="main")
    root_dir = Column(String, default="")
    auto_deploy = Column(Integer, default=1)
    webhook_secret = Column(String, default="")
    env_vars_json = Column(Text, default="{}")
    status = Column(String, default="Stopped")
    container_id = Column(String, nullable=True)
    deploy_logs = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def get_env(self) -> dict:
        try:
            return json.loads(self.env_vars_json or "{}")
        except Exception:
            return {}

    def set_env(self, env_dict: dict):
        self.env_vars_json = json.dumps(env_dict)


class GitHubAuth(Base):
    __tablename__ = "github_auth"
    id = Column(Integer, primary_key=True, index=True)
    owner_username = Column(String, unique=True, nullable=False)
    username = Column(String, nullable=False)
    avatar_url = Column(String, default="")
    access_token = Column(String, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def log_activity(db, username: str, action: str, details: str = "", ip: str = ""):
    db.add(ActivityLog(username=username, action=action, details=details, ip_address=ip))
    db.commit()


def init_db():
    Base.metadata.create_all(bind=engine)
    if DATABASE_URL.startswith("sqlite"):
        migrations = {
            "projects": {
                "owner_username": "VARCHAR DEFAULT 'admin'",
                "custom_domain": "VARCHAR DEFAULT ''",
                "github_repo": "VARCHAR DEFAULT ''",
                "github_branch": "VARCHAR DEFAULT 'main'",
                "root_dir": "VARCHAR DEFAULT ''",
                "auto_deploy": "INTEGER DEFAULT 1",
                "webhook_secret": "VARCHAR DEFAULT ''",
            },
            "github_auth": {"owner_username": "VARCHAR DEFAULT 'admin'"},
        }
        with engine.connect() as conn:
            for table, cols in migrations.items():
                existing = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}
                for col, col_def in cols.items():
                    if col not in existing:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}"))
            conn.commit()

    db = SessionLocal()
    try:
        admin_user = os.getenv("ADMIN_USERNAME", "admin")
        admin_pass = os.getenv("ADMIN_PASSWORD", "admin123")
        existing_admin = db.query(User).filter(User.username == admin_user).first()
        if not existing_admin:
            db.add(User(username=admin_user, password_hash=hash_password(admin_pass),
                        role="admin", github_username=os.getenv("ALLOWED_GITHUB_USER", ""), is_active=1))
        else:
            existing_admin.role = "admin"
            existing_admin.password_hash = hash_password(admin_pass)

        tg_admin = os.getenv("TELEGRAM_ADMIN_ID", "").strip()
        if tg_admin.lstrip("-").isdigit() and int(tg_admin) != 0:
            cid = int(tg_admin)
            if not db.query(TelegramAllowedUser).filter(TelegramAllowedUser.chat_id == cid).first():
                db.add(TelegramAllowedUser(chat_id=cid, label="Super Admin", added_by="system"))

        for raw_id in os.getenv("ALLOWED_TELEGRAM_IDS", "").split(","):
            if raw_id.strip().lstrip("-").isdigit():
                cid = int(raw_id.strip())
                if not db.query(TelegramAllowedUser).filter(TelegramAllowedUser.chat_id == cid).first():
                    db.add(TelegramAllowedUser(chat_id=cid, label="Env Allowed", added_by="system"))
        db.commit()
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
