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
import re
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


def _live_view_owner(env: bytes) -> int | None:
    """Owner PID recorded in a process's AGENT_LIVE_VIEW tag, or None."""
    for item in env.split(b"\0"):
        if item.startswith(b"AGENT_LIVE_VIEW="):
            raw = item.split(b"=", 1)[1]
            try:
                return int(raw)
            except ValueError:
                return None
    return None


def _pid_alive(pid: int) -> bool:
    """Is the process still running? A zombie counts as dead.

    This matters: the watchdog kills the bot, but its children keep running and
    the dead parent lingers as a zombie. os.kill(pid, 0) succeeds for a zombie,
    so treating that as alive made the sweep skip exactly the leftovers it was
    written to clear, and the next start failed on the port they still held.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    # state is the field after the (comm) group, which may contain spaces.
    try:
        state = stat[stat.rindex(")") + 2]
    except (ValueError, IndexError):
        return False
    return state != "Z"


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
            # A previous bot process may still hold :5900/:12001 - the watchdog
            # kills the parent, but these children are not always reaped, and a
            # stale x11vnc then makes the whole restart fail with "gagal start
            # x11vnc". Clear our own leftovers before binding, so a restart
            # recovers instead of degrading into no screen at all.
            await self._reap_stale()
            await self._start()
            return self.state

    async def _reap_stale(self) -> None:
        """Kill leftover Xvfb/x11vnc/websockify from a previous run of this bot.

        Only processes this service could have started are touched: they are
        matched on the exact display/ports it owns, never by bare name, so an
        unrelated X server on the host is left alone.
        """
        for proc in (self.state.websockify, self.state.x11vnc, self.state.xvfb):
            if proc is None or getattr(proc, "returncode", None) is not None:
                continue
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self.state.websockify = self.state.x11vnc = self.state.xvfb = None

        # Every helper is launched with AGENT_LIVE_VIEW=<pid>, so a leftover from a
        # previous bot run is identifiable by its own environment rather than by
        # name. That keeps the sweep exact: no unrelated X server on the host is
        # ever touched.
        for pid in self._tagged_pids():
            try:
                os.kill(pid, 15)
            except (ProcessLookupError, PermissionError):
                pass
        await asyncio.sleep(0.4)
        for pid in self._tagged_pids():
            try:
                os.kill(pid, 9)
            except (ProcessLookupError, PermissionError):
                pass

        # Fall back to the ports, in case a helper was started before tagging.
        for port in (self.state.vnc_port, self.state.web_port):
            for pid in await self._pids_on_port(port):
                if await self._is_our_tool(pid):
                    try:
                        os.kill(pid, 9)
                    except (ProcessLookupError, PermissionError):
                        pass
        await asyncio.sleep(0.3)

    def _tagged_pids(self) -> list[int]:
        """Helpers this process owns, or whose owner is already gone.

        Returns PIDs safe to kill: ours, plus orphans left by a bot that died.
        A helper still owned by a running process is never touched.
        """
        me = os.getpid()
        found = []
        try:
            entries = list(Path("/proc").iterdir())
        except OSError:
            return found
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                env = (entry / "environ").read_bytes()
            except OSError:
                continue
            owner = _live_view_owner(env)
            if owner is None:
                continue
            if owner == me or not _pid_alive(owner):
                found.append(int(entry.name))
        return found

    async def _pids_on_port(self, port: int) -> list[int]:
        """PIDs listening on a local port, read from /proc.

        ``ss`` is not installed in this image, so shelling out to it silently
        returned nothing and the port sweep never ran. The kernel exposes the
        same data in /proc/net/tcp: a listening socket is state 0A, and its
        inode can be matched against the socket links in each process's fds.
        """
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, self._port_owners_sync, port), timeout=6
            )
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _port_owners_sync(port: int) -> list[int]:
        inodes = set()
        for table in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                lines = Path(table).read_text().splitlines()[1:]
            except OSError:
                continue
            for line in lines:
                parts = line.split()
                if len(parts) < 10:
                    continue
                try:
                    local_port = int(parts[1].split(":")[1], 16)
                except (IndexError, ValueError):
                    continue
                if local_port == port and parts[3] == "0A":  # 0A = LISTEN
                    inodes.add(parts[9])

        if not inodes:
            return []
        pids = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fds = list((entry / "fd").iterdir())
            except OSError:
                continue
            for fd in fds:
                try:
                    target = os.readlink(fd)
                except OSError:
                    continue
                if target.startswith("socket:[") and target[8:-1] in inodes:
                    pids.append(int(entry.name))
                    break
        return pids

    async def _is_our_tool(self, pid: int) -> bool:
        """Is this PID one of the live-screen tools, rather than a stranger?"""
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="ignore")
        except OSError:
            return False
        return any(tool in cmdline for tool in ("x11vnc", "websockify", "Xvfb"))

    async def stop(self) -> None:
        for proc in (self.state.websockify, self.state.x11vnc, self.state.xvfb):
            if proc is None:
                continue
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        # Terminate can miss a child that ignored the signal, and a leftover
        # helper is what breaks the *next* start. Sweep by tag so a clean stop
        # really leaves nothing behind.
        await asyncio.sleep(0.3)
        for pid in self._tagged_pids():
            try:
                os.kill(pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
        self.state.websockify = self.state.x11vnc = self.state.xvfb = None
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
    async def missing_tools(self) -> list[str]:
        """Which live-screen tools are absent, empty when all are present.

        The feed has two halves that fail differently: the in-chat GIF feed
        needs only screenshots, while /live needs Xvfb + x11vnc + noVNC. A
        stored "on" preference said nothing about whether the tools existed, so
        the user was told the screen was on and then saw nothing. Report the
        capability, not the preference.
        """
        missing = []
        if not await self._which("Xvfb"):
            missing.append("Xvfb")
        if not await self._which("x11vnc"):
            missing.append("x11vnc")
        if not Path(NOVNC_DIR).exists():
            missing.append("noVNC")
        return missing

    async def _start(self) -> None:
        self.state.errors.clear()
        missing = await self.missing_tools()
        if missing:
            self.state.errors.extend(f"{name} tidak ada" for name in missing)
            log.warning("live view unavailable: %s", "; ".join(self.state.errors))
            return

        display = await self._start_xvfb()
        if not display:
            self.state.errors.append("gagal start Xvfb")
            return
        self.state.display = display
        os.environ["DISPLAY"] = display

        self.state.password = self._resolve_password()
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

    def _resolve_password(self) -> str:
        """Reuse the saved VNC password so the user only copies it once.

        A fresh password on every restart meant the user had to re-read it from
        Telegram each time they restarted the bot - which is most of the point
        of having a screen. The value lives in a gitignored file under the data
        directory, and is still random when first generated.
        """
        env_password = os.environ.get("LIVE_VIEW_PASSWORD", "").strip()
        if env_password:
            return env_password
        stored = Path(self._password_file())
        try:
            if stored.exists():
                saved = stored.read_text().strip()
                if saved:
                    return saved
        except OSError:
            pass
        password = secrets.token_urlsafe(9)
        try:
            stored.parent.mkdir(parents=True, exist_ok=True)
            stored.write_text(password)
            stored.chmod(0o600)
        except OSError:
            pass
        return password

    def _password_file(self) -> str:
        data_dir = ""
        if self.settings is not None:
            data_dir = getattr(self.settings, "data_dir", "") or ""
        data_dir = data_dir or os.environ.get("DATA_DIR", "data")
        return str(Path(data_dir) / "live_view_password")

    async def _start_xvfb(self) -> str:
        # Only a handful of displays are tried, each with a short budget. The
        # old loop could spend minutes walking :99..:129, and a start that takes
        # minutes is indistinguishable from a hang for the user waiting on it.
        for number in range(99, 106):
            display = f":{number}"
            # A leftover socket file does not mean the display is alive. The old
            # code skipped any display with a socket and then picked one whose
            # server was dead, so x11vnc failed with "XOpenDisplay failed" and
            # the whole live screen was reported as broken. Probe the display
            # instead: skip the ones that really work, reuse the ones that do not.
            if await self._display_live(display):
                continue
            self._clear_stale_display(number)
            proc = await asyncio.create_subprocess_exec(
                "Xvfb",
                display,
                "-screen",
                "0",
                "1280x800x24",
                "-nolisten",
                "tcp",
                env=self._child_env(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            for _ in range(12):
                await asyncio.sleep(0.25)
                if await self._display_live(display):
                    self.state.xvfb = proc
                    return display
                if proc.returncode is not None:
                    break
            if proc.returncode is None:
                proc.terminate()
        return ""

    async def _display_live(self, display: str) -> bool:
        """Is an X server actually answering on this display?"""
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "xdpyinfo", "-display", display,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=4)
            # Check returncode, not the result of communicate(): with DEVNULL
            # that tuple is (None, None), so comparing it to 0 was always False
            # and every display looked dead - which broke the whole live view.
            return proc.returncode == 0
        except Exception:  # noqa: BLE001
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            return False

    def _clear_stale_display(self, number: int) -> None:
        """Remove the socket and lock of a display we just proved is dead."""
        for path in (f"/tmp/.X11-unix/X{number}", f"/tmp/.X{number}-lock"):
            try:
                os.unlink(path)
            except OSError:
                pass

    @staticmethod
    def _child_env() -> dict:
        """Environment for our helper processes, tagged with our own PID.

        The PID in the tag is what makes the sweep safe: helpers from a previous
        bot run are identified as orphans (their owner is gone) and cleared,
        while helpers belonging to a *live* bot - including one running beside
        this process, such as a test - are left alone.
        """
        env = dict(os.environ)
        env["AGENT_LIVE_VIEW"] = str(os.getpid())
        return env

    async def _start_x11vnc(self, pw_file: Path) -> bool:
        # 5900 is often still held - by a socket left behind by an earlier run,
        # by another VNC tool, or by a process this container cannot see. Trying
        # one fixed port made the whole live screen fail whenever that happened,
        # so take the first free port instead of insisting on 5900.
        port = await self._pick_free_port(self.state.vnc_port)
        if port is None:
            log.warning("no free VNC port near %s", self.state.vnc_port)
            return False
        self.state.vnc_port = port
        proc = await asyncio.create_subprocess_exec(
            "x11vnc",
            "-display",
            self.state.display,
            "-rfbport",
            str(port),
            # localhost only: the public door is noVNC, which has its own auth.
            "-localhost",
            "-rfbauth",
            str(pw_file),
            "-forever",
            "-shared",
            "-noxdamage",
            env=self._child_env(),
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
        # The noVNC port is the public one (work-2 -> 12001), so it must stay
        # fixed: moving it would hand the user a URL nothing serves. A stale
        # websockify from our own dead run is already cleared by the sweep.
        proc = await asyncio.create_subprocess_exec(
            "websockify",
            "--web",
            NOVNC_DIR,
            str(self.state.web_port),
            f"127.0.0.1:{self.state.vnc_port}",
            env=self._child_env(),
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
    async def _pick_free_port(start: int, tries: int = 10) -> int | None:
        """First port at or above ``start`` that can actually be bound."""
        for port in range(start, start + tries):
            try:
                server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", port)
            except OSError:
                continue
            server.close()
            try:
                await server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            return port
        return None

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
