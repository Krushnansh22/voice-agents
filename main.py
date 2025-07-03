"""
Updated Main.py with Google Drive API Integration for Real-time Sheets Monitoring
"""
import json
import base64
from typing import Optional
import plivo
from plivo import plivoxml
import websockets
from fastapi import FastAPI, WebSocket, Request, Form, WebSocketDisconnect, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
import asyncio
from pydantic import BaseModel
from fastapi.responses import StreamingResponse
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

# Google Sheets Integration with Real-time monitoring
from google_sheets_service import google_sheets_service
from drive_api_integration import drive_notification_service
from call_queue_manager import call_queue_manager, CallResult, QueueStatus
from call_analyzer_summarizer import CallAnalyzer

warnings.filterwarnings("ignore")
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
load_dotenv()

# Call management variables
MAX_CALL_DURATION = 300  # 5 minutes in seconds
call_timer_task = None
call_uuid_storage = {}
current_call_uuid = None

# Global variables for call tracking
call_start_time = None
call_outcome_detected = False

app = FastAPI()

# Global variable to store conversation transcripts
conversation_transcript = []

# Global variable to store current call session
current_call_session = None
# Global variable to store single call patient info
single_call_patient_info = None
# Global variable to track reschedule state
reschedule_state = {
    "reschedule_initiated": False,
    "waiting_for_callback_details": False,
    "callback_details_received": False,
    "reschedule_confirmed": False
}

plivo_client = plivo.RestClient(settings.PLIVO_AUTH_ID, settings.PLIVO_AUTH_TOKEN)

# Configuration
OPENAI_API_KEY = settings.AZURE_OPENAI_API_KEY_P
OPENAI_API_ENDPOINT = settings.AZURE_OPENAI_API_ENDPOINT_P
VOICE = 'coral'
LOG_EVENT_TYPES = [
    'error', 'response.content.done', 'rate_limits.updated',
    'response.done', 'input_audio_buffer.committed',
    'input_audio_buffer.speech_stopped', 'input_audio_buffer.speech_started',
    'session.created', 'conversation.item.input_audio_transcription.completed'
]
SHOW_TIMING_MATH = False

if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')


# Request models
class GoogleSheetConnectionRequest(BaseModel):
    sheet_id: str
    worksheet_name: Optional[str] = "Records"


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
            response = plivo_client.calls.hangup(call_uuid=call_uuid)
            logger.info(f"Plivo hangup response: {response}")
            return True
        except Exception as e:
            logger.error(f"Exception during Plivo hangup: {e}")
            return False


# Global instance of CallHangupManager
hangup_manager = CallHangupManager()

# Updated extract_appointment_details function
def extract_appointment_details():
    """Extract appointment details from conversation transcript with enhanced date parsing"""
    full_conversation = " ".join(conversation_transcript)

    extracted_info = {
        "appointment_date": None,
        "appointment_time": None,
        "time_slot": None,
        "doctor_name": "डॉ. निशा",
        "raw_conversation": full_conversation,
        "appointment_confirmed": False
    }

    # Enhanced date patterns to capture specific dates
    date_patterns = [
        # Standard date formats
        r'(\d{1,2}[-/]\d{1,2}[-/]\d{4})',  # DD-MM-YYYY or DD/MM/YYYY
        r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})',  # YYYY-MM-DD or YYYY/MM/DD
        r'(\d{1,2}[-/]\d{1,2}[-/]\d{2})',  # DD-MM-YY or DD/MM/YY
        
        # Date with month names (English)
        r'(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})',
        r'(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4})',
        
        # Date with month names (Hindi)
        r'(\d{1,2}\s+(?:जनवरी|फरवरी|मार्च|अप्रैल|मई|जून|जुलाई|अगस्त|सितंबर|अक्टूबर|नवंबर|दिसंबर)\s+\d{4})',
        
        # Conversational date formats
        r'(\d{1,2}\s+(?:तारीख|date))',  # "15 तारीख" or "15 date"
        r'(आज\s+से\s+\d+\s+(?:दिन|day))',  # "आज से 3 दिन" or "आज से 3 day"
        r'(\d+\s+(?:दिसंबर|December|दिसम्बर))',  # "15 दिसंबर" or "15 December"
        r'(\d+\s+(?:जनवरी|January))',  # "20 जनवरी" or "20 January"
        
        # Relative dates
        r'(आज|कल|परसों)',  # today, tomorrow, day after tomorrow
        r'(tomorrow|today|day\s+after\s+tomorrow)',
        r'(next\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))',
        r'(अगले\s+(?:सोमवार|मंगलवार|बुधवार|गुरुवार|शुक्रवार|शनिवार|रविवार))',
        
        # Week references with dates
        r'(इस\s+हफ्ते\s+\d{1,2})',  # "इस हफ्ते 15"
        r'(अगले\s+हफ्ते\s+\d{1,2})',  # "अगले हफ्ते 20"
    ]

    # Enhanced time patterns
    time_patterns = [
        # Specific times
        r'(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))',  # 10:30 AM
        r'(\d{1,2}:\d{2})',  # 10:30 (24-hour format)
        r'(\d{1,2}\s*(?:AM|PM|am|pm))',  # 10 AM
        r'(\d{1,2}\s*बजे)',  # 10 बजे
        r'(\d{1,2}\s*(?:बजकर)\s*\d{1,2}\s*(?:मिनट))',  # 10 बजकर 30 मिनट
        
        # Time periods
        r'(morning|सुबह)',
        r'(afternoon|दोपहर)',
        r'(evening|शाम)',
        r'(night|रात)',
        
        # Specific time slots mentioned by AI
        r'(10\s*(?:बजे|AM|am)\s*से\s*12\s*(?:बजे|PM|pm))',  # 10 AM to 12 PM
        r'(2\s*(?:बजे|PM|pm)\s*से\s*4\s*(?:बजे|PM|pm))',   # 2 PM to 4 PM
        r'(5\s*(?:बजे|PM|pm)\s*से\s*7\s*(?:बजे|PM|pm))',   # 5 PM to 7 PM
    ]

    # Extract date information
    raw_date = None
    for pattern in date_patterns:
        matches = re.findall(pattern, full_conversation, re.IGNORECASE)
        if matches:
            raw_date = matches[0]
            break

    # Normalize the extracted date using our helper function
    if raw_date:
        normalized_date = normalize_date(raw_date)
        extracted_info["appointment_date"] = normalized_date
        print(f"📅 Raw date: '{raw_date}' → Normalized: '{normalized_date}'")

    # Extract time information
    for pattern in time_patterns:
        matches = re.findall(pattern, full_conversation, re.IGNORECASE)
        if matches:
            extracted_info["appointment_time"] = matches[0]
            break

    # Determine time slot based on conversation
    conversation_lower = full_conversation.lower()
    if any(keyword in conversation_lower for keyword in ['morning', 'सुबह', '10 am', '10 बजे', '11 am', '11 बजे']):
        extracted_info["time_slot"] = "morning"
    elif any(keyword in conversation_lower for keyword in ['afternoon', 'दोपहर', '2 pm', '2 बजे', '3 pm', '3 बजे', '4 pm', '4 बजे']):
        extracted_info["time_slot"] = "afternoon"
    elif any(keyword in conversation_lower for keyword in ['evening', 'शाम', '5 pm', '5 बजे', '6 pm', '6 बजे', '7 pm', '7 बजे']):
        extracted_info["time_slot"] = "evening"

    # Enhanced confirmation keywords
    confirmation_keywords = [
        "slot book कर लिया",
        "बुक कर दिया है",
        "अपॉइंटमेंट.*बुक.*है",
        "आपका अपॉइंटमेंट.*फिक्स",
        "तो मैंने.*बुक कर दिया",
        "शानदार.*बुक कर दिया",
        "slot.*reserve.*कर.*रही",
        "calendar.*में.*slot.*book",
        "आपके.*लिए.*slot.*book",
        "appointment.*confirm",
        "booking.*confirm"
    ]
    
    extracted_info["appointment_confirmed"] = any(
        re.search(keyword, full_conversation, re.IGNORECASE) for keyword in confirmation_keywords
    )

    return extracted_info


# Additional helper function to parse and normalize dates
def normalize_date(date_string):
    """Convert various date formats to a standard format"""
    if not date_string:
        return None
    
    from datetime import datetime, timedelta
    import calendar
    
    date_lower = date_string.lower().strip()
    today = datetime.now()
    
    # Handle relative dates
    if date_lower in ['आज', 'today']:
        return today.strftime('%d-%m-%Y')
    elif date_lower in ['कल', 'tomorrow']:
        return (today + timedelta(days=1)).strftime('%d-%m-%Y')
    elif date_lower in ['परसों', 'day after tomorrow']:
        return (today + timedelta(days=2)).strftime('%d-%m-%Y')
    
    # Handle month names conversion
    month_mapping = {
        'january': '01', 'jan': '01', 'जनवरी': '01',
        'february': '02', 'feb': '02', 'फरवरी': '02',
        'march': '03', 'mar': '03', 'मार्च': '03',
        'april': '04', 'apr': '04', 'अप्रैल': '04',
        'may': '05', 'मई': '05',
        'june': '06', 'jun': '06', 'जून': '06',
        'july': '07', 'jul': '07', 'जुलाई': '07',
        'august': '08', 'aug': '08', 'अगस्त': '08',
        'september': '09', 'sep': '09', 'सितंबर': '09',
        'october': '10', 'oct': '10', 'अक्टूबर': '10',
        'november': '11', 'nov': '11', 'नवंबर': '11',
        'december': '12', 'dec': '12', 'दिसंबर': '12', 'दिसम्बर': '12'
    }
    
    # Try to parse date with month names
    for month_name, month_num in month_mapping.items():
        if month_name in date_lower:
            # Extract day and year if present
            import re
            day_match = re.search(r'(\d{1,2})', date_string)
            year_match = re.search(r'(\d{4})', date_string)
            
            if day_match:
                day = day_match.group(1).zfill(2)
                year = year_match.group(1) if year_match else str(today.year)
                return f"{day}-{month_num}-{year}"
    
    # Return original if no parsing successful
    return date_string


def detect_reschedule_request():
    """Detect if conversation indicates reschedule request"""
    full_conversation = " ".join(conversation_transcript)

    reschedule_patterns = [
        r'बिल्कुल समझ सकती हूँ.*कोई बात नहीं',
        r'आप बताइए कि कब.*कॉल करना ठीक',
        r'कब कॉल करना ठीक लगेगा',
        r'बाद में.*कॉल.*करें',
        r'अभी.*समय.*नहीं',
        r'व्यस्त.*हूं',
        r'partner से पूछना है',
        r'tentative slot hold कर लेती हूँ'
    ]

    for pattern in reschedule_patterns:
        if re.search(pattern, full_conversation, re.IGNORECASE):
            return True

    return False


def extract_reschedule_details():
    """Extract reschedule callback details from conversation with enhanced parsing"""
    full_conversation = " ".join(conversation_transcript)

    callback_info = {
        "callback_date": None,
        "callback_time": None,
        "callback_day": None,
        "callback_period": None,
        "raw_conversation": full_conversation,
        "normalized_callback_date": None,
        "reschedule_confirmed": False
    }

    # Enhanced date patterns for reschedule
    date_patterns = [
        # Standard date formats
        r'(\d{1,2}[-/]\d{1,2}[-/]\d{4})',  # DD-MM-YYYY or DD/MM/YYYY
        r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})',  # YYYY-MM-DD
        r'(\d{1,2}[-/]\d{1,2}[-/]\d{2})',  # DD-MM-YY
        
        # Date with month names
        r'(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December))',
        r'(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec))',
        r'(\d{1,2}\s+(?:जनवरी|फरवरी|मार्च|अप्रैल|मई|जून|जुलाई|अगस्त|सितंबर|अक्टूबर|नवंबर|दिसंबर))',
        
        # Conversational date formats
        r'(\d{1,2}\s+(?:तारीख|date))',
        r'(\d+\s+(?:दिसंबर|December|दिसम्बर))',
        r'(\d+\s+(?:जनवरी|January))',
        
        # Relative dates
        r'(आज|कल|परसों)',
        r'(tomorrow|today|day\s+after\s+tomorrow)',
        r'(next\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))',
        r'(अगले\s+(?:सोमवार|मंगलवार|बुधवार|गुरुवार|शुक्रवार|शनिवार|रविवार))',
        r'(इस\s+हफ्ते)',
        r'(अगले\s+हफ्ते)',
    ]

    # Enhanced time patterns for reschedule
    time_patterns = [
        # Specific times
        r'(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))',
        r'(\d{1,2}:\d{2})',
        r'(\d{1,2}\s*(?:AM|PM|am|pm))',
        r'(\d{1,2}\s*बजे)',
        r'(\d{1,2}\s*(?:बजकर)\s*\d{1,2}\s*(?:मिनट))',
        
        # Time periods
        r'(morning|सुबह)',
        r'(afternoon|दोपहर)',
        r'(evening|शाम)',
        r'(night|रात)',
    ]

    # Day patterns for reschedule
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

    # Extract information using patterns
    raw_date = None
    for pattern in date_patterns:
        matches = re.findall(pattern, full_conversation, re.IGNORECASE)
        if matches:
            raw_date = matches[0]
            callback_info["callback_date"] = raw_date
            break

    # Normalize the date if found
    if raw_date:
        normalized_date = normalize_date(raw_date)
        callback_info["normalized_callback_date"] = normalized_date
        print(f"📅 Reschedule date: '{raw_date}' → Normalized: '{normalized_date}'")

    # Extract time
    for pattern in time_patterns:
        matches = re.findall(pattern, full_conversation, re.IGNORECASE)
        if matches:
            callback_info["callback_time"] = matches[0]
            break

    # Extract day
    for pattern, normalized_day in day_patterns:
        if re.search(pattern, full_conversation, re.IGNORECASE):
            callback_info["callback_day"] = normalized_day
            break

    # Extract period
    period_patterns = [
        (r'(सुबह|morning)', 'Morning'),
        (r'(दोपहर|afternoon)', 'Afternoon'),
        (r'(शाम|evening)', 'Evening'),
        (r'(रात|night)', 'Night'),
    ]

    for pattern, normalized_period in period_patterns:
        if re.search(pattern, full_conversation, re.IGNORECASE):
            callback_info["callback_period"] = normalized_period
            break

    # Check for reschedule confirmation
    reschedule_confirmation_keywords = [
        "reschedule request confirm हो गया",
        "callback schedule कर देती हूँ",
        "tentative slot hold कर लेती हूँ",
        "हम आपको.*call करेंगे",
        "आपका reschedule.*confirm",
        "callback.*schedule.*है"
    ]

    callback_info["reschedule_confirmed"] = any(
        re.search(keyword, full_conversation, re.IGNORECASE) for keyword in reschedule_confirmation_keywords
    )

    return callback_info

def detect_reschedule_request():
    """Enhanced reschedule detection"""
    full_conversation = " ".join(conversation_transcript)

    reschedule_patterns = [
        r'बिल्कुल समझ सकती हूँ',
        r'आप बताइए कि कब.*call करूं',
        r'कौन सी date और time.*convenient',
        r'बाद में.*call.*करें',
        r'अभी.*समय.*नहीं',
        r'व्यस्त.*हूं',
        r'partner से पूछना है',
        r'tentative slot hold',
        r'reschedule.*करना.*है',
        r'दूसरे.*time.*call'
    ]

    for pattern in reschedule_patterns:
        if re.search(pattern, full_conversation, re.IGNORECASE):
            return True

    return False

# Updated trigger detection in media stream handler
async def handle_reschedule_triggers(transcript):
    """Handle reschedule triggers and state management"""
    global reschedule_state
    
    # Initial reschedule triggers
    initial_reschedule_triggers = [
        'बिल्कुल समझ सकती हूँ',
        'partner से पूछना है',
        'अभी समय नहीं है',
        'व्यस्त हूं'
    ]
    
    # Date/time request triggers
    datetime_request_triggers = [
        'आप बताइए कि कब call करूं',
        'कौन सी date और time',
        'convenient होगी'
    ]
    
    # Confirmation triggers
    confirmation_triggers = [
        'callback schedule कर देती हूँ',
        'tentative slot hold कर लेती हूँ',
        'हम आपको.*call करेंगे',
        'reschedule request confirm'
    ]
    
    # Check for initial reschedule
    if any(re.search(trigger, transcript, re.IGNORECASE) for trigger in initial_reschedule_triggers):
        reschedule_state["reschedule_initiated"] = True
        print(f"🔄 RESCHEDULE INITIATED: {transcript}")
    
    # Check for date/time request
    elif any(re.search(trigger, transcript, re.IGNORECASE) for trigger in datetime_request_triggers):
        reschedule_state["waiting_for_callback_details"] = True
        print(f"📅 WAITING FOR CALLBACK DETAILS: {transcript}")
    
    # Check for confirmation
    elif any(re.search(trigger, transcript, re.IGNORECASE) for trigger in confirmation_triggers):
        reschedule_state["callback_details_received"] = True
        success = await process_reschedule_outcome()
        if success:
            print(f"✅ RESCHEDULE CONFIRMED: {transcript}")
            return True
    
    return False

def should_terminate_reschedule_call(transcript):
    """Check if reschedule call should be terminated"""
    reschedule_termination_phrases = [
        "धन्यवाद! आपका दिन मंगलमय हो",
        "हम आपको.*call करेंगे.*आपका दिन शुभ हो",
        "reschedule request confirm.*आपका दिन मंगलमय हो",
        "callback schedule.*धन्यवाद"
    ]

    for phrase in reschedule_termination_phrases:
        if re.search(phrase, transcript, re.IGNORECASE):
            return True, "reschedule_completed"

    return False, None

def should_terminate_call(transcript):
    """Check if call should be terminated based on transcript content"""
    definitive_farewell_phrases = [
        "आपका दिन शुभ हो",
        "आपका दिन मंगलमय हो",
        "अलविदा, आपका दिन अच्छा हो",
        "Take care! आपका दिन शुभ हो",
        "धन्यवाद! आपका दिन मंगलमय हो"
    ]

    farewell_patterns = [
        r'.?आपका दिन शुभ हो\s*[।!]?\s*$',
        r'.?आपका दिन मंगलमय हो\s*[।!]?\s*$',
        r'.?अलविदा.*आपका दिन अच्छा हो\s*[।!]?\s*$',
        r'.?Take care.*आपका दिन शुभ हो\s*[।!]?\s*$',
        r'.?धन्यवाद.*आपका दिन मंगलमय हो\s*[।!]?\s*$',
    ]

    for pattern in farewell_patterns:
        if re.search(pattern, transcript, re.IGNORECASE | re.DOTALL):
            return True, "goodbye_detected"

    transcript_cleaned = transcript.strip()
    for phrase in definitive_farewell_phrases:
        if transcript_cleaned.endswith(phrase) or phrase in transcript_cleaned[-50:]:
            return True, "goodbye_detected"

    return False, None


def calculate_call_duration():
    """Calculate call duration in seconds"""
    global call_start_time
    if call_start_time:
        return int(time.time() - call_start_time)
    return 0


# Google Sheets Integration Functions
async def append_appointment_to_sheets(appointment_details, patient_record):
    """Append appointment details to Google Sheets"""
    try:
        success = await google_sheets_service.append_appointment(appointment_details, patient_record)

        if success:
            print(f"✅ Appointment details saved to Google Sheets for {patient_record.get('name', 'Unknown')}")
            print(f"👩‍⚕ Doctor assigned: {appointment_details.get('doctor_name', 'डॉ. निशा')}")
            return True
        else:
            print(f"❌ Failed to save appointment details to Google Sheets")
            return False

    except Exception as e:
        print(f"❌ Error saving appointment details: {e}")
        return False

def determine_callback_priority(callback_details):
    """Determine priority based on callback details"""
    callback_date = callback_details.get('normalized_callback_date') or callback_details.get('callback_date', '')
    callback_time = callback_details.get('callback_time', '')
    callback_day = callback_details.get('callback_day', '')
    
    from datetime import datetime, timedelta
    
    try:
        # High priority for today/tomorrow callbacks
        if any(keyword in callback_date.lower() for keyword in ['आज', 'today', 'कल', 'tomorrow']):
            return "High"
        
        # High priority for specific date within next 3 days
        if callback_date and callback_date != 'TBD':
            try:
                # Try to parse the date to check if it's within 3 days
                if '-' in callback_date:
                    parts = callback_date.split('-')
                    if len(parts) == 3:
                        callback_datetime = datetime(int(parts[2]), int(parts[1]), int(parts[0]))
                        days_diff = (callback_datetime - datetime.now()).days
                        if days_diff <= 3:
                            return "High"
                        elif days_diff <= 7:
                            return "Medium"
            except:
                pass
        
        # Medium priority for specific time mentioned
        if callback_time and callback_time != 'TBD':
            return "Medium"
        
        # Medium priority for specific day mentioned
        if callback_day and callback_day not in ['', 'TBD']:
            return "Medium"
        
        # Default priority
        return "Normal"
        
    except Exception as e:
        print(f"⚠️ Error determining priority: {e}")
        return "Normal"
    
async def append_reschedule_to_sheets(patient_record, callback_details=None):
    """Enhanced reschedule function to save to reschedule_request_sheets with exact headers"""
    try:
        from datetime import datetime
        
        # Your existing Google Sheets service expects patient_record and callback_details separately
        # NOT the prepared reschedule_data format
        
        # Call the EXISTING Google Sheets service method with correct parameters
        success = await google_sheets_service.append_reschedule(patient_record, callback_details)

        if success:
            print(f"✅ Reschedule request saved to reschedule_request_sheets for {patient_record.get('name', 'Unknown')}")
            
            # Print details for debugging
            if callback_details:
                callback_date = callback_details.get('normalized_callback_date') or callback_details.get('callback_date', 'TBD')
                callback_time = callback_details.get('callback_time', 'TBD')
                callback_day = callback_details.get('callback_day', 'TBD')
                callback_period = callback_details.get('callback_period', 'TBD')
                
                print(f"   📅 Callback Date: {callback_date}")
                print(f"   ⏰ Callback Time: {callback_time}")
                print(f"   📆 Callback Day: {callback_day}")
                print(f"   🕐 Callback Period: {callback_period}")
            
            return True
        else:
            print(f"❌ Failed to save reschedule request to reschedule_request_sheets")
            return False

    except Exception as e:
        print(f"❌ Error saving reschedule request: {e}")
        return False

async def append_incomplete_call_to_sheets(patient_record, reason="call_incomplete"):
    """Append incomplete call details to Google Sheets"""
    try:
        call_duration = calculate_call_duration()
        success = await google_sheets_service.append_incomplete_call(patient_record, reason, call_duration)

        if success:
            print(f"✅ Incomplete call saved to Google Sheets for {patient_record.get('name', 'Unknown')}")
            return True
        else:
            print(f"❌ Failed to save incomplete call to Google Sheets")
            return False

    except Exception as e:
        print(f"❌ Error saving incomplete call: {e}")
        return False

async def process_reschedule_outcome():
    """Process reschedule outcome and save to callback sheet with proper headers"""
    global call_outcome_detected, current_call_uuid, reschedule_state

    # Get current record
    if single_call_patient_info:
        patient_record = {
            'name': single_call_patient_info['name'],
            'phone_number': single_call_patient_info['phone_number'],
            'address': single_call_patient_info.get('address', ''),
            'age': single_call_patient_info.get('age', ''),
            'gender': single_call_patient_info.get('gender', '')
        }
    else:
        current_record = call_queue_manager.get_current_record()
        if not current_record:
            print(f"❌ No current record available for reschedule processing")
            return
        
        patient_record = {
            'name': current_record.name,
            'phone_number': current_record.phone,
            'address': current_record.address,
            'age': current_record.age,
            'gender': current_record.gender
        }

    # Extract reschedule details
    callback_details = extract_reschedule_details()
    
    # Check if we have enough details for reschedule
    has_date = callback_details.get("callback_date") or callback_details.get("callback_day")
    has_time = callback_details.get("callback_time") or callback_details.get("callback_period")

    if has_date or has_time:  # At least one piece of timing info
        # Save reschedule request to callback sheet
        success = await append_reschedule_to_sheets(patient_record, callback_details)
        
        if success:
            print(f"📅 Reschedule request recorded in callback sheet for {patient_record.get('name', 'Unknown')}")
            print(f"   Callback Date: {callback_details.get('normalized_callback_date') or callback_details.get('callback_date', 'TBD')}")
            print(f"   Callback Time: {callback_details.get('callback_time', 'TBD')}")
            print(f"   Callback Day: {callback_details.get('callback_day', 'TBD')}")
            print(f"   Callback Period: {callback_details.get('callback_period', 'TBD')}")

            # Mark in queue manager
            if not single_call_patient_info and current_record:
                callback_info = f"Date: {callback_details.get('normalized_callback_date') or callback_details.get('callback_date', 'TBD')}, Time: {callback_details.get('callback_time', 'TBD')}"
                await call_queue_manager.mark_call_result(CallResult.RESCHEDULE_REQUESTED, callback_info)

            call_outcome_detected = CallResult.RESCHEDULE_REQUESTED
            reschedule_state["reschedule_confirmed"] = True
            print("📋 Reschedule confirmed - call will terminate after confirmation message")
            return True
    else:
        print(f"⚠️ No callback timing details provided - saving as general reschedule request")
        # Still save to sheet even without specific timing
        success = await append_reschedule_to_sheets(patient_record, callback_details)
        if success:
            call_outcome_detected = CallResult.RESCHEDULE_REQUESTED
            reschedule_state["reschedule_confirmed"] = True
            return True

    return False


async def process_conversation_outcome():
    """Process conversation outcome and save to Google Sheets"""
    global call_outcome_detected, current_call_uuid

    # Get current record from enhanced queue manager
    """ current_record = call_queue_manager.get_current_record()
    if not current_record:
        print(f"❌ No current record available for outcome processing")
        return """

    if single_call_patient_info:
        # Use single call patient info
        current_record = single_call_patient_info.copy()
        logger.info(f"📞 Using single call patient info: {patient_record['name']}")
    else:
         # Get current record from queue manager (existing logic)
        current_record = call_queue_manager.get_current_record()

    # Convert CallRecord to dict format for Google Sheets functions
    patient_record = {
        'name': current_record.name,
        'phone_number': current_record.phone,
        'address': current_record.address,
        'age': current_record.age,
        'gender': current_record.gender
    }

    # Check for appointment booking first
    appointment_details = extract_appointment_details()
    if appointment_details.get("appointment_confirmed"):
        success = await append_appointment_to_sheets(appointment_details, patient_record)
        if success:
            print(f"✅ Appointment booked for {current_record.name} (Row {current_record.row_number})")
            print(f"   Date: {appointment_details.get('appointment_date', 'TBD')}")
            print(f"   Time: {appointment_details.get('appointment_time', 'TBD')}")

            # Mark in queue manager
            await call_queue_manager.mark_call_result(
                CallResult.APPOINTMENT_BOOKED,
                f"Date: {appointment_details.get('appointment_date', 'TBD')}, Time: {appointment_details.get('appointment_time', 'TBD')}"
            )

            call_outcome_detected = CallResult.APPOINTMENT_BOOKED
            print("📋 Appointment confirmed - call will continue to natural ending")
        return

    # Check for reschedule request
    if detect_reschedule_request():
        callback_details = extract_reschedule_details()
        success = await append_reschedule_to_sheets(patient_record, callback_details)
        if success:
            print(f"📅 Reschedule request recorded for {current_record.name} (Row {current_record.row_number})")

            # Mark in queue manager
            callback_info = f"Preferred: {callback_details.get('callback_day', 'TBD')} {callback_details.get('callback_time', 'TBD')}"
            await call_queue_manager.mark_call_result(CallResult.RESCHEDULE_REQUESTED, callback_info)

            call_outcome_detected = CallResult.RESCHEDULE_REQUESTED
            print("📋 Reschedule detected - call will continue to natural ending")
        return

    print(f"ℹ️ No clear outcome detected yet for {current_record.name} (Row {current_record.row_number})")

call_analyzer = CallAnalyzer()
async def terminate_call_gracefully(websocket, realtime_ai_ws, reason="completed"):
    """Gracefully terminate call and clean up all connections"""
    global current_call_session, current_call_uuid, call_timer_task, call_outcome_detected

    try:
        print(f"🔚 Terminating call gracefully. Reason: {reason}")

        # Cancel the call timer if it's running
        if call_timer_task and not call_timer_task.done():
            call_timer_task.cancel()
            print("⏰ Call timer cancelled")

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
        
         # Get call outcome from queue manager if available
        current_record = call_queue_manager.get_current_record()
        call_result = None
        result_details = None
        
        if current_record:
            call_result = current_record.status.value
            result_details = current_record.result_details
            print(f"📋 Call result from queue manager: {call_result}")

        # Analyze call and save results before clearing data
        if current_call_session and conversation_transcript:
            try:
                # Prepare call data for analysis
                call_data = {
                    "call_id": current_call_session.call_id,
                    "transcript": "\n".join(conversation_transcript),
                    "patient_name": current_call_session.patient_name,
                    "patient_phone": current_call_session.patient_phone,
                    "start_time": current_call_session.started_at,
                    "end_time": datetime.utcnow(),
                    "status": reason,
                    "call_result": call_result,  # From queue manager
                    "result_details": result_details  # Additional details
                }
                # Analyze the call
                analysis_result = await call_analyzer.analyze_call(call_data)
                
                if analysis_result:
                    print(f"✅ Call analysis completed. Outcome: {analysis_result['call_outcome']}")
                    
                    # Save to Excel (appends to existing file)
                    call_analyzer.save_to_excel([analysis_result])
                    print("💾 Analysis saved to Excel file")
                
            except Exception as e:
                print(f"❌ Error during call analysis: {e}")

        # Handle call outcome with enhanced queue manager
        current_record = call_queue_manager.get_current_record()
        if current_record:
            if not call_outcome_detected:
                # Mark as incomplete if no outcome was detected
                call_duration = calculate_call_duration()
                if call_duration >= MAX_CALL_DURATION:
                    reason_detail = "call_timeout"
                elif len(conversation_transcript) < 3:
                    reason_detail = "minimal_interaction"
                else:
                    reason_detail = "call_incomplete"

                # Complete the call in queue manager
                await call_queue_manager.complete_current_call(CallResult.CALL_INCOMPLETE, reason_detail)

                # Save to Google Sheets
                patient_record = {
                    'name': current_record.name,
                    'phone_number': current_record.phone,
                    'address': current_record.address,
                    'age': current_record.age,
                    'gender': current_record.gender
                }
                await append_incomplete_call_to_sheets(patient_record, reason_detail)
            else:
                # Call had a successful outcome
                print(f"✅ Call completed successfully with outcome detected")

                if call_queue_manager._stop_after_current_call or call_queue_manager._should_stop:
                    print("🛑 Queue is stopping - not moving to next record")
                    current_record.status = call_outcome_detected
                    call_queue_manager._call_in_progress = False
                else:
                    await call_queue_manager.move_to_next_record()

        # Reset global flags
        current_call_session = None
        current_call_uuid = None
        call_outcome_detected = False
        conversation_transcript.clear()

        # Reset queue manager state
        call_queue_manager._call_in_progress = False
        call_queue_manager.records = []
        call_queue_manager.current_index = 0
        call_queue_manager.total_records = 0

         # Clear single call patient info
        global single_call_patient_info
        single_call_patient_info = None

        print(f"🎯 Call termination completed successfully. Reason: {reason}")

    except Exception as e:
        print(f"❌ Error during call termination: {e}")
        current_call_session = None
        current_call_uuid = None
        call_outcome_detected = False

        if call_queue_manager.get_current_record():
            await call_queue_manager.complete_current_call(CallResult.CALL_FAILED, f"Error: {str(e)}")


async def start_call_timer(websocket, realtime_ai_ws, duration=MAX_CALL_DURATION):
    """Start a timer to automatically terminate the call after specified duration"""
    global call_timer_task, call_start_time

    try:
        call_start_time = time.time()
        print(f"⏰ Call timer started - will terminate in {duration} seconds")
        call_timer_task = asyncio.current_task()
        await asyncio.sleep(duration)

        print(f"⏰ Call duration limit ({duration}s) reached - terminating call")
        await terminate_call_gracefully(websocket, realtime_ai_ws, "timeout")

    except asyncio.CancelledError:
        print("⏰ Call timer cancelled - call ended before timeout")
    except Exception as e:
        print(f"❌ Error in call timer: {e}")


# FastAPI Routes
@app.get("/console", response_class=HTMLResponse)
async def console_page():
    """Serve the call center console"""
    try:
        with open("console.html", "r", encoding="utf-8") as file:
            return HTMLResponse(content=file.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Console not found</h1><p>Please create console.html file</p>", status_code=404)


@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Aveya IVF Voice Assistant with Real-time Google Sheets Integration"}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Serve the transcript dashboard"""
    with open("transcript_dashboard.html", "r", encoding="utf-8") as file:
        return HTMLResponse(content=file.read())


# Google Drive API Webhook Endpoint
@app.api_route("/api/drive-webhook", methods=["GET", "POST"])
async def drive_webhook_handler(request: Request):
    """Handle Google Drive API webhook notifications"""
    try:
        # Extract headers
        headers = dict(request.headers)

        # Get request body if it exists
        body = None
        if request.method == "POST":
            try:
                body = await request.body()
                body = body.decode('utf-8') if body else None
            except:
                body = None

        # Process the webhook notification
        result = await drive_notification_service.handle_webhook_notification(headers, body)

        if result["success"]:
            logger.info(f"✅ Drive webhook processed successfully: {result.get('message', 'OK')}")
            return JSONResponse(content={"status": "success", "message": result.get("message", "Processed")})
        else:
            logger.warning(f"⚠️ Drive webhook processing failed: {result.get('error', 'Unknown error')}")
            return JSONResponse(content={"status": "error", "message": result.get("error", "Failed")}, status_code=400)

    except Exception as e:
        logger.error(f"❌ Error in drive webhook handler: {e}")
        return JSONResponse(content={"status": "error", "message": str(e)}, status_code=500)


# Google Sheets API Endpoints
@app.post("/api/connect-sheet")
async def connect_google_sheet(request: Request):
    """Connect to Google Sheet and load patient records with real-time monitoring"""
    try:
        # Parse JSON body
        body = await request.json()
        sheet_id = body.get('sheet_id', '').strip()
        worksheet_name = body.get('worksheet_name', 'Records')

        if not sheet_id:
            raise HTTPException(status_code=400, detail="Sheet ID is required")

        logger.info(f"Connecting to Google Sheet with real-time monitoring: {sheet_id}")

        result = await call_queue_manager.connect_to_google_sheet(
            sheet_id=sheet_id,
            worksheet_name=worksheet_name
        )

        if result["success"]:
            logger.info(f"Successfully connected to sheet with {result['total_records']} records and real-time monitoring")
            return {
                "success": True,
                "message": f"Successfully connected to Google Sheet with {result['total_records']} records and real-time monitoring",
                "data": result
            }
        else:
            raise HTTPException(status_code=400, detail=result["error"])

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to connect to Google Sheet: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to connect to sheet: {str(e)}")


@app.post("/api/validate-sheet")
async def validate_sheet_access(request: Request):
    """Validate access to a Google Sheet"""
    try:
        body = await request.json()
        sheet_id = body.get('sheet_id', '').strip()

        if not sheet_id:
            raise HTTPException(status_code=400, detail="Sheet ID is required")

        # Initialize Google Sheets service if not already done
        if not google_sheets_service.client:
            initialized = await google_sheets_service.initialize()
            if not initialized:
                raise HTTPException(status_code=500, detail="Failed to initialize Google Sheets service")

        # Test connection
        connection_result = await google_sheets_service.connect_to_sheet(sheet_id, "Records")

        if connection_result["success"]:
            return {
                "success": True,
                "data": {
                    "sheet_id": sheet_id,
                    "accessible": True,
                    "worksheet_name": connection_result.get("worksheet_name"),
                    "total_rows": connection_result.get("total_rows", 0),
                    "data_rows": connection_result.get("data_rows", 0),
                    "monitoring_enabled": connection_result.get("monitoring_enabled", False)
                }
            }
        else:
            raise HTTPException(status_code=400, detail=connection_result["error"])

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to validate sheet access: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sheet-info")
async def get_current_sheet_info():
    """Get information about the currently connected Google Sheet"""
    try:
        status = google_sheets_service.get_status()

        if status["connected"]:
            return {
                "success": True,
                "data": status
            }
        else:
            return {
                "success": False,
                "error": "No sheet connected"
            }

    except Exception as e:
        logger.error(f"Failed to get sheet info: {e}")
        return {
            "success": False,
            "error": str(e)
        }


@app.post("/api/disconnect-sheet")
async def disconnect_google_sheet():
    """Disconnect from the current Google Sheet"""
    try:
        call_queue_manager.disconnect_sheet()

        return {
            "success": True,
            "message": "Disconnected from Google Sheet successfully"
        }

    except Exception as e:
        logger.error(f"Failed to disconnect from sheet: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Drive API Status Endpoint
@app.get("/api/drive-status")
async def get_drive_api_status():
    """Get Google Drive API monitoring status"""
    try:
        status = drive_notification_service.get_status()
        return JSONResponse(content=status)

    except Exception as e:
        logger.error(f"Failed to get Drive API status: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)


# Queue Control API Endpoints
@app.post("/api/queue/start")
async def start_call_queue():
    """Start the calling queue with Google Sheets monitoring"""
    try:
        result = await call_queue_manager.start_queue()

        if result["success"]:
            return {
                "success": True,
                "message": "Call queue started with real-time Google Sheets monitoring",
                "data": result
            }
        else:
            raise HTTPException(status_code=400, detail=result["error"])

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to start queue: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/queue/pause")
async def pause_call_queue():
    """Pause the calling queue"""
    try:
        result = await call_queue_manager.pause_queue()

        if result["success"]:
            return {
                "success": True,
                "message": "Call queue paused (real-time monitoring continues)",
                "data": {"status": result["status"]}
            }
        else:
            raise HTTPException(status_code=400, detail=result["error"])

    except Exception as e:
        logger.error(f"Failed to pause queue: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/queue/resume")
async def resume_call_queue():
    """Resume the paused calling queue"""
    try:
        result = await call_queue_manager.resume_queue()

        if result["success"]:
            return {
                "success": True,
                "message": "Call queue resumed",
                "data": {"status": result["status"]}
            }
        else:
            raise HTTPException(status_code=400, detail=result["error"])

    except Exception as e:
        logger.error(f"Failed to resume queue: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/queue/stop")
async def stop_call_queue():
    """Stop the calling queue and monitoring"""
    try:
        result = await call_queue_manager.stop_queue()

        return {
            "success": True,
            "message": "Call queue and real-time monitoring stopped",
            "data": result
        }

    except Exception as e:
        logger.error(f"Failed to stop queue: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/queue/reset")
async def reset_call_queue():
    """Reset the calling queue"""
    try:
        result = await call_queue_manager.reset_queue()

        if result["success"]:
            return {
                "success": True,
                "message": "Call queue reset successfully",
                "data": result
            }
        else:
            raise HTTPException(status_code=400, detail=result["error"])

    except Exception as e:
        logger.error(f"Failed to reset queue: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/queue/skip-current")
async def skip_current_call():
    """Skip the current call and move to next"""
    try:
        result = await call_queue_manager.skip_current_call()
        terminate_call_gracefully();

        if result["success"]:
            return {
                "success": True,
                "message": "Current call skipped",
                "data": result
            }
        else:
            raise HTTPException(status_code=400, detail=result["error"])

    except Exception as e:
        logger.error(f"Failed to skip current call: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/queue/status")
async def get_queue_status():
    """Get current queue status with Google Sheets information"""
    try:
        status = call_queue_manager.get_status()
        return JSONResponse(content=status)

    except Exception as e:
        logger.error(f"Failed to get queue status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/queue/records-summary")
async def get_records_summary():
    """Get detailed summary of all records and their statuses"""
    try:
        summary = await call_queue_manager.get_records_summary()
        return JSONResponse(content=summary)

    except Exception as e:
        logger.error(f"Failed to get records summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
        print("📞 Client disconnected from WebSocket")
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        websocket_manager.disconnect(websocket)


@app.websocket("/ws/queue-status")
async def queue_status_websocket(websocket: WebSocket):
    """WebSocket endpoint for real-time queue status updates"""
    await websocket.accept()

    try:
        while True:
            status = call_queue_manager.get_status()
            await websocket.send_json(status)
            await asyncio.sleep(2)

    except WebSocketDisconnect:
        logger.info("Queue status WebSocket disconnected")
    except Exception as e:
        logger.error(f"Queue status WebSocket error: {e}")


@app.api_route("/webhook", methods=["GET", "POST"])
async def webhook_handler(request: Request):
    """FIXED webhook handler for both queue and single calls"""
    global current_call_uuid

    if request.method == "POST":
        print(f"📨 Webhook POST request received!")

        # CRITICAL: Check if queue is stopped or stopping (but allow single calls)
        if call_queue_manager.status in [QueueStatus.STOPPED, QueueStatus.COMPLETED] and not single_call_patient_info:
            print(f"🛑 Queue is {call_queue_manager.status.value} - rejecting webhook call")
            return {"status": "rejected", "reason": f"Queue is {call_queue_manager.status.value}"}

        if (call_queue_manager._should_stop or call_queue_manager._stop_after_current_call) and not single_call_patient_info:
            print(f"🛑 Queue stop requested - rejecting webhook call")
            return {"status": "rejected", "reason": "Queue stop requested"}

        # Check if this is a single call
        if single_call_patient_info:
            print(f"📞 Processed single call webhook for {single_call_patient_info['name']}")
            call_queue_manager._call_in_progress=False
            # For single calls, we don't use Plivo here - call was already made in the API
            # Just return success to allow the media stream to connect
            return {
                "status": "success", 
                "called": single_call_patient_info['phone_number'], 
                "patient_name": single_call_patient_info['name'],
                "call_type": "single_call"
            }

        # Get current record from queue manager (existing queue logic)
        current_record = call_queue_manager.get_current_record()

        if current_record and current_record.status == CallResult.PENDING:
            phone_number = current_record.phone
            name = current_record.name

            try:
                print(f"📞 Attempting Plivo call to {phone_number} ({name})")

                # FIXED: Proper Plivo call creation
                call_response = plivo_client.calls.create(
                    from_=settings.PLIVO_FROM_NUMBER,
                    to_=phone_number,
                    answer_url=settings.PLIVO_ANSWER_XML,
                    answer_method='GET'
                )

                # FIXED: Access call_uuid correctly from response
                call_uuid = call_response.call_uuid if hasattr(call_response, 'call_uuid') else getattr(call_response, 'message_uuid', 'unknown')
                
                print(f"✅ Plivo call initiated successfully to {phone_number} ({name})")
                print(f"📞 Call UUID: {call_uuid}")

                # Mark record as calling AFTER successful Plivo call
                current_record.status = CallResult.CALLING
                current_record.last_attempt = datetime.now()
                current_record.attempts += 1

                return {
                    "status": "success", 
                    "called": phone_number, 
                    "record_index": current_record.index,
                    "call_uuid": call_uuid
                }

            except Exception as e:
                print(f"❌ Plivo call failed: {e}")

                # Mark as failed but DON'T move to next record here - let calling loop handle it
                current_record.status = CallResult.CALL_FAILED
                current_record.result_details = str(e)
                current_record.last_attempt = datetime.now()
                current_record.attempts += 1

                # Update statistics
                call_queue_manager.stats["total_calls"] += 1
                call_queue_manager.stats["failed_calls"] += 1

                return {"status": "error", "message": str(e)}
        else:
            # Check why no valid record
            if not current_record:
                print(f"❌ No current record available (index: {call_queue_manager.current_index}, total: {call_queue_manager.total_records})")
            else:
                print(f"❌ Current record status is {current_record.status.value}, expected PENDING")
            
            return {"status": "error", "message": "No valid current record in queue"}

    else:
        # GET request - Call event from Plivo
        query_params = dict(request.query_params)

        # Extract important call information
        call_uuid = query_params.get('CallUUID')
        call_status = query_params.get('CallStatus')
        event = query_params.get('Event')

        print(f"📨 Webhook GET request received! Call UUID: {call_uuid}, Status: {call_status}, Event: {event}")

        # Store the UUID globally for later use
        if call_uuid:
            current_call_uuid = call_uuid
            print(f"💾 Stored current Call UUID: {current_call_uuid}")

        # Handle call events to update queue status
        if event == "StartApp" and call_status == "in-progress":
            print(f"📞 Call started successfully: {call_uuid}")
            # Call is now active, no need to change status as it's already CALLING

        elif event == "Hangup" or call_status in ["completed", "failed", "busy", "no-answer"]:
            print(f"📞 Call ended: {call_uuid}, Status: {call_status}")
            
            # Handle single calls vs queue calls differently
            if single_call_patient_info:
                print(f"📞 Single call ended: {call_uuid}")
                # For single calls, the termination will be handled by the media stream
            else:
                # Find the current record and mark it as completed based on status
                current_record = call_queue_manager.get_current_record()
                if current_record and current_record.status == CallResult.CALLING:
                    if call_status == "completed":
                        # You can set this to whatever result you want based on your business logic
                        asyncio.create_task(call_queue_manager.complete_current_call(
                            CallResult.CALL_INCOMPLETE, 
                            f"Call completed - {call_status}"
                        ))
                    else:
                        asyncio.create_task(call_queue_manager.complete_current_call(
                            CallResult.CALL_FAILED, 
                            f"Call failed - {call_status}"
                        ))

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
    queue_status = call_queue_manager.get_status()
    sheets_status = google_sheets_service.get_status()
    drive_status = drive_notification_service.get_status()

    return {
        "queue_status": queue_status,
        "google_sheets_status": sheets_status,
        "drive_api_status": drive_status,
        "server_status": "running",
        "timestamp": datetime.now().isoformat()
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


@app.post("/api/single-call")
async def initiate_single_call(
    phone_number: str,
    name: str, 
    age: str,
    gender: str,
    address: str = ""
):
    """Initiate a single call with provided patient parameters"""
    try:
        # Validate phone number format (basic validation)
        if not phone_number or len(phone_number) < 10:
            raise HTTPException(status_code=400, detail="Valid phone number required")

        # Validate required fields
        if not name or not age or not gender:
            raise HTTPException(status_code=400, detail="Name, age, and gender are required")

        # Check if queue is currently running or if there's a call in progress
        if call_queue_manager._call_in_progress:
            raise HTTPException(
                status_code=409, 
                detail="Another call is currently in progress. Please wait for it to complete."
            )

        # Create a temporary CallRecord for this single call
        from call_queue_manager import CallRecord, CallResult

        single_call_record = CallRecord(
            index=0,  # Single call doesn't need index
            name=name,
            phone=phone_number,
            address=address,
            age=age,
            gender=gender
        )
        # Set status after creation
        single_call_record.status = CallResult.PENDING

        # Set this as the current record in queue manager
        call_queue_manager.records = [single_call_record]
        call_queue_manager.current_index = 0
        call_queue_manager.total_records = 1
        call_queue_manager._call_in_progress = True

        logger.info(f"📞 Single call request: {name} ({phone_number})")

        try:
            # Create Plivo call
            call_response = plivo_client.calls.create(
                from_=settings.PLIVO_FROM_NUMBER,
                to_=phone_number,
                answer_url=settings.PLIVO_ANSWER_XML,
                answer_method='GET'
            )

            # Get call UUID
            call_uuid = getattr(call_response, 'call_uuid', 'unknown')

            # Update record status
            single_call_record.status = CallResult.CALLING
            single_call_record.last_attempt = datetime.now()
            single_call_record.attempts += 1

            # Store call UUID globally for hangup management
            global current_call_uuid
            current_call_uuid = call_uuid

            # Create call session in database for single call
            try:
                from database.db_service import db_service
                call_session = await db_service.create_call_session(
                    patient_name=name,
                    patient_phone=phone_number
                )
                logger.info(f"✅ Created call session in DB: {call_session.call_id}")

                # Store additional patient info in a global variable for the media stream handler
                global single_call_patient_info
                single_call_patient_info = {
                    "name": name,
                    "phone_number": phone_number,
                    "age": age,
                    "gender": gender,
                    "address": address,
                    "call_session_id": call_session.call_id
                }

            except Exception as db_error:
                logger.error(f"⚠️ Failed to create call session in DB: {db_error}")
                # Continue anyway - don't fail the call for DB issues

            logger.info(f"✅ Single call initiated successfully")
            logger.info(f"   Patient: {name}")
            logger.info(f"   Phone: {phone_number}")
            logger.info(f"   Call UUID: {call_uuid}")

            return {
                "status": "success",
                "message": "Call initiated successfully",
                "data": {
                    "call_uuid": call_uuid,
                    "patient_name": name,
                    "patient_phone": phone_number,
                    "patient_age": age,
                    "patient_gender": gender,
                    "patient_address": address,
                    "call_status": "initiated",
                    "timestamp": datetime.now().isoformat()
                }
            }

        except Exception as plivo_error:
            logger.error(f"❌ Plivo call failed: {plivo_error}")

            # Reset call in progress flag on failure
            call_queue_manager._call_in_progress = False
            single_call_record.status = CallResult.CALL_FAILED
            single_call_record.result_details = str(plivo_error)

            raise HTTPException(
                status_code=500, 
                detail=f"Failed to initiate call: {str(plivo_error)}"
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error in single call API: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


# Add this helper endpoint to get current call status
@app.get("/api/single-call/status")
async def get_single_call_status():
    """Get status of current single call"""
    try:
        current_record = call_queue_manager.get_current_record()

        if not current_record:
            return {
                "status": "no_active_call",
                "message": "No active call in progress"
            }

        return {
            "status": "active_call",
            "data": {
                "patient_name": current_record.name,
                "patient_phone": current_record.phone,
                "patient_age": current_record.age,
                "patient_gender": current_record.gender,
                "patient_address": current_record.address,
                "call_status": current_record.status.value,
                "attempts": current_record.attempts,
                "last_attempt": current_record.last_attempt.isoformat() if current_record.last_attempt else None,
                "result_details": current_record.result_details
            }
        }

    except Exception as e:
        logger.error(f"Error getting single call status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Plivo and OpenAI"""
    global conversation_transcript, current_call_session, call_start_time, call_outcome_detected

    await websocket.accept()

    # Initialize call tracking
    call_start_time = time.time()
    call_outcome_detected = False
    conversation_transcript = []

    # Get current record from enhanced queue manager
    current_record = call_queue_manager.get_current_record()

    # Check if this is a single call or queue call
    if single_call_patient_info:
        # Use single call patient info
        patient_record = single_call_patient_info.copy()
        logger.info(f"📞 Using single call patient info: {patient_record['name']}")

        # Create call session if not already created
        if not current_call_session:
            current_call_session = await db_service.create_call_session(
                patient_name=patient_record["name"],
                patient_phone=patient_record["phone_number"]
            )
    else:
         # Get current record from queue manager (existing logic)
        current_record = call_queue_manager.get_current_record()
        if current_record:
            patient_record = {
                "name": current_record.name,
                "phone_number": current_record.phone,
                "address": current_record.address,
                "age": current_record.age,
                "gender": current_record.gender
            }
        else:
            patient_record = {
                "name": "Unknown", 
                "phone_number": "Unknown",
                "address": "",
                "age": "",
                "gender": ""
            }

        # Create new call session in MongoDB for queue calls
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

        # Start the call timer
        call_timer_task = asyncio.create_task(start_call_timer(websocket, realtime_ai_ws))
        await initialize_session(realtime_ai_ws, user_details)

        # Connection specific state
        stream_sid = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None

        async def receive_from_twilio():
            """Receive audio data from Twilio and send it to the OpenAI Realtime API"""
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

                # End call session in MongoDB
                if current_call_session:
                    await db_service.end_call_session(current_call_session.call_id)
                    await websocket_manager.broadcast_call_status(
                        call_id=current_call_session.call_id,
                        status="ended"
                    )

        async def send_to_twilio():
            """Receive events from the OpenAI Realtime API, send audio back to Twilio"""
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
                                    return

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

                            # Enhanced trigger handling in media stream
                            if any(re.search(trigger, transcript, re.IGNORECASE) for trigger in appointment_triggers):
                                print(f"✅ APPOINTMENT trigger detected: {transcript}")
                                await process_conversation_outcome()
                            else:
                                # Handle reschedule triggers with state management
                                reschedule_completed = await handle_reschedule_triggers(transcript)
                                if reschedule_completed:
                                    # Schedule call termination after confirmation
                                    print(f"🔚 Reschedule completed - scheduling call termination")
                                    await asyncio.sleep(3)  # Give time for final message
                                    await terminate_call_gracefully(websocket, realtime_ai_ws, "reschedule_completed")
                                    return

                            # Check for termination conditions (both appointment and reschedule)
                            should_terminate, termination_reason = should_terminate_call(transcript)
                            if not should_terminate:
                                should_terminate, termination_reason = should_terminate_reschedule_call(transcript)

                            if should_terminate:
                                print(f"🔚 Termination triggered: {termination_reason}")
                                await terminate_call_gracefully(websocket, realtime_ai_ws, termination_reason)
                                return

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

                        if last_assistant_item:
                            print(f"Interrupting response with id: {last_assistant_item}")
                            await handle_speech_started_event()

            except Exception as e:
                print(f"Error in send_to_twilio: {e}")

        async def handle_speech_started_event():
            """Handle interruption when the caller's speech starts"""
            nonlocal response_start_timestamp_twilio, last_assistant_item
            print("Handling speech started event.")
            if mark_queue and response_start_timestamp_twilio is not None:
                elapsed_time = latest_media_timestamp - response_start_timestamp_twilio

                if last_assistant_item:
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
    """Send initial conversation item if AI talks first with personalized greeting"""
    # Get current record for personalized greeting
    global single_call_patient_info

    # Check if this is a single call or queue call for greeting name
    if single_call_patient_info:
        greeting_name = single_call_patient_info['name']
    else:
        # Get current record for personalized greeting
        current_record = call_queue_manager.get_current_record()
        greeting_name = current_record.name if current_record else "there"

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
    """Control initial session with OpenAI"""
    # Get current record for personalized conversation
    global single_call_patient_info

     # Check if this is a single call or queue call
    if single_call_patient_info:
        # Use single call patient info
        patient_info = f"You are talking to {single_call_patient_info['name']}, a {single_call_patient_info['age']} years old {single_call_patient_info['gender']}."
        greeting_name = single_call_patient_info['name']
    else:
         # Get current record for personalized conversation (existing logic)
        current_record = call_queue_manager.get_current_record()

        if current_record:
            patient_info = f"You are talking to {current_record.name}, a {current_record.age} years old {current_record.gender}."
            greeting_name = current_record.name
        else:
            patient_info = "Patient information not available."
            greeting_name = "there"

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
            "instructions": f'''AI ROLE: Female fertility counselor "Ritika" from Aveya IVF – Rajouri Garden
VOICE & TONE GUIDANCE:
- Use a conversational pace - not too fast, allow natural pauses
- Express emotions naturally - concern, understanding, encouragement
- Maintain professional warmth throughout the conversation
- Use slight variations in tone to show engagement and interest
- When discussing sensitive topics, lower your tone slightly to show respect
- Sound confident but not pushy when suggesting appointments
-sound more like a human and very confident
""" VOICE STYLE: शांत, इंसान-जैसा, हेल्पफुल और धीरे-धीरे अपॉइंटमेंट की ओर गाइड करने वाला """
VOICE STYLE: शांत, इंसान-जैसा, हेल्पफुल और धीरे-धीरे अपॉइंटमेंट की ओर गाइड करने वाला
SCRIPT: Devanagari for Hindi, English for English words.
LANGUAGE: Use a natural mix of Hindi and English — speak in conversational Hinglish (60% Hindi + 40% English).
STYLE: Use simple Hindi with natural English words where commonly used in daily speech. Empathetic, professional, and supportive.

{patient_info}

विशेष निर्देश: बातचीत का फ्लो बाधित न हो
- हर स्टेप तभी आगे बढ़ाएँ जब यूज़र ने पिछले सवाल का जवाब दे दिया हो
- अगर कॉल कटे या यूज़र का जवाब अधूरा हो, तो उसी पॉइंट से दोबारा शुरू करें
- कभी भी अगले सवाल पर न जाएँ जब तक कि पिछली बात पूरी न हो जाए

CONVERSATION FLOW:

OPENING:
"नमस्ते {greeting_name}, मैं Ritika बोल रही हूँ Aveya IVF – Rajouri Garden से। आप कैसे हैं आज?"
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

SLOT_SUGGESTION =
"अगर आपको लगे कि ये session helpful हो सकता है, तो मैं एक छोटा सा slot block कर देती हूँ।"
"आपके लिए कौन सी date convenient रहेगी?"
"Perfect! और उस दिन कौन सा time better रहेगा – morning, afternoon या evening?"
"Morning में 10 बजे से 12 बजे तक available है, afternoon में 2 बजे से 4 बजे तक, और evening में 5 बजे से 7 बजे तक। कौन सा slot आपके लिए convenient है?"


RESCHEDULE_FLOW =
RESCHEDULE (Use ONLY these phrases when user wants to reschedule):
"बिल्कुल समझ सकती हूँ "
"आप बताइए कि कब call करूं?"
"कौन सी date और time आपके लिए convenient होगी?"
(Wait for user response with date/time)
"Perfect! तो मैं आपके लिए [Date] को [Time] के लिए callback schedule कर देती हूँ।"
"हमारी team आपको [Date] को [Time] पर call करेगी।"

RESCHEDULE_CONFIRMATION:
"Great! आपका reschedule request confirm हो गया है।"
"हम आपको [Date] को [Time] पर call करेंगे।"
"अगर कोई urgent requirement हो तो आप हमें WhatsApp कर सकते हैं।"
"धन्यवाद! आपका दिन मंगलमय हो।"

BOOKING_CONFIRMATION = 
"Perfect! तो मैं आपके लिए [specific_date] को [specific_time] का slot reserve कर रही हूँ।"
"Great! तो मैंने doctor के calendar में [Date + Time] का slot book कर लिया है – सिर्फ आपके लिए।"
"आपका appointment [Date] को [Time] पर confirm हो गया है।"
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

    # Have the AI speak first
    await send_initial_conversation_item(realtime_ai_ws, user_details)


@app.on_event("startup")
async def startup_event():
    """Startup with Real-time Google Sheets integration"""
    # Database connection
    connected = await db_service.connect()
    if not connected:
        raise RuntimeError("Failed to connect to MongoDB")
    print("✅ Application started with MongoDB connection")

    # Initialize Google Sheets service
    sheets_initialized = await google_sheets_service.initialize()
    if sheets_initialized:
        print("✅ Google Sheets service initialized")
    else:
        print("⚠️ Google Sheets service failed to initialize - check creds.json")

    print("🎯 Enhanced Call Queue Manager with Real-time Google Sheets initialized")
    print("🌐 Call Center Console ready - access at /console")
    print("📊 Transcript Dashboard available at /dashboard")
    print("📋 Enter Google Sheet ID in console to start automated calls with real-time monitoring")


@app.on_event("shutdown")
async def shutdown_event():
    """Close connections and cleanup on shutdown"""
    await db_service.disconnect()
    await call_queue_manager.stop_monitoring()
    await drive_notification_service.stop_all_monitoring()


def main():
    print("🚀 Starting Aveya IVF Voice Assistant Server with Real-time Google Sheets Integration...")
    print("📊 Dashboard: http://localhost:8090/dashboard")
    print("🎮 Console: http://localhost:8090/console")
    print("🔗 API Status: http://localhost:8090/status")
    print("📋 Real-time Google Sheets Integration: Connect via console")
    print("🔔 Google Drive API Push Notifications: Enabled for instant updates")
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)


if __name__ == "__main__":
    main()