from pydantic_settings import BaseSettings
from pydantic import Extra
from dotenv import load_dotenv

load_dotenv()


class Settings(BaseSettings):
    # Plivo Configuration
    PLIVO_AUTH_ID: str
    PLIVO_AUTH_TOKEN: str
    PLIVO_FROM_NUMBER: str
    PLIVO_TO_NUMBER: str
    PLIVO_ANSWER_XML: str

    # Azure OpenAI Configuration
    AZURE_OPENAI_API_KEY_P: str
    AZURE_OPENAI_API_ENDPOINT_P: str

    # Server Configuration
    HOST_URL: str
    PORT: int = 8090

    # Auto Hangup Configuration
    AUTO_HANGUP_DELAY: int = 3  # seconds to wait before hanging up
    HANGUP_URL: str = "https://7768-103-187-249-66.ngrok-free.app/hangup"

    # MongoDB Settings
    MONGODB_URL: str = "mongodb://localhost:27017"
    MONGODB_DATABASE: str = "voice_assistant_db"

    # Call Management Settings
    MAX_CALL_DURATION: int = 600  # 10 minutes max call duration
    CALL_RETRY_ATTEMPTS: int = 3
    CALL_RETRY_DELAY: int = 30  # seconds between retry attempts

    # Audio Settings
    AUDIO_SAMPLE_RATE: int = 8000
    AUDIO_FORMAT: str = "audio/x-mulaw"

    # AI Configuration
    AI_VOICE: str = "sage"
    AI_TEMPERATURE: float = 0.8
    AI_LANGUAGE: str = "hi"  # Hindi

    # Logging Configuration
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "call_center.log"

    # Excel Files Configuration
    PATIENT_RECORDS_FILE: str = "Hospital_Records.xlsx"
    APPOINTMENT_DETAILS_FILE: str = "Appointment_Details.xlsx"
    RESCHEDULE_REQUESTS_FILE: str = "Reschedule_Requests.xlsx"
    INCOMPLETE_CALLS_FILE: str = "Incomplete_Calls.xlsx"
    NOT_INTERESTED_CALLS_FILE: str = "Not_Interested_Calls.xlsx"

    # Security Settings
    ENABLE_CALL_RECORDING: bool = True
    ENCRYPT_PATIENT_DATA: bool = True

    class Config:
        env_file = ".env"
        extra = Extra.allow


settings = Settings()
