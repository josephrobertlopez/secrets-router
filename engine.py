"""
Recipe execution engine for secrets-router.

Executes YAML recipes with credential isolation. Credentials are resolved
into handles, steps are executed sequentially, and percepts are returned
to the caller. Values never appear in percepts or logs.
"""

import yaml
import asyncio
import re
from pathlib import Path
from typing import Any, Callable, Awaitable


class RecipeEngine:
    """Execute YAML recipes with credential isolation."""

    def __init__(self, recipes_dir: Path):
        self.recipes_dir = Path(recipes_dir)
        self.credentials = {}  # namespace -> {field -> value}
        self.config = {}       # config values (non-sensitive)
        self.extracts = {}     # extracted values from pages
        self.handles = {}      # namespace.field -> handle_id

    def load_recipe(self, name: str) -> dict:
        """Load and validate a recipe YAML.

        Args:
            name: Recipe filename without .yaml extension

        Returns:
            Parsed recipe dict

        Raises:
            FileNotFoundError: Recipe not found
            yaml.YAMLError: Invalid YAML
            ValueError: Recipe validation failed
        """
        recipe_path = self.recipes_dir / f"{name}.yaml"
        if not recipe_path.exists():
            raise FileNotFoundError(f"Recipe not found: {recipe_path}")

        with open(recipe_path) as f:
            recipe = yaml.safe_load(f)

        # Validate required fields
        required = ["name", "description", "steps"]
        for field in required:
            if field not in recipe:
                raise ValueError(f"Recipe missing required field: {field}")

        return recipe

    def resolve_credentials(self, recipe: dict) -> dict:
        """Resolve all credentials from stores into in-memory dict.

        Called before execution. This function needs access to a fetch backend
        (Bitwarden, rbw, yaml, pass, env) to actually fetch values.

        Args:
            recipe: Recipe dict with credentials block
            fetch_fn: Callable(store, key, field) -> value

        Returns:
            {namespace: {field: value}} with all credentials resolved
        """
        creds_spec = recipe.get("credentials", {})
        self.credentials = {}

        for namespace, spec in creds_spec.items():
            store = spec.get("store")
            item = spec.get("item")
            fields = spec.get("fields", {})

            if not store or not item:
                raise ValueError(
                    f"Credential {namespace} missing store or item"
                )

            self.credentials[namespace] = {}
            # Values will be populated by the caller via set_credential()

        return self.credentials

    def set_credential(self, namespace: str, field: str, value: str):
        """Store a resolved credential value.

        Called by the executor after fetching from a backend.

        Args:
            namespace: Credential namespace (e.g. 'card')
            field: Field name (e.g. 'number')
            value: The actual credential value
        """
        if namespace not in self.credentials:
            self.credentials[namespace] = {}
        self.credentials[namespace][field] = value

    def set_config(self, config: dict):
        """Set config values (non-sensitive)."""
        self.config = config or {}

    def set_extracts(self, extracts: dict):
        """Set extracted values from previous steps."""
        self.extracts = extracts or {}

    async def execute(
        self,
        recipe: dict,
        on_percept: Callable[[int, str], Awaitable[None]],
        on_gate: Callable[[str, str, bytes], Awaitable[bool]],
        actuator,
    ) -> dict:
        """Execute all steps in a recipe.

        Args:
            recipe: Recipe dict
            on_percept: async callback(step_index, percept_text)
            on_gate: async callback(gate_name, message, screenshot) -> bool
            actuator: ExecutionActuator instance

        Returns:
            {
                "status": "completed" | "cancelled" | "error",
                "percepts": [...],
                "extracts": {...},
                "gates": [{"name": "...", "approved": bool}],
                "steps_executed": int,
                "errors": []
            }
        """
        percepts = []
        gates = []
        errors = []
        steps_executed = 0

        try:
            await actuator.connect()

            for step_idx, step in enumerate(recipe.get("steps", [])):
                try:
                    action = step.get("action")

                    # Handle approval gates
                    if action == "await_approval":
                        gate_name = step.get("gate", f"gate_{step_idx}")
                        message = self.interpolate(step.get("message", "Approve?"))
                        screenshot = await actuator.screenshot()

                        approved = await on_gate(gate_name, message, screenshot)
                        gates.append({"name": gate_name, "approved": approved})

                        if not approved:
                            await on_percept(
                                step_idx,
                                f"Approval gate '{gate_name}' rejected. Workflow cancelled."
                            )
                            return {
                                "status": "cancelled",
                                "percepts": percepts,
                                "extracts": self.extracts,
                                "gates": gates,
                                "steps_executed": steps_executed,
                                "errors": errors,
                            }
                        continue

                    # Execute step via actuator
                    percept = await self.execute_step(step, actuator)

                    # Interpolate percept to show extracted values
                    percept = self.interpolate(percept)

                    percepts.append(percept)
                    await on_percept(step_idx, percept)
                    steps_executed += 1

                except Exception as e:
                    error_msg = f"Step {step_idx} ({action}): {str(e)}"
                    errors.append(error_msg)
                    percepts.append(f"ERROR: {error_msg}")
                    await on_percept(step_idx, f"ERROR: {error_msg}")

        except Exception as e:
            errors.append(f"Execution failed: {str(e)}")
        finally:
            await actuator.disconnect()
            self.cleanup()

        return {
            "status": "completed" if not errors else "error",
            "percepts": percepts,
            "extracts": self.extracts,
            "gates": gates,
            "steps_executed": steps_executed,
            "errors": errors,
        }

    async def execute_step(self, step: dict, actuator) -> str:
        """Execute one step, return percept string.

        Args:
            step: Step dict with action and params
            actuator: ExecutionActuator instance

        Returns:
            Percept text (template string, not yet interpolated)
        """
        action = step.get("action")
        percept_template = step.get("percept", f"Executed {action}")

        if action == "navigate":
            url = step.get("url")
            wait_for = step.get("wait_for")
            await actuator.navigate(url, wait_for=wait_for)

        elif action == "click":
            target = step.get("target")
            wait_for = step.get("wait_for")
            wait_for_tab = step.get("wait_for_tab")
            await actuator.click(target, wait_for=wait_for, wait_for_tab=wait_for_tab)

        elif action == "fill":
            target = step.get("target")
            value = self.interpolate(step.get("value", ""))
            await actuator.fill(target, value)

        elif action == "secure_fill":
            target = step.get("target")
            credential_ref = step.get("credential")  # e.g. "card.number"
            transform = step.get("transform")
            transform_args = step.get("transform_args", {})

            # Resolve credential value
            ns, field = credential_ref.split(".", 1)
            value = self.credentials.get(ns, {}).get(field, "")

            if not value:
                raise ValueError(f"Credential not resolved: {credential_ref}")

            # Apply transform if specified
            if transform:
                value = self.apply_transform(value, transform, transform_args)

            await actuator.secure_fill(target, value)

        elif action == "select":
            target = step.get("target")
            option = self.interpolate(step.get("option", ""))
            await actuator.select(target, option)

        elif action == "check":
            target = step.get("target")
            await actuator.check(target)

        elif action == "uncheck":
            target = step.get("target")
            await actuator.uncheck(target)

        elif action == "screenshot":
            label = step.get("label", "")
            await actuator.screenshot(label=label)

        elif action == "extract":
            targets = step.get("targets", {})
            extracted = await actuator.extract(targets)
            self.extracts.update(extracted)

        elif action == "assert":
            target = step.get("target")
            contains = step.get("contains")
            await actuator.assert_element(target, contains=contains)

        elif action == "sleep":
            seconds = step.get("seconds", 1)
            await asyncio.sleep(seconds)

        elif action == "conditional":
            if_target = step.get("if_exists")
            then_steps = step.get("then", [])
            else_steps = step.get("else", [])

            exists = await actuator.element_exists(if_target)
            steps_to_run = then_steps if exists else else_steps

            for sub_step in steps_to_run:
                await self.execute_step(sub_step, actuator)

        elif action == "back":
            await actuator.back()

        elif action == "switch_tab":
            index = step.get("index", 0)
            await actuator.switch_tab(index)

        else:
            raise ValueError(f"Unknown action: {action}")

        return percept_template

    def interpolate(self, template: str) -> str:
        """Replace variables in template string.

        Supports:
        - ${config.KEY} -> config[KEY]
        - ${extract.KEY} -> extracts[KEY]
        - ${credential.NAMESPACE.FIELD|TRANSFORM} -> value (masked)
        - ${NAMESPACE.FIELD|TRANSFORM} -> value (masked)

        Credential values are always masked in output.

        Args:
            template: Template string with ${...} placeholders

        Returns:
            Interpolated string (with credential values masked)
        """
        if not template:
            return template

        def replace_var(match):
            var = match.group(1)

            # config.KEY
            if var.startswith("config."):
                key = var[7:]
                return str(self.config.get(key, ""))

            # extract.KEY
            if var.startswith("extract."):
                key = var[8:]
                return str(self.extracts.get(key, ""))

            # credential.NAMESPACE.FIELD|TRANSFORM or NAMESPACE.FIELD|TRANSFORM
            if var.startswith("credential."):
                var = var[11:]

            if "|" in var:
                var, transform = var.split("|", 1)
                parts = var.split(".", 1)
                if len(parts) == 2:
                    ns, field = parts
                    value = self.credentials.get(ns, {}).get(field, "")
                    if value:
                        value = self.apply_transform(value, transform, {})
                        return self._mask(value)
            else:
                parts = var.split(".", 1)
                if len(parts) == 2:
                    ns, field = parts
                    value = self.credentials.get(ns, {}).get(field, "")
                    if value:
                        return self._mask(value)

            return f"${{{var}}}"

        return re.sub(r"\$\{([^}]+)\}", replace_var, template)

    def apply_transform(self, value: str, transform: str, args: dict) -> str:
        """Apply transform to a credential value.

        Args:
            value: The credential value to transform
            transform: Transform name (pad2, last4, pad2_slash_last2, etc.)
            args: Additional args (e.g. year_field for pad2_slash_last2)

        Returns:
            Transformed value
        """
        if transform == "pad2":
            return value.zfill(2)

        elif transform == "last2":
            return value[-2:] if len(value) >= 2 else value

        elif transform == "last4":
            return value[-4:] if len(value) >= 4 else value

        elif transform == "pad2_slash_last2":
            # Expects value to be month, and year_field ref in args
            month = value.zfill(2)
            year_ref = args.get("year_field")
            if year_ref:
                ns, field = year_ref.split(".", 1)
                year = self.credentials.get(ns, {}).get(field, "")
                year_short = year[-2:] if len(year) >= 2 else year
                return f"{month} / {year_short}"
            return month

        elif transform == "mask":
            return self._mask(value)

        elif transform == "concat":
            # args = {"parts": [field1, " ", field2, ...]}
            parts = args.get("parts", [])
            result = ""
            for part in parts:
                if part.startswith("${"):
                    part = self.interpolate(part)
                result += part
            return result

        elif transform == "upper":
            return value.upper()

        elif transform == "strip":
            return value.strip()

        else:
            raise ValueError(f"Unknown transform: {transform}")

    @staticmethod
    def _mask(value: str) -> str:
        """Mask a credential value for display (last 4 only)."""
        if len(value) <= 4:
            return "****"
        return "****" + value[-4:]

    def cleanup(self):
        """Zero all credential values from memory."""
        for ns in self.credentials:
            for field in self.credentials[ns]:
                self.credentials[ns][field] = ""
        self.credentials.clear()
        self.handles.clear()
