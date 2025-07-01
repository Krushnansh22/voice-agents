"""
Updated Main.py with Streamlined Google Sheets Integration
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

# Google Sheets Integration
from google_sheets_service import google_sheets_service
from call_queue_manager import call_queue_manager, CallResult, QueueStatus

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

plivo_client = plivo.RestClient(settings.PLIVO_AUTH_ID, settings.PLIVO_AUTH_TOKEN)

# Configuration
OPENAI_API_KEY = settings.AZURE_OPENAI_API_KEY_P
OPENAI_API_ENDPOINT = settings.AZURE_OPENAI_API_ENDPOINT_P
VOICE = 'sage'
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


def extract_appointment_details():
    """Extract appointment details from conversation transcript"""
    full_conversation = " ".join(conversation_transcript)

    extracted_info = {
        "appointment_date": None,
        "appointment_time": None,
        "time_slot": None,
        "doctor_name": "डॉ. निशा",
        "raw_conversation": full_conversation,
        "appointment_confirmed": False
    }

    # Date patterns
    date_patterns = [
        r'(\d{1,2}[-/]\d{1,2}[-/]\d{4})',
        r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})',
        r'(\d{1,2}\s*\w+\s*\d{4})',
        r'(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)',
        r'(सोमवार|मंगलवार|बुधवार|गुरुवार|शुक्रवार|शनिवार|रविवार)',
        r'(आज|कल|परसों)',
        r'(tomorrow|today|day after tomorrow)',
    ]

    # Time patterns
    time_patterns = [
        r'(morning|सुबह)',
        r'(afternoon|दोपहर)',
        r'(evening|शाम)',
        r'(\d{1,2}:\d{2})',
        r'(\d{1,2}\s*बजे)',
        r'(\d{1,2}\s*AM|\d{1,2}\s*PM)',
    ]

    # Extract information
    for pattern in date_patterns:
        matches = re.findall(pattern, full_conversation, re.IGNORECASE)
        if matches:
            extracted_info["appointment_date"] = matches[0]
            break

    for pattern in time_patterns:
        matches = re.findall(pattern, full_conversation, re.IGNORECASE)
        if matches:
            extracted_info["appointment_time"] = matches[0]
            break

    # Determine time slot
    conversation_lower = full_conversation.lower()
    if 'morning' in conversation_lower or 'सुबह' in conversation_lower:
        extracted_info["time_slot"] = "morning"
    elif 'afternoon' in conversation_lower or 'दोपहर' in conversation_lower:
        extracted_info["time_slot"] = "afternoon"
    elif 'evening' in conversation_lower or 'शाम' in conversation_lower:
        extracted_info["time_slot"] = "evening"

    # Check for confirmation
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
    """Extract reschedule callback details from conversation"""
    full_conversation = " ".join(conversation_transcript)

    callback_info = {
        "callback_date": None,
        "callback_time": None,
        "callback_day": None,
        "callback_period": None,
        "raw_conversation": full_conversation
    }

    # Date patterns
    date_patterns = [
        (r'(\d{1,2}[-/]\d{1,2}[-/]\d{4})', 'dd-mm-yyyy'),
        (r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})', 'yyyy-mm-dd'),
    ]

    # Time patterns
    time_patterns = [
        (r'(\d{1,2}:\d{2})', 'hh:mm'),
        (r'(\d{1,2})\s*बजे', 'hindi-hour'),
        (r'(\d{1,2})\s*(AM|PM|am|pm)', 'english-ampm'),
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
    for pattern, _ in date_patterns:
        matches = re.findall(pattern, full_conversation, re.IGNORECASE)
        if matches:
            callback_info["callback_date"] = matches[0] if isinstance(matches[0], str) else ' '.join(matches[0])
            break

    for pattern, _ in time_patterns:
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


async def append_reschedule_to_sheets(patient_record, callback_details=None):
    """Append reschedule request details to Google Sheets"""
    try:
        success = await google_sheets_service.append_reschedule(patient_record, callback_details)

        if success:
            print(f"✅ Reschedule request saved to Google Sheets for {patient_record.get('name', 'Unknown')}")
            return True
        else:
            print(f"❌ Failed to save reschedule request to Google Sheets")
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


async def process_conversation_outcome():
    """Process conversation outcome and save to Google Sheets"""
    global call_outcome_detected, current_call_uuid

    # Get current record from enhanced queue manager
    current_record = call_queue_manager.get_current_record()
    if not current_record:
        print(f"❌ No current record available for outcome processing")
        return

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
    return {"message": "Aveya IVF Voice Assistant with Google Sheets Integration"}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Serve the transcript dashboard"""
    with open("transcript_dashboard.html", "r", encoding="utf-8") as file:
        return HTMLResponse(content=file.read())


# Google Sheets API Endpoints
@app.post("/api/connect-sheet")
async def connect_google_sheet(request: Request):
    """Connect to Google Sheet and load patient records"""
    try:
        # Parse JSON body
        body = await request.json()
        sheet_id = body.get('sheet_id', '').strip()
        worksheet_name = body.get('worksheet_name', 'Records')

        if not sheet_id:
            raise HTTPException(status_code=400, detail="Sheet ID is required")

        logger.info(f"Connecting to Google Sheet: {sheet_id}")

        result = await call_queue_manager.connect_to_google_sheet(
            sheet_id=sheet_id,
            worksheet_name=worksheet_name
        )

        if result["success"]:
            logger.info(f"Successfully connected to sheet with {result['total_records']} records")
            return {
                "success": True,
                "message": f"Successfully connected to Google Sheet with {result['total_records']} records",
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
                    "data_rows": connection_result.get("data_rows", 0)
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


# Queue Control API Endpoints
@app.post("/api/queue/start")
async def start_call_queue():
    """Start the calling queue with Google Sheets monitoring"""
    try:
        result = await call_queue_manager.start_queue()

        if result["success"]:
            return {
                "success": True,
                "message": "Call queue started with Google Sheets monitoring",
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
                "message": "Call queue paused (monitoring continues)",
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
            "message": "Call queue and monitoring stopped",
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
    """Webhook handler for Plivo integration"""
    global current_call_uuid

    if request.method == "POST":
        print(f"📨 Webhook POST request received!")

        # Check if queue is stopped
        if call_queue_manager.status in [QueueStatus.STOPPED, QueueStatus.COMPLETED]:
            print(f"🛑 Queue is {call_queue_manager.status.value} - rejecting webhook call")
            return {"status": "rejected", "reason": f"Queue is {call_queue_manager.status.value}"}

        if call_queue_manager._should_stop or call_queue_manager._stop_after_current_call:
            print(f"🛑 Queue stop requested - rejecting webhook call")
            return {"status": "rejected", "reason": "Queue stop requested"}

        # Get current record from enhanced queue manager
        current_record = call_queue_manager.get_current_record()

        if current_record and current_record.status == CallResult.PENDING:
            phone_number = current_record.phone
            name = current_record.name
            row_number = current_record.row_number

            try:
                print(f"📞 Attempting Plivo call to {phone_number} ({name}) from Google Sheets row {row_number}")

                call_response = plivo_client.calls.create(
                    from_=settings.PLIVO_FROM_NUMBER,
                    to_=phone_number,
                    answer_url=settings.PLIVO_ANSWER_XML,
                    answer_method='GET'
                )

                call_uuid = call_response.call_uuid if hasattr(call_response, 'call_uuid') else getattr(call_response,
                                                                                                        'message_uuid',
                                                                                                        'unknown')

                print(f"✅ Plivo call initiated successfully to {phone_number} ({name}) from row {row_number}")
                print(f"📞 Call UUID: {call_uuid}")

                # Mark record as calling
                current_record.status = CallResult.CALLING
                current_record.last_attempt = datetime.now()
                current_record.attempts += 1

                return {
                    "status": "success",
                    "called": phone_number,
                    "record_index": current_record.index,
                    "call_uuid": call_uuid,
                    "google_sheet_row": row_number
                }

            except Exception as e:
                print(f"❌ Plivo call failed: {e}")

                current_record.status = CallResult.CALL_FAILED
                current_record.result_details = str(e)
                current_record.last_attempt = datetime.now()
                current_record.attempts += 1

                call_queue_manager.stats["total_calls"] += 1
                call_queue_manager.stats["failed_calls"] += 1

                return {"status": "error", "message": str(e)}
        else:
            if not current_record:
                print(f"❌ No current record available")
            else:
                print(f"❌ Current record status is {current_record.status.value}, expected PENDING")

            return {"status": "error", "message": "No valid current record in queue"}

    else:
        # GET request - Call event from Plivo
        query_params = dict(request.query_params)

        call_uuid = query_params.get('CallUUID')
        call_status = query_params.get('CallStatus')
        event = query_params.get('Event')

        print(f"📨 Webhook GET request received! Call UUID: {call_uuid}, Status: {call_status}, Event: {event}")

        if call_uuid:
            current_call_uuid = call_uuid
            print(f"💾 Stored current Call UUID: {current_call_uuid}")

        # Handle call events
        if event == "StartApp" and call_status == "in-progress":
            print(f"📞 Call started successfully: {call_uuid}")

        elif event == "Hangup" or call_status in ["completed", "failed", "busy", "no-answer"]:
            print(f"📞 Call ended: {call_uuid}, Status: {call_status}")

            current_record = call_queue_manager.get_current_record()
            if current_record and current_record.status == CallResult.CALLING:
                if call_status == "completed":
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
    return {
        "queue_status": queue_status,
        "google_sheets_status": sheets_status,
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

    if current_record:
        patient_record = {
            "name": current_record.name,
            "phone_number": current_record.phone
        }
        print(f"📞 Starting call stream for {current_record.name} (Row {current_record.row_number})")
    else:
        patient_record = {"name": "Unknown", "phone_number": "Unknown"}

    # Create new call session in MongoDB
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

                            # Check for reschedule triggers
                            reschedule_triggers = [
                                'बिल्कुल समझ सकती हूँ',
                                'आप बताइए कि कब',
                                'tentative slot hold कर लेती हूँ',
                                'partner से पूछना है'
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

    # Have the AI speak first
    await send_initial_conversation_item(realtime_ai_ws, user_details)


@app.on_event("startup")
async def startup_event():
    """Startup with Google Sheets integration"""
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

    print("🎯 Enhanced Call Queue Manager with Google Sheets initialized")
    print("🌐 Call Center Console ready - access at /console")
    print("📊 Transcript Dashboard available at /dashboard")
    print("📋 Enter Google Sheet ID in console to start automated calls")


@app.on_event("shutdown")
async def shutdown_event():
    """Close connections and cleanup on shutdown"""
    await db_service.disconnect()
    await call_queue_manager.stop_monitoring()


def main():
    print("🚀 Starting Aveya IVF Voice Assistant Server with Google Sheets Integration...")
    print("📊 Dashboard: http://localhost:8090/dashboard")
    print("🎮 Console: http://localhost:8090/console")
    print("🔗 API Status: http://localhost:8090/status")
    print("📋 Google Sheets Integration: Connect via console")
    uvicorn.run(app, host="0.0.0.0", port=settings.PORT)


if __name__ == "__main__":
    main()