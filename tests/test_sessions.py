import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.app import create_app
from server.providers import Decision


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
