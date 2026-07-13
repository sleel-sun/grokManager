"""Reverse pipeline result classifier.

Maps upstream HTTP status codes and response bodies to a ResultCategory.
"""

from typing import Any

from app.dataplane.reverse.protocol.xai_usage import is_invalid_credentials_body

from .types import ResultCategory


def classify_result(
    status_code: int,
    body: str = "",
    *,
    payload: Any = None,
) -> ResultCategory:
    """Classify an upstream response into a ResultCategory.

    ``body`` is the raw response body (or first ~400 chars for error responses).
    ``payload`` is the parsed JSON, if available.
    """
    if status_code == 200:
        return ResultCategory.SUCCESS

    if status_code == 429:
        return ResultCategory.RATE_LIMITED

    if status_code == 401:
        return ResultCategory.AUTH_FAILURE

    if status_code == 400 and is_invalid_credentials_body(body):
        return ResultCategory.AUTH_FAILURE

    if status_code == 403:
        # Known blocked/invalid account markers take precedence.
        if is_invalid_credentials_body(body):
            return ResultCategory.AUTH_FAILURE
        # Check if the body indicates a Cloudflare challenge.
        text = (body or "").lower()
        if text and (
            "cf-challenge" in text
            or "cloudflare" in text
            or "request was blocked" in text
            or "you have been blocked" in text
        ):
            return ResultCategory.FORBIDDEN
        # Generic 403 (suspension, WAF, etc.) — not a credential issue.
        return ResultCategory.FORBIDDEN

    if status_code == 404:
        return ResultCategory.NOT_FOUND

    if status_code >= 500:
        return ResultCategory.UPSTREAM_5XX

    return ResultCategory.UNKNOWN


__all__ = ["classify_result"]
