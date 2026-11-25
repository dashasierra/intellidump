"""Intellidump: a pg_dump-style data exporter driven by a SELECT query.

The rows selected by the query are exported together with every row that
depends on them (recursively, through foreign keys) and, by default, every row
those rows reference, so the resulting ``.sql`` file can be replayed into a
database with all foreign key constraints enabled.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .connection import ConnectionOptions
from .dump import DumpOptions, DumpResult, run_dump
from .errors import IntellidumpError

try:
    __version__ = version("intellidump")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0+unknown"

__all__ = [
    "ConnectionOptions",
    "DumpOptions",
    "DumpResult",
    "IntellidumpError",
    "__version__",
    "run_dump",
]
