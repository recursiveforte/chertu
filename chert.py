#!/usr/bin/env python3
"""Select Chert's backend without importing the other backend's dependencies."""
import os
from pathlib import Path

from dotenv import load_dotenv


def main():
    load_dotenv(Path(__file__).with_name('.env'))
    backend = os.environ.get('CHERT_BACKEND', 'both').strip().lower()
    if backend in {'both', 'codex'}:
        from shared_frontend import main as run
    elif backend == 'claude':
        from discord_bot import main as run
    else:
        raise SystemExit('CHERT_BACKEND must be both, codex, or claude')
    run()


if __name__ == '__main__':
    main()
