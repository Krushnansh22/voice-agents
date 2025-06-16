"""
MongoDB Database Connection - Simplified Schema
"""
import os
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class DatabaseConnection:
    """Manages MongoDB connection for the voice assistant"""

    def __init__(self, mongodb_url: str, database_name: str):
        self.mongodb_url = mongodb_url
        self.database_name = database_name
        self.client: Optional[AsyncIOMotorClient] = None
        self.database = None

    async def connect(self):
        """Establish connection to MongoDB"""
        try:
            self.client = AsyncIOMotorClient(self.mongodb_url)
            self.database = self.client[self.database_name]

            # Test the connection
            await self.client.admin.command('ping')
            logger.info(f"✅ Connected to MongoDB: {self.database_name}")

            # Create indexes for better performance
            await self.create_indexes()

            return True

        except Exception as e:
            logger.error(f"❌ Failed to connect to MongoDB: {e}")
            return False

    async def disconnect(self):
        """Close MongoDB connection"""
        if self.client:
            self.client.close()
            logger.info("🔌 Disconnected from MongoDB")

    async def create_indexes(self):
        """Create database indexes for better performance"""
        try:
            # Users collection indexes (users represent sessions now)
            users = self.database.users
            await users.create_index("user_id", unique=True)
            await users.create_index("created_at")
            await users.create_index("status")
            await users.create_index("ended_at")

            # Conversations collection indexes
            conversations = self.database.conversations
            await conversations.create_index("conversation_id", unique=True)
            await conversations.create_index("user_id")
            await conversations.create_index("timestamp")
            await conversations.create_index([("user_id", 1), ("timestamp", 1)])  # Compound index

            logger.info("✅ Database indexes created successfully")

        except Exception as e:
            logger.warning(f"⚠️ Failed to create indexes: {e}")

    def get_collection(self, collection_name: str):
        """Get a specific collection"""
        if not self.database:
            raise RuntimeError("Database not connected")
        return self.database[collection_name]

    async def health_check(self) -> bool:
        """Check if database connection is healthy"""
        try:
            if not self.client:
                return False
            await self.client.admin.command('ping')
            return True
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
            return False


# Global database instance
db_connection = None


async def get_database():
    """Get database connection (singleton pattern)"""
    global db_connection

    if db_connection is None:
        from config import MONGODB_CONFIG
        db_connection = DatabaseConnection(
            mongodb_url=MONGODB_CONFIG["url"],
            database_name=MONGODB_CONFIG["database_name"]
        )

        connected = await db_connection.connect()
        if not connected:
            raise RuntimeError("Failed to connect to MongoDB")

    return db_connection


async def init_database():
    """Initialize database connection"""
    return await get_database()


async def close_database():
    """Close database connection"""
    global db_connection
    if db_connection:
        await db_connection.disconnect()
        db_connection = None