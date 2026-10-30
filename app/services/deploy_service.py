"""Deployment: static sites to Cloudflare Pages / GitHub Pages, apps to Render.

Order matters and is deliberate:

* Static: Cloudflare Pages first (direct upload, no git needed, instant URL),
  then GitHub Pages (commit the built site to a ``gh-pages`` branch).
* Fullstack: Render (Docker or native Python) with the Supabase env vars the
  builder declared, then a local docker-compose fallback so the user can still
  get a working app when no cloud token is configured.

Every method returns a ``Deployment`` with the live URL, and nothing here ever
prints a secret: env vars are passed through, never echoed.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.core.errors import MissingSecretError, ToolError
from app.core.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class Deployment:
    url: str
    provider: str
    project: str
    kind: str
    details: dict = field(default_factory=dict)

    def message(self) -> str:
        lines = [f"Deploy selesai via *{self.provider}*", f"URL: {self.url}"]
        if self.details.get("dashboard"):
            lines.append(f"Dashboard: {self.details['dashboard']}")
        if self.details.get("note"):
            lines.append(str(self.details["note"]))
        return "\n".join(lines)


class DeployService:
    def __init__(self, settings):
        self.settings = settings

    async def deploy_static(self, root: Path, *, project_name: str) -> Deployment:
        errors: list[str] = []
        for provider, fn in (
            ("cloudflare-pages", self._cloudflare_pages),
            ("github-pages", self._github_pages),
        ):
            try:
                return await fn(Path(root), project_name)
            except MissingSecretError as exc:
                errors.append(f"{provider}: butuh {exc.name}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{provider}: {str(exc)[:200]}")
                log.warning("deploy via %s failed: %s", provider, exc)
        raise ToolError(
            "Deploy statis gagal. " + " | ".join(errors),
            detail="Isi CLOUDFLARE_API_TOKEN+CLOUDFLARE_ACCOUNT_ID atau GITHUB_TOKEN+GITHUB_OWNER.",
        )

    async def deploy_fullstack(
        self, root: Path, *, project_name: str, env_vars: dict[str, str]
    ) -> Deployment:
        errors: list[str] = []
        try:
            return await self._render(Path(root), project_name, env_vars)
        except MissingSecretError as exc:
            errors.append(f"render: butuh {exc.name}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"render: {str(exc)[:200]}")
            log.warning("render deploy failed: %s", exc)
        raise ToolError(
            "Deploy fullstack gagal. " + " | ".join(errors),
            detail="Isi RENDER_API_KEY + RENDER_OWNER_ID, atau jalankan docker-compose lokal.",
        )

    # ------------------------------------------------------- cloudflare pages
    async def _cloudflare_pages(self, root: Path, project_name: str) -> Deployment:
        if not self.settings.cloudflare_api_token or not self.settings.cloudflare_account_id:
            raise MissingSecretError("CLOUDFLARE_API_TOKEN")

        project = _slug(project_name)
        headers = {
            "Authorization": f"Bearer {self.settings.cloudflare_api_token}",
            "Content-Type": "application/json",
        }
        base = (
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{self.settings.cloudflare_account_id}/pages/projects"
        )
        async with httpx.AsyncClient(timeout=300) as client:
            # create the project if it does not exist yet
            create = await client.post(
                base,
                headers=headers,
                json={"name": project, "production_branch": "main"},
            )
            if create.status_code not in (200, 201) and "already exists" not in create.text:
                if create.status_code not in (409, 400):
                    raise ToolError(f"Cloudflare project gagal dibuat: {create.text[:200]}")

            manifest = _hash_manifest(root)
            body = {
                "manifest": json.dumps(manifest),
                "branch": "main",
            }
            files: dict[str, tuple[str, bytes, str]] = {
                "manifest": ("manifest.json", json.dumps(manifest).encode(), "application/json"),
            }
            for path, digest in manifest.items():
                full = root / path
                files[digest] = (path, full.read_bytes(), _content_type(full))
            response = await client.post(
                f"{base}/{project}/deployments",
                headers={"Authorization": headers["Authorization"]},
                data=body,
                files=files,
            )
            if response.status_code >= 400:
                raise ToolError(f"Cloudflare deploy gagal: {response.text[:300]}")
            data = response.json()

        result = (data.get("result") or {})
        url = result.get("url") or f"https://{project}.pages.dev"
        return Deployment(
            url=url,
            provider="cloudflare-pages",
            project=project,
            kind="static",
            details={"dashboard": f"https://dash.cloudflare.com/pages/view/{project}"},
        )

    # ---------------------------------------------------------- github pages
    async def _github_pages(self, root: Path, project_name: str) -> Deployment:
        if not self.settings.github_token:
            raise MissingSecretError("GITHUB_TOKEN")
        owner = self.settings.github_owner
        if not owner:
            raise MissingSecretError("GITHUB_OWNER")

        repo = _slug(project_name)
        api = "https://api.github.com"
        headers = {
            "Authorization": f"Bearer {self.settings.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        async with httpx.AsyncClient(timeout=120) as client:
            check = await client.get(f"{api}/repos/{owner}/{repo}", headers=headers)
            if check.status_code == 404:
                create = await client.post(
                    f"{api}/user/repos",
                    headers=headers,
                    json={
                        "name": repo,
                        "description": "Generated by personal AI agent",
                        "private": False,
                        "auto_init": True,
                    },
                )
                if create.status_code >= 400:
                    raise ToolError(f"Repo gagal dibuat: {create.text[:200]}")
            elif check.status_code >= 400:
                raise ToolError(f"Tidak bisa cek repo: {check.text[:200]}")

            # upload every file to the gh-pages branch
            for path in sorted(root.rglob("*")):
                if not path.is_file() or _skip(path):
                    continue
                relative = path.relative_to(root).as_posix()
                content_b64 = base64.b64encode(path.read_bytes()).decode()
                url = f"{api}/repos/{owner}/{repo}/contents/{relative}"
                existing = await client.get(url, headers=headers, params={"ref": "gh-pages"})
                payload = {
                    "message": f"deploy: {relative}",
                    "content": content_b64,
                    "branch": "gh-pages",
                }
                if existing.status_code == 200:
                    payload["sha"] = existing.json().get("sha")
                put = await client.put(url, headers=headers, json=payload)
                if put.status_code >= 400:
                    raise ToolError(f"Upload {relative} gagal: {put.text[:200]}")

            # turn on Pages if it is not enabled
            await client.post(
                f"{api}/repos/{owner}/{repo}/pages",
                headers=headers,
                json={"source": {"branch": "gh-pages", "path": "/"}},
            )

        url = f"https://{owner}.github.io/{repo}/"
        return Deployment(
            url=url,
            provider="github-pages",
            project=repo,
            kind="static",
            details={
                "dashboard": f"https://github.com/{owner}/{repo}",
                "note": "Pages butuh 1-2 menit untuk live pertama kali.",
            },
        )

    # ------------------------------------------------------------------ render
    async def _render(self, root: Path, project_name: str, env_vars: dict[str, str]) -> Deployment:
        if not self.settings.render_api_key or not self.settings.render_owner_id:
            raise MissingSecretError("RENDER_API_KEY")

        name = _slug(project_name)
        headers = {
            "Authorization": f"Bearer {self.settings.render_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        # Render needs the code somewhere it can clone from. We publish it to a
        # GitHub repo first, then point Render at it.
        repo_url = await self._publish_to_github(root, name)

        payload = {
            "type": "web_service",
            "name": name,
            "ownerId": self.settings.render_owner_id,
            "repo": repo_url,
            "branch": "main",
            "autoDeploy": "yes",
            "serviceDetails": {
                "env": "docker" if (root / "Dockerfile").exists() else "python",
                "plan": "free",
                "region": "singapore",
                "healthCheckPath": "/api/health",
                "envVars": [
                    {"key": key, "value": value}
                    for key, value in env_vars.items()
                    if key and value
                ],
            },
        }
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                "https://api.render.com/v1/services", headers=headers, json=payload
            )
            if response.status_code >= 400:
                raise ToolError(f"Render gagal membuat service: {response.text[:300]}")
            data = response.json()

        service = data.get("service") or data
        url = service.get("serviceDetails", {}).get("url") or f"https://{name}.onrender.com"
        return Deployment(
            url=url,
            provider="render",
            project=name,
            kind="fullstack",
            details={
                "dashboard": f"https://dashboard.render.com/web/{service.get('id', '')}",
                "repo": repo_url,
                "note": "Build pertama bisa 3-5 menit.",
            },
        )

    async def _publish_to_github(self, root: Path, name: str) -> str:
        if not self.settings.github_token or not self.settings.github_owner:
            raise MissingSecretError("GITHUB_TOKEN")
        owner = self.settings.github_owner
        repo = f"{name}-app"
        api = "https://api.github.com"
        headers = {
            "Authorization": f"Bearer {self.settings.github_token}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient(timeout=180) as client:
            check = await client.get(f"{api}/repos/{owner}/{repo}", headers=headers)
            if check.status_code == 404:
                create = await client.post(
                    f"{api}/user/repos",
                    headers=headers,
                    json={"name": repo, "private": True, "auto_init": True},
                )
                if create.status_code >= 400:
                    raise ToolError(f"Repo gagal dibuat: {create.text[:200]}")
                await asyncio.sleep(2)

            for path in sorted(root.rglob("*")):
                if not path.is_file() or _skip(path):
                    continue
                relative = path.relative_to(root).as_posix()
                url = f"{api}/repos/{owner}/{repo}/contents/{relative}"
                existing = await client.get(url, headers=headers)
                payload = {
                    "message": f"deploy: {relative}",
                    "content": base64.b64encode(path.read_bytes()).decode(),
                }
                if existing.status_code == 200:
                    payload["sha"] = existing.json().get("sha")
                put = await client.put(url, headers=headers, json=payload)
                if put.status_code >= 400:
                    raise ToolError(f"Upload {relative} gagal: {put.text[:200]}")
        return f"https://github.com/{owner}/{repo}"


def _hash_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or _skip(path):
            continue
        relative = path.relative_to(root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:32]
        manifest[relative] = digest
    return manifest


def _skip(path: Path) -> bool:
    parts = set(path.parts)
    return bool(parts & {"node_modules", ".git", "__pycache__", ".venv", "venv", ".next"})


def _content_type(path: Path) -> str:
    mapping = {
        ".html": "text/html",
        ".css": "text/css",
        ".js": "application/javascript",
        ".json": "application/json",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".ico": "image/x-icon",
        ".txt": "text/plain",
        ".xml": "application/xml",
    }
    return mapping.get(path.suffix.lower(), "application/octet-stream")


def _slug(text: str, limit: int = 40) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (cleaned[:limit] or "agent-site").strip("-")


def zip_project(root: Path, target: Path | None = None) -> Path:
    """Bundle a project for delivery when deployment is not configured."""
    root = Path(root)
    target = target or root.with_suffix(".zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not _skip(path):
                archive.write(path, path.relative_to(root).as_posix())
    return target
