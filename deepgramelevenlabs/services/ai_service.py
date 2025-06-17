"""
AI Response Services
"""
from openai import AzureOpenAI, OpenAI
from config import AZURE_OPENAI_CONFIG


class AzureOpenAIService:
    def __init__(self, config=None):
        self.config = config or AZURE_OPENAI_CONFIG
        self.client = AzureOpenAI(
            api_key=self.config["api_key"],
            api_version=self.config["api_version"],
            azure_endpoint=self.config["endpoint"]
        )

    def get_response(self, text):
        """
        Get AI response from Azure OpenAI

        Args:
            text (str): Input text

        Returns:
            str: AI response or None if failed
        """
        print("🤖 Getting response from Azure OpenAI...")

        try:
            response = self.client.chat.completions.create(
                model=self.config["model"],
                messages=[
                    {"role": "system", "content": self.config["system_message"]},
                    {"role": "user", "content": text}
                ],
                max_tokens=self.config.get("max_tokens", 150),
                temperature=self.config.get("temperature", 0.7)
            )

            ai_response = response.choices[0].message.content
            print(f"💬 AI Response: {ai_response}")
            return ai_response

        except Exception as e:
            print(f"❌ Azure OpenAI error: {e}")
            return None


class OpenAIService:
    """Alternative OpenAI service (non-Azure)"""

    def __init__(self, config=None):
        self.config = config
        self.client = OpenAI(api_key=config.get("api_key") if config else None)

    def get_response(self, text):
        """Get response from OpenAI"""
        print("🤖 Getting response from OpenAI...")

        try:
            response = self.client.chat.completions.create(
                model=self.config.get("model", "gpt-3.5-turbo"),
                messages=[
                    {"role": "system", "content": self.config.get("system_message", "You are a helpful assistant.")},
                    {"role": "user", "content": text}
                ],
                max_tokens=self.config.get("max_tokens", 150),
                temperature=self.config.get("temperature", 0.7)
            )

            ai_response = response.choices[0].message.content
            print(f"💬 AI Response: {ai_response}")
            return ai_response

        except Exception as e:
            print(f"❌ OpenAI error: {e}")
            return None


class AnthropicService:
    """Example Claude AI service"""

    def __init__(self, config=None):
        self.config = config
        # Initialize Anthropic client here

    def get_response(self, text):
        # Implement Claude AI response
        pass


class LocalLLMService:
    """Example local LLM service (Ollama, etc.)"""

    def __init__(self, config=None):
        self.config = config
        # Initialize local LLM here

    def get_response(self, text):
        # Implement local LLM response
        pass