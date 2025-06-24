"""
Simplified Database Models for Call Transcripts
"""
from datetime import datetime
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field
import uuid

class CallSession(BaseModel):
    """Call session model - represents each unique call"""
    call_id: str = Field(default_factory=lambda: f"call_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}")
    patient_name: str
    patient_phone: str
    status: str = "active"  # active, ended
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None
        }


class TranscriptEntry(BaseModel):
    """Individual transcript entry"""
    entry_id: str = Field(default_factory=lambda: f"entry_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}")
    call_id: str
    speaker: str  # "user" or "ai"
    message: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


# Conversion helpers
def call_session_to_dict(session: CallSession) -> Dict[str, Any]:
    """Convert CallSession to dictionary for MongoDB"""
    return {
        "call_id": session.call_id,
        "patient_name": session.patient_name,
        "patient_phone": session.patient_phone,
        "status": session.status,
        "started_at": session.started_at,
        "ended_at": session.ended_at
    }


def transcript_entry_to_dict(entry: TranscriptEntry) -> Dict[str, Any]:
    """Convert TranscriptEntry to dictionary for MongoDB"""
    return {
        "entry_id": entry.entry_id,
        "call_id": entry.call_id,
        "speaker": entry.speaker,
        "message": entry.message,
        "timestamp": entry.timestamp
    }


def dict_to_call_session(data: Dict[str, Any]) -> CallSession:
    """Convert dictionary to CallSession"""
    return CallSession(
        call_id=data["call_id"],
        patient_name=data["patient_name"],
        patient_phone=data["patient_phone"],
        status=data["status"],
        started_at=data["started_at"],
        ended_at=data.get("ended_at")
    )


def dict_to_transcript_entry(data: Dict[str, Any]) -> TranscriptEntry:
    """Convert dictionary to TranscriptEntry"""
    return TranscriptEntry(
        entry_id=data["entry_id"],
        call_id=data["call_id"],
        speaker=data["speaker"],
        message=data["message"],
        timestamp=data["timestamp"]
    )