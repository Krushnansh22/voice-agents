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

   ### Core Application
   - `GET /` — Health check endpoint  
   - `GET /dashboard` — Transcript dashboard interface  
   - `GET /console` — Call center console interface  
   - `GET /status` — System status and statistics  

   ### Call Management
   - `POST /webhook` — Plivo webhook for call events and initiation  
   - `WebSocket /media-stream` — Audio streaming between caller and AI  
   - `POST /hangup` — Handle call hangup requests  

   ### Queue Management
   - `POST /api/upload-records` — Upload patient records Excel file  
   - `POST /api/queue/start` — Start the calling queue  
   - `POST /api/queue/pause` — Pause the calling queue  
   - `POST /api/queue/resume` — Resume paused queue  
   - `POST /api/queue/stop` — Stop the calling queue  
   - `POST /api/queue/reset` — Reset queue to beginning  
   - `POST /api/queue/skip-current` — Skip current call  
   - `GET /api/queue/status` — Get queue status and statistics  

   ### Data Retrieval
   - `GET /api/recent-calls` — Get recent call sessions  
   - `GET /api/call-transcripts/{call_id}` — Get transcripts for specific call  
   - `GET /appointment-details` — Get extracted appointment details  

   ### WebSocket Endpoints
   - `WebSocket /ws/transcripts` — Real-time transcript updates  
   - `WebSocket /ws/queue-status` — Real-time queue status updates  

3. **Call Flow:**
   - When a call is received, the system prompts the user to select a language.
   - The call audio is streamed to the AI model, which processes and responds in real time.
   - The conversation continues until the call ends.

## Customization

- **AI Model & Prompts:** You can modify the system prompt and voice in `main.py` (`SYSTEM_MESSAGE`, `VOICE`).
- **Language Support:** The `/voice` endpoint can be extended for more languages or custom prompts.

