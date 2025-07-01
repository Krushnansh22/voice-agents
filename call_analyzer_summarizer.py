import asyncio
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import logging
import pytz
import google.generativeai as genai
from enum import Enum
import os
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
    """Analyze and summarize call transcriptions"""
    
    def __init__(self):
        # Configure Gemini
        genai.configure(api_key=settings.GEMINI_API_KEY) 
        self.gemini_model = genai.GenerativeModel('gemini-2.5-flash')
        
        # Outcome mapping from queue manager to analyzer
        self.outcome_mapping = {
            CallResult.APPOINTMENT_BOOKED.value: "appointment_booked",
            CallResult.RESCHEDULE_REQUESTED.value: "reschedule_requested",
            CallResult.CALL_INCOMPLETE.value: "call_incomplete",
            # Add other mappings as needed
        }
    
    async def analyze_call(self, call_data: Dict) -> Optional[Dict]:
        """Analyze a call and return summary"""
        try:
            logger.info(f"🔍 Analyzing call: {call_data['call_id']}")
            
            # Calculate duration
            duration = (call_data['end_time'] - call_data['start_time']).total_seconds()
            duration_str = str(timedelta(seconds=int(duration)))
            
            # Use pre-determined outcome from queue manager if available
            if call_data.get('call_result'):
                call_outcome = self._map_queue_outcome(call_data['call_result'])
                ai_summary = call_data.get('result_details', 'No details provided')
                print(f"Using pre-determined outcome from queue: {call_outcome}")
            else:
                # Generate AI summary if no outcome from queue manager
                ai_summary = await self.generate_ai_summary(
                    call_data['transcript'],
                    call_data['patient_name']
                )
                call_outcome = self.determine_call_outcome(
                    call_data['transcript'],
                    ai_summary
                )
            
            # Prepare result
            result = {
                "call_id": call_data['call_id'],
                "name": call_data['patient_name'],
                "phone_number": call_data['patient_phone'],
                "summary": ai_summary,
                "duration": duration_str,
                "date": call_data['start_time'].astimezone(pytz.timezone('Asia/Kolkata')).strftime("%Y-%m-%d %H:%M:%S %Z"),
                "call_outcome": call_outcome,
                "transcript_count": len(call_data['transcript'].split('\n')),
                "outcome_details": call_data.get('result_details', '')
            }
            
            return result
            
        except Exception as e:
            logger.error(f"Error analyzing call: {str(e)}")
            return None
    
    def _map_queue_outcome(self, queue_outcome: str) -> str:
        """Map queue manager outcome to analyzer outcome"""
        return self.outcome_mapping.get(queue_outcome, "follow_up_needed")
    
    async def generate_ai_summary(self, transcript: str, patient_name: str) -> str:
        """Generate AI summary using Gemini"""
        try:
            prompt = f"""Analyze this IVF clinic call with {patient_name} and provide a comprehensive summary covering:
1. Call purpose and context
2. Patient's main concerns
3. Relevant medical history mentioned
4. Current fertility status
5. Any appointment details discussed
6. Final outcome of the call

Focus on extracting key medical and logistical information. Be concise but thorough.

CALL TRANSCRIPT:
{transcript}

SUMMARY:"""
            
            response = await asyncio.to_thread(
                self.gemini_model.generate_content,
                prompt
            )
            
            return response.text.strip()
        except Exception as e:
            logger.error(f"Error generating Gemini summary: {str(e)}")
            return "Error generating summary"
    
    def determine_call_outcome(self, transcript: str, summary: str) -> str:
        """Determine call outcome based on transcript and summary (only used if no queue outcome)"""
        content = (transcript + " " + summary).lower()
        
        # Check for appointment booking triggers
        if any(trigger.lower() in content for trigger in [
            'slot book कर लिया', 'appointment scheduled', 'अपॉइंटमेंट बुक हो गया'
        ]):
            return "appointment_booked"
            
        # Check for reschedule triggers    
        elif any(trigger.lower() in content for trigger in [
            'बिल्कुल समझ सकती हूँ', 'आप बताइए कि कब', 'reschedule'
        ]):
            return "reschedule_requested"
            
        # Check for incomplete call triggers
        elif any(trigger.lower() in content for trigger in [
            'call disconnected', 'call dropped', 'कॉल कट गई'
        ]):
            return "call_incomplete"
            
        return "follow_up_needed"
    
    def save_to_excel(self, summaries: List[Dict]) -> str:
        """Save summaries to Excel file (appends to existing)"""
        try:
            filename = "call-summaries.xlsx"
            data = [{
                "Call ID": s["call_id"],
                "Name": s["name"],
                "Phone Number": s["phone_number"],
                "Date": s["date"],
                "Duration": s["duration"],
                "Call Outcome": s["call_outcome"],
                "Transcript Count": s["transcript_count"],
                "AI Summary": s["summary"],
                "Outcome Details": s.get("outcome_details", "")
            } for s in summaries]
            
            df = pd.DataFrame(data)
            
            if os.path.exists(filename):
                existing_df = pd.read_excel(filename)
                df = pd.concat([existing_df, df], ignore_index=True)
            
            df.to_excel(filename, index=False)
            return filename
            
        except Exception as e:
            logger.error(f"Error saving to Excel: {e}")
            return ""
        
""" 
# Test data for CallAnalyzer
test_call_data = {
    "call_id": "test123",
    "patient_name": "Priya Sharma",
    "patient_phone": "+919876543210",
    "start_time": datetime.now(pytz.timezone('Asia/Kolkata')) - timedelta(minutes=15),
    "end_time": datetime.now(pytz.timezone('Asia/Kolkata')),
    "transcript":,
    # Optional: You can test with/without call_result
    # "call_result": "appointment_booked",
    # "result_details": "Booked consultation for IVF treatment"
}

# Example test script
async def test_analyzer():
    analyzer = CallAnalyzer()
    result = await analyzer.analyze_call(test_call_data)
    
    if result:
        print("\n📝 Call Analysis Result:")
        print(f"Call ID: {result['call_id']}")
        print(f"Patient: {result['name']}")
        print(f"Outcome: {result['call_outcome']}")
        print(f"Summary:\n{result['summary']}")
        print(f"Duration: {result['duration']}")
        
        # Save to Excel test
        filename = analyzer.save_to_excel([result])
        if filename:
            print(f"\n✅ Results saved to {filename}")

# Run the test
if __name__ == "__main__":
    asyncio.run(test_analyzer()) """