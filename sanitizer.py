"""Three-layer sanitization for LLM output to prevent PII leakage.

Layer 1: JSON schema constrains structure (in ollama_client.py format parameter)
Layer 2: System prompt instructs no-PII behavior
Layer 3: Regex post-processing catches leaked PII
"""

import logging
import re

log = logging.getLogger("secrets-router")

# PII patterns with replacement strings
PII_PATTERNS = [
    # Credit cards: 16-digit (Visa, Mastercard, Discover), 15-digit (Amex)
    (r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b", "[REDACTED_CC]"),
    (r"\b3[47][0-9]{13}\b", "[REDACTED_AMEX]"),
    # Social Security Number: dashes, spaces, or dots
    (r"\b\d{3}-\d{2}-\d{4}\b", "[REDACTED_SSN]"),
    (r"\b\d{3}\s\d{2}\s\d{4}\b", "[REDACTED_SSN]"),
    (r"\b\d{3}\.\d{2}\.\d{4}\b", "[REDACTED_SSN]"),
    # API keys and tokens (prefix followed by dash or underscore, then alphanumeric)
    (r"(?:sk|ghp|AKIA|xoxb|xoxp)[-_][A-Za-z0-9_-]{16,}", "[REDACTED_API_KEY]"),
    # Passwords (heuristic: word adjacent to password=, password:, passwd=, pwd=)
    (r"(?i)(password|passwd|pwd)[\s:=]+[^\s,;\]\}]+", "[REDACTED_PASSWORD]"),
    # Email
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[REDACTED_EMAIL]"),
    # Phone number with parentheses or dashes
    (r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b", "[REDACTED_PHONE]"),
    (r"\(\d{3}\)\s?\d{3}[-.]?\d{4}", "[REDACTED_PHONE]"),
    # Generic long numbers
    (r"\b\d{9,16}\b", "[REDACTED_NUM]"),
]

# Compile patterns for efficiency
COMPILED_PATTERNS = [(re.compile(pattern), replacement) for pattern, replacement in PII_PATTERNS]


def sanitize_string(s: str) -> str:
    """Sanitize a single string: apply PII patterns and truncate.

    Args:
        s: Input string

    Returns:
        Sanitized string
    """
    if not isinstance(s, str):
        return str(s)

    # Apply all PII patterns
    for pattern, replacement in COMPILED_PATTERNS:
        s = pattern.sub(replacement, s)

    # Truncate to 500 chars max
    if len(s) > 500:
        s = s[:497] + "..."

    return s


def sanitize_output(analysis: dict) -> dict:
    """Recursively sanitize LLM output dict.

    Applies PII patterns to all strings, caps arrays at 20 items,
    truncates strings to 500 chars.

    Args:
        analysis: Dict from LLM (may contain nested dicts/lists)

    Returns:
        Sanitized copy of dict
    """
    if not isinstance(analysis, dict):
        return analysis

    result = {}

    for key, value in analysis.items():
        if isinstance(value, str):
            # Sanitize string
            result[key] = sanitize_string(value)
        elif isinstance(value, list):
            # Cap at 20 items, recurse
            capped = value[:20]
            result[key] = [
                sanitize_output(item)
                if isinstance(item, dict)
                else sanitize_string(item)
                if isinstance(item, str)
                else item
                for item in capped
            ]
        elif isinstance(value, dict):
            # Recurse into nested dict
            result[key] = sanitize_output(value)
        else:
            # Keep as-is (int, bool, null, etc.)
            result[key] = value

    return result
