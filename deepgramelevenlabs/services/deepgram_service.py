"""
Deepgram Speech-to-Text Service
"""
import requests
import os
from config import DEEPGRAM_CONFIG


class DeepgramService:
    def __init__(self, config=None):
        self.config = config or DEEPGRAM_CONFIG
        self.api_key = self.config["api_key"]
        self.base_url = "https://api.deepgram.com/v1/listen"

    def transcribe(self, audio_file_path):
        """
        Transcribe audio file to text

        Args:
            audio_file_path (str): Path to audio file

        Returns:
            str: Transcribed text or None if failed
        """
        print("🎤 Transcribing audio with Deepgram...")

        if not os.path.exists(audio_file_path):
            print(f"❌ Audio file '{audio_file_path}' not found!")
            return None

        # Determine content type based on file extension
        file_extension = os.path.splitext(audio_file_path)[1].lower()
        content_type_map = {
            '.mp3': 'audio/mp3',
            '.wav': 'audio/wav',
            '.m4a': 'audio/mp4',
            '.flac': 'audio/flac'
        }
        content_type = content_type_map.get(file_extension, 'audio/mp3')

        headers = {
            'Authorization': f'Token {self.api_key}',
            'Content-Type': content_type
        }

        params = {
            'punctuate': str(self.config.get("punctuate", True)).lower(),
            'language': self.config.get("language", "en")
        }

        try:
            with open(audio_file_path, 'rb') as audio_file:
                response = requests.post(
                    self.base_url,
                    headers=headers,
                    params=params,
                    data=audio_file
                )

            if response.status_code == 200:
                transcript = response.json()['results']['channels'][0]['alternatives'][0]['transcript']
                print(f"📝 Transcript: {transcript}")
                return transcript
            else:
                print(f"❌ Deepgram error: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            print(f"❌ Error transcribing audio: {e}")
            return None


# Alternative STT services can be added here
class OpenAIWhisperService:
    """Example alternative STT service"""

    def __init__(self, config=None):
        self.config = config
        # Initialize OpenAI Whisper here

    def transcribe(self, audio_file_path):
        # Implement OpenAI Whisper transcription
        pass


class GoogleSTTService:
    """Example alternative STT service"""

    def __init__(self, config=None):
        self.config = config
        # Initialize Google STT here

    def transcribe(self, audio_file_path):
        # Implement Google STT transcription
        pass