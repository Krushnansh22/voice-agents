"""
Google Sheets Service for Dynamic Patient Record Management
"""
import asyncio
import logging
from typing import List, Dict, Optional, Tuple
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import pandas as pd
import io

logger = logging.getLogger(__name__)


class GoogleSheetsService:
    """Service for handling Google Sheets operations with dynamic data detection"""

    def __init__(self, credentials_file: str = "creds.json"):
        self.credentials_file = credentials_file
        self.client = None
        self.current_sheet = None
        self.last_row_count = 0
        self.sheet_id = None
        self.monitoring_active = False
        self._monitor_task = None

        # Callbacks for new data detection
        self.new_records_callback = None

    async def initialize(self) -> bool:
        """Initialize Google Sheets client"""
        try:
            # Setup credentials (only Sheets API scope)
            scopes = ["https://www.googleapis.com/auth/spreadsheets"]

            creds = Credentials.from_service_account_file(
                self.credentials_file,
                scopes=scopes
            )

            # Use asyncio to run blocking gspread operations
            self.client = await asyncio.get_event_loop().run_in_executor(
                None, lambda: gspread.authorize(creds)
            )

            logger.info("✅ Google Sheets service initialized successfully")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to initialize Google Sheets service: {e}")
            return False

    async def connect_to_sheet(self, sheet_id: str, worksheet_name: str = None) -> Dict:
        """Connect to a specific Google Sheet"""
        try:
            logger.info(f"🔗 Connecting to Google Sheet: {sheet_id}")

            # Open spreadsheet by ID
            spreadsheet = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.client.open_by_key(sheet_id)
            )

            # Get first worksheet or specified worksheet
            if worksheet_name:
                self.current_sheet = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: spreadsheet.worksheet(worksheet_name)
                )
            else:
                self.current_sheet = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: spreadsheet.sheet1
                )

            self.sheet_id = sheet_id

            # Get initial row count
            all_values = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.current_sheet.get_all_values()
            )

            self.last_row_count = len(all_values)

            # Validate sheet structure
            validation_result = await self._validate_sheet_structure()
            if not validation_result["valid"]:
                return {
                    "success": False,
                    "error": f"Invalid sheet structure: {validation_result['error']}"
                }

            logger.info(f"✅ Connected to sheet with {self.last_row_count} rows")

            return {
                "success": True,
                "sheet_id": sheet_id,
                "worksheet_name": self.current_sheet.title,
                "total_rows": self.last_row_count,
                "data_rows": max(0, self.last_row_count - 1)  # Excluding header
            }

        except Exception as e:
            logger.error(f"❌ Failed to connect to sheet: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def _validate_sheet_structure(self) -> Dict:
        """Validate that the sheet has required columns"""
        try:
            # Get header row
            header_row = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.current_sheet.row_values(1)
            )

            # Required columns (case-insensitive)
            required_columns = ['Name', 'Phone Number', 'Address', 'Age', 'Gender']
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

    async def read_all_records(self) -> Tuple[List[Dict], List[str]]:
        """Read all records from the connected sheet"""
        try:
            if not self.current_sheet:
                raise ValueError("No sheet connected")

            # Get all records
            records = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.current_sheet.get_all_records()
            )

            # Process and validate records
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
                None, lambda: self.current_sheet.get_all_values()
            )

            current_row_count = len(all_values)

            # Check if new rows were added
            if current_row_count > self.last_row_count:
                new_row_count = current_row_count - self.last_row_count
                logger.info(f"🆕 Detected {new_row_count} new rows in sheet")

                # Get only the new records
                new_records = []

                # Get records starting from the last known row
                for row_num in range(self.last_row_count + 1, current_row_count + 1):
                    try:
                        row_values = await asyncio.get_event_loop().run_in_executor(
                            None, lambda: self.current_sheet.row_values(row_num)
                        )

                        # Skip empty rows
                        if not any(row_values):
                            continue

                        # Map to record format (assuming same structure as header)
                        header_row = await asyncio.get_event_loop().run_in_executor(
                            None, lambda: self.current_sheet.row_values(1)
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
                        # Call the callback function with new records
                        await self.new_records_callback(new_records)

                except Exception as e:
                    logger.error(f"❌ Error in monitoring loop: {e}")

                # Wait for next check
                await asyncio.sleep(check_interval)

        except asyncio.CancelledError:
            logger.info("📊 Monitoring loop cancelled")
        except Exception as e:
            logger.error(f"❌ Monitoring loop error: {e}")

    async def write_to_results_sheet(self, sheet_id: str, worksheet_name: str, data: List[Dict], headers: List[str]):
        """Write results to a Google Sheet (for appointment results, etc.)"""
        try:
            # Open results spreadsheet
            results_spreadsheet = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self.client.open_by_key(sheet_id)
            )

            try:
                results_sheet = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: results_spreadsheet.worksheet(worksheet_name)
                )
            except:
                # Create worksheet if it doesn't exist
                results_sheet = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: results_spreadsheet.add_worksheet(
                        title=worksheet_name,
                        rows=1000,
                        cols=len(headers)
                    )
                )

                # Add headers
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: results_sheet.append_row(headers)
                )

            # Append data rows
            for record in data:
                row_data = [record.get(header.lower().replace(' ', '_'), '') for header in headers]
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: results_sheet.append_row(row_data)
                )

            logger.info(f"✅ Written {len(data)} records to {worksheet_name}")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to write to results sheet: {e}")
            return False

    def get_status(self) -> Dict:
        """Get current service status"""
        return {
            "connected": self.current_sheet is not None,
            "sheet_id": self.sheet_id,
            "monitoring_active": self.monitoring_active,
            "last_row_count": self.last_row_count,
            "worksheet_name": self.current_sheet.title if self.current_sheet else None
        }


# Global instance
google_sheets_service = GoogleSheetsService()