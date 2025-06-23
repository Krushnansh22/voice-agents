"""
Simplified Database Service for Call Transcripts
"""
import logging
from datetime import datetime
from typing import List, Optional
from motor.motor_asyncio import AsyncIOMotorClient
from .models import (
    CallSession, TranscriptEntry,
    call_session_to_dict, transcript_entry_to_dict,
    dict_to_call_session, dict_to_transcript_entry
)
from V2.settings import settings

logger = logging.getLogger(__name__)


class DatabaseService:
    """Simplified database service for call transcripts"""

    def __init__(self):
        self.client: Optional[AsyncIOMotorClient] = None
        self.database = None

    async def connect(self):
        """Connect to MongoDB"""
        try:
            self.client = AsyncIOMotorClient(settings.MONGODB_URL)
            self.database = self.client[settings.MONGODB_DATABASE]

            # Test connection
            await self.client.admin.command('ping')
            logger.info(f"✅ Connected to MongoDB: {settings.MONGODB_DATABASE}")

            # Create indexes
            await self._create_indexes()
            return True
        except Exception as e:
            logger.error(f"❌ Failed to connect to MongoDB: {e}")
            return False

    async def _create_indexes(self):
        """Create necessary indexes"""
        try:
            # Call sessions indexes
            await self.database.call_sessions.create_index("call_id", unique=True)
            await self.database.call_sessions.create_index("started_at")

            # Transcripts indexes
            await self.database.transcripts.create_index("entry_id", unique=True)
            await self.database.transcripts.create_index("call_id")
            await self.database.transcripts.create_index("timestamp")
            await self.database.transcripts.create_index([("call_id", 1), ("timestamp", 1)])

            logger.info("✅ Database indexes created")
        except Exception as e:
            logger.warning(f"⚠️ Failed to create indexes: {e}")

    async def disconnect(self):
        """Disconnect from MongoDB"""
        if self.client:
            self.client.close()
            logger.info("🔌 Disconnected from MongoDB")

    # Call Session Operations
    async def create_call_session(self, patient_name: str, patient_phone: str, call_id: str = None) -> CallSession:
        """Create a new call session"""
        try:
            session = CallSession(
                call_id=call_id,
                patient_name=patient_name,
                patient_phone=patient_phone
            ) if call_id else CallSession(
                patient_name=patient_name,
                patient_phone=patient_phone
            )

            await self.database.call_sessions.insert_one(call_session_to_dict(session))
            logger.info(f"✅ Created call session: {session.call_id}")
            return session
        except Exception as e:
            logger.error(f"❌ Failed to create call session: {e}")
            raise

    async def end_call_session(self, call_id: str) -> bool:
        """End a call session"""
        try:
            result = await self.database.call_sessions.update_one(
                {"call_id": call_id},
                {
                    "$set": {
                        "status": "ended",
                        "ended_at": datetime.utcnow()
                    }
                }
            )

            if result.modified_count > 0:
                logger.info(f"✅ Ended call session: {call_id}")
                return True
            return False
        except Exception as e:
            logger.error(f"❌ Failed to end call session: {e}")
            return False

    async def get_call_session(self, call_id: str) -> Optional[CallSession]:
        """Get call session by ID"""
        try:
            session_data = await self.database.call_sessions.find_one({"call_id": call_id})
            if session_data:
                return dict_to_call_session(session_data)
            return None
        except Exception as e:
            logger.error(f"❌ Failed to get call session: {e}")
            return None

    # Transcript Operations
    async def save_transcript(self, call_id: str, speaker: str, message: str) -> TranscriptEntry:
        """Save a transcript entry"""
        try:
            entry = TranscriptEntry(
                call_id=call_id,
                speaker=speaker,
                message=message
            )

            await self.database.transcripts.insert_one(transcript_entry_to_dict(entry))
            logger.info(f"✅ Saved transcript entry for call: {call_id}")
            return entry
        except Exception as e:
            logger.error(f"❌ Failed to save transcript: {e}")
            raise

    async def get_call_transcripts(self, call_id: str) -> List[TranscriptEntry]:
        """Get all transcripts for a call"""
        try:
            cursor = self.database.transcripts.find({"call_id": call_id}).sort("timestamp", 1)
            transcripts = []

            async for transcript_data in cursor:
                transcripts.append(dict_to_transcript_entry(transcript_data))

            return transcripts
        except Exception as e:
            logger.error(f"❌ Failed to get transcripts for call {call_id}: {e}")
            return []

    async def get_recent_calls(self, limit: int = 20) -> List[CallSession]:
        """Get recent call sessions"""
        try:
            cursor = self.database.call_sessions.find({}).sort("started_at", -1).limit(limit)
            sessions = []

            async for session_data in cursor:
                sessions.append(dict_to_call_session(session_data))

            return sessions
        except Exception as e:
            logger.error(f"❌ Failed to get recent calls: {e}")
            return []


# Global database service instance
db_service = DatabaseService()