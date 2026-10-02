import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field

from aiohttp import web

PREFIX = "/developer/v1"


@dataclass
class Scripted:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)
    delay_s: float = 0


@dataclass
class Received:
    path: str
    query: dict[str, str]
    headers: dict[str, str]


class FakeServer:
    def __init__(self) -> None:
        self.answers: dict[str, Scripted] = {}
        self.received: list[Received] = []
        self.app = web.Application()
        self.app.router.add_route("*", "/{tail:.*}", self._handle)

    def script(self, path: str, answer: Scripted) -> None:
        self.answers[path] = answer

    async def _handle(self, request: web.Request) -> web.Response:
        self.received.append(
            Received(
                path=request.path,
                query=dict(request.query),
                headers=dict(request.headers),
            )
        )
        answer = self.answers.get(request.path)
        if answer is None:
            return web.Response(status=599, body=f"not scripted: {request.path}".encode())
        if answer.delay_s:
            await asyncio.sleep(answer.delay_s)
        return web.Response(status=answer.status, body=answer.body, headers=dict(answer.headers))
