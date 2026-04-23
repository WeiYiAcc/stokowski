# Polylith Component Examples

Minimal reference implementations for each supported language.
Copy and adapt these when creating new components.

## Examples

| Language | Directory | Interface | Implementation | Notes |
|----------|-----------|-----------|----------------|-------|
| Clojure  | `clj-component/` | `interface.clj` | `core.clj` | Native Polylith. Namespace = directory path. |
| Python   | `py-component/`  | `__init__.py`   | `core.py`  | polylith-cli. `__init__.py` re-exports public API. |
| TypeScript | `ts-component/` | `index.ts`     | `core.ts`  | Nx libs. Uses Effect for typed errors. Path alias `@project/name`. |

## Key Pattern

Every component follows the same structure regardless of language:

```
component-name/
├── interface file    ← Public API (the ONLY thing bases import)
└── implementation    ← Private logic (never imported directly)
```

**Bases** consume components via interface only:

```python
# Python base
from toolbox.greeter import greet       # ✓ via __init__.py

# NOT this:
from toolbox.greeter.core import greet  # ✗ bypasses interface
```

```typescript
// TypeScript base
import { greet } from "@project/greeter";       // ✓ via index.ts

// NOT this:
import { greet } from "@project/greeter/core";   // ✗ bypasses interface
```

## Live References

Full-scale examples from the codebase:

- **Clojure:** `~/my-agent-monorepo/systems/my-tools/my-clj/components/` (16 components)
- **Python:** `~/my-agent-monorepo/systems/my-tools/my-py/components/toolbox/` (9 components)
- **TypeScript:** `~/ariadne-fact/lib/` (4 components) + `~/ariadne-fact/workspace.json`
