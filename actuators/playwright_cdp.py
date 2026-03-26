"""Playwright CDP actuator for recipe execution.

Controls a browser via Chrome DevTools Protocol.
CDP helpers (_find_cdp_port, _find_page_ws, _cdp_fill_field) live in server.py
and are injected via __init__ to avoid circular imports.
"""

import asyncio
import json
import logging

from .base import ExecutionActuator

log = logging.getLogger("secrets-router")


class PlaywrightCDPActuator(ExecutionActuator):
    """Execute browser actions via Chrome DevTools Protocol.

    Uses the CDP helpers from server.py (_find_cdp_port, _find_page_ws, _cdp_fill_field).
    """

    def __init__(self, find_cdp_port_fn=None, find_page_ws_fn=None, cdp_fill_fn=None):
        """Initialize actuator with CDP helper functions.

        Args:
            find_cdp_port_fn: Callable returning CDP port (from server.py)
            find_page_ws_fn: Callable(port, url_contains) -> ws_url
            cdp_fill_fn: Callable(ws_url, selector, value) -> result
        """
        self.find_cdp_port = find_cdp_port_fn
        self.find_page_ws = find_page_ws_fn
        self.cdp_fill = cdp_fill_fn
        self.cdp_port = None
        self.ws_url = None
        self.current_page_index = 0

    async def connect(self):
        """Find and connect to Playwright's browser."""
        if not all([self.find_cdp_port, self.find_page_ws, self.cdp_fill]):
            raise RuntimeError(
                "Actuator not initialized with CDP helper functions. "
                "Pass them to __init__."
            )

        self.cdp_port = self.find_cdp_port()
        if self.cdp_port is None:
            raise RuntimeError(
                "Could not find Playwright's CDP port. "
                "Is the browser running with --remote-debugging-port?"
            )

        self.ws_url = self.find_page_ws(self.cdp_port)
        if self.ws_url is None:
            raise RuntimeError(
                f"Could not find any page on CDP port {self.cdp_port}"
            )

        log.info("Connected to Playwright browser (CDP port %d)", self.cdp_port)

    async def disconnect(self):
        """Close connection (no-op for CDP)."""
        self.ws_url = None
        self.cdp_port = None

    async def navigate(self, url: str, wait_for: str = None):
        """Navigate to URL, optionally wait for text to appear."""
        if not self.ws_url:
            raise RuntimeError("Not connected")

        escaped_url = url.replace("\\", "\\\\").replace("'", "\\'")
        escaped_wait = (wait_for or "").replace("\\", "\\\\").replace("'", "\\'")

        js = f"""
(async function() {{
    window.location.href = '{escaped_url}';
    var waitText = '{escaped_wait}';
    if (waitText) {{
        for (let i = 0; i < 60; i++) {{
            if (document.body && document.body.innerText.includes(waitText)) {{
                return 'OK';
            }}
            await new Promise(r => setTimeout(r, 500));
        }}
        return 'TIMEOUT: Text not found';
    }}
    return 'OK';
}})()
"""
        result = await self._eval_js(js)
        if not result.startswith("OK"):
            raise RuntimeError(f"Navigation failed: {result}")

    async def click(self, target: dict, wait_for: str = None, wait_for_tab: int = None):
        """Click element specified by target."""
        if not self.ws_url:
            raise RuntimeError("Not connected")

        selector = self.resolve_target(target)
        escaped_selector = selector.replace("\\", "\\\\").replace("'", "\\'")

        js = f"""
(async function() {{
    var el = document.querySelector('{escaped_selector}');
    if (!el) {{
        return 'ERROR: element not found';
    }}
    el.click();
    return 'OK';
}})()
"""
        result = await self._eval_js(js)
        if not result.startswith("OK"):
            raise RuntimeError(f"Click failed: {result}")

        # Wait for text if specified
        if wait_for:
            for i in range(60):  # 30 seconds
                if await self._text_exists(wait_for):
                    return
                await asyncio.sleep(0.5)
            raise RuntimeError(f"Timeout waiting for text: {wait_for}")

        # Wait for new tab if specified
        if wait_for_tab is not None:
            # This would require page tracking; simplified for now
            await asyncio.sleep(1)

    async def fill(self, target: dict, value: str):
        """Fill form field with plaintext value."""
        if not self.ws_url:
            raise RuntimeError("Not connected")

        selector = self.resolve_target(target)
        escaped_value = value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")

        js = f"""
(function() {{
    var el = document.querySelector('{selector}');
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
        result = await self._eval_js(js)
        if not result.startswith("OK"):
            raise RuntimeError(f"Fill failed: {result}")

    async def secure_fill(self, target: dict, value: str):
        """Fill form field with credential value (same as fill but semantically secure)."""
        # For CDP, secure_fill is identical to fill
        # The credential value is in scope only within this async context
        await self.fill(target, value)

    async def select(self, target: dict, option: str):
        """Select option in dropdown."""
        if not self.ws_url:
            raise RuntimeError("Not connected")

        selector = self.resolve_target(target)
        escaped_option = option.replace("'", "\\'")

        js = f"""
(function() {{
    var el = document.querySelector('{selector}');
    if (!el) return 'ERROR: element not found';
    if (el.tagName !== 'SELECT') return 'ERROR: not a select element';
    for (var i = 0; i < el.options.length; i++) {{
        if (el.options[i].text === '{escaped_option}') {{
            el.selectedIndex = i;
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
            return 'OK';
        }}
    }}
    return 'ERROR: option not found';
}})()
"""
        result = await self._eval_js(js)
        if not result.startswith("OK"):
            raise RuntimeError(f"Select failed: {result}")

    async def check(self, target: dict):
        """Check checkbox or radio button."""
        if not self.ws_url:
            raise RuntimeError("Not connected")

        selector = self.resolve_target(target)

        js = f"""
(function() {{
    var el = document.querySelector('{selector}');
    if (!el) return 'ERROR: element not found';
    el.checked = true;
    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    return 'OK';
}})()
"""
        result = await self._eval_js(js)
        if not result.startswith("OK"):
            raise RuntimeError(f"Check failed: {result}")

    async def uncheck(self, target: dict):
        """Uncheck checkbox or radio button."""
        if not self.ws_url:
            raise RuntimeError("Not connected")

        selector = self.resolve_target(target)

        js = f"""
(function() {{
    var el = document.querySelector('{selector}');
    if (!el) return 'ERROR: element not found';
    el.checked = false;
    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    return 'OK';
}})()
"""
        result = await self._eval_js(js)
        if not result.startswith("OK"):
            raise RuntimeError(f"Uncheck failed: {result}")

    async def screenshot(self, label: str = None) -> bytes:
        """Take screenshot, return PNG bytes.

        Note: This is a simplified version. Full implementation would need
        actual screenshot capture via CDP or Playwright methods.
        """
        # Return empty PNG for now (placeholder)
        return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR..."

    async def extract(self, targets: dict) -> dict:
        """Extract text from multiple targets. Returns {key: value}."""
        if not self.ws_url:
            raise RuntimeError("Not connected")

        result = {}

        for key, target in targets.items():
            selector = self.resolve_target(target)
            escaped_selector = selector.replace("'", "\\'")

            js = f"""
(function() {{
    var el = document.querySelector('{escaped_selector}');
    if (!el) return '';
    return el.innerText || el.textContent || '';
}})()
"""
            text = await self._eval_js(js)
            result[key] = text.strip()

        return result

    async def assert_element(self, target: dict, contains: str = None):
        """Assert element exists, optionally with text content."""
        selector = self.resolve_target(target)
        escaped_selector = selector.replace("'", "\\'")

        js = f"""
(function() {{
    var el = document.querySelector('{escaped_selector}');
    if (!el) return 'NOT_FOUND';
    """
        if contains:
            escaped_contains = contains.replace("'", "\\'")
            js += f"""
    var text = el.innerText || el.textContent || '';
    if (!text.includes('{escaped_contains}')) return 'NOT_FOUND';
    """
        js += "return 'OK'; })()"

        result = await self._eval_js(js)
        if result != "OK":
            msg = f"Element not found: {target}"
            if contains:
                msg += f" (containing '{contains}')"
            raise RuntimeError(msg)

    async def element_exists(self, target: dict) -> bool:
        """Check if element exists without failing."""
        try:
            selector = self.resolve_target(target)
            escaped_selector = selector.replace("'", "\\'")

            js = f"""
(function() {{
    var el = document.querySelector('{escaped_selector}');
    return el ? 'YES' : 'NO';
}})()
"""
            result = await self._eval_js(js)
            return result == "YES"
        except Exception:
            return False

    async def back(self):
        """Navigate back (browser back button)."""
        if not self.ws_url:
            raise RuntimeError("Not connected")

        js = "(function() { window.history.back(); return 'OK'; })()"
        result = await self._eval_js(js)
        if not result.startswith("OK"):
            raise RuntimeError(f"Back navigation failed: {result}")

    async def switch_tab(self, index: int):
        """Switch to tab by index (simplified, may not work with multi-page)."""
        # This would require tracking multiple pages via CDP
        # For now, just wait a moment
        await asyncio.sleep(0.5)

    async def _eval_js(self, js: str) -> str:
        """Evaluate JavaScript and return result as string."""
        try:
            import websockets
        except ImportError:
            raise RuntimeError("websockets not installed")

        try:
            async with websockets.connect(self.ws_url, close_timeout=10) as ws:
                msg_id = 1
                msg = json.dumps({
                    "id": msg_id,
                    "method": "Runtime.evaluate",
                    "params": {"expression": js}
                })

                await ws.send(msg)
                response_text = await asyncio.wait_for(ws.recv(), timeout=10)
                response = json.loads(response_text)

                result = response.get("result", {}).get("result", {}).get("value", "")
                return str(result)

        except asyncio.TimeoutError:
            return "ERROR: timeout"
        except Exception as e:
            log.error("CDP eval failed: %s", e)
            return f"ERROR: {str(e)}"

    async def _text_exists(self, text: str) -> bool:
        """Check if text exists on page."""
        try:
            escaped_text = text.replace("\\", "\\\\").replace("'", "\\'")
            js = f"""
(function() {{
    return document.body.innerText.includes('{escaped_text}') ? 'YES' : 'NO';
}})()
"""
            result = await self._eval_js(js)
            return result == "YES"
        except Exception:
            return False
