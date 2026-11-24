import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from time import monotonic
from pathlib import Path
from tkinter import ttk
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
import qrcode
from PIL import ImageTk
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

ROOT = Path(sys.executable if getattr(sys, "frozen", False) else __file__).parent
SERVER_PREFIX = "/vtoh"
DEFAULT_SERVER_URL = "https://www.ploverjoe.xyz/vtoh/"
TRANSLATION_OPTIONS = {
    "关闭翻译": "", "简体中文": "zh", "英语": "en", "日语": "ja", "韩语": "ko",
    "法语": "fr", "西班牙语": "es", "德语": "de",
}
CAPTURE = ROOT / "vtoh_capture.exe"
if not CAPTURE.is_file():
    CAPTURE = ROOT / ".tools" / "bin" / "vtoh_capture.exe"


def server_base(raw: str) -> str:
    url = urlsplit(raw.strip())
    if (url.scheme not in ("https", "http") or not url.hostname
            or url.path.rstrip("/") not in ("", SERVER_PREFIX)
            or url.query or url.fragment or url.username is not None):
        raise ValueError("服务地址格式有误")
    return urlunsplit((url.scheme, url.netloc, SERVER_PREFIX, "", ""))


def endpoint(base: str, path: str, token: str, target: str = "") -> str:
    url = urlsplit(base)
    query = {"token": token}
    if target:
        query["translate"] = target
    return urlunsplit(("wss" if url.scheme == "https" else "ws", url.netloc,
                        url.path.rstrip("/") + path, urlencode(query), ""))


class Connection:
    def __init__(self, events: queue.Queue, stop: threading.Event, commands: queue.Queue):
        self.events = events
        self.stop = stop
        self.commands = commands
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
        session = self.session
        path = f"/sessions/{session['id']}"
        pair = f"{base}/pair#" + urlencode({"id": session["id"], "token": session["viewer"]})
        self.events.put({"type": "pair", "url": pair})
        restart = asyncio.Event()
        async with connect(endpoint(base, f"{path}/control", session["owner"])) as control:
            async def receive():
                async for raw in control:
                    self.events.put(json.loads(raw))

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
                    self.events.put({"type": "status", "message": "已停止监听，可选择定稿字幕回答"})
                    restart_wait = group.create_task(restart.wait())
                    await asyncio.wait((listener, restart_wait), return_when=asyncio.FIRST_COMPLETED)
                    restart_wait.cancel()
                finally:
                    listener.cancel()
                    sender.cancel()

    async def capture(self, base: str, path: str, token: str, pid: str, target: str) -> None:
        async with connect(endpoint(base, f"{path}/audio", token, target)) as audio:
            if json.loads(await asyncio.wait_for(audio.recv(), timeout=10))["type"] != "ready":
                raise RuntimeError("服务端未就绪")
            process = await asyncio.create_subprocess_exec(
                str(CAPTURE), "capture", pid,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
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
                await audio.send('{"type":"stop"}')

            async def receive_result():
                if json.loads(await audio.recv()).get("type") != "stopped":
                    raise RuntimeError("语音识别未正常结束")

            sender = asyncio.create_task(send_frames())
            receiver = asyncio.create_task(receive_result())
            try:
                await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
                if sender.done():
                    sender.result()
                    await asyncio.wait_for(receiver, timeout=8)
                else:
                    receiver.result()
            finally:
                for task in (sender, receiver):
                    task.cancel()
                await asyncio.gather(sender, receiver, return_exceptions=True)
                if process.returncode is None:
                    try:
                        process.stdin.write(b"stop")
                        await process.stdin.drain()
                        await asyncio.wait_for(process.communicate(), timeout=2)
                    except (BrokenPipeError, ConnectionResetError, TimeoutError):
                        if process.returncode is None:
                            process.kill()
                            await process.wait()


class Desktop:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("VtoH · 实时问答")
        self.root.geometry("1120x740")
        self.root.minsize(760, 575)
        background = "#f2f5f3"
        ink = "#172922"
        muted = "#51645a"
        accent = "#236349"
        border = "#d7e1db"
        self.root.configure(bg=background)
        self.events = queue.Queue()
        self.commands = queue.Queue()
        self.stop = threading.Event()
        self.connection = Connection(self.events, self.stop, self.commands)
        self.thread = None
        self.rows = {}
        self.answers = {}
        self.sources = {}
        self.restarting = False
        self.pair_url = ""
        self.last_sound = 0
        self.capture_status = tk.StringVar(value="待机")
        self.status = tk.StringVar()
        self.server = tk.StringVar(value=os.getenv("VTOH_SERVER_URL", DEFAULT_SERVER_URL))
        self.key = tk.StringVar(value=os.getenv("VTOH_ACCESS_KEY", ""))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10))
        style.configure("TFrame", background=background)
        style.configure("TLabel", background=background, foreground=ink)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 23, "bold"))
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("Muted.TLabel", foreground=muted)
        style.configure("Status.TLabel", background="#e2eee7", foreground=accent, padding=(12, 6))
        style.configure("TButton", padding=(12, 7))
        style.configure("TEntry", padding=6)
        style.configure("TCombobox", padding=6)
        style.configure("Accent.TButton", background=accent, foreground="white", padding=(18, 8))
        style.map("Accent.TButton", background=[("disabled", "#82958a"), ("active", "#1b513b")])
        style.configure("Horizontal.TProgressbar", background=accent, troughcolor="#dce6df", thickness=7)
        style.configure("Panel.TFrame", background="#e7eeea")
        style.configure("Panel.TLabel", background="#e7eeea", font=("Microsoft YaHei UI", 13, "bold"))

        layout = ttk.Frame(root, padding=24)
        layout.pack(fill="both", expand=True)
        header = ttk.Frame(layout)
        header.pack(fill="x")
        ttk.Label(header, text="VtoH", style="Title.TLabel").pack(side="left")
        self.settings_button = ttk.Button(header, text="设置", command=self.show_settings)
        self.settings_button.pack(side="right", padx=(8, 0))
        self.pair_button = ttk.Button(header, text="手机配对", command=self.show_pair, state="disabled")
        self.pair_button.pack(side="right")
        ttk.Label(header, textvariable=self.capture_status, style="Status.TLabel").pack(side="right", padx=12)

        ttk.Separator(layout).pack(fill="x", pady=(16, 0))
        settings = ttk.Frame(layout, padding=(0, 14, 0, 0))
        settings.pack(fill="x")
        settings.columnconfigure(0, weight=1)
        self.source = ttk.Combobox(settings, state="readonly", width=20)
        self.target = ttk.Combobox(settings, state="readonly", width=16, values=list(TRANSLATION_OPTIONS))
        self.target.current(0)
        for column, (label, widget) in enumerate((("播放音源", self.source), ("翻译为", self.target))):
            ttk.Label(settings, text=label, style="Muted.TLabel").grid(row=0, column=column, sticky="w", padx=(0, 10), pady=(0, 5))
            widget.grid(row=1, column=column, sticky="ew", padx=(0, 10))
        self.refresh_button = ttk.Button(settings, text="刷新音源", command=self.refresh)
        self.refresh_button.grid(row=1, column=2, padx=(0, 8))
        self.toggle = ttk.Button(settings, text="开始监听", style="Accent.TButton", command=self.toggle_capture)
        self.toggle.grid(row=1, column=3)
        meter = ttk.Frame(layout)
        meter.pack(fill="x", pady=(9, 0))
        ttk.Label(meter, text="输入电平", style="Muted.TLabel").pack(side="left", padx=(0, 12))
        self.level = ttk.Progressbar(meter, maximum=20000)
        self.level.pack(side="left", fill="x", expand=True)
        ttk.Label(layout, textvariable=self.status, style="Muted.TLabel").pack(fill="x", pady=(2, 10))

        body = ttk.Frame(layout)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=7, uniform="panels")
        body.columnconfigure(1, weight=5, uniform="panels")
        body.rowconfigure(0, weight=1)
        transcript_panel = self._panel(body, "字幕", 0, (0, 9))
        answers_panel = self._panel(body, "问答", 1, (9, 0))
        self.manual_button = ttk.Button(
            transcript_panel, text="回答这段", command=self.manual, state="disabled"
        )
        self.manual_button.grid(row=0, column=1, sticky="e", pady=(0, 8))
        self.tabs = ttk.Notebook(transcript_panel)
        self.tabs.grid(row=1, column=0, columnspan=2, sticky="nsew")
        originals = ttk.Frame(self.tabs, padding=4)
        translations = ttk.Frame(self.tabs, padding=4)
        for page in (originals, translations):
            page.columnconfigure(0, weight=1)
            page.rowconfigure(1, weight=1)
        self.tabs.add(originals, text="原文")
        self.tabs.add(translations, text="双语翻译")
        self.tabs.bind("<<NotebookTabChanged>>", lambda _: self._update_manual())
        self.subtitles = tk.Listbox(
            originals, selectmode="extended", exportselection=False, width=1,
            bg="white", fg=ink, selectbackground="#dcece3", selectforeground=ink,
            font=("Microsoft YaHei UI", 12), highlightthickness=1,
            highlightbackground=border, highlightcolor=accent, relief="flat",
        )
        self.subtitles.bind("<<ListboxSelect>>", lambda _: self._update_manual())
        self._attach_scrollbar(originals, self.subtitles)
        horizontal = ttk.Scrollbar(originals, orient="horizontal", command=self.subtitles.xview)
        horizontal.grid(row=2, column=0, sticky="ew")
        self.subtitles.configure(xscrollcommand=horizontal.set)
        self.transcript_empty = tk.Label(
            originals, text="开始监听后，字幕会显示在这里。",
            bg="white", fg=muted, justify="left", padx=14, pady=14,
        )
        self.translation_box = tk.Text(
            translations, wrap="word", width=1, bg="white", fg=ink,
            font=("Microsoft YaHei UI", 12), padx=15, pady=12, relief="flat",
            highlightthickness=1, highlightbackground=border, highlightcolor=accent,
        )
        self.translation_box.tag_configure("original", foreground=muted, spacing1=8)
        self.translation_box.tag_configure("translated", foreground=accent, spacing3=8)
        self._attach_scrollbar(translations, self.translation_box)
        self.answer_box = tk.Text(
            answers_panel, wrap="word", state="disabled", width=1, bg="white", fg=ink,
            font=("Microsoft YaHei UI", 12), padx=16, pady=14, spacing3=4,
            highlightthickness=1, highlightbackground=border, highlightcolor=accent, relief="flat",
        )
        self.answer_box.tag_configure("question", font=("Microsoft YaHei UI", 13, "bold"), spacing1=6)
        self.answer_box.tag_configure("point", lmargin1=8, lmargin2=26)
        self.answer_box.tag_configure("empty", foreground=muted)
        self._attach_scrollbar(answers_panel, self.answer_box)
        self._render_transcripts()
        self._render_answers()
        self.refresh()
        self.root.after(65, self.poll)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    @staticmethod
    def _panel(parent, title, column, horizontal_padding):
        panel = ttk.Frame(parent, padding=12, style="Panel.TFrame")
        panel.grid(row=0, column=column, sticky="nsew", padx=horizontal_padding)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=1)
        ttk.Label(panel, text=title, style="Panel.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        return panel

    @staticmethod
    def _attach_scrollbar(panel, widget):
        scrollbar = ttk.Scrollbar(panel, orient="vertical", command=widget.yview)
        widget.configure(yscrollcommand=scrollbar.set)
        widget.grid(row=1, column=0, sticky="nsew")
        scrollbar.grid(row=1, column=1, sticky="ns")

    def _selected_transcripts(self):
        rows = list(self.rows.values())
        return [rows[i]["id"] for i in self.subtitles.curselection() if rows[i]["final"]]

    def _update_manual(self):
        enabled = (self.tabs.index("current") == 0 and self.thread is not None
                   and self.thread.is_alive() and 1 <= len(self._selected_transcripts()) <= 8)
        self.manual_button.configure(state="normal" if enabled else "disabled")

    def _render_transcripts(self, selected=()):
        self.subtitles.delete(0, "end")
        for index, row in enumerate(self.rows.values()):
            self.subtitles.insert("end", row["text"] if row["final"] else f'转写中 · {row["text"]}')
            if row["id"] in selected:
                self.subtitles.selection_set(index)
        if self.rows:
            self.transcript_empty.grid_remove()
            if not selected:
                self.subtitles.see("end")
        else:
            self.transcript_empty.grid(row=1, column=0, sticky="nw", padx=2, pady=2)
        self._update_manual()
        self._render_translations()

    def _render_translations(self):
        translations = [row for row in self.rows.values() if row.get("translation_status")]
        self.translation_box.configure(state="normal")
        self.translation_box.delete("1.0", "end")
        if not translations:
            self.translation_box.insert("end", "选择目标语言后，定稿字幕会在这里显示译文。", "original")
        for row in reversed(translations):
            self.translation_box.insert("end", row["text"] + "\n", "original")
            status = "正在翻译…" if row["translation_status"] == "pending" else "暂无法翻译"
            self.translation_box.insert("end", row.get("translation", status) + "\n", "translated")
        self.translation_box.configure(state="disabled")

    def show_settings(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("VtoH · 设置")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()
        content = ttk.Frame(dialog, padding=24)
        content.pack(fill="both", expand=True)
        ttk.Label(content, text="连接设置", style="Section.TLabel").pack(anchor="w", pady=(0, 16))
        address = tk.StringVar(value=self.server.get())
        key = tk.StringVar(value=self.key.get())
        ttk.Label(content, text="服务地址").pack(anchor="w")
        field = ttk.Entry(content, textvariable=address, width=52)
        field.pack(fill="x", pady=(4, 12))
        ttk.Label(content, text="访问密钥").pack(anchor="w")
        ttk.Entry(content, textvariable=key, show="•").pack(fill="x", pady=(4, 8))
        error = tk.StringVar()
        ttk.Label(content, textvariable=error, foreground="#8d3028").pack(anchor="w")
        actions = ttk.Frame(content)
        actions.pack(fill="x", pady=(12, 0))

        def save():
            try:
                self.server.set(server_base(address.get()) + "/")
            except ValueError as failure:
                error.set(str(failure))
                return
            self.key.set(key.get().strip())
            self.status.set("")
            dialog.destroy()

        ttk.Button(actions, text="保存", style="Accent.TButton", command=save).pack(side="right")
        ttk.Button(actions, text="取消", command=dialog.destroy).pack(side="right", padx=(0, 8))
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        field.focus_set()

    def _capture_controls(self, active):
        self.settings_button.configure(state="disabled" if active else "normal")
        self.refresh_button.configure(state="disabled" if active else "normal")
        for control in (self.source, self.target):
            control.configure(state="disabled" if active else "readonly")

    def _render_answers(self):
        self.answer_box.configure(state="normal")
        self.answer_box.delete("1.0", "end")
        if not self.answers:
            self.answer_box.insert("end", "等待完整的问题。", "empty")
        for answer in reversed(self.answers.values()):
            self.answer_box.insert("end", f'{answer["question"]}\n', "question")
            self.answer_box.insert("end", f'{answer["conclusion"]}\n')
            for point in answer["points"]:
                self.answer_box.insert("end", f"• {point}\n", "point")
            self.answer_box.insert("end", "\n")
        self.answer_box.configure(state="disabled")

    def refresh(self):
        try:
            output = subprocess.check_output([str(CAPTURE), "list"], timeout=4,
                                             creationflags=subprocess.CREATE_NO_WINDOW).decode("utf-8")
            names = {}
            for line in output.splitlines():
                pid, name = line.split("\t", 1)
                names["默认播放设备" if pid == "0" else f"{name}  ·  {pid}"] = pid
            old = self.source.get()
            self.sources = names
            self.source["values"] = list(names)
            self.source.set(old if old in names else next(iter(names), ""))
        except (OSError, subprocess.SubprocessError, ValueError):
            self.status.set("无法读取 WASAPI 音源")

    def toggle_capture(self):
        if self.thread and self.thread.is_alive() and not self.stop.is_set():
            self.stop.set()
            self.capture_status.set("正在停止")
            self.toggle.configure(text="正在停止", state="disabled")
            return
        pid = self.sources.get(self.source.get())
        try:
            base = server_base(self.server.get())
        except ValueError:
            self.status.set("请填写有效的服务地址")
            return
        if pid is None:
            self.status.set("请填写服务地址并选择音源")
            return
        if self.thread and self.thread.is_alive():
            if not self.restarting:
                self.restarting = True
                self.commands.put(None)
            self.root.after(200, self.toggle_capture)
            return
        self.restarting = False
        key = self.key.get()
        if not key and server_base(DEFAULT_SERVER_URL) == base:
            self.show_settings()
            return
        target = TRANSLATION_OPTIONS[self.target.get()]
        if target:
            self.tabs.select(1)
        self._capture_controls(True)
        self.stop.clear()
        self.toggle.configure(text="停止监听")
        self.capture_status.set("正在连接")
        self.status.set("")
        self.last_sound = monotonic()

        def work():
            try:
                asyncio.run(self.connection.run(base, key, pid, target))
            except Exception as failure:
                while isinstance(failure, BaseExceptionGroup):
                    failure = failure.exceptions[0]
                if isinstance(failure, InvalidStatus) and failure.response.status_code == 403:
                    self.connection.session = None
                if isinstance(failure, httpx.HTTPStatusError) and failure.response.status_code == 401:
                    message = "访问密钥有误"
                elif isinstance(failure, InvalidStatus) and failure.response.status_code in (401, 403):
                    message = "访问密钥有误，或 Nginx 没有转发 WebSocket"
                elif isinstance(failure, InvalidStatus):
                    message = f"WebSocket 连接失败（HTTP {failure.response.status_code}）"
                elif isinstance(failure, RuntimeError) and not str(failure).startswith(("Task", "Cannot")):
                    message = str(failure)
                else:
                    message = "请检查服务地址、网络连接或重新选择音源"
                self.events.put({"type": "error", "message": f"监听中断 · {message}"})
            finally:
                self.stop.set()
                self.events.put({"type": "finished"})

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()

    def manual(self):
        ids = self._selected_transcripts()
        if 1 <= len(ids) <= 8:
            if self.thread and self.thread.is_alive():
                self.commands.put({"type": "answer", "ids": ids})
            else:
                self.status.set("连接已断开，请重新开始监听")
        else:
            self.status.set("请选择 1 至 8 条已定稿字幕")

    def show_pair(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("手机配对")
        dialog.resizable(False, False)
        image = ImageTk.PhotoImage(qrcode.make(self.pair_url).convert("RGB").resize((272, 272)))
        label = ttk.Label(dialog, image=image)
        label.image = image
        label.pack(padx=20, pady=(18, 6))
        ttk.Label(dialog, text="用手机相机或客户端扫描").pack(pady=(0, 12))

        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(self.pair_url)

        ttk.Button(dialog, text="复制配对链接", command=copy).pack(pady=(0, 18))

    def poll(self):
        while not self.events.empty():
            event = self.events.get_nowait()
            kind = event["type"]
            selected = self._selected_transcripts() if kind in ("snapshot", "transcript") else ()
            if kind == "snapshot":
                self.rows = {item["id"]: item for item in event["transcripts"]}
                self.answers = {item["id"]: item for item in event["answers"]}
            elif kind == "transcript":
                self.rows[event["id"]] = event
                if len(self.rows) > 250:
                    del self.rows[next(iter(self.rows))]
            elif kind == "answer":
                self.answers[event["id"]] = event
                if len(self.answers) > 120:
                    del self.answers[next(iter(self.answers))]
            elif kind == "level":
                self.level["value"] = event["peak"]
                if not self.stop.is_set():
                    self.capture_status.set("正在监听")
                if event["peak"] > 200:
                    self.last_sound = monotonic()
                silence_hint = "持续无声，请检查播放音源"
                if not self.stop.is_set() and monotonic() - self.last_sound > 4 and not self.status.get():
                    self.status.set(silence_hint)
                elif event["peak"] > 200 and self.status.get() == silence_hint:
                    self.status.set("")
            elif kind in ("error", "status"):
                self.status.set(event["message"])
                if kind == "status":
                    self._capture_controls(False)
                    self.capture_status.set("已停止")
                    self.toggle.configure(text="开始监听", state="normal")
                    self.level["value"] = 0
            elif kind == "pair":
                self.pair_url = event["url"]
                self.pair_button.configure(state="normal")
            elif kind == "finished":
                self._capture_controls(False)
                self.capture_status.set("待机")
                self.toggle.configure(text="开始监听", state="normal")
                self.level["value"] = 0
                self.manual_button.configure(state="disabled")
            if kind in ("snapshot", "transcript"):
                self._render_transcripts(selected)
            if kind in ("snapshot", "answer"):
                self._render_answers()
        self.root.after(65, self.poll)

    def close(self):
        self.stop.set()
        self.commands.put(None)
        self.root.destroy()


if __name__ == "__main__":
    window = tk.Tk()
    Desktop(window)
    window.mainloop()
