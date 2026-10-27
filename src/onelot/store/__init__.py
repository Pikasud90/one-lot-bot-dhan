"""Persistence: SQLite in WAL mode.

*** v1 defect D1 ***
v1 required Docker + TimescaleDB to start. Docker is not installed on the
target Mac, needs WSL2 on Windows, and Docker Desktop is not free for larger
organisations — so the "standalone, FOSS, runs on Mac and Windows" requirement
was broken by the very first dependency.

SQLite ships with Python, installs nothing, and handles one user recording one
instrument without noticing. That is the whole storage story now.
"""

from .db import Database, init_database  # noqa: F401
from .schema import METADATA  # noqa: F401
