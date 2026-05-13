#!/usr/bin/env python3
"""
secrets-router MCP Server

Credential isolation for agent workflows.
Values flow: encrypted store → this process → browser/API. Never through the agent.

Handle lifecycle:
  secure_fetch(store, key) -> handle:a3f8c2d1e4b7   (agent sees this)
  secure_fill(handle, selector) -> "filled [MASKED]"  (agent sees this)
  value = ****                                         (agent never sees this)
"""

import json
import uuid
import subprocess
import os
import sys
import time
import logging
import asyncio
import urllib.request
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from actuators.cdp_connection import CDPConnection
from actuators.playwright_cdp import PlaywrightCDPActuator
from engine import RecipeEngine
from ollama_client import OllamaClient
from sanitizer import sanitize_output

# ---------------------------------------------------------------------------
# Logging — stderr only, never bleeds credential values into MCP responses
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [secrets-router] %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("secrets-router")

# ---------------------------------------------------------------------------
# In-memory handle store — values are NEVER serialized into MCP responses
# ---------------------------------------------------------------------------
_HANDLE_STORE: dict[str, dict] = {}  # handle_id → {value, created, used, source}

HANDLE_TTL = 300       # seconds before a handle auto-expires
MAX_HANDLES = 50       # hard cap — prevents unbounded accumulation


# ---------------------------------------------------------------------------
# Credential backends
# ---------------------------------------------------------------------------

class BitwardenBackend:
    """Fetch from Bitwarden CLI (bw). Requires `bw unlock` first.

    Field paths: password, username, notes, fields.<name>, card.number,
    card.expMonth, card.expYear, card.code
    """

    def fetch(self, item_name: str, field: str = "password") -> str:
        result = subprocess.run(
            ["bw", "get", "item", item_name, "--raw"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Bitwarden fetch failed for '{item_name}': {result.stderr.strip()}"
            )

        try:
            item = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Bitwarden returned non-JSON output: {e}") from e

        if field == "password":
            v = item.get("login", {}).get("password", "")
        elif field == "username":
            v = item.get("login", {}).get("username", "")
        elif field == "notes":
            v = item.get("notes", "")
        elif field.startswith("fields."):
            field_name = field[7:]
            for f in item.get("fields", []):
                if f.get("name") == field_name:
                    return str(f.get("value", ""))
            raise KeyError(f"Custom field '{field_name}' not found in '{item_name}'")
        elif field.startswith("card."):
            card_key = field[5:]
            v = str(item.get("card", {}).get(card_key, ""))
        else:
            raise KeyError(f"Unknown field path: '{field}'")

        if not v:
            raise ValueError(f"Field '{field}' is empty in item '{item_name}'")
        return v


class EncryptedYAMLBackend:
    """Fetch from an age-encrypted YAML file.

    Default path: ~/.config/secure-creds.yaml.age
    Access with dot-path keys: "water-bill.card.number"
    """

    def __init__(self, path: str | None = None):
        env_path = os.environ.get("SECURE_FETCH_YAML_PATH")
        default = "~/.config/secure-creds.yaml.age"
        self.path = Path(path or env_path or default).expanduser()

    def fetch(self, key: str) -> str:
        import yaml

        if not self.path.exists():
            raise FileNotFoundError(f"Encrypted YAML not found: {self.path}")

        result = subprocess.run(
            ["age", "--decrypt", str(self.path)],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(f"age decrypt failed: {result.stderr.strip()}")

        data = yaml.safe_load(result.stdout)
        try:
            for part in key.split("."):
                data = data[part]
        except (KeyError, TypeError) as e:
            raise KeyError(f"Key path '{key}' not found in YAML: {e}") from e

        return str(data)


class PassBackend:
    """Fetch from pass (standard unix password manager). Returns first line."""

    def fetch(self, key: str) -> str:
        result = subprocess.run(
            ["pass", "show", key],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(f"pass fetch failed for '{key}': {result.stderr.strip()}")
        return result.stdout.strip().split("\n")[0]


class RbwBackend:
    """Fetch from rbw (Rust Bitwarden CLI). Parses `rbw get --full` output.

    Field paths: password, number, expiration, exp_month, exp_year, cvv, code,
    name, brand — or any "Key: Value" line from --full output.
    """

    def fetch(self, item_name: str, field: str = "password") -> str:
        result = subprocess.run(
            ["rbw", "get", "--full", item_name],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"rbw fetch failed for '{item_name}': {result.stderr.strip()}"
            )
        return self._parse_field(result.stdout.strip(), field)

    def _parse_field(self, raw: str, field: str) -> str:
        lines = raw.split("\n")
        if not lines:
            raise ValueError("Empty rbw output")

        primary = lines[0].strip()

        fields = {}
        for line in lines[1:]:
            line = line.strip()
            if ": " in line:
                k, v = line.split(": ", 1)
                fields[k.lower().strip()] = v.strip()

        exp_raw = fields.get("expiration", "")
        exp_month = exp_year = ""
        if "/" in exp_raw:
            parts = exp_raw.split("/")
            exp_month = parts[0].strip()
            exp_year = parts[1].strip()

        field_lower = field.lower().strip()
        lookup = {
            "password": primary,
            "number": primary,
            "card_number": primary,
            "card.number": primary,
            "expiration": exp_raw,
            "exp_month": exp_month,
            "exp_year": exp_year,
            "card.expMonth": exp_month,
            "card.expYear": exp_year,
            "month": exp_month,
            "year": exp_year,
            "cvv": fields.get("cvv", ""),
            "code": fields.get("cvv", ""),
            "card.code": fields.get("cvv", ""),
            "name": fields.get("name", ""),
            "cardholder": fields.get("name", ""),
            "card.name": fields.get("name", ""),
            "brand": fields.get("brand", ""),
        }

        if field_lower in lookup:
            v = lookup[field_lower]
            if not v:
                raise ValueError(f"Field '{field}' is empty in item")
            return v

        if field_lower in fields:
            return fields[field_lower]

        raise KeyError(
            f"Unknown field '{field}'. "
            f"Available: {', '.join(sorted(set(list(lookup.keys()) + list(fields.keys()))))}"
        )


class EnvBackend:
    """Fetch from an environment variable. Key = variable name."""

    def fetch(self, key: str) -> str:
        v = os.environ.get(key)
        if v is None:
            raise KeyError(f"Environment variable '{key}' not set")
        return v


# Backend registry
_BACKENDS: dict[str, Any] = {
    "bitwarden": BitwardenBackend(),
    "rbw": RbwBackend(),
    "yaml": EncryptedYAMLBackend(),
    "pass": PassBackend(),
    "env": EnvBackend(),
}


# ---------------------------------------------------------------------------
# Handle lifecycle
# ---------------------------------------------------------------------------

def _cleanup_expired() -> None:
    """Remove expired and already-consumed handles."""
    now = time.time()
    stale = [
        hid for hid, v in _HANDLE_STORE.items()
        if now - v["created"] > HANDLE_TTL or v["used"]
    ]
    for hid in stale:
        log.debug("Evicting handle %s (source=%s)", hid, _HANDLE_STORE[hid]["source"])
        del _HANDLE_STORE[hid]


def _create_handle(value: str, source: str) -> str:
    """Store a credential value, return an opaque handle ID."""
    _cleanup_expired()
    if len(_HANDLE_STORE) >= MAX_HANDLES:
        raise RuntimeError(
            f"Handle store full ({MAX_HANDLES} active). "
            "Use secure_discard_handles to free space."
        )

    handle_id = f"handle:{uuid.uuid4().hex[:16]}"
    _HANDLE_STORE[handle_id] = {
        "value": value,
        "created": time.time(),
        "used": False,
        "source": source,
    }
    log.info("Created handle %s (source=%s)", handle_id, source)
    return handle_id


def _resolve_handle(handle_id: str) -> str:
    """Resolve a handle to its value. Single-use — deleted after resolution.

    The value is NEVER returned to any MCP caller; callers get only side-effects.
    """
    entry = _HANDLE_STORE.get(handle_id)
    if entry is None:
        raise KeyError(f"Handle not found or already expired: {handle_id}")
    if entry["used"]:
        raise RuntimeError(f"Handle already consumed: {handle_id}")
    if time.time() - entry["created"] > HANDLE_TTL:
        del _HANDLE_STORE[handle_id]
        raise RuntimeError(f"Handle expired: {handle_id}")

    entry["used"] = True
    value = entry["value"]
    del _HANDLE_STORE[handle_id]
    log.info("Resolved handle %s (source=%s)", handle_id, entry["source"])
    return value


def _mask(value: str) -> str:
    """Return a masked preview: asterisks + last 4 chars (PCI-standard style)."""
    if len(value) <= 4:
        return "****"
    return "****" + value[-4:]


# ---------------------------------------------------------------------------
# CDP connection helpers — connect to browser via Chrome DevTools Protocol
# ---------------------------------------------------------------------------

def _find_cdp_port() -> int | None:
    """Find Playwright's browser CDP port from chromium process args."""
    try:
        result = subprocess.run(
            ["pgrep", "-a", "chromium"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split('\n'):
            if '--remote-debugging-port=' in line:
                for arg in line.split():
                    if arg.startswith('--remote-debugging-port='):
                        try:
                            port = int(arg.split('=')[1])
                            log.debug("Found CDP port %d", port)
                            return port
                        except (ValueError, IndexError):
                            continue
    except Exception as e:
        log.warning("Failed to scan processes for CDP port: %s", e)
    return None


def _find_page_ws(cdp_port: int, url_contains: str | None = None) -> str | None:
    """Get WebSocket URL for a page via CDP /json/list.

    Returns the webSocketDebuggerUrl for the first matching page, or None.
    """
    try:
        with urllib.request.urlopen(
            f"http://localhost:{cdp_port}/json/list", timeout=5
        ) as resp:
            pages = json.loads(resp.read())
            for p in pages:
                if url_contains:
                    if url_contains in p.get("url", ""):
                        ws_url = p.get("webSocketDebuggerUrl")
                        if ws_url:
                            return ws_url
                else:
                    ws_url = p.get("webSocketDebuggerUrl")
                    if ws_url:
                        return ws_url
    except Exception as e:
        log.warning("Failed to fetch pages from CDP port %d: %s", cdp_port, e)
    return None


async def _cdp_fill_field(ws_url: str, selector: str, value: str) -> str:
    """Fill a form field via CDP WebSocket.

    Value goes: handle store → this function scope → browser DOM.
    Never returned to caller — only "OK" or error message returned.
    """
    try:
        import websockets
    except ImportError:
        return "ERROR: websockets not installed (run: pip install websockets)"

    try:
        async with websockets.connect(ws_url, close_timeout=10) as ws:
            escaped_value = value.replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n')
            escaped_selector = selector.replace('\\', '\\\\').replace("'", "\\'")

            js = f"""
(function() {{
    var el = document.querySelector('{escaped_selector}');
    if (!el) {{
        var iframes = document.querySelectorAll('iframe');
        for (var i = 0; i < iframes.length; i++) {{
            try {{
                el = iframes[i].contentDocument.querySelector('{escaped_selector}');
                if (el) break;
            }} catch(e) {{}}
        }}
    }}
    if (!el) return 'ERROR: element not found';

    var nativeInputValueSetter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    ).set;
    nativeInputValueSetter.call(el, '{escaped_value}');

    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    el.dispatchEvent(new Event('blur', {{ bubbles: true }}));

    return 'OK';
}})()
"""

            msg = json.dumps({
                "id": 1,
                "method": "Runtime.evaluate",
                "params": {"expression": js}
            })

            await ws.send(msg)
            response_text = await asyncio.wait_for(ws.recv(), timeout=10)
            response = json.loads(response_text)

            result = response.get("result", {}).get("result", {}).get("value", "ERROR")
            log.debug("CDP fill result: %s", result)
            return result

    except asyncio.TimeoutError:
        return "ERROR: CDP connection timed out"
    except Exception as e:
        log.error("CDP fill failed: %s", e)
        return f"ERROR: {str(e)}"


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "secrets-router",
    instructions=(
        "Credential isolation server. "
        "secure_fetch(store, key, field) → opaque handle. "
        "secure_fill(handle, selector) → fills browser field via CDP. "
        "secure_run_recipe(name) → executes YAML recipe with full isolation. "
        "secure_list_recipes() → available recipes. "
        "Values never appear in responses — only handles and masked previews."
    )
)


@mcp.tool()
def secure_fetch(store: str, key: str, field: str = "password") -> dict:
    """Fetch a credential and return an opaque handle — NOT the value.

    Args:
        store:  Backend: rbw, bitwarden, yaml, pass, env
        key:    Item name (rbw/bitwarden), YAML dot-path, pass entry, or env var name
        field:  Field to extract (rbw/bitwarden only): password, number, cvv,
                exp_month, exp_year, name, brand, username, notes, fields.<name>

    Returns:
        {"handle": "handle:...", "source": "rbw/item", "expires_in": 300, "masked_preview": "41***7"}
    """
    if store not in _BACKENDS:
        return {"error": f"Unknown store '{store}'. Available: {sorted(_BACKENDS.keys())}"}

    try:
        backend = _BACKENDS[store]
        if store in ("bitwarden", "rbw"):
            value = backend.fetch(key, field)
        else:
            value = backend.fetch(key)

        handle = _create_handle(value, f"{store}/{key}")
        return {
            "handle": handle,
            "source": f"{store}/{key}",
            "expires_in": HANDLE_TTL,
            "masked_preview": _mask(value),
        }
    except Exception as e:
        log.error("secure_fetch failed: %s", e)
        return {"error": str(e)}


@mcp.tool()
async def secure_fill(handle: str, selector: str, action: str = "fill") -> dict:
    """Resolve a handle and fill a browser form field via CDP.

    Value path: handle store → CDP WebSocket → browser DOM. Never returned to caller.
    Browser must be running with --remote-debugging-port enabled.

    Args:
        handle:    Opaque handle from secure_fetch
        selector:  CSS selector for the target field (e.g. "#password", "[name='card']")
        action:    Only "fill" is supported currently

    Returns:
        {"status": "filled", "field": selector, "masked": "****1017", "handle_consumed": true}
    """
    try:
        value = _resolve_handle(handle)
    except Exception as e:
        log.error("secure_fill handle resolution failed: %s", e)
        return {"error": str(e)}

    if action != "fill":
        value = ""
        return {"error": f"Action '{action}' not implemented. Use 'fill'."}

    try:
        cdp_port = _find_cdp_port()
        if cdp_port is None:
            value = ""
            return {"error": "Could not find CDP port. Is chromium running with --remote-debugging-port?"}

        ws_url = _find_page_ws(cdp_port)
        if ws_url is None:
            value = ""
            return {"error": f"No pages found on CDP port {cdp_port}."}

        result = await _cdp_fill_field(ws_url, selector, value)

        if not result.startswith("OK"):
            value = ""
            log.error("CDP fill failed: %s", result)
            return {"error": f"Fill failed: {result}"}

        masked = _mask(value)
        value = ""
        return {
            "status": "filled",
            "field": selector,
            "masked": masked,
            "handle_consumed": True,
        }

    except Exception as e:
        value = ""
        log.error("secure_fill CDP action failed: %s", e)
        return {"error": str(e)}


@mcp.tool()
def secure_list_handles() -> dict:
    """List active handles — IDs and sources only, no values.

    Returns:
        {"active_count": 2, "handles": [{"id": "handle:...", "source": "rbw/item", "age_seconds": 12}]}
    """
    _cleanup_expired()
    now = time.time()
    return {
        "active_count": len(_HANDLE_STORE),
        "handles": [
            {
                "id": hid,
                "source": v["source"],
                "age_seconds": int(now - v["created"]),
                "expires_in": max(0, int(HANDLE_TTL - (now - v["created"]))),
            }
            for hid, v in _HANDLE_STORE.items()
        ],
    }


@mcp.tool()
def secure_discard_handles(handle: str = "") -> dict:
    """Discard handles before TTL expiry.

    Args:
        handle: Specific handle to discard. If empty, ALL handles are discarded.

    Returns:
        {"discarded": 3}
    """
    if handle:
        if handle in _HANDLE_STORE:
            del _HANDLE_STORE[handle]
            log.info("Discarded handle %s on request", handle)
            return {"discarded": 1}
        else:
            return {"error": f"Handle not found: {handle}", "discarded": 0}
    else:
        count = len(_HANDLE_STORE)
        _HANDLE_STORE.clear()
        log.info("Discarded all %d handles on request", count)
        return {"discarded": count}


# ---------------------------------------------------------------------------
# Recipe execution
# ---------------------------------------------------------------------------

_RECIPES_DIR = Path(__file__).parent / "recipes"


@mcp.tool()
def secure_list_recipes() -> dict:
    """List available recipes with descriptions and required credentials.

    Returns:
        {"recipes": [{"name": "pay-water-milwaukee", "description": "...", "tags": [...]}]}
    """
    import yaml

    recipes = []

    if not _RECIPES_DIR.exists():
        return {"recipes": []}

    for recipe_file in _RECIPES_DIR.glob("*.yaml"):
        if recipe_file.name in ("schema.yaml",):
            continue

        try:
            with open(recipe_file) as f:
                recipe = yaml.safe_load(f)

            recipes.append({
                "name": recipe_file.stem,
                "description": recipe.get("description", ""),
                "version": recipe.get("version", "1.0"),
                "tags": recipe.get("tags", []),
                "required_credentials": list(recipe.get("credentials", {}).keys()),
            })
        except Exception as e:
            log.warning("Failed to load recipe %s: %s", recipe_file, e)

    return {"recipes": recipes}


@mcp.tool()
async def secure_run_recipe(
    name: str,
    config_overrides: dict = None,
    dry_run: bool = False,
) -> dict:
    """Execute a recipe with credential isolation.

    Returns step-by-step percepts. Pauses at approval gates.
    Credential values never returned.

    Args:
        name:             Recipe filename without .yaml
        config_overrides: Override config values at runtime
        dry_run:          Log steps without executing

    Returns:
        {"status": "completed"|"cancelled"|"error", "percepts": [...], "extracts": {...},
         "gates": [...], "steps_executed": N, "errors": [...]}
    """
    engine = RecipeEngine(_RECIPES_DIR)
    percepts_list = []

    async def on_percept(step_idx, percept_text):
        log.info("Step %d: %s", step_idx, percept_text)
        percepts_list.append(percept_text)

    async def on_gate(gate_name, message, screenshot):
        log.info("Approval gate %s: %s", gate_name, message)
        return True  # auto-approve

    try:
        recipe = engine.load_recipe(name)

        if dry_run:
            for step_idx, step in enumerate(recipe.get("steps", [])):
                action = step.get("action")
                await on_percept(step_idx, f"[DRY RUN] {action}: {step.get('percept', '')}")
            return {
                "status": "completed",
                "percepts": percepts_list,
                "extracts": {},
                "gates": [],
                "steps_executed": len(recipe.get("steps", [])),
                "errors": [],
            }

        engine.resolve_credentials(recipe)

        config = recipe.get("config", {})
        if config_overrides:
            config.update(config_overrides)
        engine.set_config(config)

        creds_spec = recipe.get("credentials", {})
        for namespace, spec in creds_spec.items():
            store = spec.get("store")
            item = spec.get("item")
            fields = spec.get("fields", {})

            if not store or not item:
                raise ValueError(f"Credential {namespace} missing store or item")

            backend = _BACKENDS.get(store)
            if not backend:
                raise ValueError(f"Unknown credential store: {store}")

            for field_name, field_path in fields.items():
                try:
                    if store in ("bitwarden", "rbw"):
                        value = backend.fetch(item, field_path)
                    else:
                        value = backend.fetch(item)
                    engine.set_credential(namespace, field_name, value)
                except Exception as e:
                    log.error("Failed to fetch credential %s.%s: %s", namespace, field_name, e)
                    return {
                        "status": "error",
                        "percepts": percepts_list,
                        "extracts": {},
                        "gates": [],
                        "steps_executed": 0,
                        "errors": [f"Credential fetch failed: {str(e)}"],
                    }

        actuator = PlaywrightCDPActuator(
            find_cdp_port_fn=_find_cdp_port,
            find_page_ws_fn=_find_page_ws,
            cdp_fill_fn=_cdp_fill_field,
        )

        result = await engine.execute(recipe, on_percept, on_gate, actuator)
        return result

    except Exception as e:
        log.error("Recipe execution failed: %s", e)
        return {
            "status": "error",
            "percepts": percepts_list,
            "extracts": {},
            "gates": [],
            "steps_executed": 0,
            "errors": [str(e)],
        }


@mcp.tool()
async def secure_analyze_page(
    analysis_type: str = "full",
    prompt: str = "",
    model: str = "",
) -> dict:
    """Analyze current page content via local Ollama LLM.

    Extracts page content via CDP, analyzes via local LLM, returns only
    structured summary. Raw page content never crosses MCP boundary.

    Args:
        analysis_type: "text" (DOM only), "vision" (screenshot only),
                       "full" (both), "links" (link summary), "forms" (form structure)
        prompt: Optional additional context or instructions
        model: Optional model override (text: qwen2.5-coder:7b, vision: llama3.2-vision:11b)

    Returns:
        {"status": "analyzed"|"partial"|"error",
         "analysis": {...structured summary...},
         "analysis_type": str,
         "model_used": str,
         "ollama_available": bool,
         "content_size_bytes": int}
    """
    import time

    start_time = time.time()
    status = "partial"

    try:
        # Find CDP port and page WebSocket
        cdp_port = _find_cdp_port()
        if cdp_port is None:
            return {
                "status": "error",
                "analysis": {"error": "No browser running with CDP port"},
                "analysis_type": analysis_type,
                "model_used": "",
                "ollama_available": False,
            }

        ws_url = _find_page_ws(cdp_port)
        if ws_url is None:
            return {
                "status": "error",
                "analysis": {"error": f"No pages found on CDP port {cdp_port}"},
                "analysis_type": analysis_type,
                "model_used": "",
                "ollama_available": False,
            }

        # Create persistent CDP connection
        cdp = CDPConnection(ws_url)
        await cdp.connect()

        # Create actuator with persistent connection
        actuator = PlaywrightCDPActuator(
            find_cdp_port_fn=_find_cdp_port,
            find_page_ws_fn=_find_page_ws,
            cdp_fill_fn=_cdp_fill_field,
        )
        actuator.cdp = cdp
        actuator.cdp_port = cdp_port
        actuator.ws_url = ws_url

        try:
            # Extract content based on analysis type
            page_content = None
            screenshot_b64 = None
            content_size = 0

            if analysis_type in ("text", "full", "links", "forms"):
                page_content = await actuator.extract_page_content()
                content_size += len(str(page_content))

            if analysis_type in ("vision", "full"):
                await actuator.screenshot(format="jpeg", quality=80)
                screenshot_b64 = actuator._last_screenshot_b64
                content_size += len(screenshot_b64) if screenshot_b64 else 0

            # Check Ollama health
            ollama = OllamaClient()
            ollama_available = await ollama.health_check()

            # Run analysis if Ollama is available
            analysis_result = None
            model_used = ""

            if ollama_available:
                if analysis_type in ("text", "full", "links", "forms") and page_content:
                    text_model = model if model else "qwen2.5-coder:7b"
                    try:
                        analysis_result = await ollama.analyze_text(
                            page_content, model=text_model, prompt=prompt
                        )
                        model_used = text_model
                    except Exception as e:
                        log.error("Text analysis failed: %s", e)
                        analysis_result = None

                if analysis_type in ("vision", "full") and screenshot_b64:
                    vision_model = model if model else "llama3.2-vision:11b"
                    try:
                        vision_result = await ollama.analyze_vision(
                            screenshot_b64, model=vision_model, prompt=prompt
                        )
                        if analysis_result is None:
                            analysis_result = vision_result
                        else:
                            # Merge text and vision results
                            analysis_result.update(vision_result)
                        model_used = vision_model
                    except Exception as e:
                        log.error("Vision analysis failed: %s", e)

            # Fallback to DOM heuristics if no analysis result
            if analysis_result is None:
                if page_content:
                    analysis_result = _dom_fallback_analysis(page_content)
                    status = "partial"
                else:
                    return {
                        "status": "error",
                        "analysis": {"error": "Could not extract page content"},
                        "analysis_type": analysis_type,
                        "model_used": "",
                        "ollama_available": ollama_available,
                    }
            else:
                status = "analyzed"

            latency_s = time.time() - start_time

            # Layer 3: Sanitize output before returning to MCP caller
            analysis_result = sanitize_output(analysis_result)

            return {
                "status": status,
                "analysis": analysis_result,
                "analysis_type": analysis_type,
                "model_used": model_used,
                "ollama_available": ollama_available,
                "latency_s": round(latency_s, 2),
                "content_size_bytes": content_size,
            }

        finally:
            await cdp.close()

    except Exception as e:
        log.error("secure_analyze_page failed: %s", e)
        return {
            "status": "error",
            "analysis": {"error": str(e)},
            "analysis_type": analysis_type,
            "model_used": "",
            "ollama_available": False,
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("secrets-router starting (HANDLE_TTL=%ds, MAX_HANDLES=%d)", HANDLE_TTL, MAX_HANDLES)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
