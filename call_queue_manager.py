"""
Enhanced Call Queue Manager with Improved Stop Functionality
"""
import asyncio
import logging
import openpyxl
from typing import List, Dict, Optional
from enum import Enum
from datetime import datetime
import io
import pandas as pd
import httpx
from settings import settings

logger = logging.getLogger(__name__)


class QueueStatus(Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    COMPLETED = "completed"
    ERROR = "error"


class CallResult(Enum):
    PENDING = "pending"
    CALLING = "calling"
    APPOINTMENT_BOOKED = "appointment_booked"
    RESCHEDULE_REQUESTED = "reschedule_requested"
    CALL_INCOMPLETE = "call_incomplete"
    CALL_FAILED = "call_failed"
    SKIPPED = "skipped"


class CallRecord:
    def __init__(self, index: int, name: str, phone: str, address: str, age: str, gender: str):
        self.index = index
        self.name = name
        self.phone = phone
        self.address = address
        self.age = age
        self.gender = gender
        self.status = CallResult.PENDING
        self.attempts = 0
        self.last_attempt = None
        self.result_details = None
        self.created_at = datetime.now()

    def to_dict(self):
        return {
            "index": self.index,
            "name": self.name,
            "phone": self.phone,
            "address": self.address,
            "age": self.age,
            "gender": self.gender,
            "status": self.status.value,
            "attempts": self.attempts,
            "last_attempt": self.last_attempt.isoformat() if self.last_attempt else None,
            "result_details": self.result_details,
            "created_at": self.created_at.isoformat()
        }


class CallQueueManager:
    """Manages the calling queue with full control capabilities"""

    def __init__(self):
        self.status = QueueStatus.IDLE
        self.records: List[CallRecord] = []
        self.current_index = 0
        self.total_records = 0
        self.uploaded_filename = None
        self.upload_timestamp = None

        # Statistics
        self.stats = {
            "total_calls": 0,
            "successful_appointments": 0,
            "reschedule_requests": 0,
            "incomplete_calls": 0,
            "failed_calls": 0,
            "queue_started_at": None,
            "queue_completed_at": None
        }

        # Control flags - IMPROVED
        self._should_stop = False
        self._calling_task = None
        self._call_in_progress = False  # NEW: Track if a call is currently active
        self._stop_after_current_call = False  # NEW: Flag to stop after current call

        logger.info("CallQueueManager initialized")

    async def upload_records(self, file_content: bytes, filename: str) -> Dict:
        """Upload and parse Excel file with patient records"""
        try:
            logger.info(f"Uploading records from file: {filename}")

            # Reset previous data
            self.records = []
            self.current_index = 0
            self.status = QueueStatus.IDLE

            # Parse Excel file
            df = pd.read_excel(io.BytesIO(file_content))

            # Validate required columns
            required_columns = ['Name', 'Phone Number', 'Address', 'Age', 'Gender']
            missing_columns = [col for col in required_columns if col not in df.columns]

            if missing_columns:
                raise ValueError(f"Missing required columns: {missing_columns}")

            # Process records
            valid_records = 0
            errors = []

            for index, row in df.iterrows():
                try:
                    # Validate phone number
                    phone = str(row['Phone Number']).strip()
                    if not phone or phone == 'nan':
                        errors.append(f"Row {index + 2}: Missing phone number")
                        continue

                    # Create call record
                    record = CallRecord(
                        index=valid_records,
                        name=str(row['Name']).strip(),
                        phone=phone,
                        address=str(row['Address']).strip(),
                        age=str(row['Age']).strip(),
                        gender=str(row['Gender']).strip()
                    )

                    self.records.append(record)
                    valid_records += 1

                except Exception as e:
                    errors.append(f"Row {index + 2}: {str(e)}")

            self.total_records = len(self.records)
            self.uploaded_filename = filename
            self.upload_timestamp = datetime.now()

            logger.info(f"Successfully loaded {self.total_records} records from {filename}")

            return {
                "success": True,
                "total_records": self.total_records,
                "valid_records": valid_records,
                "errors": errors[:10],  # Limit errors shown
                "filename": filename,
                "upload_timestamp": self.upload_timestamp.isoformat()
            }

        except Exception as e:
            logger.error(f"Failed to upload records: {e}")
            return {
                "success": False,
                "error": str(e),
                "total_records": 0
            }

    async def start_queue(self) -> Dict:
        """Start the calling queue"""
        try:
            if not self.records:
                return {"success": False, "error": "No records uploaded"}

            if self.status == QueueStatus.RUNNING:
                return {"success": False, "error": "Queue is already running"}

            self.status = QueueStatus.RUNNING
            self._should_stop = False
            self._stop_after_current_call = False  # Reset stop flag
            self.stats["queue_started_at"] = datetime.now()

            # Start the calling task
            self._calling_task = asyncio.create_task(self._calling_loop())

            logger.info(f"Started calling queue with {self.total_records} records")

            return {
                "success": True,
                "status": self.status.value,
                "total_records": self.total_records,
                "current_index": self.current_index
            }

        except Exception as e:
            logger.error(f"Failed to start queue: {e}")
            self.status = QueueStatus.ERROR
            return {"success": False, "error": str(e)}

    async def pause_queue(self) -> Dict:
        """Pause the calling queue"""
        if self.status == QueueStatus.RUNNING:
            self.status = QueueStatus.PAUSED
            logger.info("Queue paused")
            return {"success": True, "status": self.status.value}

        return {"success": False, "error": f"Cannot pause queue in {self.status.value} state"}

    async def resume_queue(self) -> Dict:
        """Resume the paused calling queue"""
        if self.status == QueueStatus.PAUSED:
            self.status = QueueStatus.RUNNING
            logger.info("Queue resumed")
            return {"success": True, "status": self.status.value}

        return {"success": False, "error": f"Cannot resume queue in {self.status.value} state"}

    async def stop_queue(self) -> Dict:
        """Stop the calling queue - IMPROVED VERSION"""
        try:
            logger.info("🛑 Stop queue requested")
            
            # Set stop flags
            self._should_stop = True
            
            # Check if a call is currently in progress
            if self._call_in_progress:
                logger.info("📞 Call in progress - will stop after current call completes")
                self._stop_after_current_call = True
                self.status = QueueStatus.STOPPED  # Update status immediately
                
                return {
                    "success": True,
                    "status": self.status.value,
                    "message": "Queue will stop after current call completes",
                    "call_in_progress": True,
                    "calls_completed": self.current_index,
                    "total_records": self.total_records
                }
            else:
                # No call in progress, stop immediately
                self.status = QueueStatus.STOPPED

                # Cancel the calling task
                if self._calling_task and not self._calling_task.done():
                    self._calling_task.cancel()
                    try:
                        await self._calling_task
                    except asyncio.CancelledError:
                        logger.info("✅ Calling task cancelled successfully")

                logger.info("✅ Queue stopped immediately - no active call")

                return {
                    "success": True,
                    "status": self.status.value,
                    "message": "Queue stopped immediately",
                    "call_in_progress": False,
                    "calls_completed": self.current_index,
                    "total_records": self.total_records
                }

        except Exception as e:
            logger.error(f"Failed to stop queue: {e}")
            return {"success": False, "error": str(e)}

    async def skip_current_call(self) -> Dict:
        """Skip the current call and move to next"""
        if self.status != QueueStatus.RUNNING:
            return {"success": False, "error": "Queue is not running"}

        if self.current_index < len(self.records):
            current_record = self.records[self.current_index]
            current_record.status = CallResult.SKIPPED
            current_record.result_details = "Manually skipped"

            self.current_index += 1
            logger.info(f"Skipped call to {current_record.name} ({current_record.phone})")

            return {
                "success": True,
                "skipped_record": current_record.to_dict(),
                "next_index": self.current_index
            }

        return {"success": False, "error": "No current call to skip"}

    async def reset_queue(self) -> Dict:
        """Reset the queue to start from beginning"""
        try:
            await self.stop_queue()

            # Reset all record statuses
            for record in self.records:
                record.status = CallResult.PENDING
                record.attempts = 0
                record.last_attempt = None
                record.result_details = None

            self.current_index = 0
            self.status = QueueStatus.IDLE
            self._call_in_progress = False  # Reset call progress flag
            self._stop_after_current_call = False  # Reset stop flag

            # Reset stats
            self.stats = {
                "total_calls": 0,
                "successful_appointments": 0,
                "reschedule_requests": 0,
                "incomplete_calls": 0,
                "failed_calls": 0,
                "queue_started_at": None,
                "queue_completed_at": None
            }

            logger.info("Queue reset successfully")

            return {
                "success": True,
                "status": self.status.value,
                "total_records": self.total_records
            }

        except Exception as e:
            logger.error(f"Failed to reset queue: {e}")
            return {"success": False, "error": str(e)}

    def get_status(self) -> Dict:
        """Get current queue status and statistics"""

        # Helper function to serialize datetime objects
        def serialize_datetime(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            return obj

        # Serialize stats with datetime conversion
        serialized_stats = {}
        for key, value in self.stats.items():
            serialized_stats[key] = serialize_datetime(value)

        return {
            "status": self.status.value,
            "total_records": self.total_records,
            "current_index": self.current_index,
            "progress_percentage": (self.current_index / self.total_records * 100) if self.total_records > 0 else 0,
            "remaining_calls": max(0, self.total_records - self.current_index),
            "uploaded_filename": self.uploaded_filename,
            "upload_timestamp": self.upload_timestamp.isoformat() if self.upload_timestamp else None,
            "stats": serialized_stats,
            "current_record": self.records[self.current_index].to_dict() if self.current_index < len(
                self.records) else None,
            "call_in_progress": self._call_in_progress,  # NEW: Include call progress status
            "stop_pending": self._stop_after_current_call  # NEW: Include stop pending status
        }

    def get_current_record(self) -> Optional[CallRecord]:
        """Get the current record being processed"""
        if self.current_index < len(self.records):
            return self.records[self.current_index]
        return None

    async def mark_call_result(self, result: CallResult, details: str = None):
        """Mark the result of the current call"""
        if self.current_index < len(self.records):
            record = self.records[self.current_index]
            record.status = result
            record.last_attempt = datetime.now()
            record.attempts += 1
            record.result_details = details

            # Update statistics
            self.stats["total_calls"] += 1

            if result == CallResult.APPOINTMENT_BOOKED:
                self.stats["successful_appointments"] += 1
            elif result == CallResult.RESCHEDULE_REQUESTED:
                self.stats["reschedule_requests"] += 1
            elif result == CallResult.CALL_INCOMPLETE:
                self.stats["incomplete_calls"] += 1
            elif result == CallResult.CALL_FAILED:
                self.stats["failed_calls"] += 1

            logger.info(f"Call result marked: {result.value} for {record.name}")

    async def move_to_next_record(self):
        """Move to the next record in the queue"""
        self.current_index += 1

        if self.current_index >= self.total_records:
            self.status = QueueStatus.COMPLETED
            self.stats["queue_completed_at"] = datetime.now()
            logger.info("All calls completed!")

    async def _calling_loop(self):
        """Internal calling loop - COMPLETELY FIXED VERSION"""
        try:
            while (self.current_index < self.total_records and
                not self._should_stop and
                self.status in [QueueStatus.RUNNING, QueueStatus.PAUSED]):

                # Check for stop condition at the start of each iteration
                if self._should_stop:
                    logger.info("🛑 Stop flag detected - exiting calling loop")
                    break

                if self.status == QueueStatus.PAUSED:
                    await asyncio.sleep(1)
                    continue

                current_record = self.get_current_record()
                if current_record and current_record.status == CallResult.PENDING:
                    logger.info(
                        f"🔄 Processing call {self.current_index + 1}/{self.total_records}: {current_record.name}")

                    # Set call in progress flag BEFORE making call
                    self._call_in_progress = True

                    # Make the actual call via webhook
                    success = await self._make_actual_call(current_record)

                    if success:
                        logger.info(f"✅ Call initiated successfully for {current_record.name}")

                        # CRITICAL: Wait for call to complete - FIXED LOGIC
                        call_timeout = 0
                        max_call_duration = 600  # 10 minutes max per call
                        check_interval = 5  # Check every 5 seconds

                        # Wait until call is no longer in CALLING status
                        while (current_record.status == CallResult.CALLING and
                            not self._should_stop and
                            self.status in [QueueStatus.RUNNING, QueueStatus.STOPPED] and
                            call_timeout < max_call_duration):
                            
                            logger.info(f"⏳ Waiting for call to complete: {current_record.name} (timeout: {call_timeout}s)")
                            await asyncio.sleep(check_interval)
                            call_timeout += check_interval
                            
                            # Check for stop condition during call
                            if self._should_stop:
                                logger.info(f"🛑 Stop requested during call to {current_record.name}")
                                break

                        # Call completed or timed out - clear in progress flag
                        self._call_in_progress = False

                        # If call timed out, mark as failed and move to next
                        if call_timeout >= max_call_duration and current_record.status == CallResult.CALLING:
                            logger.warning(f"⏰ Call timed out for {current_record.name}")
                            await self.mark_call_result(CallResult.CALL_FAILED, "Call timeout - exceeded maximum duration")
                            await self.move_to_next_record()

                        # Check if we should stop after this call
                        if self._stop_after_current_call:
                            logger.info(f"🛑 Stopping queue after completing call to {current_record.name}")
                            break

                        # If call completed successfully, the move_to_next_record will be handled by complete_current_call
                        logger.info(f"✅ Call completed for {current_record.name}, status: {current_record.status.value}")

                    else:
                        # Call failed to initiate, move to next
                        logger.error(f"❌ Failed to initiate call for {current_record.name}")
                        await self.mark_call_result(CallResult.CALL_FAILED, "Failed to initiate call")
                        self._call_in_progress = False  # Clear flag
                        await self.move_to_next_record()

                    # Brief pause between call attempts (only if not stopping)
                    if not self._should_stop and self.status == QueueStatus.RUNNING:
                        await asyncio.sleep(10)  # 10 second pause between calls

                else:
                    # Current record already processed or no record, move to next
                    logger.info(f"📝 Current record already processed or invalid, moving to next")
                    await self.move_to_next_record()

            # Queue completed or stopped
            if self.current_index >= self.total_records and not self._should_stop:
                self.status = QueueStatus.COMPLETED
                self.stats["queue_completed_at"] = datetime.now()
                logger.info("🎉 All calls in queue completed!")
            elif self._should_stop:
                self.status = QueueStatus.STOPPED
                logger.info("🛑 Queue stopped as requested")

        except asyncio.CancelledError:
            logger.info("⏹️ Calling loop cancelled")
            self.status = QueueStatus.STOPPED
        except Exception as e:
            logger.error(f"❌ Error in calling loop: {e}")
            self.status = QueueStatus.ERROR
        finally:
            # Clean up flags
            self._call_in_progress = False
            self._stop_after_current_call = False

    async def _make_actual_call(self, record):
        """Make the actual call via webhook to Plivo"""
        try:
            logger.info(f"📞 Initiating call to {record.name} ({record.phone})")

            # Make the webhook call to trigger Plivo
            async with httpx.AsyncClient(timeout=60.0) as client:
                webhook_url = f"{settings.HOST_URL}/webhook"
                logger.info(f"🔗 Calling webhook: {webhook_url}")

                response = await client.post(webhook_url, headers={
                    "Content-Type": "application/json"
                })

            if response.status_code == 200:
                response_data = response.json()
                logger.info(f"✅ Webhook response: {response_data}")
                return True
            else:
                logger.error(f"❌ Webhook failed - Status: {response.status_code}, Response: {response.text}")
                return False

        except Exception as e:
            logger.error(f"❌ Exception during webhook call: {e}")
            return False

    async def complete_current_call(self, result: CallResult, details: str = None):
        """Mark current call as complete and move to next - IMPROVED WITH STOP HANDLING"""
        if self.current_index < len(self.records):
            current_record = self.records[self.current_index]
            current_record.status = result
            current_record.result_details = details
            current_record.last_attempt = datetime.now()

            # Update statistics
            self.stats["total_calls"] += 1

            if result == CallResult.APPOINTMENT_BOOKED:
                self.stats["successful_appointments"] += 1
            elif result == CallResult.RESCHEDULE_REQUESTED:
                self.stats["reschedule_requests"] += 1
            elif result == CallResult.CALL_INCOMPLETE:
                self.stats["incomplete_calls"] += 1
            elif result == CallResult.CALL_FAILED:
                self.stats["failed_calls"] += 1

            logger.info(f"✅ Call completed: {result.value} for {current_record.name}")

            # Clear call in progress flag
            self._call_in_progress = False

            # CRITICAL: Check if we should stop before moving to next record
            if self._stop_after_current_call or self._should_stop:
                logger.info("🛑 Queue stop requested - NOT moving to next record")
                self.status = QueueStatus.STOPPED
                
                # Cancel the calling task if it exists
                if self._calling_task and not self._calling_task.done():
                    self._calling_task.cancel()
                
                # Reset stop flags
                self._stop_after_current_call = False
                
                logger.info(f"🛑 Queue stopped after completing call to {current_record.name}")
            else:
                # Normal flow - move to next record
                await self.move_to_next_record()
                logger.info(f"➡️ Moving to next record (index: {self.current_index})")

        else:
            logger.warning("⚠️ No current record to complete")
            self._call_in_progress = False


# Global instance
call_queue_manager = CallQueueManager()