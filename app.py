"""Gate Change: an airport-themed colour-and-number card game for everyone on the hotspot.

Play against bots on your own, or host a table and have others join with a 4-letter code (or
from the list of open games on this hotspot). Rules: engine.py. Rooms, long-polling and bots:
rooms.py and bots.py. Everything is in memory; nothing is written to disk.

API (relative to the app's root; the platform serves it at /gatechange/):
  GET  api/rooms                         open public lobbies on this hotspot
  POST api/rooms {name, solo, public}    new room -> {code, token}; solo = you + 3 bots, started
  POST api/rooms/{code}/join {name}      take a seat in a lobby -> {code, token}
  GET  api/rooms/{code}/state?since=N    your view of the room; waits up to 20 s for a change
  POST api/rooms/{code}/act {type, ...}  a move or a lobby change; returns your new view
The seat token goes in the "X-Player" header (and in join's body to rejoin). POSTs also need
"X-GateChange: 1" (blocks cross-site requests).
"""
import asyncio
import contextlib
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from amrooms import RoomError, whois

from engine import IllegalMove
from rooms import SOLO_BOTS, make_rooms

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

HERE = Path(__file__).parent
rooms = make_rooms()


@contextlib.asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(rooms.sweep_forever())
    yield
    task.cancel()


app = FastAPI(title="Gate Change", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def need_header(request: Request):
    if request.headers.get("x-gatechange") != "1":
        raise HTTPException(403, "Missing request header.")


async def body(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Send JSON.")
    if not isinstance(data, dict):
        raise HTTPException(400, "Send a JSON object.")
    return data


def room_or_404(code):
    return rooms.get(code)          # RoomError 404 if there is none


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/rooms")
async def open_games():
    return {"rooms": [r.summary() for r in rooms.open_lobbies()]}


@app.post("/api/rooms")
async def create(request: Request):
    need_header(request)
    data = await body(request)
    who = await whois(request)
    solo = data.get("solo") is True
    room = rooms.create(public=data.get("public", True) is not False and not solo)
    token = room.join(data.get("name"), who=who)
    if solo:
        for _ in range(SOLO_BOTS):
            room.add_bot("normal")
        room.start()
    return {"code": room.code, "token": token}


@app.post("/api/rooms/{code}/join")
async def join(code: str, request: Request):
    need_header(request)
    data = await body(request)
    who = await whois(request)
    room = room_or_404(code)
    async with room.lock:
        token = room.join(data.get("name"), data.get("token") or request.headers.get("x-player"), who=who)
    return {"code": room.code, "token": token}


@app.get("/api/rooms/{code}/state")
async def state(code: str, request: Request, since: int = -1):
    room = room_or_404(code)
    token = request.headers.get("x-player", "")
    if room.seat_of(token) is None:
        raise HTTPException(403, "You're not at this table.")
    room.view(token)                   # marks you as here while you wait
    await room.wait(since)
    if room.closed or room.seat_of(token) is None:
        raise HTTPException(404, "This game has ended.")
    return room.view(token)


@app.post("/api/rooms/{code}/act")
async def act(code: str, request: Request):
    need_header(request)
    data = await body(request)
    room = room_or_404(code)
    token = request.headers.get("x-player", "")
    async with room.lock:
        try:
            room.act(token, data)
        except IllegalMove as e:
            raise HTTPException(409, str(e))
        if data.get("type") == "leave":
            rooms.sweep()               # an empty lobby goes at once
            return {"left": True}
        return room.view(token)


@app.exception_handler(RoomError)
async def room_error(_req, exc):
    return JSONResponse({"detail": exc.message}, status_code=exc.status)


@app.exception_handler(HTTPException)
async def http_error(_req, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


@app.get("/icon.svg")
async def icon():
    return FileResponse(HERE / "static" / "icon.svg", media_type="image/svg+xml",
                        headers={"Cache-Control": "max-age=86400"})


@app.get("/")
async def index():
    return FileResponse(HERE / "static" / "index.html", headers={"Cache-Control": "no-cache"})
