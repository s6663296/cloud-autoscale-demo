import os

FIXED_URL = os.environ.get("FIXED_URL", "")
AUTO_URL = os.environ.get("AUTO_URL", "")
TIMEOUT_MS = int(os.environ.get("TIMEOUT_MS", "10000"))
TRACK_INTERVAL_MS = int(os.environ.get("TRACK_INTERVAL_MS", "2000"))
