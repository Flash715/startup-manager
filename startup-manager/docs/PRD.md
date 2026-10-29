# PRD — Startup Manager (startupmgr)

| Field | Value |
|---|---|
| Product | `startupmgr` — Windows Startup Program Manager |
| Version | 1.0.0 |
| Author | Engineering |
| Status | Implemented |
| Platform | Windows 10 / 11 (x64, ARM64) |
| Language | Python 3.12+ |
| Package manager | `uv` |

---

## 1. Problem Statement

Windows spreads autostart entries across **at least eight** different subsystems:

1. `HKCU\...\CurrentVersion\Run`
2. `HKLM\...\CurrentVersion\Run`
3. `HKLM\...\WOW6432Node\...\Run` (32-bit view)
4. `RunOnce` variants (HKCU / HKLM / WOW64)
5. User Startup folder (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`)
6. Common Startup folder (`%ProgramData%\...\Startup`)
7. Task Scheduler tasks with a logon/boot trigger
8. Services set to `Automatic` start type

Task Manager's "Startup" tab only shows items 1–3 and 5–6, and it **hides** disabled
state behind an undocumented binary blob (`Explorer\StartupApproved`). Users who want a
complete picture — or who want to script/audit startup on a fleet — have no single tool.

**Result:** boot slowdown, privacy surprises (spyware persistence), and no audit trail.

---

## 2. Goals

| # | Goal | Success Metric |
|---|---|---|
| G1 | Enumerate **every** autostart entry across all 8 sources | 0 false negatives on a stock Win11 VM |
| G2 | Show the real reason an entry is disabled | `enabled/disabled` matches Task Manager 100% |
| G3 | Safe, reversible removal | Every delete creates a restorable backup |
| G4 | Scriptable | `--json` output valid for `jq`; non-zero exit codes |
| G5 | No dependencies | stdlib only; works offline, single-file installable |
| G6 | Elevation-aware | Read-only degrades gracefully; writes require admin only when needed |

## 3. Non-Goals

- Detecting malware (we are not an AV; we report, we don't judge).
- Modifying `BootExecute` / driver load order (too risky, out of scope v1).
- Linux/macOS support.
- GUI. (CLI-first; a TUI/GUI is a v2 candidate.)

---

## 4. Personas

| Persona | Need |
|---|---|
| **Power user** | "Disable Spotify from booting without uninstalling it." |
| **IT admin** | "Dump JSON of every autostart on 200 machines and diff it." |
| **Privacy-conscious user** | "What is silently starting with me?" |
| **Tinkerer** | "I broke my startup, restore my backup." |

---

## 5. Functional Requirements

### FR-1 — Enumeration

Must read all of the following and tag each row with `source`:

| Source ID | Registry / Path |
|---|---|
| `registry-user-run` | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` |
| `registry-user-runonce` | `HKCU\...\RunOnce` |
| `registry-machine-run` | `HKLM\Software\Microsoft\Windows\CurrentVersion\Run` |
| `registry-machine-runonce` | `HKLM\...\RunOnce` |
| `registry-machine-run-wow64` | `HKLM\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run` |
| `registry-machine-runonce-wow64` | `HKLM\Software\WOW6432Node\...\RunOnce` |
| `startup-folder-user` | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup` |
| `startup-folder-common` | `%ProgramData%\Microsoft\Windows\Start Menu\Programs\Startup` |
| `scheduled-task` | Task Scheduler, triggers = Logon / Boot |
| `service` | Services with `StartType = Automatic` |
| `startup-approved` | Explorer delay/disabled blob (state overlay, not a list) |

**Acceptance:** `startupmgr list --all` on a stock install returns ≥1 row per populated
source and prints a per-source count in the summary.

### FR-2 — Enabled / Disabled State

- For registry + Startup-folder items, read
  `HKCU|HKLM\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run`
  (and `\StartupFolder`, `\Run32`, `\StartupFolder32`).
- Byte 0 of the `REG_BINARY` value decides state:
  - `0x02` or `0x06` → **enabled**
  - `0x03` → **disabled** (byte 8+ is a FILETIME of the disable event)
  - absent → **enabled** (default)
- For scheduled tasks → `<Enabled>` in the task XML.
- For services → `StartType != Disabled`.
- For `RunOnce` → always reported enabled (state is not tracked by Explorer).

**Acceptance:** Toggling an item in Task Manager flips our output accordingly.

### FR-3 — Metadata Enrichment

Each row must carry:

| Field | Meaning |
|---|---|
| `name` | Registry value name / `.lnk` stem / task name / service name |
| `command` | Raw command line or file path |
| `exe` | Resolved absolute executable path (env-var expanded) |
| `args` | Argument string |
| `exists` | `true` if `exe` is present on disk |
| `publisher` | `CompanyName` from the PE version resource (best effort) |
| `file_version` | `FileVersion` string |
| `source` | Source ID from FR-1 |
| `location` | Human-readable registry path or folder |
| `scope` | `user` or `machine` |
| `enabled` | bool |
| `deletable` | bool + `delete_block_reason` when `false` |

**Acceptance:** For msedge.exe, `publisher` = `Microsoft Corporation`.

### FR-4 — Disable / Enable (non-destructive)

- `startupmgr disable <id>` writes byte `0x03` to the `StartupApproved` key,
  creating the key and value if missing.
- `startupmgr enable <id>` writes byte `0x02`.
- Must **not** delete the underlying `Run` value.
- Machine-scope items require elevation → fail with exit code `4` and a clear message.

### FR-5 — Delete (destructive, backed up)

- `startupmgr delete <id>` removes the actual entry:
  - registry → `RegDeleteValue`
  - folder → delete `.lnk`/file
  - scheduled task → `schtasks /delete` (or disable with `--disable-only`)
  - service → **refuses** unless `--i-know-what-im-doing`; then `sc config <name> start= disabled`
- Before any delete, write a JSON snapshot to
  `%LOCALAPPDATA%\startupmgr\backups\<timestamp>.json`.
- `--dry-run` prints the planned action and exits 0 without touching anything.

**Acceptance:** `delete` then `restore <backup>` returns the system to the exact prior state.

### FR-6 — Backup / Restore

- `startupmgr backup` → snapshot of all entries.
- `startupmgr backups` → list snapshots.
- `startupmgr restore <file|latest>` → re-create deleted entries from a snapshot
  (registry values, `.lnk` copies stored as base64 blob, task re-import via XML).

### FR-7 — Filtering & Output

- `--source <id>` (repeatable), `--scope user|machine`, `--search <substr>`,
  `--enabled-only`, `--disabled-only`.
- Output formats: `table` (default, aligned), `json`, `csv`.
- Colour output on TTY only; `--no-color` and `NO_COLOR` env respected.

### FR-8 — Exit Codes

| Code | Meaning |
|---|---|
| 0 | OK |
| 1 | Generic/unexpected error |
| 2 | Bad usage (argparse) |
| 3 | Entry not found |
| 4 | Elevation required |
| 5 | Operation refused (safety guard) |
| 6 | Platform unsupported (non-Windows) |

---

## 6. Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR-1 | **Zero third-party runtime dependencies** (stdlib only). Dev deps allowed. |
| NFR-2 | `list` completes in < 2 s for 250 startup entries on an i5. |
| NFR-3 | Graceful degradation when not elevated — never crash on `AccessDenied`. |
| NFR-4 | Never silently delete; every mutation is logged to `%LOCALAPPDATA%\startupmgr\audit.log`. |
| NFR-5 | All registry access via `winreg` (no `reg.exe` parsing except scheduled tasks). |
| NFR-6 | Type-hinted, `ruff`-clean, `mypy --strict` on the core modules. |
| NFR-7 | Tests runnable on Windows CI without a display; mocked registry layer. |

---

## 7. Architecture

```
                  ┌──────────────────────────┐
                  │           CLI            │  argparse, subcommands
                  │   (startupmgr/cli.py)    │  color, table/json/csv render
                  └────────────┬─────────────┘
                               │
        ┌──────────────────────┼───────────────────────┐
        ▼                      ▼                       ▼
┌───────────────┐    ┌──────────────────┐    ┌────────────────┐
│  collectors   │    │     actions      │    │     store      │
│ registry.py   │    │ disable/enable   │    │ backup/restore │
│ folder.py     │    │ delete/restore   │    │ audit log      │
│ tasks.py      │    │ (+elevation gate)│    └────────────────┘
│ services.py   │    └──────────────────┘
└───────┬───────┘
        ▼
┌───────────────┐    ┌──────────────────┐
│  models.py    │    │     winapi.py    │
│ StartupEntry  │    │ reg helpers, PE  │
│  dataclass    │    │ version, admin   │
└───────────────┘    └──────────────────┘
```

**Key design decision — stable IDs.** Every entry gets
`id = sha1(source + "|" + name + "|" + location)[:10]`. This survives re-scan, so
`startupmgr disable 3f9a1c0b22` works across sessions without a server-side store.

---

## 8. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Deleting a critical service breaks boot | High | Services refused by default; `--i-know-what-im-doing` required |
| Registry key ACL denies write | Medium | Catch `PermissionError` → exit 4 with a hint |
| `StartupApproved` binary format changes | Medium | Unknown byte → report `unknown`, never crash |
| Scheduled task XML parsing edge cases | Low | Fall back to `Get-ScheduledTask` JSON via PowerShell |
| User deletes something then forgets | High | Mandatory backup before every mutation + `restore` |

---

## 9. Milestones

| # | Scope | Status |
|---|---|---|
| M1 | `models` + `winapi` + registry collector + `list` | ✅ |
| M2 | Startup folder + scheduled task + service collectors | ✅ |
| M3 | `enable` / `disable` / `delete` + safety guards | ✅ |
| M4 | `backup` / `restore` / audit log | ✅ |
| M5 | JSON/CSV output, filters, tests | ✅ |
| M6 (v2) | TUI (textual), diff between two snapshots, allowlist | ⬜ |

---

## 10. Open Questions (v2)

- Should we support "Startup impact" (the boot-time ms in Task Manager)? It lives in
  `NTUSER.DAT` event data — currently undocumented.
- Add `winget`/Store app autostart detection (`Windows.ApplicationModel.StartupTask`)?
- Should `restore` be able to restore *system* state to a machine that never had the app?
