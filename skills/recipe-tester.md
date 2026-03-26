---
name: recipe-tester
description: Dry-run a recipe step by step without executing — validates structure, reachability, and credential references, then estimates runtime. Triggers on phrases like "test recipe without running", "dry run", "recipe-tester", "what would this recipe do", "simulate recipe", "preview recipe steps".
---

# recipe-tester Skill

**Walk through every step. Nothing executes. Everything gets checked.**

```
Usage: /recipe-tester pay-water-milwaukee
       /recipe-tester recipes/pay-electric.yaml
```

Difference from `/route-validator`: tester walks each step in order with a simulated execution trace. Validator does structural checks. Use tester when you want to see exactly what will happen before it happens.

---

## Step-by-step protocol

### 1. Load recipe

Read the YAML file. Parse it. If missing or malformed, stop with a clear error.

Extract:
- `steps` array (ordered)
- `credentials` block
- `config` block
- `actuator` type
- `gates` block (if present)

### 2. Walk each step

For each step in order, apply the check below. Never call `secure_run_recipe`. Never open a browser. Never touch rbw values.

---

## Step validation rules

### `navigate`
- HEAD request to `url` (curl -sI, follow redirects, max 10s)
- HTTP 200/301/302 → PASS
- HTTP 4xx/5xx → WARN (login-gated pages expected — flag)
- Timeout/refused → FAIL
- Log: `wait_for: "<text>"` — note it but can't verify without page load
- Estimate: **2-5s**

### `fill`
- Check `target.selector` — valid CSS syntax? → PASS/WARN
- Check `value` — if `${config.<key>}`, verify key exists in config → PASS/FAIL
- Log: "Would fill [selector] with [value]"
- Estimate: **0.1s**

### `secure_fill`
- Check `credential` reference format: `<alias>.<field>`
- Check `<alias>` exists in `credentials:` block → PASS/FAIL
- Check `<field>` is listed in that alias's `fields:` map → PASS/FAIL
- Run: `rbw get --field <field> "<item>"` to verify credential resolves (non-empty exit 0)
- Log: "Would securely fill [selector] using [alias].[field] from rbw:[item] → [MASKED]"
- Estimate: **0.5s** (rbw lookup + CDP fill)

### `click`
- Check `target` has at least one of: `text`, `selector`, `role`, `ref`
- Log: "Would click [target description]"
- If `wait_for` present: log "Would wait for '[text]' to appear"
- Estimate: **1-3s** (click + page response)

### `screenshot`
- Log: "Would capture screenshot labeled '[label]'"
- Estimate: **0.5s**

### `extract`
- Check each target has a `selector` → PASS/WARN
- Check `return_to_claude` value
- Log: "Would extract: [key list] from page"
- Estimate: **0.5s**

### `await_approval`
- Log: "GATE: Would pause and show '[message]' — user must confirm before continuing"
- Estimate: **user-dependent (excluded from auto-estimate)**

### `select`
- Check `target` present, `option` present
- Log: "Would select '[option]' from [target]"
- Estimate: **0.2s**

### `check`
- Check `target` present
- Log: "Would check/tick [target]"
- Estimate: **0.2s**

### `assert`
- Check `target` present
- Log: "Would assert [target] is visible — pass/fail determines flow"
- Estimate: **0.3s**

### `conditional`
- Log: "Would check if [condition] — then: [N steps] / else: [M steps]"
- Recurse into `then` and `else` steps with SKIP status (can't resolve without runtime state)
- Estimate: **0.3s + branch time**

### `sleep`
- Log: "Would sleep [seconds]s"
- Estimate: **[seconds]s**

### `secure_request` / `secure_run`
- Check `url` or `command_template` present
- Check credential references in `headers` / `body_template` / `command_template`
- Log: "Would send [METHOD] to [url] with [N] credential fields masked"
- Estimate: **1-5s**

---

## Report format

```
Dry Run: pay-water-milwaukee
Recipe: v1.0 | Actuator: playwright | Steps: 9
─────────────────────────────────────────────────────

Step 1  navigate     PASS    https://milwaukeewaterworks.com → 302 (login-gated, expected)
                             wait_for: "Account Login"
                             Est: 3s

Step 2  fill         PASS    input[name="account"] ← ${config.account_number} = "1290850300"
                             Est: 0.1s

Step 3  secure_fill  PASS    input[type="password"] ← login.password
                             rbw:"water account" field:password → resolves OK [MASKED]
                             Est: 0.5s

Step 4  click        PASS    text:"Sign In" → wait_for:"My Account"
                             Est: 2s

Step 5  extract      PASS    balance ← strong:near-text("Balance"), due_date ← td+td
                             return_to_claude: true
                             Est: 0.5s

Step 6  screenshot   PASS    label: "pre-submit"
                             Est: 0.5s

Step 7  extract      PASS    total ← .payment-total
                             Est: 0.5s

Step 8  await_appr.  GATE    "Confirm payment of ${total}?"
                             ← USER MUST CONFIRM BEFORE STEP 9 EXECUTES
                             Est: user-dependent

Step 9  click        PASS    text:"Pay Bill" → wait_for:"Thank you"
                             Est: 3s

Step 10 extract      PASS    confirmation ← .confirmation-number
                             return_to_claude: true
                             Est: 0.5s

─────────────────────────────────────────────────────
SUMMARY
  Steps:    10 total | 9 PASS | 0 FAIL | 1 GATE
  Issues:   None
  Est time: ~11s automated + user approval at gate

VERDICT: READY TO RUN
  "Run it now?" (yes / no)
```

---

## After the dry run

- PASS: "Looks clean. Run it? (`yes` / `no`)"
- FAIL on any step: "Fix these before running. Want help?" → invoke recipe-debugger patterns
- GATE present: "There's an approval gate at step N — you'll need to confirm before it submits."

---

## Timing estimates

Auto-total excludes `await_approval` gates (user-dependent). Typical ranges:

| Step type | Typical time |
|---|---|
| navigate | 2-5s |
| fill / check / select | 0.1-0.2s |
| secure_fill | 0.3-0.8s |
| click | 1-3s |
| screenshot | 0.3-0.5s |
| extract | 0.3-0.5s |
| sleep | exact |
| await_approval | excluded |

---

## Examples

```
User: "/recipe-tester pay-water-milwaukee"
Claude: [runs dry-run trace, shows step-by-step with PASS/FAIL/GATE, reports total]

User: "what would happen if I ran my electric bill recipe?"
Claude: [same — finds pay-electric.yaml, runs dry-run]

User: "dry run my recipe but skip the URL checks — I'm offline"
Claude: [runs without HEAD requests, marks navigates as SKIP instead of PASS/FAIL]
```
