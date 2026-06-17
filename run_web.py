#!/usr/bin/env python3
"""Convenience launcher for the local web console backend (127.0.0.1:8000).

Equivalent to `python -m assistant.web.api`. The React UI runs separately:
    cd assistant/web/frontend && npm install && npm run dev   # http://localhost:5173
"""

from assistant.web.api import main

if __name__ == "__main__":
    main()
