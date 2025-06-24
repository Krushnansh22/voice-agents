import json
import asyncio
from typing import List
from fastapi import WebSocket
from datetime import datetime

class WebSocketManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        print(f"WebSocket connected. Total connections: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        print(f"WebSocket disconnected. Total connections: {len(self.active_connections)}")

    async def send_personal_message(self, message: str, websocket: WebSocket):
        try:
            await websocket.send_text(message)
        except Exception as e:
            print(f"Error sending personal message: {e}")
            self.disconnect(websocket)

    async def broadcast(self, message: str):
        """Broadcast message to all connected clients"""
        if not self.active_connections:
            print("No active connections to broadcast to")
            return

        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception as e:
                print(f"Error broadcasting to connection: {e}")
                disconnected.append(connection)

        # Remove disconnected clients
        for conn in disconnected:
            self.disconnect(conn)

    async def broadcast_transcript(self, call_id: str, speaker: str, message: str, timestamp: str):
        """Broadcast transcript message to all connected dashboard clients"""
        data = {
            "type": "transcript",
            "call_id": call_id,
            "speaker": speaker,
            "message": message,
            "timestamp": timestamp
        }
        await self.broadcast(json.dumps(data))

    async def broadcast_call_status(self, call_id: str, status: str, patient_name: str = None):
        """Broadcast call status update to all connected dashboard clients"""
        data = {
            "type": "call_status",
            "call_id": call_id,
            "status": status,
            "patient_name": patient_name,
            "timestamp": datetime.utcnow().isoformat()
        }
        await self.broadcast(json.dumps(data))

# Create a global instance
websocket_manager = WebSocketManager()