"""memory-engine client SDK — typed Python wrapper over the engine's /v1 HTTP API."""

from .client import HealthStatus, MemoriesPage, MemoryEngine, RecallHit, RecallResponse
from .exceptions import (
    Conflict,
    EndpointNotAvailable,
    EngineConnectionError,
    EngineOverloaded,
    EngineServerError,
    InvalidInput,
    MemoryEngineError,
    NotFound,
)

__version__ = '0.1.0'

__all__ = [
    'MemoryEngine',
    'RecallHit',
    'RecallResponse',
    'MemoriesPage',
    'HealthStatus',
    'MemoryEngineError',
    'InvalidInput',
    'NotFound',
    'EndpointNotAvailable',
    'Conflict',
    'EngineOverloaded',
    'EngineServerError',
    'EngineConnectionError',
    '__version__',
]
