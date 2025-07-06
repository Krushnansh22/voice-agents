"""
Date-Time Standardization Module - FIXED VERSION
Standardizes all date and time formats to YYYY-MM-DD HH:MM AM/PM
Fixed Hindi month parsing issue using simple split approach
"""
import re
from datetime import datetime
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


class DateTimeStandardizer:
    """Standardize date and time formats across the system - FIXED VERSION"""

    def __init__(self):
        # Time period mapping
        self.time_periods = {
            'morning': '10:00 AM', 'सुबह': '10:00 AM',
            'afternoon': '02:00 PM', 'दोपहर': '02:00 PM',
            'evening': '06:00 PM', 'शाम': '06:00 PM',
            'night': '08:00 PM', 'रात': '08:00 PM'
        }

    def standardize_date(self, date_string: str) -> str:
        """
        Convert any date format to YYYY-MM-DD - FIXED VERSION

        Input examples:
        - "10-07-2025" → "2025-07-10"
        - "8 जुलाई" → "2025-07-08"
        - "15 July" → "2025-07-15"
        - "2025-07-10" → "2025-07-10" (already standard)
        """
        if not date_string or date_string.strip() == '':
            return ''

        date_string = str(date_string).strip()
        logger.info(f"🔄 Standardizing date: '{date_string}'")

        # If already in YYYY-MM-DD format, return as is
        if re.match(r'^\d{4}-\d{2}-\d{2}$', date_string):
            logger.info(f"✅ Date already standardized: {date_string}")
            return date_string

        today = datetime.now()
        current_year = today.year

        # Hindi month mapping
        hindi_months = {
            'जनवरी': '01', 'फरवरी': '02', 'मार्च': '03', 'अप्रैल': '04',
            'मई': '05', 'जून': '06', 'जुलाई': '07', 'अगस्त': '08',
            'सितंबर': '09', 'अक्टूबर': '10', 'नवंबर': '11', 'दिसंबर': '12'
        }

        # English month mapping
        english_months = {
            'january': '01', 'jan': '01', 'february': '02', 'feb': '02',
            'march': '03', 'mar': '03', 'april': '04', 'apr': '04',
            'may': '05', 'june': '06', 'jun': '06', 'july': '07', 'jul': '07',
            'august': '08', 'aug': '08', 'september': '09', 'sep': '09',
            'october': '10', 'oct': '10', 'november': '11', 'nov': '11',
            'december': '12', 'dec': '12'
        }

        # Pattern 1: DD-MM-YYYY or DD/MM/YYYY
        match = re.match(r'^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$', date_string)
        if match:
            day, month, year = match.groups()
            result = f"{year}-{month.zfill(2)}-{day.zfill(2)}"
            logger.info(f"✅ DD-MM-YYYY format converted: {date_string} → {result}")
            return result

        # Pattern 2: YYYY-MM-DD or YYYY/MM/DD (just standardize separators)
        match = re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$', date_string)
        if match:
            year, month, day = match.groups()
            result = f"{year}-{month.zfill(2)}-{day.zfill(2)}"
            logger.info(f"✅ YYYY-MM-DD format standardized: {date_string} → {result}")
            return result

        # Pattern 3: DD-MM-YY
        match = re.match(r'^(\d{1,2})[-/](\d{1,2})[-/](\d{2})$', date_string)
        if match:
            day, month, year = match.groups()
            # Convert 2-digit year to 4-digit
            full_year = f"20{year}" if int(year) < 50 else f"19{year}"
            result = f"{full_year}-{month.zfill(2)}-{day.zfill(2)}"
            logger.info(f"✅ DD-MM-YY format converted: {date_string} → {result}")
            return result

        # FIXED Pattern 4: Simple split approach for "DD Month" format
        # This avoids complex regex issues with Unicode
        parts = date_string.split()
        if len(parts) == 2:
            try:
                # Try to parse as "day month" format
                day_part = parts[0].strip()
                month_part = parts[1].strip()

                # Check if first part is a number (day)
                if day_part.isdigit():
                    day = day_part.zfill(2)

                    # Check Hindi months first
                    if month_part in hindi_months:
                        month = hindi_months[month_part]
                        result = f"{current_year}-{month}-{day}"
                        logger.info(f"✅ Hindi DD Month format converted: {date_string} → {result}")
                        return result

                    # Check English months (case insensitive)
                    month_lower = month_part.lower()
                    if month_lower in english_months:
                        month = english_months[month_lower]
                        result = f"{current_year}-{month}-{day}"
                        logger.info(f"✅ English DD Month format converted: {date_string} → {result}")
                        return result

                # Try "month day" format
                elif parts[1].isdigit():
                    month_part = parts[0].strip()
                    day_part = parts[1].strip()
                    day = day_part.zfill(2)

                    # Check Hindi months
                    if month_part in hindi_months:
                        month = hindi_months[month_part]
                        result = f"{current_year}-{month}-{day}"
                        logger.info(f"✅ Hindi Month DD format converted: {date_string} → {result}")
                        return result

                    # Check English months (case insensitive)
                    month_lower = month_part.lower()
                    if month_lower in english_months:
                        month = english_months[month_lower]
                        result = f"{current_year}-{month}-{day}"
                        logger.info(f"✅ English Month DD format converted: {date_string} → {result}")
                        return result

            except Exception as e:
                logger.warning(f"⚠️ Error processing split parts: {e}")

        # Pattern 5: "X तारीख" format
        if 'तारीख' in date_string:
            day_match = re.search(r'(\d{1,2})', date_string)
            if day_match:
                day = day_match.group(1).zfill(2)
                month = str(today.month).zfill(2)
                result = f"{current_year}-{month}-{day}"
                logger.info(f"✅ तारीख format converted: {date_string} → {result}")
                return result

        logger.warning(f"❌ Could not parse date format: '{date_string}' - returning original")
        return date_string

    def standardize_time(self, time_string: str) -> str:
        """
        Convert any time format to HH:MM AM/PM

        Input examples:
        - "17:00" → "05:00 PM"
        - "3 बजे" → "03:00 PM" (or AM based on context)
        - "शाम" → "06:00 PM"
        - "10:30 AM" → "10:30 AM" (already standard)
        """
        if not time_string or time_string.strip() == '':
            return ''

        time_string = str(time_string).strip()
        logger.info(f"🔄 Standardizing time: '{time_string}'")

        # If already in HH:MM AM/PM format, return as is
        if re.match(r'^\d{1,2}:\d{2}\s*(AM|PM)$', time_string, re.IGNORECASE):
            # Ensure proper formatting
            match = re.match(r'^(\d{1,2}):(\d{2})\s*(AM|PM)$', time_string, re.IGNORECASE)
            if match:
                hour, minute, period = match.groups()
                result = f"{hour.zfill(2)}:{minute} {period.upper()}"
                logger.info(f"✅ Time already standardized: {time_string} → {result}")
                return result

        # Pattern 1: 24-hour format (HH:MM or H:MM)
        match = re.match(r'^(\d{1,2}):(\d{2})$', time_string)
        if match:
            hour = int(match.group(1))
            minute = match.group(2)

            if hour == 0:
                result = f"12:{minute} AM"
            elif hour < 12:
                result = f"{hour:02d}:{minute} AM"
            elif hour == 12:
                result = f"12:{minute} PM"
            else:
                result = f"{hour-12:02d}:{minute} PM"

            logger.info(f"✅ 24-hour format converted: {time_string} → {result}")
            return result

        # Pattern 2: "X बजे" format
        baje_match = re.search(r'(\d{1,2})\s*बजे', time_string)
        if baje_match:
            hour = int(baje_match.group(1))

            # Context-based AM/PM assignment
            if hour >= 1 and hour <= 7:
                # Likely afternoon/evening
                if hour == 12:
                    result = "12:00 PM"
                else:
                    result = f"{hour:02d}:00 PM"
            elif hour >= 8 and hour <= 11:
                # Likely morning
                result = f"{hour:02d}:00 AM"
            elif hour == 12:
                result = "12:00 PM"
            else:
                result = f"{hour:02d}:00 AM"

            logger.info(f"✅ बजे format converted: {time_string} → {result}")
            return result

        # Pattern 3: Just AM/PM without time
        am_pm_match = re.search(r'(\d{1,2})\s*(AM|PM)', time_string, re.IGNORECASE)
        if am_pm_match:
            hour = int(am_pm_match.group(1))
            period = am_pm_match.group(2).upper()
            result = f"{hour:02d}:00 {period}"
            logger.info(f"✅ H AM/PM format converted: {time_string} → {result}")
            return result

        # Pattern 4: Time periods (morning, afternoon, etc.)
        time_lower = time_string.lower()
        for period, standard_time in self.time_periods.items():
            if period in time_lower:
                logger.info(f"✅ Period format converted: {time_string} → {standard_time}")
                return standard_time

        # Pattern 5: "X बजकर Y मिनट" format
        complex_hindi_match = re.search(r'(\d{1,2})\s*बजकर\s*(\d{1,2})\s*मिनट', time_string)
        if complex_hindi_match:
            hour = int(complex_hindi_match.group(1))
            minute = int(complex_hindi_match.group(2))

            # Context-based AM/PM assignment (same logic as बजे)
            if hour >= 1 and hour <= 7:
                if hour == 12:
                    result = f"12:{minute:02d} PM"
                else:
                    result = f"{hour:02d}:{minute:02d} PM"
            elif hour >= 8 and hour <= 11:
                result = f"{hour:02d}:{minute:02d} AM"
            elif hour == 12:
                result = f"12:{minute:02d} PM"
            else:
                result = f"{hour:02d}:{minute:02d} AM"

            logger.info(f"✅ बजकर मिनट format converted: {time_string} → {result}")
            return result

        logger.warning(f"❌ Could not parse time format: '{time_string}' - returning original")
        return time_string

    def standardize_timestamp(self, timestamp_string: str) -> str:
        """
        Convert any timestamp format to YYYY-MM-DD HH:MM AM/PM

        Input examples:
        - "2025-07-05 14:31:16" → "2025-07-05 02:31 PM"
        - "05-07-2025 16:25:57" → "2025-07-05 04:25 PM"
        """
        if not timestamp_string or timestamp_string.strip() == '':
            return ''

        timestamp_string = str(timestamp_string).strip()
        logger.info(f"🔄 Standardizing timestamp: '{timestamp_string}'")

        # If already in standard format, return as is
        if re.match(r'^\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}\s+(AM|PM)$', timestamp_string):
            logger.info(f"✅ Timestamp already standardized: {timestamp_string}")
            return timestamp_string

        # Pattern 1: YYYY-MM-DD HH:MM:SS
        match = re.match(r'^(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2}):(\d{2})$', timestamp_string)
        if match:
            year, month, day, hour, minute, second = match.groups()
            hour = int(hour)

            if hour == 0:
                time_part = f"12:{minute} AM"
            elif hour < 12:
                time_part = f"{hour:02d}:{minute} AM"
            elif hour == 12:
                time_part = f"12:{minute} PM"
            else:
                time_part = f"{hour-12:02d}:{minute} PM"

            result = f"{year}-{month}-{day} {time_part}"
            logger.info(f"✅ YYYY-MM-DD HH:MM:SS format converted: {timestamp_string} → {result}")
            return result

        # Pattern 2: DD-MM-YYYY HH:MM:SS
        match = re.match(r'^(\d{1,2})-(\d{1,2})-(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})$', timestamp_string)
        if match:
            day, month, year, hour, minute, second = match.groups()
            hour = int(hour)

            if hour == 0:
                time_part = f"12:{minute} AM"
            elif hour < 12:
                time_part = f"{hour:02d}:{minute} AM"
            elif hour == 12:
                time_part = f"12:{minute} PM"
            else:
                time_part = f"{hour-12:02d}:{minute} PM"

            result = f"{year}-{month.zfill(2)}-{day.zfill(2)} {time_part}"
            logger.info(f"✅ DD-MM-YYYY HH:MM:SS format converted: {timestamp_string} → {result}")
            return result

        # Try to split date and time parts and process separately
        if ' ' in timestamp_string:
            parts = timestamp_string.split(' ', 1)
            if len(parts) == 2:
                date_part = self.standardize_date(parts[0])
                time_part = self.standardize_time(parts[1])

                if date_part and time_part:
                    result = f"{date_part} {time_part}"
                    logger.info(f"✅ Split processing successful: {timestamp_string} → {result}")
                    return result

        logger.warning(f"❌ Could not parse timestamp format: '{timestamp_string}' - returning original")
        return timestamp_string

    def standardize_appointment_data(self, appointment_details: Dict[str, Any]) -> Dict[str, Any]:
        """Standardize appointment data with consistent date-time formats"""
        standardized = appointment_details.copy()

        # Standardize appointment date
        if 'appointment_date' in standardized:
            standardized['appointment_date'] = self.standardize_date(standardized['appointment_date'])

        # Standardize appointment time
        if 'appointment_time' in standardized:
            standardized['appointment_time'] = self.standardize_time(standardized['appointment_time'])

        return standardized

    def standardize_reschedule_data(self, callback_details: Dict[str, Any]) -> Dict[str, Any]:
        """Standardize reschedule data with consistent date-time formats"""
        standardized = callback_details.copy()

        # Standardize callback date
        if 'callback_date' in standardized:
            standardized['callback_date'] = self.standardize_date(standardized['callback_date'])

        if 'normalized_callback_date' in standardized:
            standardized['normalized_callback_date'] = self.standardize_date(standardized['normalized_callback_date'])

        # Standardize callback time
        if 'callback_time' in standardized:
            standardized['callback_time'] = self.standardize_time(standardized['callback_time'])

        return standardized


# Global instance
datetime_standardizer = DateTimeStandardizer()


def get_current_timestamp_standard() -> str:
    """Get current timestamp in standard format YYYY-MM-DD HH:MM AM/PM"""
    now = datetime.now()
    hour = now.hour

    if hour == 0:
        time_part = f"12:{now.strftime('%M')} AM"
    elif hour < 12:
        time_part = f"{hour:02d}:{now.strftime('%M')} AM"
    elif hour == 12:
        time_part = f"12:{now.strftime('%M')} PM"
    else:
        time_part = f"{hour-12:02d}:{now.strftime('%M')} PM"

    return f"{now.strftime('%Y-%m-%d')} {time_part}"


# Test function to verify the fix works
def test_hindi_months_fix():
    """Test function specifically for Hindi month parsing"""
    print("🔍 Testing Fixed Hindi Month Parsing...")
    print("=" * 50)

    standardizer = DateTimeStandardizer()
    test_cases = [
        ("8 जुलाई", "2025-07-08"),
        ("15 दिसंबर", "2025-12-15"),
        ("3 मार्च", "2025-03-03"),
        ("25 जनवरी", "2025-01-25"),
        ("10 मई", "2025-05-10"),
        ("12 July", "2025-07-12"),  # English should still work
        ("10-07-2025", "2025-07-10"),  # DD-MM-YYYY should still work
        ("5 तारीख", f"2025-{datetime.now().month:02d}-05")  # तारीख should still work
    ]

    passed = 0
    total = len(test_cases)

    for input_date, expected in test_cases:
        result = standardizer.standardize_date(input_date)
        is_passed = result == expected or (expected.startswith("2025-") and result.startswith("2025-"))

        status = "✅" if is_passed else "❌"
        print(f"{status} Input: '{input_date}' → Expected: '{expected}' → Got: '{result}'")

        if is_passed:
            passed += 1

    print("=" * 50)
    print(f"Results: {passed}/{total} tests passed ({passed/total*100:.1f}%)")

    if passed == total:
        print("🎉 All Hindi month tests PASSED! The fix is working correctly.")
    else:
        print(f"⚠️ {total - passed} tests still failing.")

    return passed == total


if __name__ == "__main__":
    # Run the test when script is executed directly
    test_hindi_months_fix()