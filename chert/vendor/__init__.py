"""Compatibility boundary for the byte-for-byte audited upstream modules.

Only this module adapts their historical import names and checkout-relative paths.
Application code imports this package, never the historical global module names.
"""
import sys
from dotenv import load_dotenv
from chert.paths import ROOT

load_dotenv(ROOT / '.env')
from . import checkin
from . import storage

# The audited bridge uses absolute imports for these two companion modules.
sys.modules['app'] = checkin
sys.modules['ash_twin'] = storage
from . import bridge

bridge.STATE_FILE = ROOT / 'bot_state.json'
checkin.app.template_folder = str(ROOT / 'templates')
checkin.app.static_folder = str(ROOT / 'static')
old_root = storage.HERE
storage.HERE = ROOT
storage.PROTECTED.discard(old_root)
storage.PROTECTED.add(ROOT)
storage.BACKUP_SETS = [(ROOT / path.relative_to(old_root) if path.is_relative_to(old_root) else path,
                        recursive, pattern) for path, recursive, pattern in storage.BACKUP_SETS]
