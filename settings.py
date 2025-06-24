from pydantic_settings import BaseSettings
from pydantic import Extra
from dotenv import load_dotenv
load_dotenv()
 
class Settings(BaseSettings):
    PLIVO_AUTH_ID: str
    PLIVO_AUTH_TOKEN: str
    PLIVO_FROM_NUMBER: str
    PLIVO_TO_NUMBER: str
    PLIVO_ANSWER_XML: str
    AZURE_OPENAI_API_KEY_P: str
    AZURE_OPENAI_API_ENDPOINT_P: str
    HOST_URL: str
    PORT: int = 8090

    class Config:
        env_file = ".env"
        extra = Extra.allow

settings = Settings()