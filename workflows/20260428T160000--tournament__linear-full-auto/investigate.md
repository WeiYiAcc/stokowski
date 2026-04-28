# Investigation

You are investigating: **{{ issue_title }}**

{{ issue_description }}

## Your job

The user's description may be brief (even one sentence). Your job is to expand it into a complete, actionable specification before implementation begins.

### Step 1: Clarify requirements
Based on the title and description, determine:
- What are the core features? List each one explicitly.
- What are the inputs and outputs of each feature?
- What are the boundary conditions and error cases?
- What technology stack should be used? (If not specified, choose sensible defaults and state your choices.)
- What files need to be created?

### Step 2: Define acceptance criteria
For each feature, write a testable acceptance criterion. Example:
- "POST /shorten with invalid URL returns 400"
- "Shortening the same URL twice produces different codes"

### Step 3: Write the spec
Output a structured specification with:
1. **Architecture** — files, modules, their responsibilities
2. **API / Interface** — endpoints, CLI commands, function signatures
3. **Data model** — schemas, storage format
4. **Test plan** — list of test cases (at least 5)
5. **Edge cases** — what could go wrong, how to handle it

Post this spec as a Linear comment so the user can see it.
