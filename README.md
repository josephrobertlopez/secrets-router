# secrets-router

MCP server for credential isolation in agent workflows.

**Problem:** AI agents need to fill passwords, card numbers, and API keys into browsers and APIs. If the agent sees the value, it enters the context window and potentially logs, tool calls, or conversation history. secrets-router keeps credential values inside the server process -- the agent sees only opaque handles and masked previews.

```
encrypted store ──> secrets-router ──> browser/API
                         |
                    agent sees only:
                    handle:a3f8c2d1...
                    ****1017
```

## Quick start

```bash
# Install
pip install -e .

# Register with Claude Code
claude mcp add secrets-router -- python3 /path/to/secrets-router/server.py
```

## Requirements

- Python 3.11+
- A credential store: [rbw](https://github.com/doy/rbw), [Bitwarden CLI](https://bitwarden.com/help/cli/), [pass](https://www.passwordstore.org/), or an [age](https://age-encryption.org/)-encrypted YAML file
- For browser fill: Chromium running with `--remote-debugging-port` (see [CDP setup](#cdp-setup))

## Tools

| Tool | What it does | Agent sees |
|------|-------------|------------|
| `secure_fetch(store, key, field)` | Fetch credential, return opaque handle | `handle:a3f8c2d1...` + `****1017` |
| `secure_fill(handle, selector)` | Fill browser field via CDP | `{"status": "filled", "masked": "****1017"}` |
| `secure_list_handles()` | List active handles | IDs, sources, age -- no values |
| `secure_discard_handles(handle?)` | Drop handles early | count discarded |
| `secure_list_recipes()` | List available YAML recipes | names, descriptions, tags |
| `secure_run_recipe(name, ...)` | Execute a full recipe | step-by-step percepts, masked |

## Credential backends

| Backend | CLI tool | Example |
|---------|----------|---------|
| `rbw` | [rbw](https://github.com/doy/rbw) | `secure_fetch("rbw", "primary card", "number")` |
| `bitwarden` | [bw](https://bitwarden.com/help/cli/) | `secure_fetch("bitwarden", "my-login", "password")` |
| `pass` | [pass](https://www.passwordstore.org/) | `secure_fetch("pass", "payments/card")` |
| `yaml` | [age](https://age-encryption.org/) | `secure_fetch("yaml", "water-bill.card.number")` |
| `env` | -- | `secure_fetch("env", "API_KEY")` |

## How it works

```
1. Agent calls: secure_fetch("rbw", "primary card", "number")
   Agent receives: {"handle": "handle:a3f8c2d1...", "masked": "****1017"}

2. Agent calls: secure_fill("handle:a3f8c2d1...", "#card-number")
   Server resolves handle -> fills browser DOM via CDP WebSocket
   Agent receives: {"status": "filled", "masked": "****1017"}
   Handle is consumed (single-use) and deleted.

3. The card number never entered the agent's context.
```

## Recipes

Recipes are YAML files that define multi-step workflows with credential isolation. The agent calls one tool and gets back percepts (what happened) without seeing any credential values.

```bash
# List available recipes
secure_list_recipes()

# Run a recipe
secure_run_recipe("pay-water-milwaukee")

# Dry run (shows steps without executing)
secure_run_recipe("pay-water-milwaukee", dry_run=True)

# Override config values
secure_run_recipe("pay-water-milwaukee", config_overrides={"account_number": "9999"})
```

See [`recipes/README.md`](recipes/README.md) for the full recipe language reference.

## CDP setup

secrets-router fills browser fields via Chrome DevTools Protocol. The browser must be running with remote debugging enabled:

```bash
# Launch Chromium with CDP
chromium --remote-debugging-port=9222

# Or with Google Chrome
google-chrome --remote-debugging-port=9222

# secrets-router auto-discovers the CDP port from running processes.
# No configuration needed if Chromium is running with the flag above.
```

## Security model

- **Handles are single-use** and expire after 5 minutes (configurable)
- **Values stay in-process** -- dereferenced immediately after use
- **Masked output** shows last 4 characters only (`****1017`)
- **Approval gates** in recipes pause execution for human/agent review
- **No value in logs** -- only handle IDs and source names are logged

### Limitations

- Python strings are immutable and garbage-collected. Values cannot be cryptographically zeroed. They are dereferenced (not memset'd) after use.
- The isolation boundary is process-level, not network-level. If the server process is compromised, values are exposed.

## Project structure

```
secrets-router/
  server.py                  MCP tools + handle store + credential backends
  engine.py                  Recipe execution engine
  actuators/
    base.py                  Abstract actuator interface
    playwright_cdp.py        Browser automation via CDP
  recipes/
    schema.yaml              Recipe language spec
    README.md                Action reference
    pay-water-milwaukee.yaml Example: utility bill payment
    example-login-github.yaml    Example: GitHub login with 2FA
    example-api-call.yaml        Example: authenticated API call
    example-template.yaml        Blank template for new recipes
  precepts/
    generate.py              Scan vault, generate credential inventory
  skills/
    secrets-router.md        Record/play/list/edit recipes (progressive disclosure)
    route-validator.md       Validate recipe before running (credentials, URLs, selectors)
    recipe-debugger.md       Diagnose failed recipe execution
    recipe-tester.md         Dry-run trace without execution
    step-recorder.md         Watch browser actions → auto-generate recipe YAML
    credential-auditor.md    Audit credential usage across recipes
```

## Skills (Claude Code)

Six skills ship with the repo. Copy to `~/.claude/skills/` or use directly.

| Skill | What it does |
|-------|-------------|
| `secrets-router` | Record workflows manually → generate recipe → replay autonomously |
| `route-validator` | Pre-flight check: credentials exist, URLs reachable, selectors valid |
| `recipe-debugger` | Post-failure diagnosis: which step broke, why, suggested fix |
| `recipe-tester` | Dry-run trace: validate each step without executing |
| `step-recorder` | Watch Playwright actions → auto-generate recipe steps |
| `credential-auditor` | Cross-recipe audit: used/missing/unused credentials |

```bash
# Install skills
cp skills/*.md ~/.claude/skills/

# Use in Claude Code
/secrets-router record "pay electric bill"
/secrets-router play pay-water-milwaukee
/route-validator recipes/pay-water-milwaukee.yaml
/recipe-tester pay-water-milwaukee
/credential-auditor
```

## CLI usage

```bash
# Start the MCP server
python3 server.py

# Standalone: list recipes
python3 -c "from engine import RecipeEngine; e = RecipeEngine('recipes'); print([r['name'] for r in e.list_recipes()])"

# Standalone: load and check a recipe
python3 -c "from engine import RecipeEngine; e = RecipeEngine('recipes'); print(e.load_recipe('pay-water-milwaukee')['name'])"

# Scan vault inventory (local only, not exposed to agents)
python3 precepts/generate.py
```

## License

MIT
