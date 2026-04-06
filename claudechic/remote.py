"""HTTP server for remote control of claudechic.

Enables external processes (like Claude in another terminal) to:
- Take screenshots (SVG or PNG)
- Send messages to the active agent
- Simulate key presses
- Wait for agent idle
- Get screen content as text
- Exit the app (for restart)

Start with --remote-port flag or CLAUDECHIC_REMOTE_PORT env var.
"""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path

from rich.console import Console
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from claudechic.app import ChatApp

from claudechic.tasks import create_safe_task

log = logging.getLogger(__name__)

_app: ChatApp | None = None
_server: web.AppRunner | None = None


async def handle_screenshot(request: web.Request) -> web.Response:
    """Save screenshot. Query params: ?path=<tempdir>/shot.svg&format=svg|png

    For PNG, uses macOS qlmanage for conversion (falls back to SVG if unavailable).
    """
    import tempfile

    if _app is None:
        return web.json_response({"error": "App not initialized"}, status=500)

    fmt = request.query.get("format", "svg")
    default_path = str(Path(tempfile.gettempdir()) / f"claudechic-screenshot.{fmt}")
    path = request.query.get("path", default_path)

    try:
        # Always save SVG first
        svg_path = path if fmt == "svg" else path.replace(".png", ".svg")
        result_path = _app.save_screenshot(
            filename=Path(svg_path).name, path=str(Path(svg_path).parent)
        )

        if fmt == "png":
            # Convert SVG to PNG using macOS qlmanage
            from subprocess import DEVNULL

            png_path = path if path.endswith(".png") else f"{path}.png"
            proc = await asyncio.create_subprocess_exec(
                "qlmanage",
                "-t",
                "-s",
                "1200",
                "-o",
                str(Path(png_path).parent),
                result_path,
                stdout=DEVNULL,
                stderr=DEVNULL,
            )
            await proc.wait()
            # qlmanage adds .png to the filename
            actual_png = f"{result_path}.png"
            if Path(actual_png).exists():
                # Rename to requested path
                Path(actual_png).rename(png_path)
                result_path = png_path

        return web.json_response({"path": result_path, "format": fmt})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_send(request: web.Request) -> web.Response:
    """Send a message or command to the active agent. Body: {"text": "message"}

    If text starts with / or !, it's treated as a command.
    Otherwise it's sent to the agent as a prompt.
    """
    if _app is None:
        return web.json_response({"error": "App not initialized"}, status=500)

    try:
        data = await request.json()
        text = data.get("text", "")
    except Exception:
        # Plain text body
        text = await request.text()

    if not text:
        return web.json_response({"error": "No text provided"}, status=400)

    # Check for slash/bang commands
    stripped = text.strip()
    if stripped.startswith("/") or stripped.startswith("!"):
        from claudechic.commands import handle_command as do_command

        try:
            handled = do_command(_app, text)
            return web.json_response(
                {"status": "executed" if handled else "not_handled", "command": text}
            )
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # Send to active agent
    agent = _app._agent
    if agent is None:
        return web.json_response({"error": "No active agent"}, status=400)

    try:
        _app._send_to_active_agent(text)
        return web.json_response({"status": "sent", "text": text[:100]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_screen_text(request: web.Request) -> web.Response:
    """Get current screen content as plain text.

    Returns the full screen rendered as text, preserving 2D layout.
    Uses the same rendering pipeline as export_screenshot but outputs plain text.

    Query params:
        compact: If "false", include blank lines (default: true, removes blank lines)
    """
    compact = request.query.get("compact", "true").lower() != "false"
    if _app is None:
        return web.json_response({"error": "App not initialized"}, status=500)

    try:
        width, height = _app.size
        console = Console(
            width=width,
            height=height,
            file=io.StringIO(),
            force_terminal=True,
            color_system="truecolor",
            record=True,
            legacy_windows=False,
            safe_box=False,
        )
        screen_render = _app.screen._compositor.render_update(
            full=True, screen_stack=_app._background_screens
        )
        console.print(screen_render)
        text = console.export_text(clear=True, styles=False)
        if compact:
            text = "\n".join(line for line in text.splitlines() if line.strip())
        return web.json_response({"text": text})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_wait_idle(request: web.Request) -> web.Response:
    """Wait until active agent is idle. Query param: ?timeout=30"""
    if _app is None:
        return web.json_response({"error": "App not initialized"}, status=500)

    timeout = float(request.query.get("timeout", "30"))
    agent = _app._agent
    if agent is None:
        return web.json_response({"error": "No active agent"}, status=400)

    from claudechic.enums import AgentStatus

    try:
        loop = asyncio.get_running_loop()
        start = loop.time()
        while agent.status != AgentStatus.IDLE:
            if loop.time() - start > timeout:
                return web.json_response(
                    {"error": "Timeout waiting for idle"}, status=408
                )
            await asyncio.sleep(0.1)
        return web.json_response({"status": "idle"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_status(request: web.Request) -> web.Response:  # noqa: ARG001
    """Get app/agent status."""
    if _app is None:
        return web.json_response({"error": "App not initialized"}, status=500)

    agent = _app._agent
    agents = []
    if _app.agent_mgr:
        for a in _app.agent_mgr:
            agents.append(
                {
                    "name": a.name,
                    "id": a.id,
                    "status": str(a.status),
                    "cwd": str(a.cwd),
                    "active": a.id == _app.agent_mgr.active_id,
                }
            )

    return web.json_response(
        {
            "agents": agents,
            "active_agent": agent.name if agent else None,
        }
    )


async def handle_key(request: web.Request) -> web.Response:
    """Simulate key presses. Body: {"keys": ["escape", "j", "k", "enter"]}

    Supported keys:
    - Single characters: "a", "1", etc.
    - Named keys: "escape", "enter", "tab", "space", "backspace", "delete"
    - Arrow keys: "up", "down", "left", "right"
    - Function keys: "f1" through "f12"
    - Modifiers: "ctrl+c", "shift+tab", "ctrl+n"
    - Special: "wait:500" to pause 500ms between keys
    """
    if _app is None:
        return web.json_response({"error": "App not initialized"}, status=500)

    try:
        data = await request.json()
        keys = data.get("keys", [])
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    if not keys:
        return web.json_response({"error": "No keys provided"}, status=400)

    if not isinstance(keys, list):
        keys = [keys]

    try:
        # Handle wait:N delays between keys
        batch: list[str] = []
        for key in keys:
            if key.startswith("wait:"):
                if batch:
                    await _app._press_keys(batch)
                    batch = []
                ms = int(key.split(":")[1])
                await asyncio.sleep(ms / 1000)
            else:
                batch.append(key)
        if batch:
            await _app._press_keys(batch)
        return web.json_response({"status": "pressed", "keys": keys})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_attach_image(request: web.Request) -> web.Response:
    """Attach an image file to the active agent's pending images.

    Body: {"path": "/absolute/path/to/image.png"}

    Used by ShareX custom action to auto-attach captures without manual paste.
    """
    if _app is None:
        return web.json_response({"error": "App not initialized"}, status=500)

    try:
        data = await request.json()
        path_str = data.get("path")
        if not path_str:
            return web.json_response({"error": "Missing 'path' field"}, status=400)
        path = Path(path_str)
        if not path.exists():
            return web.json_response({"error": f"File not found: {path}"}, status=404)
        _app._attach_image(path)
        return web.json_response({"ok": True, "filename": path.name})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_exit(request: web.Request) -> web.Response:  # noqa: ARG001
    """Exit the app cleanly. Use this before restarting."""
    if _app is None:
        return web.json_response({"error": "App not initialized"}, status=500)

    # Schedule exit after response is sent
    async def do_exit():
        await asyncio.sleep(0.1)  # Let response complete
        if _app:
            await _app._cleanup_and_exit()

    create_safe_task(do_exit(), name="exit-handler")
    return web.json_response({"status": "exiting"})


async def start_server(app: ChatApp, port: int) -> None:
    """Start the remote control HTTP server."""
    global _app, _server
    _app = app

    webapp = web.Application()
    webapp.router.add_get("/screenshot", handle_screenshot)
    webapp.router.add_post("/send", handle_send)
    webapp.router.add_get("/screen_text", handle_screen_text)
    webapp.router.add_get("/wait_idle", handle_wait_idle)
    webapp.router.add_get("/status", handle_status)
    webapp.router.add_post("/exit", handle_exit)
    webapp.router.add_post("/key", handle_key)
    webapp.router.add_post("/attach-image", handle_attach_image)

    runner = web.AppRunner(webapp)
    await runner.setup()
    _server = runner

    site = web.TCPSite(runner, "localhost", port)
    await site.start()
    log.info(f"Remote control server started on http://localhost:{port}")
