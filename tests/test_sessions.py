import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.app import create_app
from server.providers import Decision
from server.sessions import Session


class Answers:
    def __init__(self):
        self.calls = []

    async def decide(self, context, selected, manual):
        self.calls.append((context, selected, manual))
        if selected.endswith("……"):
            return Decision(pending=True)
        return Decision(question=selected, conclusion="交流可以帮助确认需求。", points=["先确认目标"])


class Speech:
    async def stream(self, frames):
        async for frame in frames:
            text = frame.decode("utf-16-le")
            yield (str(text), text[:3], False)
            yield (str(text), text, True)


def test_manual_question_moves_ahead_of_queued_auto_questions():
    async def run():
        model = Answers()
        session = Session(model)
        session.put_transcript("1", "第一个问题？", True)
        session.put_transcript("2", "第二个问题？", True)
        session.enqueue(("2",), manual=True)
        await session.worker

        assert model.calls == [
            ("第一个问题？", "第二个问题？", True),
            ("", "第一个问题？", False),
        ]

    asyncio.run(run())


def test_final_transcript_translation_is_shared_and_saved():
    class Translator:
        def __init__(self):
            self.calls = []

        async def translate(self, text, target):
            self.calls.append((text, target))
            return "The meeting starts now."

    async def run():
        translator = Translator()
        session = Session(Answers(), translator)
        owner = asyncio.Queue()
        viewer = asyncio.Queue()
        session.subscribers.update((owner, viewer))
        session.put_transcript("1", "会议现在", False, "en")
        session.put_transcript("1", "会议现在开始。", True, "en")
        session.put_transcript("1", "会议现在开始。", True, "en")
        await session.translation_worker
        for queue in (owner, viewer):
            assert queue.get_nowait()["final"] is False
            assert queue.get_nowait()["translation_status"] == "pending"
            translated = queue.get_nowait()
            assert translated["id"] == "1"
            assert "".join(translated["text_lines"]) == translated["text"]
            assert translated["translation"] == "The meeting starts now."
            assert "".join(translated["translation_lines"]) == translated["translation"]
            assert translated["translation_language"] == "en"
        assert session.snapshot()["transcripts"] == [translated]
        assert translator.calls == [("会议现在开始。", "English")]

    asyncio.run(run())


def test_translation_arrives_before_speech_is_final():
    class Translator:
        async def translate(self, text, target):
            return {"今天开": "Today we start", "今天开会。": "We have a meeting today."}[text]

    async def run():
        session = Session(Answers(), Translator())
        viewer = asyncio.Queue()
        owner = asyncio.Queue()
        session.subscribers.update((viewer, owner))
        session.capture(True)
        session.put_transcript("1", "今天开", False, "en")
        await asyncio.wait_for(session.translation_worker, 2)
        for subscriber in (viewer, owner):
            assert subscriber.get_nowait()["recording"]
            assert subscriber.get_nowait()["translation_status"] == "pending"
            live = subscriber.get_nowait()
            assert not live["final"]
            assert live["translation"] == "Today we start"
        assert session.recording
        session.put_transcript("1", "今天开会。", True, "en")
        await asyncio.wait_for(session.translation_worker, 2)
        row = session.snapshot()["transcripts"][0]
        assert row["final"] and row["translation_final"]
        assert row["translation"] == "We have a meeting today."

    asyncio.run(run())


def test_stream_pair_and_deduplicate():
    answers = Answers()
    with TestClient(create_app(Speech(), answers, access_key="secret")) as client:
        assert client.get("/vtoh/health").json() == {"status": "ok"}
        assert client.get("/vtoh/docs").status_code == 200
        assert client.get("/vtoh/pair").status_code == 200
        assert client.post("/vtoh/sessions").status_code == 401
        assert client.post("/sessions").status_code == 404
        session = client.post("/vtoh/sessions", headers={"Authorization": "Bearer secret"}).json()
        path = f"/vtoh/sessions/{session['id']}"
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"{path}/watch?token=bad"):
                pass
        with client.websocket_connect(f"{path}/watch?token={session['viewer']}") as viewer:
            assert viewer.receive_json()["type"] == "snapshot"
            with client.websocket_connect(f"{path}/control?token={session['owner']}") as control:
                assert control.receive_json()["type"] == "snapshot"
                with client.websocket_connect(f"{path}/audio?token={session['owner']}") as audio:
                    assert audio.receive_json()["type"] == "ready"
                    assert viewer.receive_json() == {"type": "capture", "recording": True}
                    audio.send_bytes("我有个问题……".encode("utf-16-le"))
                    partial = viewer.receive_json()
                    final = viewer.receive_json()
                    assert partial["id"] == final["id"]
                    assert partial["final"] is False and final["final"] is True
                    audio.send_bytes("怎么确认需求？".encode("utf-16-le"))
                    viewer.receive_json()
                    question = viewer.receive_json()
                    answer = viewer.receive_json()
                    assert answer["type"] == "answer"
                    assert answer["points"] == ["先确认目标"]
                    control.send_json({"type": "answer", "ids": [question["id"]]})
                    assert viewer.receive_json() == answer
                    audio.send_json({"type": "stop"})
                    assert audio.receive_json()["type"] == "stopped"
            assert len(answers.calls) == 2
            assert "我有个问题……" in answers.calls[-1][0]
        with client.websocket_connect(f"{path}/watch?token={session['viewer']}") as viewer:
            snapshot = viewer.receive_json()
            assert snapshot["answers"] == [answer]
            assert len(snapshot["transcripts"]) == 2
