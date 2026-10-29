# startupmgr

**List, disable and remove Windows startup programs — all 10 autostart sources, zero dependencies.**

Task Manager's Startup tab hides most of what actually runs when you log in. `startupmgr`
shows you everything: registry `Run`/`RunOnce` keys (including the 32-bit view), both
Startup folders, logon/boot scheduled tasks, and auto-start services — with the real
disabled/enabled state, publisher, and whether the target file still exists.

Then it lets you turn things off — safely, with an automatic backup before every
destructive change.

```
$ startupmgr list --search discord
  ID          STATE  SOURCE    NAME      COMMAND                                  PUBLISHER
  ─────────────────────────────────────────────────────────────────────────────────────────
  66f30be51d  off    HKCU Run  Discord   C:\Users\Ivy\AppData\Local\Discord\Update.exe  Discord Inc.

  1 entries · 0 enabled · 1 disabled
```

---

## Quick start

```bash
uv sync
uv run startupmgr doctor          # sanity-check the environment
uv run startupmgr list            # show everything
```

Prefer a global command?

```bash
uv tool install .
startupmgr list
```

---

## What it reads

| Source | Where it lives |
|---|---|
| `registry-user-run` | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` |
| `registry-user-runonce` | `HKCU\...\RunOnce` |
| `registry-machine-run` | `HKLM\...\Run` |
| `registry-machine-runonce` | `HKLM\...\RunOnce` |
| `registry-machine-run-wow64` | `HKLM\Software\WOW6432Node\...\Run` (32-bit view) |
| `registry-machine-runonce-wow64` | `HKLM\Software\WOW6432Node\...\RunOnce` |
| `startup-folder-user` | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup` |
| `startup-folder-common` | `%ProgramData%\Microsoft\Windows\Start Menu\Programs\Startup` |
| `scheduled-task` | Task Scheduler — Logon + Boot triggers only |
| `service` | Services with `StartMode = Auto` |

Disabled state is decoded from the undocumented `Explorer\StartupApproved` binary blob
(byte 0: `0x02`/`0x06` = enabled, `0x03` = disabled), so the `STATE` column matches
Task Manager exactly. An unknown marker is reported as `?` rather than guessed.

---

## Commands

### `list` (alias `ls`)

```bash
startupmgr list                          # everything, table
startupmgr list --source service         # one source
startupmgr list --scope user --enabled-only
startupmgr list --search chrome          # name / path / publisher
startupmgr list --missing-only           # broken entries (file gone)
startupmgr list -f json                  # also: jsonl, csv
```

| Flag | Meaning |
|---|---|
| `-s, --source <id>` | limit to a source (repeatable) |
| `--scope user\|machine` | only HKCU-side or HKLM-side entries |
| `-q, --search <text>` | substring match on name, command, path, publisher |
| `--enabled-only` / `--disabled-only` | state filter (mutually exclusive) |
| `--missing-only` | target file no longer exists |
| `-f, --format` | `table` (default), `json`, `jsonl`, `csv` |
| `--no-summary` | hide the trailing count line |

### `show <id|name>`

Full record for one entry, including resolved `exe`, `args`, `publisher`,
`file_version` and its stable id.

```bash
startupmgr show laragon
startupmgr show 766ba47e02
```

### `disable` / `enable`

Non-destructive. Flips the `StartupApproved` state — the underlying `Run` value or
`.lnk` file is left untouched, exactly like unticking a box in Task Manager.

```bash
startupmgr disable Discord
startupmgr enable  66f30be51d
```

Startup-folder items and `Run` values support this. `RunOnce` keys do not track state,
so they can only be deleted.

### `delete`

Permanently removes the entry. **A full snapshot is written first**, and `.lnk` files
are copied to quarantine rather than destroyed.

```bash
startupmgr delete Discord
startupmgr delete 66f30be51d --dry-run     # show the plan, change nothing
```

Output tells you the undo command:

```
✗ deleted Discord  66f30be51d

  backup written: C:\Users\Ivy\AppData\Local\startupmgr\backups\2026-10-29T18-03-35Z_pre-delete.json
  undo with: startupmgr restore C:\Users\Ivy\AppData\Local\startupmgr\backups\2026-10-29T18-03-35Z_pre-delete.json
```

### `backup` / `backups` / `restore`

```bash
startupmgr backup --label before-cleanup
startupmgr backups
startupmgr restore latest
startupmgr restore <path> --dry-run
```

`restore` is **surgical**: it checks each snapshot entry first and only re-creates the
ones that are actually gone. Running it twice is a no-op, not a double-apply.

```
  ✓ ZZTestItem                       restored C:\Users\Ivy\...\Startup\ZZTestItem.lnk

  1 restored · 139 already present · 140 in snapshot
```

### `tui` — mode interaktif

Tabel navigasi arrow key. Butuh dependensi opsional `textual`:

```bash
uv tool install . --with textual --force     # global
# atau di project: uv sync --all-extras
```

```bash
startupmgr tui                # tabel penuh
startupmgr tui -s service     # mulai terfilter satu sumber
```

| Tombol | Aksi |
|---|---|
| `↑` `↓` | pindah baris |
| `d` | disable entri terpilih |
| `e` | enable entri terpilih |
| `/` | cari (name/path/publisher) |
| `s` | ganti sumber (cycle) |
| `r` | scan ulang |
| `q` | keluar |

Setiap toggle menulis snapshot dulu (sama seperti CLI), dan perubahan tampil
langsung di layar. Entri machine-scope butuh terminal Administrator — kalau tidak,
muncul pesan tanpa mengubah apa pun. Service memang tidak bisa di-toggle dari TUI.

### `audit` / `doctor` / `sources`

```bash
startupmgr audit -n 20        # tail the mutation log
startupmgr doctor             # platform, elevation, store path, entry count
startupmgr sources            # list every source id
```

---

## Safety model

This tool touches things that run at boot, so it is deliberately conservative.

1. **Backup before mutation.** `enable`, `disable` and `delete` all write a snapshot first.
2. **Services are never deleted.** The command refuses outright — disabling is the
   strongest thing allowed, and it requires an explicit flag.
3. **Core services are hard-blocked.** `RpcSs`, `DcomLaunch`, `BFE`, `WinDefend`,
   `gpsvc`, `EventLog` and ~30 others cannot be disabled at all.
4. **Elevation is checked up front.** Machine-scope writes fail with exit code `4` and a
   clear hint instead of a half-applied change.
5. **`--dry-run` on every mutation.**
6. **Everything is logged** to `%LOCALAPPDATA%\startupmgr\audit.log` (append-only JSONL).

### Disabling a service

```bash
startupmgr disable WwanSvc                        # refused
startupmgr disable WwanSvc --i-know-what-im-doing # allowed (as admin)
startupmgr delete  WwanSvc                        # always refused
```

---

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | generic error |
| 2 | bad usage |
| 3 | entry not found |
| 4 | elevation required |
| 5 | operation refused (safety guard) |
| 6 | not running on Windows |

Useful for scripting:

```bash
if ! startupmgr show myapp >/dev/null 2>&1; then
  echo "myapp is not registered to start"
fi
```

---

## Scripting examples

**Export a fleet audit as JSON:**

```bash
startupmgr list -f json > "$COMPUTERNAME-startup.json"
```

**Find broken autostarts:**

```bash
startupmgr list --missing-only -f csv
```

**Disable everything from one publisher:**

```bash
startupmgr list -f json \
  | jq -r '.entries[] | select(.publisher | test("Adobe"; "i")) | .id' \
  | xargs -r -n1 startupmgr disable
```

**Daily snapshot via Task Scheduler (PowerShell):**

```powershell
schtasks /create /tn "startupmgr snapshot" /sc daily /st 09:00 `
  /tr "uv run --project C:\tools\startup-manager startupmgr backup --label daily"
```

---

## Data locations

Everything lives under `%LOCALAPPDATA%\startupmgr` (override with `STARTUPMGR_HOME`):

```
startupmgr/
├── backups/      full JSON snapshots (one per mutation + manual backups)
├── quarantine/   .lnk files removed by `delete`
└── audit.log     append-only JSONL of every mutation
```

---

## JSON shape

```json
{
  "count": 1,
  "entries": [
    {
      "id": "766ba47e02",
      "name": "laragon",
      "source": "startup-folder-user",
      "scope": "user",
      "enabled": true,
      "location": "C:\\Users\\Ivy\\AppData\\Roaming\\Microsoft\\Windows\\Start Menu\\Programs\\Startup",
      "command": "C:\\laragon\\laragon.exe",
      "exe": "C:\\laragon\\laragon.exe",
      "args": "",
      "exists": true,
      "publisher": "Le Ngoc Khoa",
      "file_version": "6.0.0.2",
      "deletable": true,
      "delete_block_reason": ""
    }
  ]
}
```

Ids are `sha1(source|name|location)[:10]`, so they are **stable across runs** — you can
script against them without a cache.

---

## Development

```bash
uv sync --all-extras
uv run pytest              # 35 tests
uv run ruff check .
uv run ruff format .
```

Layout:

```
startupmgr/
├── cli.py            argparse subcommands, exit codes
├── scanner.py        runs every collector, filters
├── models.py         StartupEntry, stable ids
├── render.py         table / json / csv, colour, CJK-aware widths
├── store.py          snapshots, restore, quarantine, audit log
├── winapi.py         ctypes + winreg wrappers (never raise)
└── collectors/
    ├── registry.py   Run / RunOnce + StartupApproved decode
    ├── folder.py     .lnk / .url, COM + binary fallback target resolve
    ├── tasks.py      Get-ScheduledTask via PowerShell, schtasks fallback
    └── services.py   Win32_Service, protected-service allowlist
```

Full requirements and design rationale: [`docs/PRD.md`](docs/PRD.md).

---

## Limitations

- **Windows only.** On other platforms every command exits `6`.
- **No boot-impact timing.** The ms figures in Task Manager are stored in undocumented
  `NTUSER.DAT` event data and are not exposed.
- **COM-handler tasks** show `<COM handler>` as their command — there is no executable
  to point at, only a COM class id.
- **Store/UWP app autostarts** (`Windows.ApplicationModel.StartupTask`) are not
  enumerated yet.
- **`RunOnce` entries** do not track enabled/disabled state in Windows, so they can only
  be deleted, not toggled.

---

## License

MIT.
