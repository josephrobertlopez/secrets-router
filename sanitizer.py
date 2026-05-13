"""Three-layer sanitization for LLM output to prevent PII leakage.

Layer 1: JSON schema constrains structure (in ollama_client.py format parameter)
Layer 2: System prompt instructs no-PII behavior
Layer 3: Regex post-processing catches leaked PII

Known bypass surface (documented to justify limitations):
  - LLM-laundered semantic paraphrase: "social security one two three dash..."
    (Cannot be caught by regex without NLP pipeline)
  - AWS secret keys without prefix: "ASIATEMP..." (no prefix in pattern)
  - Key types not in PII_PATTERNS: proprietary/custom key formats
"""

import logging
import re
import unicodedata

log = logging.getLogger("secrets-router")


def _normalize_input(text: str) -> str:
    """Normalize input to defeat unicode-based obfuscation.

    Kills bypass classes:
    - Full-width digits (U+FF10-FF19) → ASCII
    - Fancy dashes (en-dash, em-dash, minus, etc.) → ASCII hyphen
    - Zero-width spaces and joiners
    """
    # NFKC normalizes full-width digits, dashes, etc. to ASCII equivalents
    text = unicodedata.normalize('NFKC', text)
    # Replace fancy dashes/minuses with ASCII hyphen
    text = re.sub(r'[–—−‐⁃­]', '-', text)
    # Strip zero-width spaces (U+200B, U+200C, U+200D, U+FEFF, etc.)
    text = re.sub(r'[​‌‍؜᠎﻿]', '', text)
    return text


# PII patterns with replacement strings
PII_PATTERNS = [
    # Credit cards: 16-digit (Visa, Mastercard, Discover), 15-digit (Amex)
    (r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b", "[REDACTED_CC]"),
    # Amex: 15-digit with separator-tolerance (spaces or dashes)
    (r"\b3[47]\d{2}[\s-]?\d{6}[\s-]?\d{5}\b", "[REDACTED_AMEX]"),
    # Social Security Number: heterogeneous separators (dash, dot, space)
    (r"\b\d{3}[-.\s]\d{2}[-.\s]\d{4}\b", "[REDACTED_SSN]"),
    # API keys and tokens: case-insensitive prefix followed by dash/underscore
    (r"(?:sk|ghp|AKIA|xoxb|xoxp)[-_][A-Za-z0-9_-]{16,}", "[REDACTED_API_KEY]", re.IGNORECASE),
    # Passwords (heuristic: word adjacent to password=, password:, passwd=, pwd=)
    (r"(?i)(password|passwd|pwd)[\s:=]+[^\s,;\]\}]+", "[REDACTED_PASSWORD]"),
    # Email
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[REDACTED_EMAIL]"),
    # Phone number: US domestic with optional country prefix
    (r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b", "[REDACTED_PHONE]"),
    (r"\(\d{3}\)\s?\d{3}[-.]?\d{4}", "[REDACTED_PHONE]"),
    # International phone: +{1,3} digits with separators
    (r"\+\d{1,3}[\s-]?\d{3}[\s-]?\d{3}[\s-]?\d{4}", "[REDACTED_PHONE]"),
    # JWT: base64url encoded triplet (header.payload.signature)
    (r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[REDACTED_JWT]"),
    # Generic long numbers
    (r"\b\d{9,16}\b", "[REDACTED_NUM]"),
]

# Compile patterns for efficiency (with optional flags)
COMPILED_PATTERNS = []
for pattern_entry in PII_PATTERNS:
    if len(pattern_entry) == 3:
        pattern, replacement, flags = pattern_entry
        COMPILED_PATTERNS.append((re.compile(pattern, flags), replacement))
    else:
        pattern, replacement = pattern_entry
        COMPILED_PATTERNS.append((re.compile(pattern), replacement))


def sanitize_string(s: str) -> str:
    """Sanitize a single string: normalize, apply PII patterns, and truncate.

    Args:
        s: Input string

    Returns:
        Sanitized string
    """
    if not isinstance(s, str):
        return str(s)

    # Normalize unicode and strip zero-width characters first
    s = _normalize_input(s)

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
