import asyncio
import json
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from .providers import Answers, ChatCompletions, DashScopeSpeech, Speech
from .sessions import Session


def create_app(speech: Speech, answers: Answers, *, access_key: str = "") -> FastAPI:
    sessions: dict[str, Session] = {}

    async def expire() -> None:
        while True:
            await asyncio.sleep(60)
            for key, session in list(sessions.items()):
                if not session.recording and monotonic() - session.updated > 7200:
                    del sessions[key]
                    session.publish({"type": "expired"})

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        cleanup = asyncio.create_task(expire())
        try:
            yield
        finally:
            tasks = [cleanup, *(s.worker for s in sessions.values() if s.worker is not None)]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    prefix = "/vtoh"
    app = FastAPI(
        lifespan=lifespan,
        docs_url=f"{prefix}/docs",
        redoc_url=f"{prefix}/redoc",
        openapi_url=f"{prefix}/openapi.json",
        swagger_ui_oauth2_redirect_url=f"{prefix}/docs/oauth2-redirect",
    )
    router = APIRouter(prefix=prefix)

    @router.get("/health")
    async def health():
        return {"status": "ok"}

    @router.get("/pair", include_in_schema=False)
    async def pair():
        return FileResponse(Path(__file__).with_name("viewer.html"), headers={
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        })

    @router.post("/sessions", status_code=201)
    async def new_session(authorization: str = Header(default="")):
        if access_key and not secrets.compare_digest(authorization.encode(), f"Bearer {access_key}".encode()):
            raise HTTPException(401, "Invalid access key")
        if len(sessions) >= 100:
            raise HTTPException(503, "Session capacity reached")
        session = Session(answers)
        sessions[session.id] = session
        return {"id": session.id, "owner": session.owner, "viewer": session.viewer}

    async def authorize(ws: WebSocket, session_id: str, *, viewer: bool = False) -> Session | None:
        session = sessions.get(session_id)
        token = ws.query_params.get("token", "").encode()
        if session is None or not secrets.compare_digest(token, (session.viewer if viewer else session.owner).encode()):
            await ws.close(code=1008)
            return None
        await ws.accept()
        return session

    async def subscribe(ws: WebSocket, session_id: str, *, viewer: bool) -> None:
        session = await authorize(ws, session_id, viewer=viewer)
        if session is None:
            return
        queue: asyncio.Queue[dict] = asyncio.Queue(64)
        queue.put_nowait(session.snapshot())
        session.subscribers.add(queue)

        async def send() -> None:
            while True:
                event = await queue.get()
                await ws.send_json(event)
                if event["type"] == "expired":
                    await ws.close()
                    return

        async def receive() -> None:
            while True:
                raw = await ws.receive_text()
                if viewer:
                    continue
                try:
                    message = json.loads(raw)
                    ids = message.get("ids") if isinstance(message, dict) and message.get("type") == "answer" else None
                    if isinstance(ids, list) and 1 <= len(ids) <= 8 and all(isinstance(key, str) for key in ids):
                        session.enqueue(tuple(ids), manual=True)
                except ValueError:
                    continue

        tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            session.subscribers.discard(queue)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @router.websocket("/sessions/{session_id}/watch")
    async def watch(ws: WebSocket, session_id: str):
        await subscribe(ws, session_id, viewer=True)

    @router.websocket("/sessions/{session_id}/control")
    async def control(ws: WebSocket, session_id: str):
        await subscribe(ws, session_id, viewer=False)

    @router.websocket("/sessions/{session_id}/audio")
    async def audio(ws: WebSocket, session_id: str):
        session = await authorize(ws, session_id)
        if session is None:
            return
        if session.recording:
            await ws.close(code=1008)
            return
        session.capture(True)
        prefix = uuid4().hex[:12]

        async def frames() -> AsyncIterator[bytes]:
            first = True
            while True:
                message = await asyncio.wait_for(ws.receive(), timeout=120 if first else 30)
                if message["type"] == "websocket.disconnect":
                    return
                data = message.get("bytes")
                if data:
                    if len(data) > 32000 or len(data) % 2:
                        raise ValueError("Expected PCM16 mono audio")
                    first = False
                    yield data
                elif message.get("text"):
                    if json.loads(message["text"]) == {"type": "stop"}:
                        return

        try:
            await ws.send_json({"type": "ready"})
            async with asyncio.timeout(7200):
                async for key, text, final in speech.stream(frames()):
                    session.put_transcript(f"{prefix}-{key}", text, final)
            await ws.send_json({"type": "stopped"})
        except (WebSocketDisconnect, OSError):
            pass
        except Exception:
            session.publish({"type": "error", "message": "语音识别连接已中断，请检查服务配置或重新开始监听。"})
        finally:
            session.capture(False)

    app.include_router(router)
    return app


app = create_app(DashScopeSpeech(), ChatCompletions(), access_key=os.getenv("VTOH_ACCESS_KEY", ""))
