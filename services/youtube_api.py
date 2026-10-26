"""Единый объект YouTube Data API."""

import sys

from google.oauth2 import service_account
from googleapiclient.discovery import build

from config import SERVICE_ACCOUNT_FILE, YOUTUBE_SCOPES

try:
    _credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=YOUTUBE_SCOPES
    )
    youtube = build("youtube", "v3", credentials=_credentials)
    print("✅ YouTube API OK")
except Exception as _e:
    print(f"❌ YouTube API: {_e}")
    sys.exit(1)