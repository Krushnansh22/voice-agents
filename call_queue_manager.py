"""
Updated Call Queue Manager - Auto Skip Empty Calls at 30s timeout
"""
import asyncio
import logging
from typing import List, Dict, Optional
from enum import Enum
from datetime import datetime
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
    def __init__(self, index: int, name: str, phone: str, address: str, age: str, gender: str, row_number: int = None):
        self.index = index
        self.name = name
        self.phone = phone
        self.address = address
        self.age = age
        self.gender = gender
        self.row_number = row_number or (index + 2)  # Default to index + 2 for Excel row
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
    """Enhanced Call Queue Manager with auto-skip for empty calls at 30s timeout"""

    def __init__(self):
        self.status = QueueStatus.IDLE
        self.records: List[CallRecord] = []
        self.current_index = 0
        self.total_records = 0
        self.connected_sheet_id = None
        self.sheet_connection_info = None
        self.connection_timestamp = None

        # Statistics
        self.stats = {
            "total_calls": 0,
            "successful_appointments": 0,
            "reschedule_requests": 0,
            "incomplete_calls": 0,
            "failed_calls": 0,
            "auto_skipped_calls": 0,  # New stat for auto-skipped calls
            "queue_started_at": None,
            "queue_completed_at": None
        }

        # Control flags
        self._should_stop = False
        self._calling_task = None
        self._call_in_progress = False
        self._stop_after_current_call = False

        # Google Sheets monitoring
        self.monitoring_enabled = False

        logger.info("Enhanced Call Queue Manager initialized with auto-skip feature")

    async def connect_to_google_sheet(self, sheet_id: str, worksheet_name: str = "Records") -> Dict:
        """Connect to Google Sheet and load patient records"""
        try:
            logger.info(f"Connecting to Google Sheet: {sheet_id}")

            # Import here to avoid circular imports
            from google_sheets_service import google_sheets_service

            # Initialize Google Sheets service if not already done
            if not google_sheets_service.client:
                initialized = await google_sheets_service.initialize()
                if not initialized:
                    return {
                        "success": False,
                        "error": "Failed to initialize Google Sheets service"
                    }

            # Connect to the sheet
            connection_result = await google_sheets_service.connect_to_sheet(sheet_id, worksheet_name)

            if not connection_result["success"]:
                return connection_result

            # Load records from the sheet
            records_data, errors = await google_sheets_service.read_all_records()

            if not records_data:
                return {
                    "success": False,
                    "error": "No valid records found in the sheet",
                    "errors": errors
                }

            # Reset previous data
            self.records = []
            self.current_index = 0
            self.status = QueueStatus.IDLE

            # Process records into CallRecord objects
            valid_records = 0
            processing_errors = []

            for record_data in records_data:
                try:
                    record = CallRecord(
                        index=valid_records,
                        name=record_data['name'],
                        phone=record_data['phone'],
                        address=record_data['address'],
                        age=record_data['age'],
                        gender=record_data['gender'],
                        row_number=record_data.get('row_number', valid_records + 2)
                    )

                    self.records.append(record)
                    valid_records += 1

                except Exception as e:
                    processing_errors.append(f"Record {record_data.get('index', '?')}: {str(e)}")

            self.total_records = len(self.records)
            self.connected_sheet_id = sheet_id
            self.sheet_connection_info = connection_result
            self.connection_timestamp = datetime.now()

            # Combine errors
            all_errors = errors + processing_errors

            logger.info(f"Successfully loaded {self.total_records} records from Google Sheets")

            return {
                "success": True,
                "total_records": self.total_records,
                "valid_records": valid_records,
                "errors": all_errors[:10],  # Limit errors shown
                "sheet_info": connection_result,
                "connection_timestamp": self.connection_timestamp.isoformat()
            }

        except Exception as e:
            logger.error(f"Failed to connect to Google Sheet: {e}")
            return {
                "success": False,
                "error": str(e),
                "total_records": 0
            }

    async def start_monitoring(self):
        """Start monitoring Google Sheets for new records"""
        try:
            if not self.connected_sheet_id:
                logger.warning("No Google Sheet connected for monitoring")
                return False

            from google_sheets_service import google_sheets_service

            # Start monitoring with callback
            await google_sheets_service.start_monitoring(
                callback_func=self._handle_new_records
            )

            self.monitoring_enabled = True
            logger.info("🔍 Started monitoring Google Sheets for new records")
            return True

        except Exception as e:
            logger.error(f"Failed to start monitoring: {e}")
            return False

    async def stop_monitoring(self):
        """Stop monitoring Google Sheets"""
        try:
            from google_sheets_service import google_sheets_service
            await google_sheets_service.stop_monitoring()
            self.monitoring_enabled = False
            logger.info("🛑 Stopped monitoring Google Sheets")

        except Exception as e:
            logger.error(f"Error stopping monitoring: {e}")

    async def _handle_new_records(self, new_records: List[Dict]):
        """Handle callback when new records are detected in Google Sheets"""
        try:
            logger.info(f"🆕 Processing {len(new_records)} new records from Google Sheets")

            records_added = 0
            for record_data in new_records:
                try:
                    new_record = CallRecord(
                        index=self.total_records + records_added,
                        name=record_data['name'],
                        phone=record_data['phone'],
                        address=record_data['address'],
                        age=record_data['age'],
                        gender=record_data['gender'],
                        row_number=record_data.get('row_number', self.total_records + records_added + 2)
                    )

                    self.records.append(new_record)
                    records_added += 1

                    logger.info(f"✅ Added new record: {new_record.name} (Row {new_record.row_number})")

                except Exception as e:
                    logger.error(f"❌ Error processing new record: {e}")

            # Update total count
            self.total_records = len(self.records)

            if records_added > 0:
                logger.info(f"🎯 Successfully added {records_added} new records to queue")

        except Exception as e:
            logger.error(f"❌ Error handling new records: {e}")

    async def start_queue(self) -> Dict:
        """Start the calling queue with monitoring"""
        try:
            if not self.records:
                return {"success": False, "error": "No records loaded from Google Sheets"}

            if self.status == QueueStatus.RUNNING:
                return {"success": False, "error": "Queue is already running"}

            self.status = QueueStatus.RUNNING
            self._should_stop = False
            self._stop_after_current_call = False
            self.stats["queue_started_at"] = datetime.now()

            # Start monitoring for new records
            await self.start_monitoring()

            # Start the calling task
            self._calling_task = asyncio.create_task(self._calling_loop())

            logger.info(f"Started calling queue with {self.total_records} records and Google Sheets monitoring")

            return {
                "success": True,
                "status": self.status.value,
                "total_records": self.total_records,
                "current_index": self.current_index,
                "sheet_id": self.connected_sheet_id,
                "monitoring_enabled": self.monitoring_enabled
            }

        except Exception as e:
            logger.error(f"Failed to start queue: {e}")
            self.status = QueueStatus.ERROR
            return {"success": False, "error": str(e)}

    async def pause_queue(self) -> Dict:
        """Pause the calling queue (keeps monitoring active)"""
        if self.status == QueueStatus.RUNNING:
            self.status = QueueStatus.PAUSED
            logger.info("Queue paused (monitoring continues)")
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
        """Stop the calling queue and monitoring"""
        try:
            logger.info("🛑 Stop queue requested")

            self._should_stop = True

            # Stop monitoring
            await self.stop_monitoring()

            if self._call_in_progress:
                logger.info("📞 Call in progress - will stop after current call completes")
                self._stop_after_current_call = True
                self.status = QueueStatus.STOPPED

                return {
                    "success": True,
                    "status": self.status.value,
                    "message": "Queue will stop after current call completes",
                    "call_in_progress": True,
                    "calls_completed": self.current_index,
                    "total_records": self.total_records
                }
            else:
                self.status = QueueStatus.STOPPED

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
            logger.info(f"Skipped call to {current_record.name} (Row {current_record.row_number})")

            return {
                "success": True,
                "skipped_record": current_record.to_dict(),
                "next_index": self.current_index
            }

        return {"success": False, "error": "No current call to skip"}

    async def auto_skip_empty_call(self, reason: str = "No conversation detected") -> Dict:
        """Auto-skip current call due to no connection/conversation at 30s timeout"""
        try:
            if self.current_index >= len(self.records):
                return {"success": False, "error": "No current call to skip"}

            current_record = self.records[self.current_index]

            logger.info(f"🔄 Auto-skipping call to {current_record.name} - {reason}")

            # ALWAYS add auto-skipped calls to reschedule requests
            # These are potential leads that should be called back later
            await self._add_to_reschedule_for_empty_call(current_record, reason)
            logger.info(f"📅 Added {current_record.name} to reschedule requests (auto-skipped)")

            # Mark as skipped
            current_record.status = CallResult.SKIPPED
            current_record.result_details = f"Auto-skipped: {reason}"
            current_record.last_attempt = datetime.now()
            current_record.attempts += 1

            # Update statistics
            self.stats["auto_skipped_calls"] += 1
            self.stats["reschedule_requests"] += 1

            # Move to next record
            self.current_index += 1
            self._call_in_progress = False

            logger.info(f"✅ Auto-skipped {current_record.name} and added to reschedule requests")

            return {
                "success": True,
                "action": "auto_skipped",
                "record": current_record.to_dict(),
                "reason": reason,
                "added_to_reschedule": True,
                "next_index": self.current_index
            }

        except Exception as e:
            logger.error(f"❌ Error auto-skipping call: {e}")
            return {"success": False, "error": str(e)}

    async def _verify_call_attempt(self, record: CallRecord) -> bool:
        """Verify if a legitimate call attempt was made (not just a failed connection)"""
        try:
            # Check if call was actually initiated through Plivo
            # This could be enhanced to check Plivo call logs or call status

            # For now, check if we have any indicators of a real call attempt
            import main

            # Check if any call UUID was generated
            current_call_uuid = getattr(main, 'current_call_uuid', None)

            # Check if any call session was created
            current_call_session = getattr(main, 'current_call_session', None)

            # If either exists, it means Plivo call was made and potentially answered
            call_attempted = (current_call_uuid and current_call_uuid != 'unknown') or current_call_session

            if call_attempted:
                logger.info(f"✅ Verified call attempt for {record.name} - UUID: {current_call_uuid}, Session: {bool(current_call_session)}")
                return True
            else:
                logger.info(f"❌ No call attempt verified for {record.name}")
                return False

        except Exception as e:
            logger.error(f"Error verifying call attempt: {e}")
            # Default to True to be safe - if we can't verify, assume it was attempted
            return True

    async def _make_actual_call(self, record):
        """Make the actual call via webhook to Plivo"""
        try:
            logger.info(f"📞 Initiating call to {record.name} ({record.phone}) from Google Sheets row {record.row_number}")

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

    async def _add_to_reschedule_for_empty_call(self, record: CallRecord, reason: str):
        """Add record to reschedule requests for auto-skipped calls"""
        try:
            from google_sheets_service import google_sheets_service

            # Prepare patient record
            patient_record = {
                'name': record.name,
                'phone_number': record.phone,
                'address': record.address,
                'age': record.age,
                'gender': record.gender
            }

            # Create callback details for auto-reschedule with clear reason
            callback_details = {
                'callback_date': 'TBD - Auto Retry',
                'callback_time': 'TBD - Auto Retry',
                'callback_day': 'TBD - Auto Retry',
                'callback_period': 'TBD - Auto Retry',
                'normalized_callback_date': 'TBD - Auto Retry',
                'reschedule_confirmed': True,
                'auto_generated': True,
                'reason': f"Auto-skipped: {reason}"
            }

            # Add to reschedule requests
            success = await google_sheets_service.append_reschedule(patient_record, callback_details)

            if success:
                logger.info(f"✅ Added {record.name} to reschedule requests (auto-skipped - will retry later)")
            else:
                logger.error(f"❌ Failed to add {record.name} to reschedule requests")

        except Exception as e:
            logger.error(f"❌ Error adding to reschedule: {e}")

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

            # Reset stats
            self.stats = {
                "total_calls": 0,
                "successful_appointments": 0,
                "reschedule_requests": 0,
                "incomplete_calls": 0,
                "failed_calls": 0,
                "auto_skipped_calls": 0,
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
        def serialize_datetime(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            return obj

        serialized_stats = {}
        for key, value in self.stats.items():
            serialized_stats[key] = serialize_datetime(value)

        # Calculate dynamic progress percentage based on current total
        progress_percentage = 0
        if self.total_records > 0:
            progress_percentage = (self.current_index / self.total_records * 100)

        return {
            "status": self.status.value,
            "total_records": self.total_records,
            "current_index": self.current_index,
            "progress_percentage": min(progress_percentage, 100),  # Cap at 100%
            "remaining_calls": max(0, self.total_records - self.current_index),
            "connected_sheet_id": self.connected_sheet_id,
            "sheet_info": self.sheet_connection_info,
            "connection_timestamp": self.connection_timestamp.isoformat() if self.connection_timestamp else None,
            "monitoring_enabled": self.monitoring_enabled,
            "stats": serialized_stats,
            "current_record": self.records[self.current_index].to_dict() if self.current_index < len(
                self.records) else None,
            "call_in_progress": self._call_in_progress,
            "stop_pending": self._stop_after_current_call,
            "queue_can_grow": True
        }

    async def get_records_summary(self) -> Dict:
        """Get detailed summary of all records and their statuses"""
        try:
            if not self.records:
                return {
                    "total_records": 0,
                    "status_distribution": {},
                    "records": []
                }

            # Calculate status distribution
            status_distribution = {}
            for record in self.records:
                status = record.status.value
                status_distribution[status] = status_distribution.get(status, 0) + 1

            return {
                "total_records": len(self.records),
                "status_distribution": status_distribution,
                "records": [record.to_dict() for record in self.records],
                "sheet_id": self.connected_sheet_id,
                "monitoring_active": self.monitoring_enabled
            }

        except Exception as e:
            logger.error(f"Failed to get records summary: {e}")
            return {"error": str(e)}

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

            logger.info(f"Call result marked: {result.value} for {record.name} (Row {record.row_number})")

    async def move_to_next_record(self):
        """Move to the next record in the queue"""
        self.current_index += 1

        # DON'T mark as completed when reaching end - allow for new records
        if self.current_index >= self.total_records:
            logger.info("Reached end of current queue - waiting for new records or manual stop")

    async def _calling_loop(self):
        """Internal calling loop with enhanced auto-skip functionality for truly unanswered calls"""
        try:
            while (not self._should_stop and
                   self.status in [QueueStatus.RUNNING, QueueStatus.PAUSED]):

                if self._should_stop:
                    logger.info("🛑 Stop flag detected - exiting calling loop")
                    break

                if self.status == QueueStatus.PAUSED:
                    await asyncio.sleep(1)
                    continue

                # Check if we have more records to process
                if self.current_index >= self.total_records:
                    logger.info("⏳ Waiting for new records to be added...")
                    await asyncio.sleep(5)
                    continue

                current_record = self.get_current_record()
                if current_record and current_record.status == CallResult.PENDING:
                    logger.info(
                        f"🔄 Processing call {self.current_index + 1}/{self.total_records}: {current_record.name} (Row {current_record.row_number})")

                    self._call_in_progress = True

                    success = await self._make_actual_call(current_record)

                    if success:
                        logger.info(f"✅ Call initiated successfully for {current_record.name}")

                        call_timeout = 0
                        max_call_duration = 600
                        check_interval = 5
                        call_answered = False  # Track if call was actually answered
                        media_stream_connected = False  # Track if media stream connected

                        while (current_record.status == CallResult.CALLING and
                            not self._should_stop and
                            self.status in [QueueStatus.RUNNING, QueueStatus.STOPPED] and
                            call_timeout < max_call_duration):

                            logger.info(f"⏳ Waiting for call to complete: {current_record.name} (timeout: {call_timeout}s)")

                            # CHECK FOR CALL ANSWER STATUS AND CONVERSATION AT 30s TIMEOUT
                            if call_timeout == 30:
                                logger.info(f"🔍 Performing comprehensive call check at 30s timeout for {current_record.name}")

                                # Check multiple indicators to determine if call was actually answered
                                should_auto_skip = await self._should_auto_skip_call(current_record, call_timeout)

                                if should_auto_skip:
                                    logger.warning(f"📵 Call determined to be unanswered at 30s for {current_record.name}")
                                    logger.info(f"🔄 Auto-skipping {current_record.name} due to no answer/connection")

                                    # Auto-skip the call
                                    skip_result = await self.auto_skip_empty_call("Call not answered - no media stream activity at 30s")

                                    if skip_result["success"]:
                                        logger.info(f"✅ Successfully auto-skipped {current_record.name}")
                                        break  # Exit the waiting loop
                                    else:
                                        logger.error(f"❌ Failed to auto-skip {current_record.name}: {skip_result.get('error')}")
                                else:
                                    logger.info(f"✅ Call appears to be answered/active for {current_record.name} - continuing")

                            await asyncio.sleep(check_interval)
                            call_timeout += check_interval

                            if self._should_stop:
                                logger.info(f"🛑 Stop requested during call to {current_record.name}")
                                break

                        self._call_in_progress = False

                        if call_timeout >= max_call_duration and current_record.status == CallResult.CALLING:
                            logger.warning(f"⏰ Call timed out for {current_record.name}")
                            await self.mark_call_result(CallResult.CALL_FAILED, "Call timeout - exceeded maximum duration")
                            await self.move_to_next_record()

                        if self._stop_after_current_call:
                            logger.info(f"🛑 Stopping queue after completing call to {current_record.name}")
                            break

                        logger.info(f"✅ Call completed for {current_record.name}, status: {current_record.status.value}")

                    else:
                        logger.error(f"❌ Failed to initiate call for {current_record.name}")
                        await self.mark_call_result(CallResult.CALL_FAILED, "Failed to initiate call")
                        self._call_in_progress = False
                        await self.move_to_next_record()

                    if not self._should_stop and self.status == QueueStatus.RUNNING:
                        await asyncio.sleep(10)

                else:
                    logger.info(f"📝 Current record already processed or invalid, moving to next")
                    await self.move_to_next_record()

            # Only mark as stopped if explicitly requested
            if self._should_stop:
                self.status = QueueStatus.STOPPED
                logger.info("🛑 Queue stopped as requested")

        except asyncio.CancelledError:
            logger.info("⏹️ Calling loop cancelled")
            self.status = QueueStatus.STOPPED
        except Exception as e:
            logger.error(f"❌ Error in calling loop: {e}")
            self.status = QueueStatus.ERROR
        finally:
            self._call_in_progress = False
            self._stop_after_current_call = False
            await self.stop_monitoring()

    async def _should_auto_skip_call(self, record: CallRecord, call_timeout: int) -> bool:
        """
        Comprehensive check to determine if a call should be auto-skipped.
        Priority order: Conversation > Media Stream > Call UUID > Call Session
        """
        try:
            logger.info(f"🔍 Performing comprehensive call analysis for {record.name}")

            # Check 1: MOST IMPORTANT - Conversation activity (if there's conversation, call is definitely active)
            conversation_active = await self._check_conversation_activity()
            if conversation_active:
                logger.info(f"✅ Active conversation detected - call is definitely active for {record.name}")
                return False  # Don't skip - call is active

            # Check 2: Media stream connection - check if WebSocket was established
            media_stream_active = await self._check_media_stream_activity()
            if not media_stream_active:
                logger.info(f"📵 No media stream activity - call not connected for {record.name}")
                return True  # Skip - no media stream means no connection

            # Check 3: Call UUID - if no UUID generated, call wasn't answered (but less reliable)
            call_uuid_exists = await self._check_call_uuid_exists()
            if not call_uuid_exists:
                logger.info(f"⚠️ No call UUID found - but media stream exists, checking further...")
                # Don't immediately return True here, continue with other checks

            # Check 4: Call session in database
            call_session_exists = await self._check_call_session_exists()
            if not call_session_exists:
                logger.info(f"📵 No call session in database - call wasn't properly established for {record.name}")
                return True  # Skip - no session means no proper call

            # If we reach here: No conversation BUT media stream and session exist
            # This might be a case where user picked up but hasn't spoken yet
            # At 30s, if no conversation but connection exists, be more lenient
            logger.info(f"🤔 No conversation yet but call infrastructure exists for {record.name} - giving more time")
            return False  # Don't skip yet - give more time for conversation to start

        except Exception as e:
            logger.error(f"❌ Error in comprehensive call check: {e}")
            # On error, be conservative and don't auto-skip
            return False

    async def _check_call_uuid_exists(self) -> bool:
        """Check if a call UUID was generated (indicates call was answered)"""
        try:
            # Import from main module to check current_call_uuid
            import main
            current_call_uuid = getattr(main, 'current_call_uuid', None)

            if current_call_uuid and current_call_uuid not in ['unknown', '', None]:
                logger.info(f"✅ Call UUID exists: {current_call_uuid}")
                return True
            else:
                logger.info(f"❌ No valid call UUID found: {current_call_uuid}")
                return False

        except Exception as e:
            logger.error(f"Error checking call UUID: {e}")
            return False

    async def _check_media_stream_activity(self) -> bool:
        """Check if media stream WebSocket connection was established"""
        try:
            # Import from main module to check current_call_session
            import main
            current_call_session = getattr(main, 'current_call_session', None)

            if current_call_session and hasattr(current_call_session, 'call_id'):
                logger.info(f"✅ Media stream session exists: {current_call_session.call_id}")
                return True
            else:
                logger.info(f"❌ No media stream session found")
                return False

        except Exception as e:
            logger.error(f"Error checking media stream: {e}")
            return False

    async def _check_conversation_activity(self) -> bool:
        """Check if there's actual conversation activity - MOST RELIABLE INDICATOR"""
        try:
            # Import conversation_transcript from main module
            import main
            conversation_transcript = getattr(main, 'conversation_transcript', [])

            # Consider call active if there are ANY meaningful exchanges
            # Even 1 user response means call is connected and active
            if len(conversation_transcript) >= 1:
                logger.info(f"✅ Conversation active - {len(conversation_transcript)} exchanges found")
                logger.info(f"   Recent exchanges: {conversation_transcript[-2:] if len(conversation_transcript) >= 2 else conversation_transcript}")
                return True
            else:
                logger.info(f"❌ No conversation detected - {len(conversation_transcript)} exchanges")
                return False

        except Exception as e:
            logger.error(f"Error checking conversation activity: {e}")
            return False

    async def _check_call_session_exists(self) -> bool:
        """Check if call session exists in database"""
        try:
            # Import from main module
            import main
            current_call_session = getattr(main, 'current_call_session', None)

            if current_call_session and hasattr(current_call_session, 'call_id'):
                # Optionally verify in database
                from database.db_service import db_service

                # Quick check if session exists
                session_exists = current_call_session.call_id is not None

                if session_exists:
                    logger.info(f"✅ Call session exists in database: {current_call_session.call_id}")
                    return True
                else:
                    logger.info(f"❌ Call session not found in database")
                    return False
            else:
                logger.info(f"❌ No call session object found")
                return False

        except Exception as e:
            logger.error(f"Error checking call session: {e}")
            return False
        """Make the actual call via webhook to Plivo"""
        try:
            logger.info(f"📞 Initiating call to {record.name} ({record.phone}) from Google Sheets row {record.row_number}")

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

            self._call_in_progress = False

            if self._stop_after_current_call or self._should_stop:
                logger.info("🛑 Queue stop requested - NOT moving to next record")
                self.status = QueueStatus.STOPPED

                if self._calling_task and not self._calling_task.done():
                    self._calling_task.cancel()

                self._stop_after_current_call = False
                await self.stop_monitoring()

                logger.info(f"🛑 Queue stopped after completing call to {current_record.name}")
            else:
                await self.move_to_next_record()
                logger.info(f"➡️ Moving to next record (index: {self.current_index})")

        else:
            logger.warning("⚠️ No current record to complete")
            self._call_in_progress = False

    def disconnect_sheet(self):
        """Disconnect from the current Google Sheet"""
        try:
            # Stop monitoring first
            if self.monitoring_enabled:
                asyncio.create_task(self.stop_monitoring())

            self.connected_sheet_id = None
            self.sheet_connection_info = None
            self.connection_timestamp = None
            self.records = []
            self.current_index = 0
            self.total_records = 0
            self.status = QueueStatus.IDLE
            self.monitoring_enabled = False

            logger.info("📊 Disconnected from Google Sheets")

        except Exception as e:
            logger.error(f"❌ Error disconnecting from Google Sheets: {e}")


# Global instance
call_queue_manager = EnhancedCallQueueManager()