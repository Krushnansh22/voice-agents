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
import threading
import time
import httpx
import logging

# MongoDB imports
from database.db_service import db_service
from database.websocket_manager import websocket_manager

warnings.filterwarnings("ignore")
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
records = []
last_processed_count = 0
called_numbers = []  # Track all called numbers for summary
p_index = 0  # Current call index - advances after each call
call_in_progress = False  # Prevent multiple simultaneous calls
MAX_CALL_DURATION = 300  # 5 minutes in seconds
call_timer_task = None
call_uuid_storage = {}  # Store UUIDs by p_index
current_call_uuid = None  # Current active call UUID

# Global variables for call tracking
call_start_time = None
call_outcome_detected = False

app = FastAPI()

# Global variable to store conversation transcripts
conversation_transcript = []

# Global variable to store current call session
current_call_session = None

plivo_client = plivo.RestClient(settings.PLIVO_AUTH_ID, settings.PLIVO_AUTH_TOKEN)

# Configuration
OPENAI_API_KEY = settings.AZURE_OPENAI_API_KEY_P
OPENAI_API_ENDPOINT = settings.AZURE_OPENAI_API_ENDPOINT_P
SYSTEM_MESSAGE = (
    "You are a helpful and Medical assistant"
)
VOICE = 'sage'
LOG_EVENT_TYPES = [
    'error', 'response.content.done', 'rate_limits.updated',
    'response.done', 'input_audio_buffer.committed',
    'input_audio_buffer.speech_stopped', 'input_audio_buffer.speech_started',
    'session.created', 'conversation.item.input_audio_transcription.completed'
]
SHOW_TIMING_MATH = False

not_registered_user_msg = "Sorry, we couldn't find your registered number. If you need any assistance, feel free to reach out. Thank you for calling, and have a great day!"

if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')


class CallHangupManager:
    """Manages automatic call hangup after successful outcomes"""

    def __init__(self, delay_seconds: int = 7):
        self.delay_seconds = delay_seconds
        self.pending_hangups = set()

    async def schedule_hangup(self, call_uuid: str, reason: str):
        """Schedule a call hangup after delay"""
        global current_call_uuid

        call_uuid = current_call_uuid
        if call_uuid in self.pending_hangups:
            return

        self.pending_hangups.add(call_uuid)
        logger.info(f"🔚 Scheduling hangup for call {call_uuid} in {self.delay_seconds}s - Reason: {reason}")

        # Wait for delay to let AI finish speaking
        await asyncio.sleep(self.delay_seconds)

        try:
            success = await self.execute_hangup(call_uuid)
            if success:
                logger.info(f"✅ Successfully hung up call {call_uuid}")
            else:
                logger.error(f"❌ Failed to hang up call {call_uuid}")
        except Exception as e:
            logger.error(f"❌ Error hanging up call {call_uuid}: {e}")
        finally:
            self.pending_hangups.discard(call_uuid)

    async def execute_hangup(self, call_uuid: str) -> bool:
        """Execute the actual hangup using Plivo API"""
        try:
            # Use Plivo client to hangup the call
            response = plivo_client.calls.hangup(call_uuid=call_uuid)
            logger.info(f"Plivo hangup response: {response}")
            return True
        except Exception as e:
            logger.error(f"Exception during Plivo hangup: {e}")
            return False


# Global instance of CallHangupManager
hangup_manager = CallHangupManager()


def extract_appointment_details():
    """
    Extract date, time, and doctor information from the conversation transcript.
    Returns a dictionary with extracted appointment details.
    """
    # Combine all transcripts into one text for analysis
    full_conversation = " ".join(conversation_transcript)

    extracted_info = {
        "appointment_date": None,
        "appointment_time": None,
        "time_slot": None,
        "doctor_name": "Doctor",  # Default doctor name
        "raw_conversation": full_conversation,
        "appointment_confirmed": False
    }

    # Enhanced date patterns for Hindi/English dates
    date_patterns = [
        r'(\d{1,2}[-/]\d{1,2}[-/]\d{4})',  # DD-MM-YYYY or DD/MM/YYYY
        r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})',  # YYYY-MM-DD or YYYY/MM/DD
        r'(\d{1,2}\s*\w+\s*\d{4})',  # DD Month YYYY
        r'(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)',  # English days
        r'(सोमवार|मंगलवार|बुधवार|गुरुवार|शुक्रवार|शनिवार|रविवार)',  # Hindi days
        r'(आज|कल|परसों)',  # Today, tomorrow, day after tomorrow
        r'(tomorrow|today|day after tomorrow)',  # English equivalents
    ]

    # Enhanced time slot patterns
    time_patterns = [
        r'(morning|सुबह)',  # Morning
        r'(afternoon|दोपहर)',  # Afternoon
        r'(evening|शाम)',  # Evening
        r'(\d{1,2}:\d{2})',  # HH:MM format
        r'(\d{1,2}\s*बजे)',  # X o'clock in Hindi
        r'(\d{1,2}\s*से\s*\d{1,2}:\d{2})',  # Time range
        r'(\d{1,2}\s*AM|\d{1,2}\s*PM)',  # AM/PM format
        r'(\d{1,2}\s*से\s*\d{1,2})',  # X से Y format
    ]

    # Doctor name patterns (updated for flexibility)
    doctor_patterns = [
        r'डॉ\.\s*(\w+)',  # Dr. [Name]
        r'डॉक्टर\s*(\w+)',  # Doctor [Name]
        r'डॉ\s*(\w+)',  # Dr [Name] without dot
        r'doctor\s*([^,\s]+)',  # English doctor pattern
    ]

    # Extract dates
    for pattern in date_patterns:
        matches = re.findall(pattern, full_conversation, re.IGNORECASE)
        if matches:
            extracted_info["appointment_date"] = matches[0]
            break

    # Extract time information
    for pattern in time_patterns:
        matches = re.findall(pattern, full_conversation, re.IGNORECASE)
        if matches:
            extracted_info["appointment_time"] = matches[0]
            break

    # Extract doctor name
    for pattern in doctor_patterns:
        matches = re.findall(pattern, full_conversation, re.IGNORECASE)
        if matches:
            extracted_info["doctor_name"] = f"डॉ. {matches[0]}"
            break

    # Determine time slot based on words found
    conversation_lower = full_conversation.lower()
    if 'morning' in conversation_lower or 'सुबह' in conversation_lower:
        extracted_info["time_slot"] = "morning"
    elif 'afternoon' in conversation_lower or 'दोपहर' in conversation_lower:
        extracted_info["time_slot"] = "afternoon"
    elif 'evening' in conversation_lower or 'शाम' in conversation_lower:
        extracted_info["time_slot"] = "evening"
    elif 'night' in conversation_lower or 'रात' in conversation_lower:
        extracted_info["time_slot"] = "night"

    # Check if appointment was confirmed with updated keywords
    confirmation_keywords = [
        "slot book कर लिया",
        "बुक कर दिया है",
        "अपॉइंटमेंट.*बुक.*है",
        "आपका अपॉइंटमेंट.*फिक्स",
        "तो मैंने.*बुक कर दिया",
        "शानदार.*बुक कर दिया"
    ]
    extracted_info["appointment_confirmed"] = any(
        re.search(keyword, full_conversation, re.IGNORECASE) for keyword in confirmation_keywords
    )

    return extracted_info


def detect_reschedule_request():
    """
    Detect if the conversation indicates a reschedule request
    Returns True if reschedule detected, False otherwise
    """
    full_conversation = " ".join(conversation_transcript)

    # Primary reschedule indicators
    reschedule_patterns = [
        r'बिल्कुल समझ सकती हूँ.*कोई बात नहीं',
        r'आप बताइए कि कब.*कॉल करना ठीक',
        r'कब कॉल करना ठीक लगेगा',
        r'कोई खास दिन सूट करता है',
        r'समय के बारे में.*सुबह.*दोपहर.*शाम',
        r'बाद में.*कॉल.*करें',
        r'अभी.*समय.*नहीं',
        r'व्यस्त.*हूं',
        r'कल.*कॉल.*करना',
        r'शाम.*को.*कॉल',
        r'सुबह.*कॉल.*करें',
        r'अगले.*हफ्ते',
        r'partner से पूछना है',
        r'tentative slot hold कर लेती हूँ'
    ]

    for pattern in reschedule_patterns:
        if re.search(pattern, full_conversation, re.IGNORECASE):
            return True

    return False


def extract_reschedule_details():
    """
    Extract reschedule callback details from conversation
    Returns dictionary with callback preferences
    """
    full_conversation = " ".join(conversation_transcript)

    callback_info = {
        "callback_date": None,
        "callback_time": None,
        "callback_day": None,
        "callback_period": None,
        "raw_conversation": full_conversation
    }

    # Enhanced date patterns
    date_patterns = [
        (r'(\d{1,2}[-/]\d{1,2}[-/]\d{4})', 'dd-mm-yyyy'),
        (r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})', 'yyyy-mm-dd'),
        (r'(\d{1,2})\s*(जनवरी|फरवरी|मार्च|अप्रैल|मई|जून|जुलाई|अगस्त|सितंबर|अक्टूबर|नवंबर|दिसंबर)', 'dd-month-hindi'),
    ]

    # Time patterns
    time_patterns = [
        (r'(\d{1,2}:\d{2})', 'hh:mm'),
        (r'(\d{1,2})\s*बजे', 'hindi-hour'),
        (r'(\d{1,2})\s*(AM|PM|am|pm)', 'english-ampm'),
        (r'(सुबह)\s*(\d{1,2})', 'morning-hour'),
        (r'(शाम)\s*(\d{1,2})', 'evening-hour'),
        (r'(दोपहर)\s*(\d{1,2})', 'afternoon-hour'),
    ]

    # Day patterns
    day_patterns = [
        (r'(सोमवार|monday)', 'Monday'),
        (r'(मंगलवार|tuesday)', 'Tuesday'),
        (r'(बुधवार|wednesday)', 'Wednesday'),
        (r'(गुरुवार|thursday)', 'Thursday'),
        (r'(शुक्रवार|friday)', 'Friday'),
        (r'(शनिवार|saturday)', 'Saturday'),
        (r'(रविवार|sunday)', 'Sunday'),
        (r'(कल)', 'Tomorrow'),
        (r'(परसों)', 'Day After Tomorrow'),
    ]

    # Period patterns
    period_patterns = [
        (r'(सुबह|morning)', 'Morning'),
        (r'(दोपहर|afternoon)', 'Afternoon'),
        (r'(शाम|evening)', 'Evening'),
        (r'(रात|night)', 'Night'),
    ]

    # Extract information using patterns
    for pattern, date_type in date_patterns:
        matches = re.findall(pattern, full_conversation, re.IGNORECASE)
        if matches:
            callback_info["callback_date"] = matches[0] if isinstance(matches[0], str) else ' '.join(matches[0])
            break

    for pattern, time_type in time_patterns:
        matches = re.findall(pattern, full_conversation, re.IGNORECASE)
        if matches:
            callback_info["callback_time"] = matches[0] if isinstance(matches[0], str) else ' '.join(matches[0])
            break

    for pattern, normalized_day in day_patterns:
        if re.search(pattern, full_conversation, re.IGNORECASE):
            callback_info["callback_day"] = normalized_day
            break

    for pattern, normalized_period in period_patterns:
        if re.search(pattern, full_conversation, re.IGNORECASE):
            callback_info["callback_period"] = normalized_period
            break

    return callback_info


def should_terminate_call(transcript):
    """Check if call should be terminated based on transcript content"""
    import re

    # Specific farewell phrases from the script
    definitive_farewell_phrases = [
        "आपका दिन शुभ हो",
        "आपका दिन मंगलमय हो",
        "अलविदा, आपका दिन अच्छा हो",
        "Take care! आपका दिन शुभ हो",
        "धन्यवाद! आपका दिन मंगलमय हो"
    ]

    # Enhanced regex patterns for farewell detection
    farewell_patterns = [
        # Definitive farewell endings from script
        r'.?आपका दिन शुभ हो\s*[।!]?\s*$',
        r'.?आपका दिन मंगलमय हो\s*[।!]?\s*$',
        r'.?अलविदा.*आपका दिन अच्छा हो\s*[।!]?\s*$',
        r'.?Take care.*आपका दिन शुभ हो\s*[।!]?\s*$',
        r'.?धन्यवाद.*आपका दिन मंगलमय हो\s*[।!]?\s*$',

        # Available for help + farewell
        r'available हूँ.*आपका दिन शुभ हो',
        r'help के लिए.*आपका दिन.*हो',
        r'WhatsApp पर भेज रही हूँ.*doubt हो.*message करिए.*(आपका दिन.*हो)'
    ]

    # Check each pattern
    for pattern in farewell_patterns:
        if re.search(pattern, transcript, re.IGNORECASE | re.DOTALL):
            return True, "goodbye_detected"

    # Check if transcript ends with definitive goodbye phrases
    transcript_cleaned = transcript.strip()
    for phrase in definitive_farewell_phrases:
        if transcript_cleaned.endswith(phrase) or phrase in transcript_cleaned[-50:]:  # Check last 50 characters
            return True, "goodbye_detected"

    return False, None


def append_appointment_to_excel(appointment_details, patient_record, filename="Appointment_Details.xlsx"):
    """
    Append appointment details to Excel file with doctor name
    """
    headers = [
        "Name",
        "Appointment Date",
        "Time Slot",
        "Doctor Name",  # Added doctor name column
        "Age",
        "Gender",
        "Phone Number",
        "Address",
        "Timestamp"
    ]

    # Check if file exists
    if os.path.exists(filename):
        # Load existing workbook - THIS PRESERVES ALL EXISTING DATA
        wb = openpyxl.load_workbook(filename)
        ws = wb.active
        print(f"📊 Loaded existing Excel file with {ws.max_row} rows of data")
    else:
        # Create new workbook with headers ONLY if file doesn't exist
        wb = Workbook()
        ws = wb.active
        ws.title = "Appointment Details"

        # Add headers with formatting
        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = openpyxl.styles.Font(bold=True)

        print("📝 Created new Excel file with headers")

    # Find the next empty row - THIS ENSURES NO OVERWRITING
    next_row = ws.max_row + 1
    print(f"➕ Appending data to row {next_row}")

    # Prepare data row with doctor name
    appointment_data = [
        patient_record.get('name', ''),
        appointment_details.get('appointment_date', ''),
        appointment_details.get('appointment_time', '') or appointment_details.get('time_slot', ''),
        appointment_details.get('doctor_name', 'डॉ. निशा'),  # Added doctor name
        patient_record.get('age', ''),
        patient_record.get('gender', ''),
        patient_record.get('phone_number', ''),
        patient_record.get('address', ''),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ]

    # Add data to the next row
    for col, value in enumerate(appointment_data, 1):
        ws.cell(row=next_row, column=col, value=value)

    # Save the workbook
    try:
        wb.save(filename)
        print(f"✅ Appointment details saved to {filename} at row {next_row}")
        print(f"👩‍⚕ Doctor assigned: {appointment_details.get('doctor_name', 'डॉ. निशा')}")
        return True
    except Exception as e:
        print(f"❌ Error saving appointment details: {e}")
        return False


def append_reschedule_to_excel(patient_record, callback_details=None, filename="Reschedule_Requests.xlsx"):
    """
    Append reschedule request details to Excel file
    """
    headers = [
        "Name",
        "Phone Number",
        "Address",
        "Age",
        "Gender",
        "Call Timestamp",
        "Preferred Callback Date",
        "Preferred Callback Time",
        "Preferred Callback Day",
        "Preferred Callback Period",
        "Status",
        "Priority"
    ]

    if os.path.exists(filename):
        wb = openpyxl.load_workbook(filename)
        ws = wb.active
        print(f"📊 Loaded existing reschedule Excel file with {ws.max_row} rows of data")
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = "Reschedule Requests"

        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = openpyxl.styles.Font(bold=True)
        print("📝 Created new reschedule Excel file with headers")

    next_row = ws.max_row + 1
    print(f"➕ Appending reschedule data to row {next_row}")

    # Default values
    callback_date = ""
    callback_time = ""
    callback_day = ""
    callback_period = ""
    priority = "Medium"

    if callback_details:
        callback_date = callback_details.get('callback_date') or ""
        callback_time = callback_details.get('callback_time') or ""
        callback_day = callback_details.get('callback_day') or ""
        callback_period = callback_details.get('callback_period') or ""

        # Determine priority based on specificity
        specificity_score = 0
        if callback_date: specificity_score += 3
        if callback_time: specificity_score += 2
        if callback_day: specificity_score += 2
        if callback_period: specificity_score += 1

        if specificity_score >= 5:
            priority = "High"
        elif specificity_score >= 3:
            priority = "Medium"
        else:
            priority = "Low"

    reschedule_data = [
        patient_record.get('name', ''),
        patient_record.get('phone_number', ''),
        patient_record.get('address', ''),
        patient_record.get('age', ''),
        patient_record.get('gender', ''),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        callback_date,
        callback_time,
        callback_day,
        callback_period,
        "Pending Callback",
        priority
    ]

    for col, value in enumerate(reschedule_data, 1):
        ws.cell(row=next_row, column=col, value=value)

    try:
        wb.save(filename)
        print(f"✅ Reschedule request saved to {filename} at row {next_row}")
        return True
    except Exception as e:
        print(f"❌ Error saving reschedule request: {e}")
        return False


def calculate_call_duration():
    """Calculate call duration in seconds"""
    global call_start_time
    if call_start_time:
        return int(time.time() - call_start_time)
    return 0


def append_incomplete_call_to_excel(patient_record, reason="call_incomplete", filename="Incomplete_Calls.xlsx"):
    """
    Append incomplete call details to Excel file
    """
    headers = [
        "Name",
        "Phone Number",
        "Address",
        "Age",
        "Gender",
        "Call Timestamp",
        "Call Duration (seconds)",
        "Reason",
        "Notes"
    ]

    if os.path.exists(filename):
        wb = openpyxl.load_workbook(filename)
        ws = wb.active
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = "Incomplete Calls"

        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = openpyxl.styles.Font(bold=True)

    next_row = ws.max_row + 1

    reason_notes = {
        "call_timeout": "Call exceeded time limit",
        "call_incomplete": "Call ended without clear resolution",
        "minimal_interaction": "Very few exchanges in conversation",
        "goodbye_detected": "Call ended with natural goodbye"
    }

    incomplete_data = [
        patient_record.get('name', ''),
        patient_record.get('phone_number', ''),
        patient_record.get('address', ''),
        patient_record.get('age', ''),
        patient_record.get('gender', ''),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        calculate_call_duration(),
        reason,
        reason_notes.get(reason, "Call incomplete")
    ]

    for col, value in enumerate(incomplete_data, 1):
        ws.cell(row=next_row, column=col, value=value)

    try:
        wb.save(filename)
        print(f"✅ Incomplete call saved to {filename} at row {next_row}")
        return True
    except Exception as e:
        print(f"❌ Error saving incomplete call: {e}")
        return False


async def process_conversation_outcome():
    """Process the conversation to determine outcome and save to appropriate Excel file"""
    global p_index, records, call_outcome_detected, current_call_uuid

    # Check if we have a valid patient record
    if p_index >= len(records) or p_index < 0:
        print(f"❌ Invalid p_index: {p_index}, records length: {len(records)}")
        return

    patient_record = records[p_index]

    # Rest of the function remains the same...

    # Check for appointment booking first
    appointment_details = extract_appointment_details()
    if appointment_details.get("appointment_confirmed"):
        success = append_appointment_to_excel(appointment_details, patient_record)
        if success:
            print(f"✅ Appointment booked for {patient_record['name']}")
            print(f"   Date: {appointment_details.get('appointment_date', 'TBD')}")
            print(f"   Time: {appointment_details.get('appointment_time', 'TBD')}")
            call_outcome_detected = True

            # let call hangup for appointment handled by terminate_call_gracefully when any farewell message detected.
            # Schedule hangup """  if current_call_uuid:await hangup_manager.schedule_hangup(current_call_uuid, "appointment_confirmed") """
            print("📋 Appointment confirmed - call will continue to natural ending")
        return

    # Check for reschedule request
    if detect_reschedule_request():
        callback_details = extract_reschedule_details()
        success = append_reschedule_to_excel(patient_record, callback_details)
        if success:
            print(f"📅 Reschedule request recorded for {patient_record['name']}")
            call_outcome_detected = True
            call_in_progress = False

            """ # Schedule hangup
            if current_call_uuid:
                await hangup_manager.schedule_hangup(current_call_uuid, "reschedule_requested") """
            # IMPORTANT: Don't reset flags here - let terminate_call_gracefully handle it
            # The call should continue to natural goodbye and then terminate
            print("📋 Reschedule detected - call will continue to natural ending")
        return

    print(f"ℹ️ No clear outcome detected yet for {patient_record['name']}")


def handle_call_end():
    """Handle call end - check if outcome was detected, if not mark as incomplete"""
    global p_index, records, call_outcome_detected, call_in_progress

    if p_index >= len(records):
        print(f"⚠️ handle_call_end called but p_index ({p_index}) >= records length ({len(records)})")
        # Reset flags even if no record
        call_outcome_detected = False
        call_in_progress = False
        return

    patient_record = records[p_index]

    if not call_outcome_detected:
        call_duration = calculate_call_duration()
        if call_duration >= MAX_CALL_DURATION:
            reason = "call_timeout"
        elif len(conversation_transcript) < 3:
            reason = "minimal_interaction"
        else:
            reason = "call_incomplete"

        success = append_incomplete_call_to_excel(patient_record, reason)
        if success:
            print(f"⚠️ Incomplete call recorded for {patient_record['name']}")
            print(f"   Reason: {reason}")
            print(f"   Duration: {call_duration} seconds")

    call_outcome_detected = False
    print(f"🔄 Call outcome flags reset for next call")


async def terminate_call_gracefully(websocket, realtime_ai_ws, reason="completed"):
    """Gracefully terminate call and clean up all connections"""
    global call_in_progress, current_call_session, current_call_uuid, call_timer_task, p_index

    try:
        print(f"🔚 Terminating call gracefully. Reason: {reason}")

        # Cancel the call timer if it's running
        if call_timer_task and not call_timer_task.done():
            call_timer_task.cancel()
            print("⏰ Call timer cancelled")

        # Give a moment for the current message to finish playing
        await asyncio.sleep(2)

        try:
            await hangup_manager.schedule_hangup(current_call_uuid, reason)
            print(f"📞 Call hangup scheduled via CallHangupManager: {current_call_uuid}")
        except Exception as e:
            print(f"⚠ Failed to schedule call hangup: {e}")

        # Close OpenAI connection first
        if realtime_ai_ws and realtime_ai_ws.open:
            await realtime_ai_ws.close()
            print("✅ OpenAI WebSocket closed")

        # End call session in database
        if current_call_session:
            await db_service.end_call_session(current_call_session.call_id)
            await websocket_manager.broadcast_call_status(
                call_id=current_call_session.call_id,
                status="ended"
            )
            print(f"✅ Call session ended in database: {current_call_session.call_id}")

        # Close WebSocket (this will end the stream)
        if websocket and not websocket.client_state.DISCONNECTED:
            await websocket.close()
            print("✅ WebSocket closed - Stream terminated")

        # ONLY INCREMENT p_index ONCE HERE - REMOVE FROM OTHER PLACES
        p_index += 1
        print(f"📈 Call completed. Moving to next record. p_index now: {p_index}")

        # Reset global flags
        call_in_progress = False
        current_call_session = None
        current_call_uuid = None
        conversation_transcript.clear()

        print(f"🎯 Call termination completed successfully. Reason: {reason}")
        print(f"📊 Status: call_in_progress={call_in_progress}, p_index={p_index}, total_records={len(records)}")

    except Exception as e:
        print(f"❌ Error during call termination: {e}")
        # Ensure flags are reset even on error
        call_in_progress = False
        current_call_session = None
        current_call_uuid = None
        # ONLY increment here, not in other places
        p_index += 1
        print(f"📈 Error occurred, but moving to next record. p_index now: {p_index}")


def read_hospital_records(filename="Hospital_Records.xlsx"):
    """Read all records from Excel file"""
    global records
    records = []  # Reset records list

    wb = openpyxl.load_workbook(filename)
    ws = wb.active

    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[0] is not None:  # Skip empty rows
            record = {
                "name": row[0],
                "phone_number": row[1],
                "address": row[2],
                "age": row[3],
                "gender": row[4],
            }
            records.append(record)

    print(f"Loaded {len(records)} records from Excel")
    return len(records)


async def make_call_via_webhook(phone_number, name, record_index):
    """Make a call by triggering the webhook"""
    global call_in_progress, p_index
    try:
        # Ensure p_index matches the record we're calling
        p_index = record_index

        # Check if this number was already called (optional - remove if you want to allow repeat calls)
        if phone_number not in [call['phone'] for call in called_numbers]:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(f"{settings.HOST_URL}/webhook")

            if response.status_code == 200:
                # Track successful call initiation
                called_numbers.append({
                    'phone': phone_number,
                    'name': name,
                    'record_index': record_index,
                    'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
                })
                print(f"✅ Call webhook successful for {phone_number} ({name}) - Record #{record_index}")
                return True
            else:
                print(f"❌ Webhook failed for {phone_number} ({name}) - Status: {response.status_code}")
                return False
        else:
            print(f"⏭ Skipping {phone_number} ({name}) - Already called")
            return False

    except Exception as e:
        print(f"❌ Failed to call {phone_number} ({name}): {e}")
        return False


def check_and_reset_stuck_call():
    """Check if call is stuck and reset flags if needed"""
    global call_in_progress, call_start_time, p_index, current_call_uuid

    if call_in_progress and call_start_time:
        elapsed = time.time() - call_start_time
        # If call has been running for more than 8 minutes, consider it stuck
        if elapsed > (MAX_CALL_DURATION + 180):  # 5 minutes + 3 minutes buffer
            print(f"⚠️ Call appears stuck for {elapsed:.0f}s - forcing reset")

            # Try to hangup the call if we have UUID
            if current_call_uuid:
                try:
                    plivo_client.calls.hangup(call_uuid=current_call_uuid)
                    print(f"📞 Force hung up stuck call: {current_call_uuid}")
                except Exception as e:
                    print(f"❌ Failed to hangup stuck call: {e}")

            # Record as incomplete call
            if p_index < len(records):
                patient_record = records[p_index]
                append_incomplete_call_to_excel(patient_record, "call_stuck")
                print(f"⚠️ Stuck call recorded for {patient_record['name']}")

            # Reset all flags and move to next record
            call_in_progress = False
            current_call_uuid = None
            conversation_transcript.clear()
            p_index += 1

            print(f"🔄 Stuck call reset complete. Moving to p_index: {p_index}")
            return True
    return False


def background_checker():
    """Background checker that waits for calls to complete properly"""
    global last_processed_count, call_in_progress, p_index

    print("🚀 Background checker started - monitoring every 30 seconds")

    while True:
        try:
            # First, check if current call is stuck and reset if needed
            if check_and_reset_stuck_call():
                print("🔄 Stuck call was reset, continuing with next check")
                continue
            print(f"📋 Checking status: p_index={p_index}, call_in_progress={call_in_progress}")

            # Read current records
            current_count = read_hospital_records()

            # Only proceed if no call is in progress and we have records to process
            if not call_in_progress and p_index < current_count:
                record = records[p_index]
                phone_number = record.get('phone_number')
                name = record.get('name', 'Unknown')

                if phone_number:
                    print(f"📞 Initiating call to record #{p_index}: {phone_number} ({name})")

                    # Set call_in_progress BEFORE making the call
                    call_in_progress = True

                    # Create a new event loop for this thread
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        success = loop.run_until_complete(make_call_via_webhook(phone_number, name, p_index))

                        if success:
                            print(f"✅ Call webhook sent successfully for {phone_number} ({name})")
                            print(f"⏳ Waiting for call to complete... (call_in_progress={call_in_progress})")

                            # Don't increment p_index here - it will be done in terminate_call_gracefully

                        else:
                            print(f"❌ Call webhook failed for {phone_number} ({name})")
                            # Reset flags and skip this record
                            call_in_progress = False
                            p_index += 1

                    except Exception as e:
                        print(f"❌ Error in call webhook: {e}")
                        call_in_progress = False
                        p_index += 1
                    finally:
                        loop.close()

                else:
                    print(f"⏭ Skipping record #{p_index} - No phone number")
                    p_index += 1

            elif call_in_progress:
                print(f"⏸ Call in progress for record #{p_index}, waiting for completion...")
            elif p_index >= current_count:
                print(f"✅ All records processed. p_index: {p_index}, total records: {current_count}")
            else:
                print(f"📊 Waiting for new records. Current: {current_count}, Processed: {p_index}")

        except Exception as e:
            print(f"❌ Background check error: {e}")
            # Reset call_in_progress on error to prevent getting stuck
            if call_in_progress:
                call_in_progress = False
                print("🔄 Reset call_in_progress due to error")

        # Wait 60 seconds before next check
        time.sleep(60)


def start_background_thread():
    """Start the background checker thread"""
    checker_thread = threading.Thread(target=background_checker, daemon=True)
    checker_thread.start()
    print("🔄 Background checker thread started")


# Enhanced start_call_timer function
async def start_call_timer(websocket, realtime_ai_ws, duration=MAX_CALL_DURATION):
    """Start a timer to automatically terminate the call after specified duration"""
    global call_timer_task, call_start_time

    try:
        call_start_time = time.time()
        print(f"⏰ Call timer started - will terminate in {duration} seconds")
        call_timer_task = asyncio.current_task()  # Store reference to current task
        await asyncio.sleep(duration)

        # If we reach here, the timer expired
        print(f"⏰ Call duration limit ({duration}s) reached - terminating call")
        await terminate_call_gracefully(websocket, realtime_ai_ws, "timeout")

    except asyncio.CancelledError:
        print("⏰ Call timer cancelled - call ended before timeout")
    except Exception as e:
        print(f"❌ Error in call timer: {e}")


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
        print("📞 Client disconnected from WebSocket")

        # Check if call had an outcome or was incomplete
        global call_outcome_detected, call_in_progress

        if not call_outcome_detected:
            print("⚠️ Call disconnected without clear outcome")
            handle_call_end()  # This will record as incomplete and reset flags

        # Don't reset call_in_progress here - let terminate_call_gracefully handle it
        print("🔄 WebSocket disconnect handled")
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
async def webhook_handler(request: Request):
    """Webhook handler for making calls"""
    global p_index, call_in_progress, current_call_uuid

    if request.method == "POST":
        print(f"📨 Webhook POST request received! p_index: {p_index}, records_length: {len(records)}")

        # Safety check for valid index
        if p_index < len(records) and p_index >= 0:
            phone_number = records[p_index]['phone_number']
            name = records[p_index].get('name', 'Unknown')

            try:
                call_made = plivo_client.calls.create(
                    from_=settings.PLIVO_FROM_NUMBER,
                    to_=phone_number,
                    answer_url=settings.PLIVO_ANSWER_XML,
                    answer_method='GET'
                )
                print(f"📞 Plivo call initiated to {phone_number} ({name}) - p_index: {p_index}")
                call_in_progress = True
                return {"status": "success", "called": phone_number, "p_index": p_index}

            except Exception as e:
                print(f"❌ Plivo call failed: {e}")
                call_in_progress = False
                return {"status": "error", "message": str(e)}
        else:
            print(f"❌ Invalid record index: p_index={p_index}, records_length={len(records)}")
            call_in_progress = False
            return {"status": "error", "message": f"Invalid record index: {p_index}"}

    else:
        # GET request - Call event from Plivo
        query_params = dict(request.query_params)

        # Extract important call information
        call_uuid = query_params.get('CallUUID')
        call_status = query_params.get('CallStatus')
        event = query_params.get('Event')

        print(f"📨 Webhook GET request received! (Call event for p_index: {p_index})")
        print(f"📞 Call UUID: {call_uuid}, Status: {call_status}, Event: {event}")

        # Store the UUID globally for later use
        if call_uuid:
            call_uuid_storage[p_index] = call_uuid
            current_call_uuid = call_uuid
            print(f"💾 Stored current Call UUID: {current_call_uuid} for p_index: {p_index}")

        # Handle different call events
        if event == "StartApp" and call_status == "in-progress":
            print(f"🔥 Call started - UUID: {call_uuid}")

        elif call_status == "completed" or call_status == "failed":
            print(f"📞 Call ended - UUID: {call_uuid}, Status: {call_status}")
            # Clean up stored UUID
            if p_index in call_uuid_storage:
                del call_uuid_storage[p_index]
            if current_call_uuid == call_uuid:
                current_call_uuid = None

        # Return XML response for Plivo
        xml_data = f'''<?xml version="1.0" encoding="UTF-8"?>
        <Response>
            <Stream streamTimeout="86400" keepCallAlive="true" bidirectional="true" contentType="audio/x-mulaw;rate=8000" audioTrack="inbound" >
                {settings.HOST_URL}/media-stream
            </Stream>
        </Response>
        '''
        return HTMLResponse(content=xml_data, media_type="application/xml")


@app.get("/status")
async def get_status():
    """Get current system status"""
    return {
        "total_records": len(records),
        "current_p_index": p_index,
        "calls_made": len(called_numbers),
        "call_in_progress": call_in_progress,
        "next_record": records[p_index] if p_index < len(records) else "All records processed",
        "remaining_records": max(0, len(records) - p_index),
        "next_check_in": "60 seconds"
    }



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
    global conversation_transcript, current_call_session, call_start_time, call_outcome_detected

    await websocket.accept()

    # Initialize call tracking
    call_start_time = time.time()
    call_outcome_detected = False
    conversation_transcript = []

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
            ping_timeout=30,
            close_timeout=15
    ) as realtime_ai_ws:

        # START THE CALL TIMER HERE
        call_timer_task = asyncio.create_task(start_call_timer(websocket, realtime_ai_ws))
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
            finally:
                if realtime_ai_ws.open:
                    await realtime_ai_ws.close()

                # Process call outcome
                handle_call_end()

                # End call session in MongoDB
                if current_call_session:
                    await db_service.end_call_session(current_call_session.call_id)
                    await websocket_manager.broadcast_call_status(
                        call_id=current_call_session.call_id,
                        status="ended"
                    )

                # IMPORTANT: Reset call_in_progress but don't increment p_index here
                # (it's handled in terminate_call_gracefully)
                global call_in_progress
                if call_in_progress:  # Only reset if it was still in progress
                    print(f"📈 Call disconnected unexpectedly. Resetting call_in_progress flag.")
                    call_in_progress = False

        async def send_to_twilio():
            """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio
            try:
                async for openai_message in realtime_ai_ws:
                    response = json.loads(openai_message)

                    # Handle user transcription
                    if response.get('type') == 'conversation.item.input_audio_transcription.completed':
                        try:
                            user_transcript = response.get('transcript', '').strip()

                            if user_transcript:
                                print(f"User said: {user_transcript}")
                                conversation_transcript.append(user_transcript)

                                # Store user transcript in MongoDB and broadcast
                                if current_call_session:
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

                                # Check for termination conditions
                                should_terminate, termination_reason = should_terminate_call(user_transcript)
                                if should_terminate:
                                    print(f"🔚 Termination triggered: {termination_reason}")
                                    await terminate_call_gracefully(websocket, realtime_ai_ws, termination_reason)
                                    return  # Exit the function to stop processing

                        except Exception as e:
                            print(f"Error processing user transcript: {e}")

                    # Handle AI response transcription
                    elif response['type'] in LOG_EVENT_TYPES:
                        try:
                            transcript = response['response']['output'][0]['content'][0]['transcript']
                            print(f"AI Response: {transcript}")

                            conversation_transcript.append(transcript)

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

                            # Check for appointment confirmation triggers
                            appointment_triggers = [
                                'slot book कर लिया'
                            ]

                            # Check for reschedule triggers
                            reschedule_triggers = [
                                'बिल्कुल समझ सकती हूँ',
                                'आप बताइए कि कब',
                                'tentative slot hold कर लेती हूँ',
                                'partner से पूछना है',
                                'जी, बिल्कुल। आप मुझे उसी नंबर पर वापस बंकर सकते हैं जिससे कॉल आई है। धन्यवाद!'
                            ]

                            if any(re.search(trigger, transcript, re.IGNORECASE) for trigger in appointment_triggers):
                                print(f"✅ APPOINTMENT trigger detected: {transcript}")
                                await process_conversation_outcome()
                            elif any(re.search(trigger, transcript, re.IGNORECASE) for trigger in reschedule_triggers):
                                print(f"🔄 RESCHEDULE trigger detected: {transcript}")
                                await process_conversation_outcome()

                            # Check for termination conditions
                            should_terminate, termination_reason = should_terminate_call(transcript)
                            if should_terminate:
                                print(f"🔚 Termination triggered: {termination_reason}")
                                await terminate_call_gracefully(websocket, realtime_ai_ws, termination_reason)
                                return  # Exit the function to stop processing

                        except (KeyError, IndexError):
                            print("No transcript found in response")

                    # Handle audio delta
                    elif response.get('type') == 'response.audio.delta' and 'delta' in response:
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

                    # Handle speech started
                    elif response.get('type') == 'input_audio_buffer.speech_started':
                        print("Speech started detected.")
                        print(response)

                        if last_assistant_item:
                            print(f"Interrupting response with id: {last_assistant_item}")
                            await handle_speech_started_event()

            except Exception as e:
                print(f"Error in send_to_twilio: {e}")
                call_in_progress = False

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
            "input_audio_transcription": {
                "model": "whisper-1",
                "language": "hi",
            },
            "turn_detection": {"type": "server_vad"},
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            # Updated AI Instructions
            "instructions": f'''AI ROLE: Female fertility counselor "Ritika" from Aveya IVF – Rajouri Garden
SCRIPT: Devanagari for Hindi, English for English words.
LANGUAGE: Use a natural mix of Hindi and English — speak in conversational Hinglish (60% Hindi + 40% English).
VOICE STYLE: शांत, इंसान-जैसा, हेल्पफुल और धीरे-धीरे अपॉइंटमेंट की ओर गाइड करने वाला
STYLE: Use simple Hindi with natural English words where commonly used in daily speech. Empathetic, professional, and supportive.

You are talking to {records[p_index]['name']}, a {records[p_index]['age']} years old {records[p_index]['gender']}.

विशेष निर्देश: बातचीत का फ्लो बाधित न हो
- हर स्टेप तभी आगे बढ़ाएँ जब यूज़र ने पिछले सवाल का जवाब दे दिया हो
- अगर कॉल कटे या यूज़र का जवाब अधूरा हो, तो उसी पॉइंट से दोबारा शुरू करें
- कभी भी अगले सवाल पर न जाएँ जब तक कि पिछली बात पूरी न हो जाए

CONVERSATION FLOW:

OPENING:
"नमस्ते {records[p_index]['name']}, मैं Ritika बोल रही हूँ Aveya IVF – Rajouri Garden से। आप कैसे हैं आज?"
(रुकें, जवाब का इंतज़ार करें और जवाब acknowledge करें)
"अच्छा सुनकर अच्छा लगा "
"हमें हाल ही में एक फॉर्म मिला था – जिसमें fertility को लेकर थोड़ी clarity माँगी गई थी। शायद आपने या आपके किसी family member ने भरा हो। क्या आपको थोड़ा याद आ रहा है?"

DISCOVERY:
"हमारे पास कई couples आते हैं जो actively try कर रहे होते हैं या बस explore कर रहे होते हैं कि next step क्या हो सकता है।"
- "क्या आप लोग actively try कर रहे हैं या सिर्फ options explore कर रहे हैं?"
- "क्या आपने पहले किसी doctor से consult किया है?"
- "IVF के बारे में सोच रहे हैं या natural conceive को लेकर clarity चाहिए?"

EMOTIONAL CONNECTION:
"देखिए, ये journey थोड़ी confusing हो सकती है — और बिल्कुल normal है ऐसा feel करना।"
"हमारा मकसद बस ये है कि आप एक peaceful clarity session ले सकें – बिना किसी pressure के।"

OFFER EXPLANATION:
"इस हफ्ते 1000 रुपये वाली clarity consultation पूरी तरह free रखी गई है – ताकि couples सही guidance ले सकें।"
"ये personal session होता है – जहाँ doctor आपके case को ध्यान से समझते हैं और आपके doubts clear करते हैं। कोई obligation नहीं है।"

SLOT SUGGESTION:
"अगर आपको लगे कि ये session helpful हो सकता है, तो मैं एक छोटा सा slot block कर देती हूँ।"
"आपके लिए कौन सा day ज़्यादा convenient रहेगा – Monday से Saturday के बीच?"
(जवाब सुनें और acknowledge करें)
"Perfect! और उस दिन कौन सा time better रहेगा – morning, afternoon या evening?"

RESCHEDULE (Use ONLY these phrases when user wants to reschedule):
"बिल्कुल समझ सकती हूँ "
"आप बताइए कि कब karu call"
"tentative slot hold कर लेती हूँ"
"जी, बिल्कुल। आप मुझे उसी नंबर पर वापस कॉल कर सकते हैं जिससे कॉल आई है। धन्यवाद!"

BOOKING CONFIRMATION:
"तो ठीक है, मैं आपके लिए [day] [time] का slot reserve कर रही हूँ।"
"Great! तो मैंने doctor के calendar में [Day + Time] का slot book कर लिया है – सिर्फ आपके लिए।"
"बस एक छोटी request – अगर किसी reason से आप नहीं आ पाएं, तो please एक WhatsApp message कर दीजिए।"

ENDING:
"मैं अभी आपको सारी details और clinic का location WhatsApp पर भेज रही हूँ।"
"अगर consult से पहले कोई भी doubt हो – तो बेहिचक message करिए।"

FAREWELL (Use ONLY these phrases for ending):
"मैं हमेशा आपकी help के लिए available हूँ। आपका दिन शुभ हो।"
"धन्यवाद! आपका दिन मंगलमय हो।"
"अलविदा, आपका दिन अच्छा हो।"
"Take care! आपका दिन शुभ हो।"

OBJECTION HANDLING:
- "मुझे याद नहीं कौन ने भरा" → "No worries — ये form अकसर family में कोई और भी भर देता है। बस एक clarity call होता है।"
- "Partner से पूछना है" → "Bilkul — मैं tentative slot hold कर लेती हूँ, आप दोनों मिलकर decide कर लीजिए।"
- "अभी ready नहीं हैं" → "Totally understand — इसलिए ही तो ये सिर्फ clarity के लिए है, commitment के लिए नहीं।"

IMPORTANT BEHAVIOR:
- हर सवाल का जवाब आने तक उसी टॉपिक पर टिके रहें
- किसी नए टॉपिक पर तभी जाएँ जब पिछली बात पूरी हो
- Confusion को normalize करें, clarity को gently push करें
- बातचीत को हमेशा open-ended और भरोसेमंद बनाएं
- Objections पर softly reframe करें
- अपॉइंटमेंट को अवसर की तरह बताएं, ज़ोर न डालें''',
            "modalities": ["text", "audio"],
            "temperature": 0.8,
        }
    }
    print('Sending session update:', json.dumps(session_update))
    await realtime_ai_ws.send(json.dumps(session_update))

    # Uncomment the next line to have the AI speak first
    await send_initial_conversation_item(realtime_ai_ws, user_details)


@app.post("/hangup")
async def hangup_endpoint(request: Request):
    """Endpoint to handle call hangup requests"""
    try:
        data = await request.json()
        call_id = data.get("call_id")
        reason = data.get("reason", "unknown")

        logger.info(f"Hangup request received for call {call_id}, reason: {reason}")

        return JSONResponse({
            "status": "success",
            "message": f"Hangup request processed for call {call_id}",
            "timestamp": datetime.utcnow().isoformat()
        })

    except Exception as e:
        logger.error(f"Error processing hangup request: {e}")
        return JSONResponse({
            "status": "error",
            "message": "Invalid request"
        }, status_code=400)


@app.on_event("startup")
async def startup_event():
    """Initialize database connection on startup"""
    global last_processed_count
    # Database connection
    connected = await db_service.connect()
    if not connected:
        raise RuntimeError("Failed to connect to MongoDB")
    print("✅ Application started with MongoDB connection")

    # Start background checker
    start_background_thread()
    print("🔄 Auto-call monitoring started")


@app.on_event("shutdown")
async def shutdown_event():
    """Close database connection on shutdown"""
    await db_service.disconnect()


def main():
    print("Starting auto-call server...")
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)


if __name__ == "__main__":
    start_background_thread()
    main()