Feature: Recipe Engine
  Secure recipes with credential isolation and approval gates

  Scenario: List available recipes
    When I call secure_list_recipes
    Then I get a list of recipes
    And each recipe has name, description, version, tags, and required_credentials

  Scenario: Dry run a recipe
    When I call secure_run_recipe("test-recipe", dry_run=True)
    Then I get percepts for each step
    And no credentials are fetched
    And status is "completed"

  Scenario: Execute a recipe with secure_fill
    Given a test recipe with secure_fill steps
    And credentials are available in mock store
    When I execute the recipe
    Then credential values are resolved from the store
    And each step produces a percept
    And credential values never appear in percepts (only masked)
    And credential values are zeroed after execution
    And status is "completed"

  Scenario: Approval gate pauses execution
    Given a recipe with an await_approval step
    And a screenshot is captured before the gate
    When execution reaches the gate
    Then the gate callback is invoked with message and screenshot
    And execution pauses until approval response
    And if approved, execution continues
    And if rejected, execution stops with status "cancelled"

  Scenario: Config overrides replace defaults
    Given a recipe with config.account_number = "1290850300"
    When I execute with config_overrides = {"account_number": "9999999999"}
    Then the fill step uses "9999999999"
    And the interpolation shows the override value in percepts

  Scenario: Interpolation handles all variable types
    Given a recipe with variables
    When interpolation processes ${config.KEY}
    Then it resolves to config[KEY]
    When interpolation processes ${extract.KEY}
    Then it resolves to extracts[KEY]
    When interpolation processes ${credential.NS.FIELD|last4}
    Then it resolves to masked credential value

  Scenario: Transforms are applied to credentials
    Given a credential value "9" and transform "pad2"
    When the transform is applied
    Then the result is "09"

  Scenario: pad2_slash_last2 combines month and year
    Given month credential "9" and year credential "2028"
    And transform is "pad2_slash_last2"
    When applied with year_field reference
    Then the result is "09 / 28"

  Scenario: Extract step captures page values
    Given a recipe with extract step targeting multiple elements
    When the step executes
    Then extracted values are stored in engine.extracts
    And subsequent interpolations can reference them via ${extract.KEY}

  Scenario: Errors are captured and returned
    Given a recipe with an action that fails
    When the recipe executes
    Then the error is logged in errors[]
    And status is "error"
    And execution continues (or stops at failure, depending on config)

  Scenario: Recipe YAML is loaded and validated
    When I load a valid recipe YAML
    Then it parses without error
    And required fields are present (name, description, steps)
    When I load an invalid recipe YAML
    Then it raises ValueError with clear message
