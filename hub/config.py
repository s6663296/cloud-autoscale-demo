import os

FIXED_URL = os.environ.get("FIXED_URL", "")
AUTO_URL = os.environ.get("AUTO_URL", "")
TIMEOUT_MS = int(os.environ.get("TIMEOUT_MS", "15000"))
