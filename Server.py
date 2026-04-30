"""
server.py  –  Serveur de salle vidéo partagée
Lancer UNE SEULE FOIS sur le PC "hôte" :
    python server.py
"""
import asyncio
import websockets
import json
import time
from collections import defaultdict

HOST = "0.0.0.0"
PORT = 9765

clients = {}
rooms   = defaultdict(set)


def is_open(ws):
    try:
        return ws.open
    except AttributeError:
        return ws.state.name == "OPEN"


async def broadcast(room, message, exclude=None):
    targets = [ws for ws in rooms[room] if ws != exclude and is_open(ws)]
    if targets:
        await asyncio.gather(*[ws.send(message) for ws in targets],
                             return_exceptions=True)


async def broadcast_members(room):
    members = [clients[ws]["name"] for ws in rooms[room] if ws in clients]
    msg = json.dumps({"type": "members", "members": members})
    targets = [ws for ws in rooms[room] if is_open(ws)]
    if targets:
        await asyncio.gather(*[ws.send(msg) for ws in targets],
                             return_exceptions=True)


async def handler(ws):
    print(f"[+] Connexion : {ws.remote_address}")
    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except Exception:
                continue

            t = data.get("type")

            if t == "join":
                name = data.get("name", "Anonyme")[:30]
                # FORCE tout le monde dans la même salle, peu importe ce qu'ils tapent
                room = "Salles_Unique_Pour_Tous" 
                
                clients[ws] = {"name": name, "room": room}
                rooms[room].add(ws)
                print(f"  [GLOBAL] {name} rejoint (Total: {len(rooms[room])} membres)")
                
                await ws.send(json.dumps({
                    "type": "joined", "name": name,
                    "room": "Polymeet", "count": len(rooms[room])
                }))
                await broadcast(room, json.dumps(
                    {"type": "user_joined", "name": name}), exclude=ws)
                await broadcast_members(room)


            elif t == "video":
                if ws in clients:
                    room = clients[ws]["room"]
                    name = clients[ws]["name"]
                    await broadcast(room, json.dumps({
                        "type": "video", "name": name,
                        "frame": data.get("frame", "")
                    }), exclude=ws)

            elif t == "audio":
                if ws in clients:
                    room = clients[ws]["room"]
                    name = clients[ws]["name"]
                    await broadcast(room, json.dumps({
                        "type": "audio", "name": name,
                        "chunk": data.get("chunk", "")
                    }), exclude=ws)

            elif t == "chat":
                if ws in clients:
                    room = clients[ws]["room"]
                    name = clients[ws]["name"]
                    await broadcast(room, json.dumps({
                        "type": "chat", "name": name,
                        "text": data.get("text", "")[:500],
                        "time": time.strftime("%H:%M")
                    }))

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if ws in clients:
            info = clients.pop(ws)
            room = info["room"]
            rooms[room].discard(ws)
            print(f"  [{room}] {info['name']} quitté ({len(rooms[room])} restants)")
            await broadcast(room, json.dumps(
                {"type": "user_left", "name": info["name"]}))
            await broadcast_members(room)
            if not rooms[room]:
                del rooms[room]


async def main():
    import socket
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = "127.0.0.1"

    print("╔══════════════════════════════════════════╗")
    print(f"║  Serveur vidéo  –  port {PORT}          ║")
    print("║  En attente de connexions…               ║")
    print("╚══════════════════════════════════════════╝")
    print(f"\n  ✅ IP locale  : {ip}")
    print(f"  📋 Donnez cette IP aux participants : {ip}\n")

    async with websockets.serve(handler, HOST, PORT, max_size=10_000_000):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())