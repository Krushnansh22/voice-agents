#call_analyzer_summarizer
import asyncio
import json
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import logging
import pytz
import google.generativeai as genai
from enum import Enum
from settings import settings

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s:%(name)s:%(message)s')
logger = logging.getLogger(__name__)


# Replicate the CallResult enum from queue manager
class CallResult(Enum):
    APPOINTMENT_BOOKED = "appointment_booked"
    RESCHEDULE_REQUESTED = "reschedule_requested"
    CALL_INCOMPLETE = "call_incomplete"
    NOT_INTERESTED = "not_interested"
    FOLLOW_UP_NEEDED = "follow_up_needed"


class CallAnalyzer:
    """Analyze and summarize call transcriptions with Google Sheets storage"""

    def __init__(self):
        # Configure Gemini
        genai.configure(api_key=settings.GEMINI_API_KEY)
        self.gemini_model = genai.GenerativeModel('gemini-2.5-flash')

        # Outcome mapping from queue manager to analyzer
        self.outcome_mapping = {
            CallResult.APPOINTMENT_BOOKED.value: "Appointment Booked",
            CallResult.RESCHEDULE_REQUESTED.value: "Reschedule Requested",
            CallResult.CALL_INCOMPLETE.value: "Call Incomplete",
            CallResult.NOT_INTERESTED.value: "Not Interested",
            CallResult.FOLLOW_UP_NEEDED.value: "Follow Up Needed"
        }

        # Google Sheets service will be injected
        self.sheets_service = None

    def set_sheets_service(self, sheets_service):
        """Inject Google Sheets service"""
        self.sheets_service = sheets_service
        logger.info("✅ Google Sheets service injected into Call Analyzer")

    async def analyze_call(self, call_data: Dict) -> Optional[Dict]:
        """Analyze a call and return summary"""
        try:
            logger.info(f"🔍 Analyzing call: {call_data['call_id']}")
            logger.info(f"📝 Transcript length: {len(call_data.get('transcript', ''))}")

            # Calculate duration
            duration = (call_data['end_time'] - call_data['start_time']).total_seconds()
            duration_str = str(timedelta(seconds=int(duration)))

            # Use pre-determined outcome from queue manager if available
            if call_data.get('call_result'):
                call_outcome = self._map_queue_outcome(call_data['call_result'])
                ai_summary = call_data.get('result_details', 'No details provided')
                logger.info(f"📋 Using pre-determined outcome from queue: {call_outcome}")
            else:
                # Generate AI summary and extract outcome using new format
                logger.info("🤖 Generating AI summary with Gemini...")
                gemini_response = await self.generate_ai_summary(
                    call_data['transcript'],
                    call_data['patient_name']
                )

                logger.info(f"🤖 Raw Gemini response: {gemini_response}")

                # Parse the new Gemini response format
                parsed_result = self.parse_gemini_response(gemini_response)
                call_outcome = parsed_result['call_outcome']
                ai_summary = parsed_result['summary']

                logger.info(f"✅ Parsed outcome: {call_outcome}")
                logger.info(f"✅ Parsed summary: {ai_summary[:100]}...")

            # Prepare result
            result = {
                "call_id": call_data['call_id'],
                "name": call_data['patient_name'],
                "phone_number": call_data['patient_phone'],
                "summary": ai_summary,
                "duration": duration_str,
                "date": call_data['start_time'].astimezone(pytz.timezone('Asia/Kolkata')).strftime(
                    "%Y-%m-%d %H:%M:%S %Z"),
                "call_outcome": call_outcome,
                "transcript_count": len(call_data['transcript'].split('\n')),
                "outcome_details": call_data.get('result_details', ''),
                "start_time": call_data['start_time'],
                "end_time": call_data['end_time']
            }

            logger.info(f"📊 Analysis result prepared: {result['call_outcome']}")

            # Save to Google Sheets instead of Excel
            if self.sheets_service:
                logger.info("💾 Attempting to save to Google Sheets...")
                success = await self.save_to_google_sheets(result)
                if success:
                    logger.info("✅ Call analysis saved to Google Sheets")
                else:
                    logger.error("❌ Failed to save call analysis to Google Sheets")
            else:
                logger.error("❌ Google Sheets service not available")

            return result

        except Exception as e:
            logger.error(f"❌ Error analyzing call: {str(e)}")
            import traceback
            logger.error(f"❌ Full traceback: {traceback.format_exc()}")
            return None

    def parse_gemini_response(self, gemini_response: str) -> Dict:
        """Parse the new Gemini response format with enhanced debugging"""
        try:
            logger.info(f"🔍 Parsing Gemini response: {gemini_response[:200]}...")

            # Clean up the response - remove any markdown formatting
            clean_response = gemini_response.strip()

            # Remove code block markers if present
            if clean_response.startswith("```json"):
                clean_response = clean_response[7:]
            if clean_response.startswith("```"):
                clean_response = clean_response[3:]
            if clean_response.endswith("```"):
                clean_response = clean_response[:-3]

            clean_response = clean_response.strip()
            logger.info(f"🧹 Cleaned response: {clean_response[:200]}...")

            # Try to parse as JSON first
            try:
                parsed = json.loads(clean_response)
                logger.info("✅ Successfully parsed as JSON")

                call_outcome = parsed.get('call_outcome', parsed.get('Call_Outcome', 'Follow Up Needed'))
                summary = parsed.get('Summary', parsed.get('summary', 'No summary provided'))

                logger.info(f"📋 Extracted outcome: {call_outcome}")
                logger.info(f"📝 Extracted summary: {summary[:100]}...")

                return {
                    'call_outcome': call_outcome,
                    'summary': summary
                }
            except json.JSONDecodeError as json_error:
                logger.warning(f"⚠️ JSON parsing failed: {json_error}")

            # If not JSON, try regex parsing for the expected format
            # Pattern: {call_outcome : "value", Summary : "value"}
            patterns = [
                # Standard format
                r'\{\s*["\']?call_outcome["\']?\s*:\s*["\']([^"\']+)["\']?\s*,\s*["\']?Summary["\']?\s*:\s*["\']([^"\']*)["\']?\s*\}',
                # Alternative format
                r'\{\s*["\']?Call_Outcome["\']?\s*:\s*["\']([^"\']+)["\']?\s*,\s*["\']?summary["\']?\s*:\s*["\']([^"\']*)["\']?\s*\}',
                # More flexible format
                r'call_outcome["\']?\s*:\s*["\']([^"\']+)["\']?.*?Summary["\']?\s*:\s*["\']([^"\']*)["\']?',
            ]

            for i, pattern in enumerate(patterns):
                match = re.search(pattern, clean_response, re.IGNORECASE | re.DOTALL)
                if match:
                    logger.info(f"✅ Matched with pattern {i + 1}")
                    return {
                        'call_outcome': match.group(1).strip(),
                        'summary': match.group(2).strip()
                    }

            # Fallback: try to extract call_outcome and Summary separately
            logger.warning("⚠️ Trying fallback parsing...")

            outcome_patterns = [
                r'call_outcome["\']?\s*:\s*["\']([^"\']+)["\']?',
                r'Call_Outcome["\']?\s*:\s*["\']([^"\']+)["\']?',
            ]

            summary_patterns = [
                r'Summary["\']?\s*:\s*["\']([^"\']*)["\']?',
                r'summary["\']?\s*:\s*["\']([^"\']*)["\']?',
            ]

            call_outcome = 'Follow Up Needed'
            summary = clean_response

            for pattern in outcome_patterns:
                outcome_match = re.search(pattern, clean_response, re.IGNORECASE)
                if outcome_match:
                    call_outcome = outcome_match.group(1).strip()
                    logger.info(f"📋 Found outcome with fallback: {call_outcome}")
                    break

            for pattern in summary_patterns:
                summary_match = re.search(pattern, clean_response, re.IGNORECASE | re.DOTALL)
                if summary_match:
                    summary = summary_match.group(1).strip()
                    logger.info(f"📝 Found summary with fallback: {summary[:100]}...")
                    break

            return {
                'call_outcome': call_outcome,
                'summary': summary if summary else clean_response
            }

        except Exception as e:
            logger.error(f"❌ Error parsing Gemini response: {e}")
            logger.error(f"❌ Raw response was: {gemini_response}")
            return {
                'call_outcome': 'Follow Up Needed',
                'summary': f"Error parsing response: {gemini_response[:500]}..."
            }

    def _map_queue_outcome(self, queue_outcome: str) -> str:
        """Map queue manager outcome to analyzer outcome"""
        mapped = self.outcome_mapping.get(queue_outcome, "Follow Up Needed")
        logger.info(f"📋 Mapped queue outcome '{queue_outcome}' to '{mapped}'")
        return mapped

    async def generate_ai_summary(self, transcript: str, patient_name: str) -> str:
        """Generate AI summary using Gemini with new response format"""
        try:
            # Check if transcript is meaningful
            if not transcript or len(transcript.strip()) < 10:
                logger.warning("⚠️ Transcript too short or empty")
                return '{"call_outcome": "Call Incomplete", "Summary": "Transcript was too short or empty"}'

            prompt = f"""You are analyzing a call transcript from an IVF clinic. The patient's name is {patient_name}.

    Please analyze this call transcript and respond with EXACTLY this JSON format (no extra text, no markdown):

    {{
        "call_outcome": "one of: Appointment Booked, Reschedule Requested, Call Incomplete, Not Interested, Follow Up Needed",
        "Summary": "comprehensive detailed summary of what happened in the call"
    }}

    Guidelines for call_outcome:
    - "Appointment Booked": When a specific appointment is scheduled and confirmed (look for phrases like "slot book कर लिया", "appointment confirm", "बुक हो गया")
    - "Reschedule Requested": When patient asks for callback or different timing (look for "reschedule", "बाद में call", "दूसरे time")
    - "Call Incomplete": When call drops, technical issues, or very short interaction
    - "Not Interested": When patient clearly declines or shows no interest
    - "Follow Up Needed": When more information or future contact is required

    IMPORTANT: For the Summary field, ALWAYS provide a conversational summary of what happened during the call, NOT structured data or appointment details.

    For the Summary, include:
    - How the call started and who initiated it
    - What the patient's main concerns or questions were
    - Key points discussed during the conversation
    - Any medical information or advice shared
    - How the call concluded
    - If an appointment was booked, mention it naturally in the summary (e.g., "Patient agreed to book an appointment for next week") but DO NOT provide structured appointment details like dates/times

    Example of GOOD Summary for appointment booking:
    "Patient called regarding fertility consultation. Discussed their concerns about conceiving after 2 years of trying. Explained the IVF process and available treatments. Patient showed interest and agreed to book an appointment for initial consultation. Call ended positively with patient expressing gratitude for the information provided."

    Example of BAD Summary (DO NOT DO THIS):
    "Date: 2025-07-12 10:00 AM, Time: 10:00 AM"

    CALL TRANSCRIPT:
    {transcript}

    Respond with ONLY the JSON format above:"""

            logger.info(f"🤖 Sending prompt to Gemini (transcript length: {len(transcript)})")

            response = await asyncio.to_thread(
                self.gemini_model.generate_content,
                prompt
            )

            result = response.text.strip()
            logger.info(f"🤖 Gemini response received (length: {len(result)})")

            return result

        except Exception as e:
            logger.error(f"❌ Error generating Gemini summary: {str(e)}")
            return f'{{"call_outcome": "Follow Up Needed", "Summary": "Error generating summary: {str(e)}"}}'

    async def save_to_google_sheets(self, analysis_result: Dict) -> bool:
        """Save call analysis to Google Sheets Call_Analysis worksheet"""
        try:
            if not self.sheets_service:
                logger.error("❌ Google Sheets service not available")
                return False

            if not self.sheets_service.current_spreadsheet:
                logger.error("❌ No spreadsheet connected")
                return False

            logger.info(f"💾 Saving analysis to Google Sheets: {analysis_result['call_id']}")

            # Use the append_call_analysis method from the sheets service
            success = await self.sheets_service.append_call_analysis(analysis_result)

            if success:
                logger.info("✅ Successfully saved to Google Sheets")
            else:
                logger.error("❌ Failed to save to Google Sheets")

            return success

        except Exception as e:
            logger.error(f"❌ Error saving to Google Sheets: {e}")
            import traceback
            logger.error(f"❌ Full traceback: {traceback.format_exc()}")
            return False