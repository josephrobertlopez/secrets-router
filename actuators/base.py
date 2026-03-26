"""Base actuator interface."""

from abc import ABC, abstractmethod


class ExecutionActuator(ABC):
    """Abstract base for recipe step executors."""

    @abstractmethod
    async def connect(self):
        """Establish connection to execution environment."""

    @abstractmethod
    async def disconnect(self):
        """Close connection and cleanup."""

    @abstractmethod
    async def navigate(self, url: str, wait_for: str = None):
        """Navigate to URL, optionally wait for text to appear."""

    @abstractmethod
    async def click(self, target: dict, wait_for: str = None, wait_for_tab: int = None):
        """Click element specified by target."""

    @abstractmethod
    async def fill(self, target: dict, value: str):
        """Fill form field with plaintext value."""

    @abstractmethod
    async def secure_fill(self, target: dict, value: str):
        """Fill form field with credential value (same as fill but semantically secure)."""

    @abstractmethod
    async def select(self, target: dict, option: str):
        """Select option in dropdown."""

    @abstractmethod
    async def check(self, target: dict):
        """Check checkbox or radio button."""

    @abstractmethod
    async def uncheck(self, target: dict):
        """Uncheck checkbox or radio button."""

    @abstractmethod
    async def screenshot(self, label: str = None) -> bytes:
        """Take screenshot, return PNG bytes."""

    @abstractmethod
    async def extract(self, targets: dict) -> dict:
        """Extract text from multiple targets. Returns {key: value}."""

    @abstractmethod
    async def assert_element(self, target: dict, contains: str = None):
        """Assert element exists, optionally with text content."""

    @abstractmethod
    async def element_exists(self, target: dict) -> bool:
        """Check if element exists without failing."""

    @abstractmethod
    async def back(self):
        """Navigate back (browser back button or previous page)."""

    @abstractmethod
    async def switch_tab(self, index: int):
        """Switch to tab by index."""

    def resolve_target(self, target: dict) -> str:
        """Convert target spec to implementation-specific selector.

        Subclasses must implement based on their backend.

        Args:
            target: One of:
                {selector: "css selector"}
                {text: "visible text"}
                {role: "button", name: "label"}
                {ref: "element_id"}

        Returns:
            Backend-specific selector string

        Raises:
            ValueError: Unknown target type
        """
        if "selector" in target:
            return target["selector"]
        elif "text" in target:
            return f":text('{target['text']}')"
        elif "role" in target and "name" in target:
            return f"[role='{target['role']}'][name='{target['name']}']"
        elif "ref" in target:
            return f"[data-ref='{target['ref']}']"
        else:
            raise ValueError(f"Cannot resolve target: {target}")
