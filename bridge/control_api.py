"""Small HTTP API for camera controls (pan/tilt, zoom), protected by the go2rtc login.

POST /api/control  {"stream": "eufy_frontyard_east", "action": "ptz", "move": "tap",
                    "direction": "left"}   or   {"stream": ..., "action": "zoom", "level": 4}
The request goes to the running stream engine over a unix socket; it only works while
that camera is live (the NVR accepts controls inside the open live session).
"""
import asyncio
import base64
import hmac
import json
import os
import sys

from aiohttp import web

from camera_control import ControlError, parse_request

SOCKET = os.environ.get("EUFY_CONTROL_SOCKET", "/tmp/eufy_nvr_control.sock")
TIMEOUT = 5.0


def authorized(header, username, password):
    if not header or not header.startswith("Basic ") or not username or not password:
        return False
    try:
        user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(user, username) and hmac.compare_digest(pw, password)


async def forward(obj, path=SOCKET, timeout=TIMEOUT):
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path), timeout)
    except (FileNotFoundError, ConnectionRefusedError, asyncio.TimeoutError, OSError):
        return 409, {"ok": False, "error": "no live stream"}
    try:
        writer.write((json.dumps(obj) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
        reply = json.loads(line or b"{}")
    except (asyncio.TimeoutError, ValueError, OSError):
        return 504, {"ok": False, "error": "engine did not answer"}
    finally:
        writer.close()
    return (200 if reply.get("ok") else 409), reply


def make_app(username, password, socket_path=SOCKET):
    async def control(request):
        if not authorized(request.headers.get("Authorization"), username, password):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401,
                                     headers={"WWW-Authenticate": 'Basic realm="eufy"'})
        try:
            obj = await request.json()
            parse_request(obj)
        except ControlError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except ValueError:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        status, reply = await forward(obj, socket_path)
        return web.json_response(reply, status=status)

    app = web.Application(client_max_size=4096)
    app.router.add_post("/api/control", control)
    return app


def main():
    port = int(os.environ.get("EUFY_CONTROL_PORT", "1986"))
    user = os.environ.get("GO2RTC_USERNAME", "")
    password = os.environ.get("GO2RTC_PASSWORD", "")
    if not user or not password:
        print("control API: go2rtc credentials missing; not starting", file=sys.stderr)
        return 1
    web.run_app(make_app(user, password), host=os.environ.get("EUFY_CONTROL_HOST", "0.0.0.0"),
                port=port, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
