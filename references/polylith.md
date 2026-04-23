# Polylith Architecture Reference

All projects follow Polylith: components (business logic) + bases (entry points) + projects (deployables).

## Structure

```
workspace/
├── components/     ← Pure business logic, only exposes interface
│   └── auth/
│       ├── core.py / core.ts / core.clj    ← Implementation (private)
│       └── __init__.py / index.ts / interface.clj  ← Interface (public API)
├── bases/          ← Entry points that consume components
│   └── cli/
│       └── main.py / main.ts / core.clj
├── projects/       ← Deployable assemblies (select components + bases)
│   └── my-app/
│       └── pyproject.toml / package.json / deps.edn
└── workspace.toml / workspace.json / workspace.edn
```

## Rules

1. Components NEVER import from other components directly — only via interface
2. Bases import from components, never the reverse
3. Projects assemble components + bases, add no logic
4. Namespace migration = regex replace + mv directory, do NOT refactor code

## Per-Language Setup

### Clojure (native Polylith)

```
poly create component name:auth
poly create base name:cli
```

Interface: `(ns my-project.auth.interface)` re-exports public functions.

**Live reference:** `~/my-agent-monorepo/systems/my-tools/my-clj/`
- 16 components, 6 bases, 2 projects
- Read any component under `components/` to see the pattern
- Run: `poly check`, `poly info`, `poly diff`

**Inline example — component interface:**
```clojure
;; components/atuin/src/repo_sync/atuin/interface.clj
(ns repo-sync.atuin.interface
  "Atuin 历史查询"
  (:require [repo-sync.atuin.core :as core]))

(defn query-history [opts] (core/query-history opts))
```

### Python (polylith-cli)

```
poly create component --name auth
poly create base --name cli
```

Interface: `__init__.py` re-exports from `core.py`.
Namespace set in `workspace.toml` under `[tool.polylith] namespace = "toolbox"`.

**Live reference:** `~/my-agent-monorepo/systems/my-tools/my-py/`
- 9 components, 2 bases, 6 projects
- `workspace.toml` shows namespace and theme config
- Run: `uv run poly check`, `uv run poly info`

**Inline example — component:**
```python
# components/toolbox/ariadne_verify/core.py
"""ariadne_verify — grep vs datalog 一致性验证引擎。"""

def verify(query: str, source: str) -> VerifyResult:
    ...

# components/toolbox/ariadne_verify/__init__.py (interface)
from .core import verify
```

**Inline example — base consuming component:**
```python
# bases/toolbox/data_pipeline/core.py
from toolbox.ariadne_verify import verify
from toolbox.excel_reader import read_workbook

def main():
    data = read_workbook("input.xlsx")
    result = verify(data, source="datalog")
```

### TypeScript (Nx as Polylith equivalent + Effect)

Two options:

**Option A: Nx (recommended for new projects)**
```
nx generate @nx/js:library auth --directory=libs/auth
nx generate @nx/js:application cli --directory=apps/cli
```

- `libs/` = components, `apps/` = bases
- Interface: `index.ts` re-exports, path alias `@project/auth`
- Boundary rules in `nx.json` enforce dependency direction
- `npx nx affected -t test` — DAG-aware, only tests what changed

**Option B: workspace.json (lightweight, no Nx overhead)**

**Live reference:** `~/ariadne-fact/`
- 4 components (fact, store, hooks, lint), 3 bases (fact-manager, fact-hooks, polylith-lint)
- `workspace.json` declares components and base→component dependencies

**Inline example — workspace.json:**
```json
{
  "components": {
    "fact":  "lib/fact/index.ts",
    "store": "lib/store/index.ts",
    "hooks": "lib/hooks/index.ts"
  },
  "bases": [
    { "name": "fact-manager", "path": "bases/fact-manager.ts", "deps": ["fact", "store"] },
    { "name": "fact-hooks",   "path": "bases/fact-hooks.ts",   "deps": ["hooks"] }
  ]
}
```

**Inline example — component (lib/store/remote.ts):**
```typescript
// lib/store/remote.ts — implementation (private)
export class RemoteStore {
  async fetchAllFacts(): Promise<Fact[]> { ... }
  async upsertFact(fact: Fact): Promise<void> { ... }
}

// lib/store/index.ts — interface (public)
export { RemoteStore } from "./remote.ts"
export type { Fact } from "../fact/types.ts"
```

**Inline example — base consuming components:**
```typescript
// bases/fact-manager.ts
import { RemoteStore } from "../lib/store/index.ts"
import { searchByTitle } from "../lib/fact/index.ts"

// registers as a Pi tool with 6 actions: search, get, upsert, query, export, list-tags
```

**Effect integration:**
- Components return `Effect<Success, TypedError>` instead of throwing
- Bases use `@effect/cli` for CLI entry points or unwrap Effect at the boundary
- Errors are tracked in types — compiler enforces exhaustive handling

### Verifying structure

```bash
# Clojure
poly check

# Python
uv run poly check

# TypeScript (Nx)
npx nx graph           # visualize dependency graph
npx nx lint            # check boundary violations

# TypeScript (workspace.json)
# Read workspace.json to verify deps are correct
```
