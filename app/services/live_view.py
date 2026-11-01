"""Live view: watch and control the agent's browser from a phone.

This answers "why can Astra type by itself but my bot cannot?" - Astra runs the
browser somewhere with a screen and streams it. Nothing is stopping this bot
from doing the same; it just needed the screen to be reachable.

How it fits together:

    Xvfb (:N)  <- the browser draws here
      x11vnc   <- exports that display over VNC (localhost only)
      websockify/noVNC <- VNC to a browser page, on the public port

The noVNC page is served on the work host's second port, so the user opens one
URL on their phone and gets the agent's actual screen: they can watch every
click and take over whenever a captcha or a login appears.

Security: x11vnc listens on 127.0.0.1 only and is protected by a password
generated at start. The public side is noVNC, which needs that password before
it can control anything. The page is never exposed without a password, because
whoever reaches it controls a browser holding the user's sessions.
"""
from __future__ import annotations

import asyncio
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging_setup import get_logger

log = get_logger(__name__)

NOVNC_DIR = "/usr/share/novnc"
VNC_PORT = 5900
# 12001 is the second port the runtime publishes for this workspace, so the
# noVNC page is reachable from the user's phone without extra plumbing.
def _default_web_port() -> int:
    return int(os.environ.get("LIVE_VIEW_PORT", "12001"))


WEB_PORT = _default_web_port()


@dataclass
class LiveView:
    display: str = ""
    vnc_port: int = VNC_PORT
    web_port: int = WEB_PORT
    password: str = ""
    xvfb: object = None
    x11vnc: object = None
    websockify: object = None
    running: bool = False
    errors: list[str] = field(default_factory=list)

    def url(self, host: str) -> str:
        """The page the user opens. ``vnc.html`` gives a full-screen viewer."""
        return f"{host.rstrip('/')}/vnc.html?autoconnect=1&resize=scale"


class LiveViewService:
    """Owns the Xvfb + x11vnc + noVNC stack for one shared agent display."""

    def __init__(self, settings):
        self.settings = settings
        port = int(getattr(settings, "live_view_port", WEB_PORT) or WEB_PORT)
        self.state = LiveView(web_port=port)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------- public API
    async def ensure(self) -> LiveView:
        """Start the stack once; later calls reuse the running one."""
        async with self._lock:
            if self.state.running and self._alive():
                return self.state
            await self._start()
            return self.state

    async def stop(self) -> None:
        for proc in (self.state.websockify, self.state.x11vnc, self.state.xvfb):
            if proc is None:
                continue
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        self.state.running = False
        log.info("live view stopped")

    def _alive(self) -> bool:
        return all(
            proc is not None and getattr(proc, "returncode", None) is None
            for proc in (self.state.xvfb, self.state.x11vnc, self.state.websockify)
        )

    def viewer_url(self) -> str:
        """Public URL of the viewer page, or '' when the host is not configured."""
        host = ""
        if self.settings is not None:
            host = getattr(self.settings, "live_view_host", "") or ""
        host = host or os.environ.get("LIVE_VIEW_HOST", "")
        if not host:
            return ""
        return self.state.url(host)

    # -------------------------------------------------------------- internals
    async def _start(self) -> None:
        self.state.errors.clear()
        if not await self._which("Xvfb"):
            self.state.errors.append("Xvfb tidak ada")
        if not await self._which("x11vnc"):
            self.state.errors.append("x11vnc tidak ada (apt install x11vnc)")
        if not Path(NOVNC_DIR).exists():
            self.state.errors.append("noVNC tidak ada (apt install novnc)")
        if self.state.errors:
            log.warning("live view unavailable: %s", "; ".join(self.state.errors))
            return

        display = await self._start_xvfb()
        if not display:
            self.state.errors.append("gagal start Xvfb")
            return
        self.state.display = display
        os.environ["DISPLAY"] = display

        self.state.password = secrets.token_urlsafe(9)
        pw_file = Path("/tmp/.agent_vnc_pass")
        pw_file.write_text(self.state.password)
        pw_file.chmod(0o600)

        if not await self._start_x11vnc(pw_file):
            self.state.errors.append("gagal start x11vnc")
            return
        if not await self._start_websockify():
            self.state.errors.append("gagal start noVNC")
            return

        self.state.running = True
        log.info(
            "live view ready on %s (vnc :%s, web %s)",
            display,
            self.state.vnc_port,
            self.state.web_port,
        )

    async def _start_xvfb(self) -> str:
        for number in range(99, 130):
            socket = Path(f"/tmp/.X11-unix/X{number}")
            if socket.exists():
                continue  # a stale socket from a dead server would fail
            display = f":{number}"
            proc = await asyncio.create_subprocess_exec(
                "Xvfb",
                display,
                "-screen",
                "0",
                "1280x800x24",
                "-nolisten",
                "tcp",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            for _ in range(24):
                await asyncio.sleep(0.25)
                if socket.exists():
                    self.state.xvfb = proc
                    return display
                if proc.returncode is not None:
                    break
            if proc.returncode is None:
                proc.terminate()
        return ""

    async def _start_x11vnc(self, pw_file: Path) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "x11vnc",
            "-display",
            self.state.display,
            "-rfbport",
            str(self.state.vnc_port),
            # localhost only: the public door is noVNC, which has its own auth.
            "-localhost",
            "-rfbauth",
            str(pw_file),
            "-forever",
            "-shared",
            "-noxdamage",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        for _ in range(20):
            await asyncio.sleep(0.25)
            if proc.returncode is not None:
                return False
            if await self._port_open("127.0.0.1", self.state.vnc_port):
                self.state.x11vnc = proc
                return True
        return False

    async def _start_websockify(self) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "websockify",
            "--web",
            NOVNC_DIR,
            str(self.state.web_port),
            f"127.0.0.1:{self.state.vnc_port}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        for _ in range(24):
            await asyncio.sleep(0.25)
            if proc.returncode is not None:
                return False
            if await self._port_open("127.0.0.1", self.state.web_port):
                self.state.websockify = proc
                return True
        return False

    @staticmethod
    async def _port_open(host: str, port: int) -> bool:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=1.0
            )
            writer.close()
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    @staticmethod
    async def _which(name: str) -> bool:
        import shutil

        return shutil.which(name) is not None


def build_live_view(settings) -> LiveViewService:
    return LiveViewService(settings)
