"""Unified HTTP client for Ollama /api/chat endpoint with structured output support.

Supports:
- Text analysis with custom schemas (secrets-router pattern)
- Vision/image analysis with base64 images
- RAG generation with optional prompts
- Health checks and backend availability

All blocking HTTP operations are async-safe via run_in_executor.

This is a vendored copy of shared.ml.ollama for the standalone secrets-router
repo. Keep in sync with monorepo-v2/packages/shared/src/shared/ml/ollama.py
if/when that diverges.
"""

import asyncio
import json
import logging
import os
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


# Default system prompts for common use cases

ANALYSIS_SYSTEM_PROMPT = """You are a web page content analyzer. Your task is to analyze page content and return structured JSON.

Guidelines:
- Group similar items into categories rather than enumerating each item
- Never include account numbers, names, email addresses, phone numbers, or financial values in your response
- Use generic descriptions (e.g., "login form with email and password fields" instead of listing specific field names)
- Keep summary under 200 characters
- Suggest concrete next actions based on page content
- Return JSON matching the schema exactly"""

RAG_SYSTEM_PROMPT = """You are a helpful assistant analyzing a personal zettelkasten (note collection).

Based on the provided context from notes, answer questions comprehensively and empathetically.
Be specific and reference note IDs when relevant."""


# Default schemas for common use cases

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "page_type": {
            "type": "string",
            "enum": [
                "login",
                "form",
                "content",
                "search",
                "error",
                "captcha",
                "dashboard",
                "other",
            ],
            "description": "Classification of page type",
        },
        "summary": {
            "type": "string",
            "description": "Brief summary of page content and purpose",
            "maxLength": 500,
        },
        "forms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "purpose": {"type": "string", "description": "What the form is for"},
                    "field_count": {"type": "integer"},
                },
            },
            "maxItems": 5,
        },
        "links": {
            "type": "array",
            "items": {"type": "object", "properties": {"category": {"type": "string"}}},
            "maxItems": 8,
        },
        "action_suggestions": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 5,
            "description": "Next steps for this page",
        },
        "custom_analysis": {
            "type": "object",
            "description": "Response to user's specific analysis request, if any. Structure freely.",
        },
        "content_safety": {
            "type": "object",
            "properties": {
                "contains_pii": {"type": "boolean"},
                "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
            },
        },
    },
    "required": ["page_type", "summary"],
}

VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "page_type": {
            "type": "string",
            "enum": [
                "login",
                "form",
                "content",
                "search",
                "error",
                "captcha",
                "dashboard",
                "other",
            ],
        },
        "captcha_detected": {
            "type": "boolean",
            "description": "Whether a CAPTCHA element is visible",
        },
        "visible_forms": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "buttons": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        "error_messages": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "visual_description": {
            "type": "string",
            "description": "Visual layout and appearance",
            "maxLength": 500,
        },
    },
    "required": ["page_type", "captcha_detected"],
}

IMAGE_TAG_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "maxLength": 200,
            "description": "Brief description of the image content",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 20,
            "description": "Extracted tags/labels for the image",
        },
        "colors": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 5,
            "description": "Primary colors in the image",
        },
        "style": {
            "type": "string",
            "description": "Art style or visual style (e.g., 'digital', 'photograph', 'drawing')",
        },
    },
    "required": ["description", "tags"],
}


class OllamaClient:
    """Unified HTTP client for Ollama with text, vision, and RAG support.

    Supports:
    - Text analysis with custom JSON schemas
    - Vision/image analysis with base64 images
    - RAG generation with context retrieval
    - Health checks for backend availability

    All HTTP operations are async-safe via asyncio.run_in_executor.
    Configuration via environment variables:
    - OLLAMA_HOST: Ollama API endpoint (default: http://localhost:11434)
    - OLLAMA_MODEL: Default text model (default: qwen2.5-coder:7b)
    - OLLAMA_VISION_MODEL: Vision model (default: llama3.2-vision:11b)
    - OLLAMA_IMAGE_MODEL: Image tagging model (default: llava:latest)
    """

    def __init__(
        self,
        base_url: str | None = None,
        default_model: str | None = None,
        vision_model: str | None = None,
        image_model: str | None = None,
    ):
        """Initialize Ollama client with optional overrides.

        Args:
            base_url: Ollama API endpoint. Falls back to OLLAMA_HOST env var.
            default_model: Default text model. Falls back to OLLAMA_MODEL env var.
            vision_model: Vision model name. Falls back to OLLAMA_VISION_MODEL env var.
            image_model: Image tagging model. Falls back to OLLAMA_IMAGE_MODEL env var.
        """
        self.base_url = base_url or os.getenv(
            "OLLAMA_HOST", "http://localhost:11434"
        )
        self.default_model = default_model or os.getenv(
            "OLLAMA_MODEL", "qwen2.5-coder:7b"
        )
        self.vision_model = vision_model or os.getenv(
            "OLLAMA_VISION_MODEL", "llama3.2-vision:11b"
        )
        self.image_model = image_model or os.getenv(
            "OLLAMA_IMAGE_MODEL", "llava:latest"
        )

    async def analyze_text(
        self,
        page_content: dict,
        model: str | None = None,
        prompt: str | None = None,
        schema: dict | None = None,
        system_prompt: str | None = None,
    ) -> dict:
        """Analyze page content text via LLM."""
        model = model or self.default_model
        schema = schema or ANALYSIS_SCHEMA
        system_prompt = system_prompt or ANALYSIS_SYSTEM_PROMPT

        page_text = page_content.get("text", "")[:8000]
        page_title = page_content.get("title", "")
        page_url = page_content.get("url", "")

        if prompt:
            user_message = f"""USER REQUEST: {prompt}

Analyze this page to answer the user's request above. Put your detailed response in the "custom_analysis" field.

Title: {page_title}
URL: {page_url}

Content:
{page_text}

Return JSON with page_type, summary, AND custom_analysis addressing the user's request."""
        else:
            user_message = f"""Analyze this page:

Title: {page_title}
URL: {page_url}

Content:
{page_text}

Return JSON analysis."""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        try:
            return await self._call_ollama(
                model=model,
                messages=messages,
                schema=schema,
            )
        except Exception as e:
            logger.error("Text analysis failed: %s", e)
            raise

    async def analyze_vision(
        self,
        base64_image: str,
        model: str | None = None,
        prompt: str | None = None,
        schema: dict | None = None,
    ) -> dict:
        """Analyze screenshot via vision model."""
        model = model or self.vision_model
        schema = schema or VISION_SCHEMA

        user_prompt = (
            prompt
            or "Analyze this screenshot. Identify the page type, check for CAPTCHAs, list visible forms and buttons, note any error messages, and describe the visual layout."
        )

        messages = [
            {
                "role": "user",
                "content": user_prompt,
                "images": [base64_image],
            }
        ]

        try:
            return await self._call_ollama(
                model=model,
                messages=messages,
                schema=schema,
                timeout=60,
            )
        except Exception as e:
            logger.error("Vision analysis failed: %s", e)
            raise

    async def tag_image(
        self,
        base64_image: str,
        model: str | None = None,
        prompt: str | None = None,
        schema: dict | None = None,
    ) -> dict:
        """Tag an image via vision model."""
        model = model or self.image_model
        schema = schema or IMAGE_TAG_SCHEMA

        user_prompt = (
            prompt
            or "Analyze this image. Provide a brief description, extract 3-8 tags describing the content, identify primary colors, and note the visual style."
        )

        messages = [
            {
                "role": "user",
                "content": user_prompt,
                "images": [base64_image],
            }
        ]

        try:
            return await self._call_ollama(
                model=model,
                messages=messages,
                schema=schema,
                timeout=60,
            )
        except Exception as e:
            logger.error("Image tagging failed: %s", e)
            raise

    async def generate(
        self,
        prompt: str,
        model: str | None = None,
        system_prompt: str | None = None,
        max_tokens: int = 512,
        timeout: float = 30,
    ) -> str:
        """Generate text via LLM (RAG/generation use case)."""
        model = model or self.default_model

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        try:
            response = await self._call_ollama(
                model=model,
                messages=messages,
                schema=None,
                timeout=timeout,
                max_tokens=max_tokens,
            )
            if isinstance(response, dict) and "content" in response:
                return response["content"]
            return str(response)
        except Exception as e:
            logger.error("Generation failed: %s", e)
            raise

    async def health_check(self) -> bool:
        """Check if Ollama is healthy."""
        try:

            def check():
                try:
                    req = Request(f"{self.base_url}/api/tags", method="GET")
                    with urlopen(req, timeout=2) as resp:
                        return resp.status == 200
                except URLError:
                    return False

            result = await asyncio.get_running_loop().run_in_executor(None, check)
            return result
        except Exception as e:
            logger.warning("Ollama health check failed: %s", e)
            return False

    async def _call_ollama(
        self,
        model: str,
        messages: list,
        schema: dict | None = None,
        timeout: float = 30,
        max_tokens: int = 1024,
    ) -> Any:
        """Make HTTP request to Ollama /api/chat."""

        def make_request():
            payload = {
                "model": model,
                "messages": messages,
                "stream": False,
                "options": {
                    "temperature": 0,
                    "num_predict": max_tokens,
                },
            }

            if schema:
                payload["format"] = schema

            req = Request(
                f"{self.base_url}/api/chat",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urlopen(req, timeout=timeout) as resp:
                response_text = resp.read().decode("utf-8")
                response = json.loads(response_text)
                content = response.get("message", {}).get("content", "")

                if schema and content:
                    try:
                        return json.loads(content)
                    except json.JSONDecodeError:
                        logger.warning(
                            "Failed to parse schema-constrained response as JSON: %s",
                            content,
                        )
                        return {"error": "Invalid JSON response", "raw": content}

                return content

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, make_request)


__all__ = [
    "OllamaClient",
    "ANALYSIS_SCHEMA",
    "ANALYSIS_SYSTEM_PROMPT",
    "VISION_SCHEMA",
    "IMAGE_TAG_SCHEMA",
]
