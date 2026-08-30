"""Transport-independent domain primitives."""

from .errors import ErrorCode, ErrorInfo, RetryDecision, RetryPolicy, classify_error, safe_traceback

__all__ = ["ErrorCode", "ErrorInfo", "RetryDecision", "RetryPolicy", "classify_error", "safe_traceback"]
