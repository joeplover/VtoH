import asyncio
import secrets
from collections import OrderedDict
from time import monotonic
from uuid import uuid4

from .providers import Answers

Event = dict


class Session:
    def __init__(self, model: Answers) -> None:
        self.id = uuid4().hex
        self.owner = secrets.token_urlsafe(24)
        self.viewer = secrets.token_urlsafe(24)
        self.model = model
        self.updated = monotonic()
        self.recording = False
        self.transcripts: OrderedDict[str, Event] = OrderedDict()
        self.answers: OrderedDict[tuple[str, ...], Event] = OrderedDict()
        self.subscribers: set[asyncio.Queue[Event]] = set()
        self.jobs: asyncio.Queue[tuple[tuple[str, ...], str, str, bool]] = asyncio.Queue(32)
        self.worker: asyncio.Task | None = None
        self.pending: tuple[str, ...] = ()

    def snapshot(self) -> Event:
        return {"type": "snapshot", "transcripts": list(self.transcripts.values()),
                "answers": list(self.answers.values()), "recording": self.recording}

    def publish(self, event: Event) -> None:
        self.updated = monotonic()
        for queue in self.subscribers:
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(self.snapshot())
            else:
                queue.put_nowait(event)

    def capture(self, value: bool) -> None:
        self.recording = value
        self.publish({"type": "capture", "recording": value})

    def put_transcript(self, key: str, text: str, final: bool) -> None:
        previous = self.transcripts.get(key)
        if previous is not None and (previous["final"] or (previous["text"] == text and not final)):
            return
        event = {"type": "transcript", "id": key, "text": text[:4000], "final": final}
        self.transcripts[key] = event
        if len(self.transcripts) > 250:
            self.transcripts.popitem(last=False)
        self.publish(event)
        if final:
            self.enqueue((key,), manual=False)

    def enqueue(self, ids: tuple[str, ...], *, manual: bool) -> None:
        finals = {key: row["text"] for key, row in self.transcripts.items() if row["final"]}
        selected_ids = tuple(key for key in finals if key in ids)
        if not selected_ids or len(selected_ids) != len(ids):
            self.publish({"type": "error", "message": "请选择已定稿的字幕。"})
            return
        previous = [text for key, text in finals.items() if key not in selected_ids]
        context = "\n".join(previous[-8:])[-6000:]
        selected = "\n".join(finals[key] for key in selected_ids)
        if self.jobs.full():
            self.publish({"type": "error", "message": "待回答内容过多，请稍后手动重试。"})
            return
        self.jobs.put_nowait((selected_ids, context, selected, manual))
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self.answer_jobs())

    async def answer_jobs(self) -> None:
        while not self.jobs.empty():
            ids, context, selected, manual = self.jobs.get_nowait()
            cached = self.answers.get(ids)
            if cached is None and len(ids) == 1:
                cached = next((answer for keys, answer in reversed(self.answers.items()) if keys[-1] == ids[0]), None)
            if cached is not None:
                if manual:
                    self.publish(cached)
                continue
            try:
                decision = await self.model.decide(context, selected, manual)
            except Exception:
                self.publish({"type": "error", "message": "问答服务暂不可用，请稍后点「回答这段」重试。"})
                continue
            if not manual and decision.pending:
                self.pending = (*self.pending, *ids)[-5:]
                continue
            if not decision.conclusion.strip():
                if not manual:
                    self.pending = ()
                continue
            if not manual:
                ids = (*self.pending, *ids)
                self.pending = ()
            event = {
                "type": "answer", "id": uuid4().hex, "transcript_ids": list(ids),
                "question": decision.question or selected, "conclusion": decision.conclusion,
                "points": decision.points,
            }
            self.answers[ids] = event
            if len(self.answers) > 120:
                self.answers.popitem(last=False)
            self.publish(event)
