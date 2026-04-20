"""Backward-compatible wrapper \u2014 all logic moved to llm_router.py."""
from modules.llm_router import (  # noqa: F401
    LLMError,
    complete,
    format_provider_status,
    get_cost_estimate,
    route_request,
)

__all__ = [
    "LLMError",
    "complete",
    "format_provider_status",
    "get_cost_estimate",
    "route_request",
]
