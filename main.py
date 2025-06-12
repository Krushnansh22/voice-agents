import json
import base64
from typing import Optional
import plivo
from plivo import plivoxml
import websockets
from fastapi import FastAPI, WebSocket, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
import asyncio
from settings import settings
import uvicorn
import warnings
import openpyxl

warnings.filterwarnings("ignore")
from dotenv import load_dotenv

load_dotenv()
records = []
p_index = 0

plivo_client = plivo.RestClient(settings.PLIVO_AUTH_ID, settings.PLIVO_AUTH_TOKEN)

# Configuration
OPENAI_API_KEY = settings.AZURE_OPENAI_API_KEY_P
OPENAI_API_ENDPOINT = settings.AZURE_OPENAI_API_ENDPOINT_P
SYSTEM_MESSAGE = (
    "You are a helpful and Medical assistant  "

)
VOICE = 'sage'
LOG_EVENT_TYPES = [
    'error', 'response.content.done', 'rate_limits.updated',
    'response.done', 'input_audio_buffer.committed',
    'input_audio_buffer.speech_stopped', 'input_audio_buffer.speech_started',
    'session.created'
]
SHOW_TIMING_MATH = False
app = FastAPI()

not_registered_user_msg = "Sorry, we couldn't find your registered number.I. If you need any assistance, feel free to reach out. Thank you for calling, and have a great day!"

if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')

def read_hospital_records(filename="Hospital_Records.xlsx"):
    global records
    wb = openpyxl.load_workbook(filename)
    ws = wb.active

    for row in ws.iter_rows(min_row=2, values_only=True):
        record = {
            "name": row[0],
            "phone_number": row[1],
            "address": row[2],
            "age": row[3],
            "gender": row[4],
            "appointment_date": row[5],
            "visit_reason": row[6]
        }
        records.append(record)


@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Stream Server is running!"}


@app.api_route("/webhook", methods=["GET", "POST"])
def home(request: Request):
    global p_index
    if request.method == "POST":
        #make calls here
        p_index +=1
        call_made = plivo_client.calls.create(
            from_=settings.PLIVO_FROM_NUMBER,
            to_=records[p_index]['phone_number'],
            answer_url=settings.PLIVO_ANSWER_XML,
            answer_method='GET')
        print("Webhook POST request detected!")

    xml_data = f'''<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Speak>Please wait while we connect your call to the AI Agent. OK you can start speaking.</Speak>
        <Stream streamTimeout="86400" keepCallAlive="true" bidirectional="true" contentType="audio/x-mulaw;rate=8000" audioTrack="inbound" >
            {settings.HOST_URL}/media-stream
        </Stream>
    </Response>
    '''
    return HTMLResponse(xml_data, media_type='application/xml')


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    # Extract caller phone number from Plivo request
    form_data = await request.form()
    caller_phone = form_data.get("From", "unknown")

    # Store caller phone in request state to use in websocket connection
    request.state.caller_phone = caller_phone

    # Get the base URLs for your application
    wss_host = settings.HOST_URL  # WebSocket URL
    # Convert WSS URL to HTTPS URL for action attributes
    http_host = wss_host.replace('wss://', 'https://')

    response = plivoxml.ResponseElement()

    # Use absolute HTTPS URL for the GetInput action
    get_input = plivoxml.GetInputElement() \
        .set_action(f"{http_host}/voice") \
        .set_method("POST") \
        .set_input_type("dtmf") \
        .set_redirect(True) \
        .set_language("en-US") \
        .set_num_digits(1)

    get_input.add_speak(
        content="To switch to Hindi, please press 5. To continue in English, press any other key.",
        voice="Polly.Salli",
        language="en-US"
    )

    # Add the GetInput element to the response
    response.add(get_input)

    # Add a message for when no selection is received
    response.add_speak(
        content="No selection received. Continuing in English.",
        voice="Polly.Salli",
        language="en-US"
    )

    return HTMLResponse('<?xml version="1.0" encoding="UTF-8"?>\n' + response.to_string(), media_type="application/xml")


@app.post("/voice")
async def voice_post(Digits: Optional[str] = Form(None)):
    """Handle the user's input"""
    response = plivoxml.ResponseElement()
    lang_code = 'en-US'

    if Digits == '5':  # User pressed 5, switch to Hindi
        lang_code = 'hi-IN'
        response.add(plivoxml.SpeakElement('नमस्ते, मैं आपकी कैसे मदद कर सकती हूँ?', language=lang_code))
    else:
        response.add(plivoxml.SpeakElement('Hello, How can I help you today?', language=lang_code))

    wss_host = settings.HOST_URL

    # Create stream element with WebSocket URL
    stream = response.add(plivoxml.StreamElement(f'{wss_host}/media-stream', extraHeaders=f"lang_code={lang_code}",
                                                 bidirectional=True,
                                                 streamTimeout=86400,  # 24 hours in seconds
                                                 keepCallAlive=True,
                                                 contentType="audio/x-mulaw;rate=8000",
                                                 audioTrack="inbound"
                                                 ))

    return HTMLResponse('<?xml version="1.0" encoding="UTF-8"?>\n' + stream.to_string(), media_type="application/xml")


@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI."""
    await websocket.accept()

    user_details = None

    async with websockets.connect(
            OPENAI_API_ENDPOINT,
            extra_headers={"api-key": OPENAI_API_KEY},
            ping_timeout=20,
            close_timeout=10
    ) as realtime_ai_ws:
        await initialize_session(realtime_ai_ws, user_details)

        # Connection specific state
        stream_sid = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None

        async def receive_from_twilio():
            """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
            nonlocal stream_sid, latest_media_timestamp
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data['event'] == 'media' and realtime_ai_ws.open:
                        latest_media_timestamp = int(data['media']['timestamp'])
                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": data['media']['payload']
                        }
                        await realtime_ai_ws.send(json.dumps(audio_append))
                    elif data['event'] == 'start':
                        stream_sid = data['start']['streamId']
                        print(f"Incoming stream has started {stream_sid}")
                        await realtime_ai_ws.send(json.dumps(data))
                        response_start_timestamp_twilio = None
                        latest_media_timestamp = 0
                        last_assistant_item = None
                    elif data['event'] == 'mark':
                        if mark_queue:
                            mark_queue.pop(0)
            except WebSocketDisconnect:
                print("Client disconnected.")
                if realtime_ai_ws.open:
                    await realtime_ai_ws.close()

        async def send_to_twilio():
            """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio
            try:
                async for openai_message in realtime_ai_ws:
                    response = json.loads(openai_message)
                    if response['type'] in LOG_EVENT_TYPES:
                        print(f"Received event: {response['type']}", response)

                    if response.get('type') == 'response.audio.delta' and 'delta' in response:
                        audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                        audio_delta = {
                            "event": "playAudio",
                            "media": {
                                "contentType": 'audio/x-mulaw',
                                "sampleRate": 8000,
                                "payload": audio_payload
                            }
                        }
                        await websocket.send_json(audio_delta)

                        if response_start_timestamp_twilio is None:
                            response_start_timestamp_twilio = latest_media_timestamp
                            if SHOW_TIMING_MATH:
                                print(f"Setting start timestamp for new response: {response_start_timestamp_twilio}ms")

                        # Update last_assistant_item safely
                        if response.get('item_id'):
                            last_assistant_item = response['item_id']

                        await send_mark(websocket, stream_sid)

                    # Trigger an interruption. Your use case might work better using `input_audio_buffer.speech_stopped`, or combining the two.
                    if response.get('type') == 'input_audio_buffer.speech_started':
                        print("Speech started detected.")
                        if last_assistant_item:
                            print(f"Interrupting response with id: {last_assistant_item}")
                            await handle_speech_started_event()
            except Exception as e:
                print(f"Error in send_to_twilio: {e}")

        async def handle_speech_started_event():
            """Handle interruption when the caller's speech starts."""
            nonlocal response_start_timestamp_twilio, last_assistant_item
            print("Handling speech started event.")
            if mark_queue and response_start_timestamp_twilio is not None:
                elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                if SHOW_TIMING_MATH:
                    print(
                        f"Calculating elapsed time for truncation: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms")

                if last_assistant_item:
                    if SHOW_TIMING_MATH:
                        print(f"Truncating item with ID: {last_assistant_item}, Truncated at: {elapsed_time}ms")

                    truncate_event = {
                        "type": "conversation.item.truncate",
                        "item_id": last_assistant_item,
                        "content_index": 0,
                        "audio_end_ms": elapsed_time
                    }
                    await realtime_ai_ws.send(json.dumps(truncate_event))

                await websocket.send_json({
                    "event": "clear",
                    "streamSid": stream_sid
                })

                mark_queue.clear()
                last_assistant_item = None
                response_start_timestamp_twilio = None

        async def send_mark(connection, stream_sid):
            if stream_sid:
                mark_event = {
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {"name": "responsePart"}
                }
                await connection.send_json(mark_event)
                mark_queue.append('responsePart')

        await asyncio.gather(receive_from_twilio(), send_to_twilio())


async def send_initial_conversation_item(realtime_ai_ws, user_details=None):
    """Send initial conversation item if AI talks first with personalized greeting."""
    greeting_name = user_details.get("FirstName", "there") if user_details else "there"

    # Directly send the greeting message (not instructions for the AI to generate one)
    initial_conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "assistant",
            "content": [{
                "type": "text",
                "text": f"Hello {greeting_name}! I am an AI voice assistant. How can I help you today?"
            }]
        }
    }
    await realtime_ai_ws.send(json.dumps(initial_conversation_item))
    await realtime_ai_ws.send(json.dumps({"type": "response.create"}))


async def initialize_session(realtime_ai_ws, user_details=None):
    """Control initial session with OpenAI."""
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": f"You are a helpful medical assistant/receptionist at INDRA IVF center nagpur which provides all solutions for IVF and related problems. You speak in hindi calm, supportive, composed tone. You are talking to {records[p_index]['name']}, a {records[p_index]['age']} years old {records[p_index]['gender']}. You have called them regarding taking a follow up.",
            "modalities": ["text", "audio"],
            "temperature": 0.8,
        }
    }
    print('Sending session update:', json.dumps(session_update))
    await realtime_ai_ws.send(json.dumps(session_update))

    # Uncomment the next line to have the AI speak first
    await send_initial_conversation_item(realtime_ai_ws, user_details)


@app.on_event("startup")
async def startup_event():
    pass

read_hospital_records("Hospital_Records.xlsx")
def main():
    call_made = plivo_client.calls.create(
        from_=settings.PLIVO_FROM_NUMBER,
        to_=records[p_index]['phone_number'],
        answer_url=settings.PLIVO_ANSWER_XML,
        answer_method='GET')
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)
main()
