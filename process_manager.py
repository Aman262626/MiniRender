import os
import signal
import shutil
import zipfile
import subprocess
import threading
from collections import deque
import psutil
import notifier

DEPLOYMENTS_DIR = os.path.abspath("deployments")
os.makedirs(DEPLOYMENTS_DIR, exist_ok=True)

RUNNING_PROCS: dict[str, subprocess.Popen] = {}
LOG_BUFFERS: dict[str, deque] = {}


def safe_extract_zip(zip_path: str, target_dir: str):
    if os.path.exists(target_dir):
        shutil.rmtree(target_dir)
    os.makedirs(target_dir, exist_ok=True)
    real_target = os.path.realpath(target_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            member_path = os.path.realpath(os.path.join(real_target, member.filename))
            if not member_path.startswith(real_target + os.sep) and member_path != real_target:
                raise ValueError("Unsafe ZIP file detected!")
        zf.extractall(real_target)
    while True:
        items = [i for i in os.listdir(real_target) if i != "__MACOSX"]
        if len(items) == 1 and os.path.isdir(os.path.join(real_target, items[0])):
            inner = os.path.join(real_target, items[0])
            for f in os.listdir(inner):
                shutil.move(os.path.join(inner, f), os.path.join(real_target, f))
            shutil.rmtree(inner)
        else:
            break


def stop_process(name: str):
    proc = RUNNING_PROCS.pop(name, None)
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=5)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass


def _reader_thread(name: str, proc: subprocess.Popen):
    buf = LOG_BUFFERS.setdefault(name, deque(maxlen=400))
    try:
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            buf.append(line)
    except Exception:
        pass
    code = proc.poll()
    if name in RUNNING_PROCS and code is not None and code != 0:
        RUNNING_PROCS.pop(name, None)
        last_err = "".join(list(buf)[-8:])
        notifier.send_alert_sync(
            f"💥 <b>Service Crashed: {notifier.esc(name)}</b>\n"
            f"Exit Code: <code>{code}</code>\n<pre>{notifier.esc(last_err[:800])}</pre>"
        )


def deploy_project(project) -> tuple[str, str]:
    project_dir = os.path.join(DEPLOYMENTS_DIR, project.name)
    os.makedirs(project_dir, exist_ok=True)
    logs = []
    stop_process(project.name)
    if project.service_type == "static":
        logs.append("==> Deploying Static Site...")
        if not os.path.exists(os.path.join(project_dir, "index.html")):
            for f in os.listdir(project_dir):
                if f.endswith(".html"):
                    shutil.copy(os.path.join(project_dir, f), os.path.join(project_dir, "index.html"))
                    logs.append(f"==> Mapped {f} -> index.html")
                    break
        logs.append("==> Static Site is Live!")
        return "static", "\n".join(logs)
    env = os.environ.copy()
    env.update(project.get_env())
    env["PORT"] = str(project.host_port)
    build_cmd = (project.build_command or "").strip()
    if not build_cmd:
        if project.runtime == "python" and os.path.exists(os.path.join(project_dir, "requirements.txt")):
            build_cmd = "pip install -r requirements.txt"
        elif project.runtime == "node" and os.path.exists(os.path.join(project_dir, "package.json")):
            build_cmd = "npm install"
    if build_cmd:
        logs.append(f"==> Running build: {build_cmd}")
        res = subprocess.run(build_cmd, cwd=project_dir, shell=True, env=env,
            capture_output=True, text=True, timeout=300)
        if res.stdout:
            logs.append(res.stdout)
        if res.stderr:
            logs.append(res.stderr)
        if res.returncode != 0:
            raise RuntimeError(f"Build failed (code {res.returncode}):\n{res.stderr or res.stdout}")
    start_cmd = (project.start_command or "").strip()
    if not start_cmd:
        if project.runtime == "python":
            if os.path.exists(os.path.join(project_dir, "app.py")):
                start_cmd = "python app.py"
            elif os.path.exists(os.path.join(project_dir, "main.py")):
                start_cmd = "python main.py"
            else:
                start_cmd = "python app.py"
        else:
            start_cmd = "npm start"
    logs.append(f"==> Starting Web Service: {start_cmd} (internal port {project.host_port})")
    LOG_BUFFERS[project.name] = deque(maxlen=400)
    proc = subprocess.Popen(start_cmd, cwd=project_dir, shell=True, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, preexec_fn=os.setsid)
    RUNNING_PROCS[project.name] = proc
    threading.Thread(target=_reader_thread, args=(project.name, proc), daemon=True).start()
    logs.append("==> Web Service is Live!")
    return str(proc.pid), "\n".join(logs)


def control_process(project, action: str):
    if action == "stop":
        stop_process(project.name)
    elif action in ("start", "restart") and project.service_type == "web":
        deploy_project(project)


def get_metrics(name: str) -> dict:
    proc = RUNNING_PROCS.get(name)
    if not proc or proc.poll() is not None:
        return {"running": False, "cpu_percent": 0.0, "mem_mb": 0.0, "mem_limit_mb": 512.0}
    try:
        p = psutil.Process(proc.pid)
        procs = [p] + p.children(recursive=True)
        cpu = sum(pr.cpu_percent(interval=0.1) for pr in procs)
        mem = sum(pr.memory_info().rss for pr in procs) / (1024 * 1024)
        return {"running": True, "cpu_percent": round(cpu, 2), "mem_mb": round(mem, 2), "mem_limit_mb": 512.0}
    except Exception:
        return {"running": False, "cpu_percent": 0.0, "mem_mb": 0.0, "mem_limit_mb": 512.0}
