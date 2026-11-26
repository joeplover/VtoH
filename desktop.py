import asyncio
import ctypes
import json
import locale
import os
import queue
import subprocess
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
import pythoncom
import qrcode
import pywintypes
import win32crypt
import win32com.client
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

ROOT = Path(sys.executable if getattr(sys, "frozen", False) else __file__).parent
SERVER_URL = "https://www.ploverjoe.xyz/vtoh"
TRANSLATION_OPTIONS = {
    "关闭翻译": "", "简体中文": "zh", "英语": "en", "日语": "ja", "韩语": "ko",
    "法语": "fr", "西班牙语": "es", "德语": "de",
}
OVERLAY_OPACITY = {"standard": 0.84, "light": 0.67, "contrast": 0.96}
OVERLAY_FONTS = {"system": "Segoe UI", "serif": "Georgia", "mono": "Cascadia Mono"}
OVERLAY_FONT_SIZES = range(12, 41, 2)
CAPTURE = ROOT / "vtoh_capture.exe"
if not CAPTURE.is_file():
    CAPTURE = ROOT / ".tools" / "bin" / "vtoh_capture.exe"


def endpoint(base: str, path: str, token: str, target: str = "") -> str:
    url = urlsplit(base)
    query = {"token": token}
    if target:
        query["translate"] = target
    return urlunsplit(("wss" if url.scheme == "https" else "ws", url.netloc,
                        url.path.rstrip("/") + path, urlencode(query), ""))


class Connection:
    def __init__(self, events: queue.Queue, stop: threading.Event, commands: queue.Queue,
                 on_ready, on_stopped, on_caption):
        self.events = events
        self.stop = stop
        self.commands = commands
        self.on_ready = on_ready
        self.on_stopped = on_stopped
        self.on_caption = on_caption
        self.session = None
        self.base = ""
        self.key = ""

    async def run(self, base: str, key: str, pid: str, target: str = "") -> None:
        if self.session is None or self.base != base or self.key != key:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(f"{base}/sessions", headers={"Authorization": f"Bearer {key}"})
                response.raise_for_status()
                self.session = response.json()
                self.base = base
                self.key = key
        if self.stop.is_set():
            return
        session = self.session
        self.on_caption({"type": "session", "id": session["id"]})
        path = f"/sessions/{session['id']}"
        pair = f"{base}/pair#" + urlencode({"id": session["id"], "token": session["viewer"]})
        self.events.put({"type": "pair", "url": pair})
        restart = asyncio.Event()
        async with connect(endpoint(base, f"{path}/control", session["owner"])) as control:
            async def receive():
                async for raw in control:
                    event = json.loads(raw)
                    self.on_caption(event)
                    self.events.put(event)

            async def send():
                while True:
                    try:
                        command = self.commands.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.05)
                    else:
                        if command is None:
                            restart.set()
                            return
                        await control.send(json.dumps(command))

            async with asyncio.TaskGroup() as group:
                listener = group.create_task(receive())
                sender = group.create_task(send())
                try:
                    await self.capture(base, path, session["owner"], pid, target)
                    self.on_stopped("idle")
                    self.events.put({"type": "status", "message": "已停止监听，可选择定稿字幕回答"})
                    restart_wait = group.create_task(restart.wait())
                    await asyncio.wait((listener, restart_wait), return_when=asyncio.FIRST_COMPLETED)
                    restart_wait.cancel()
                finally:
                    listener.cancel()
                    sender.cancel()

    async def capture(self, base: str, path: str, token: str, pid: str, target: str) -> None:
        if self.stop.is_set():
            return
        async with connect(endpoint(base, f"{path}/audio", token, target)) as audio:
            if json.loads(await asyncio.wait_for(audio.recv(), timeout=10))["type"] != "ready":
                raise RuntimeError("服务端未就绪")
            if self.stop.is_set():
                await audio.send('{"type":"stop"}')
                return
            process = await asyncio.create_subprocess_exec(
                str(CAPTURE), "capture", pid,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self.on_ready()
            async def send_frames():
                while not self.stop.is_set():
                    try:
                        frame = await asyncio.wait_for(process.stdout.readexactly(3200), timeout=12)
                    except asyncio.IncompleteReadError as failure:
                        message = (await process.stderr.read()).decode(errors="replace").strip()
                        raise RuntimeError(message or "采集进程已退出") from failure
                    await audio.send(frame)
                    peak = max(abs(sample) for sample in memoryview(frame).cast("h"))
                    self.events.put({"type": "level", "peak": peak})

            async def wait_for_stop():
                while not self.stop.is_set():
                    await asyncio.sleep(0.05)

            async def receive_result():
                if json.loads(await audio.recv()).get("type") != "stopped":
                    raise RuntimeError("语音识别未正常结束")

            sender = asyncio.create_task(send_frames())
            receiver = asyncio.create_task(receive_result())
            stopping = asyncio.create_task(wait_for_stop())
            try:
                await asyncio.wait((sender, receiver, stopping), return_when=asyncio.FIRST_COMPLETED)
                if sender.done():
                    sender.result()
                if receiver.done():
                    receiver.result()
                elif self.stop.is_set():
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
                    await audio.send('{"type":"stop"}')
                    await asyncio.wait_for(receiver, timeout=8)
            finally:
                for task in (sender, receiver, stopping):
                    task.cancel()
                await asyncio.gather(sender, receiver, stopping, return_exceptions=True)
                if process.returncode is None:
                    try:
                        process.stdin.write(b"stop")
                        await process.stdin.drain()
                        await asyncio.wait_for(process.communicate(), timeout=2)
                    except (BrokenPipeError, ConnectionResetError, TimeoutError):
                        if process.returncode is None:
                            process.kill()
                            await process.wait()


class DesktopBridge:
    def __init__(self, settings_path=None):
        self.events = queue.Queue()
        self.commands = queue.Queue()
        self.stop_signal = threading.Event()
        self._state_lock = threading.Lock()
        self._phase = "idle"
        self.connection = Connection(
            self.events, self.stop_signal, self.commands, self._capture_ready, self._set_phase,
            self._record_caption,
        )
        self.thread = None
        self.key = os.getenv("VTOH_ACCESS_KEY", "")
        self._settings_path = settings_path or Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "VtoH" / "connection.dat"
        self._settings_error = None
        self._overlay = None
        self._overlay_open = False
        self._overlay_lock = threading.Lock()
        self._overlay_style = "standard"
        self._overlay_mode = "bilingual"
        self._overlay_font_size = 18
        self._overlay_font_family = "system"
        self._caption_lock = threading.Lock()
        self._caption_session = None
        self._caption_revision = 0
        self._caption_rows = {}
        self._speech_lock = threading.Lock()
        self._speech_stop = None
        self._speech_error = ""
        try:
            _, data = win32crypt.CryptUnprotectData(self._settings_path.read_bytes(), None, None, None, 1)
            saved = json.loads(data)
            self.key = saved["key"]
            if saved.get("overlay_style") in OVERLAY_OPACITY:
                self._overlay_style = saved["overlay_style"]
            if saved.get("overlay_mode") in ("bilingual", "translation"):
                self._overlay_mode = saved["overlay_mode"]
            if saved.get("overlay_font_size") in OVERLAY_FONT_SIZES:
                self._overlay_font_size = saved["overlay_font_size"]
            if saved.get("overlay_font_family") in OVERLAY_FONTS:
                self._overlay_font_family = saved["overlay_font_family"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError, pywintypes.error):
            self._settings_error = "无法读取已保存的连接设置，请重新保存。"

    def settings(self):
        return {"has_key": bool(self.key)}

    def save_settings(self, key):
        secret = self.key if key is None else key.strip()
        if not self._write_settings(secret, self._overlay_style, self._overlay_mode,
                                    self._overlay_font_size, self._overlay_font_family):
            return {"error": "无法安全保存连接设置。"}
        self.key = secret
        return self.settings()

    def _write_settings(self, secret, style, mode, font_size, font_family):
        temporary = None
        try:
            data = json.dumps({"key": secret,
                               "overlay_style": style, "overlay_mode": mode,
                               "overlay_font_size": font_size,
                               "overlay_font_family": font_family}).encode("utf-8")
            encrypted = win32crypt.CryptProtectData(data, "VtoH", None, None, None, 1)
            self._settings_path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=self._settings_path.parent, delete=False) as file:
                temporary = Path(file.name)
                file.write(encrypted)
            temporary.replace(self._settings_path)
        except (OSError, pywintypes.error):
            return False
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink(missing_ok=True)
        return True

    def overlay_style(self):
        return self._overlay_style

    def set_overlay_style(self, style):
        if style not in OVERLAY_OPACITY:
            return False
        if not self._write_settings(self.key, style, self._overlay_mode,
                                    self._overlay_font_size, self._overlay_font_family):
            return False
        self._overlay_style = style
        self._apply_overlay_opacity()
        return True

    def overlay_mode(self):
        return self._overlay_mode

    def set_overlay_mode(self, mode):
        if mode not in ("bilingual", "translation"):
            return False
        if not self._write_settings(self.key, self._overlay_style, mode,
                                    self._overlay_font_size, self._overlay_font_family):
            return False
        self._overlay_mode = mode
        self.stop_caption_speech()
        return True

    def overlay_font(self):
        return {"size": self._overlay_font_size, "family": self._overlay_font_family}

    def set_overlay_font(self, size, family):
        if int(size) not in OVERLAY_FONT_SIZES or family not in OVERLAY_FONTS:
            return False
        if not self._write_settings(self.key, self._overlay_style, self._overlay_mode,
                                    int(size), family):
            return False
        self._overlay_font_size = int(size)
        self._overlay_font_family = family
        return True

    def _apply_overlay_opacity(self):
        if self._overlay is None or self._overlay.native is None:
            return
        from System import Action

        form = self._overlay.native
        opacity = OVERLAY_OPACITY[self._overlay_style]
        form.Invoke(Action(lambda: setattr(form, "Opacity", opacity)))

    def _set_overlay_visible(self, visible):
        if self._overlay is not None and visible != self._overlay_open:
            if visible:
                self._overlay.show()
                self._apply_overlay_opacity()
            else:
                self.stop_caption_speech()
                self._overlay.hide()
            self._overlay_open = visible
        return self._overlay_open

    def toggle_overlay(self):
        with self._overlay_lock:
            return self._set_overlay_visible(not self._overlay_open)

    def hide_overlay(self):
        with self._overlay_lock:
            return self._set_overlay_visible(False)

    def _set_phase(self, phase):
        with self._state_lock:
            self._phase = phase

    def _capture_ready(self):
        with self._state_lock:
            if self._phase == "starting":
                self._phase = "live"

    def workspace_status(self):
        with self._state_lock:
            return {"phase": self._phase, "overlay_open": self._overlay_open}

    def _record_caption(self, event):
        kind = event["type"]
        if kind not in ("session", "snapshot", "transcript"):
            return
        with self._caption_lock:
            if kind == "session":
                if event["id"] != self._caption_session:
                    self.stop_caption_speech()
                    self._caption_session = event["id"]
                    self._caption_rows.clear()
                    self._caption_revision += 1
                return
            for row in event["transcripts"] if kind == "snapshot" else (event,):
                self._caption_revision += 1
                self._caption_rows[row["id"]] = (self._caption_revision, row)

    def overlay_caption(self, revision=-1, session=None):
        with self._caption_lock:
            reset = session != self._caption_session
            rows = []
            if reset or revision != self._caption_revision:
                rows = [row for updated, row in self._caption_rows.values() if reset or updated > revision]
            history = {"transcripts": rows, "session": self._caption_session,
                       "revision": self._caption_revision, "reset": reset}
        return {**history, **self.workspace_status()}

    def caption_speech_status(self):
        with self._speech_lock:
            return {"speaking": self._speech_stop is not None, "error": self._speech_error}

    def stop_caption_speech(self):
        with self._speech_lock:
            if self._speech_stop is not None:
                self._speech_stop.set()
            self._speech_error = ""

    def speak_caption(self, identifier):
        if not isinstance(identifier, str):
            return
        with self._caption_lock:
            row = self._caption_rows.get(identifier)
            if row is None:
                return
            caption = row[1]
            translation = caption.get("translation", "").strip()
            text = translation or (caption["text"].strip() if self._overlay_mode == "bilingual" else "")
            if not text:
                return
            language = caption.get("translation_language", "") if translation else ""
            with self._speech_lock:
                if self._speech_stop is not None:
                    return
                stop = self._speech_stop = threading.Event()
                self._speech_error = ""
        threading.Thread(target=self._play_caption, args=(text, language, stop), daemon=True).start()

    def _play_caption(self, text, language, stop):
        error = ""
        voice = None
        pythoncom.CoInitialize()
        try:
            if stop.is_set():
                return
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            if not language:
                if any("\u3040" <= char <= "\u30ff" for char in text):
                    language = "ja"
                elif any("\uac00" <= char <= "\ud7af" for char in text):
                    language = "ko"
                elif any("\u3400" <= char <= "\u9fff" for char in text):
                    language = "zh"
            if language:
                for token in voice.GetVoices():
                    names = (locale.windows_locale.get(int(code, 16), "").split("_")[0]
                             for code in token.GetAttribute("Language").split(";"))
                    if language in names:
                        voice.Voice = token
                        break
                else:
                    error = "请在 Windows 设置中安装对应的朗读语音。"
                    return
            voice.Speak(text, 1 | 16)  # Asynchronous and plain text.
            while not voice.WaitUntilDone(50):
                if stop.is_set():
                    voice.Speak("", 1 | 2 | 16)  # Purge the current utterance.
                    break
        except (pywintypes.com_error, ValueError):
            error = "无法使用 Windows 朗读语音。"
        finally:
            voice = None
            pythoncom.CoUninitialize()
            with self._speech_lock:
                self._speech_stop = None
                self._speech_error = "" if stop.is_set() else error

    def resize_overlay(self, width, height):
        if self._overlay is not None and self._overlay_open:
            self._overlay.resize(max(360, min(1200, int(width))), max(115, min(440, int(height))))

    def overlay_toggle_capture(self):
        if self.workspace_status()["phase"] != "idle":
            self.stop_capture()
        else:
            self.events.put({"type": "toggle_capture"})

    def _hotkeys(self):
        user32 = ctypes.windll.user32
        actions = {1: ("toggle_capture", 0x78), 2: ("toggle_overlay", 0x79)}
        registered = []
        for identifier, (action, key) in actions.items():
            if user32.RegisterHotKey(None, identifier, 0x4003, key):
                registered.append(identifier)
            else:
                label = "F9" if action == "toggle_capture" else "F10"
                self.events.put({"type": "error", "message": f"无法注册 Ctrl + Alt + {label}，请检查快捷键是否被占用。"})
        message = wintypes.MSG()
        try:
            while registered and user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == 0x0312:
                    if message.wParam == 1:
                        self.overlay_toggle_capture()
                    elif message.wParam == 2:
                        self.events.put({"type": "toggle_overlay"})
        finally:
            for identifier in registered:
                user32.UnregisterHotKey(None, identifier)

    def sources(self):
        try:
            output = subprocess.check_output(
                [str(CAPTURE), "list"], timeout=4, creationflags=subprocess.CREATE_NO_WINDOW,
            ).decode("utf-8")
            sources = []
            for line in output.splitlines():
                pid, name = line.split("\t", 1)
                sources.append({"id": pid, "label": "默认播放设备" if pid == "0" else f"{name} · {pid}"})
            return {"sources": sources}
        except (OSError, subprocess.SubprocessError, ValueError):
            return {"error": "无法读取 Windows 播放音源。"}

    def start_capture(self, pid, target):
        if self.workspace_status()["phase"] != "idle":
            return {"error": "已在监听。"}
        if not self.key:
            return {"settings_required": True}
        if not isinstance(pid, str) or not pid.isdecimal() or target not in TRANSLATION_OPTIONS.values():
            return {"error": "请选择有效的音源和目标语言。"}
        if self.thread is not None and self.thread.is_alive():
            self.commands.put(None)
            self.thread.join(2)
            if self.thread.is_alive():
                return {"error": "上次监听正在结束，请稍候再试。"}
        key = self.key
        with self._state_lock:
            if self._phase != "idle":
                return {"error": "已在监听。"}
            self.stop_signal.clear()
            self._phase = "starting"

        def work():
            try:
                asyncio.run(self.connection.run(SERVER_URL, key, pid, target))
            except Exception as failure:
                while isinstance(failure, BaseExceptionGroup):
                    failure = failure.exceptions[0]
                if isinstance(failure, InvalidStatus):
                    if failure.response.status_code == 403:
                        self.connection.session = None
                    message = f"WebSocket 连接失败（HTTP {failure.response.status_code}）"
                elif isinstance(failure, httpx.HTTPStatusError) and failure.response.status_code == 401:
                    message = "访问密钥有误，请在设置中检查。"
                elif isinstance(failure, RuntimeError):
                    message = str(failure)
                else:
                    message = "连接中断，请检查网络和播放音源。"
                self.events.put({"type": "error", "message": message})
            finally:
                self.stop_signal.set()
                self._set_phase("idle")
                self.events.put({"type": "finished"})

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        return {"started": True}

    def stop_capture(self):
        with self._state_lock:
            if self._phase in ("starting", "live"):
                self._phase = "stopping"
                self.stop_signal.set()
            return self._phase

    def answer(self, ids):
        if self.thread is None or not self.thread.is_alive():
            return {"error": "请重新开始监听后再回答。"}
        if not isinstance(ids, list) or not 1 <= len(ids) <= 8 or not all(isinstance(item, str) for item in ids):
            return {"error": "请选择 1 至 8 条定稿字幕。"}
        self.commands.put({"type": "answer", "ids": ids})
        return {"ok": True}

    def poll(self):
        batch = []
        while len(batch) < 120:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event["type"] == "pair":
                import base64
                from io import BytesIO

                png = BytesIO()
                qrcode.make(event["url"]).save(png, format="PNG")
                event["image"] = "data:image/png;base64," + base64.b64encode(png.getvalue()).decode("ascii")
            batch.append(event)
        return batch

    def poll_workspace(self):
        return {"events": self.poll(), **self.workspace_status()}


if __name__ == "__main__":
    import webview

    bridge = DesktopBridge()
    asset_root = Path(getattr(sys, "_MEIPASS", ROOT))
    window = webview.create_window(
        "VtoH · 字幕工作台", str(asset_root / "desktop_ui" / "index.html"),
        js_api=bridge, width=960, height=620, min_size=(640, 480),
        background_color="#f5f4ee",
    )
    bridge._overlay = webview.create_window(
        "VtoH · 悬浮字幕", str(asset_root / "desktop_ui" / "overlay.html"),
        js_api=bridge, width=680, height=152, min_size=(360, 115), x=80, y=12,
        on_top=True, hidden=True, frameless=True, easy_drag=False, background_color="#101010",
    )

    def shutdown():
        bridge.stop_caption_speech()
        bridge.stop_capture()
        bridge._overlay.destroy()

    window.events.closing += shutdown
    if bridge._settings_error:
        bridge.events.put({"type": "error", "message": bridge._settings_error})
    threading.Thread(target=bridge._hotkeys, daemon=True).start()
    webview.start(gui="edgechromium")
