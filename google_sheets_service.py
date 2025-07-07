"""
Updated Google Sheets Service with Date-Time Standardization
All dates and times are now standardized to YYYY-MM-DD HH:MM AM/PM format
Updated to use environment variables for credentials
"""
import asyncio
import logging
from typing import List, Dict, Optional, Tuple
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

# Import the datetime standardizer
from datetime_standardizer import datetime_standardizer, get_current_timestamp_standard

logger = logging.getLogger(__name__)


class GoogleSheetsService:
    """Enhanced Google Sheets service with standardized date-time formats"""

    def __init__(self, credentials_dict: dict = None):
        self.credentials_dict = credentials_dict
        self.client = None
        self.current_spreadsheet = None
        self.current_sheet = None
        self.sheet_id = None
        self.executor = ThreadPoolExecutor(max_workers=4)

        # Real-time monitoring
        self.monitoring_active = False
        self.new_records_callback = None
        self.drive_monitoring_enabled = False

        # Cache for efficiency
        self.last_row_count = 0
        self.last_known_data = []

        # Worksheet mappings
        self.worksheets = {
            'records': 'Records',
            'appointments': 'Appointment_Details',
            'reschedules': 'Reschedule_Requests',
            'incomplete': 'Incomplete_Calls',
            'analysis': 'Call_Analysis'
        }

        # UPDATED Headers with standardized date-time column names
        self.headers = {
            'records': ['Name', 'Phone Number', 'Address', 'Age', 'Gender'],
            'appointments': [
                'Name', 'Appointment Date', 'Appointment Time', 'Doctor Name',
                'Age', 'Gender', 'Phone Number', 'Address', 'Timestamp', 'Summary'
            ],
            'reschedules': [
                'Name', 'Phone Number', 'Address', 'Age', 'Gender',
                'Call Timestamp', 'Preferred Callback Date', 'Preferred Callback Time',
                'Preferred Callback Day', 'Preferred Callback Period', 'Status', 'Priority'
            ],
            'incomplete': [
                'Name', 'Phone Number', 'Address', 'Age', 'Gender',
                'Call Timestamp', 'Call Duration (seconds)', 'Reason', 'Notes', 'Customer Intent'
            ],
            'analysis': [
                'Call ID', 'Patient Name', 'Phone Number', 'Call Date',
                'Call Duration', 'Call Outcome', 'AI Summary', 'Transcript Count',
                'Outcome Details', 'Analysis Timestamp'
            ]
        }

    async def initialize(self) -> bool:
        """Initialize Google Sheets client"""
        try:
            scopes = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"
            ]

            if self.credentials_dict:
                # Use credentials from environment variables
                creds = Credentials.from_service_account_info(
                    self.credentials_dict,
                    scopes=scopes
                )
                logger.info("✅ Using credentials from environment variables")
            else:
                # Fallback to file-based credentials
                # from settings import settings
                # credentials_file = settings.GOOGLE_SERVICE_ACCOUNT_FILE
                # creds = Credentials.from_service_account_file(
                #     credentials_file,
                #     scopes=scopes
                # )
                logger.info(f"creds issue")

            self.client = await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: gspread.authorize(creds)
            )

            logger.info("✅ Google Sheets service initialized successfully")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to initialize Google Sheets service: {e}")
            return False

    async def connect_to_sheet(self, sheet_id: str, worksheet_name: str = "Records") -> Dict:
        """Connect to Google Sheet and setup monitoring"""
        try:
            logger.info(f"🔗 Connecting to Google Sheet: {sheet_id}")

            # Open spreadsheet by ID
            self.current_spreadsheet = await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: self.client.open_by_key(sheet_id)
            )

            # Connect to the main records worksheet
            self.current_sheet = await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: self.current_spreadsheet.worksheet(worksheet_name)
            )

            self.sheet_id = sheet_id

            # Get initial data and row count
            all_values = await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: self.current_sheet.get_all_values()
            )
            self.last_row_count = len(all_values)
            self.last_known_data = all_values

            # Validate main sheet structure
            validation_result = await self._validate_sheet_structure()
            if not validation_result["valid"]:
                return {
                    "success": False,
                    "error": f"Invalid sheet structure: {validation_result['error']}"
                }

            # Setup result worksheets
            await self._setup_result_worksheets()

            # Setup Drive API monitoring
            drive_setup_success = await self._setup_drive_monitoring()

            logger.info(f"✅ Connected to sheet with {self.last_row_count} rows")
            if drive_setup_success:
                logger.info("🔔 Real-time monitoring enabled via Google Drive API")
            else:
                logger.warning("⚠️ Real-time monitoring not available - continuing without it")

            return {
                "success": True,
                "sheet_id": sheet_id,
                "worksheet_name": self.current_sheet.title,
                "total_rows": self.last_row_count,
                "data_rows": max(0, self.last_row_count - 1),
                "monitoring_enabled": self.drive_monitoring_enabled
            }

        except Exception as e:
            logger.error(f"❌ Failed to connect to sheet: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def _setup_drive_monitoring(self) -> bool:
        """Setup Google Drive API monitoring for real-time updates"""
        try:
            from drive_api_integration import drive_notification_service
            from settings import settings

            # Initialize Drive notification service
            webhook_url = f"{settings.HTTPS_HOST_URL}/api/drive-webhook"

            if not drive_notification_service.drive_service:
                initialized = await drive_notification_service.initialize(webhook_url)
                if not initialized:
                    logger.warning("⚠️ Could not initialize Drive notifications")
                    return False

            # Setup file monitoring
            result = await drive_notification_service.setup_file_monitoring(
                self.sheet_id,
                callback=self._handle_drive_notification
            )

            if result["success"]:
                self.drive_monitoring_enabled = True
                logger.info("✅ Real-time Drive API monitoring enabled")
                return True
            else:
                logger.warning(f"⚠️ Could not setup Drive monitoring: {result.get('error')}")
                return False

        except Exception as e:
            logger.warning(f"⚠️ Could not setup Drive monitoring: {e}")
            return False

    async def _handle_drive_notification(self, file_id: str, resource_state: str):
        """Handle notification from Google Drive API"""
        try:
            logger.info(f"📡 Drive notification: {file_id} - {resource_state}")

            if file_id != self.sheet_id:
                logger.warning(f"⚠️ Received notification for different file: {file_id}")
                return

            if resource_state in ['update', 'change']:
                # Wait a moment for changes to propagate
                await asyncio.sleep(2)

                # Check for new records
                new_records = await self._check_for_real_changes()

                if new_records and self.new_records_callback:
                    logger.info(f"🆕 Found {len(new_records)} new records via Drive notification")
                    await self.new_records_callback(new_records)

        except Exception as e:
            logger.error(f"❌ Error handling Drive notification: {e}")

    async def _check_for_real_changes(self) -> List[Dict]:
        """Check for actual new records (called by Drive notification)"""
        try:
            # Get current data
            current_values = await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: self.current_sheet.get_all_values()
            )

            current_row_count = len(current_values)

            # Check if new rows were added
            if current_row_count > self.last_row_count:
                new_row_count = current_row_count - self.last_row_count
                logger.info(f"🆕 Detected {new_row_count} new rows via Drive API")

                new_records = []
                header_row = current_values[0] if current_values else []

                # Process new rows
                for row_num in range(self.last_row_count, current_row_count):
                    try:
                        row_values = current_values[row_num]

                        # Skip empty rows
                        if not any(row_values):
                            continue

                        # Create record dict
                        record_dict = {}
                        for i, header in enumerate(header_row):
                            value = row_values[i] if i < len(row_values) else ''
                            record_dict[header] = value

                        # Clean and validate
                        phone = str(record_dict.get('Phone Number', '')).strip()
                        name = str(record_dict.get('Name', '')).strip()

                        if phone and name and phone.lower() not in ['', 'nan', 'none']:
                            clean_record = {
                                'name': name,
                                'phone': phone,
                                'address': str(record_dict.get('Address', '')).strip(),
                                'age': str(record_dict.get('Age', '')).strip(),
                                'gender': str(record_dict.get('Gender', '')).strip(),
                                'row_number': row_num + 1
                            }
                            new_records.append(clean_record)

                    except Exception as e:
                        logger.warning(f"⚠️ Error processing new row {row_num + 1}: {e}")

                # Update cache
                self.last_row_count = current_row_count
                self.last_known_data = current_values

                return new_records

            else:
                # No new rows, but might be edits - you can implement edit detection here
                logger.info("📝 Sheet changed but no new rows detected")
                return []

        except Exception as e:
            logger.error(f"❌ Error checking for real changes: {e}")
            return []

    async def _validate_sheet_structure(self) -> Dict:
        """Validate that the main sheet has required columns"""
        try:
            header_row = await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: self.current_sheet.row_values(1)
            )

            required_columns = self.headers['records']
            header_lower = [col.lower().strip() for col in header_row]

            missing_columns = []
            for required_col in required_columns:
                if required_col.lower() not in header_lower:
                    missing_columns.append(required_col)

            if missing_columns:
                return {
                    "valid": False,
                    "error": f"Missing required columns: {missing_columns}"
                }

            return {
                "valid": True,
                "headers": header_row
            }

        except Exception as e:
            return {
                "valid": False,
                "error": f"Error validating sheet: {e}"
            }

    async def _setup_result_worksheets(self):
        """Setup or create result worksheets for appointments, reschedules, incomplete calls, and call analysis"""
        try:
            for key, worksheet_name in self.worksheets.items():
                if key == 'records':  # Skip the main records sheet
                    continue

                try:
                    # Try to get existing worksheet
                    await asyncio.get_event_loop().run_in_executor(
                        self.executor, lambda name=worksheet_name: self.current_spreadsheet.worksheet(name)
                    )
                    logger.info(f"📊 Found existing worksheet: {worksheet_name}")

                except gspread.WorksheetNotFound:
                    # Create new worksheet
                    worksheet = await asyncio.get_event_loop().run_in_executor(
                        self.executor, lambda name=worksheet_name, headers=self.headers[key]: self.current_spreadsheet.add_worksheet(
                            title=name,
                            rows=1000,
                            cols=len(headers)
                        )
                    )

                    # Add headers
                    await asyncio.get_event_loop().run_in_executor(
                        self.executor, lambda w=worksheet, h=self.headers[key]: w.append_row(h)
                    )
                    logger.info(f"✅ Created new worksheet: {worksheet_name}")

        except Exception as e:
            logger.error(f"❌ Failed to setup result worksheets: {e}")
            raise

    async def read_all_records(self) -> Tuple[List[Dict], List[str]]:
        """Read all patient records from the main sheet"""
        try:
            if not self.current_sheet:
                raise ValueError("No sheet connected")

            records = await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: self.current_sheet.get_all_records()
            )

            valid_records = []
            errors = []

            for i, record in enumerate(records):
                try:
                    # Clean and validate phone number
                    phone = str(record.get('Phone Number', '')).strip()
                    if not phone or phone.lower() in ['', 'nan', 'none']:
                        errors.append(f"Row {i + 2}: Missing or invalid phone number")
                        continue

                    # Create clean record
                    clean_record = {
                        'index': len(valid_records),
                        'name': str(record.get('Name', '')).strip(),
                        'phone': phone,
                        'address': str(record.get('Address', '')).strip(),
                        'age': str(record.get('Age', '')).strip(),
                        'gender': str(record.get('Gender', '')).strip(),
                        'row_number': i + 2  # Excel row number for reference
                    }

                    # Validate required fields
                    if not clean_record['name']:
                        errors.append(f"Row {i + 2}: Missing name")
                        continue

                    valid_records.append(clean_record)

                except Exception as e:
                    errors.append(f"Row {i + 2}: Error processing record - {str(e)}")

            logger.info(f"📊 Read {len(valid_records)} valid records from sheet")
            if errors:
                logger.warning(f"⚠️ {len(errors)} records had errors")

            return valid_records, errors

        except Exception as e:
            logger.error(f"❌ Failed to read records: {e}")
            return [], [f"Failed to read sheet: {str(e)}"]

    async def start_monitoring(self, callback_func=None):
        """Start monitoring the sheet for new records"""
        try:
            self.monitoring_active = True
            self.new_records_callback = callback_func

            if self.drive_monitoring_enabled:
                logger.info("🔍 Real-time monitoring active via Google Drive API")
            else:
                logger.info("🔍 Real-time monitoring not available - Drive API setup failed")

        except Exception as e:
            logger.error(f"Failed to start monitoring: {e}")

    async def stop_monitoring(self):
        """Stop monitoring the sheet"""
        try:
            self.monitoring_active = False

            if self.drive_monitoring_enabled:
                from drive_api_integration import drive_notification_service
                await drive_notification_service.stop_all_monitoring()
                self.drive_monitoring_enabled = False

            logger.info("🛑 Stopped monitoring sheet")

        except Exception as e:
            logger.error(f"Error stopping monitoring: {e}")

    # UPDATED: Append methods with date-time standardization
    async def append_appointment(self, appointment_details: Dict, patient_record: Dict, ai_summary: str = "") -> bool:
        """Append successful appointment to Appointment_Details worksheet with combined date-time in date column"""
        try:
            logger.info(f"📝 Saving appointment for {patient_record.get('name', 'Unknown')}")

            worksheet = await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: self.current_spreadsheet.worksheet(self.worksheets['appointments'])
            )

            # STANDARDIZE appointment details using datetime_standardizer
            from datetime_standardizer import datetime_standardizer, get_current_timestamp_standard

            standardized_details = datetime_standardizer.standardize_appointment_data(appointment_details)

            # COMBINE date and time into single field for "Appointment Date" column
            appointment_date = standardized_details.get('appointment_date', '')
            appointment_time = standardized_details.get('appointment_time', '')

            # Create combined date-time for the "Appointment Date" column
            if appointment_date and appointment_time:
                combined_appointment_datetime = f"{appointment_date} {appointment_time}"
            elif appointment_date:
                combined_appointment_datetime = appointment_date  # Just date if no time
            else:
                combined_appointment_datetime = ""  # Empty if no date

            # Keep original time for "Time Slot" column (for backward compatibility/reference)
            time_slot_value = standardized_details.get('appointment_time', '') or standardized_details.get('time_slot', '')

            # Get standardized timestamp
            standard_timestamp = get_current_timestamp_standard()

            # Row data with COMBINED date-time in "Appointment Date" column
            # Columns: Name, Appointment Date, Time Slot, Doctor Name, Age, Gender, Phone Number, Address, Timestamp, Summary
            row_data = [
                patient_record.get('name', ''),  # Name
                combined_appointment_datetime,  # Appointment Date (NOW CONTAINS: YYYY-MM-DD HH:MM AM/PM)
                time_slot_value,  # Time Slot (UNCHANGED: Original time reference)
                standardized_details.get('doctor_name', 'डॉ. निशा'),  # Doctor Name
                patient_record.get('age', ''),  # Age
                patient_record.get('gender', ''),  # Gender
                patient_record.get('phone_number', ''),  # Phone Number
                patient_record.get('address', ''),  # Address
                standard_timestamp,  # Timestamp (YYYY-MM-DD HH:MM AM/PM)
                ai_summary or "Appointment booked successfully"  # Summary
            ]

            await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: worksheet.append_row(row_data)
            )

            logger.info(f"✅ Appointment saved with combined date-time in Appointment Date column")
            logger.info(f"   Appointment Date: '{combined_appointment_datetime}' (Combined format)")
            logger.info(f"   Time Slot: '{time_slot_value}' (Reference)")
            logger.info(f"   Timestamp: '{standard_timestamp}'")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to save appointment: {e}")
            return False

    async def append_reschedule(self, patient_record: Dict, callback_details: Dict = None) -> bool:
        """Append reschedule request to Reschedule_Requests worksheet with standardized date-time"""
        try:
            logger.info(f"📅 Saving reschedule request for {patient_record.get('name', 'Unknown')}")

            worksheet = await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: self.current_spreadsheet.worksheet(self.worksheets['reschedules'])
            )

            # STANDARDIZE callback details using datetime_standardizer
            if callback_details:
                standardized_callback = datetime_standardizer.standardize_reschedule_data(callback_details)
            else:
                standardized_callback = {}

            # Get standardized timestamp
            standard_timestamp = get_current_timestamp_standard()

            # Process callback details with standardization
            callback_date = standardized_callback.get('normalized_callback_date') or standardized_callback.get('callback_date', "")
            callback_time = standardized_callback.get('callback_time', "")

            # Standardize the callback time if it exists
            if callback_time:
                callback_time = datetime_standardizer.standardize_time(callback_time)

            callback_day = standardized_callback.get('callback_day', "")
            callback_period = standardized_callback.get('callback_period', "")

            # Enhanced priority calculation
            priority = self._calculate_reschedule_priority(standardized_callback)

            # Prepare row data with standardized formats
            row_data = [
                patient_record.get('name', ''),                     # Name
                patient_record.get('phone_number', ''),             # Phone Number
                patient_record.get('address', ''),                  # Address
                patient_record.get('age', ''),                     # Age
                patient_record.get('gender', ''),                  # Gender
                standard_timestamp,                                 # Call Timestamp (YYYY-MM-DD HH:MM AM/PM)
                callback_date,                                      # Preferred Callback Date (YYYY-MM-DD)
                callback_time,                                      # Preferred Callback Time (HH:MM AM/PM)
                callback_day,                                       # Preferred Callback Day
                callback_period,                                    # Preferred Callback Period
                "Reschedule Requested",                             # Status
                priority                                            # Priority
            ]

            await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: worksheet.append_row(row_data)
            )

            logger.info(f"✅ Reschedule request saved with standardized date-time format")
            logger.info(f"   Call Timestamp: {standard_timestamp}")
            logger.info(f"   Callback Date: {callback_date}")
            logger.info(f"   Callback Time: {callback_time}")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to save reschedule request: {e}")
            return False

    def _calculate_reschedule_priority(self, callback_details: Dict) -> str:
        """Calculate priority based on callback details"""
        try:
            from datetime import datetime, timedelta

            callback_date = callback_details.get('normalized_callback_date') or callback_details.get('callback_date', '')
            callback_time = callback_details.get('callback_time', '')
            callback_day = callback_details.get('callback_day', '')

            # High priority for urgent/immediate requests
            if any(keyword in callback_date.lower() for keyword in ['आज', 'today', 'कल', 'tomorrow']):
                return "High"

            # High priority for specific date within next 3 days
            if callback_date and callback_date != 'TBD':
                try:
                    # Try to parse YYYY-MM-DD format (now standardized)
                    if '-' in callback_date and len(callback_date.split('-')) == 3:
                        parts = callback_date.split('-')
                        if len(parts[0]) == 4:  # YYYY-MM-DD format
                            callback_datetime = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
                        else:  # DD-MM-YYYY format (fallback)
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
            logger.warning(f"⚠️ Error calculating priority: {e}")
            return "Normal"

    async def append_incomplete_call(self, patient_record: Dict, reason: str = "call_incomplete",
                                     call_duration: int = 0, customer_intent_summary: str = "",
                                     ai_summary: str = "") -> bool:
        """Append incomplete call to Incomplete_Calls worksheet with standardized timestamp"""
        try:
            logger.info(f"📞 Saving incomplete call for {patient_record.get('name', 'Unknown')}")

            worksheet = await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: self.current_spreadsheet.worksheet(self.worksheets['incomplete'])
            )

            reason_notes = {
                "call_timeout": "Call exceeded time limit",
                "call_incomplete": "Call ended without clear resolution",
                "minimal_interaction": "Very few exchanges in conversation",
                "goodbye_detected": "Call ended with natural goodbye",
                "Not Picked-up": "Call was not answered by the patient",
                "connection_lost": "Connection was lost unexpectedly"
            }

            # Use AI summary if provided, otherwise use reason notes
            notes_field = ai_summary if ai_summary else reason_notes.get(reason, "Call incomplete")

            # Map customer intent to proper format
            intent_mapping = {
                "interested": "Interested",
                "not_interested": "Not Interested",
                "neutral": "Neutral"
            }
            formatted_intent = intent_mapping.get(customer_intent_summary.lower(), customer_intent_summary)

            # Get standardized timestamp
            standard_timestamp = get_current_timestamp_standard()

            # Row data with standardized timestamp format
            # Name, Phone Number, Address, Age, Gender, Call Timestamp, Call Duration (seconds), Reason, Notes, Customer Intent
            row_data = [
                patient_record.get('name', ''),  # Name
                patient_record.get('phone_number', ''),  # Phone Number
                patient_record.get('address', ''),  # Address
                patient_record.get('age', ''),  # Age
                patient_record.get('gender', ''),  # Gender
                standard_timestamp,  # Call Timestamp (YYYY-MM-DD HH:MM AM/PM)
                call_duration,  # Call Duration (seconds)
                reason,  # Reason
                notes_field,  # Notes (AI Summary or default)
                formatted_intent or "Neutral"  # Customer Intent Summary
            ]

            await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: worksheet.append_row(row_data)
            )

            logger.info(f"✅ Incomplete call saved with standardized timestamp: {standard_timestamp}")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to save incomplete call: {e}")
            return False

    async def append_call_analysis(self, analysis_data: Dict) -> bool:
        """Append call analysis to Call_Analysis worksheet with standardized date-time"""
        try:
            logger.info(f"🔍 Saving call analysis for {analysis_data.get('name', 'Unknown')}")

            # Get or create Call_Analysis worksheet
            try:
                worksheet = await asyncio.get_event_loop().run_in_executor(
                    self.executor, lambda: self.current_spreadsheet.worksheet(self.worksheets['analysis'])
                )
            except gspread.WorksheetNotFound:
                # Create the worksheet if it doesn't exist
                worksheet = await asyncio.get_event_loop().run_in_executor(
                    self.executor, lambda: self.current_spreadsheet.add_worksheet(
                        title=self.worksheets['analysis'],
                        rows=1000,
                        cols=len(self.headers['analysis'])
                    )
                )

                # Add headers
                await asyncio.get_event_loop().run_in_executor(
                    self.executor, lambda: worksheet.append_row(self.headers['analysis'])
                )
                logger.info(f"✅ Created Call_Analysis worksheet")

            # Standardize the call date if it's provided
            call_date = analysis_data.get('date', '')
            if call_date:
                # Extract just the date part (YYYY-MM-DD) from full timestamp
                if ' ' in call_date:
                    call_date = call_date.split(' ')[0]
                call_date = datetime_standardizer.standardize_date(call_date)

            # Get standardized timestamp for analysis
            standard_timestamp = get_current_timestamp_standard()

            # Prepare row data with standardized date-time formats
            row_data = [
                analysis_data.get('call_id', ''),                        # Call ID
                analysis_data.get('name', ''),                           # Patient Name
                analysis_data.get('phone_number', ''),                   # Phone Number
                call_date,                                               # Call Date (YYYY-MM-DD)
                analysis_data.get('duration', ''),                       # Call Duration
                analysis_data.get('call_outcome', ''),                   # Call Outcome
                analysis_data.get('summary', ''),                        # AI Summary
                analysis_data.get('transcript_count', 0),                # Transcript Count
                analysis_data.get('outcome_details', ''),                # Outcome Details
                standard_timestamp                                       # Analysis Timestamp (YYYY-MM-DD HH:MM AM/PM)
            ]

            await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: worksheet.append_row(row_data)
            )

            logger.info(f"✅ Call analysis saved with standardized date-time format")
            logger.info(f"   Call Date: {call_date}")
            logger.info(f"   Analysis Timestamp: {standard_timestamp}")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to save call analysis: {e}")
            return False

    async def get_call_analysis_data(self) -> List[Dict]:
        """Get all call analysis data from Call_Analysis worksheet"""
        try:
            worksheet = await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: self.current_spreadsheet.worksheet(self.worksheets['analysis'])
            )

            records = await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: worksheet.get_all_records()
            )

            return records

        except gspread.WorksheetNotFound:
            logger.warning("Call_Analysis worksheet not found")
            return []
        except Exception as e:
            logger.error(f"Error getting call analysis data: {e}")
            return []

    def get_status(self) -> Dict:
        """Get current service status"""
        return {
            "connected": self.current_sheet is not None,
            "sheet_id": self.sheet_id,
            "monitoring_active": self.monitoring_active,
            "drive_monitoring_enabled": self.drive_monitoring_enabled,
            "last_row_count": self.last_row_count,
            "worksheet_name": self.current_sheet.title if self.current_sheet else None,
            "spreadsheet_title": self.current_spreadsheet.title if self.current_spreadsheet else None,
            "available_worksheets": list(self.worksheets.values()),
            "datetime_format": "YYYY-MM-DD HH:MM AM/PM",  # NEW: Indicate standardized format
            "credentials_source": "environment_variables" if self.credentials_dict else "file"
        }

    def __del__(self):
        """Cleanup on destruction"""
        if hasattr(self, 'executor'):
            self.executor.shutdown(wait=False)


# Initialize global instance with credentials from settings
def create_google_sheets_service():
    """Factory function to create Google Sheets service with proper credentials"""
    try:
        from settings import settings
        credentials_dict = settings.get_google_credentials_dict()
        return GoogleSheetsService(credentials_dict=credentials_dict)
    except Exception as e:
        logger.warning(f"⚠️ Could not load credentials from environment, falling back to file: {e}")
        return GoogleSheetsService()

# Global instance
google_sheets_service = create_google_sheets_service()