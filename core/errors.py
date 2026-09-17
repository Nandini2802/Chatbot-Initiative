"""
Shared exception hierarchy.

All custom exceptions inherit from DanubeError. HTTP status codes are assigned
at the API middleware layer — never inside business logic.
"""
from __future__ import annotations


class DanubeError(Exception):
    """Base exception for all Danube AI errors."""


class AuthenticationError(DanubeError):
    """Raised when JWT validation fails."""


class AuthorizationError(DanubeError):
    """Raised when a user lacks permission to perform an action."""


class NotFoundError(DanubeError):
    """Raised when a requested resource does not exist."""


class ValidationError(DanubeError):
    """Raised when input data is structurally invalid."""


class StorageError(DanubeError):
    """Raised on blob storage failures."""


class DatabaseError(DanubeError):
    """Raised on unrecoverable database errors."""


class LLMError(DanubeError):
    """Raised when the LLM call fails or returns an unusable response."""


class RateLimitError(DanubeError):
    """Raised when a rate limit is exceeded."""


class AgentError(DanubeError):
    """Raised when the LangGraph agent encounters an unrecoverable error."""


class SalesforceError(DanubeError):
    """Raised on Salesforce API failure (timeout, non-2xx, unexpected response)."""


class InventoryUnavailableError(SalesforceError):
    """Circuit-breaker: API quota too low to safely serve a live inventory call."""
