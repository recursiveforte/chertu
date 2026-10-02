#!/usr/bin/env python3
"""Select Chert's backend without importing the other backend's dependencies."""
import os
from pathlib import Path

from dotenv import load_dotenv


def main():
    load_dotenv(Path(__file__).with_name('.env'))
    backend = os.environ.get('CHERT_BACKEND', 'codex').strip().lower()
    if backend == 'codex':
        from codex_bot import main as run
    elif backend == 'claude':
        from discord_bot import main as run
    else:
        raise SystemExit('CHERT_BACKEND must be codex or claude')
    run()


if __name__ == '__main__':
    main()
