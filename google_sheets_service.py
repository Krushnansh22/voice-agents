"""
Streamlined Google Sheets Service for Call Center Integration
Combines reading patient records and writing results to multiple worksheets
"""
import asyncio
import logging
from typing import List, Dict, Optional, Tuple
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


class GoogleSheetsService:
    """Enhanced Google Sheets service with monitoring and multi-worksheet support"""

    def __init__(self, credentials_file: str = "credentials.json"):
        self.credentials_file = credentials_file
        self.client = None
        self.current_spreadsheet = None
        self.current_sheet = None
        self.last_row_count = 0
        self.sheet_id = None
        self.monitoring_active = False
        self._monitor_task = None
        self.executor = ThreadPoolExecutor(max_workers=4)

        # Callbacks for new data detection
        self.new_records_callback = None

        # Worksheet mappings
        self.worksheets = {
            'records': 'Records',
            'appointments': 'Appointment_Details',
            'reschedules': 'Reschedule_Requests',
            'incomplete': 'Incomplete_Calls'
        }

        # Headers for each worksheet
        self.headers = {
            'records': ['Name', 'Phone Number', 'Address', 'Age', 'Gender'],
            'appointments': [
                'Name', 'Appointment Date', 'Time Slot', 'Doctor Name',
                'Age', 'Gender', 'Phone Number', 'Address', 'Timestamp'
            ],
            'reschedules': [
                'Name', 'Phone Number', 'Address', 'Age', 'Gender',
                'Call Timestamp', 'Preferred Callback Date', 'Preferred Callback Time',
                'Preferred Callback Day', 'Preferred Callback Period', 'Status', 'Priority'
            ],
            'incomplete': [
                'Name', 'Phone Number', 'Address', 'Age', 'Gender',
                'Call Timestamp', 'Call Duration (seconds)', 'Reason', 'Notes'
            ]
        }

    async def initialize(self) -> bool:
        """Initialize Google Sheets client"""
        try:
            # Setup credentials with both Sheets and Drive access
            scopes = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"
            ]

            creds = Credentials.from_service_account_file(
                self.credentials_file,
                scopes=scopes
            )

            # Initialize client in thread pool
            self.client = await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: gspread.authorize(creds)
            )

            logger.info("✅ Google Sheets service initialized successfully")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to initialize Google Sheets service: {e}")
            return False

    async def connect_to_sheet(self, sheet_id: str, worksheet_name: str = "Records") -> Dict:
        """Connect to Google Sheet and setup all worksheets"""
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

            # Get initial row count
            all_values = await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: self.current_sheet.get_all_values()
            )
            self.last_row_count = len(all_values)

            # Validate main sheet structure
            validation_result = await self._validate_sheet_structure()
            if not validation_result["valid"]:
                return {
                    "success": False,
                    "error": f"Invalid sheet structure: {validation_result['error']}"
                }

            # Setup result worksheets
            await self._setup_result_worksheets()

            logger.info(f"✅ Connected to sheet with {self.last_row_count} rows")

            return {
                "success": True,
                "sheet_id": sheet_id,
                "worksheet_name": self.current_sheet.title,
                "total_rows": self.last_row_count,
                "data_rows": max(0, self.last_row_count - 1)
            }

        except Exception as e:
            logger.error(f"❌ Failed to connect to sheet: {e}")
            return {
                "success": False,
                "error": str(e)
            }

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
        """Setup or create result worksheets for appointments, reschedules, etc."""
        try:
            for key, worksheet_name in self.worksheets.items():
                if key == 'records':  # Skip the main records sheet
                    continue

                try:
                    # Try to get existing worksheet
                    await asyncio.get_event_loop().run_in_executor(
                        self.executor, lambda: self.current_spreadsheet.worksheet(worksheet_name)
                    )
                    logger.info(f"📊 Found existing worksheet: {worksheet_name}")

                except gspread.WorksheetNotFound:
                    # Create new worksheet
                    worksheet = await asyncio.get_event_loop().run_in_executor(
                        self.executor, lambda: self.current_spreadsheet.add_worksheet(
                            title=worksheet_name,
                            rows=1000,
                            cols=len(self.headers[key])
                        )
                    )

                    # Add headers
                    await asyncio.get_event_loop().run_in_executor(
                        self.executor, lambda: worksheet.append_row(self.headers[key])
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

    async def check_for_new_records(self) -> Tuple[List[Dict], int]:
        """Check if new records have been added to the sheet"""
        try:
            if not self.current_sheet:
                return [], 0

            # Get current row count
            all_values = await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: self.current_sheet.get_all_values()
            )
            current_row_count = len(all_values)

            # Check if new rows were added
            if current_row_count > self.last_row_count:
                new_row_count = current_row_count - self.last_row_count
                logger.info(f"🆕 Detected {new_row_count} new rows in sheet")

                new_records = []

                # Get records starting from the last known row
                for row_num in range(self.last_row_count + 1, current_row_count + 1):
                    try:
                        row_values = await asyncio.get_event_loop().run_in_executor(
                            self.executor, lambda: self.current_sheet.row_values(row_num)
                        )

                        # Skip empty rows
                        if not any(row_values):
                            continue

                        # Map to record format
                        header_row = await asyncio.get_event_loop().run_in_executor(
                            self.executor, lambda: self.current_sheet.row_values(1)
                        )

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
                                'row_number': row_num
                            }
                            new_records.append(clean_record)

                    except Exception as e:
                        logger.warning(f"⚠️ Error processing new row {row_num}: {e}")

                # Update last known row count
                self.last_row_count = current_row_count
                return new_records, new_row_count

            return [], 0

        except Exception as e:
            logger.error(f"❌ Error checking for new records: {e}")
            return [], 0

    async def append_appointment(self, appointment_details: Dict, patient_record: Dict) -> bool:
        """Append successful appointment to Appointment_Details worksheet"""
        try:
            logger.info(f"📝 Saving appointment for {patient_record.get('name', 'Unknown')}")

            worksheet = await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: self.current_spreadsheet.worksheet(self.worksheets['appointments'])
            )

            row_data = [
                patient_record.get('name', ''),
                appointment_details.get('appointment_date', ''),
                appointment_details.get('appointment_time', '') or appointment_details.get('time_slot', ''),
                appointment_details.get('doctor_name', 'डॉ. निशा'),
                patient_record.get('age', ''),
                patient_record.get('gender', ''),
                patient_record.get('phone_number', ''),
                patient_record.get('address', ''),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ]

            await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: worksheet.append_row(row_data)
            )

            logger.info(f"✅ Appointment saved successfully")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to save appointment: {e}")
            return False

    async def append_reschedule(self, patient_record: Dict, callback_details: Dict = None) -> bool:
        """Append reschedule request to Reschedule_Requests worksheet"""
        try:
            logger.info(f"📅 Saving reschedule request for {patient_record.get('name', 'Unknown')}")

            worksheet = await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: self.current_spreadsheet.worksheet(self.worksheets['reschedules'])
            )

            # Process callback details
            callback_date = ""
            callback_time = ""
            callback_day = ""
            callback_period = ""
            priority = "Medium"

            if callback_details:
                callback_date = callback_details.get('callback_date', "")
                callback_time = callback_details.get('callback_time', "")
                callback_day = callback_details.get('callback_day', "")
                callback_period = callback_details.get('callback_period', "")

                # Calculate priority based on specificity
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

            row_data = [
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

            await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: worksheet.append_row(row_data)
            )

            logger.info(f"✅ Reschedule request saved successfully")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to save reschedule request: {e}")
            return False

    async def append_incomplete_call(self, patient_record: Dict, reason: str = "call_incomplete", call_duration: int = 0) -> bool:
        """Append incomplete call to Incomplete_Calls worksheet"""
        try:
            logger.info(f"📞 Saving incomplete call for {patient_record.get('name', 'Unknown')}")

            worksheet = await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: self.current_spreadsheet.worksheet(self.worksheets['incomplete'])
            )

            reason_notes = {
                "call_timeout": "Call exceeded time limit",
                "call_incomplete": "Call ended without clear resolution",
                "minimal_interaction": "Very few exchanges in conversation",
                "goodbye_detected": "Call ended with natural goodbye"
            }

            row_data = [
                patient_record.get('name', ''),
                patient_record.get('phone_number', ''),
                patient_record.get('address', ''),
                patient_record.get('age', ''),
                patient_record.get('gender', ''),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                call_duration,
                reason,
                reason_notes.get(reason, "Call incomplete")
            ]

            await asyncio.get_event_loop().run_in_executor(
                self.executor, lambda: worksheet.append_row(row_data)
            )

            logger.info(f"✅ Incomplete call saved successfully")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to save incomplete call: {e}")
            return False

    async def start_monitoring(self, callback_func=None, check_interval: int = 30):
        """Start monitoring the sheet for new records"""
        if self.monitoring_active:
            logger.warning("⚠️ Monitoring already active")
            return

        self.monitoring_active = True
        self.new_records_callback = callback_func

        self._monitor_task = asyncio.create_task(
            self._monitor_loop(check_interval)
        )

        logger.info(f"🔍 Started monitoring sheet for new records (interval: {check_interval}s)")

    async def stop_monitoring(self):
        """Stop monitoring the sheet"""
        self.monitoring_active = False

        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        logger.info("🛑 Stopped monitoring sheet")

    async def _monitor_loop(self, check_interval: int):
        """Internal monitoring loop"""
        try:
            while self.monitoring_active:
                try:
                    new_records, new_count = await self.check_for_new_records()

                    if new_records and self.new_records_callback:
                        logger.info(f"🆕 Found {len(new_records)} new records, notifying callback")
                        await self.new_records_callback(new_records)

                except Exception as e:
                    logger.error(f"❌ Error in monitoring loop: {e}")

                await asyncio.sleep(check_interval)

        except asyncio.CancelledError:
            logger.info("📊 Monitoring loop cancelled")
        except Exception as e:
            logger.error(f"❌ Monitoring loop error: {e}")

    def get_status(self) -> Dict:
        """Get current service status"""
        return {
            "connected": self.current_sheet is not None,
            "sheet_id": self.sheet_id,
            "monitoring_active": self.monitoring_active,
            "last_row_count": self.last_row_count,
            "worksheet_name": self.current_sheet.title if self.current_sheet else None,
            "spreadsheet_title": self.current_spreadsheet.title if self.current_spreadsheet else None
        }

    def __del__(self):
        """Cleanup on destruction"""
        if hasattr(self, 'executor'):
            self.executor.shutdown(wait=False)


# Global instance
google_sheets_service = GoogleSheetsService()