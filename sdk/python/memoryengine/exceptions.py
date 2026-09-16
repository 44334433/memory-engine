"""Exception hierarchy for the memory-engine client.

Mapping (engine error semantics, see public repo README "Error semantics"):
- 400 / 422            -> InvalidInput          (bad params, contract violations)
- 404                  -> NotFound              (unknown memory id / route)
-   └ route advertised but not mounted on this engine build -> EndpointNotAvailable
- 409                  -> Conflict              (e.g. in-place rewrite of a superseded entry)
- 503                  -> EngineOverloaded      (all recall routes failed; body has retryable/degraded)
- other 5xx            -> EngineServerError
- connection failures  -> EngineConnectionError
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = [
    "MemoryEngineError",
    "InvalidInput",
    "NotFound",
    "EndpointNotAvailable",
    "Conflict",
    "EngineOverloaded",
    "EngineServerError",
    "EngineConnectionError",
]


class MemoryEngineError(Exception):
    """Base error. Carries the HTTP status and the engine's response body."""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 detail: Any = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail
        self.body = body


class InvalidInput(MemoryEngineError):
    """400 / 422 — bad parameters or contract violation (e.g. missing context)."""


class NotFound(MemoryEngineError):
    """404 — unknown memory id (or unmounted route, see EndpointNotAvailable)."""


class EndpointNotAvailable(NotFound):
    """The route exists in the documented API surface but is not mounted on this
    engine build (e.g. freshness digest on engines that run freshness host-side,
    or attachments/graph before those routers ship). Forward-compatible: the same
    call starts working when the engine adds the route."""


class Conflict(MemoryEngineError):
    """409 — e.g. in-place rewrite of an entry that has been superseded."""


class EngineOverloaded(MemoryEngineError):
    """503 — all recall routes failed. ``retryable`` / ``degraded`` come from the
    engine body so callers can implement their own backoff policy."""

    def __init__(self, message: str, *, retryable: bool = False,
                 degraded: bool = True, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retryable = retryable
        self.degraded = degraded


class EngineServerError(MemoryEngineError):
    """Unexpected 5xx from the engine."""


class EngineConnectionError(MemoryEngineError):
    """Could not reach the engine at all (connection refused / timed out)."""
