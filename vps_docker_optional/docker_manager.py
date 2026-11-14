import os
import shutil
import zipfile
import docker
from docker.errors import NotFound, APIError

client = docker.from_env()
BASE_DOMAIN = os.getenv("BASE_DOMAIN", "")
DEPLOYMENTS_DIR = os.path.abspath("deployments")
os.makedirs(DEPLOYMENTS_DIR, exist_ok=True)


def safe_extract_zip(zip_path: str, target_dir: str):
    """Zip-Slip अटैक से बचाते हुए ZIP एक्सट्रैक्ट करता है और नेस्टेड फोल्डर ठीक करता है"""
    if os.path.exists(target_dir):
        shutil.rmtree(target_dir)
    os.makedirs(target_dir, exist_ok=True)
    real_target = os.path.realpath(target_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            member_path = os.path.realpath(os.path.join(real_target, member.filename))
            if not member_path.startswith(real_target + os.sep) and member_path != real_target:
                raise ValueError("Unsafe ZIP file detected (Path Traversal blocked)!")
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


def generate_dockerfile(project_dir: str, runtime: str, build_cmd: str, start_cmd: str, internal_port: int):
    """अगर प्रोजेक्ट में Dockerfile नहीं है, तो ऑटोमैटिक Dockerfile बनाता है"""
    dockerfile_path = os.path.join(project_dir, "Dockerfile")
    if os.path.exists(dockerfile_path):
        return
    if runtime == "python":
        if not build_cmd and os.path.exists(os.path.join(project_dir, "requirements.txt")):
            build_cmd = "pip install --no-cache-dir -r requirements.txt"
        run_line = f"RUN {build_cmd}" if build_cmd else ""
        cmd_exec = start_cmd or "python app.py"
        content = f"""FROM python:3.11-slim
WORKDIR /app
COPY . .
{run_line}
EXPOSE {internal_port}
CMD ["sh", "-c", "{cmd_exec}"]
"""
    elif runtime == "node":
        if not build_cmd and os.path.exists(os.path.join(project_dir, "package.json")):
            build_cmd = "npm install"
        run_line = f"RUN {build_cmd}" if build_cmd else ""
        cmd_exec = start_cmd or "npm start"
        content = f"""FROM node:20-alpine
WORKDIR /app
COPY . .
{run_line}
EXPOSE {internal_port}
CMD ["sh", "-c", "{cmd_exec}"]
"""
    else:
        raise ValueError(f"Unsupported runtime for Dockerfile: {runtime}")
    with open(dockerfile_path, "w") as f:
        f.write(content)


def remove_container_if_exists(name: str):
    container_name = f"paas_{name}"
    try:
        old = client.containers.get(container_name)
        old.stop(timeout=5)
        old.remove(force=True)
    except NotFound:
        pass


def get_traefik_labels(project_name: str, internal_port: int) -> dict:
    if not BASE_DOMAIN:
        return {}
    return {
        "traefik.enable": "true",
        f"traefik.http.routers.{project_name}.rule": f"Host(`{project_name}.{BASE_DOMAIN}`)",
        f"traefik.http.routers.{project_name}.entrypoints": "websecure",
        f"traefik.http.routers.{project_name}.tls.certresolver": "myresolver",
        f"traefik.http.services.{project_name}.loadbalancer.server.port": str(internal_port),
    }


def deploy_project(project) -> tuple[str, str]:
    """प्रोजेक्ट को बिल्ड और स्टार्ट करता है। (container_id, logs) लौटाता है"""
    project_dir = os.path.join(DEPLOYMENTS_DIR, project.name)
    logs = []
    remove_container_if_exists(project.name)
    container_name = f"paas_{project.name}"
    env_vars = project.get_env()
    if project.service_type == "static":
        logs.append("==> Deploying Static Site via Nginx Alpine...")
        if not os.path.exists(os.path.join(project_dir, "index.html")):
            for f in os.listdir(project_dir):
                if f.endswith(".html"):
                    shutil.copy(os.path.join(project_dir, f), os.path.join(project_dir, "index.html"))
                    logs.append(f"==> Mapped {f} as index.html")
                    break
        labels = get_traefik_labels(project.name, 80)
        container = client.containers.run("nginx:alpine", name=container_name, detach=True,
            volumes={project_dir: {"bind": "/usr/share/nginx/html", "mode": "ro"}},
            ports={"80/tcp": project.host_port}, labels=labels, mem_limit="128m",
            restart_policy={"Name": "unless-stopped"})
        logs.append("==> Static Site is Live!")
        return container.id, "\n".join(logs)
    logs.append(f"==> Preparing {project.runtime} environment...")
    if project.runtime in ("python", "node"):
        generate_dockerfile(project_dir, project.runtime, project.build_command,
                            project.start_command, project.internal_port)
    image_tag = f"paas_img_{project.name}:latest"
    logs.append(f"==> Building Docker image ({image_tag})...")
    image, build_logs = client.images.build(path=project_dir, tag=image_tag, rm=True)
    for chunk in build_logs:
        if "stream" in chunk:
            line = chunk["stream"].strip()
            if line:
                logs.append(line)
    env_vars.setdefault("PORT", str(project.internal_port))
    labels = get_traefik_labels(project.name, project.internal_port)
    logs.append(f"==> Starting Web Service on internal port {project.internal_port}...")
    container = client.containers.run(image_tag, name=container_name, detach=True,
        environment=env_vars, ports={f"{project.internal_port}/tcp": project.host_port},
        labels=labels, mem_limit="512m", nano_cpus=500_000_000,
        restart_policy={"Name": "unless-stopped"})
    logs.append("==> Your service is Live!")
    return container.id, "\n".join(logs)


def control_container(project_name: str, action: str):
    container_name = f"paas_{project_name}"
    c = client.containers.get(container_name)
    if action == "stop":
        c.stop(timeout=5)
    elif action == "restart":
        c.restart(timeout=5)
    elif action == "start":
        c.start()
