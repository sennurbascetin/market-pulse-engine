"""Database access layer: connection management and schema bootstrap."""

from .connection import (
    DatabaseLockedError, close, database_path, get_connection, open_readonly,
    transaction, use_database, write_lock,
)

__all__ = [
    "DatabaseLockedError", "close", "database_path", "get_connection",
    "open_readonly", "transaction", "use_database", "write_lock",
]
