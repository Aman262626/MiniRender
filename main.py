import os
import re
import pty
import hmac
import hashlib
import secrets
import shutil
import asyncio
import threading
import subprocess
from collections import deque
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from fastapi import (
    FastAPI, UploadFile, File, Form, Depends,
    HTTPException, WebSocket, WebSocketDisconnect, Request, Response
)
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session
import httpx

from database import (
    init_db, get_db, Project, User, ActivityLog,
    TelegramAllowedUser, GitHubAuth, SessionLocal, hash_password, verify_password, log_activity
)
import process_manager as pm
import github_manager as gm
import auth
import telegram_bot
import notifier

init_db()


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    ptb_app = telegram_bot.create_bot_app()
    if ptb_app:
        await ptb_app.initialize()
        await ptb_app.start()
        await ptb_app.updater.start_polling()
        notifier.init_notifier(ptb_app.bot, loop)
        await notifier.send_alert("🟢 <b>MiniRender Platform Started on Render!</b>")
    yield
    for name in list(pm.RUNNING_PROCS.keys()):
        pm.stop_process(name)
    if ptb_app:
        await ptb_app.updater.stop()
        await ptb_app.stop()
        await ptb_app.shutdown()


app = FastAPI(title="MiniRender PaaS", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


def get_next_port(db: Session) -> int:
    used = {p.host_port for p in db.query(Project.host_port).all()}
    for port in range(10001, 60000):
        if port not in used:
            return port
    raise RuntimeError("No free internal ports")


def get_base_url(request: Request) -> str:
    env_url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    return env_url if env_url else str(request.base_url).rstrip("/")


def format_url(request: Request, p: Project) -> str:
    if p.custom_domain:
        return f"https://{p.custom_domain.strip()}/"
    prefix = "site" if p.service_type == "static" else "app"
    return f"{get_base_url(request)}/{prefix}/{p.name}/"


def serialize_project(request: Request, p: Project) -> dict:
    return {
        "id": p.id, "owner_username": p.owner_username, "name": p.name,
        "service_type": p.service_type, "runtime": p.runtime,
        "build_command": p.build_command, "start_command": p.start_command,
        "internal_port": p.host_port, "url": format_url(request, p),
        "github_repo": p.github_repo, "github_branch": p.github_branch,
        "root_dir": p.root_dir or "", "auto_deploy": p.auto_deploy,
        "env_vars": p.get_env(), "status": p.status,
        "deploy_logs": p.deploy_logs, "custom_domain": p.custom_domain or "",
        "updated_at": p.updated_at.isoformat() if p.updated_at else "",
    }


def check_project_access(p: Project, user: User):
    if not p:
        raise HTTPException(404, "Project not found")
    if user.role != "admin" and p.owner_username != user.username:
        raise HTTPException(403, "Forbidden")


def run_deploy_task(project_id: int, base_url: str):
    db = SessionLocal()
    try:
        p = db.query(Project).filter(Project.id == project_id).first()
        if not p:
            return
        p.status = "Building"
        db.commit()
        pid, logs = pm.deploy_project(p)
        p.container_id, p.deploy_logs, p.status = pid, logs, "Live"
        db.commit()
        prefix = "site" if p.service_type == "static" else "app"
        notifier.send_alert_sync(
            f"✅ <b>Project Deployed!</b>\n📛 Name: <code>{notifier.esc(p.name)}</code> (by {p.owner_username})\n🔗 URL: {base_url}/{prefix}/{p.name}/"
        )
    except Exception as e:
        p = db.query(Project).filter(Project.id == project_id).first()
        if p:
            p.status = "Failed"
            p.deploy_logs = (p.deploy_logs or "") + f"\n❌ ERROR: {str(e)}"
            db.commit()
            notifier.send_alert_sync(f"❌ <b>Deploy Error ({notifier.esc(p.name)})</b>\n<pre>{notifier.esc(str(e)[:1000])}</pre>")
    finally:
        db.close()


def run_github_deploy_task(project_id: int, base_url: str, token: str):
    db = SessionLocal()
    try:
        p = db.query(Project).filter(Project.id == project_id).first()
        if not p:
            return
        p.status = "Building"
        db.commit()
        gm.download_repo_sync(token, p.github_repo, p.github_branch, os.path.join(pm.DEPLOYMENTS_DIR, p.name), p.root_dir)
    except Exception as e:
        p = db.query(Project).filter(Project.id == project_id).first()
        if p:
            p.status = "Failed"
            p.deploy_logs = f"❌ GitHub Download Error: {e}"
            db.commit()
            notifier.send_alert_sync(f"❌ <b>GitHub Pull Error ({notifier.esc(p.name)})</b>\n<code>{notifier.esc(str(e))}</code>")
        db.close()
        return
    db.close()
    run_deploy_task(project_id, base_url)


# 🌐 HOSTED STATIC & WEB PROXY ROUTES
@app.get("/site/{site_name}/")
@app.get("/site/{site_name}/{path:path}")
def serve_static_site(site_name: str, path: str = ""):
    folder = os.path.realpath(os.path.join(pm.DEPLOYMENTS_DIR, site_name))
    if not os.path.exists(folder):
        return HTMLResponse("❌ Site not found", status_code=404)
    target = os.path.realpath(os.path.join(folder, path or "index.html"))
    if not target.startswith(folder):
        return HTMLResponse("⛔ Forbidden", status_code=403)
    if os.path.isdir(target):
        target = os.path.join(target, "index.html")
    return FileResponse(target) if os.path.isfile(target) else HTMLResponse("❌ File not found", status_code=404)


@app.api_route("/app/{app_name}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
@app.api_route("/app/{app_name}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def proxy_web_service(app_name: str, request: Request, path: str = "", db: Session = Depends(get_db)):
    p = db.query(Project).filter(Project.name == app_name, Project.service_type == "web").first()
    if not p or p.status != "Live":
        return HTMLResponse(f"<h3>Service '{app_name}' is not live</h3>", status_code=503)
    target_url = f"http://127.0.0.1:{p.host_port}/{path}" + (f"?{request.url.query}" if request.url.query else "")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    body = await request.body()
    async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
        try:
            resp = await client.request(request.method, target_url, headers=headers, content=body)
        except Exception as e:
            await notifier.send_alert(f"⚠️ <b>Site Error ({notifier.esc(app_name)})</b>\n<code>{notifier.esc(str(e))}</code>")
            return HTMLResponse(f"<h3>Service Error: {e}</h3>", status_code=502)
    excluded = {"content-encoding", "transfer-encoding", "content-length"}
    return Response(content=resp.content, status_code=resp.status_code, headers={k: v for k, v in resp.headers.items() if k.lower() not in excluded})


# 🔐 AUTHENTICATION
class LoginPayload(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(payload: LoginPayload, request: Request, response: Response, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == payload.username.strip()).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "गलत Username या Password")
    if not user.is_active:
        raise HTTPException(403, "आपका अकाउंट Admin द्वारा ब्लॉक किया गया है")
    ip = auth.get_client_ip(request)
    user.last_seen, user.last_ip = datetime.utcnow(), ip
    log_activity(db, user.username, "LOGIN", "Password login", ip)
    token = auth.create_session_token(user.username)
    response.set_cookie(key=auth.COOKIE_NAME, value=token, httponly=True, samesite="lax", max_age=auth.SESSION_TTL)
    return {"ok": True, "username": user.username, "role": user.role}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE_NAME)
    return {"ok": True}


@app.get("/api/me")
def me(user: User = Depends(auth.get_current_user)):
    return {"username": user.username, "role": user.role, "github_username": user.github_username}


# 🐙 GITHUB OAUTH
@app.get("/api/github/login")
def github_oauth_start(request: Request, mode: str = "connect"):
    if not gm.GITHUB_CLIENT_ID or not gm.GITHUB_CLIENT_SECRET:
        raise HTTPException(400, "GITHUB_CLIENT_ID और GITHUB_CLIENT_SECRET सेट करें")
    redirect_uri = f"{get_base_url(request)}/api/github/callback"
    return RedirectResponse(
        f"https://github.com/login/oauth/authorize?client_id={gm.GITHUB_CLIENT_ID}&redirect_uri={redirect_uri}&scope=repo,read:user&state={mode}:{secrets.token_urlsafe(12)}"
    )


@app.get("/api/github/callback")
async def github_oauth_callback(request: Request, code: str, db: Session = Depends(get_db)):
    token = await gm.exchange_code_for_token(code, f"{get_base_url(request)}/api/github/callback")
    gh_user = await gm.get_github_user(token)
    gh_username = gh_user["login"]
    ip = auth.get_client_ip(request)

    logged_username = auth.verify_session_token(request.cookies.get(auth.COOKIE_NAME, ""))
    user = db.query(User).filter(User.username == logged_username).first() if logged_username else None

    if not user:
        allowed_gh = os.getenv("ALLOWED_GITHUB_USER", "").strip().lower()
        if allowed_gh and gh_username.lower() == allowed_gh:
            user = db.query(User).filter(User.role == "admin").first()
        else:
            user = db.query(User).filter(User.github_username.ilike(gh_username)).first()

        if not user or not user.is_active:
            return HTMLResponse(f"<h3>⛔ Unauthorized GitHub (@{gh_username})</h3><p>Admin से अकाउंट बनवाएं या ALLOWED_GITHUB_USER सेट करें।</p>", status_code=403)

    user.github_username = gh_username
    user.last_seen, user.last_ip = datetime.utcnow(), ip

    gh_auth = db.query(GitHubAuth).filter(GitHubAuth.owner_username == user.username).first()
    if not gh_auth:
        db.add(GitHubAuth(owner_username=user.username, username=gh_username, avatar_url=gh_user.get("avatar_url", ""), access_token=token))
    else:
        gh_auth.username, gh_auth.avatar_url, gh_auth.access_token = gh_username, gh_user.get("avatar_url", ""), token
    log_activity(db, user.username, "GITHUB_AUTH", f"@{gh_username}", ip)

    resp = RedirectResponse("/")
    resp.set_cookie(key=auth.COOKIE_NAME, value=auth.create_session_token(user.username), httponly=True, samesite="lax", max_age=auth.SESSION_TTL)
    return resp


@app.get("/api/github/status")
def github_status(user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    gh = db.query(GitHubAuth).filter(GitHubAuth.owner_username == user.username).first()
    return {"connected": True, "username": gh.username, "avatar_url": gh.avatar_url} if gh else {"connected": False}


@app.get("/api/github/repos")
async def github_repos(user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    gh = db.query(GitHubAuth).filter(GitHubAuth.owner_username == user.username).first()
    if not gh:
        raise HTTPException(400, "GitHub कनेक्ट नहीं है")
    return await gm.fetch_all_repos(gh.access_token)


@app.get("/api/github/branches")
async def github_branches(repo: str, user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    gh = db.query(GitHubAuth).filter(GitHubAuth.owner_username == user.username).first()
    if not gh:
        raise HTTPException(400, "GitHub कनेक्ट नहीं है")
    return await gm.fetch_branches(gh.access_token, repo)


@app.get("/api/github/contents")
async def github_contents(repo: str, branch: str = "main", path: str = "", user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    gh = db.query(GitHubAuth).filter(GitHubAuth.owner_username == user.username).first()
    if not gh:
        raise HTTPException(400, "GitHub कनेक्ट नहीं है")
    return await gm.fetch_contents(gh.access_token, repo, branch, path)


# 🛡️ ADMIN PANEL APIs
@app.get("/api/admin/overview")
def admin_overview(admin: User = Depends(auth.require_admin), db: Session = Depends(get_db)):
    now = datetime.utcnow()
    users = db.query(User).order_by(User.id.asc()).all()
    projects = db.query(Project).all()
    logs = db.query(ActivityLog).order_by(ActivityLog.id.desc()).limit(60).all()
    tg_users = db.query(TelegramAllowedUser).order_by(TelegramAllowedUser.id.asc()).all()

    return {
        "users": [
            {
                "id": u.id, "username": u.username, "role": u.role,
                "github_username": u.github_username, "is_active": bool(u.is_active),
                "last_ip": u.last_ip or "-",
                "last_seen": u.last_seen.isoformat() if u.last_seen else None,
                "online": bool(u.last_seen and (now - u.last_seen) < timedelta(minutes=5)),
                "project_count": sum(1 for p in projects if p.owner_username == u.username),
            }
            for u in users
        ],
        "logs": [
            {"id": l.id, "username": l.username, "action": l.action, "details": l.details, "ip": l.ip_address, "time": l.created_at.isoformat()}
            for l in logs
        ],
        "telegram_admin_id": os.getenv("TELEGRAM_ADMIN_ID", "Not Set"),
        "telegram_users": [
            {"id": t.id, "chat_id": t.chat_id, "label": t.label, "added_by": t.added_by}
            for t in tg_users
        ],
    }


class CreateUserPayload(BaseModel):
    username: str
    password: str
    role: str = "user"
    github_username: str = ""


@app.post("/api/admin/users")
def admin_create_user(payload: CreateUserPayload, request: Request, admin: User = Depends(auth.require_admin), db: Session = Depends(get_db)):
    uname = payload.username.strip().lower()
    if not re.match(r"^[a-z0-9_-]+$", uname):
        raise HTTPException(400, "Username में सिर्फ़ a-z, 0-9, _ और - रखें")
    if db.query(User).filter(User.username == uname).first():
        raise HTTPException(400, "यह यूज़र पहले से मौजूद है")
    db.add(User(username=uname, password_hash=hash_password(payload.password), role=payload.role, github_username=payload.github_username.strip(), is_active=1))
    log_activity(db, admin.username, "CREATE_USER", uname, auth.get_client_ip(request))
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/toggle")
def admin_toggle_user(user_id: int, request: Request, admin: User = Depends(auth.require_admin), db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == user_id).first()
    if not u or u.username == admin.username:
        raise HTTPException(400, "Invalid operation")
    u.is_active = 0 if u.is_active else 1
    log_activity(db, admin.username, "TOGGLE_USER", f"{u.username} -> active={u.is_active}", auth.get_client_ip(request))
    return {"is_active": bool(u.is_active)}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, request: Request, admin: User = Depends(auth.require_admin), db: Session = Depends(get_db)):
    u = db.query(User).filter(User.id == user_id).first()
    if not u or u.username == admin.username:
        raise HTTPException(400, "Cannot delete self")
    log_activity(db, admin.username, "DELETE_USER", u.username, auth.get_client_ip(request))
    db.delete(u)
    db.commit()
    return {"deleted": True}


class TgUserPayload(BaseModel):
    chat_id: int
    label: str = "User"


@app.post("/api/admin/telegram")
def admin_add_tg(payload: TgUserPayload, request: Request, admin: User = Depends(auth.require_admin), db: Session = Depends(get_db)):
    if not db.query(TelegramAllowedUser).filter(TelegramAllowedUser.chat_id == payload.chat_id).first():
        db.add(TelegramAllowedUser(chat_id=payload.chat_id, label=payload.label, added_by=admin.username))
        log_activity(db, admin.username, "ADD_TG_USER", f"{payload.chat_id} ({payload.label})", auth.get_client_ip(request))
    return {"ok": True}


@app.delete("/api/admin/telegram/{chat_id}")
def admin_del_tg(chat_id: int, request: Request, admin: User = Depends(auth.require_admin), db: Session = Depends(get_db)):
    db.query(TelegramAllowedUser).filter(TelegramAllowedUser.chat_id == chat_id).delete()
    log_activity(db, admin.username, "DEL_TG_USER", str(chat_id), auth.get_client_ip(request))
    return {"deleted": True}


# 📦 PROJECTS CRUD
@app.get("/", response_class=HTMLResponse)
def dashboard():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/projects")
def list_projects(request: Request, user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    q = db.query(Project) if user.role == "admin" else db.query(Project).filter(Project.owner_username == user.username)
    return [serialize_project(request, p) for p in q.order_by(Project.id.desc()).all()]


@app.post("/api/projects")
async def create_and_deploy(
    request: Request, name: str = Form(...), service_type: str = Form("web"), runtime: str = Form("python"),
    build_command: str = Form(""), start_command: str = Form(""), internal_port: int = Form(8000),
    zip_file: UploadFile = File(...), user: User = Depends(auth.get_current_user), db: Session = Depends(get_db),
):
    clean_name = name.strip().lower()
    if not re.match(r"^[a-z0-9-]+$", clean_name):
        raise HTTPException(400, "Project name में सिर्फ़ a-z, 0-9 और '-' रखें")
    if db.query(Project).filter(Project.name == clean_name).first():
        raise HTTPException(400, "इस नाम का प्रोजेक्ट पहले से मौजूद है")
    if not zip_file.filename.endswith(".zip"):
        raise HTTPException(400, "सिर्फ़ .zip फाइल अपलोड करें")

    project_dir = os.path.join(pm.DEPLOYMENTS_DIR, clean_name)
    temp_zip = os.path.join(pm.DEPLOYMENTS_DIR, f"{clean_name}.zip")
    with open(temp_zip, "wb") as buffer:
        shutil.copyfileobj(zip_file.file, buffer)
    try:
        pm.safe_extract_zip(temp_zip, project_dir)
    finally:
        if os.path.exists(temp_zip):
            os.remove(temp_zip)

    p = Project(
        owner_username=user.username, name=clean_name, service_type=service_type,
        runtime="static" if service_type == "static" else runtime,
        build_command=build_command, start_command=start_command, internal_port=internal_port,
        host_port=get_next_port(db), status="Building", deploy_logs="==> Queued...",
    )
    db.add(p)
    log_activity(db, user.username, "DEPLOY_ZIP", clean_name, auth.get_client_ip(request))
    db.refresh(p)
    threading.Thread(target=run_deploy_task, args=(p.id, get_base_url(request)), daemon=True).start()
    return serialize_project(request, p)


@app.post("/api/projects/github")
async def create_from_github(
    request: Request, name: str = Form(...), service_type: str = Form("web"), runtime: str = Form("python"),
    build_command: str = Form(""), start_command: str = Form(""), internal_port: int = Form(8000),
    github_repo: str = Form(...), github_branch: str = Form("main"), root_dir: str = Form(""),
    auto_deploy: int = Form(1), user: User = Depends(auth.get_current_user), db: Session = Depends(get_db),
):
    gh = db.query(GitHubAuth).filter(GitHubAuth.owner_username == user.username).first()
    if not gh:
        raise HTTPException(400, "पहले GitHub कनेक्ट करें")
    clean_name = name.strip().lower()
    if not re.match(r"^[a-z0-9-]+$", clean_name):
        raise HTTPException(400, "Project name में सिर्फ़ a-z, 0-9 और '-' रखें")
    if db.query(Project).filter(Project.name == clean_name).first():
        raise HTTPException(400, "इस नाम का प्रोजेक्ट पहले से मौजूद है")

    wh_secret = secrets.token_hex(24)
    p = Project(
        owner_username=user.username, name=clean_name, service_type=service_type,
        runtime="static" if service_type == "static" else runtime,
        build_command=build_command, start_command=start_command, internal_port=internal_port,
        host_port=get_next_port(db), github_repo=github_repo, github_branch=github_branch,
        root_dir=root_dir, auto_deploy=auto_deploy, webhook_secret=wh_secret,
        status="Building", deploy_logs=f"==> Cloning {github_repo} ({github_branch})...",
    )
    db.add(p)
    log_activity(db, user.username, "DEPLOY_GITHUB", f"{clean_name} <- {github_repo}", auth.get_client_ip(request))
    db.refresh(p)

    base_url = get_base_url(request)
    if auto_deploy:
        try:
            await gm.create_webhook(gh.access_token, github_repo, f"{base_url}/api/github/webhook/{p.id}", wh_secret)
        except Exception:
            pass
    threading.Thread(target=run_github_deploy_task, args=(p.id, base_url, gh.access_token), daemon=True).start()
    return serialize_project(request, p)


@app.post("/api/github/webhook/{project_id}")
async def github_webhook(project_id: int, request: Request, db: Session = Depends(get_db)):
    p = db.query(Project).filter(Project.id == project_id).first()
    if not p or not p.auto_deploy:
        return {"ignored": True}
    gh = db.query(GitHubAuth).filter(GitHubAuth.owner_username == p.owner_username).first()
    if not gh:
        return {"ignored": True}
    body = await request.body()
    expected = "sha256=" + hmac.new(p.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(request.headers.get("X-Hub-Signature-256", ""), expected):
        raise HTTPException(403, "Invalid signature")
    payload = await request.json()
    if payload.get("ref", "") not in ("", f"refs/heads/{p.github_branch}"):
        return {"ignored": "branch"}
    await notifier.send_alert(f"⚡ <b>Auto-Deploy (Git Push):</b> <code>{notifier.esc(p.name)}</code>")
    threading.Thread(target=run_github_deploy_task, args=(p.id, get_base_url(request), gh.access_token), daemon=True).start()
    return {"redeploying": True}


@app.post("/api/projects/{project_id}/redeploy")
async def redeploy_project(
    project_id: int, request: Request, zip_file: UploadFile = File(None),
    user: User = Depends(auth.get_current_user), db: Session = Depends(get_db),
):
    p = db.query(Project).filter(Project.id == project_id).first()
    check_project_access(p, user)
    if zip_file:
        temp_zip = os.path.join(pm.DEPLOYMENTS_DIR, f"{p.name}.zip")
        with open(temp_zip, "wb") as buffer:
            shutil.copyfileobj(zip_file.file, buffer)
        try:
            pm.safe_extract_zip(temp_zip, os.path.join(pm.DEPLOYMENTS_DIR, p.name))
        finally:
            if os.path.exists(temp_zip):
                os.remove(temp_zip)
    p.status = "Building"
    log_activity(db, user.username, "REDEPLOY", p.name, auth.get_client_ip(request))
    if not zip_file and p.github_repo:
        gh = db.query(GitHubAuth).filter(GitHubAuth.owner_username == p.owner_username).first()
        if gh:
            threading.Thread(target=run_github_deploy_task, args=(p.id, get_base_url(request), gh.access_token), daemon=True).start()
            return serialize_project(request, p)
    threading.Thread(target=run_deploy_task, args=(p.id, get_base_url(request)), daemon=True).start()
    return serialize_project(request, p)


class EnvUpdate(BaseModel):
    env_vars: dict[str, str]


@app.put("/api/projects/{project_id}/env")
def update_env(project_id: int, payload: EnvUpdate, request: Request, user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    p = db.query(Project).filter(Project.id == project_id).first()
    check_project_access(p, user)
    p.set_env(payload.env_vars)
    p.status = "Building"
    log_activity(db, user.username, "ENV_UPDATE", p.name, auth.get_client_ip(request))
    threading.Thread(target=run_deploy_task, args=(p.id, get_base_url(request)), daemon=True).start()
    return serialize_project(request, p)


class SettingsUpdate(BaseModel):
    build_command: str = ""
    start_command: str = ""
    internal_port: int = 8000
    custom_domain: str = ""


@app.put("/api/projects/{project_id}/settings")
def update_settings(project_id: int, payload: SettingsUpdate, request: Request, user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    p = db.query(Project).filter(Project.id == project_id).first()
    check_project_access(p, user)
    p.build_command, p.start_command, p.custom_domain = payload.build_command, payload.start_command, payload.custom_domain.strip()
    p.status = "Building"
    log_activity(db, user.username, "SETTINGS_UPDATE", p.name, auth.get_client_ip(request))
    threading.Thread(target=run_deploy_task, args=(p.id, get_base_url(request)), daemon=True).start()
    return serialize_project(request, p)


@app.post("/api/projects/{project_id}/action/{action}")
async def project_action(project_id: int, action: str, request: Request, user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    p = db.query(Project).filter(Project.id == project_id).first()
    check_project_access(p, user)
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, "Invalid action")
    pm.control_process(p, action)
    p.status = "Stopped" if action == "stop" else "Live"
    log_activity(db, user.username, action.upper(), p.name, auth.get_client_ip(request))
    await notifier.send_alert(f"{'⏸️' if action == 'stop' else '🔄'} <b>Service {action.title()}ed:</b> <code>{notifier.esc(p.name)}</code> (by {user.username})")
    return {"status": p.status}


@app.get("/api/projects/{project_id}/metrics")
def project_metrics(project_id: int, user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    p = db.query(Project).filter(Project.id == project_id).first()
    check_project_access(p, user)
    return pm.get_metrics(p.name)


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: int, request: Request, user: User = Depends(auth.get_current_user), db: Session = Depends(get_db)):
    p = db.query(Project).filter(Project.id == project_id).first()
    check_project_access(p, user)
    pm.stop_process(p.name)
    pdir = os.path.join(pm.DEPLOYMENTS_DIR, p.name)
    if os.path.exists(pdir):
        shutil.rmtree(pdir)
    name = p.name
    db.delete(p)
    log_activity(db, user.username, "DELETE_PROJECT", name, auth.get_client_ip(request))
    await notifier.send_alert(f"🗑️ <b>Service Deleted:</b> <code>{notifier.esc(name)}</code> (by {user.username})")
    return {"deleted": True}


# 📡 WEBSOCKETS (LOGS & SHELL)
@app.websocket("/ws/logs/{project_id}")
async def ws_logs(websocket: WebSocket, project_id: int):
    db = SessionLocal()
    user = auth.verify_ws_auth(websocket, db)
    p = db.query(Project).filter(Project.id == project_id).first()
    deploy_logs = p.deploy_logs if p else ""
    project_name = p.name if p else ""
    is_allowed = bool(user and p and (user.role == "admin" or p.owner_username == user.username))
    db.close()
    if not is_allowed:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    if deploy_logs:
        await websocket.send_text(deploy_logs + "\n")
    buf = pm.LOG_BUFFERS.setdefault(project_name, deque(maxlen=400))
    sent = 0
    try:
        while True:
            lines = list(buf)
            if len(lines) > sent:
                await websocket.send_text("".join(lines[sent:]))
                sent = len(lines)
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass


@app.websocket("/ws/shell/{project_id}")
async def ws_shell(websocket: WebSocket, project_id: int):
    db = SessionLocal()
    user = auth.verify_ws_auth(websocket, db)
    p = db.query(Project).filter(Project.id == project_id).first()
    project_name = p.name if p else ""
    is_allowed = bool(user and p and (user.role == "admin" or p.owner_username == user.username))
    db.close()
    if not is_allowed:
        await websocket.close(code=1008)
        return
    await websocket.accept()

    project_dir = os.path.join(pm.DEPLOYMENTS_DIR, project_name)
    os.makedirs(project_dir, exist_ok=True)
    shell_bin = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen([shell_bin], stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, cwd=project_dir, preexec_fn=os.setsid, close_fds=True)
    os.close(slave_fd)

    loop = asyncio.get_running_loop()
    stop_flag = threading.Event()

    def read_pty():
        try:
            while not stop_flag.is_set():
                data = os.read(master_fd, 4096)
                if not data:
                    break
                asyncio.run_coroutine_threadsafe(websocket.send_text(data.decode("utf-8", errors="replace")), loop)
        except Exception:
            pass

    threading.Thread(target=read_pty, daemon=True).start()
    try:
        while True:
            os.write(master_fd, (await websocket.receive_text()).encode("utf-8"))
    except WebSocketDisconnect:
        stop_flag.set()
        try:
            os.close(master_fd)
            os.killpg(os.getpgid(proc.pid), 9)
        except Exception:
            pass