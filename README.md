# VOS Emulator (vos_emulator.py)

A small, safe emulator that simulates a subset of OpenVOS commands for interactive experimentation and testing. The emulator provides many conveniences (coloured output, tab completion, simple file operations inside a sandboxed state directory) while avoiding destructive changes to the host system outside the configured state area.

## Quick summary
- Language: Python 3.10+ (uses union type `X | None` and other modern features)
- Entry point: `vos_emulator.py` (run with `python vos_emulator.py`)
- Primary purpose: emulate VOS-style commands for learning, testing automation, or UI prototyping without needing a real VOS device
- Sandbox: file operations are performed under a configurable `STATE_DIR` (default `./vos_state`)

## Features
- Large collection of built-in command handlers (file/directory operations, display/status commands, device/system info simulators).
- Bulk registration of unimplemented commands from `vos_commands.txt` as simulated stubs (these appear in listings but return placeholder responses).
- Optional external command pack support via `commands.yaml` (simple mapping of command -> static output).
- Command parsing with support for:
  - single and two-token commands (e.g. `show status`, `change_current_dir`)
  - arguments (passed as a list to handlers)
  - prefix and glob-style matching for command names
  - fuzzy suggestions for unknown commands
- Tab completion and history when `readline` is available:
  - Command name completion with colour-coded suggestions (implemented vs simulated).
  - Path completion for file/directory commands using VOS-style '>' separators for convenience.
- Colourised terminal output via `colorama`.
- Small set of example file commands implemented: `create_file`, `copy_file`, `move_file`, `delete_file`, `list`, `ls -l` style (`ls -l` implemented as `ls_long` handler), `display_file`, `dump_file`, `compare_files`, directory operations and clones.
- Utility commands: `display_device_info`, `display_disk_info`, `display_system_usage`, `show commands status`, `help`, and more.

## Requirements
- Python 3.10 or later
- Required Python package:
  - `colorama` (for coloured output) — install with `pip install colorama`
- Optional:
  - `pyyaml` to load `commands.yaml` packs (`pip install pyyaml`)
  - `readline` is commonly available on Linux; macOS may use libedit. If present, the emulator provides tab completion and persistent history.

## Files and directories used by the emulator
- `vos_emulator.py` — main emulator script.
- `vos_state/` — default directory used for all sandboxed file operations; created automatically.
- `vos_commands.txt` — optional newline-separated list of official VOS command names. When present, the emulator will:
  - bulk-register names from this file as simulated stub commands (if they have no handler),
  - use this list as the canonical set for some filtering / reporting operations.
- `commands.yaml` — optional YAML mapping of command -> static string output to extend or override commands without changing Python code.
- `~/.vos_emulator_history` — (if `readline` available) saved history file.

## How to run
1. Ensure dependencies are installed:
   - pip install colorama
   - (optional) pip install pyyaml
2. Run:
   - python vos_emulator.py
3. In the interactive prompt:
   - Type `commands` to list available commands.
   - Type `help <command>` to get usage details for a handler.
   - Use `exit` or `Ctrl-D` to quit.

## Common usage examples
- List commands:
  - commands
- Get help:
  - help create_file
- Create a file inside emulator state:
  - create_file mydir>example.txt
- List contents of emulator state or a directory:
  - list
  - list mydir
- Show file contents:
  - display_file mydir>example.txt
- Change the emulator current directory (VOS style uses `>` separators):
  - change_current_dir >Sales>Jones
  - display_current_dir
- Compare two files:
  - compare_files a.txt b.txt
- Show which commands are implemented vs simulated:
  - show commands status

Notes:
- Use `>` as a VOS-style separator in arguments (e.g. `>Sales>Jones`) or provide standard host paths (`/tmp/...`) — absolute host paths are honoured as-is.
- If you omit the `>` prefix, paths are interpreted relative to the emulator's current working directory inside `STATE_DIR`.

## Extending and customisation
- Add static command outputs via `commands.yaml`:
  - YAML structure: `command_name: "static output string"`
  - Commands from the YAML file will be added/override the internal mapping at startup.
- Bulk-register official command names:
  - Create a `vos_commands.txt` file (one canonical command name per line). Any names not implemented in Python will be added as simulated stubs. Stubs are tracked and shown as "Not implemented" by `show commands status`.
- Implement new handlers in Python:
  1. Define a function `h_new_command(args)` returning a string (or use `_ok()` / `_err()` helpers).
  2. Add the mapping `"new_command": h_new_command` into the `BUILTINS` OrderedDict in `vos_emulator.py`, or provide it via `commands.yaml` for static responses.
  3. If the handler acts on files, add the name to the `FILE_COMMANDS` or `DIR_COMMANDS` sets to enable path completion.
- Change the sandbox directory at runtime:
  - `set state_dir /absolute/path/to/dir` (or relative) — sets `STATE_DIR` and creates it if missing. All subsequent file operations use the new location.

## Design notes & limitations
- The emulator is intentionally non-destructive to the host: relative paths and most file operations are confined to `STATE_DIR`. Absolute host paths are allowed but may perform real changes on your system — use with care.
- Simulated commands (stubs) return a placeholder; they do not attempt to emulate full behaviour.
- The emulator provides only a representative subset of VOS functionality and is not a replacement for real VOS firmware or a full virtual appliance.
- Behaviour can differ across platforms (e.g., `readline` availability, `/etc/passwd` parsing, `os.getloadavg()` presence).

## Troubleshooting
- "Please install 'colorama'": install colorama via pip.
- Tab completion not working: ensure your Python runtime includes `readline` or an appropriate equivalent (on macOS, ensure the system or brew-provided readline is available).
- If you need the exact VOS command list from the official manual, place a copy into `vos_commands.txt` — the emulator will bulk-register names and mark unimplemented ones as simulated.

## Examples (session)
- Create and inspect files:
  - create_file >Demo>file1.txt
  - display_file >Demo>file1.txt
  - list >Demo
- Directory operations:
  - create_dir >Demo>sub
  - copy_dir >Demo >Demo_copy
  - compare_dirs >Demo >Demo_copy
- Status and help:
  - display_system_usage
  - help compare_files
  - show commands status

## License
This emulator is provided as-is for testing and learning. There is no warranty; use at your own risk.

<!-- End of README -->
