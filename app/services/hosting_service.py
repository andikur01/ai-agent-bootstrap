"""Host a generated site on the bot's own public port.

The user asked for a link, not a zip file. Cloudflare and GitHub Pages are the
right answer when their tokens allow it, but on this deployment neither does:
the GitHub token is fine-grained and can push to one existing repository but
cannot create a repository, a branch, or a Pages site (all 403). So the user
built a website and received a path to a zip.

The runtime this bot already runs on publishes two ports publicly. That is a
working web server sitting unused, and it needs no token and no third party. A
generated site is copied into a served directory and handed back as a real URL.

This is deliberately the last resort, not the first: a link on a sandbox that
may be recycled is worse than a permanent GitHub Pages link. It is simply far
better than a zip file.
"""
from __future__ import annotations

import asyncio
import html
import shutil
from pathlib import Path

from aiohttp import web

from app.core.logging_setup import get_logger

log = get_logger(__name__)

DEFAULT_HOST_PORT = 12000


class SiteHost:
    """Serve a directory of generated sites over HTTP on the public port."""

    def __init__(
        self,
        sites_root: Path,
        *,
        port: int = DEFAULT_HOST_PORT,
        public_base_url: str = "",
    ):
        self.sites_root = Path(sites_root)
        self.sites_root.mkdir(parents=True, exist_ok=True)
        self.port = port
        self.public_base_url = public_base_url.rstrip("/")
        self._runner: web.AppRunner | None = None
        self._started = False

    # ------------------------------------------------------------------ serve
    async def start(self) -> bool:
        """Bind the port. Returns False when it is already taken, which is a
        normal outcome when another service owns the port - the caller should
        then report that hosting is unavailable rather than pretend."""
        if self._started:
            return True
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/s/{name}", self._serve_index)
        app.router.add_get("/s/{name}/{path:.*}", self._serve_file)
        try:
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", self.port)
            await site.start()
        except OSError as exc:
            log.warning("site host cannot bind port %s: %s", self.port, exc)
            return False
        self._runner = runner
        self._started = True
        log.info("site host listening on %s", self.port)
        return True

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._started = False

    # ---------------------------------------------------------------- publish
    def publish(self, source: Path, *, project_name: str) -> str:
        """Copy a built site into the served directory and return its URL."""
        from app.services.deploy_service import _slug

        name = _slug(project_name) or "site"
        target = self.sites_root / name
        # Re-publishing a site that is already served must not delete its own
        # source. When the build output and the served copy are the same path,
        # copying onto itself is a no-op and the rmtree below would erase it.
        try:
            if source.resolve() == target.resolve():
                return self.url_for(name)
        except OSError:
            pass
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(source, target)

        # A site whose entry point is public/index.html should be served from
        # the project root, so the URL has no /public/ in it.
        nested = target / "public"
        if nested.is_dir() and not (target / "index.html").exists():
            for item in nested.iterdir():
                shutil.move(str(item), str(target / item.name))
            nested.rmdir()

        if not (target / "index.html").exists():
            found = next(iter(sorted(target.rglob("index.html"))), None)
            if found is None:
                raise FileNotFoundError("Tidak ada index.html untuk dihosting.")
            shutil.copy(found, target / "index.html")

        return self.url_for(name)

    def url_for(self, name: str) -> str:
        if self.public_base_url:
            return f"{self.public_base_url}/s/{name}/"
        return f"/s/{name}/"

    # ----------------------------------------------------------------- routes
    async def _index(self, request: web.Request) -> web.Response:
        sites = sorted(p.name for p in self.sites_root.iterdir() if p.is_dir())
        if not sites:
            return web.Response(text="Belum ada website.", content_type="text/plain")
        items = "\n".join(
            f'<li><a href="/s/{html.escape(s)}/">{html.escape(s)}</a></li>' for s in sites
        )
        body = f"<!DOCTYPE html><html lang='id'><head><meta charset='utf-8'>"
        body += "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        body += "<title>Website kamu</title></head><body style='font-family:system-ui;"
        body += "max-width:40rem;margin:3rem auto;padding:0 1rem'>"
        body += f"<h1>Website kamu</h1><ul>{items}</ul></body></html>"
        return web.Response(text=body, content_type="text/html")

    async def _serve_index(self, request: web.Request) -> web.Response:
        return await self._serve_file(request)

    async def _serve_file(self, request: web.Request, path: str = "") -> web.Response:
        # aiohttp calls this handler with the request only, so the captured
        # ``{path:.*}`` never reached the parameter and every request for
        # /script.js or /styles.css was answered with index.html. The site
        # loaded unstyled with no JavaScript, which the QA browser correctly
        # flagged as console errors. Read the path from match_info instead.
        if not path:
            path = request.match_info.get("path", "")
        name = request.match_info.get("name", "")
        base = (self.sites_root / name).resolve()
        # A request must not escape its own site directory.
        if not str(base).startswith(str(self.sites_root.resolve())):
            raise web.HTTPNotFound()
        requested = (base / (path or "index.html")).resolve()
        if not str(requested).startswith(str(base)) or not requested.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(requested)


async def probe_public_url(base_url: str, *, timeout: float = 12.0) -> bool:
    """Confirm the public URL really answers before handing it to the user.

    The whole point of this change is to stop shipping links that do not work,
    so the link is checked from the outside first.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(base_url)
            return response.status_code < 500
    except Exception:  # noqa: BLE001
        return False
