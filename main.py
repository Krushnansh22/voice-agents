import json
import base64
from typing import Optional
import plivo
from plivo import plivoxml
import websockets
from fastapi import FastAPI, WebSocket, Request, Form, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
import asyncio

from database.models import call_session_to_dict, transcript_entry_to_dict
from settings import settings
import uvicorn
import warnings
import openpyxl
from openpyxl import Workbook
import os
from datetime import datetime, timedelta
import re

# MongoDB imports
from database.db_service import db_service
from database.websocket_manager import websocket_manager

warnings.filterwarnings("ignore")
from dotenv import load_dotenv

load_dotenv()
records = []
p_index = 0

# Global variable to store conversation transcripts
conversation_transcript = []

# Global variable to store current call session
current_call_session = None

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

not_registered_user_msg = "Sorry, we couldn't find your registered number. If you need any assistance, feel free to reach out. Thank you for calling, and have a great day!"

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
        }
        records.append(record)


def extract_appointment_details():
    """
    Extract date and time information from the conversation transcript.
    Returns a dictionary with extracted appointment details.
    """
    # Combine all transcripts into one text for analysis
    full_conversation = " ".join(conversation_transcript)

    extracted_info = {
        "appointment_date": None,
        "appointment_time": None,
        "time_slot": None,
        "raw_conversation": full_conversation
    }

    # Date patterns for Hindi/English dates
    date_patterns = [
        r'(\d{1,2}[-/]\d{1,2}[-/]\d{4})',  # DD-MM-YYYY or DD/MM/YYYY
        r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})',  # YYYY-MM-DD or YYYY/MM/DD
        r'(\d{1,2}\s*\w+\s*\d{4})',  # DD Month YYYY
    ]

    # Time slot patterns in Hindi
    time_patterns = [
        r'(सुबह)',  # Morning
        r'(दोपहर)',  # Afternoon
        r'(शाम)',  # Evening
        r'(रात)',  # Night
        r'(\d{1,2}:\d{2})',  # HH:MM format
        r'(\d{1,2}\s*बजे)',  # X o'clock in Hindi
    ]

    # Extract dates
    for pattern in date_patterns:
        matches = re.findall(pattern, full_conversation)
        if matches:
            extracted_info["appointment_date"] = matches[0]
            break

    # Extract time information
    for pattern in time_patterns:
        matches = re.findall(pattern, full_conversation, re.IGNORECASE)
        if matches:
            extracted_info["appointment_time"] = matches[0]
            break

    # Determine time slot based on Hindi words
    if 'सुबह' in full_conversation:
        extracted_info["time_slot"] = "morning"
    elif 'दोपहर' in full_conversation:
        extracted_info["time_slot"] = "afternoon"
    elif 'शाम' in full_conversation:
        extracted_info["time_slot"] = "evening"
    elif 'रात' in full_conversation:
        extracted_info["time_slot"] = "night"

    # Check if appointment was confirmed
    confirmation_keywords = ['बुक कर दिया', 'अपॉइंटमेंट', 'बुक', 'शानदार', 'ठीक है']
    extracted_info["appointment_confirmed"] = any(keyword in full_conversation for keyword in confirmation_keywords)

    return extracted_info


def append_appointment_to_excel(appointment_details, patient_record, filename="Appointment_Details.xlsx"):
    """
    Append appointment details to Excel file

    Args:
        appointment_details (dict): Dictionary containing appointment info
        patient_record (dict): Dictionary containing patient info
        filename (str): Excel filename to write to
    """
    headers = [
        "Name",
        "Appointment Date",
        "Time Slot",
        "Age",
        "Gender",
        "Phone Number",
        "Address",
    ]

    # Check if file exists
    if os.path.exists(filename):
        # Load existing workbook - THIS PRESERVES ALL EXISTING DATA
        wb = openpyxl.load_workbook(filename)
        ws = wb.active
        print(f"Loaded existing Excel file with {ws.max_row} rows of data")
    else:
        # Create new workbook with headers ONLY if file doesn't exist
        wb = Workbook()
        ws = wb.active
        ws.title = "Appointment Details"

        # Add headers
        for col, header in enumerate(headers, 1):
            ws.cell(row=1, column=col, value=header)
        print("Created new Excel file with headers")

    # Find the next empty row - THIS ENSURES NO OVERWRITING
    next_row = ws.max_row + 1
    print(f"Appending data to row {next_row}")

    # Prepare data row
    appointment_data = [
        patient_record.get('name', ''),
        appointment_details.get('appointment_date', ''),
        appointment_details.get('appointment_time', '') or appointment_details.get('time_slot', ''),
        patient_record.get('age', ''),
        patient_record.get('gender', ''),
        patient_record.get('phone_number', ''),
        patient_record.get('address', ''),
    ]

    # Add data to the next row
    for col, value in enumerate(appointment_data, 1):
        ws.cell(row=next_row, column=col, value=value)

    # Add timestamp for when the appointment was recorded
    ws.cell(row=next_row, column=len(headers) + 1, value=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # Save the workbook
    try:
        wb.save(filename)
        print(f"Appointment details saved to {filename} at row {next_row}")
        return True
    except Exception as e:
        print(f"Error saving appointment details: {e}")
        return False


@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Stream Server is running!"}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Serve the transcript dashboard"""
    with open("transcript_dashboard.html", "r", encoding="utf-8") as file:
        return HTMLResponse(content=file.read())


@app.websocket("/ws/transcripts")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time transcript updates"""
    await websocket_manager.connect(websocket)
    try:
        # Send initial connection confirmation
        await websocket.send_text(json.dumps({
            "type": "connection_status",
            "status": "connected",
            "timestamp": datetime.utcnow().isoformat()
        }))

        while True:
            try:
                # Set a timeout to prevent indefinite blocking
                message = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=30.0
                )

                # Parse and handle incoming messages
                try:
                    data = json.loads(message)

                    # Handle ping messages
                    if data.get("type") == "ping":
                        await websocket.send_text(json.dumps({
                            "type": "pong",
                            "timestamp": datetime.utcnow().isoformat()
                        }))

                    # Handle other message types as needed
                    print(f"Received from dashboard: {data}")

                except json.JSONDecodeError:
                    print(f"Invalid JSON received: {message}")

            except asyncio.TimeoutError:
                # Send keepalive ping
                try:
                    await websocket.send_text(json.dumps({
                        "type": "keepalive",
                        "timestamp": datetime.utcnow().isoformat()
                    }))
                except:
                    break  # Connection is broken

    except WebSocketDisconnect:
        print("Dashboard WebSocket disconnected")
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        websocket_manager.disconnect(websocket)


@app.get("/appointment-details")
async def get_appointment_details():
    """API endpoint to get extracted appointment details"""
    details = extract_appointment_details()
    return JSONResponse(details)


@app.api_route("/webhook", methods=["GET", "POST"])
def home(request: Request):
    global p_index
    if request.method == "POST":
        # make calls here
        p_index += 1
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


@app.get("/api/recent-calls")
async def get_recent_calls():
    """Get recent call sessions"""
    try:
        recent_calls = await db_service.get_recent_calls(limit=20)
        return [call_session_to_dict(call) for call in recent_calls]
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/call-transcripts/{call_id}")
async def get_call_transcripts(call_id: str):
    """Get transcripts for a specific call"""
    try:
        transcripts = await db_service.get_call_transcripts(call_id)
        return [transcript_entry_to_dict(transcript) for transcript in transcripts]
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI."""
    global conversation_transcript, current_call_session

    await websocket.accept()

    # Create new call session in MongoDB
    patient_record = records[p_index] if p_index < len(records) else {"name": "Unknown", "phone_number": "Unknown"}
    current_call_session = await db_service.create_call_session(
        patient_name=patient_record.get("name", "Unknown"),
        patient_phone=patient_record.get("phone_number", "Unknown")
    )

    # Broadcast call started status
    await websocket_manager.broadcast_call_status(
        call_id=current_call_session.call_id,
        status="started",
        patient_name=current_call_session.patient_name
    )

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

                # End call session in MongoDB
                if current_call_session:
                    await db_service.end_call_session(current_call_session.call_id)
                    await websocket_manager.broadcast_call_status(
                        call_id=current_call_session.call_id,
                        status="ended"
                    )

        async def send_to_twilio():
            """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio
            try:
                async for openai_message in realtime_ai_ws:
                    response = json.loads(openai_message)
                    if response['type'] in LOG_EVENT_TYPES:
                        try:
                            transcript = response['response']['output'][0]['content'][0]['transcript']
                            print(f"AI Response: {transcript}")

                            # Store AI response in MongoDB and broadcast
                            if current_call_session:
                                await db_service.save_transcript(
                                    call_id=current_call_session.call_id,
                                    speaker="ai",
                                    message=transcript
                                )

                                # Broadcast to WebSocket clients
                                await websocket_manager.broadcast_transcript(
                                    call_id=current_call_session.call_id,
                                    speaker="ai",
                                    message=transcript,
                                    timestamp=datetime.utcnow().isoformat()
                                )

                            # Store transcript in global variable
                            if "बुक कर दिया है" in transcript:
                                conversation_transcript.append(transcript)

                            # Extract appointment details after each AI response
                            current_details = extract_appointment_details()
                            if current_details["appointment_date"] or current_details["appointment_time"]:
                                # Call the function to append data to Excel before printing
                                append_appointment_to_excel(current_details, records[p_index])
                                print(f"*** Appointment Info Detected: {current_details} ***")

                        except (KeyError, IndexError):
                            print("No transcript found in response")

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

                    # Handle user speech (transcript from speech_started events would need additional processing)
                    if response.get('type') == 'input_audio_buffer.speech_started':
                        print("Speech started detected.")
                        if last_assistant_item:
                            print(f"Interrupting response with id: {last_assistant_item}")
                            await handle_speech_started_event()

                    # Handle user transcript (if available in response)
                    if response.get('type') == 'conversation.item.input_audio_transcription.completed':
                        try:
                            user_transcript = response.get('transcript', '')
                            if user_transcript and current_call_session:
                                print(f"User Transcript: {user_transcript}")

                                # Store user transcript in MongoDB and broadcast
                                await db_service.save_transcript(
                                    call_id=current_call_session.call_id,
                                    speaker="user",
                                    message=user_transcript
                                )

                                # Broadcast to WebSocket clients
                                await websocket_manager.broadcast_transcript(
                                    call_id=current_call_session.call_id,
                                    speaker="user",
                                    message=user_transcript,
                                    timestamp=datetime.utcnow().isoformat()
                                )
                        except Exception as e:
                            print(f"Error processing user transcript: {e}")

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
            "instructions": f'''AI ROLE: Female voice receptionist from Aveya IVF, Rajouri Garden
LANGUAGE: Hindi (देवनागरी लिपि)
VOICE STYLE: Calm, friendly, trustworthy, emotionally intelligent, feminine
GENDER CONSISTENCY: Use feminine forms (e.g., "बोल रही हूँ", "कर सकती हूँ", "समझ सकती हूँ")
GOAL: Invite the user for a free fertility clarity consultation and handle their responses accordingly
you are talking to {records[p_index]['name']}, a {records[p_index]['age']} years old {records[p_index]['gender']}.
"नमस्ते {{First_Name}}, मैं Aveya IVF, से Rekha बोल रही हूँ। कैसे हैं आप आज?"

(रुकें, उत्तर सुनें)

"मैं आपसे यह पूछने के लिए कॉल कर रही हूँ कि क्या आप एक फ्री फर्टिलिटी क्लैरिटी कंसल्टेशन के लिए अपॉइंटमेंट लेना चाहेंगे?"

IF USER SAYS YES / INTERESTED:

"बहुत बढ़िया! मैं आपको आने वाले कुछ दिनों की तारीखें बताती हूँ —"

"क्या आप {(datetime.today() + timedelta(days=1)).strftime("%d-%m-%Y")}, {(datetime.today() + timedelta(days=2)).strftime("%d-%m-%Y")}, या {(datetime.today() + timedelta(days=3)).strftime("%d-%m-%Y")} को आना पसंद करेंगे?"

(रुकें, तारीख चुनने दें)

"और उस दिन आपको कौन-सा समय ठीक लगेगा — सुबह, दोपहर या शाम?"

(रुकें, समय चुनने दें)

"शानदार! तो मैंने आपका अपॉइंटमेंट {{चुनी हुई तारीख}} को {{चुना हुआ समय}} के लिए बुक कर दिया है।"

IF USER SAYS NO / NOT NOW:

"कोई बात नहीं — जब भी आप तैयार महसूस करें, हम हमेशा उपलब्ध हैं। धन्यवाद!"''',
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
    """Initialize database connection on startup"""
    connected = await db_service.connect()
    if not connected:
        raise RuntimeError("Failed to connect to MongoDB")
    print("✅ Application started with MongoDB connection")


@app.on_event("shutdown")
async def shutdown_event():
    """Close database connection on shutdown"""
    await db_service.disconnect()


read_hospital_records("Hospital_Records.xlsx")


def main():
    call_made = plivo_client.calls.create(
        from_=settings.PLIVO_FROM_NUMBER,
        to_=records[p_index]['phone_number'],
        answer_url=settings.PLIVO_ANSWER_XML,
        answer_method='GET')
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)

if __name__ =="__main__":
    main()
