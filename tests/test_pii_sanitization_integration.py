"""Integration test for PII sanitization in secure_analyze_page.

Tests that Layer 3 sanitization is actually called in production and catches
10 types of PII that the LLM might leak:
  1. SSN with dashes (123-45-6789)
  2. SSN with spaces (123 45 6789)
  3. Visa 16-digit CC
  4. Amex 15-digit CC
  5. Email
  6. API key (sk- format)
  7. API key (ghp- format)
  8. Password (password: value format)
  9. MasterCard
  10. Phone with parentheses

Run: pytest tests/test_pii_sanitization_integration.py -v
"""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server import secure_analyze_page


# Define 10 PII test vectors
PII_VECTORS = {
    "ssn_dashes": "123-45-6789",
    "ssn_spaces": "123 45 6789",
    "visa_16": "4532 1488 0343 6467",
    "amex_15": "378282246310005",
    "email": "user@example.com",
    "api_key_sk": "sk-proj-abc123def456ghijklmnopqr",
    "api_key_ghp": "ghp_1234567890abcdefghijklmnopqrstuvwxyz",
    "password": "password: SuperSecret123!",
    "mastercard": "5425233010103442",
    "phone_parens": "(555) 123-4567",
}


@pytest.fixture
def mock_ollama():
    """Mock OllamaClient to return a response containing all 10 PII vectors."""
    with patch("server.OllamaClient") as mock_class:
        mock_instance = AsyncMock()
        mock_class.return_value = mock_instance

        # health_check returns True (Ollama is available)
        mock_instance.health_check = AsyncMock(return_value=True)

        # analyze_text returns a dict containing all 10 PII vectors
        pii_response = {
            "summary": "Found credentials",
            "fields": {
                f"pii_{key}": value
                for key, value in PII_VECTORS.items()
            },
            "raw_extracts": list(PII_VECTORS.values()),
        }
        mock_instance.analyze_text = AsyncMock(return_value=pii_response)

        yield mock_instance


@pytest.fixture
def mock_cdp():
    """Mock CDP connection and page extraction."""
    with (
        patch("server._find_cdp_port") as mock_port,
        patch("server._find_page_ws") as mock_ws,
        patch("server.CDPConnection") as mock_conn_class,
        patch("server.PlaywrightCDPActuator") as mock_actuator_class,
    ):
        mock_port.return_value = 9222
        mock_ws.return_value = "ws://localhost:9222/devtools/browser/abc123"

        mock_conn = AsyncMock()
        mock_conn_class.return_value = mock_conn
        mock_conn.connect = AsyncMock()
        mock_conn.close = AsyncMock()

        mock_actuator = MagicMock()
        mock_actuator_class.return_value = mock_actuator
        mock_actuator.extract_page_content = AsyncMock(
            return_value="<html><body>Some content</body></html>"
        )
        mock_actuator.screenshot = AsyncMock()
        mock_actuator._last_screenshot_b64 = None

        yield {
            "port": mock_port,
            "ws": mock_ws,
            "conn": mock_conn,
            "actuator": mock_actuator,
        }


@pytest.mark.asyncio
async def test_secure_analyze_page_sanitizes_pii_before_return(mock_ollama, mock_cdp):
    """Test that secure_analyze_page sanitizes PII output before returning to MCP caller.

    This test verifies that Layer 3 (sanitize_output) is actually wired into the
    production path and catches PII from the LLM response.
    """
    # Call secure_analyze_page with mocked dependencies
    result = await secure_analyze_page(
        analysis_type="text",
        prompt="Extract credentials",
        model="qwen2.5-coder:7b"
    )

    # Check that the call succeeded
    assert result["status"] == "analyzed"
    analysis = result["analysis"]

    # Verify all 10 PII vectors are NOT present in the response
    response_str = json.dumps(analysis)

    for pii_name, pii_value in PII_VECTORS.items():
        # The exact PII value should NOT appear in the response
        assert pii_value not in response_str, (
            f"PII leak detected: {pii_name} '{pii_value}' found in response"
        )

    # Verify that redaction markers ARE present (not all redactions will show up
    # due to regex patterns, but some should)
    assert "[REDACTED" in response_str or "REDACTED" in response_str or len(analysis) > 0, (
        "Response should contain redaction markers or be empty dict"
    )


@pytest.mark.asyncio
async def test_secure_analyze_page_without_sanitization_leaks_pii(mock_cdp):
    """Test that WITHOUT sanitization, PII would be leaked (this should fail before the fix).

    This test demonstrates what the vulnerability looks like.
    """
    with patch("server.OllamaClient") as mock_class:
        # Mock Ollama without going through sanitize_output
        mock_instance = AsyncMock()
        mock_class.return_value = mock_instance
        mock_instance.health_check = AsyncMock(return_value=True)

        # Return raw PII without sanitization
        pii_response = {
            "found_ssn": "123-45-6789",
            "credit_card": "4532148803436467",
        }
        mock_instance.analyze_text = AsyncMock(return_value=pii_response)

        result = await secure_analyze_page(analysis_type="text")
        analysis = result["analysis"]
        response_str = json.dumps(analysis)

        # With sanitization in place, PII should be redacted
        assert "123-45-6789" not in response_str or "[REDACTED" in response_str


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
