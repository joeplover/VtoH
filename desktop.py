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

ROOT = Path(sys.executable if getattr(sys, "frozen", False) else __file__).parent
SERVER_PREFIX = "/vtoh"
DEFAULT_SERVER_URL = "https://www.ploverjoe.xyz/vtoh/"
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


def endpoint(base: str, path: str, token: str) -> str:
    url = urlsplit(base)
    return urlunsplit(("wss" if url.scheme == "https" else "ws", url.netloc,
                        url.path.rstrip("/") + path, urlencode({"token": token}), ""))


class Connection:
    def __init__(self, events: queue.Queue, stop: threading.Event, commands: queue.Queue):
        self.events = events
        self.stop = stop
        self.commands = commands
        self.session = None
        self.base = ""

    async def run(self, base: str, key: str, pid: str) -> None:
        if self.session is None or self.base != base:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(f"{base}/sessions", headers={"Authorization": f"Bearer {key}"})
                response.raise_for_status()
                self.session = response.json()
                self.base = base
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
                    await self.capture(base, path, session["owner"], pid)
                    self.events.put({"type": "status", "message": "已停止监听，可选择定稿字幕回答"})
                    restart_wait = group.create_task(restart.wait())
                    await asyncio.wait((listener, restart_wait), return_when=asyncio.FIRST_COMPLETED)
                    restart_wait.cancel()
                finally:
                    listener.cancel()
                    sender.cancel()

    async def capture(self, base: str, path: str, token: str, pid: str) -> None:
        async with connect(endpoint(base, f"{path}/audio", token)) as audio:
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
        self.root.geometry("1050x690")
        self.root.minsize(760, 560)
        self.root.configure(bg="#f5f8f5")
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

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", font=("Microsoft YaHei UI", 10))
        style.configure("TFrame", background="#f5f8f5")
        style.configure("TLabel", background="#f5f8f5", foreground="#192f26")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 24, "bold"))
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("Accent.TButton", background="#236349", foreground="white", padding=(16, 8))
        style.map("Accent.TButton", background=[("disabled", "#82958a"), ("active", "#1b513b")])

        layout = ttk.Frame(root, padding=24)
        layout.pack(fill="both", expand=True)
        header = ttk.Frame(layout)
        header.pack(fill="x")
        ttk.Label(header, text="实时问答", style="Title.TLabel").pack(side="left")
        self.status = tk.StringVar(value="选择音源后开始监听")
        ttk.Label(header, textvariable=self.status).pack(side="right")

        settings = ttk.Frame(layout, padding=(0, 18, 0, 14))
        settings.pack(fill="x")
        settings.columnconfigure(0, weight=1)
        settings.columnconfigure(1, weight=1)
        self.server = ttk.Entry(settings)
        self.server.insert(0, os.getenv("VTOH_SERVER_URL", DEFAULT_SERVER_URL))
        self.key = ttk.Entry(settings, show="•")
        self.key.insert(0, os.getenv("VTOH_ACCESS_KEY", ""))
        self.source = ttk.Combobox(settings, state="readonly", width=25)
        for column, (label, widget) in enumerate((("服务地址", self.server), ("访问密钥", self.key), ("播放音源", self.source))):
            ttk.Label(settings, text=label).grid(row=0, column=column, sticky="w", padx=(0, 10), pady=(0, 5))
            widget.grid(row=1, column=column, sticky="ew", padx=(0, 10))
        ttk.Button(settings, text="刷新音源", command=self.refresh).grid(row=1, column=3, padx=(0, 8))
        self.toggle = ttk.Button(settings, text="开始监听", style="Accent.TButton", command=self.toggle_capture)
        self.toggle.grid(row=1, column=4)
        self.level = ttk.Progressbar(layout, maximum=20000)
        self.level.pack(fill="x", pady=(0, 18))

        body = ttk.Frame(layout)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=4)
        body.columnconfigure(1, weight=3)
        body.rowconfigure(1, weight=1)
        ttk.Label(body, text="实时字幕", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(body, text="答案", style="Section.TLabel").grid(row=0, column=1, sticky="w")
        self.subtitles = tk.Listbox(body, selectmode="extended", exportselection=False,
                                   bg="white", fg="#192f26", font=("Microsoft YaHei UI", 12),
                                   highlightthickness=1, highlightbackground="#d4ded6", relief="flat")
        self.subtitles.grid(row=1, column=0, sticky="nsew", padx=(0, 16), pady=8)
        self.answer_box = tk.Text(body, wrap="word", state="disabled", bg="white", fg="#192f26",
                                  font=("Microsoft YaHei UI", 12), padx=14, pady=12,
                                  highlightthickness=1, highlightbackground="#d4ded6", relief="flat")
        self.answer_box.grid(row=1, column=1, sticky="nsew", pady=8)
        ttk.Button(body, text="回答这段", command=self.manual).grid(row=2, column=0, sticky="w")
        self.pair_button = ttk.Button(body, text="手机配对", command=self.show_pair, state="disabled")
        self.pair_button.grid(row=2, column=1, sticky="e")
        self.refresh()
        self.root.after(65, self.poll)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

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
        self.stop.clear()
        self.toggle.configure(text="停止监听")
        self.status.set("正在连接服务")
        self.last_sound = monotonic()

        def work():
            try:
                asyncio.run(self.connection.run(base, key, pid))
            except Exception as failure:
                while isinstance(failure, BaseExceptionGroup):
                    failure = failure.exceptions[0]
                if isinstance(failure, httpx.HTTPStatusError) and failure.response.status_code == 401:
                    message = "访问密钥有误"
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
        rows = list(self.rows.values())
        ids = [rows[i]["id"] for i in self.subtitles.curselection() if rows[i]["final"]]
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
                if event["peak"] > 200:
                    self.last_sound = monotonic()
                    self.status.set("正在监听")
                elif monotonic() - self.last_sound > 4:
                    self.status.set("持续无声，请检查播放音源")
            elif kind in ("error", "status"):
                self.status.set(event["message"])
                if kind == "status":
                    self.toggle.configure(text="开始监听", state="normal")
                    self.level["value"] = 0
            elif kind == "pair":
                self.pair_url = event["url"]
                self.pair_button.configure(state="normal")
            elif kind == "finished":
                self.toggle.configure(text="开始监听", state="normal")
                self.level["value"] = 0
            if kind in ("snapshot", "transcript"):
                chosen = self.subtitles.curselection()
                self.subtitles.delete(0, "end")
                for row in self.rows.values():
                    self.subtitles.insert("end", ("" if row["final"] else "… ") + row["text"])
                for i in chosen:
                    self.subtitles.selection_set(i)
                if not chosen:
                    self.subtitles.see("end")
            if kind in ("snapshot", "answer"):
                self.answer_box.configure(state="normal")
                self.answer_box.delete("1.0", "end")
                for answer in reversed(self.answers.values()):
                    points = "".join(f"\n• {point}" for point in answer["points"])
                    self.answer_box.insert("end", f'{answer["question"]}\n{answer["conclusion"]}{points}\n\n')
                self.answer_box.configure(state="disabled")
        self.root.after(65, self.poll)

    def close(self):
        self.stop.set()
        self.commands.put(None)
        self.root.destroy()


if __name__ == "__main__":
    window = tk.Tk()
    Desktop(window)
    window.mainloop()
