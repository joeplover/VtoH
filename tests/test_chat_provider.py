import asyncio
import json

import httpx
import pytest

from server.providers import ChatCompletions


def test_default_answer_uses_fast_flash_json(monkeypatch):
    monkeypatch.setenv("VTOH_CHAT_KEY", "test")
    monkeypatch.delenv("VTOH_CHAT_MODEL", raising=False)

    def reply(request):
        body = json.loads(request.content)
        assert body["model"] == "qwen-flash"
        assert body["enable_thinking"] is False
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"conclusion":"答案"}'}}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            provider = ChatCompletions(client)
            assert (await provider.decide("", "怎么做？", True)).conclusion == "答案"
            monkeypatch.setenv("VTOH_CHAT_MODEL", "qwen-plus")
            with pytest.raises(ValueError, match="Invalid chat model"):
                await provider.decide("", "怎么做？", True)

    asyncio.run(run())


def test_flash_answer_stream_reports_json_deltas(monkeypatch):
    monkeypatch.setenv("VTOH_CHAT_KEY", "test")

    def reply(request):
        assert json.loads(request.content)["stream"] is True
        events = [
            {"choices": [{"delta": {"content": '{"conclusion":"先确认'}}]},
            {"choices": [{"delta": {"content": '目标","points":[]}'}}]},
        ]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async def run():
        updates = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            result = await ChatCompletions(client).decide_stream(
                "", "怎么开始？", True, lambda value: updates.append(value),
            )
        assert result.conclusion == "先确认目标"
        assert updates[-1] == '{"conclusion":"先确认目标","points":[]}'

    asyncio.run(run())
