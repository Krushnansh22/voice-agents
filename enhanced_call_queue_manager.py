"""
Enhanced Call Queue Manager with Google Sheets Integration
"""
import asyncio
import logging
from typing import List, Dict, Optional
from enum import Enum
from datetime import datetime
import httpx
from settings import settings
from google_sheets_service import google_sheets_service

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
    def __init__(self, index: int, name: str, phone: str, address: str, age: str, gender: str, row_number: int = None):
        self.index = index
        self.name = name
        self.phone = phone
        self.address = address
        self.age = age
        self.gender = gender
        self.row_number = row_number  # Google Sheets row number
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
            "row_number": self.row_number,
            "status": self.status.value,
            "attempts": self.attempts,
            "last_attempt": self.last_attempt.isoformat() if self.last_attempt else None,
            "result_details": self.result_details,
            "created_at": self.created_at.isoformat()
        }


class EnhancedCallQueueManager:
    """Enhanced Call Queue Manager with Google Sheets integration and dynamic record detection"""

    def __init__(self):
        self.status = QueueStatus.IDLE
        self.records: List[CallRecord] = []
        self.current_index = 0
        self.total_records = 0
        self.sheet_id = None
        self.sheet_connection_time = None

        # Statistics
        self.stats = {
            "total_calls": 0,
            "successful_appointments": 0,
            "reschedule_requests": 0,
            "incomplete_calls": 0,
            "failed_calls": 0,
            "queue_started_at": None,
            "queue_completed_at": None,
            "dynamically_added_records": 0
        }

        # Control flags
        self._should_stop = False
        self._calling_task = None
        self._call_in_progress = False
        self._stop_after_current_call = False

        logger.info("Enhanced CallQueueManager with Google Sheets initialized")

    async def connect_to_google_sheet(self, sheet_id: str, worksheet_name: str = None) -> Dict:
        """Connect to Google Sheet and load initial records"""
        try:
            logger.info(f"🔗 Connecting to Google Sheet: {sheet_id}")

            # Initialize Google Sheets service if not already done
            if not google_sheets_service.client:
                initialized = await google_sheets_service.initialize()
                if not initialized:
                    return {
                        "success": False,
                        "error": "Failed to initialize Google Sheets service"
                    }

            # Connect to the specific sheet
            connection_result = await google_sheets_service.connect_to_sheet(sheet_id, worksheet_name)

            if not connection_result["success"]:
                return connection_result

            # Load initial records
            records_data, errors = await google_sheets_service.read_all_records()

            # Reset previous data
            self.records = []
            self.current_index = 0
            self.status = QueueStatus.IDLE

            # Convert to CallRecord objects
            for record_data in records_data:
                call_record = CallRecord(
                    index=len(self.records),
                    name=record_data['name'],
                    phone=record_data['phone'],
                    address=record_data['address'],
                    age=record_data['age'],
                    gender=record_data['gender'],
                    row_number=record_data.get('row_number')
                )
                self.records.append(call_record)

            self.total_records = len(self.records)
            self.sheet_id = sheet_id
            self.sheet_connection_time = datetime.now()

            # Reset statistics
            self.stats["dynamically_added_records"] = 0

            logger.info(f"✅ Connected to Google Sheet with {self.total_records} initial records")

            return {
                "success": True,
                "sheet_id": sheet_id,
                "total_records": self.total_records,
                "worksheet_name": connection_result.get("worksheet_name"),
                "errors": errors[:10],  # Limit errors shown
                "connection_time": self.sheet_connection_time.isoformat()
            }

        except Exception as e:
            logger.error(f"❌ Failed to connect to Google Sheet: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def _handle_new_records(self, new_records: List[Dict]):
        """Callback function to handle dynamically added records"""
        try:
            logger.info(f"🆕 Processing {len(new_records)} new records from Google Sheet")

            added_count = 0
            for record_data in new_records:
                # Check if phone number already exists to avoid duplicates
                existing_phones = [record.phone for record in self.records]
                if record_data['phone'] not in existing_phones:

                    call_record = CallRecord(
                        index=len(self.records),
                        name=record_data['name'],
                        phone=record_data['phone'],
                        address=record_data['address'],
                        age=record_data['age'],
                        gender=record_data['gender'],
                        row_number=record_data.get('row_number')
                    )

                    self.records.append(call_record)
                    added_count += 1

                    logger.info(f"➕ Added new record: {record_data['name']} ({record_data['phone']})")
                else:
                    logger.info(f"⚠️ Skipped duplicate phone number: {record_data['phone']}")

            if added_count > 0:
                self.total_records = len(self.records)
                self.stats["dynamically_added_records"] += added_count

                logger.info(f"✅ Successfully added {added_count} new records to queue")
                logger.info(f"📊 Total records now: {self.total_records}")

        except Exception as e:
            logger.error(f"❌ Error handling new records: {e}")

    async def start_queue(self) -> Dict:
        """Start the calling queue with Google Sheets monitoring"""
        try:
            if not self.records:
                return {"success": False, "error": "No records loaded from Google Sheet"}

            if self.status == QueueStatus.RUNNING:
                return {"success": False, "error": "Queue is already running"}

            if not self.sheet_id:
                return {"success": False, "error": "No Google Sheet connected"}

            self.status = QueueStatus.RUNNING
            self._should_stop = False
            self._stop_after_current_call = False
            self.stats["queue_started_at"] = datetime.now()

            # Start monitoring Google Sheet for new records
            await google_sheets_service.start_monitoring(
                callback_func=self._handle_new_records,
                check_interval=30  # Check every 30 seconds
            )

            # Start the calling task
            self._calling_task = asyncio.create_task(self._calling_loop())

            logger.info(f"🚀 Started calling queue with {self.total_records} records and Google Sheets monitoring")

            return {
                "success": True,
                "status": self.status.value,
                "total_records": self.total_records,
                "current_index": self.current_index,
                "sheet_id": self.sheet_id,
                "monitoring_active": True
            }

        except Exception as e:
            logger.error(f"❌ Failed to start queue: {e}")
            self.status = QueueStatus.ERROR
            return {"success": False, "error": str(e)}

    async def pause_queue(self) -> Dict:
        """Pause the calling queue (keeps monitoring active)"""
        if self.status == QueueStatus.RUNNING:
            self.status = QueueStatus.PAUSED
            logger.info("⏸️ Queue paused (Google Sheets monitoring continues)")
            return {"success": True, "status": self.status.value}

        return {"success": False, "error": f"Cannot pause queue in {self.status.value} state"}

    async def resume_queue(self) -> Dict:
        """Resume the paused calling queue"""
        if self.status == QueueStatus.PAUSED:
            self.status = QueueStatus.RUNNING
            logger.info("▶️ Queue resumed")
            return {"success": True, "status": self.status.value}

        return {"success": False, "error": f"Cannot resume queue in {self.status.value} state"}

    async def stop_queue(self) -> Dict:
        """Stop the calling queue and monitoring"""
        try:
            logger.info("🛑 Stop queue requested")

            # Set stop flags
            self._should_stop = True

            # Stop Google Sheets monitoring
            await google_sheets_service.stop_monitoring()

            # Handle call in progress
            if self._call_in_progress:
                logger.info("📞 Call in progress - will stop after current call completes")
                self._stop_after_current_call = True
                self.status = QueueStatus.STOPPED

                return {
                    "success": True,
                    "status": self.status.value,
                    "message": "Queue will stop after current call completes",
                    "call_in_progress": True,
                    "monitoring_stopped": True
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

                logger.info("✅ Queue stopped immediately")

                return {
                    "success": True,
                    "status": self.status.value,
                    "message": "Queue stopped immediately",
                    "call_in_progress": False,
                    "monitoring_stopped": True
                }

        except Exception as e:
            logger.error(f"❌ Failed to stop queue: {e}")
            return {"success": False, "error": str(e)}

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
            self._call_in_progress = False
            self._stop_after_current_call = False

            # Reset stats but keep dynamic records count
            dynamic_count = self.stats.get("dynamically_added_records", 0)
            self.stats = {
                "total_calls": 0,
                "successful_appointments": 0,
                "reschedule_requests": 0,
                "incomplete_calls": 0,
                "failed_calls": 0,
                "queue_started_at": None,
                "queue_completed_at": None,
                "dynamically_added_records": dynamic_count
            }

            logger.info("🔄 Queue reset successfully")

            return {
                "success": True,
                "status": self.status.value,
                "total_records": self.total_records,
                "dynamic_records_preserved": dynamic_count
            }

        except Exception as e:
            logger.error(f"❌ Failed to reset queue: {e}")
            return {"success": False, "error": str(e)}

    def get_status(self) -> Dict:
        """Get current queue status with Google Sheets info"""
        def serialize_datetime(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            return obj

        # Serialize stats
        serialized_stats = {}
        for key, value in self.stats.items():
            serialized_stats[key] = serialize_datetime(value)

        # Get Google Sheets service status
        sheets_status = google_sheets_service.get_status()

        return {
            "status": self.status.value,
            "total_records": self.total_records,
            "current_index": self.current_index,
            "progress_percentage": (self.current_index / self.total_records * 100) if self.total_records > 0 else 0,
            "remaining_calls": max(0, self.total_records - self.current_index),
            "sheet_id": self.sheet_id,
            "sheet_connection_time": self.sheet_connection_time.isoformat() if self.sheet_connection_time else None,
            "stats": serialized_stats,
            "current_record": self.records[self.current_index].to_dict() if self.current_index < len(self.records) else None,
            "call_in_progress": self._call_in_progress,
            "stop_pending": self._stop_after_current_call,
            "google_sheets": sheets_status,
            "dynamic_records_added": self.stats.get("dynamically_added_records", 0)
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

            logger.info(f"📝 Call result marked: {result.value} for {record.name}")

    async def move_to_next_record(self):
        """Move to the next record in the queue"""
        self.current_index += 1

        if self.current_index >= self.total_records:
            # Check if we should wait for more records (if monitoring is active)
            if google_sheets_service.monitoring_active and not self._should_stop:
                logger.info("⏳ Reached end of current records, waiting for new records...")
                # Don't mark as completed yet, let monitoring add more records
                return
            else:
                self.status = QueueStatus.COMPLETED
                self.stats["queue_completed_at"] = datetime.now()
                logger.info("🎉 All calls completed!")

    async def _calling_loop(self):
        """Enhanced calling loop with dynamic record handling"""
        try:
            while (not self._should_stop and
                   self.status in [QueueStatus.RUNNING, QueueStatus.PAUSED]):

                # Check for stop condition at the start of each iteration
                if self._should_stop:
                    logger.info("🛑 Stop flag detected - exiting calling loop")
                    break

                if self.status == QueueStatus.PAUSED:
                    await asyncio.sleep(1)
                    continue

                # Check if we have records to process
                if self.current_index >= len(self.records):
                    # If monitoring is active, wait for new records
                    if google_sheets_service.monitoring_active and not self._should_stop:
                        logger.info("⏳ No more records, waiting for new records from Google Sheet...")
                        await asyncio.sleep(10)  # Wait 10 seconds then check again
                        continue
                    else:
                        # No monitoring or should stop, complete the queue
                        logger.info("🎉 All calls completed - no more records")
                        self.status = QueueStatus.COMPLETED
                        self.stats["queue_completed_at"] = datetime.now()
                        break

                current_record = self.get_current_record()
                if current_record and current_record.status == CallResult.PENDING:
                    logger.info(
                        f"🔄 Processing call {self.current_index + 1}/{self.total_records}: {current_record.name} "
                        f"(Row {current_record.row_number})"
                    )

                    # Set call in progress flag
                    self._call_in_progress = True

                    # Make the actual call
                    success = await self._make_actual_call(current_record)

                    if success:
                        logger.info(f"✅ Call initiated successfully for {current_record.name}")

                        # Wait for call to complete
                        call_timeout = 0
                        max_call_duration = 600  # 10 minutes max per call
                        check_interval = 5  # Check every 5 seconds

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

                        # Call completed or timed out
                        self._call_in_progress = False

                        # Handle timeout
                        if call_timeout >= max_call_duration and current_record.status == CallResult.CALLING:
                            logger.warning(f"⏰ Call timed out for {current_record.name}")
                            await self.mark_call_result(CallResult.CALL_FAILED, "Call timeout - exceeded maximum duration")
                            await self.move_to_next_record()

                        # Check if we should stop after this call
                        if self._stop_after_current_call:
                            logger.info(f"🛑 Stopping queue after completing call to {current_record.name}")
                            break

                        logger.info(f"✅ Call completed for {current_record.name}, status: {current_record.status.value}")

                    else:
                        # Call failed to initiate
                        logger.error(f"❌ Failed to initiate call for {current_record.name}")
                        await self.mark_call_result(CallResult.CALL_FAILED, "Failed to initiate call")
                        self._call_in_progress = False
                        await self.move_to_next_record()

                    # Brief pause between calls (only if not stopping)
                    if not self._should_stop and self.status == QueueStatus.RUNNING:
                        await asyncio.sleep(10)  # 10 second pause between calls

                else:
                    # Current record already processed, move to next
                    logger.info(f"📝 Current record already processed, moving to next")
                    await self.move_to_next_record()

            # Queue completed or stopped
            if self.current_index >= self.total_records and not self._should_stop:
                # Final check for new records if monitoring is active
                if google_sheets_service.monitoring_active:
                    new_records, _ = await google_sheets_service.check_for_new_records()
                    if new_records:
                        logger.info(f"🆕 Found {len(new_records)} new records at end of queue")
                        await self._handle_new_records(new_records)
                        # Continue processing if we got new records
                        if self.current_index < len(self.records):
                            logger.info("🔄 Continuing with new records...")
                            # Don't mark as completed, continue the loop
                            return

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
            # Clean up flags and stop monitoring
            self._call_in_progress = False
            self._stop_after_current_call = False
            await google_sheets_service.stop_monitoring()

    async def _make_actual_call(self, record):
        """Make the actual call via webhook to Plivo"""
        try:
            logger.info(f"📞 Initiating call to {record.name} ({record.phone}) from row {record.row_number}")

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
        """Mark current call as complete and move to next"""
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

            logger.info(f"✅ Call completed: {result.value} for {current_record.name} (Row {current_record.row_number})")

            # Clear call in progress flag
            self._call_in_progress = False

            # Check if we should stop before moving to next record
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

    async def skip_current_call(self) -> Dict:
        """Skip the current call and move to next"""
        if self.status != QueueStatus.RUNNING:
            return {"success": False, "error": "Queue is not running"}

        if self.current_index < len(self.records):
            current_record = self.records[self.current_index]
            current_record.status = CallResult.SKIPPED
            current_record.result_details = "Manually skipped"

            await self.move_to_next_record()
            logger.info(f"⏭️ Skipped call to {current_record.name} ({current_record.phone}) from row {current_record.row_number}")

            return {
                "success": True,
                "skipped_record": current_record.to_dict(),
                "next_index": self.current_index
            }

        return {"success": False, "error": "No current call to skip"}

    async def get_records_summary(self) -> Dict:
        """Get summary of all records and their statuses"""
        summary = {
            "total_records": len(self.records),
            "by_status": {},
            "recent_additions": [],
            "records": []
        }

        # Count by status
        for record in self.records:
            status = record.status.value
            summary["by_status"][status] = summary["by_status"].get(status, 0) + 1

        # Get recent additions (last 10 dynamically added)
        recent_additions = [r for r in self.records if r.created_at > self.sheet_connection_time][-10:]
        summary["recent_additions"] = [r.to_dict() for r in recent_additions]

        # Include all records (optional, for detailed view)
        summary["records"] = [r.to_dict() for r in self.records]

        return summary


# Global instance - replaces the old call_queue_manager
enhanced_call_queue_manager = EnhancedCallQueueManager()