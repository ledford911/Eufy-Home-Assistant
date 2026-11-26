"""Offline tests for "newest camera wins" (preempt_after_seconds)."""
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(os.name == "nt", reason="flock gate")


def load_run(monkeypatch, tmp_path, preempt="5"):
    monkeypatch.setenv("EUFY_PREEMPT_AFTER_SECONDS", preempt)
    monkeypatch.setenv("EUFY_SESSION_REUSE_SECONDS", "30")
    monkeypatch.setenv("EUFY_SESSION_SOCKET", str(tmp_path / "s.sock"))
    monkeypatch.setenv("EUFY_SESSION_LOCK", str(tmp_path / "s.lock"))
    monkeypatch.setenv("EUFY_SESSION_PREEMPT", str(tmp_path / "s.preempt"))
    spec = importlib.util.spec_from_file_location("eufy_run_preempt", ROOT / "bridge/eufy_run.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_stream_key_ignores_the_per_run_rtsp_url(monkeypatch, tmp_path):
    run = load_run(monkeypatch, tmp_path)
    assert run.stream_key(["1", "--sensor", "0", "--rtsp", "rtsp://x/abc"]) == "1 sensor 0"
    assert run.stream_key(["1", "--rtsp", "rtsp://x/def"]) == "1"
    assert run.stream_key(["g:0-1-0,1-0-0", "--rtsp", "u"]) == "g:0-1-0,1-0-0"


def test_takeover_requests_expire_and_ignore_our_own_camera(monkeypatch, tmp_path):
    run = load_run(monkeypatch, tmp_path)
    run.request_takeover("2", now=1000.0)
    assert run.takeover_request("1", now=1001.0) == "2"
    assert run.takeover_request("2", now=1001.0) is None          # our own request
    assert run.takeover_request("1", now=1000.0 + run.PREEMPT_FRESH + 1) is None


def test_a_stepped_aside_camera_cools_down(monkeypatch, tmp_path):
    run = load_run(monkeypatch, tmp_path)
    run.mark_preempted("1", now=1000.0)
    assert run.recently_preempted("1", now=1000.0 + 60)
    assert not run.recently_preempted("2", now=1000.0 + 60)
    assert not run.recently_preempted("1", now=1000.0 + run.PREEMPT_COOLDOWN + 1)


def test_holder_steps_aside_for_a_newer_camera(monkeypatch, tmp_path):
    run = load_run(monkeypatch, tmp_path)

    async def scenario():
        stop = asyncio.Event()
        watcher = asyncio.create_task(run.watch_takeover("1", stop))
        await asyncio.sleep(0.1)
        assert not stop.is_set()
        run.request_takeover("2")
        await asyncio.wait_for(stop.wait(), timeout=2)
        await watcher
    asyncio.run(scenario())
    assert run.recently_preempted("1")


def test_waiter_asks_for_takeover_only_after_waiting(monkeypatch, tmp_path):
    run = load_run(monkeypatch, tmp_path, preempt="1")

    async def scenario():
        holder = run.SessionGate(path=tmp_path / "s.lock", preempt_path=tmp_path / "s.preempt")
        assert await holder.acquire(asyncio.Event()) is True
        waiter = run.SessionGate(path=tmp_path / "s.lock", preempt_path=tmp_path / "s.preempt")
        stop = asyncio.Event()
        task = asyncio.create_task(waiter.acquire(stop, takeover_key="2"))
        await asyncio.sleep(0.5)
        assert run.takeover_request("1") is None                  # still within the wait
        await asyncio.sleep(1.0)
        assert run.takeover_request("1") == "2"
        holder.release()                                            # the holder steps aside
        assert await asyncio.wait_for(task, timeout=2) is True
        waiter.release()
    asyncio.run(scenario())


def test_a_cooled_down_camera_never_asks_and_off_by_default(monkeypatch, tmp_path):
    run = load_run(monkeypatch, tmp_path, preempt="1")
    run.mark_preempted("2")

    async def scenario(mod, key):
        holder = mod.SessionGate(path=tmp_path / "s.lock", preempt_path=tmp_path / "s.preempt")
        assert await holder.acquire(asyncio.Event()) is True
        waiter = mod.SessionGate(path=tmp_path / "s.lock", preempt_path=tmp_path / "s.preempt")
        stop = asyncio.Event()
        task = asyncio.create_task(waiter.acquire(stop, takeover_key=key))
        await asyncio.sleep(1.6)
        stop.set()
        assert await task is False
        holder.release()
    asyncio.run(scenario(run, "2"))
    assert run.takeover_request("1") is None

    off = load_run(monkeypatch, tmp_path, preempt="0")
    assert off.PREEMPT_SECONDS == 0
    asyncio.run(scenario(off, "3"))
    assert off.takeover_request("1") is None


def test_stepped_aside_camera_does_not_jump_the_queue_for_the_parked_session(monkeypatch, tmp_path):
    run = load_run(monkeypatch, tmp_path, preempt="5")
    run.mark_preempted("1")
    calls = []

    async def handoff():
        calls.append(1)
        return None

    async def scenario():
        holder = run.SessionGate(path=tmp_path / "s.lock", preempt_path=tmp_path / "s.preempt")
        assert await holder.acquire(asyncio.Event()) is True
        newer = run.SessionGate(path=tmp_path / "s.lock", preempt_path=tmp_path / "s.preempt")
        stop_newer = asyncio.Event()
        t_newer = asyncio.create_task(newer.acquire(stop_newer, takeover_key="2"))
        await asyncio.sleep(0.2)
        bumped = run.SessionGate(path=tmp_path / "s.lock", preempt_path=tmp_path / "s.preempt")
        stop_bumped = asyncio.Event()
        t_bumped = asyncio.create_task(bumped.acquire(stop_bumped, alternative=handoff, takeover_key="1"))
        await asyncio.sleep(1.2)
        assert calls == []                  # it waits behind the newer request
        for e in (stop_newer, stop_bumped):
            e.set()
        await t_newer
        await t_bumped
        holder.release()
    asyncio.run(scenario())


def test_addon_option_and_wiring():
    config = yaml.safe_load((ROOT / "eufy_nvr/config.yaml").read_text())
    assert config["options"]["preempt_after_seconds"] == 0
    assert config["schema"]["preempt_after_seconds"] == "int(0,120)?"
    run_sh = (ROOT / "eufy_nvr/run.sh").read_text()
    assert "EUFY_PREEMPT_AFTER_SECONDS" in run_sh
    supervisor = (ROOT / "bridge/eufy_run.py").read_text()
    assert "watch_takeover(key, stop_event)" in supervisor
    assert "takeover_key=key" in supervisor
