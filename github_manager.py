import os
import shutil
import httpx
import process_manager as pm

GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")


def gh_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


async def exchange_code_for_token(code: str, redirect_uri: str) -> str:
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post("https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={"client_id": GITHUB_CLIENT_ID, "client_secret": GITHUB_CLIENT_SECRET,
                  "code": code, "redirect_uri": redirect_uri})
        data = resp.json()
        if "access_token" not in data:
            raise ValueError(data.get("error_description", "GitHub OAuth failed"))
        return data["access_token"]


async def get_github_user(token: str) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get("https://api.github.com/user", headers=gh_headers(token))
        resp.raise_for_status()
        return resp.json()


async def fetch_all_repos(token: str) -> list[dict]:
    repos, page = [], 1
    async with httpx.AsyncClient(timeout=25.0) as client:
        while True:
            resp = await client.get("https://api.github.com/user/repos", headers=gh_headers(token),
                params={"per_page": 100, "page": page, "sort": "updated",
                        "affiliation": "owner,collaborator,organization_member"})
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            repos.extend({"full_name": r["full_name"], "default_branch": r.get("default_branch", "main"),
                          "private": r.get("private", False)} for r in batch)
            if len(batch) < 100:
                break
            page += 1
    return repos


async def fetch_branches(token: str, full_name: str) -> list[str]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"https://api.github.com/repos/{full_name}/branches",
            headers=gh_headers(token), params={"per_page": 100})
        resp.raise_for_status()
        return [b["name"] for b in resp.json()]


async def fetch_contents(token: str, full_name: str, branch: str, path: str = "") -> list[dict]:
    clean_path = path.strip("/")
    url = f"https://api.github.com/repos/{full_name}/contents/{clean_path}" if clean_path else f"https://api.github.com/repos/{full_name}/contents"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=gh_headers(token), params={"ref": branch})
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = [data]
        items = [{"name": i["name"], "path": i["path"], "type": i["type"]} for i in data]
        items.sort(key=lambda x: (0 if x["type"] == "dir" else 1, x["name"].lower()))
        return items


def download_repo_sync(token: str, full_name: str, branch: str, target_dir: str, root_path: str = ""):
    temp_zip, temp_extract = target_dir + "_gh.zip", target_dir + "_gh_extract"
    url = f"https://api.github.com/repos/{full_name}/zipball/{branch}"
    with httpx.Client(follow_redirects=True, timeout=120.0) as client:
        with client.stream("GET", url, headers=gh_headers(token)) as resp:
            resp.raise_for_status()
            with open(temp_zip, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
    try:
        pm.safe_extract_zip(temp_zip, temp_extract)
        if os.path.exists(target_dir):
            shutil.rmtree(target_dir)
        clean_root = (root_path or "").strip("/")
        if clean_root:
            selected = os.path.join(temp_extract, clean_root)
            if os.path.isdir(selected):
                shutil.move(selected, target_dir)
            elif os.path.isfile(selected):
                shutil.move(os.path.dirname(selected), target_dir)
                fname = os.path.basename(selected)
                if fname.endswith(".html") and fname != "index.html":
                    shutil.copy(os.path.join(target_dir, fname), os.path.join(target_dir, "index.html"))
            else:
                shutil.move(temp_extract, target_dir)
        else:
            shutil.move(temp_extract, target_dir)
    finally:
        if os.path.exists(temp_zip):
            os.remove(temp_zip)
        if os.path.exists(temp_extract):
            shutil.rmtree(temp_extract, ignore_errors=True)


async def create_webhook(token: str, full_name: str, webhook_url: str, secret: str):
    if not webhook_url.startswith("https://"):
        return
    async with httpx.AsyncClient(timeout=15.0) as client:
        existing = await client.get(f"https://api.github.com/repos/{full_name}/hooks", headers=gh_headers(token))
        if existing.status_code == 200:
            for hook in existing.json():
                if hook.get("config", {}).get("url") == webhook_url:
                    return
        await client.post(f"https://api.github.com/repos/{full_name}/hooks", headers=gh_headers(token),
            json={"name": "web", "active": True, "events": ["push"],
                  "config": {"url": webhook_url, "content_type": "json", "secret": secret}})
