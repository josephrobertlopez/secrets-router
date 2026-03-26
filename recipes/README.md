# Secure Recipes

Recipes are YAML files that define multi-step automations with credential isolation. An agent (Claude, another LLM, or a script) reads the recipe, calls `secure_run_recipe(name)`, and the MCP engine handles everything — credential fetch, browser/API interaction, approval gates — returning only masked percepts.

## Recipe Structure

```yaml
# ─── METADATA ───
name: pay-water-milwaukee          # unique ID, matches filename
description: "Pay Milwaukee Water Works bill"  # shown to agents
version: "1.0"
author: your-name
tags: [bill-pay, utility, recurring]

# ─── CREDENTIALS ───
# Where sensitive values come from. Agents see field NAMES, never VALUES.
# Values resolve inside the MCP server process only.
credentials:
  card:                            # namespace — referenced as card.number, card.cvv, etc.
    store: rbw                     # backend: rbw | bitwarden | pass | yaml | env
    item: "primary card"           # item name in the store
    fields:                        # map of logical name → store field path
      number: number               # rbw get --field number "primary card"
      name: name
      exp_month: exp_month
      exp_year: exp_year
      cvv: cvv

  bank:                            # multiple credential bundles allowed
    store: rbw
    item: "checking account"
    fields:
      routing: routing
      account: account

# ─── CONFIG ───
# Non-sensitive values. Agents CAN see these. Overridable at runtime.
config:
  account_number: "YOUR_ACCOUNT_NUMBER"
  email: "you@example.com"
  payer_name: "Your Name"
  phone: "5551234567"

# ─── ACTUATOR ───
# Which execution backend runs the steps.
actuator: playwright               # playwright | http | cli | compose

# Actuator-specific settings
actuator_config:
  headless: true                   # browser runs headless
  timeout: 30000                   # ms per step
  screenshot_on_error: true        # capture state on failure

# ─── STEPS ───
# Ordered list of actions. Each step produces a percept (what the agent sees).
steps:
  - action: navigate
    # ... (see Action Reference below)

# ─── GATES ───
# Named approval checkpoints. The recipe pauses and the agent must confirm.
# Gates are referenced in steps via `action: await_approval`.
gates:
  review_payment:
    message: "Confirm payment of ${extract.total} via ${extract.method}?"
    show: [screenshot, extract]    # what to present to the agent
    timeout: 300                   # seconds before auto-cancel
```

## Action Reference

### Navigation

```yaml
- action: navigate
  url: https://example.com
  wait_for: "text on page"        # wait until this text appears
  percept: "Loaded example.com"   # what agent sees

- action: click
  target: { text: "Submit" }      # target by visible text
  wait_for: "Success"             # wait after click
  wait_for_tab: 1                 # or wait for new tab to open
  percept: "Clicked Submit"

- action: back
  percept: "Navigated back"

- action: switch_tab
  index: 0
  percept: "Switched to tab 0"
```

### Form Filling

```yaml
# PLAINTEXT — agent CAN see the value (non-sensitive)
- action: fill
  target: { selector: 'input[name="email"]' }
  value: "${config.email}"                      # from config block
  percept: "Filled email: ${config.email}"

# SECURE — agent CANNOT see the value (sensitive)
- action: secure_fill
  target: { selector: 'input[name="card"]' }
  credential: card.number                        # from credentials block
  percept: "Filled card: [MASKED ****${card.number|last4}]"

# With transform
- action: secure_fill
  target: { selector: 'input[placeholder="MM / YY"]' }
  credential: card.exp_month
  transform: pad2_slash_last2                    # "9" + "2028" → "09 / 28"
  transform_args: { year_field: card.exp_year }
  percept: "Filled expiration: [MASKED]"

- action: select
  target: { selector: 'select[name="state"]' }
  option: "Wisconsin"
  percept: "Selected state: Wisconsin"

- action: check
  target: { selector: 'input[type="checkbox"]' }
  percept: "Checked terms"
```

### HTTP (for API actuator)

```yaml
# PLAINTEXT request
- action: request
  method: POST
  url: https://api.example.com/pay
  headers:
    Content-Type: application/json
  body:
    account: "${config.account_number}"
    amount: "${extract.balance}"
  percept: "POST /pay → ${response.status}"

# SECURE request — credential values injected server-side
- action: secure_request
  method: POST
  url: https://api.example.com/pay
  headers:
    Authorization: "Bearer ${auth.token}"        # from credentials
  body_template:
    card_number: "${card.number}"                # resolved inside MCP
    cvv: "${card.cvv}"
  percept: "POST /pay → ${response.status} [card MASKED]"
```

### CLI (for shell actuator)

```yaml
- action: run
  command: "curl -s https://api.example.com/status"
  percept: "Status: ${stdout}"

- action: secure_run
  command_template: "curl -s -d 'token=${auth.api_key}' https://api.example.com/action"
  percept: "API call complete [key MASKED]"
```

### Observation

```yaml
# Screenshot — returns actual image to agent
- action: screenshot
  label: "review"
  percept: "<screenshot: review>"

# Extract — scrape visible data from page
- action: extract
  targets:
    balance: { selector: 'strong:near-text("Balance")' }
    due_date: { selector: 'td:has-text("Due Date") + td' }
    confirmation: { selector: 'td:has-text("Confirmation") + td' }
  return_to_claude: true           # these are safe to return
  percept: "Extracted: balance=${balance}, due=${due_date}"

# Assert — verify page state
- action: assert
  target: { text: "Payment Complete" }
  percept: "Verified: payment complete"
```

### Control Flow

```yaml
# Approval gate — agent must confirm
- action: await_approval
  gate: review_payment             # references gates block
  percept: "Awaiting approval..."

# Conditional
- action: conditional
  if_exists: { text: "Error" }
  then:
    - action: screenshot
      label: "error"
    - action: fail
      message: "Payment error detected"
  else:
    - action: click
      target: { text: "Continue" }

# Sleep
- action: sleep
  seconds: 2
  percept: "Waiting 2s..."
```

## Target Resolution

Targets are backend-agnostic references. The actuator resolves them.

```yaml
# By CSS selector
target: { selector: 'input[placeholder="Card Number"]' }

# By visible text
target: { text: "Pay Bill" }

# By ARIA role
target: { role: button, name: "Continue" }

# By Playwright snapshot ref (from browser_snapshot)
target: { ref: e125 }

# For HTTP actuator
target: { json_path: "$.data.token" }
target: { header: "Authorization" }

# For CLI actuator
target: { arg: "--api-key" }
target: { env: "API_KEY" }
```

## Transforms

Applied to credential values before filling. Agent never sees the pre- or post-transform value.

| Transform | Input | Output | Use case |
|-----------|-------|--------|----------|
| `pad2` | `"9"` | `"09"` | Month padding |
| `last2` | `"2028"` | `"28"` | Year shortening |
| `last4` | `"4111...1111"` | `"1017"` | Card last 4 (for percept masking only) |
| `pad2_slash_last2` | month + year | `"09 / 28"` | Expiration field |
| `mask` | `"4111...1111"` | `"****1017"` | Masked preview |
| `concat` | `[field1, " ", field2]` | `"first last"` | Name combination |
| `upper` | `"joseph"` | `"JOSEPH"` | Case transform |
| `strip` | `" value "` | `"value"` | Whitespace cleanup |

## Percept Contract

What the agent sees for each action type:

| Action | Agent sees | Agent does NOT see |
|--------|-----------|-------------------|
| `fill` | field name + plaintext value | nothing hidden |
| `secure_fill` | field name + `[MASKED ****1017]` | full value |
| `navigate` | URL + status | nothing hidden |
| `click` | button/link text | nothing hidden |
| `screenshot` | actual image | nothing hidden |
| `extract` | scraped text values | nothing hidden |
| `await_approval` | message + screenshot | nothing hidden |
| `secure_request` | URL + status + `[card MASKED]` | request body credentials |
| `secure_run` | exit code + `[key MASKED]` | command with credentials |

## Agent Prompt Template

For other agents (not Claude) consuming recipes:

```
You have access to a secure recipe runner. Call `secure_run_recipe(name)`
to execute a predefined automation. You will receive step-by-step percepts
describing what happened. Credential values are never shown to you —
you see only masked previews like [MASKED ****1017].

At approval gates, you will see a screenshot and a confirmation message.
Respond with "proceed" to continue or "cancel" to abort.

Available recipes:
{{list_recipes output}}

To run a recipe with config overrides:
  secure_run_recipe("pay-water-milwaukee", {"account_number": "1234567890"})
```

## Writing New Recipes

1. Create `recipes/your-recipe.yaml`
2. Define credentials (store + item + fields)
3. Define config (non-sensitive defaults)
4. Write steps using action primitives
5. Add approval gates before irreversible actions (payments, submissions)
6. Test with `secure_run_recipe("your-recipe", dry_run=True)`

### Checklist

- [ ] Every sensitive field uses `secure_fill` or `secure_request`, never `fill`
- [ ] Approval gate before any payment/submission step
- [ ] `screenshot` before approval gate so agent can review visually
- [ ] `extract` after completion to capture confirmation numbers
- [ ] Config values are overridable (no hardcoded account numbers in steps)
- [ ] Percepts are descriptive enough for an agent to understand progress
- [ ] Credentials block references real store items (test with `rbw get`)

## File Naming

```
recipes/
├── schema.yaml                    # this spec
├── README.md                      # this file
├── pay-water-milwaukee.yaml       # utility bill
├── pay-we-energies.yaml           # electric bill
├── login-chase.yaml               # bank login
├── renew-domain.yaml              # domain renewal
└── ...
```

Recipe names should be: `{verb}-{service}-{qualifier}.yaml`
