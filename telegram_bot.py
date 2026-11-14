import os
import re
import shutil
import threading
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)

from database import SessionLocal, Project, TelegramAllowedUser, log_activity
import process_manager as pm
import notifier

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ADMIN_ID = int(os.getenv("TELEGRAM_ADMIN_ID", "0") or 0)
user_state = {}


def is_tg_admin(user_id: int) -> bool:
    return TELEGRAM_ADMIN_ID != 0 and user_id == TELEGRAM_ADMIN_ID


def is_authorized(user_id: int) -> bool:
    if is_tg_admin(user_id):
        return True
    db = SessionLocal()
    try:
        return db.query(TelegramAllowedUser).filter(TelegramAllowedUser.chat_id == user_id).first() is not None
    finally:
        db.close()


def get_next_port(db) -> int:
    used = {p.host_port for p in db.query(Project.host_port).all()}
    for port in range(10001, 60000):
        if port not in used:
            return port
    raise RuntimeError("No free ports")


def get_base_url() -> str:
    return os.getenv("RENDER_EXTERNAL_URL", "http://localhost:8000").rstrip("/")


def build_deploy_url(name: str, service_type: str = "web") -> str:
    prefix = "site" if service_type == "static" else "app"
    return f"{get_base_url()}/{prefix}/{name}/"


async def safe_edit(query, text: str, reply_markup=None, parse_mode="HTML"):
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode, disable_web_page_preview=True)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


def main_menu_markup(user_id: int):
    rows = [
        [InlineKeyboardButton("📦 नया प्रोजेक्ट Deploy करें (ZIP)", callback_data="menu:deploy")],
        [InlineKeyboardButton("🌐 मेरे सभी प्रोजेक्ट्स", callback_data="menu:projects:0")],
        [InlineKeyboardButton("📊 सर्वर स्टेटस", callback_data="menu:status"),
         InlineKeyboardButton("🖥️ Web Dashboard", url=get_base_url())],
    ]
    if is_tg_admin(user_id):
        rows.append([InlineKeyboardButton("👑 Bot Admin (Users Manage करें)", callback_data="tgadmin:list")])
    return InlineKeyboardMarkup(rows)


def main_menu_text(user_id: int) -> str:
    role = "👑 Admin" if is_tg_admin(user_id) else "👤 Authorized User"
    return f"🚀 <b>MiniRender Control Panel</b>\nRole: {role}\n\nनीचे दिए गए बटन से अपना सर्वर मैनेज करें 👇"


async def _render_tgadmin_list(query):
    db = SessionLocal()
    try:
        users = db.query(TelegramAllowedUser).order_by(TelegramAllowedUser.id.asc()).all()
        btns = [[InlineKeyboardButton("➕ Allow New Chat ID", callback_data="tgadmin:add")]]
        for u in users:
            if u.chat_id == TELEGRAM_ADMIN_ID:
                btns.append([InlineKeyboardButton(f"👑 {u.chat_id} ({u.label})", callback_data="tgadmin:list")])
            else:
                btns.append([InlineKeyboardButton(f"🗑️ Remove {u.chat_id} ({u.label})", callback_data=f"tgadmin:rem:{u.chat_id}")])
        btns.append([InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")])
        await safe_edit(query, f"👑 <b>Telegram Bot Access Control</b>\nकुल Allowed Users: <b>{len(users)}</b>", InlineKeyboardMarkup(btns))
    finally:
        db.close()


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid):
        await update.message.reply_text(
            f"⛔ <b>Access Denied!</b>\n\nआपकी Telegram Chat ID: <code>{uid}</code>\n"
            f"एडमिन से कहें कि यह ID Admin Panel से Allow करें।",
            parse_mode="HTML"
        )
        return
    user_state.pop(uid, None)
    await update.message.reply_text(main_menu_text(uid), reply_markup=main_menu_markup(uid), parse_mode="HTML")


async def handle_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_authorized(uid):
        await update.message.reply_text(f"⛔ Access Denied! आपकी ID: <code>{uid}</code>", parse_mode="HTML")
        return
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".zip"):
        await update.message.reply_text("❌ कृपया सिर्फ़ <code>.zip</code> फाइल भेजें।", parse_mode="HTML")
        return

    existing = user_state.get(uid, {})
    if existing.get("step") == "update_zip":
        pid = existing["project_id"]
        user_state.pop(uid, None)
        await redeploy_existing_zip(update.message, context, pid, doc.file_id, uid)
        return

    user_state[uid] = {"step": "name", "file_id": doc.file_id}
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="menu:home")]])
    await update.message.reply_text("🌐 <b>प्रोजेक्ट का नाम लिखें</b> (सिर्फ़ <code>a-z</code>, <code>0-9</code> और <code>-</code>):", reply_markup=kb, parse_mode="HTML")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in user_state:
        if is_authorized(uid):
            await update.message.reply_text(main_menu_text(uid), reply_markup=main_menu_markup(uid), parse_mode="HTML")
        return

    state = user_state[uid]
    text = update.message.text.strip()
    step = state.get("step")

    if step == "name":
        clean_name = text.lower()
        if not re.match(r"^[a-z0-9-]+$", clean_name):
            await update.message.reply_text("❌ गलत नाम! सिर्फ़ <code>a-z</code>, <code>0-9</code> और <code>-</code> लिखें।", parse_mode="HTML")
            return
        state["name"] = clean_name
        state["step"] = "type"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📄 Static Site (HTML/CSS/JS)", callback_data="newtype:static")],
            [InlineKeyboardButton("🐍 Python Web Service", callback_data="newtype:python")],
            [InlineKeyboardButton("🟢 Node.js Web Service", callback_data="newtype:node")],
            [InlineKeyboardButton("❌ Cancel", callback_data="menu:home")],
        ])
        await update.message.reply_text(f"📛 प्रोजेक्ट: <code>{clean_name}</code>\nसर्विस का टाइप चुनें:", reply_markup=kb, parse_mode="HTML")

    elif step == "custom_cmd":
        await execute_telegram_deploy(update.message, context, uid, text)

    elif step == "edit_cmd":
        pid = state["project_id"]
        user_state.pop(uid, None)
        db = SessionLocal()
        try:
            p = db.query(Project).filter(Project.id == pid).first()
            if p:
                p.start_command = text
                p.status = "Building"
                db.commit()
                start_bg_deploy(p.id)
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 प्रोजेक्ट खोलें", callback_data=f"proj:{p.id}")]])
                await update.message.reply_text(f"✅ Start Command अपडेट होकर <code>{notifier.esc(p.name)}</code> री-डिप्लॉय हो रहा है!", reply_markup=kb, parse_mode="HTML")
        finally:
            db.close()

    elif step == "add_env":
        if "=" not in text:
            await update.message.reply_text("❌ फॉर्मेट: <code>KEY=VALUE</code>", parse_mode="HTML")
            return
        k, v = text.split("=", 1)
        pid = state["project_id"]
        user_state.pop(uid, None)
        db = SessionLocal()
        try:
            p = db.query(Project).filter(Project.id == pid).first()
            if p and k.strip():
                env = p.get_env()
                env[k.strip()] = v.strip()
                p.set_env(env)
                p.status = "Building"
                db.commit()
                start_bg_deploy(p.id)
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Env Vars देखें", callback_data=f"env:{p.id}")]])
                await update.message.reply_text(f"🔑 <code>{notifier.esc(k.strip())}</code> सेव हो गया!", reply_markup=kb, parse_mode="HTML")
        finally:
            db.close()

    elif step == "tg_add_user" and is_tg_admin(uid):
        parts = text.split(None, 1)
        if not parts[0].lstrip("-").isdigit():
            await update.message.reply_text("❌ सही numeric Telegram Chat ID लिखें (जैसे: <code>123456789 Rahul</code>)", parse_mode="HTML")
            return
        new_cid = int(parts[0])
        label = parts[1] if len(parts) > 1 else "User"
        user_state.pop(uid, None)
        db = SessionLocal()
        try:
            if not db.query(TelegramAllowedUser).filter(TelegramAllowedUser.chat_id == new_cid).first():
                db.add(TelegramAllowedUser(chat_id=new_cid, label=label, added_by=f"tg:{uid}"))
                log_activity(db, f"tg:{uid}", "ALLOW_TG_USER", f"Allowed {new_cid} ({label})")
                db.commit()
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("👑 Bot Users देखें", callback_data="tgadmin:list")]])
            await update.message.reply_text(f"✅ ID <code>{new_cid}</code> ({notifier.esc(label)}) को बॉट एक्सेस दे दिया गया!", reply_markup=kb, parse_mode="HTML")
        finally:
            db.close()


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id

    if not is_authorized(uid):
        await safe_edit(query, f"⛔ Access Denied! आपकी ID: <code>{uid}</code>")
        return

    data = query.data
    parts = data.split(":")
    action = parts[0]

    if data == "menu:home":
        user_state.pop(uid, None)
        await safe_edit(query, main_menu_text(uid), main_menu_markup(uid))

    elif data == "menu:deploy":
        user_state.pop(uid, None)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu:home")]])
        await safe_edit(query, "📦 <b>नया प्रोजेक्ट Deploy करें</b>\n\nबस अपनी प्रोजेक्ट की <code>.zip</code> फाइल इसी चैट में भेजें!", kb)

    elif data == "menu:status":
        db = SessionLocal()
        try:
            all_p = db.query(Project).all()
            live = sum(1 for x in all_p if x.status == "Live")
            stopped = sum(1 for x in all_p if x.status == "Stopped")
            failed = sum(1 for x in all_p if x.status == "Failed")
            text = (
                f"📊 <b>सर्वर स्टेटस</b>\n\n• कुल प्रोजेक्ट्स: <b>{len(all_p)}</b>\n"
                f"• 🟢 Live: <b>{live}</b>\n• ⏸️ Stopped: <b>{stopped}</b>\n• 🔴 Failed: <b>{failed}</b>\n"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="menu:status"),
                 InlineKeyboardButton("🌐 Projects", callback_data="menu:projects:0")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")]
            ])
            await safe_edit(query, text, kb)
        finally:
            db.close()

    elif data.startswith("menu:projects"):
        page = int(parts[2]) if len(parts) > 2 else 0
        await show_projects_list(query, page)

    # 👑 TELEGRAM ADMIN MENU
    elif action == "tgadmin" and is_tg_admin(uid):
        sub = parts[1]
        if sub == "list":
            await _render_tgadmin_list(query)
        elif sub == "add":
            user_state[uid] = {"step": "tg_add_user"}
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="tgadmin:list")]])
            await safe_edit(query, "✍️ जिस यूज़र को बॉट एक्सेस देना है उसकी <b>Chat ID और नाम</b> लिखें:\nजैसे: <code>987654321 Rahul</code>", kb)
        elif sub == "rem":
            rem_id = int(parts[2])
            db = SessionLocal()
            try:
                db.query(TelegramAllowedUser).filter(TelegramAllowedUser.chat_id == rem_id).delete()
                log_activity(db, f"tg:{uid}", "REMOVE_TG_USER", f"Removed {rem_id}")
                db.commit()
            finally:
                db.close()
            await _render_tgadmin_list(query)

    elif action == "proj":
        await show_project_panel(query, int(parts[1]))

    elif action == "act":
        act_type, pid = parts[1], int(parts[2])
        db = SessionLocal()
        try:
            p = db.query(Project).filter(Project.id == pid).first()
            if p and act_type in ("start", "stop", "restart"):
                pm.control_process(p, act_type)
                p.status = "Stopped" if act_type == "stop" else "Live"
                log_activity(db, f"tg:{uid}", f"TG_{act_type.upper()}", p.name)
                db.commit()
                emoji = "⏸️" if act_type == "stop" else "🔄"
                await notifier.send_alert(f"{emoji} <b>Telegram {act_type.title()}:</b> <code>{notifier.esc(p.name)}</code>")
        finally:
            db.close()
        await show_project_panel(query, pid)

    elif action == "logs":
        pid = int(parts[1])
        db = SessionLocal()
        try:
            p = db.query(Project).filter(Project.id == pid).first()
            if not p:
                return
            live_buf = list(pm.LOG_BUFFERS.get(p.name, []))
            raw_logs = ("".join(live_buf[-25:]) if live_buf else (p.deploy_logs or "कोई लॉग्स नहीं।"))[-3000:]
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh Logs", callback_data=f"logs:{pid}"),
                 InlineKeyboardButton("🔙 Back", callback_data=f"proj:{pid}")]
            ])
            await safe_edit(query, f"📜 <b>Logs: {notifier.esc(p.name)}</b>\n\n<pre>{notifier.esc(raw_logs)}</pre>", kb)
        finally:
            db.close()

    elif action == "env":
        pid = int(parts[1])
        db = SessionLocal()
        try:
            p = db.query(Project).filter(Project.id == pid).first()
            if not p:
                return
            env = p.get_env()
            env_lines = "\n".join(f"• <code>{notifier.esc(k)}</code> = <code>{notifier.esc(v)}</code>" for k, v in env.items()) or "कोई Env Var नहीं है।"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add Env", callback_data=f"envadd:{pid}")],
                [InlineKeyboardButton("🗑️ Clear All", callback_data=f"envclr:{pid}")],
                [InlineKeyboardButton("🔙 Back", callback_data=f"proj:{pid}")],
            ])
            await safe_edit(query, f"🔑 <b>Environment Vars ({notifier.esc(p.name)})</b>\n\n{env_lines}", kb)
        finally:
            db.close()

    elif action == "envadd":
        pid = int(parts[1])
        user_state[uid] = {"step": "add_env", "project_id": pid}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"env:{pid}")]])
        await safe_edit(query, "✍️ नया वेरिएबल भेजें: <code>KEY=VALUE</code>", kb)

    elif action == "envclr":
        pid = int(parts[1])
        db = SessionLocal()
        try:
            p = db.query(Project).filter(Project.id == pid).first()
            if p:
                p.set_env({})
                p.status = "Building"
                db.commit()
                start_bg_deploy(p.id)
        finally:
            db.close()
        await show_project_panel(query, pid)

    elif action == "updzip":
        pid = int(parts[1])
        user_state[uid] = {"step": "update_zip", "project_id": pid}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"proj:{pid}")]])
        await safe_edit(query, "📦 इस प्रोजेक्ट को अपडेट करने के लिए नई <code>.zip</code> फाइल भेजें:", kb)

    elif action == "editcmd":
        pid = int(parts[1])
        user_state[uid] = {"step": "edit_cmd", "project_id": pid}
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"proj:{pid}")]])
        await safe_edit(query, "⚙️ नया <b>Start Command</b> लिखकर भेजें:", kb)

    elif action == "del":
        pid = int(parts[1])
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ हाँ, Delete करें", callback_data=f"delok:{pid}"),
             InlineKeyboardButton("❌ नहीं", callback_data=f"proj:{pid}")]
        ])
        await safe_edit(query, "⚠️ क्या आप वाकई इस प्रोजेक्ट को डिलीट करना चाहते हैं?", kb)

    elif action == "delok":
        pid = int(parts[1])
        db = SessionLocal()
        try:
            p = db.query(Project).filter(Project.id == pid).first()
            if p:
                pm.stop_process(p.name)
                pdir = os.path.join(pm.DEPLOYMENTS_DIR, p.name)
                if os.path.exists(pdir):
                    shutil.rmtree(pdir)
                name = p.name
                db.delete(p)
                log_activity(db, f"tg:{uid}", "TG_DELETE", name)
                db.commit()
                await notifier.send_alert(f"🗑️ <b>Deleted via Telegram:</b> <code>{notifier.esc(name)}</code>")
        finally:
            db.close()
        await show_projects_list(query, 0)

    elif action == "newtype":
        if uid not in user_state:
            await safe_edit(query, "⚠️ सेशन समाप्त। कृपया ZIP दोबारा भेजें।", main_menu_markup(uid))
            return
        choice = parts[1]
        state = user_state[uid]
        if choice == "static":
            state["service_type"], state["runtime"] = "static", "static"
            await safe_edit(query, f"⏳ <code>{state['name']}</code> डिप्लॉय हो रहा है...")
            await execute_telegram_deploy(query.message, context, uid, "")
        else:
            state["service_type"], state["runtime"], state["step"] = "web", choice, "start_cmd"
            cmds = ["python app.py", "python main.py", "uvicorn main:app --host 0.0.0.0 --port $PORT"] if choice == "python" else ["npm start", "node index.js", "node server.js"]
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"▶️ {c}", callback_data=f"cmd:{c}")] for c in cmds] +
                [[InlineKeyboardButton("✍️ खुद Command लिखें", callback_data="cmd:custom")],
                 [InlineKeyboardButton("❌ Cancel", callback_data="menu:home")]]
            )
            await safe_edit(query, "⚙️ <b>Start Command चुनें:</b>", kb)

    elif action == "cmd":
        if uid not in user_state:
            await safe_edit(query, "⚠️ सेशन समाप्त।", main_menu_markup(uid))
            return
        cmd_val = data.split(":", 1)[1]
        if cmd_val == "custom":
            user_state[uid]["step"] = "custom_cmd"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="menu:home")]])
            await safe_edit(query, "✍️ अपना Start Command लिखकर भेजें:", kb)
        else:
            await safe_edit(query, f"⏳ <code>{user_state[uid]['name']}</code> डिप्लॉय हो रहा है...")
            await execute_telegram_deploy(query.message, context, uid, cmd_val)


async def show_projects_list(query, page: int = 0):
    per_page = 15
    db = SessionLocal()
    try:
        projects = db.query(Project).order_by(Project.id.desc()).all()
        if not projects:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📦 नया प्रोजेक्ट Deploy करें", callback_data="menu:deploy")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")],
            ])
            await safe_edit(query, "📭 अभी कोई प्रोजेक्ट होस्ट नहीं है।", kb)
            return

        start = page * per_page
        chunk = projects[start:start + per_page]
        buttons = []
        for p in chunk:
            icon = "🟢" if p.status == "Live" else "🟡" if p.status == "Building" else "⏸️" if p.status == "Stopped" else "🔴"
            buttons.append([InlineKeyboardButton(f"{icon} {p.name} ({p.service_type})", callback_data=f"proj:{p.id}")])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"menu:projects:{page - 1}"))
        if start + per_page < len(projects):
            nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"menu:projects:{page + 1}"))
        if nav:
            buttons.append(nav)
        buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"menu:projects:{page}"),
                        InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")])
        await safe_edit(query, f"🌐 <b>प्रोजेक्ट चुनें (कुल {len(projects)}):</b>", InlineKeyboardMarkup(buttons))
    finally:
        db.close()


async def show_project_panel(query, pid: int):
    db = SessionLocal()
    try:
        p = db.query(Project).filter(Project.id == pid).first()
        if not p:
            await show_projects_list(query, 0)
            return
        url = build_deploy_url(p.name, p.service_type)
        icon = "🟢" if p.status == "Live" else "🟡" if p.status == "Building" else "⏸️" if p.status == "Stopped" else "🔴"
        metrics = pm.get_metrics(p.name) if p.service_type == "web" else {"cpu_percent": 0, "mem_mb": 0}
        text = (
            f"📦 <b>{notifier.esc(p.name)}</b> (by <code>{p.owner_username}</code>)\n"
            f"• Status: {icon} <b>{p.status}</b> | Type: <code>{p.service_type}</code>\n"
            f"• CPU: <code>{metrics['cpu_percent']}%</code> | RAM: <code>{metrics['mem_mb']} MB</code>\n"
            f"• 🔗 URL: {url}"
        )
        toggle_btn = InlineKeyboardButton("▶️ Start", callback_data=f"act:start:{p.id}") if p.status == "Stopped" else InlineKeyboardButton("⏸️ Stop", callback_data=f"act:stop:{p.id}")
        buttons = [
            [InlineKeyboardButton("🔗 वेबसाइट खोलें", url=url)],
            [InlineKeyboardButton("🔄 Restart", callback_data=f"act:restart:{p.id}"), toggle_btn],
            [InlineKeyboardButton("📜 Logs", callback_data=f"logs:{p.id}"), InlineKeyboardButton("🔑 Env Vars", callback_data=f"env:{p.id}")],
            [InlineKeyboardButton("📦 Update ZIP", callback_data=f"updzip:{p.id}"), InlineKeyboardButton("⚙️ Start Cmd", callback_data=f"editcmd:{p.id}")],
            [InlineKeyboardButton("🗑️ Delete", callback_data=f"del:{p.id}"), InlineKeyboardButton("🔄 Refresh", callback_data=f"proj:{p.id}")],
            [InlineKeyboardButton("🔙 All Projects", callback_data="menu:projects:0")],
        ]
        await safe_edit(query, text, InlineKeyboardMarkup(buttons))
    finally:
        db.close()


def start_bg_deploy(project_id: int):
    def bg():
        db = SessionLocal()
        try:
            proj = db.query(Project).filter(Project.id == project_id).first()
            if not proj:
                return
            cid, logs = pm.deploy_project(proj)
            proj.container_id, proj.deploy_logs, proj.status = cid, logs, "Live"
            db.commit()
            url = build_deploy_url(proj.name, proj.service_type)
            notifier.send_alert_sync(f"✅ <b>Deployed via Telegram:</b> <code>{notifier.esc(proj.name)}</code>\n🔗 {url}")
        except Exception as e:
            proj = db.query(Project).filter(Project.id == project_id).first()
            if proj:
                proj.status, proj.deploy_logs = "Failed", str(e)
                db.commit()
                notifier.send_alert_sync(f"❌ <b>Deploy Error ({notifier.esc(proj.name)})</b>\n<pre>{notifier.esc(str(e)[:1000])}</pre>")
        finally:
            db.close()
    threading.Thread(target=bg, daemon=True).start()


async def redeploy_existing_zip(message, context, project_id: int, file_id: str, uid: int):
    db = SessionLocal()
    try:
        p = db.query(Project).filter(Project.id == project_id).first()
        if not p:
            return
        project_dir = os.path.join(pm.DEPLOYMENTS_DIR, p.name)
        temp_zip = os.path.join(pm.DEPLOYMENTS_DIR, f"{p.name}_tg.zip")
        tg_file = await context.bot.get_file(file_id)
        await tg_file.download_to_drive(temp_zip)
        try:
            pm.safe_extract_zip(temp_zip, project_dir)
        finally:
            if os.path.exists(temp_zip):
                os.remove(temp_zip)
        p.status = "Building"
        log_activity(db, f"tg:{uid}", "TG_REDEPLOY", p.name)
        db.commit()
        start_bg_deploy(p.id)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📦 प्रोजेक्ट पैनल खोलें", callback_data=f"proj:{p.id}")]])
        await message.reply_text(f"🚀 <code>{notifier.esc(p.name)}</code> री-डिप्लॉय हो रहा है!", reply_markup=kb, parse_mode="HTML")
    finally:
        db.close()


async def execute_telegram_deploy(message, context, uid: int, start_cmd: str):
    state = user_state.pop(uid, None)
    if not state:
        return
    name, service_type, runtime = state["name"], state["service_type"], state["runtime"]
    project_dir = os.path.join(pm.DEPLOYMENTS_DIR, name)
    temp_zip = os.path.join(pm.DEPLOYMENTS_DIR, f"{name}_tg.zip")

    tg_file = await context.bot.get_file(state["file_id"])
    await tg_file.download_to_drive(temp_zip)
    try:
        pm.safe_extract_zip(temp_zip, project_dir)
    except Exception as e:
        await message.reply_text(f"❌ ZIP Error: {e}")
        return
    finally:
        if os.path.exists(temp_zip):
            os.remove(temp_zip)

    admin_owner = os.getenv("ADMIN_USERNAME", "admin")
    db = SessionLocal()
    try:
        p = db.query(Project).filter(Project.name == name).first()
        if not p:
            p = Project(
                owner_username=admin_owner, name=name, service_type=service_type,
                runtime=runtime, start_command=start_cmd, internal_port=8000,
                host_port=get_next_port(db), status="Building",
            )
            db.add(p)
        else:
            p.service_type, p.runtime, p.status = service_type, runtime, "Building"
            if start_cmd:
                p.start_command = start_cmd
        log_activity(db, f"tg:{uid}", "TG_DEPLOY", name)
        db.commit()
        db.refresh(p)
        pid = p.id
    finally:
        db.close()

    start_bg_deploy(pid)
    url = build_deploy_url(name, service_type)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 वेबसाइट खोलें", url=url)],
        [InlineKeyboardButton("⚙️ प्रोजेक्ट मैनेज करें", callback_data=f"proj:{pid}")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="menu:home")],
    ])
    await message.reply_text(f"🚀 <b>Deployment Started!</b>\n\n📛 Name: <code>{name}</code>\n🔗 URL: {url}", reply_markup=kb, parse_mode="HTML")


def create_bot_app():
    if not TELEGRAM_BOT_TOKEN:
        return None
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_zip))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app