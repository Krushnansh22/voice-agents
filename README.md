# Call Agent Voice AI

This project is a FastAPI-based backend for a voice agent that integrates with Plivo for telephony and OpenAI (or Azure OpenAI) for real-time AI-powered conversations. It is designed to handle incoming calls, stream audio to an AI model, and return synthesized responses, supporting both English and Hindi.

## Features

- **Plivo Integration:** Handles incoming and outgoing calls using Plivo.
- **Real-Time AI Conversation:** Streams audio to OpenAI/Azure OpenAI for real-time transcription and response.
- **FastAPI Web Server:** Provides HTTP and WebSocket endpoints for call handling and media streaming.
- **Language Selection:** Callers can switch between English and Hindi via DTMF input.
- **Configurable via Environment Variables:** All sensitive and environment-specific settings are managed via `.env` and Pydantic settings.

## Requirements

- Python 3.8+
- Plivo account (for telephony)
- OpenAI or Azure OpenAI account (for AI responses)
- The dependencies listed in `requirements.txt`

## Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/HimanshuChelani27/voice-agents.git
   cd voice-agents
   ```

2. **Create and activate a virtual environment (optional but recommended):**
   ```bash
   python -m venv env
   source env/bin/activate  # On Windows: env\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up your `.env` file:**

   Create a `.env` file in the project root with the following variables:
   ```
   PLIVO_AUTH_ID=your_plivo_auth_id
   PLIVO_AUTH_TOKEN=your_plivo_auth_token
   PLIVO_FROM_NUMBER=your_plivo_number
   PLIVO_TO_NUMBER=destination_number
   PLIVO_ANSWER_XML=https://your-server.com/webhook
   AZURE_OPENAI_API_KEY_P=your_azure_openai_key
   AZURE_OPENAI_API_ENDPOINT_P=your_azure_openai_endpoint
   HOST_URL=wss://your-server.com
   PORT=8090
   ```

## Usage

1. **Run the server:**
   ```bash
   python main.py
   ```

   The server will start on the port specified in your `.env` (default: 8090).

2. **Endpoints:**
   - `GET /` — Health check endpoint.
   - `POST /webhook` — Plivo webhook for call events.
   - `POST /incoming-call` — Handles incoming calls and language selection.
   - `POST /voice` — Handles DTMF input for language selection.
   - `WebSocket /media-stream` — Streams audio between the caller and the AI.

3. **Call Flow:**
   - When a call is received, the system prompts the user to select a language.
   - The call audio is streamed to the AI model, which processes and responds in real time.
   - The conversation continues until the call ends.

## Customization

- **AI Model & Prompts:** You can modify the system prompt and voice in `main.py` (`SYSTEM_MESSAGE`, `VOICE`).
- **Language Support:** The `/voice` endpoint can be extended for more languages or custom prompts.

