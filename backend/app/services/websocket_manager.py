"""
websocket_manager.py
---------------------
Tracks which hospital dashboards are currently connected and pushes live
dispatch events to them (FR-4.1). One hospital can have multiple connected
clients (e.g. two staff tablets); events go to all of them.
"""

from fastapi import WebSocket
from typing import Dict, List
import json


class ConnectionManager:
    def __init__(self):
        self._connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, hospital_id: str, ws: WebSocket):
        await ws.accept()
        self._connections.setdefault(hospital_id, []).append(ws)

    def disconnect(self, hospital_id: str, ws: WebSocket):
        if hospital_id in self._connections and ws in self._connections[hospital_id]:
            self._connections[hospital_id].remove(ws)

    async def send_to_hospital(self, hospital_id: str, event: dict):
        """Push an event to every connected client for one hospital. Silently
        skips if nobody's connected — the request still exists server-side,
        the dashboard just won't see it live until it (re)connects."""
        dead = []
        for ws in self._connections.get(hospital_id, []):
            try:
                await ws.send_text(json.dumps(event))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(hospital_id, ws)

    async def broadcast_all(self, event: dict):
        for hospital_id in list(self._connections.keys()):
            await self.send_to_hospital(hospital_id, event)


manager = ConnectionManager()
