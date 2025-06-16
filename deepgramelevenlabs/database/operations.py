"""
Database Operations for Voice Assistant - Simplified (No Session ID)
"""
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from .connection import get_database
from .models import (
    User, Conversation,
    user_to_dict, conversation_to_dict,
    dict_to_user, dict_to_conversation
)

logger = logging.getLogger(__name__)


class DatabaseOperations:
    """Database operations for voice assistant"""

    def __init__(self):
        self.db = None

    async def _get_db(self):
        """Get database connection"""
        if self.db is None:
            connection = await get_database()
            self.db = connection.database
        return self.db

    # User Operations (Users represent sessions)
    async def create_user(self, user_id: str = None, metadata: Dict[str, Any] = None) -> User:
        """Create a new user (which represents a session)"""
        try:
            db = await self._get_db()

            user = User(user_id=user_id) if user_id else User()
            if metadata:
                user.metadata.update(metadata)

            # Check if user already exists
            existing_user = await db.users.find_one({"user_id": user.user_id})
            if existing_user:
                logger.info(f"User {user.user_id} already exists")
                return dict_to_user(existing_user)

            # Insert new user
            result = await db.users.insert_one(user_to_dict(user))
            logger.info(f"✅ Created user: {user.user_id}")

            return user

        except Exception as e:
            logger.error(f"❌ Failed to create user: {e}")
            raise

    async def get_user(self, user_id: str) -> Optional[User]:
        """Get user by ID"""
        try:
            db = await self._get_db()
            user_data = await db.users.find_one({"user_id": user_id})

            if user_data:
                return dict_to_user(user_data)
            return None

        except Exception as e:
            logger.error(f"❌ Failed to get user {user_id}: {e}")
            return None

    async def update_user_activity(self, user_id: str) -> bool:
        """Update user's last activity and conversation count"""
        try:
            db = await self._get_db()

            # Get current conversation count
            conv_count = await db.conversations.count_documents({"user_id": user_id})

            result = await db.users.update_one(
                {"user_id": user_id},
                {
                    "$set": {
                        "total_conversations": conv_count
                    }
                }
            )
            return result.modified_count > 0

        except Exception as e:
            logger.error(f"❌ Failed to update user activity {user_id}: {e}")
            return False

    async def end_user_session(self, user_id: str) -> bool:
        """End a user session"""
        try:
            db = await self._get_db()

            # Get conversation count for this user
            conversation_count = await db.conversations.count_documents({"user_id": user_id})

            # Update user
            result = await db.users.update_one(
                {"user_id": user_id},
                {
                    "$set": {
                        "status": "ended",
                        "ended_at": datetime.utcnow(),
                        "total_conversations": conversation_count
                    }
                }
            )

            if result.modified_count > 0:
                logger.info(f"✅ Ended session for user: {user_id}")
                return True
            else:
                logger.warning(f"⚠️ User {user_id} not found or already ended")
                return False

        except Exception as e:
            logger.error(f"❌ Failed to end session for user {user_id}: {e}")
            return False

    async def get_all_users(self, limit: int = 20, offset: int = 0) -> List[User]:
        """Get all users"""
        try:
            db = await self._get_db()

            cursor = db.users.find({}).sort("created_at", -1).skip(offset).limit(limit)
            users = []

            async for user_data in cursor:
                users.append(dict_to_user(user_data))

            return users

        except Exception as e:
            logger.error(f"❌ Failed to get users: {e}")
            return []

    async def get_active_users(self, limit: int = 20) -> List[User]:
        """Get active users"""
        try:
            db = await self._get_db()

            cursor = db.users.find({"status": "active"}).sort("created_at", -1).limit(limit)
            users = []

            async for user_data in cursor:
                users.append(dict_to_user(user_data))

            return users

        except Exception as e:
            logger.error(f"❌ Failed to get active users: {e}")
            return []

    # Conversation Operations
    async def save_conversation(self, user_id: str, transcript: str,
                              ai_response: str, processing_time: float = None,
                              metadata: Dict[str, Any] = None) -> Conversation:
        """Save a conversation"""
        try:
            db = await self._get_db()

            conversation = Conversation(
                user_id=user_id,
                transcript=transcript,
                ai_response=ai_response,
                processing_time=processing_time
            )

            if metadata:
                conversation.metadata.update(metadata)

            # Insert conversation
            result = await db.conversations.insert_one(conversation_to_dict(conversation))
            logger.info(f"✅ Saved conversation: {conversation.conversation_id}")

            # Update user conversation count
            await self.update_user_activity(user_id)

            return conversation

        except Exception as e:
            logger.error(f"❌ Failed to save conversation for user {user_id}: {e}")
            raise

    async def get_user_conversations(self, user_id: str, limit: int = 50,
                                   offset: int = 0) -> List[Conversation]:
        """Get conversations for a user"""
        try:
            db = await self._get_db()

            cursor = db.conversations.find({"user_id": user_id}).sort("timestamp", 1).skip(offset).limit(limit)
            conversations = []

            async for conv_data in cursor:
                conversations.append(dict_to_conversation(conv_data))

            return conversations

        except Exception as e:
            logger.error(f"❌ Failed to get conversations for user {user_id}: {e}")
            return []

    async def get_conversation_count(self, user_id: str) -> int:
        """Get total conversation count for a user"""
        try:
            db = await self._get_db()
            return await db.conversations.count_documents({"user_id": user_id})

        except Exception as e:
            logger.error(f"❌ Failed to get conversation count for user {user_id}: {e}")
            return 0

    async def get_all_conversations(self, limit: int = 100, offset: int = 0) -> List[Conversation]:
        """Get all conversations from all users"""
        try:
            db = await self._get_db()

            cursor = db.conversations.find({}).sort("timestamp", -1).skip(offset).limit(limit)
            conversations = []

            async for conv_data in cursor:
                conversations.append(dict_to_conversation(conv_data))

            return conversations

        except Exception as e:
            logger.error(f"❌ Failed to get all conversations: {e}")
            return []

    # Analytics Operations
    async def get_user_stats(self, user_id: str) -> Dict[str, Any]:
        """Get user statistics"""
        try:
            db = await self._get_db()

            # Get user info
            user = await self.get_user(user_id)
            if not user:
                return {}

            # Get total conversations
            total_conversations = await db.conversations.count_documents({"user_id": user_id})

            return {
                "user_id": user_id,
                "status": user.status,
                "total_conversations": total_conversations,
                "created_at": user.created_at.isoformat(),
                "ended_at": user.ended_at.isoformat() if user.ended_at else None
            }

        except Exception as e:
            logger.error(f"❌ Failed to get stats for user {user_id}: {e}")
            return {}

    async def get_system_stats(self) -> Dict[str, Any]:
        """Get overall system statistics"""
        try:
            db = await self._get_db()

            # Get counts
            total_users = await db.users.count_documents({})
            active_users = await db.users.count_documents({"status": "active"})
            total_conversations = await db.conversations.count_documents({})

            # Get recent activity
            recent_users = await db.users.count_documents({
                "created_at": {"$gte": datetime.utcnow() - timedelta(hours=24)}
            })

            recent_conversations = await db.conversations.count_documents({
                "timestamp": {"$gte": datetime.utcnow() - timedelta(hours=24)}
            })

            return {
                "total_users": total_users,
                "active_users": active_users,
                "total_conversations": total_conversations,
                "recent_users_24h": recent_users,
                "recent_conversations_24h": recent_conversations,
                "timestamp": datetime.utcnow().isoformat()
            }

        except Exception as e:
            logger.error(f"❌ Failed to get system stats: {e}")
            return {}

    async def cleanup_old_users(self, days: int = 30) -> int:
        """Cleanup old ended users and their conversations"""
        try:
            db = await self._get_db()
            cutoff_date = datetime.utcnow() - timedelta(days=days)

            # Find old users
            old_users_cursor = db.users.find({
                "status": "ended",
                "ended_at": {"$lt": cutoff_date}
            })

            old_users = await old_users_cursor.to_list(length=None)
            user_ids = [user["user_id"] for user in old_users]

            if user_ids:
                # Delete conversations
                conv_result = await db.conversations.delete_many({"user_id": {"$in": user_ids}})

                # Delete users
                user_result = await db.users.delete_many({"user_id": {"$in": user_ids}})

                logger.info(f"🧹 Cleaned up {user_result.deleted_count} users and {conv_result.deleted_count} conversations")
                return user_result.deleted_count

            return 0

        except Exception as e:
            logger.error(f"❌ Failed to cleanup old users: {e}")
            return 0


# Global database operations instance
db_ops = DatabaseOperations()