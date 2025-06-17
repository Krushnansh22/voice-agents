"""
Database Models for Voice Assistant - Simplified (No Session ID)
"""
from datetime import datetime
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field
import uuid


class User(BaseModel):
    """User model - represents both user and session"""
    user_id: str = Field(default_factory=lambda: f"user_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}")
    status: str = "active"  # active, ended
    created_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None
    total_conversations: int = 0
    metadata: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None
        }


class Conversation(BaseModel):
    """Conversation model"""
    conversation_id: str = Field(default_factory=lambda: f"conv_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}")
    user_id: str
    transcript: str
    ai_response: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    processing_time: Optional[float] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


# Request/Response models for API
class StartSessionRequest(BaseModel):
    """Request to start a new session (create new user)"""
    metadata: Optional[Dict[str, Any]] = None


class StartSessionResponse(BaseModel):
    """Response for starting a session"""
    user_id: str
    status: str
    created_at: str


class EndSessionRequest(BaseModel):
    """Request to end a session"""
    user_id: str


class EndSessionResponse(BaseModel):
    """Response for ending a session"""
    user_id: str
    status: str
    ended_at: str
    total_conversations: int


class SaveConversationRequest(BaseModel):
    """Request to save a conversation"""
    user_id: str
    transcript: str
    ai_response: str
    processing_time: Optional[float] = None
    metadata: Optional[Dict[str, Any]] = None


class SaveConversationResponse(BaseModel):
    """Response for saving a conversation"""
    conversation_id: str
    user_id: str
    timestamp: str


class GetUserHistoryRequest(BaseModel):
    """Request to get user conversation history"""
    user_id: str
    limit: Optional[int] = 50
    offset: Optional[int] = 0


class GetUserHistoryResponse(BaseModel):
    """Response for user history"""
    user_id: str
    conversations: list
    total_count: int
    user_info: Dict[str, Any]


class GetAllUsersRequest(BaseModel):
    """Request to get all users"""
    limit: Optional[int] = 20
    offset: Optional[int] = 0


class GetAllUsersResponse(BaseModel):
    """Response for all users"""
    users: list
    total_count: int


# Database document conversion helpers
def conversation_to_dict(conversation: Conversation) -> Dict[str, Any]:
    """Convert Conversation model to dictionary for MongoDB"""
    return {
        "conversation_id": conversation.conversation_id,
        "user_id": conversation.user_id,
        "transcript": conversation.transcript,
        "ai_response": conversation.ai_response,
        "timestamp": conversation.timestamp,
        "processing_time": conversation.processing_time,
        "metadata": conversation.metadata
    }


def user_to_dict(user: User) -> Dict[str, Any]:
    """Convert User model to dictionary for MongoDB"""
    return {
        "user_id": user.user_id,
        "status": user.status,
        "created_at": user.created_at,
        "ended_at": user.ended_at,
        "total_conversations": user.total_conversations,
        "metadata": user.metadata
    }


def dict_to_conversation(data: Dict[str, Any]) -> Conversation:
    """Convert dictionary from MongoDB to Conversation model"""
    return Conversation(
        conversation_id=data["conversation_id"],
        user_id=data["user_id"],
        transcript=data["transcript"],
        ai_response=data["ai_response"],
        timestamp=data["timestamp"],
        processing_time=data.get("processing_time"),
        metadata=data.get("metadata", {})
    )


def dict_to_user(data: Dict[str, Any]) -> User:
    """Convert dictionary from MongoDB to User model"""
    return User(
        user_id=data["user_id"],
        status=data["status"],
        created_at=data["created_at"],
        ended_at=data.get("ended_at"),
        total_conversations=data.get("total_conversations", 0),
        metadata=data.get("metadata", {})
    )