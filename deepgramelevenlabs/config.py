"""
Configuration file for Voice Assistant
Loads all settings from .env file
"""
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Deepgram Configuration
DEEPGRAM_CONFIG = {
    "api_key": os.getenv("DEEPGRAM_API_KEY", ""),
    "language": os.getenv("DEEPGRAM_LANGUAGE", "en"),
    "punctuate": os.getenv("DEEPGRAM_PUNCTUATE", "true").lower() == "true"
}

# Azure OpenAI Configuration
AZURE_OPENAI_CONFIG = {
    "api_key": os.getenv("AZURE_OPENAI_API_KEY", ""),
    "endpoint": os.getenv("AZURE_OPENAI_ENDPOINT", ""),
    "api_version": os.getenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview"),
    "model": os.getenv("AZURE_OPENAI_MODEL", "gpt-4o-mini"),
    "max_tokens": int(os.getenv("AZURE_OPENAI_MAX_TOKENS", "150")),
    "temperature": float(os.getenv("AZURE_OPENAI_TEMPERATURE", "0.7")),
    "system_message": os.getenv("AZURE_OPENAI_SYSTEM_MESSAGE",
                                "You are a helpful assistant. Keep responses concise and conversational.")
}

# ElevenLabs Configuration
ELEVENLABS_CONFIG = {
    "api_key": os.getenv("ELEVENLABS_API_KEY", ""),
    "voice_id": os.getenv("ELEVENLABS_VOICE_ID", ""),
    "stability": float(os.getenv("ELEVENLABS_STABILITY", "0.75")),
    "similarity_boost": float(os.getenv("ELEVENLABS_SIMILARITY_BOOST", "0.75"))
}

# Regular OpenAI Configuration (for switching)
OPENAI_CONFIG = {
    "api_key": os.getenv("OPENAI_API_KEY", ""),
    "model": os.getenv("OPENAI_MODEL", "gpt-3.5-turbo"),
    "max_tokens": int(os.getenv("OPENAI_MAX_TOKENS", "150")),
    "temperature": float(os.getenv("OPENAI_TEMPERATURE", "0.7")),
    "system_message": os.getenv("OPENAI_SYSTEM_MESSAGE",
                                "You are a helpful assistant. Keep responses concise and conversational.")
}

# MongoDB Configuration
MONGODB_CONFIG = {
    "url": os.getenv("MONGODB_URL", "mongodb://localhost:27017"),
    "database_name": os.getenv("DATABASE_NAME", "voice_assistant_db")
}


# Safe type conversion functions
def safe_int(value: str, default: int) -> int:
    """Safely convert string to int with fallback"""
    try:
        return int(value) if value else default
    except (ValueError, TypeError):
        return default


def safe_float(value: str, default: float) -> float:
    """Safely convert string to float with fallback"""
    try:
        return float(value) if value else default
    except (ValueError, TypeError):
        return default


def safe_bool(value: str, default: bool = False) -> bool:
    """Safely convert string to bool with fallback"""
    if not value:
        return default
    return value.lower() in ("true", "1", "yes", "on")


# Update configurations with safe type conversion
AZURE_OPENAI_CONFIG.update({
    "max_tokens": safe_int(os.getenv("AZURE_OPENAI_MAX_TOKENS"), 150),
    "temperature": safe_float(os.getenv("AZURE_OPENAI_TEMPERATURE"), 0.7)
})

OPENAI_CONFIG.update({
    "max_tokens": safe_int(os.getenv("OPENAI_MAX_TOKENS"), 150),
    "temperature": safe_float(os.getenv("OPENAI_TEMPERATURE"), 0.7)
})

ELEVENLABS_CONFIG.update({
    "stability": safe_float(os.getenv("ELEVENLABS_STABILITY"), 0.75),
    "similarity_boost": safe_float(os.getenv("ELEVENLABS_SIMILARITY_BOOST"), 0.75)
})

DEEPGRAM_CONFIG.update({
    "punctuate": safe_bool(os.getenv("DEEPGRAM_PUNCTUATE"), True)
})


# Validation function
def validate_config():
    """Validate that required environment variables are set"""
    required_vars = [
        "DEEPGRAM_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "ELEVENLABS_API_KEY",
        "ELEVENLABS_VOICE_ID"
    ]

    missing_vars = []
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)

    if missing_vars:
        print("❌ Missing required environment variables:")
        for var in missing_vars:
            print(f"   - {var}")
        print("\nPlease add them to your .env file")
        return False

    # Validate MongoDB connection string
    mongodb_url = MONGODB_CONFIG["url"]
    if not mongodb_url.startswith(("mongodb://", "mongodb+srv://")):
        print("❌ Invalid MongoDB URL format")
        return False

    print("✅ All required environment variables are set")
    print(f"📊 Database: {MONGODB_CONFIG['database_name']}")
    print(f"🔗 MongoDB URL: {mongodb_url}")
    return True