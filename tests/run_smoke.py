#
# usr/bin/env python3
"""Smoke test: exercise implemented VOS command handlers to ensure they don't crash.

This script instantiates the emulator (which registers builtins and stubs),
then calls each implemented command handler with an empty argument list. The
goal is to detect handlers that raise exceptions when invoked with a safe
input. Handlers that return error strings are considered OK; only uncaught
exceptions are treated as failures.
"""
from __future__ import annotations
import traceback
import sys
import os

import sys
import os

# Make the repository root importable so we can import vos_emulator as a module.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import vos_emulator


def main():
    # Ensure state dirs exist (same logic as emulator uses)
    os.makedirs(vos_emulator.STATE_DIR, exist_ok=True)
    os.makedirs(vos_emulator.BATCH_ROOT, exist_ok=True)

    shell = vos_emulator.VOShell()
    cmds = shell.commands
    stubs = set(vos_emulator.STUB_COMMANDS)

    implemented = [k for k in cmds.keys() if k not in stubs]
    print(f"Found {len(cmds)} registered commands, {len(implemented)} implemented (non-stub).")

    failures = []
    for name in sorted(implemented):
        handler = cmds[name]
        # Only test callables
        if not callable(handler):
            continue
        try:
            # Call with empty args list; handlers are expected to accept a list
            _ = handler([])
        except Exception as e:
            tb = traceback.format_exc()
            failures.append((name, str(e), tb))

    if failures:
        print(f"\n{len(failures)} handlers raised exceptions:\n")
        for n, err, tb in failures:
            print(f"- {n}: {err}")
            print(tb)
        sys.exit(2)

    print("\nAll implemented handlers returned (no uncaught exceptions).")


if __name__ == '__main__':
    main()
