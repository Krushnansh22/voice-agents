"""
Text-to-Speech Services
"""
import requests
import os
from config import ELEVENLABS_CONFIG


class ElevenLabsService:
    def __init__(self, config=None):
        self.config = config or ELEVENLABS_CONFIG
        self.api_key = self.config["api_key"]
        self.voice_id = self.config["voice_id"]
        self.base_url = "https://api.elevenlabs.io/v1/text-to-speech"

    def text_to_speech(self, text, output_filename="ai_response.mp3"):
        """
        Convert text to speech and save as MP3

        Args:
            text (str): Text to convert
            output_filename (str): Output file name

        Returns:
            str: Path to saved file or None if failed
        """
        print("🔊 Converting text to speech with ElevenLabs...")

        url = f"{self.base_url}/{self.voice_id}/stream"

        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json"
        }

        data = {
            "text": text,
            "voice_settings": {
                "stability": self.config.get("stability", 0.75),
                "similarity_boost": self.config.get("similarity_boost", 0.75)
            }
        }

        try:
            response = requests.post(url, headers=headers, json=data, stream=True)

            if response.status_code == 200:
                with open(output_filename, 'wb') as audio_file:
                    audio_file.write(response.content)

                print(f"✅ Audio saved as '{output_filename}'")
                return output_filename

            else:
                print(f"❌ ElevenLabs error: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            print(f"❌ Text-to-speech error: {e}")
            return None


class AzureTTSService:
    """Alternative Azure TTS service"""

    def __init__(self, config=None):
        self.config = config
        # Initialize Azure TTS here

    def text_to_speech(self, text, output_filename="ai_response.wav"):
        # Implement Azure TTS
        pass


class GoogleTTSService:
    """Alternative Google TTS service"""

    def __init__(self, config=None):
        self.config = config
        # Initialize Google TTS here

    def text_to_speech(self, text, output_filename="ai_response.mp3"):
        # Implement Google TTS
        pass


class OpenAITTSService:
    """Alternative OpenAI TTS service"""

    def __init__(self, config=None):
        self.config = config
        # Initialize OpenAI TTS here

    def text_to_speech(self, text, output_filename="ai_response.mp3"):
        # Implement OpenAI TTS
        pass


class PiperTTSService:
    """Example local TTS service"""

    def __init__(self, config=None):
        self.config = config
        # Initialize Piper TTS here

    def text_to_speech(self, text, output_filename="ai_response.wav"):
        # Implement local TTS
        pass