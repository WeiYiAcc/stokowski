# Nix Home Manager Reference

Live config: `~/.local/share/chezmoi/home-manager/home_racknerd-f76a666.nix`

---

## Package Declaration

```nix
home.packages = [
  pkgs.rsync
  pkgs.rclone
  pkgs.curl
  pkgs.wget
];
```

Allow unfree packages:

```nix
nixpkgs.config.allowUnfree = true;
```

---

## systemd User Service

```nix
systemd.user.services.my-service = {
  Unit = {
    Description = "My background service";
  };
  Service = {
    Type = "oneshot";          # or "simple", "forking", "notify"
    ExecStart = "%h/scripts/my-script.sh";
    Environment = [
      "HOME=%h"
      "PATH=%h/.nix-profile/bin:/nix/var/nix/profiles/default/bin:/usr/local/bin:/usr/bin:/bin"
    ];
    Restart = "always";        # for persistent services
    RestartSec = "5s";
  };
  Install = {
    WantedBy = [ "default.target" ];
  };
};
```

Key notes:
- `%h` expands to the user's home directory
- `Type = "oneshot"` for scripts that run and exit (e.g. sync jobs)
- `Type = "simple"` for long-running daemons
- Environment as a list of strings for multiple entries; single string for one

---

## systemd User Timer

```nix
systemd.user.timers.my-service = {
  Unit = {
    Description = "Timer for my-service";
  };
  Timer = {
    OnBootSec = "5min";          # delay after boot before first run
    OnUnitActiveSec = "30min";   # repeat interval (e.g. "1h", "30min")
    Persistent = true;           # catch up missed runs after suspend/downtime
  };
  Install = {
    WantedBy = [ "timers.target" ];
  };
};
```

The timer name must match the service name. Timer `WantedBy` should be `timers.target`, not `default.target`.

---

## rsync Integration Pattern

Script pattern: `platform/sync/sync-claude-memory.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$ROOT/platform/runtime/manifest.sh"
mono_load_local_env

mode="$1"   # plan|push|pull
target="${2:-${CLAUDE_MEMORY_SYNC_TARGET:-racknerd}}"
# normalize: bare hostname → host:path
[[ "$target" != *:* ]] && target="${target}:${HOME}/.claude/projects"

src="$HOME/.claude/projects"
flags=(-avh --mkpath --itemize-changes --compress)
[[ "$mode" == "plan" ]] && flags+=(-n)
[[ "${MONOREPO_RSYNC_DELETE:-false}" == "true" ]] && flags+=(--delete)

case "$mode" in
  plan|push) rsync "${flags[@]}" "$src/" "$target/" ;;
  pull)      rsync "${flags[@]}" "$target/" "$src/" ;;
esac
```

systemd invocation:
```nix
ExecStart = "%h/my-agent-monorepo/platform/sync/sync-claude-memory.sh push";
```

---

## rclone Integration Pattern

Script pattern: `platform/sync/sync-onedrive.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$ROOT/platform/runtime/manifest.sh"
mono_load_local_env

mode="$1"    # plan|push|pull
scope="${2:-all}"
remote="${3:-${MONOREPO_ONEDRIVE_REMOTE:-my-onedrive:agent-state/my-agent-monorepo}}"

# validate rclone remote
[[ "$remote" != *:* ]] && { echo "not an rclone path: $remote" >&2; exit 1; }

src="$(mono_scope_dir "$scope")"
dest="${remote%/}/$(basename "$src")"
[[ "$scope" == "all" ]] && dest="$remote"

flags=(--fast-list --create-empty-src-dirs)
[[ "$mode" == "plan" ]] && flags+=(--dry-run)

case "$mode" in
  plan|push)
    if [[ "${MONOREPO_RSYNC_DELETE:-false}" == "true" ]]; then
      rclone sync "$src" "$dest" "${flags[@]}"
    else
      rclone copy "$src" "$dest" "${flags[@]}"
    fi
    ;;
  pull)
    if [[ "${MONOREPO_RSYNC_DELETE:-false}" == "true" ]]; then
      rclone sync "$dest" "$src" "${flags[@]}"
    else
      rclone copy "$dest" "$src" "${flags[@]}"
    fi
    ;;
esac
```

rclone remote `my-onedrive:` is configured via chezmoi. To reconfigure: `rclone config`.

systemd invocation:
```nix
ExecStart = "%h/my-agent-monorepo/platform/sync/sync-onedrive.sh push all";
```

---

## Environment Variables (platform/runtime/local.env)

```bash
# Sync targets
CLAUDE_MEMORY_SYNC_TARGET=racknerd
MONOREPO_ONEDRIVE_REMOTE=my-onedrive:agent-state/my-agent-monorepo
MONOREPO_SYNC_TARGET_DEFAULT=racknerd:/srv/sync/my-agent-monorepo

# Behavior flags
MONOREPO_RSYNC_DELETE=false   # set true to enable --delete / rclone sync
```

---

## Existing Services in home_racknerd-f76a666.nix

| Service | Description |
|---|---|
| `omniroute` | OmniRoute AI API router (node) |
| `gproxy` | GProxy LLM multi-channel proxy |
| `cliproxy` | CliProxy API - Claude OAuth proxy |
| `caddy` | Caddy HTTPS reverse proxy |
| `sync-claude-memory` | Sync ~/.claude/projects/ to racknerd (30min timer) |
| `sync-onedrive` | Sync agent state to OneDrive (1h timer) |
| `syncthing` | File sync daemon (via services.syncthing) |
