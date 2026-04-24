# bb-task → Nx + TypeScript Polylith-like Mapping
# bb-task → Nx + TypeScript 类 Polylith 对照表

## Structure Mapping / 结构对照

```
bb (Clojure Polylith)              →  Nx (TypeScript Polylith-like)
─────────────────────────          ─────────────────────────────────
workspace.edn                     →  nx.json
bb.edn (root)                     →  project.json (root) + nx.json targetDefaults
bb.edn (per component)            →  project.json (per lib/app)
components/auth/                   →  libs/auth/
  src/my_clj/auth/interface.clj   →    src/index.ts
  src/my_clj/auth/core.clj        →    src/core.ts
bases/cli/                         →  apps/cli/
  src/my_clj/cli/core.clj         →    src/main.ts
projects/my-app/                   →  (Nx project tags + build targets)
poly check                         →  npx nx lint (enforce-module-boundaries)
poly info                          →  npx nx show projects
poly diff                          →  npx nx affected --print-affected
poly deps                          →  npx nx graph
```

## Task Mapping / 任务对照

### bb.edn `:depends` → Nx `dependsOn`

```clojure
;; bb.edn
data_pipeline:test {:depends [docx_reader:test excel_reader:test]
                    :task (shell "pytest" ...)}
```

```json
// libs/data-pipeline/project.json
{
  "targets": {
    "test": {
      "executor": "nx:run-commands",
      "command": "uv run pytest",
      "dependsOn": ["^test"]
    }
  }
}
```

`"^test"` means: run `test` on all dependencies first.
`"^test"` 表示：先跑所有依赖项的 test。

### bb.edn shell passthrough → Nx run-commands

```clojure
;; bb.edn
fact:server {:doc "启动 ariadne-fact server"
             :task (apply shell {:dir "systems/ariadne-fact"} "bb" "server" *command-line-args*)}
```

```json
// systems/ariadne-fact/project.json
{
  "targets": {
    "server": {
      "executor": "nx:run-commands",
      "command": "bb server {args._}",
      "options": { "cwd": "{projectRoot}" }
    }
  }
}
```

### bb.edn inline logic → TS script

```clojure
;; bb.edn — inline Clojure
state:init {:task (let [root (or (System/getenv "MONOREPO_STATE_ROOT")
                                 (str (System/getenv "HOME") "/agent-state/my-agent-monorepo"))]
                    (doseq [d ["ariadne-fact/db" "ariadne-fact/exports" ...]]
                      (fs/create-dirs (str root "/" d))))}
```

```typescript
// scripts/state-init.ts — equivalent TS
import { mkdirSync } from "fs";
import { join } from "path";

const root = process.env.MONOREPO_STATE_ROOT
  ?? join(process.env.HOME!, "agent-state/my-agent-monorepo");

const dirs = ["ariadne-fact/db", "ariadne-fact/exports", "ariadne-fact/logs",
              "my-pydanticai/runs", "my-pydanticai/artifacts"];

dirs.forEach(d => mkdirSync(join(root, d), { recursive: true }));
console.log("initialized:", root);
```

```json
// project.json
{ "targets": { "state-init": { "command": "npx tsx scripts/state-init.ts" } } }
```

## Polylith Concepts / Polylith 概念对照

### Component interface enforcement / 组件接口强制

```clojure
;; Clojure: interface.clj re-exports
(ns my-clj.auth.interface
  (:require [my-clj.auth.core :as core]))
(defn authenticate [creds] (core/authenticate creds))
```

```typescript
// TS: index.ts re-exports + package.json exports restriction
// libs/auth/src/index.ts
export { authenticate } from "./core.js";

// libs/auth/package.json — enforce interface
{ "exports": { ".": "./src/index.ts" } }
```

```json
// .eslintrc.json — enforce boundaries
{
  "rules": {
    "@nx/enforce-module-boundaries": ["error", {
      "depConstraints": [
        { "sourceTag": "type:lib", "notDependOnLibsWithTags": ["type:app"] },
        { "sourceTag": "type:app", "onlyDependOnLibsWithTags": ["type:lib"] }
      ]
    }]
  }
}
```

### Component tags / 组件标签

```clojure
;; Polylith: tags in workspace.edn or data
{:name "my-repo-sync" :tags #{:infra :dev}}
```

```json
// Nx: tags in project.json
{
  "tags": ["type:lib", "scope:infra", "lang:ts"]
}
```

## Multi-language Nx / 多语言 Nx

Nx is language-agnostic. Each sub-project uses its own toolchain:
Nx 不限语言，每个子项目用自己的工具链：

```json
// systems/ariadne-fact/project.json (Clojure + TS)
{
  "tags": ["type:system", "lang:clj", "lang:ts"],
  "targets": {
    "check": { "command": "poly check" },
    "test":  { "command": "bb test" },
    "server": { "command": "bb server" }
  }
}

// apps/my-PydanticAI/project.json (Python)
{
  "tags": ["type:app", "lang:py"],
  "targets": {
    "check": { "command": "uv run mypy src/" },
    "test":  { "command": "uv run pytest" },
    "lint":  { "command": "uv run ruff check src/" }
  }
}

// upstreams/stokowski/project.json (Python)
{
  "tags": ["type:upstream", "lang:py"],
  "targets": {
    "dry-run": { "command": ".venv/bin/stokowski --dry-run" }
  }
}
```

```bash
# Run all checks across all languages / 跨语言检查所有项目
npx nx run-many -t check

# Only test what changed / 只测改过的
npx nx affected -t test

# Filter by tag / 按标签过滤
npx nx run-many -t test --projects=tag:lang:py
```

## 1:1 Clojure → TypeScript Component Example
## Clojure → TypeScript 组件同构示例

### Clojure (current) / 当前 Clojure 版

```clojure
;; components/repo-registry/src/my_clj/repo_registry/interface.clj
(ns my-clj.repo-registry.interface)

(def repos
  [{:name "my-dotfiles-linux" :local ".local/share/chezmoi" :vcs :jj :branch "master" :tags #{:infra}}
   {:name "my-agent-monorepo" :local "my-agent-monorepo"    :vcs :git :branch "main"   :tags #{:infra :dev}}])

(def github-user "WeiYiAcc")

(defn by-tag [tag]
  (filter #(contains? (:tags %) tag) repos))

(defn by-name [name]
  (first (filter #(= (:name %) name) repos)))
```

### TypeScript (isomorphic) / 同构 TypeScript 版

```typescript
// libs/repo-registry/src/index.ts
export { repos, githubUser, byTag, byName } from "./core.js";
export type { Repo } from "./core.js";

// libs/repo-registry/src/core.ts
export interface Repo {
  name: string;
  local: string;
  vcs: "git" | "jj";
  branch: string;
  tags: Set<string>;
}

export const repos: Repo[] = [
  { name: "my-dotfiles-linux", local: ".local/share/chezmoi", vcs: "jj", branch: "master", tags: new Set(["infra"]) },
  { name: "my-agent-monorepo", local: "my-agent-monorepo",    vcs: "git", branch: "main",   tags: new Set(["infra", "dev"]) },
];

export const githubUser = "WeiYiAcc";

export const byTag = (tag: string): Repo[] =>
  repos.filter(r => r.tags.has(tag));

export const byName = (name: string): Repo | undefined =>
  repos.find(r => r.name === name);
```

### Side-by-side comparison / 并排对比

```
Clojure                              TypeScript
───────                              ──────────
(def repos [{...}])                  export const repos: Repo[] = [{...}]
(defn by-tag [tag]                   export const byTag = (tag: string) =>
  (filter #(contains? ...) repos))     repos.filter(r => r.tags.has(tag))
(defn by-name [name]                 export const byName = (name: string) =>
  (first (filter #(= ...) repos)))     repos.find(r => r.name === name)
```

Structure is 1:1. Logic is 1:1. Only syntax differs.
结构一致，逻辑一致，只有语法不同。
