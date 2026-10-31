# deploy/ — from-source install of the `als` branch ("Al's") on Linux

The `als` branch is `main` (upstream releases) plus the MCE fleet's own
customization: named layout presets, keep-layouts-while-offline, and these
deployment scripts. Upstream tags are **merged** into it (never rebased), so a
deployed checkout only ever needs `git pull --ff-only`.

| Script | Purpose |
|---|---|
| `install.sh` | One-shot, idempotent from-source install: clone/update the checkout, build the venv, link the launcher, desktop entry and icon. |
| `upgrade.sh` | Pull the newest `als` (or `--tag vX.Y.Z-als.N`), show dependency changes, reinstall. Refuses while the app runs, backs up `config.toml` first. |
| `surveillance-launcher.sh` | Runtime wrapper: mpv tuning via `SURVEILLANCE_MPV_OPTS`, optional memory cap via `systemd-run --scope`. |
| `config.example.toml` | Connection profiles and layout presets, placeholders only. Site values live outside this public repo. |

```bash
git clone -b als https://github.com/PhenixStar/surveillance-station-client
surveillance-station-client/deploy/install.sh            # installs under ~/.local/opt/surveillance-station-client
surveillance                                             # or the "Surveillance Station" desktop entry
```

Environment knobs (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `SS_CLIENT_PREFIX` | `~/.local/opt/surveillance-station-client` | Install root; the checkout is `$SS_CLIENT_PREFIX/src` |
| `SS_CLIENT_BRANCH` | `als` | Branch (or tag) the checkout follows |
| `SURVEILLANCE_MPV_OPTS` | `hwdec-extra-frames=12` | Extra mpv options; the default fixes NVDEC "No decoder surfaces left" ghosting on deep-reorder streams |
| `SURVEILLANCE_MEMORY_MAX` | `16G` | `MemoryMax` of the app's systemd scope; empty disables the scope |

Credentials go to the system keyring (service `surveillance-station`, items
`<profile>:username` / `<profile>:password`); the login dialog stores them on
first connect, or seed them with `secret-tool store`.
