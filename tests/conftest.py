import os
import sys
import tempfile
from pathlib import Path

# Pytest imports conftest.py before collecting any test module in this
# directory, so setting these here (instead of per-file) guarantees they are
# in place before app.core.config.Settings() is instantiated for the first
# time, regardless of which test file pytest happens to import first.
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/compactpay-test.db")
os.environ.setdefault("START_MQTT_WORKER", "false")
os.environ.setdefault("START_COMMAND_QUEUE_WORKER", "false")
os.environ.setdefault("START_RETENTION_WORKER", "false")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("APP_VERSION", "test")
os.environ.setdefault("APP_REVISION", "pytest")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
