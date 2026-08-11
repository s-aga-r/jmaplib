"""jmaplib - a complete, capability-driven JMAP client for Python.

Install as ``jmaplib``; import as ``jmap``.
"""

from __future__ import annotations

from jmap.__about__ import SPEC_REVISIONS, __version__
from jmap.core.errors import (
    AuthenticationError,
    BatchTooLargeError,
    CapabilityFieldError,
    CapabilityNotSupportedError,
    JMAPError,
    MethodError,
    RequestError,
    ServerPartialFailError,
    SetError,
    SetFailedError,
    TransportError,
)
from jmap.core.ids import CreationRef, Id, InvalidIdError, is_valid_id, parse_id
from jmap.core.invocation import NestedResultRefError

__all__ = [
    "SPEC_REVISIONS",
    "AuthenticationError",
    "BatchTooLargeError",
    "CapabilityFieldError",
    "CapabilityNotSupportedError",
    "CreationRef",
    "Id",
    "InvalidIdError",
    "JMAPError",
    "MethodError",
    "NestedResultRefError",
    "RequestError",
    "ServerPartialFailError",
    "SetError",
    "SetFailedError",
    "TransportError",
    "__version__",
    "is_valid_id",
    "parse_id",
]
