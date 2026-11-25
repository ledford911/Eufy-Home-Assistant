"""Pan/tilt and zoom commands for NVR cameras, as eufy's web viewer sends them.

Pan/tilt: command_id 1700 (cmd 6030) on the camera's channel. The JSON body carries the
command fields at the top level (unlike command_id 1350, which nests them in "payload"):
    {"account_id": ..., "cmd": 6030, "commandType": 6030,
     "data": {"cmd_type": <1 tap | 4 hold | 3 release>, "rotate_type": <1-4>, "zoom": 1}}
Zoom: command_id 1350, cmd 6203, payload {"dstZoom": <level>} on the camera's channel.
The web viewer allows zoom levels 1-8 on the S4 Max (T8E00).
"""
import json
import struct

PTZ_COMMAND_ID = 1700
PTZ_CMD = 6030
ZOOM_CMD = 6203
DIRECTIONS = {"left": 1, "right": 2, "up": 3, "down": 4}
MOVES = {"tap": 1, "hold": 4, "release": 3}
ZOOM_MIN, ZOOM_MAX = 1, 8


class ControlError(ValueError):
    """A control request that cannot be sent."""


def _frame(command_id, channel, body):
    raw = json.dumps(body, separators=(",", ":")).encode()
    h = bytearray(16)
    h[0:4] = b"XZYH"
    struct.pack_into("<H", h, 4, command_id)
    struct.pack_into("<I", h, 6, len(raw))
    h[12] = channel & 0xFF
    h[15] = 2                                   # dev_type: cloud web client
    return bytes(h) + raw


def build_ptz(user_id, channel, move, direction):
    return _frame(PTZ_COMMAND_ID, channel, {
        "account_id": user_id, "cmd": PTZ_CMD, "commandType": PTZ_CMD,
        "data": {"cmd_type": MOVES[move], "rotate_type": DIRECTIONS[direction], "zoom": 1},
    })


def build_zoom(user_id, channel, level):
    return _frame(1350, channel, {"account_id": user_id, "cmd": ZOOM_CMD,
                                  "payload": {"dstZoom": level}})


def parse_request(obj):
    """Validate a control request. Returns (action, args) or raises ControlError."""
    if not isinstance(obj, dict):
        raise ControlError("request must be a JSON object")
    stream = obj.get("stream")
    if not isinstance(stream, str) or not stream:
        raise ControlError("stream is required")
    action = obj.get("action")
    if action == "ptz":
        move = obj.get("move", "tap")
        direction = obj.get("direction")
        if move not in MOVES:
            raise ControlError(f"move must be one of {sorted(MOVES)}")
        if direction not in DIRECTIONS:
            raise ControlError(f"direction must be one of {sorted(DIRECTIONS)}")
        return stream, "ptz", {"move": move, "direction": direction}
    if action == "zoom":
        level = obj.get("level")
        if isinstance(level, bool) or not isinstance(level, (int, float)):
            raise ControlError("level must be a number")
        level = int(round(level))
        if not ZOOM_MIN <= level <= ZOOM_MAX:
            raise ControlError(f"level must be {ZOOM_MIN}-{ZOOM_MAX}")
        return stream, "zoom", {"level": level}
    raise ControlError("action must be ptz or zoom")


def build(user_id, channel, action, args):
    if action == "ptz":
        return build_ptz(user_id, channel, args["move"], args["direction"])
    return build_zoom(user_id, channel, args["level"])
