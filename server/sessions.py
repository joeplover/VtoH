import asyncio
import logging
import secrets
from collections import OrderedDict
from time import monotonic
from uuid import uuid4

from .providers import Answers, TRANSLATION_LANGUAGES, Translator

Event = dict
logger = logging.getLogger("vtoh")
PARTIAL_TRANSLATION_INTERVAL = 1.0
MAX_TRANSLATION_JOBS = 24


class Session:
    def __init__(self, model: Answers, translator: Translator | None = None) -> None:
        self.id = uuid4().hex
        self.owner = secrets.token_urlsafe(24)
        self.viewer = secrets.token_urlsafe(24)
        self.model = model
        self.translator = translator
        self.translation_jobs: OrderedDict[str, bool] = OrderedDict()
        self.translation_worker: asyncio.Task | None = None
        self.next_partial_translation = 0.0
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

    def put_transcript(self, key: str, text: str, final: bool, target: str = "") -> None:
        previous = self.transcripts.get(key)
        if previous is not None and (previous["final"] or (previous["text"] == text and not final)):
            return
        event = {"type": "transcript", "id": key, "text": text[:4000], "final": final}
        if target:
            event["translation_language"] = target
            event["translation_final"] = False
            if previous and previous.get("translation_language") == target and previous.get("translation"):
                event["translation"] = previous["translation"]
            if (event.get("translation") and previous["text"] == event["text"]
                    and previous["translation_status"] == "ready"):
                event["translation_status"] = "ready"
                event["translation_final"] = final
            elif self.translator is not None and self.queue_translation(key, final):
                event["translation_status"] = "pending"
            else:
                event.pop("translation", None)
                event["translation_status"] = "unavailable"
        self.transcripts[key] = event
        if len(self.transcripts) > 250:
            expired, _ = self.transcripts.popitem(last=False)
            self.translation_jobs.pop(expired, None)
        self.publish(event)
        if final:
            self.enqueue((key,), manual=False)

    def queue_translation(self, key: str, final: bool) -> bool:
        if key not in self.translation_jobs and len(self.translation_jobs) >= MAX_TRANSLATION_JOBS:
            return False
        self.translation_jobs[key] = final
        if self.translation_worker is None or self.translation_worker.done():
            self.translation_worker = asyncio.create_task(self.translate_jobs(self.translator))
        return True

    async def translate_jobs(self, translator: Translator) -> None:
        while self.translation_jobs:
            key = next((key for key, final in self.translation_jobs.items() if final), None)
            if key is None:
                delay = self.next_partial_translation - monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                    continue
                key = next(iter(self.translation_jobs))
                self.next_partial_translation = monotonic() + PARTIAL_TRANSLATION_INTERVAL
            self.translation_jobs.pop(key)
            submitted = self.transcripts[key]
            text = submitted["text"]
            try:
                language = TRANSLATION_LANGUAGES[submitted["translation_language"]]
                translation = await translator.translate(text, language)
            except Exception as error:
                logger.warning("translation failed for session %s: %s", self.id, type(error).__name__)
                translation = None
            row = self.transcripts.get(key)
            if row is None or (row["final"] and row["text"] != text):
                continue
            matches = row["text"] == text
            if not matches and translation is None:
                continue
            event = {**row}
            if translation is None:
                event.pop("translation", None)
                event["translation_status"] = "unavailable"
            else:
                event["translation"] = translation
                event["translation_status"] = "ready" if matches else "pending"
                event["translation_final"] = matches and row["final"]
                if matches:
                    self.translation_jobs.pop(key, None)
            self.transcripts[key] = event
            self.publish(event)

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
