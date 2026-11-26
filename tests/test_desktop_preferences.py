import asyncio
import json
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="requires Windows DPAPI")

if sys.platform == "win32":
    import win32crypt

    from desktop import DesktopBridge


def test_overlay_font_is_validated_and_saved(tmp_path):
    path = tmp_path / "connection.dat"
    bridge = DesktopBridge(settings_path=path)
    assert bridge.overlay_font() == {"size": 18, "family": "system"}
    assert bridge.set_overlay_font(26, "serif")
    assert not bridge.set_overlay_font(999, "serif")
    assert not bridge.set_overlay_font(22, "unavailable")
    assert bridge.set_overlay_mode("translation")
    assert DesktopBridge(settings_path=path).overlay_font() == {"size": 26, "family": "serif"}


def test_connection_settings_survive_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("VTOH_ACCESS_KEY", raising=False)
    path = tmp_path / "connection.dat"
    legacy = json.dumps({"address": "https://example.com/vtoh", "key": "test-access-secret"})
    path.write_bytes(win32crypt.CryptProtectData(legacy.encode(), "VtoH", None, None, None, 1))
    assert b"test-access-secret" not in path.read_bytes()

    restored = DesktopBridge(settings_path=path)
    assert restored.settings() == {"has_key": True}
    assert restored.key == "test-access-secret"
    assert restored.save_settings(None)["has_key"]
    _, saved = win32crypt.CryptUnprotectData(path.read_bytes(), None, None, None, 1)
    assert "address" not in json.loads(saved)
    assert DesktopBridge(settings_path=path).key == "test-access-secret"
    assert restored.set_overlay_style("light")
    assert DesktopBridge(settings_path=path).overlay_style() == "light"
    assert not restored.set_overlay_style("invalid")


def test_overlay_translation_mode_and_direct_stop(tmp_path):
    path = tmp_path / "connection.dat"
    bridge = DesktopBridge(settings_path=path)
    assert bridge.set_overlay_mode("translation")
    assert DesktopBridge(settings_path=path).overlay_mode() == "translation"
    bridge._set_phase("live")
    bridge.overlay_toggle_capture()
    assert bridge.stop_signal.is_set()
    assert bridge.poll_workspace()["phase"] == "stopping"


def test_overlay_close_is_idempotent_and_controls_share_state(tmp_path):
    bridge = DesktopBridge(settings_path=tmp_path / "connection.dat")
    bridge._overlay = Mock()
    bridge._overlay.native = None
    assert bridge.toggle_overlay()
    bridge.hide_overlay()
    bridge.hide_overlay()
    bridge._overlay.show.assert_called_once()
    bridge._overlay.hide.assert_called_once()
    assert not bridge.workspace_status()["overlay_open"]
    assert bridge.workspace_status()["phase"] == bridge.overlay_caption()["phase"] == "idle"
    bridge._set_phase("starting")
    assert bridge.workspace_status()["phase"] == bridge.overlay_caption()["phase"] == "starting"
    bridge.overlay_toggle_capture()
    assert bridge.workspace_status()["phase"] == bridge.overlay_caption()["phase"] == "stopping"
    bridge._capture_ready()
    assert bridge.workspace_status()["phase"] == "stopping"


def test_overlay_keeps_all_rows_and_updates_translation_in_place(tmp_path):
    bridge = DesktopBridge(settings_path=tmp_path / "connection.dat")
    bridge._record_caption({"type": "session", "id": "test"})
    bridge._record_caption({"type": "transcript", "id": "1", "text": "第一句"})
    bridge._record_caption({"type": "transcript", "id": "2", "text": "第二句"})
    before = bridge.overlay_caption()
    bridge._record_caption({"type": "transcript", "id": "1", "text": "第一句", "translation": "First."})
    rows = bridge.overlay_caption()["transcripts"]
    assert [row["id"] for row in rows] == ["1", "2"]
    assert rows[0]["translation"] == "First."
    assert bridge.overlay_caption(before["revision"], "test")["transcripts"] == [rows[0]]
    bridge._record_caption({"type": "session", "id": "next"})
    assert bridge.overlay_caption(before["revision"], "test")["reset"]
    assert not bridge.overlay_caption()["transcripts"]


@pytest.mark.parametrize(
    ("mode", "field", "text", "language"),
    [
        ("bilingual", "text", "第二小段", ""),
        ("bilingual", "translation", "First.", "en"),
        ("translation", "translation", "First.", "en"),
        ("translation", "text", "第二小段", None),
        ("bilingual", "translation", "已经更新的译文", None),
        ("bilingual", "text", "兼容旧版本", ""),
    ],
)
def test_overlay_speaks_only_the_selected_segment(tmp_path, monkeypatch, mode, field, text, language):
    bridge = DesktopBridge(settings_path=tmp_path / "connection.dat")
    assert bridge.set_overlay_mode(mode)
    bridge._record_caption({"type": "transcript", "id": "1", "text": "第一小段 第二小段",
                            "text_lines": ["第一小段 ", "第二小段"], "translation": "First. Second.",
                            "translation_lines": ["First. ", "Second."], "translation_language": "en"})
    bridge._record_caption({"type": "transcript", "id": "legacy", "text": "兼容旧版本"})
    bridge._record_caption({"type": "transcript", "id": "new", "text": "最新一句"})
    thread = Mock()
    monkeypatch.setattr("desktop.threading.Thread", thread)
    identifier = "legacy" if text == "兼容旧版本" else "1"
    assert bridge.speak_caption(identifier, field, text) is (language is not None)
    if language is None:
        thread.assert_not_called()
    else:
        assert thread.call_args.kwargs["args"][:2] == (text, language)
        thread.return_value.start.assert_called_once()


def test_capture_can_stop_when_audio_source_stalls(tmp_path, monkeypatch):
    async def run():
        bridge = DesktopBridge(settings_path=tmp_path / "connection.dat")
        reading = asyncio.Event()
        stopped = asyncio.Event()

        async def read_frame(_):
            reading.set()
            await asyncio.Future()

        async def receive():
            await stopped.wait()
            return '{"type":"stopped"}'

        calls = 0

        async def recv():
            nonlocal calls
            calls += 1
            return '{"type":"ready"}' if calls == 1 else await receive()

        async def send(data):
            assert data == '{"type":"stop"}'
            stopped.set()

        socket = SimpleNamespace(recv=recv, send=send)

        @asynccontextmanager
        async def connect(_):
            yield socket

        process = SimpleNamespace(
            stdout=SimpleNamespace(readexactly=read_frame), returncode=None,
            stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()),
            communicate=AsyncMock(),
        )
        monkeypatch.setattr("desktop.connect", connect)
        monkeypatch.setattr("desktop.asyncio.create_subprocess_exec", AsyncMock(return_value=process))
        bridge._set_phase("starting")
        task = asyncio.create_task(bridge.connection.capture("http://example.com/vtoh", "/sessions/1", "token", "0", ""))
        await asyncio.wait_for(reading.wait(), 1)
        bridge.overlay_toggle_capture()
        await asyncio.wait_for(task, 1)
        assert stopped.is_set()
        process.stdin.write.assert_called_once_with(b"stop")

    asyncio.run(run())
