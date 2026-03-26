# secrets-router Architecture

## Credential isolation

The agent sees handles and masked values. The server sees credentials.
This is enforced by the process boundary, not by trust or policy.

```
Agent's view:               Server's view:
─────────────               ─────────────
recipe name    ──fetch──>   item name → rbw/bitwarden → "4111111111111111"
                                                                 │
"handle:a3f8"  <──────────  opaque handle created ──────────────┘
     │
     │  ──fill──>           handle resolved → browser.fill("#card", value)
     │
"filled ****1017" <───────  "filled" returned, value dereferenced
```

## Architecture

```
                    CREDENTIAL ISOLATION BOUNDARY
                    ══════════════════════════════
Agent               │                            │  Credential Store
─────               │  secrets-router            │  ────────────────
                    │  MCP Server                │
"run recipe:        │                            │  rbw/bitwarden
 pay-water" ──────► │  Recipe Engine             │◄──── decrypt
                    │       │                    │
"filled [MASKED]" ◄─│  Percept Layer             │
"screenshot" ◄──────│       │                    │  Actuator Backends
"confirm $216?" ◄───│  Approval Gate             │  ─────────────────
"proceed" ─────────►│       │                    │
"conf: 010550" ◄────│  Actuator Layer            │──► Playwright (CDP)
                    │                            │──► HTTP (httpx)
                    │                            │──► CLI (subprocess)
                    ══════════════════════════════
```

## Tool surface

The agent sees 6 tools:

1. `secure_run_recipe(name, config_overrides?, dry_run?)` — run a full recipe
2. `secure_fetch(store, key, field)` — get an opaque handle (ad-hoc use)
3. `secure_fill(handle, selector)` — fill a single browser field (ad-hoc use)
4. `secure_list_recipes()` — list available recipes
5. `secure_list_handles()` — show active handles (IDs only, no values)
6. `secure_discard_handles(handle?)` — drop handles early

## Recipe execution flow

```
1. Agent calls: secure_run_recipe("pay-water-milwaukee")
2. Engine loads: recipes/pay-water-milwaukee.yaml
3. Engine fetches credentials from rbw (values in server memory only)
4. Engine connects to browser via CDP (same session the agent's browser MCP uses)
5. For each step:
   a. Execute action (navigate, fill, secure_fill, click...)
   b. For secure_fill: value goes store → memory → browser, never to agent
   c. Generate percept (what the agent sees)
   d. At await_approval: return screenshot + message, wait for agent response
6. Final extract: return confirmation number, total, date
7. Zero all credential values from memory
```

## Security properties

1. **Credential isolation**: Agent's view of secure_fill is `{"masked": "****1017"}` --
   contains only last 4 characters, which reveals nothing about the full value.

2. **Single-use resolution**: Values exist in server memory only during the step that uses
   them. Dereferenced immediately after the browser field is filled. (Python cannot
   cryptographically zero immutable strings; values are garbage-collected.)

3. **Approval gates**: `await_approval` pauses execution and returns a screenshot.
   Agent inspects visually and decides to continue or cancel.

4. **Auditable recipes**: YAML recipes are version-controlled and readable.
   They contain credential references (`card.number`) not values.

5. **Backend dispatch**: Config selects the backend (rbw, bitwarden, pass, yaml, env).
   Recipe structure stays the same regardless of which backend is used.

## File structure

```
secrets-router/
├── server.py              # MCP tools + handle store + CDP helpers
├── engine.py              # Recipe execution engine
├── actuators/
│   ├── base.py            # Abstract base class
│   └── playwright_cdp.py  # Browser automation via CDP
├── recipes/
│   ├── schema.yaml        # Recipe language reference
│   ├── README.md          # Action reference
│   └── pay-water-milwaukee.yaml
├── pyproject.toml
├── README.md
└── ARCHITECTURE.md        # this file
```
