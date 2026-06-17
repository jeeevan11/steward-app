#!/usr/bin/env python3
"""Convenience entry point. Equivalent to `python -m assistant`.

Usage:
    python run.py                 # start the assistant (mode comes from .env)
    python run.py --onboard       # run first-time onboarding then start
    python run.py --once          # do a single poll/process pass and exit (debug)
    python run.py --status        # print a one-shot status summary and exit
"""

import sys

from assistant.main import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
