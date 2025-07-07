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
    GEMINI_API_KEY: str

    # Server Configuration
    HOST_URL: str
    HTTPS_HOST_URL: str
    PORT: int = 8090

    # Auto Hangup Configuration
    AUTO_HANGUP_DELAY: int = 3  # seconds to wait before hanging up
    HANGUP_URL: str

    # MongoDB Settings
    MONGODB_URL: str
    MONGODB_DATABASE: str = "voice_assistant_db"

    # Google Service Account Configuration (from environment variables)
    GOOGLE_SERVICE_ACCOUNT_TYPE: str = "service_account"
    GOOGLE_PROJECT_ID: str
    GOOGLE_PRIVATE_KEY_ID: str
    GOOGLE_PRIVATE_KEY: str
    GOOGLE_CLIENT_EMAIL: str
    GOOGLE_CLIENT_ID: str
    GOOGLE_AUTH_URI: str = "https://accounts.google.com/o/oauth2/auth"
    GOOGLE_TOKEN_URI: str = "https://oauth2.googleapis.com/token"
    GOOGLE_AUTH_PROVIDER_X509_CERT_URL: str = "https://www.googleapis.com/oauth2/v1/certs"
    GOOGLE_CLIENT_X509_CERT_URL: str
    GOOGLE_UNIVERSE_DOMAIN: str = "googleapis.com"

    # Google Sheets Configuration
    GOOGLE_SERVICE_ACCOUNT_FILE: str = "credentials.json"  # Path to service account JSON file (fallback)
    DEFAULT_SHEET_ID: str

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

    # Excel Files Configuration (keeping for backward compatibility)
    PATIENT_RECORDS_FILE: str = "Hospital_Records.xlsx"
    APPOINTMENT_DETAILS_FILE: str = "Appointment_Details.xlsx"
    RESCHEDULE_REQUESTS_FILE: str = "Reschedule_Requests.xlsx"
    INCOMPLETE_CALLS_FILE: str = "Incomplete_Calls.xlsx"
    NOT_INTERESTED_CALLS_FILE: str = "Not_Interested_Calls.xlsx"

    # Security Settings
    ENABLE_CALL_RECORDING: bool = True
    ENCRYPT_PATIENT_DATA: bool = True

    def get_google_credentials_dict(self) -> dict:
        """Get Google service account credentials as a dictionary"""
        return {
            "type": self.GOOGLE_SERVICE_ACCOUNT_TYPE,
            "project_id": self.GOOGLE_PROJECT_ID,
            "private_key_id": self.GOOGLE_PRIVATE_KEY_ID,
            "private_key": self.GOOGLE_PRIVATE_KEY.replace('\\n', '\n'),  # Handle newlines in private key
            "client_email": self.GOOGLE_CLIENT_EMAIL,
            "client_id": self.GOOGLE_CLIENT_ID,
            "auth_uri": self.GOOGLE_AUTH_URI,
            "token_uri": self.GOOGLE_TOKEN_URI,
            "auth_provider_x509_cert_url": self.GOOGLE_AUTH_PROVIDER_X509_CERT_URL,
            "client_x509_cert_url": self.GOOGLE_CLIENT_X509_CERT_URL,
            "universe_domain": self.GOOGLE_UNIVERSE_DOMAIN
        }

    class Config:
        env_file = ".env"
        extra = Extra.allow


settings = Settings()