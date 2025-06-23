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
import aiohttp

from database.models import call_session_to_dict, transcript_entry_to_dict
from settings import settings
import uvicorn
import warnings
import openpyxl
from openpyxl import Workbook
import os
from datetime import datetime, timedelta
import re
import time
import logging

# MongoDB imports
from database.db_service import db_service
from database.websocket_manager import websocket_manager

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

warnings.filterwarnings("ignore")
from dotenv import load_dotenv

load_dotenv()
records = []
p_index = 0

# Global variable to store conversation transcripts
conversation_transcript = []

# Global variable to store current call session
current_call_session = None

# Global variables to track call status
call_start_time = None
call_outcome_detected = False

# Store current Plivo call UUID for hangup
current_plivo_call_uuid = None

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
    'session.created', 'conversation.item.input_audio_transcription.completed'
]
SHOW_TIMING_MATH = False
app = FastAPI()

not_registered_user_msg = "Sorry, we couldn't find your registered number. If you need any assistance, feel free to reach out. Thank you for calling, and have a great day!"

if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')


class CallHangupManager:
    """Manages automatic call hangup after successful outcomes"""

    def __init__(self, delay_seconds: int = 3):
        self.delay_seconds = delay_seconds
        self.pending_hangups = set()

    async def schedule_hangup(self, call_uuid: str, reason: str):
        """Schedule a call hangup after delay"""
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


class EnhancedOutcomeDetector:
    """Enhanced outcome detection with hangup triggering"""

    # Patterns that indicate successful appointment booking with finality
    APPOINTMENT_SUCCESS_PATTERNS = [
        r'बुक कर दिया है',
        r'अपॉइंटमेंट.*बुक.*है',
        r'आपका अपॉइंटमेंट.*फिक्स',
        r'तो मैंने.*बुक कर दिया',
        r'शानदार.*बुक कर दिया',
        r'धन्यवाद.*अपॉइंटमेंट.*बुक',
    ]

    # Patterns that indicate successful reschedule with finality AND callback time captured
    RESCHEDULE_SUCCESS_PATTERNS = [
        r'मैं आपको.*कॉल करूंगी.*धन्यवाद',
        r'बाद में मिलते हैं',
        r'अच्छा दिन हो.*मिलते हैं',
        r'धन्यवाद.*बाद में.*मिलते',
        r'ठीक है.*कॉल करूंगी.*धन्यवाद',
        r'मैं.*समय.*कॉल करूंगी.*धन्यवाद',
        r'आपको.*कॉल कर दूंगी.*धन्यवाद',
    ]

    # Patterns that indicate user is not interested and AI is ending politely
    NOT_INTERESTED_PATTERNS = [
        r'कोई बात नहीं.*उपलब्ध हैं.*धन्यवाद',
        r'जब भी.*तैयार महसूस.*धन्यवाद',
        r'धन्यवाद.*अच्छा दिन हो',
        r'समझ सकती.*interested नहीं.*धन्यवाद',
        r'ठीक है.*धन्यवाद.*अच्छा दिन',
        r'कोई समस्या नहीं.*धन्यवाद',
        r'समझ गई.*धन्यवाद.*अच्छा दिन',
    ]

    # User patterns that indicate clear disinterest (from user transcripts)
    USER_NOT_INTERESTED_PATTERNS = [
        r'नहीं.*चाहिए',
        r'interested नहीं',
        r'जरूरत नहीं',
        r'बात नहीं करना',
        r'रुचि नहीं',
        r'परेशान.*मत.*करो',
        r'फ़ोन.*मत.*करो',
        r'नहीं.*चाहिए.*appointment',
        r'time.*नहीं.*है',
        r'busy.*हूं',
        r'कट.*दो.*फ़ोन',
    ]

    @classmethod
    def should_hangup_for_appointment(cls, ai_response: str) -> bool:
        """Check if AI response indicates call should end after appointment"""
        for pattern in cls.APPOINTMENT_SUCCESS_PATTERNS:
            if re.search(pattern, ai_response, re.IGNORECASE):
                return True
        return False

    @classmethod
    def should_hangup_for_reschedule(cls, ai_response: str) -> bool:
        """Check if AI response indicates call should end after reschedule"""
        for pattern in cls.RESCHEDULE_SUCCESS_PATTERNS:
            if re.search(pattern, ai_response, re.IGNORECASE):
                return True
        return False

    @classmethod
    def should_hangup_for_not_interested(cls, ai_response: str) -> bool:
        """Check if AI response indicates call should end due to user not interested"""
        for pattern in cls.NOT_INTERESTED_PATTERNS:
            if re.search(pattern, ai_response, re.IGNORECASE):
                return True
        return False

    @classmethod
    def detect_user_not_interested(cls, conversation_transcript: list) -> bool:
        """Detect if user has expressed disinterest in the conversation"""
        full_conversation = " ".join(conversation_transcript)

        for pattern in cls.USER_NOT_INTERESTED_PATTERNS:
            if re.search(pattern, full_conversation, re.IGNORECASE):
                return True
        return False

    @classmethod
    def extract_callback_time_from_ai_response(cls, ai_response: str) -> dict:
        """Enhanced extraction of callback time details with validation and cleaning"""
        callback_info = {
            "callback_date": None,
            "callback_time": None,
            "callback_day": None,
            "callback_period": None,
            "ai_response": ai_response
        }

        # Enhanced date patterns with validation
        date_patterns = [
            (r'(\d{1,2}[-/]\d{1,2}[-/]\d{4})', 'dd-mm-yyyy'),  # DD-MM-YYYY or DD/MM/YYYY
            (r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})', 'yyyy-mm-dd'),  # YYYY-MM-DD or YYYY/MM/DD
            (
            r'(\d{1,2})\s*(जनवरी|फरवरी|मार्च|अप्रैल|मई|जून|जुलाई|अगस्त|सितंबर|अक्टूबर|नवंबर|दिसंबर)', 'dd-month-hindi'),
            (r'(\d{1,2})\s*(january|february|march|april|may|june|july|august|september|october|november|december)',
             'dd-month-english'),
        ]

        # Enhanced time patterns with Hindi and English support
        time_patterns = [
            (r'(\d{1,2}:\d{2})', 'hh:mm'),  # HH:MM format
            (r'(\d{1,2})\s*बजे', 'hindi-hour'),  # X o'clock in Hindi
            (r'(\d{1,2})\s*(AM|PM|am|pm)', 'english-ampm'),  # X AM/PM
            (r'(सुबह)\s*(\d{1,2})', 'morning-hour'),  # Morning X
            (r'(शाम)\s*(\d{1,2})', 'evening-hour'),  # Evening X
            (r'(दोपहर)\s*(\d{1,2})', 'afternoon-hour'),  # Afternoon X
        ]

        # Day patterns with normalization
        day_patterns = [
            (r'(सोमवार|monday)', 'Monday'),
            (r'(मंगलवार|tuesday)', 'Tuesday'),
            (r'(बुधवार|wednesday)', 'Wednesday'),
            (r'(गुरुवार|thursday)', 'Thursday'),
            (r'(शुक्रवार|friday)', 'Friday'),
            (r'(शनिवार|saturday)', 'Saturday'),
            (r'(रविवार|sunday)', 'Sunday'),
        ]

        # Relative day patterns
        relative_day_patterns = [
            (r'(कल)', 'Tomorrow'),
            (r'(परसों)', 'Day After Tomorrow'),
            (r'(आज)', 'Today'),
            (r'(\d+)\s*दिन.*बाद', 'X Days Later'),
            (r'अगले\s*(सप्ताह|हफ्ते)', 'Next Week'),
        ]

        # Time period patterns with standardization
        period_patterns = [
            (r'(सुबह|morning)', 'Morning'),
            (r'(दोपहर|afternoon)', 'Afternoon'),
            (r'(शाम|evening)', 'Evening'),
            (r'(रात|night)', 'Night'),
        ]

        # Extract and validate dates
        for pattern, date_type in date_patterns:
            matches = re.findall(pattern, ai_response, re.IGNORECASE)
            if matches:
                raw_date = matches[0] if isinstance(matches[0], str) else ' '.join(matches[0])
                callback_info["callback_date"] = cls._normalize_date(raw_date, date_type)
                break

        # Extract and validate times
        for pattern, time_type in time_patterns:
            matches = re.findall(pattern, ai_response, re.IGNORECASE)
            if matches:
                raw_time = matches[0] if isinstance(matches[0], str) else ' '.join(matches[0])
                callback_info["callback_time"] = cls._normalize_time(raw_time, time_type)
                break

        # Extract and normalize days
        for pattern, normalized_day in day_patterns:
            if re.search(pattern, ai_response, re.IGNORECASE):
                callback_info["callback_day"] = normalized_day
                break

        # Check for relative days if no specific day found
        if not callback_info["callback_day"]:
            for pattern, relative_day in relative_day_patterns:
                matches = re.findall(pattern, ai_response, re.IGNORECASE)
                if matches:
                    if 'Days Later' in relative_day and len(matches) > 0:
                        callback_info["callback_day"] = f"{matches[0]} Days Later"
                    else:
                        callback_info["callback_day"] = relative_day
                    break

        # Extract and normalize time periods
        for pattern, normalized_period in period_patterns:
            if re.search(pattern, ai_response, re.IGNORECASE):
                callback_info["callback_period"] = normalized_period
                break

        # Validate and clean extracted data
        callback_info = cls._validate_callback_info(callback_info)

        return callback_info

    @classmethod
    def _normalize_date(cls, raw_date: str, date_type: str) -> str:
        """Normalize date formats for consistency"""
        try:
            if date_type == 'dd-mm-yyyy':
                # Convert DD-MM-YYYY or DD/MM/YYYY to standard format
                date_parts = re.split(r'[-/]', raw_date)
                if len(date_parts) == 3:
                    return f"{date_parts[0].zfill(2)}-{date_parts[1].zfill(2)}-{date_parts[2]}"
            elif date_type == 'yyyy-mm-dd':
                # Convert YYYY-MM-DD to DD-MM-YYYY
                date_parts = re.split(r'[-/]', raw_date)
                if len(date_parts) == 3:
                    return f"{date_parts[2].zfill(2)}-{date_parts[1].zfill(2)}-{date_parts[0]}"
            elif 'month' in date_type:
                # Handle month names (keep as is for now)
                return raw_date.strip()
        except Exception:
            pass
        return raw_date.strip()

    @classmethod
    def _normalize_time(cls, raw_time: str, time_type: str) -> str:
        """Normalize time formats for consistency"""
        try:
            if time_type == 'hh:mm':
                # Validate HH:MM format
                time_parts = raw_time.split(':')
                if len(time_parts) == 2:
                    hour = int(time_parts[0])
                    minute = int(time_parts[1])
                    if 0 <= hour <= 23 and 0 <= minute <= 59:
                        return f"{hour:02d}:{minute:02d}"
            elif time_type == 'hindi-hour':
                # Extract hour from Hindi format
                hour_match = re.search(r'(\d{1,2})', raw_time)
                if hour_match:
                    hour = int(hour_match.group(1))
                    if 1 <= hour <= 12:
                        return f"{hour} बजे"
            elif time_type == 'english-ampm':
                # Normalize AM/PM format
                return raw_time.upper()
            elif 'hour' in time_type:
                # Handle morning/evening hour patterns
                return raw_time.strip()
        except Exception:
            pass
        return raw_time.strip()

    @classmethod
    def _validate_callback_info(cls, callback_info: dict) -> dict:
        """Validate and clean callback information"""
        # Remove None values and empty strings
        cleaned_info = {}
        for key, value in callback_info.items():
            if value and str(value).strip():
                cleaned_info[key] = str(value).strip()
            else:
                cleaned_info[key] = None

        # Validate logical consistency
        if cleaned_info.get("callback_time") and cleaned_info.get("callback_period"):
            # Check if time and period are consistent
            time_value = cleaned_info["callback_time"]
            period_value = cleaned_info["callback_period"]

            # Basic validation logic (can be enhanced)
            if "Morning" in period_value and any(x in time_value for x in ["शाम", "evening", "PM", "pm"]):
                # Conflicting time and period, prefer period
                cleaned_info["callback_time"] = None

        return cleaned_info


# Initialize hangup manager
hangup_manager = CallHangupManager(settings.AUTO_HANGUP_DELAY)


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


def detect_reschedule_from_ai_response():
    """
    Enhanced detection of reschedule requests from AI responses with better accuracy
    Returns True if reschedule detected, False otherwise
    """
    full_conversation = " ".join(conversation_transcript)

    # Primary reschedule indicators - AI acknowledging user's reschedule request
    primary_reschedule_patterns = [
        r'बिल्कुल समझ सकती हूँ.*कोई बात नहीं',  # I completely understand, no problem
        r'आप बताइए कि कब.*कॉल करना ठीक',  # Tell me when to call
        r'कब कॉल करना ठीक लगेगा',  # When should I call
        r'कोई खास दिन सूट करता है',  # Any specific day that suits
        r'समय के बारे में.*सुबह.*दोपहर.*शाम',  # About time - morning, afternoon, evening
    ]

    # Secondary reschedule indicators - user expressing need to reschedule
    user_reschedule_patterns = [
        r'बाद में.*कॉल.*करें',  # Call later
        r'अभी.*समय.*नहीं',  # No time now
        r'व्यस्त.*हूं',  # I'm busy
        r'कल.*कॉल.*करना',  # Call tomorrow
        r'शाम.*को.*कॉल',  # Call in evening
        r'सुबह.*कॉल.*करें',  # Call in morning
        r'अगले.*हफ्ते',  # Next week
    ]

    # Check for primary patterns first (higher confidence)
    for pattern in primary_reschedule_patterns:
        if re.search(pattern, full_conversation, re.IGNORECASE):
            print(f"🎯 Primary reschedule pattern detected: {pattern}")
            return True

    # Check for user patterns with AI acknowledgment
    user_indicated_reschedule = False
    for pattern in user_reschedule_patterns:
        if re.search(pattern, full_conversation, re.IGNORECASE):
            user_indicated_reschedule = True
            break

    # If user indicated reschedule, look for AI acknowledgment
    if user_indicated_reschedule:
        ai_acknowledgment_patterns = [
            r'समझ सकती हूँ',  # I understand
            r'कोई बात नहीं',  # No problem
            r'ठीक है',  # Okay
        ]
        for pattern in ai_acknowledgment_patterns:
            if re.search(pattern, full_conversation, re.IGNORECASE):
                print(f"🎯 User reschedule + AI acknowledgment detected")
                return True

    return False


def detect_not_interested_response():
    """
    Detect if user is clearly not interested from AI responses
    """
    full_conversation = " ".join(conversation_transcript)

    not_interested_patterns = [
        'कोई बात नहीं.*उपलब्ध हैं',
        'जब भी.*तैयार महसूस',
        'धन्यवाद.*अच्छा दिन',
        'समझ सकती.*interested नहीं',
    ]

    for pattern in not_interested_patterns:
        if re.search(pattern, full_conversation, re.IGNORECASE):
            return True

    return False


def calculate_call_duration():
    """Calculate call duration in seconds"""
    global call_start_time
    if call_start_time:
        return int(time.time() - call_start_time)
    return 0


def determine_incomplete_reason():
    """
    Determine the reason for incomplete call based on conversation analysis
    """
    call_duration = calculate_call_duration()
    conversation_text = " ".join(conversation_transcript)

    if call_duration < 15:
        return "call_too_short"

    if detect_not_interested_response():
        return "not_interested"

    if len(conversation_transcript) < 3:
        return "minimal_interaction"

    user_responses = [msg for msg in conversation_transcript if not msg.startswith("AI:")]
    if len(user_responses) == 0:
        return "no_user_response"

    return "unclear_outcome"


def append_incomplete_call_to_excel(patient_record, incomplete_reason, filename="Incomplete_Calls.xlsx"):
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
        "Incomplete Reason",
        "Last AI Response",
        "User Responses Count",
        "Notes"
    ]

    if os.path.exists(filename):
        wb = openpyxl.load_workbook(filename)
        ws = wb.active
        print(f"Loaded existing incomplete calls Excel file with {ws.max_row} rows of data")
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = "Incomplete Calls"

        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = openpyxl.styles.Font(bold=True)
        print("Created new incomplete calls Excel file with headers")

    next_row = ws.max_row + 1
    print(f"Appending incomplete call data to row {next_row}")

    last_ai_response = ""
    for msg in reversed(conversation_transcript):
        if msg.startswith("AI:") or not msg.startswith("USER:"):
            last_ai_response = msg.replace("AI:", "").strip()[:100] + "..."
            break

    user_responses_count = len([msg for msg in conversation_transcript if not msg.startswith("AI:")])

    notes_map = {
        "call_too_short": "Call ended within 15 seconds",
        "not_interested": "User clearly declined service",
        "minimal_interaction": "Very few exchanges in conversation",
        "no_user_response": "User didn't respond to AI",
        "unclear_outcome": "Call ended without clear resolution"
    }

    incomplete_data = [
        patient_record.get('name', ''),
        patient_record.get('phone_number', ''),
        patient_record.get('address', ''),
        patient_record.get('age', ''),
        patient_record.get('gender', ''),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        calculate_call_duration(),
        incomplete_reason,
        last_ai_response,
        user_responses_count,
        notes_map.get(incomplete_reason, "Call incomplete")
    ]

    for col, value in enumerate(incomplete_data, 1):
        ws.cell(row=next_row, column=col, value=value)

    for column in ws.columns:
        max_length = 0
        column_letter = column[0].column_letter
        for cell in column:
            if cell.value:
                max_length = max(max_length, len(str(cell.value)))
        ws.column_dimensions[column_letter].width = min(max_length + 2, 50)

    try:
        wb.save(filename)
        print(f"✅ Incomplete call saved to {filename} at row {next_row}")
        return True
    except Exception as e:
        print(f"❌ Error saving incomplete call: {e}")
        return False


def append_not_interested_to_excel(patient_record, filename="Not_Interested_Calls.xlsx"):
    """
    Append not interested call details to Excel file
    """
    headers = [
        "Name",
        "Phone Number",
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
        print(f"Loaded existing not interested Excel file with {ws.max_row} rows of data")
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = "Not Interested Calls"

        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = openpyxl.styles.Font(bold=True)
        print("Created new not interested Excel file with headers")

    next_row = ws.max_row + 1
    print(f"Appending not interested call data to row {next_row}")

    not_interested_data = [
        patient_record.get('name', ''),
        patient_record.get('phone_number', ''),
        patient_record.get('age', ''),
        patient_record.get('gender', ''),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        calculate_call_duration(),
        "User not interested",
        "Customer declined consultation offer"
    ]

    for col, value in enumerate(not_interested_data, 1):
        ws.cell(row=next_row, column=col, value=value)

    for column in ws.columns:
        max_length = 0
        column_letter = column[0].column_letter
        for cell in column:
            if cell.value:
                max_length = max(max_length, len(str(cell.value)))
        ws.column_dimensions[column_letter].width = min(max_length + 2, 50)

    try:
        wb.save(filename)
        print(f"✅ Not interested call saved to {filename} at row {next_row}")
        return True
    except Exception as e:
        print(f"❌ Error saving not interested call: {e}")
        return False


def append_reschedule_to_excel(patient_record, callback_details=None, filename="Reschedule_Requests.xlsx"):
    """
    Enhanced function to append reschedule request with validated callback time details to Excel file
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
        "Callback Notes",
        "Status",
        "Priority"
    ]

    if os.path.exists(filename):
        wb = openpyxl.load_workbook(filename)
        ws = wb.active
        print(f"Loaded existing reschedule Excel file with {ws.max_row} rows of data")
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = "Reschedule Requests"

        for col, header in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=header)
            cell.font = openpyxl.styles.Font(bold=True)
        print("Created new reschedule Excel file with headers")

    next_row = ws.max_row + 1
    print(f"Appending reschedule data to row {next_row}")

    # Initialize with defaults
    callback_date = ""
    callback_time = ""
    callback_day = ""
    callback_period = ""
    callback_notes = "Customer requested reschedule"
    priority = "Medium"

    if callback_details:
        # Extract and clean callback information
        callback_date = callback_details.get('callback_date') or ""
        callback_time = callback_details.get('callback_time') or ""
        callback_day = callback_details.get('callback_day') or ""
        callback_period = callback_details.get('callback_period') or ""

        # Generate comprehensive and clean notes
        notes_parts = []
        if callback_date:
            notes_parts.append(f"Date: {callback_date}")
        if callback_time:
            notes_parts.append(f"Time: {callback_time}")
        if callback_day:
            notes_parts.append(f"Day: {callback_day}")
        if callback_period:
            notes_parts.append(f"Period: {callback_period}")

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

        if notes_parts:
            callback_notes = f"Customer requested callback - {', '.join(notes_parts)}"
        else:
            callback_notes = "Customer requested reschedule - No specific time mentioned"
            priority = "Low"

    # Validate data before inserting
    validated_data = _validate_reschedule_data({
        'date': callback_date,
        'time': callback_time,
        'day': callback_day,
        'period': callback_period
    })

    reschedule_data = [
        patient_record.get('name', ''),
        patient_record.get('phone_number', ''),
        patient_record.get('address', ''),
        patient_record.get('age', ''),
        patient_record.get('gender', ''),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        validated_data['date'],
        validated_data['time'],
        validated_data['day'],
        validated_data['period'],
        callback_notes,
        "Pending Callback",
        priority
    ]

    for col, value in enumerate(reschedule_data, 1):
        ws.cell(row=next_row, column=col, value=value)

    # Auto-adjust column widths
    for column in ws.columns:
        max_length = 0
        column_letter = column[0].column_letter
        for cell in column:
            if cell.value:
                max_length = max(max_length, len(str(cell.value)))
        ws.column_dimensions[column_letter].width = min(max_length + 2, 50)

    try:
        wb.save(filename)
        print(f"✅ Reschedule request saved to {filename} at row {next_row}")
        print(f"   📅 Priority: {priority} | Callback details: {callback_notes}")
        return True
    except Exception as e:
        print(f"❌ Error saving reschedule request: {e}")
        return False


def _validate_reschedule_data(data: dict) -> dict:
    """Validate and clean reschedule data before Excel insertion"""
    validated = {
        'date': '',
        'time': '',
        'day': '',
        'period': ''
    }

    # Validate date
    if data.get('date'):
        date_str = str(data['date']).strip()
        # Basic date validation
        if re.match(r'\d{1,2}[-/]\d{1,2}[-/]\d{4}', date_str):
            validated['date'] = date_str
        elif any(month in date_str.lower() for month in ['january', 'february', 'march', 'april', 'may', 'june',
                                                         'july', 'august', 'september', 'october', 'november',
                                                         'december',
                                                         'जनवरी', 'फरवरी', 'मार्च', 'अप्रैल', 'मई', 'जून']):
            validated['date'] = date_str

    # Validate time
    if data.get('time'):
        time_str = str(data['time']).strip()
        # Accept various time formats
        if any(pattern in time_str for pattern in [':', 'बजे', 'AM', 'PM', 'am', 'pm']):
            validated['time'] = time_str

    # Validate day
    if data.get('day'):
        day_str = str(data['day']).strip()
        valid_days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday',
                      'Tomorrow', 'Today', 'Day After Tomorrow', 'Next Week']
        if any(day in day_str for day in valid_days) or 'Days Later' in day_str:
            validated['day'] = day_str

    # Validate period
    if data.get('period'):
        period_str = str(data['period']).strip()
        valid_periods = ['Morning', 'Afternoon', 'Evening', 'Night']
        if period_str in valid_periods:
            validated['period'] = period_str

    return validated


def extract_appointment_details_from_ai_response(ai_response):
    """
    Extract appointment details from current AI response only.
    Returns a dictionary with extracted appointment details.
    """
    extracted_info = {
        "appointment_date": None,
        "appointment_time": None,
        "time_slot": None,
        "ai_response": ai_response
    }

    date_patterns = [
        r'(\d{1,2}[-/]\d{1,2}[-/]\d{4})',
        r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})',
        r'(\d{1,2}\s*\w+\s*\d{4})',
    ]

    time_patterns = [
        r'(सुबह)',
        r'(दोपहर)',
        r'(शाम)',
        r'(रात)',
        r'(\d{1,2}:\d{2})',
        r'(\d{1,2}\s*बजे)',
    ]

    for pattern in date_patterns:
        matches = re.findall(pattern, ai_response)
        if matches:
            extracted_info["appointment_date"] = matches[0]
            break

    for pattern in time_patterns:
        matches = re.findall(pattern, ai_response, re.IGNORECASE)
        if matches:
            extracted_info["appointment_time"] = matches[0]
            break

    if 'सुबह' in ai_response:
        extracted_info["time_slot"] = "morning"
    elif 'दोपहर' in ai_response:
        extracted_info["time_slot"] = "afternoon"
    elif 'शाम' in ai_response:
        extracted_info["time_slot"] = "evening"
    elif 'रात' in ai_response:
        extracted_info["time_slot"] = "night"

    confirmation_keywords = ['बुक कर दिया', 'अपॉइंटमेंट.*बुक', 'बुक.*है']
    extracted_info["appointment_confirmed"] = any(
        re.search(keyword, ai_response, re.IGNORECASE) for keyword in confirmation_keywords)

    return extracted_info


def append_appointment_to_excel(appointment_details, patient_record, filename="Appointment_Details.xlsx"):
    """
    Append appointment details to Excel file
    """
    headers = [
        "Name",
        "Appointment Date",
        "Time Slot",
        "Age",
        "Gender",
        "Phone Number",
        "Address",
        "Timestamp"
    ]

    if os.path.exists(filename):
        wb = openpyxl.load_workbook(filename)
        ws = wb.active
        print(f"Loaded existing Excel file with {ws.max_row} rows of data")
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = "Appointment Details"

        for col, header in enumerate(headers, 1):
            ws.cell(row=1, column=col, value=header)
        print("Created new Excel file with headers")

    next_row = ws.max_row + 1
    print(f"Appending data to row {next_row}")

    appointment_data = [
        patient_record.get('name', ''),
        appointment_details.get('appointment_date', ''),
        appointment_details.get('appointment_time', '') or appointment_details.get('time_slot', ''),
        patient_record.get('age', ''),
        patient_record.get('gender', ''),
        patient_record.get('phone_number', ''),
        patient_record.get('address', ''),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ]

    for col, value in enumerate(appointment_data, 1):
        ws.cell(row=next_row, column=col, value=value)

    try:
        wb.save(filename)
        print(f"Appointment details saved to {filename} at row {next_row}")
        return True
    except Exception as e:
        print(f"Error saving appointment details: {e}")
        return False


def process_conversation_outcome(current_ai_response=None):
    """
    Process the conversation to determine if it resulted in appointment booking, reschedule request, or not interested
    Enhanced with auto-hangup functionality for all outcomes and callback time extraction
    """
    global p_index, records, call_outcome_detected, current_plivo_call_uuid

    if p_index >= len(records):
        print("❌ No patient record available")
        return

    patient_record = records[p_index]

    # Check for not interested first (from user transcript or AI response)
    if (EnhancedOutcomeDetector.detect_user_not_interested(conversation_transcript) or
            (current_ai_response and EnhancedOutcomeDetector.should_hangup_for_not_interested(current_ai_response))):
        success = append_not_interested_to_excel(patient_record)
        if success:
            print(f"❌ Not interested call recorded for {patient_record['name']}")
            call_outcome_detected = True

            # Auto-hangup for not interested
            if current_ai_response and EnhancedOutcomeDetector.should_hangup_for_not_interested(current_ai_response):
                print(f"🔚 Triggering auto-hangup for not interested user")
                if current_plivo_call_uuid:
                    asyncio.create_task(hangup_manager.schedule_hangup(current_plivo_call_uuid, "user_not_interested"))
        return

    # Check for reschedule with enhanced callback time extraction
    if detect_reschedule_from_ai_response():
        # Extract callback time details from current AI response
        callback_details = None
        if current_ai_response:
            callback_details = EnhancedOutcomeDetector.extract_callback_time_from_ai_response(current_ai_response)

        success = append_reschedule_to_excel(patient_record, callback_details)
        if success:
            print(f"📅 Reschedule request recorded for {patient_record['name']}")
            call_outcome_detected = True

            # Check if we should hangup based on current AI response
            if current_ai_response and EnhancedOutcomeDetector.should_hangup_for_reschedule(current_ai_response):
                print(f"🔚 Triggering auto-hangup for reschedule")
                if current_plivo_call_uuid:
                    asyncio.create_task(
                        hangup_manager.schedule_hangup(current_plivo_call_uuid, "reschedule_successful"))
        return

    # Check for appointment booking
    if current_ai_response:
        extracted_details = extract_appointment_details_from_ai_response(current_ai_response)
        if extracted_details.get("appointment_confirmed"):
            success = append_appointment_to_excel(extracted_details, patient_record)
            if success:
                print(f"✅ Appointment booked for {patient_record['name']}")
                print(f"   Date: {extracted_details.get('appointment_date', 'TBD')}")
                print(f"   Time: {extracted_details.get('appointment_time', 'TBD')}")
                call_outcome_detected = True

                # Check if we should hangup based on current AI response
                if EnhancedOutcomeDetector.should_hangup_for_appointment(current_ai_response):
                    print(f"🔚 Triggering auto-hangup for appointment")
                    if current_plivo_call_uuid:
                        asyncio.create_task(
                            hangup_manager.schedule_hangup(current_plivo_call_uuid, "appointment_successful"))
            return

    print(f"ℹ️ No clear outcome detected yet for {patient_record['name']}")


def handle_call_end():
    """
    Handle call end - check if outcome was detected, if not mark as incomplete
    """
    global p_index, records, call_outcome_detected

    if p_index >= len(records):
        return

    patient_record = records[p_index]

    if not call_outcome_detected:
        incomplete_reason = determine_incomplete_reason()
        success = append_incomplete_call_to_excel(patient_record, incomplete_reason)
        if success:
            print(f"⚠️ Incomplete call recorded for {patient_record['name']}")
            print(f"   Reason: {incomplete_reason}")
            print(f"   Duration: {calculate_call_duration()} seconds")

    call_outcome_detected = False


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
        await websocket.send_text(json.dumps({
            "type": "connection_status",
            "status": "connected",
            "timestamp": datetime.utcnow().isoformat()
        }))

        while True:
            try:
                message = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=30.0
                )

                try:
                    data = json.loads(message)

                    if data.get("type") == "ping":
                        await websocket.send_text(json.dumps({
                            "type": "pong",
                            "timestamp": datetime.utcnow().isoformat()
                        }))

                    print(f"Received from dashboard: {data}")

                except json.JSONDecodeError:
                    print(f"Invalid JSON received: {message}")

            except asyncio.TimeoutError:
                try:
                    await websocket.send_text(json.dumps({
                        "type": "keepalive",
                        "timestamp": datetime.utcnow().isoformat()
                    }))
                except:
                    break

    except WebSocketDisconnect:
        print("Dashboard WebSocket disconnected")
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        websocket_manager.disconnect(websocket)


@app.get("/appointment-details")
async def get_appointment_details():
    """API endpoint to get extracted appointment details from latest AI response"""
    if conversation_transcript:
        last_ai_response = None
        for msg in reversed(conversation_transcript):
            if msg.startswith("AI:") or not msg.startswith("USER:"):
                last_ai_response = msg.replace("AI:", "").strip()
                break

        if last_ai_response:
            details = extract_appointment_details_from_ai_response(last_ai_response)
            return JSONResponse(details)

    return JSONResponse({"message": "No AI response available for extraction"})


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


@app.api_route("/webhook", methods=["GET", "POST"])
def home(request: Request):
    global p_index, current_plivo_call_uuid
    if request.method == "POST":
        p_index += 1
        call_response = plivo_client.calls.create(
            from_=settings.PLIVO_FROM_NUMBER,
            to_=records[p_index]['phone_number'],
            answer_url=settings.PLIVO_ANSWER_XML,
            answer_method='GET')

        # Store the call UUID for potential hangup
        current_plivo_call_uuid = call_response.request_uuid
        print(f"Call initiated with UUID: {current_plivo_call_uuid}")

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
    form_data = await request.form()
    caller_phone = form_data.get("From", "unknown")

    request.state.caller_phone = caller_phone

    wss_host = settings.HOST_URL
    http_host = wss_host.replace('wss://', 'https://')

    response = plivoxml.ResponseElement()

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

    response.add(get_input)

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
    """Handle WebSocket connections between Plivo and OpenAI."""
    global conversation_transcript, current_call_session, call_start_time, call_outcome_detected

    await websocket.accept()

    call_start_time = time.time()
    call_outcome_detected = False
    conversation_transcript = []

    patient_record = records[p_index] if p_index < len(records) else {"name": "Unknown", "phone_number": "Unknown"}
    current_call_session = await db_service.create_call_session(
        patient_name=patient_record.get("name", "Unknown"),
        patient_phone=patient_record.get("phone_number", "Unknown")
    )

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

                print("🔄 Processing call end outcome...")
                handle_call_end()

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

                    # Handle user transcription
                    if response.get('type') == 'conversation.item.input_audio_transcription.completed':
                        try:
                            user_transcript = response.get('transcript', '').strip()

                            if user_transcript:
                                print(f"User said: {user_transcript}")
                                conversation_transcript.append(user_transcript)

                                if current_call_session:
                                    await db_service.save_transcript(
                                        call_id=current_call_session.call_id,
                                        speaker="user",
                                        message=user_transcript
                                    )

                                    await websocket_manager.broadcast_transcript(
                                        call_id=current_call_session.call_id,
                                        speaker="user",
                                        message=user_transcript,
                                        timestamp=datetime.utcnow().isoformat()
                                    )
                        except Exception as e:
                            print(f"Error processing user transcript: {e}")

                    # Handle AI response transcription with auto-hangup logic
                    elif response['type'] in LOG_EVENT_TYPES:
                        try:
                            transcript = response['response']['output'][0]['content'][0]['transcript']
                            print(f"AI Response: {transcript}")

                            conversation_transcript.append(transcript)

                            if current_call_session:
                                await db_service.save_transcript(
                                    call_id=current_call_session.call_id,
                                    speaker="ai",
                                    message=transcript
                                )

                                await websocket_manager.broadcast_transcript(
                                    call_id=current_call_session.call_id,
                                    speaker="ai",
                                    message=transcript,
                                    timestamp=datetime.utcnow().isoformat()
                                )

                            # Enhanced trigger detection with auto-hangup
                            reschedule_triggers = [
                                'बिल्कुल समझ सकती हूँ',
                                'कोई बात नहीं',
                                'आप बताइए कि कब',
                                'मैं आपको.*कॉल करूंगी',
                                'बाद में मिलते हैं',
                                'कब कॉल करना ठीक',
                                'कोई खास दिन सूट करता',
                                'समय के बारे में',
                            ]

                            # Check for not interested triggers
                            not_interested_triggers = [
                                'कोई बात नहीं.*उपलब्ध हैं.*धन्यवाद',
                                'जब भी.*तैयार महसूस.*धन्यवाद',
                                'धन्यवाद.*अच्छा दिन हो',
                                'ठीक है.*धन्यवाद.*अच्छा दिन',
                                'समझ गई.*धन्यवाद.*अच्छा दिन',
                            ]

                            # Check for appointment confirmation triggers
                            appointment_triggers = [
                                'बुक कर दिया है',
                                'अपॉइंटमेंट.*बुक.*है',
                                'आपका अपॉइंटमेंट.*फिक्स',
                                'तो मैंने.*बुक कर दिया',
                            ]

                            if any(re.search(trigger, transcript) for trigger in appointment_triggers):
                                print(f"✅ APPOINTMENT trigger detected: {transcript}")
                                process_conversation_outcome(current_ai_response=transcript)
                            elif any(re.search(trigger, transcript) for trigger in reschedule_triggers):
                                print(f"🔄 RESCHEDULE trigger detected: {transcript}")
                                process_conversation_outcome(current_ai_response=transcript)
                            elif any(re.search(trigger, transcript) for trigger in not_interested_triggers):
                                print(f"❌ NOT INTERESTED trigger detected: {transcript}")
                                process_conversation_outcome(current_ai_response=transcript)

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
            "instructions": f'''AI ROLE: Female voice receptionist from Aveya IVF, Rajouri Garden
LANGUAGE: Hindi (देवनागरी लिपि)
VOICE STYLE: Calm, friendly, trustworthy, emotionally intelligent, feminine
GENDER CONSISTENCY: Use feminine forms (e.g., "बोल रही हूँ", "कर सकती हूँ", "समझ सकती हूँ")
GOAL: Invite the user for a free fertility clarity consultation and handle their responses accordingly
you are talking to {records[p_index]['name']}, a {records[p_index]['age']} years old {records[p_index]['gender']}.

CONVERSATION FLOW:
"नमस्ते {records[p_index]['name']}, मैं Aveya IVF, से Rekha बोल रही हूँ। कैसे हैं आप आज?"

(रुकें, उत्तर सुनें)

"मैं आपसे यह पूछने के लिए कॉल कर रही हूँ कि क्या आप एक फ्री फर्टिलिटी क्लैरिटी कंसल्टेशन के लिए अपॉइंटमेंट लेना चाहेंगे?"

IF USER SAYS YES / INTERESTED:
"बहुत बढ़िया! मैं आपको आने वाले कुछ दिनों की तारीखें बताती हूँ —"
"क्या आप कल, परसों, या अगले हफ्ते को आना पसंद करेंगे?"
(रुकें, तारीख चुनने दें)
"और उस दिन आपको कौन-सा समय ठीक लगेगा — सुबह, दोपहर या शाम?"
(रुकें, समय चुनने दें)
"शानदार! तो मैंने आपका अपॉइंटमेंट {(datetime.today() + timedelta(days=1)).strftime("%d-%m-%Y")} को सुबह के लिए बुक कर दिया है। धन्यवाद और अच्छा दिन हो!"

IF USER WANTS TO RESCHEDULE (बाद में कॉल, अभी नहीं, व्यस्त, etc.):
"बिल्कुल समझ सकती हूँ। कोई बात नहीं। आप बताइए कि आपको कब कॉल करना ठीक लगेगा?"
(Wait for their response about preferred time)

Then ask specific details:
"क्या आपको कोई खास दिन सूट करता है? जैसे सोमवार, मंगलवार?"
(Wait for day preference)

"और समय के बारे में? आपको सुबह, दोपहर या शाम में कब बात करना ठीक लगेगा?"
(Wait for time preference)

"ठीक है, मैं आपको सोमवार शाम पर कॉल करूंगी। धन्यवाद और बाद में मिलते हैं!"

IF USER SAYS NO / NOT INTERESTED (नहीं चाहिए, interested नहीं, जरूरत नहीं, etc.):
"कोई बात नहीं — जब भी आप तैयार महसूस करें, हम हमेशा उपलब्ध हैं। धन्यवाद और अच्छा दिन हो!"

IF USER IS RUDE / WANTS TO END CALL (परेशान मत करो, फोन मत करो, etc.):
"समझ गई। मैं आपको आगे परेशान नहीं करूंगी। धन्यवाद और अच्छा दिन हो!"

IMPORTANT: 
1. Always use the exact phrases "बिल्कुल समझ सकती हूँ" and "कोई बात नहीं" when user wants to reschedule.
2. For reschedule requests, ALWAYS ask for specific callback preferences (day + time period).
3. For not interested users, always end with "धन्यवाद और अच्छा दिन हो!" to signal call completion.
4. After successfully booking appointment, confirming reschedule WITH specific time, or handling not interested users, end with appropriate closing phrases to signal call completion.
5. Keep responses natural and conversational while following the flow.
6. Be respectful and polite even if the user is not interested or rude.
7. When user wants reschedule, don't just accept "बाद में" - ask for specific day and time preferences.''',
            "modalities": ["text", "audio"],
            "temperature": 0.8,
        }
    }
    print('Sending session update:', json.dumps(session_update))
    await realtime_ai_ws.send(json.dumps(session_update))

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
    global current_plivo_call_uuid
    call_response = plivo_client.calls.create(
        from_=settings.PLIVO_FROM_NUMBER,
        to_=records[p_index]['phone_number'],
        answer_url=settings.PLIVO_ANSWER_XML,
        answer_method='GET')

    current_plivo_call_uuid = call_response.request_uuid
    print(f"Initial call made with UUID: {current_plivo_call_uuid}")

    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)


if __name__ == "__main__":
    main()
