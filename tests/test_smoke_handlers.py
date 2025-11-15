import sys
import os
import traceback
import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import vos_emulator as v


def test_smoke_handlers_do_not_raise():
    """Call each implemented handler with an empty-args list and fail if
    any handler raises an uncaught exception. Handlers returning error
    strings are considered acceptable.
    """
    shell = v.VOShell()
    cmds = shell.commands
    stubs = set(v.STUB_COMMANDS)

    implemented = [k for k in cmds.keys() if k not in stubs]
    failures = []
    for name in sorted(implemented):
        handler = cmds[name]
        if not callable(handler):
            continue
        try:
            # call with an empty argument list; many handlers will return
            # usage/error strings but should not raise exceptions
            handler([])
        except Exception:
            failures.append((name, traceback.format_exc()))

    if failures:
        msgs = []
        for n, tb in failures:
            msgs.append(f"{n}:\n{tb}")
        pytest.fail(f"{len(failures)} handlers raised exceptions:\n\n" + "\n\n".join(msgs))
