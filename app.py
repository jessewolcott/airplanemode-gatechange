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
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from engine import IllegalMove
from rooms import Rooms, RoomsFull

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

HERE = Path(__file__).parent
app = FastAPI(title="Gate Change", docs_url=None, redoc_url=None, openapi_url=None)
rooms = Rooms()


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
    room = rooms.get(code)
    if room is None:
        raise HTTPException(404, "No game with that code (it may have ended).")
    return room


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/rooms")
async def open_games():
    return {"rooms": rooms.open_games()}


@app.post("/api/rooms")
async def create(request: Request):
    need_header(request)
    data = await body(request)
    try:
        room = rooms.create(data.get("name"), public=data.get("public", True) is not False,
                            solo=data.get("solo") is True)
    except RoomsFull as e:
        raise HTTPException(503, str(e))
    return {"code": room.code, "token": room.host.token}


@app.post("/api/rooms/{code}/join")
async def join(code: str, request: Request):
    need_header(request)
    data = await body(request)
    room = room_or_404(code)
    async with room.lock:
        try:
            token = room.join(data.get("name"), data.get("token") or request.headers.get("x-player"))
        except IllegalMove as e:
            raise HTTPException(409, str(e))
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
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except IllegalMove as e:
            raise HTTPException(409, str(e))
        if room.closed:
            rooms.sweep()
            return {"left": True}
        if data.get("type") == "leave":
            return {"left": True}
        return room.view(token)


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
