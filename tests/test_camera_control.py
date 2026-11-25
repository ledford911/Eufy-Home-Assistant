"""Offline tests for camera controls (pan/tilt, zoom) and the control API."""
import asyncio
import base64
import importlib.util
import json
import os
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
import camera_control  # noqa: E402

pytestmark = pytest.mark.skipif(os.name == "nt", reason="unix sockets")


def decode(frame):
    assert frame[:4] == b"XZYH"
    command_id = struct.unpack_from("<H", frame, 4)[0]
    length = struct.unpack_from("<I", frame, 6)[0]
    assert length == len(frame) - 16
    return command_id, frame[12], frame[15], json.loads(frame[16:])


def test_ptz_frame_matches_the_web_viewer():
    cid, ch, dev, body = decode(camera_control.build_ptz("U", 2, "tap", "left"))
    assert (cid, ch, dev) == (1700, 2, 2)
    assert body == {"account_id": "U", "cmd": 6030, "commandType": 6030,
                    "data": {"cmd_type": 1, "rotate_type": 1, "zoom": 1}}
    assert decode(camera_control.build_ptz("U", 0, "hold", "down"))[3]["data"]["cmd_type"] == 4
    assert decode(camera_control.build_ptz("U", 0, "release", "up"))[3]["data"] == {
        "cmd_type": 3, "rotate_type": 3, "zoom": 1}


def test_zoom_frame_matches_the_web_viewer():
    cid, ch, _dev, body = decode(camera_control.build_zoom("U", 3, 8))
    assert (cid, ch) == (1350, 3)
    assert body == {"account_id": "U", "cmd": 6203, "payload": {"dstZoom": 8}}


@pytest.mark.parametrize("obj", [
    None, {}, {"stream": "s"}, {"stream": "s", "action": "spin"},
    {"stream": "s", "action": "ptz", "direction": "sideways"},
    {"stream": "s", "action": "ptz", "move": "fling", "direction": "left"},
    {"stream": "s", "action": "zoom", "level": 0},
    {"stream": "s", "action": "zoom", "level": 9},
    {"stream": "s", "action": "zoom", "level": True},
    {"stream": "s", "action": "zoom", "level": "4"},
])
def test_bad_requests_are_refused(obj):
    with pytest.raises(camera_control.ControlError):
        camera_control.parse_request(obj)


def test_good_requests_parse():
    assert camera_control.parse_request(
        {"stream": "eufy_a", "action": "ptz", "direction": "right"}) == (
        "eufy_a", "ptz", {"move": "tap", "direction": "right"})
    assert camera_control.parse_request({"stream": "eufy_a", "action": "zoom", "level": 4.4}) == (
        "eufy_a", "zoom", {"level": 4})


def load_api():
    spec = importlib.util.spec_from_file_location("control_api", ROOT / "bridge/control_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def basic(user, pw):
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def test_authorization_check():
    api = load_api()
    assert api.authorized(basic("u", "p" * 16), "u", "p" * 16)
    assert not api.authorized(basic("u", "wrong"), "u", "p" * 16)
    assert not api.authorized(None, "u", "p" * 16)
    assert not api.authorized("Basic !!!", "u", "p" * 16)
    assert not api.authorized(basic("", ""), "", "")


def test_api_forwards_to_the_engine_and_needs_the_login(tmp_path):
    pytest.importorskip("aiohttp")
    from aiohttp.test_utils import TestClient, TestServer
    api = load_api()
    sock = str(tmp_path / "c.sock")
    seen = []

    async def engine(reader, writer):
        seen.append(json.loads(await reader.readline()))
        writer.write(b'{"ok": true, "channel": 0}\n')
        await writer.drain()
        writer.close()

    async def run():
        server = await asyncio.start_unix_server(engine, path=sock)
        cfg = tmp_path / "go2rtc.yaml"
        cfg.write_text('streams:\n  eufy_a: "exec:python eufy_run.py 3 --rtsp {output}#x"\n')
        client = TestClient(TestServer(api.make_app("u", "p" * 16, sock, str(cfg))))
        await client.start_server()
        try:
            body = {"stream": "eufy_a", "action": "zoom", "level": 2}
            r = await client.post("/api/control", json=body)
            assert r.status == 401
            r = await client.post("/api/control", json=body, headers={"Authorization": basic("u", "p" * 16)})
            assert r.status == 200 and (await r.json())["ok"]
            r = await client.post("/api/control", json={"stream": "eufy_a", "action": "zoom", "level": 99},
                                  headers={"Authorization": basic("u", "p" * 16)})
            assert r.status == 400
        finally:
            await client.close()
            server.close()
        assert seen == [{"stream": "eufy_a", "action": "zoom", "level": 2, "channel": 3, "sensor": None}]

    asyncio.run(run())


def test_api_without_a_live_engine(tmp_path):
    api = load_api()
    status, reply = asyncio.run(api.forward({"stream": "x"}, str(tmp_path / "none.sock")))
    assert status == 409 and reply["error"] == "no live stream"


def test_engine_and_addon_wiring():
    stream = (ROOT / "bridge/eufy_stream.py").read_text()
    assert "camera_control.build(USER_ID, CHANNELS[0], action, args)" in stream
    assert "the fixed lens has no pan/tilt or zoom" in stream
    run = (ROOT / "eufy_nvr/run.sh").read_text()
    assert "control_api.py" in run and "camera_controls" in run
    config = (ROOT / "eufy_nvr/config.yaml").read_text()
    assert "camera_controls: bool?" in config and "1986/tcp" in config


def test_stream_names_resolve_from_the_generated_config():
    spec = importlib.util.spec_from_file_location("gen_for_control", ROOT / "bridge/gen_go2rtc.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    named = [("eufy_front_gate", {"channel": 0, "status": 1}),
             ("eufy_porch", {"channel": 6, "status": 1})]
    text = gen.render_config(named, "user", "p" * 16, 1985, 8556, 8557,
                             extra_lenses={0: [0]}, camera_groups=[("front", [(0, 0, 1), (1, 6, 1)])])
    assert camera_control.resolve_stream("eufy_front_gate", text) == (0, None)
    assert camera_control.resolve_stream("eufy_front_gate_lens0", text) == (0, 0)
    assert camera_control.resolve_stream("eufy_porch", text) == (6, None)
    with pytest.raises(camera_control.ControlError, match="multi-camera"):
        camera_control.resolve_stream("eufy_group_front", text)
    with pytest.raises(camera_control.ControlError, match="unknown"):
        camera_control.resolve_stream("eufy_nope", text)


def test_ha_buttons_only_for_ptz_cameras_and_zoom_steps():
    pytest.importorskip("homeassistant")
    sys.path.insert(0, str(ROOT))
    from custom_components.eufy_nvr import button
    streams = ["eufy_front_gate", "eufy_front_gate_lens0", "eufy_porch", "eufy_group_all"]
    assert button.ptz_streams(streams) == ["eufy_front_gate"]
    assert [button.next_zoom(z, "in") for z in (1, 2, 4, 8)] == [2, 4, 8, 8]
    assert [button.next_zoom(z, "out") for z in (8, 4, 2, 1)] == [4, 2, 1, 1]
    assert button.next_zoom(8, "reset") == 1
    assert len({b.key for b in button.BUTTONS}) == 7
