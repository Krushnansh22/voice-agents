"""
Google Drive API Integration with Push Notifications
Real-time monitoring of Google Sheets changes using official Google Drive API
Updated to use environment variables for credentials
"""
import asyncio
import logging
from typing import List, Dict, Optional, Tuple, Callable
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import uuid
import hashlib
import hmac
import json

logger = logging.getLogger(__name__)


class GoogleDriveNotificationService:
    """Service for managing Google Drive push notifications"""

    def __init__(self, credentials_dict: dict = None):
        self.credentials_dict = credentials_dict
        self.sheets_client = None
        self.drive_service = None
        self.credentials = None
        self.executor = ThreadPoolExecutor(max_workers=4)

        # Notification management
        self.active_channels = {}  # channel_id -> channel_info
        self.webhook_secret = None
        self.notification_callback = None

        # Configuration
        self.webhook_url = None
        self.webhook_token = None

    async def initialize(self, webhook_url: str, webhook_secret: str = None) -> bool:
        """Initialize Google Drive API client and set webhook URL"""
        try:
            # Setup credentials with all required scopes
            scopes = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/drive.file",
                "https://www.googleapis.com/auth/drive.metadata"
            ]

            if self.credentials_dict:
                # Use credentials from environment variables
                self.credentials = Credentials.from_service_account_info(
                    self.credentials_dict,
                    scopes=scopes
                )
                logger.info("✅ Using credentials from environment variables")
            else:
                # Fallback to file-based credentials
                from settings import settings
                credentials_file = settings.GOOGLE_SERVICE_ACCOUNT_FILE
                self.credentials = Credentials.from_service_account_file(
                    credentials_file,
                    scopes=scopes
                )
                logger.info(f"✅ Using credentials from file: {credentials_file}")

            # Initialize services in thread pool
            def _init_services():
                sheets_client = gspread.authorize(self.credentials)
                drive_service = build('drive', 'v3', credentials=self.credentials)
                return sheets_client, drive_service

            self.sheets_client, self.drive_service = await asyncio.get_event_loop().run_in_executor(
                self.executor, _init_services
            )

            self.webhook_url = webhook_url
            self.webhook_secret = webhook_secret or self._generate_webhook_secret()

            logger.info("✅ Google Drive API service initialized successfully")
            return True

        except Exception as e:
            logger.error(f"❌ Failed to initialize Google Drive API service: {e}")
            return False

    def _generate_webhook_secret(self) -> str:
        """Generate a secure webhook secret"""
        return hashlib.sha256(f"sheet-monitor-{uuid.uuid4()}".encode()).hexdigest()

    async def setup_file_monitoring(self, file_id: str, callback: Callable = None) -> Dict:
        """Setup push notifications for a specific Google Sheets file"""
        try:
            logger.info(f"🔔 Setting up monitoring for file: {file_id}")

            # Generate unique channel ID
            channel_id = f"sheet-monitor-{uuid.uuid4()}"

            # Setup webhook endpoint URL with auth token
            webhook_endpoint = f"{self.webhook_url}?token={self.webhook_secret}&file_id={file_id}"

            def _setup_watch():
                try:
                    # Create watch request
                    body = {
                        'id': channel_id,
                        'type': 'web_hook',
                        'address': webhook_endpoint,
                        'token': self.webhook_secret,
                        'expiration': str(int((datetime.now() + timedelta(hours=24)).timestamp() * 1000))
                    }

                    # Start watching the file
                    result = self.drive_service.files().watch(
                        fileId=file_id,
                        body=body
                    ).execute()

                    return result

                except Exception as e:
                    logger.error(f"❌ Error setting up watch: {e}")
                    raise

            # Execute in thread pool
            result = await asyncio.get_event_loop().run_in_executor(
                self.executor, _setup_watch
            )

            # Store channel information
            channel_info = {
                'id': channel_id,
                'file_id': file_id,
                'resource_id': result.get('resourceId'),
                'expiration': result.get('expiration'),
                'callback': callback,
                'created_at': datetime.now()
            }

            self.active_channels[channel_id] = channel_info
            self.notification_callback = callback

            logger.info(f"✅ Successfully setup monitoring for file {file_id}")
            logger.info(f"📡 Channel ID: {channel_id}")
            logger.info(f"⏰ Expires: {datetime.fromtimestamp(int(result.get('expiration', 0)) / 1000)}")

            return {
                "success": True,
                "channel_id": channel_id,
                "file_id": file_id,
                "webhook_url": webhook_endpoint,
                "expiration": result.get('expiration')
            }

        except Exception as e:
            logger.error(f"❌ Failed to setup file monitoring: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def stop_file_monitoring(self, channel_id: str) -> bool:
        """Stop monitoring a specific file"""
        try:
            if channel_id not in self.active_channels:
                logger.warning(f"⚠️ Channel {channel_id} not found")
                return False

            channel_info = self.active_channels[channel_id]

            def _stop_watch():
                try:
                    # Stop the watch
                    self.drive_service.channels().stop(
                        body={
                            'id': channel_id,
                            'resourceId': channel_info['resource_id']
                        }
                    ).execute()
                    return True
                except Exception as e:
                    logger.error(f"❌ Error stopping watch: {e}")
                    return False

            # Execute in thread pool
            success = await asyncio.get_event_loop().run_in_executor(
                self.executor, _stop_watch
            )

            if success:
                # Remove from active channels
                del self.active_channels[channel_id]
                logger.info(f"✅ Stopped monitoring channel: {channel_id}")
                return True
            else:
                logger.error(f"❌ Failed to stop monitoring channel: {channel_id}")
                return False

        except Exception as e:
            logger.error(f"❌ Error stopping file monitoring: {e}")
            return False

    async def stop_all_monitoring(self):
        """Stop all active file monitoring"""
        try:
            logger.info(f"🛑 Stopping all monitoring ({len(self.active_channels)} channels)")

            for channel_id in list(self.active_channels.keys()):
                await self.stop_file_monitoring(channel_id)

            logger.info("✅ All monitoring stopped")

        except Exception as e:
            logger.error(f"❌ Error stopping all monitoring: {e}")

    async def handle_webhook_notification(self, headers: Dict, body: str = None) -> Dict:
        """Handle incoming webhook notification from Google Drive"""
        try:
            # Validate webhook
            if not self._validate_webhook(headers, body):
                logger.warning("⚠️ Invalid webhook signature")
                return {"success": False, "error": "Invalid signature"}

            # Extract notification information
            channel_id = headers.get('x-goog-channel-id')
            resource_state = headers.get('x-goog-resource-state')
            changed = headers.get('x-goog-changed')

            logger.info(f"📡 Webhook notification received:")
            logger.info(f"   Channel: {channel_id}")
            logger.info(f"   State: {resource_state}")
            logger.info(f"   Changed: {changed}")

            # Check if this is a channel we're monitoring
            if channel_id not in self.active_channels:
                logger.warning(f"⚠️ Received notification for unknown channel: {channel_id}")
                return {"success": False, "error": "Unknown channel"}

            channel_info = self.active_channels[channel_id]

            # Handle different resource states
            if resource_state in ['update', 'change']:
                logger.info(f"📝 File {channel_info['file_id']} has been updated")

                # Call the callback function if provided
                if self.notification_callback:
                    try:
                        await self.notification_callback(channel_info['file_id'], resource_state)
                    except Exception as e:
                        logger.error(f"❌ Error in notification callback: {e}")

                return {
                    "success": True,
                    "message": "Notification processed",
                    "file_id": channel_info['file_id'],
                    "state": resource_state
                }

            elif resource_state == 'sync':
                logger.info(f"🔄 Sync notification for channel {channel_id}")
                return {"success": True, "message": "Sync notification received"}

            else:
                logger.info(f"ℹ️ Unhandled resource state: {resource_state}")
                return {"success": True, "message": f"Unhandled state: {resource_state}"}

        except Exception as e:
            logger.error(f"❌ Error handling webhook notification: {e}")
            return {"success": False, "error": str(e)}

    def _validate_webhook(self, headers: Dict, body: str = None) -> bool:
        """Validate webhook signature"""
        try:
            # Check for required headers
            required_headers = ['x-goog-channel-id', 'x-goog-resource-state']
            for header in required_headers:
                if header not in headers:
                    logger.warning(f"⚠️ Missing required header: {header}")
                    return False

            return True

        except Exception as e:
            logger.error(f"❌ Error validating webhook: {e}")
            return False

    async def get_file_metadata(self, file_id: str) -> Dict:
        """Get metadata for a Google Sheets file"""
        try:
            def _get_metadata():
                return self.drive_service.files().get(
                    fileId=file_id,
                    fields='id,name,modifiedTime,version,size'
                ).execute()

            metadata = await asyncio.get_event_loop().run_in_executor(
                self.executor, _get_metadata
            )

            return {
                "success": True,
                "metadata": metadata
            }

        except Exception as e:
            logger.error(f"❌ Error getting file metadata: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def refresh_channel_expiration(self, channel_id: str) -> bool:
        """Refresh the expiration time for a monitoring channel"""
        try:
            if channel_id not in self.active_channels:
                return False

            channel_info = self.active_channels[channel_id]
            file_id = channel_info['file_id']

            # Stop current monitoring
            await self.stop_file_monitoring(channel_id)

            # Setup new monitoring
            result = await self.setup_file_monitoring(
                file_id,
                channel_info.get('callback')
            )

            return result.get("success", False)

        except Exception as e:
            logger.error(f"❌ Error refreshing channel expiration: {e}")
            return False

    def get_status(self) -> Dict:
        """Get current monitoring status"""
        try:
            channels_info = []
            for channel_id, info in self.active_channels.items():
                expiration_timestamp = int(info.get('expiration', 0)) / 1000
                channels_info.append({
                    "channel_id": channel_id,
                    "file_id": info['file_id'],
                    "created_at": info['created_at'].isoformat(),
                    "expires_at": datetime.fromtimestamp(
                        expiration_timestamp).isoformat() if expiration_timestamp else None,
                    "resource_id": info.get('resource_id')
                })

            return {
                "service_initialized": self.drive_service is not None,
                "webhook_url": self.webhook_url,
                "active_channels": len(self.active_channels),
                "channels": channels_info,
                "credentials_source": "environment_variables" if self.credentials_dict else "file"
            }

        except Exception as e:
            logger.error(f"❌ Error getting status: {e}")
            return {"error": str(e)}

    def __del__(self):
        """Cleanup on destruction"""
        if hasattr(self, 'executor'):
            self.executor.shutdown(wait=False)


# Initialize global instance with credentials from settings
def create_drive_notification_service():
    """Factory function to create drive notification service with proper credentials"""
    try:
        from settings import settings
        credentials_dict = settings.get_google_credentials_dict()
        return GoogleDriveNotificationService(credentials_dict=credentials_dict)
    except Exception as e:
        logger.warning(f"⚠️ Could not load credentials from environment, falling back to file: {e}")
        return GoogleDriveNotificationService()

# Global instance
drive_notification_service = create_drive_notification_service()