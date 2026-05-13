"""Persistent WebSocket connection pooling for CDP.

Multiplexes multiple CDP commands over a single WebSocket connection,
eliminating the per-eval TCP handshake overhead.
"""

import asyncio
import contextlib
import json
import logging

log = logging.getLogger("secrets-router")


class CDPConnection:
    """Persistent CDP WebSocket with message ID-based multiplexing.

    Maintains a single WebSocket connection and routes responses to pending
    futures via message ID.
    """

    def __init__(self, ws_url: str):
        """Initialize connection (not yet connected).

        Args:
            ws_url: Chrome DevTools Protocol WebSocket URL
        """
        self.ws_url = ws_url
        self.ws = None
        self._msg_id_counter = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._listener_task = None

    async def connect(self) -> None:
        """Open WebSocket and start listener task."""
        try:
            import websockets
        except ImportError as e:
            raise RuntimeError("websockets not installed") from e

        self.ws = await websockets.connect(self.ws_url, close_timeout=10)
        self._listener_task = asyncio.create_task(self._listen())
        log.debug("CDPConnection opened to %s", self.ws_url)

    async def _listen(self) -> None:
        """Listen for CDP responses and route by message ID."""
        try:
            async for raw in self.ws:
                try:
                    response = json.loads(raw)
                    msg_id = response.get("id")
                    if msg_id is not None and msg_id in self._pending:
                        future = self._pending.pop(msg_id)
                        if not future.done():
                            future.set_result(response)
                except json.JSONDecodeError as e:
                    log.error("Failed to parse CDP response: %s", e)
        except Exception as e:
            log.error("CDPConnection listener error: %s", e)
            # Mark all pending futures as failed
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError(f"CDP connection lost: {e}"))
            self._pending.clear()

    async def send(
        self,
        method: str,
        params: dict | None = None,
        timeout: float = 30,
    ) -> dict:
        """Send a CDP command and wait for response.

        Args:
            method: CDP method name (e.g., "Runtime.evaluate")
            params: Method parameters dict
            timeout: Seconds to wait for response

        Returns:
            Response dict from CDP

        Raises:
            asyncio.TimeoutError: Response not received within timeout
            RuntimeError: Connection not open or listener failed
        """
        if not self.connected:
            raise RuntimeError("CDP connection not open")

        self._msg_id_counter += 1
        msg_id = self._msg_id_counter

        future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future

        msg = {
            "id": msg_id,
            "method": method,
            "params": params or {},
        }

        try:
            await self.ws.send(json.dumps(msg))
            response = await asyncio.wait_for(future, timeout=timeout)
            return response
        except TimeoutError:
            self._pending.pop(msg_id, None)
            raise
        except Exception:
            self._pending.pop(msg_id, None)
            raise

    async def close(self) -> None:
        """Close WebSocket and cancel listener task."""
        if self._listener_task:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
        if self.ws:
            await self.ws.close()
        log.debug("CDPConnection closed")

    @property
    def connected(self) -> bool:
        """Check if WebSocket is open."""
        if self.ws is None:
            return False
        try:
            return self.ws.protocol is not None and self.ws.protocol.state.name == "OPEN"
        except AttributeError:
            # websockets v14+: use state directly or check close_code
            try:
                return self.ws.close_code is None
            except Exception:
                return False
