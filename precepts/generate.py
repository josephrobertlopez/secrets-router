#!/usr/bin/env python3
"""
Generate credential inventory precept from rbw.
Scans the vault, extracts metadata (never values), saves to inventory.yaml.
Agents read this to know what credentials exist before writing recipes.
"""
import subprocess
import json
import yaml
import sys
from pathlib import Path
from datetime import datetime


def _mask_primary(value: str, item_type: str) -> str:
    """Mask the primary value for preview."""
    if not value:
        return "****"
    if item_type == "card":
        return f"****{value[-4:]}" if len(value) >= 4 else "****"
    if "@" in value:
        user, domain = value.split("@", 1)
        return f"{user[:2]}***@{domain}"
    if len(value) > 4:
        return f"{value[:2]}***{value[-1]}"
    return "****"


def scan_rbw() -> list:
    """Scan rbw vault and extract metadata for each item."""
    result = subprocess.run(["rbw", "list"], capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        print(f"ERROR: rbw list failed: {result.stderr}", file=sys.stderr)
        return []

    items = [i.strip() for i in result.stdout.strip().split("\n") if i.strip()]
    catalog = []

    for item_name in items:
        try:
            full = subprocess.run(
                ["rbw", "get", "--full", item_name],
                capture_output=True, text=True, timeout=5,
            )
            if full.returncode != 0:
                continue

            lines = full.stdout.strip().split("\n")
            primary = lines[0].strip() if lines else ""

            # Parse key: value fields
            fields = {}
            for line in lines[1:]:
                if ": " in line:
                    k, v = line.split(": ", 1)
                    fields[k.strip().lower()] = v.strip()

            # Determine type
            if "expiration" in fields or "cvv" in fields:
                item_type = "card"
                brand = fields.get("brand", "card")
                masked = f"{brand} {_mask_primary(primary, 'card')}"
            elif "@" in primary:
                item_type = "login"
                masked = _mask_primary(primary, "login")
            else:
                item_type = "login"
                masked = _mask_primary(primary, "other")

            catalog.append({
                "name": item_name,
                "type": item_type,
                "fields_available": sorted(list(fields.keys()) + ["password"]),
                "masked_preview": masked,
            })

        except subprocess.TimeoutExpired:
            continue
        except Exception as e:
            print(f"WARN: skipped {item_name}: {e}", file=sys.stderr)
            continue

    return catalog


def main():
    print("Scanning rbw vault...")
    catalog = scan_rbw()
    print(f"Found {len(catalog)} items")

    # Group by type
    cards = [i for i in catalog if i["type"] == "card"]
    logins = [i for i in catalog if i["type"] == "login"]

    inventory = {
        "generated": datetime.now().isoformat(),
        "store": "rbw",
        "total": len(catalog),
        "summary": {
            "cards": len(cards),
            "logins": len(logins),
        },
        "items": catalog,
    }

    out_path = Path(__file__).parent / "inventory.yaml"
    with open(out_path, "w") as f:
        yaml.dump(inventory, f, default_flow_style=False, sort_keys=False)

    print(f"Written to {out_path}")

    # Also print summary
    print(f"\nCards ({len(cards)}):")
    for c in cards:
        print(f"  {c['name']:30s} {c['masked_preview']:20s} fields: {', '.join(c['fields_available'])}")

    print(f"\nLogins ({len(logins)}):")
    for l in logins[:10]:
        print(f"  {l['name']:30s} {l['masked_preview']:20s}")
    if len(logins) > 10:
        print(f"  ... and {len(logins) - 10} more")


if __name__ == "__main__":
    main()
