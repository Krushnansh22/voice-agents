from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import asyncio
import json
import base64
import uuid
import tempfile
import os
from typing import Dict, Optional
import logging
from datetime import datetime
import time

# Import your existing services
from services.deepgram_service import DeepgramService
from services.ai_service import AzureOpenAIService
from services.tts_service import ElevenLabsService

# Import database components
from database.connection import init_database, close_database, get_database
from database.operations import db_ops
from database.models import User, Conversation

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="WebSocket Voice Assistant - Simplified")


class UserManager:
    """Manages active users and their state"""

    def __init__(self):
        self.active_users: Dict[str, User] = {}

    async def create_user(self, client_metadata: Dict = None) -> User:
        """Create a new user"""
        # Create user
        user = await db_ops.create_user(metadata=client_metadata)
        self.active_users[user.user_id] = user
        logger.info(f"📝 Created new user: {user.user_id}")
        return user

    async def end_user_session(self, user_id: str) -> bool:
        """End a user session"""
        success = await db_ops.end_user_session(user_id)
        if success and user_id in self.active_users:
            del self.active_users[user_id]
            logger.info(f"🔚 Ended session for user: {user_id}")
        return success

    async def get_user(self, user_id: str) -> Optional[User]:
        """Get user information"""
        if user_id in self.active_users:
            return self.active_users[user_id]

        # Try to get from database
        user = await db_ops.get_user(user_id)
        if user and user.status == "active":
            self.active_users[user_id] = user
        return user

    def is_user_active(self, user_id: str) -> bool:
        """Check if user is active"""
        return user_id in self.active_users


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.client_users: Dict[str, str] = {}  # client_id -> user_id
        self.processing_status: Dict[str, str] = {}

    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        self.active_connections[client_id] = websocket
        self.processing_status[client_id] = "connected"
        logger.info(f"Client {client_id} connected")

    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]
        if client_id in self.processing_status:
            del self.processing_status[client_id]
        if client_id in self.client_users:
            del self.client_users[client_id]
        logger.info(f"Client {client_id} disconnected")

    async def send_message(self, client_id: str, message: dict):
        if client_id in self.active_connections:
            try:
                await self.active_connections[client_id].send_text(json.dumps(message))
            except Exception as e:
                logger.error(f"Error sending message to {client_id}: {e}")

    async def send_status(self, client_id: str, status: str, data: Optional[dict] = None):
        message = {"type": "status", "status": status}
        if data:
            message.update(data)
        await self.send_message(client_id, message)

    async def send_error(self, client_id: str, error: str):
        await self.send_message(client_id, {"type": "error", "message": error})

    async def send_result(self, client_id: str, result: dict):
        await self.send_message(client_id, {"type": "result", **result})

    def set_client_user(self, client_id: str, user_id: str):
        """Associate client with a user"""
        self.client_users[client_id] = user_id

    def get_client_user(self, client_id: str) -> Optional[str]:
        """Get user ID for a client"""
        return self.client_users.get(client_id)


# Global managers
manager = ConnectionManager()
user_manager = UserManager()


class VoiceAssistantWebSocket:
    def __init__(self):
        self.stt_service = DeepgramService()
        self.ai_service = AzureOpenAIService()
        self.tts_service = ElevenLabsService()

    async def start_session(self, client_id: str, metadata: Dict = None):
        """Start a new session by creating a new user"""
        try:
            # Create new user
            user = await user_manager.create_user(metadata)

            # Associate client with user
            manager.set_client_user(client_id, user.user_id)

            # Send response
            await manager.send_message(client_id, {
                "type": "session_started",
                "user_id": user.user_id,
                "status": user.status,
                "created_at": user.created_at.isoformat(),
                "message": f"New session started with user {user.user_id}"
            })

            logger.info(f"✅ Started session for new user {user.user_id} (client {client_id})")

        except Exception as e:
            logger.error(f"❌ Failed to start session for client {client_id}: {e}")
            await manager.send_error(client_id, f"Failed to start session: {str(e)}")

    async def end_session(self, client_id: str, user_id: str):
        """End a user session"""
        try:
            # Get user info before ending
            user = await user_manager.get_user(user_id)
            if not user:
                await manager.send_error(client_id, "User not found")
                return

            # End the user session
            success = await user_manager.end_user_session(user_id)

            if success:
                # Get final conversation count
                conv_count = await db_ops.get_conversation_count(user_id)

                await manager.send_message(client_id, {
                    "type": "session_ended",
                    "user_id": user_id,
                    "status": "ended",
                    "ended_at": datetime.utcnow().isoformat(),
                    "total_conversations": conv_count,
                    "message": f"Session ended. User {user_id} had {conv_count} conversations."
                })

                # Clear client user association
                if client_id in manager.client_users:
                    del manager.client_users[client_id]

                logger.info(f"✅ Ended session for user {user_id}")
            else:
                await manager.send_error(client_id, "Failed to end session")

        except Exception as e:
            logger.error(f"❌ Failed to end session for user {user_id}: {e}")
            await manager.send_error(client_id, f"Failed to end session: {str(e)}")

    async def get_user_history(self, client_id: str, user_id: str, limit: int = 50):
        """Get conversation history for a user"""
        try:
            # Get user info
            user = await db_ops.get_user(user_id)
            if not user:
                await manager.send_error(client_id, "User not found")
                return

            # Get conversations
            conversations = await db_ops.get_user_conversations(user_id, limit)

            # Convert to serializable format
            conv_data = []
            for conv in conversations:
                conv_data.append({
                    "conversation_id": conv.conversation_id,
                    "transcript": conv.transcript,
                    "ai_response": conv.ai_response,
                    "timestamp": conv.timestamp.isoformat(),
                    "processing_time": conv.processing_time
                })

            await manager.send_message(client_id, {
                "type": "user_history",
                "user_id": user_id,
                "user_info": {
                    "user_id": user.user_id,
                    "status": user.status,
                    "created_at": user.created_at.isoformat(),
                    "ended_at": user.ended_at.isoformat() if user.ended_at else None,
                    "total_conversations": len(conv_data)
                },
                "conversations": conv_data
            })

        except Exception as e:
            logger.error(f"❌ Failed to get user history {user_id}: {e}")
            await manager.send_error(client_id, f"Failed to get user history: {str(e)}")

    async def get_all_users(self, client_id: str, limit: int = 20):
        """Get all users from the database"""
        try:
            # Get all users
            users = await db_ops.get_all_users(limit)

            users_data = []
            for user in users:
                # Get conversation count for each user
                conv_count = await db_ops.get_conversation_count(user.user_id)

                users_data.append({
                    "user_id": user.user_id,
                    "status": user.status,
                    "created_at": user.created_at.isoformat(),
                    "ended_at": user.ended_at.isoformat() if user.ended_at else None,
                    "total_conversations": conv_count
                })

            await manager.send_message(client_id, {
                "type": "all_users",
                "users": users_data,
                "total_count": len(users_data)
            })

        except Exception as e:
            logger.error(f"❌ Failed to get all users: {e}")
            await manager.send_error(client_id, f"Failed to get users: {str(e)}")

    async def get_system_stats(self, client_id: str):
        """Get system statistics"""
        try:
            stats = await db_ops.get_system_stats()

            await manager.send_message(client_id, {
                "type": "system_stats",
                "stats": stats
            })

        except Exception as e:
            logger.error(f"❌ Failed to get system stats: {e}")
            await manager.send_error(client_id, f"Failed to get system stats: {str(e)}")

    async def process_audio_stream(self, client_id: str, audio_data: bytes):
        """Process audio with user management"""
        # Get current user
        user_id = manager.get_client_user(client_id)
        if not user_id:
            await manager.send_error(client_id, "No active session. Please start a session first.")
            return

        # Verify user is active
        user = await user_manager.get_user(user_id)
        if not user or user.status != "active":
            await manager.send_error(client_id, "Session is not active. Please start a new session.")
            return

        temp_dir = tempfile.gettempdir()
        conversation_id = str(uuid.uuid4())
        input_path = os.path.join(temp_dir, f"input_{conversation_id}.wav")
        output_path = os.path.join(temp_dir, f"output_{conversation_id}.mp3")

        start_time = time.time()

        try:
            # Save audio data
            await manager.send_status(client_id, "saving_audio")
            with open(input_path, 'wb') as f:
                f.write(audio_data)

            # Step 1: Speech to Text
            await manager.send_status(client_id, "transcribing")
            transcript = await asyncio.to_thread(
                self.stt_service.transcribe, input_path
            )

            if not transcript:
                await manager.send_error(client_id, "Failed to transcribe audio")
                return

            await manager.send_status(client_id, "transcription_complete",
                                      {"transcript": transcript})

            # Step 2: Get AI Response
            await manager.send_status(client_id, "generating_response")
            ai_response = await asyncio.to_thread(
                self.ai_service.get_response, transcript
            )

            if not ai_response:
                await manager.send_error(client_id, "Failed to generate AI response")
                return

            await manager.send_status(client_id, "response_generated",
                                      {"ai_response": ai_response})

            # Step 3: Text to Speech
            await manager.send_status(client_id, "generating_speech")
            output_file = await asyncio.to_thread(
                self.tts_service.text_to_speech, ai_response, output_path
            )

            if not output_file:
                await manager.send_error(client_id, "Failed to generate speech")
                return

            # Calculate processing time
            processing_time = time.time() - start_time

            # Save conversation to database
            await manager.send_status(client_id, "saving_conversation")
            conversation = await db_ops.save_conversation(
                user_id=user.user_id,
                transcript=transcript,
                ai_response=ai_response,
                processing_time=processing_time,
                metadata={
                    "audio_duration": len(audio_data) / 16000,  # Rough estimate
                    "conversation_id": conversation_id,
                    "client_id": client_id
                }
            )

            # Read the generated audio file
            with open(output_path, 'rb') as f:
                audio_content = f.read()

            # Send final result with audio data
            await manager.send_result(client_id, {
                "conversation_id": conversation.conversation_id,
                "user_id": user_id,
                "transcript": transcript,
                "ai_response": ai_response,
                "audio_data": base64.b64encode(audio_content).decode('utf-8'),
                "processing_time": processing_time,
                "timestamp": conversation.timestamp.isoformat()
            })

            await manager.send_status(client_id, "completed")

        except Exception as e:
            logger.error(f"Processing error for client {client_id}: {e}")
            await manager.send_error(client_id, f"Processing failed: {str(e)}")

        finally:
            # Cleanup temp files
            for file_path in [input_path, output_path]:
                if os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception as e:
                        logger.warning(f"Failed to cleanup {file_path}: {e}")

    async def process_text_to_speech(self, client_id: str, text: str):
        """Convert text directly to speech with user management"""
        # Get current user
        user_id = manager.get_client_user(client_id)
        if not user_id:
            await manager.send_error(client_id, "No active session. Please start a session first.")
            return

        # Verify user is active
        user = await user_manager.get_user(user_id)
        if not user or user.status != "active":
            await manager.send_error(client_id, "Session is not active. Please start a new session.")
            return

        temp_dir = tempfile.gettempdir()
        conversation_id = str(uuid.uuid4())
        output_path = os.path.join(temp_dir, f"tts_{conversation_id}.mp3")

        start_time = time.time()

        try:
            await manager.send_status(client_id, "generating_speech")

            output_file = await asyncio.to_thread(
                self.tts_service.text_to_speech, text, output_path
            )

            if not output_file:
                await manager.send_error(client_id, "Failed to generate speech")
                return

            # Calculate processing time
            processing_time = time.time() - start_time

            # Save conversation to database (text input, same as response for TTS-only)
            await manager.send_status(client_id, "saving_conversation")
            conversation = await db_ops.save_conversation(
                user_id=user.user_id,
                transcript=f"[TEXT INPUT] {text}",
                ai_response=text,
                processing_time=processing_time,
                metadata={
                    "type": "text_to_speech",
                    "conversation_id": conversation_id,
                    "client_id": client_id
                }
            )

            # Read the generated audio file
            with open(output_path, 'rb') as f:
                audio_content = f.read()

            await manager.send_result(client_id, {
                "conversation_id": conversation.conversation_id,
                "user_id": user_id,
                "text": text,
                "audio_data": base64.b64encode(audio_content).decode('utf-8'),
                "processing_time": processing_time,
                "timestamp": conversation.timestamp.isoformat()
            })

            await manager.send_status(client_id, "completed")

        except Exception as e:
            logger.error(f"TTS error for client {client_id}: {e}")
            await manager.send_error(client_id, f"TTS failed: {str(e)}")

        finally:
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except Exception as e:
                    logger.warning(f"Failed to cleanup {output_path}: {e}")


assistant = VoiceAssistantWebSocket()


@app.on_event("startup")
async def startup_event():
    """Initialize database connection on startup"""
    try:
        await init_database()
        logger.info("✅ Database connected successfully")
    except Exception as e:
        logger.error(f"❌ Failed to connect to database: {e}")
        raise


@app.on_event("shutdown")
async def shutdown_event():
    """Close database connection on shutdown"""
    await close_database()
    logger.info("🔌 Database connection closed")


@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await manager.connect(websocket, client_id)

    try:
        while True:
            # Receive message from client
            data = await websocket.receive_text()
            message = json.loads(data)

            message_type = message.get("type")

            if message_type == "start_session":
                # Start a new session by creating new user
                metadata = message.get("metadata", {})
                metadata.update({
                    "client_id": client_id,
                    "user_agent": message.get("user_agent", ""),
                    "timestamp": datetime.utcnow().isoformat()
                })
                await assistant.start_session(client_id, metadata)

            elif message_type == "end_session":
                # End current session
                user_id = message.get("user_id")
                if user_id:
                    await assistant.end_session(client_id, user_id)
                else:
                    await manager.send_error(client_id, "User ID required")

            elif message_type == "get_user_history":
                # Get user history
                user_id = message.get("user_id")
                limit = message.get("limit", 50)
                if user_id:
                    await assistant.get_user_history(client_id, user_id, limit)
                else:
                    await manager.send_error(client_id, "User ID required")

            elif message_type == "get_all_users":
                # Get all users
                limit = message.get("limit", 20)
                await assistant.get_all_users(client_id, limit)

            elif message_type == "get_system_stats":
                # Get system statistics
                await assistant.get_system_stats(client_id)

            elif message_type == "audio_upload":
                # Handle audio file upload
                audio_data = base64.b64decode(message["audio_data"])
                await assistant.process_audio_stream(client_id, audio_data)

            elif message_type == "text_to_speech":
                # Handle direct text-to-speech
                text = message.get("text", "")
                if text:
                    await assistant.process_text_to_speech(client_id, text)
                else:
                    await manager.send_error(client_id, "No text provided")

            elif message_type == "ping":
                # Handle ping/keepalive
                await manager.send_message(client_id, {"type": "pong"})

            else:
                await manager.send_error(client_id, f"Unknown message type: {message_type}")

    except WebSocketDisconnect:
        manager.disconnect(client_id)
    except Exception as e:
        logger.error(f"WebSocket error for client {client_id}: {e}")
        await manager.send_error(client_id, f"Connection error: {str(e)}")
        manager.disconnect(client_id)


@app.get("/health")
async def health_check():
    """Health check endpoint with database status"""
    try:
        db = await get_database()
        db_healthy = await db.health_check()

        # Get system stats
        stats = await db_ops.get_system_stats()

        return {
            "status": "healthy" if db_healthy else "unhealthy",
            "database": "connected" if db_healthy else "disconnected",
            "active_connections": len(manager.active_connections),
            "active_users": len(user_manager.active_users),
            "system_stats": stats,
            "timestamp": datetime.utcnow().isoformat(),
            "message": "WebSocket Voice Assistant - Simplified"
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "database": "error",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }


@app.get("/")
async def get_homepage():
    """Serve the main page"""
    with open("index.html", "r") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)


if __name__ == "__main__":
    import uvicorn
    from config import validate_config

    # Validate configuration before starting
    if not validate_config():
        logger.error("❌ Configuration validation failed")
        exit(1)

    logger.info("🚀 Starting Voice Assistant - Simplified (No Session ID)")
    uvicorn.run(app, host="0.0.0.0", port=8000)