import sys
import os
import re

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import vos_emulator as v


def test_alias_resolves_to_status():
    s = v.VOShell()
    key, handler, args = s._resolve('show commands report')
    assert key == 'show commands status'
    assert callable(handler)


def test_report_contains_header_and_colours():
    out = v.h_show_commands_status([])
    assert isinstance(out, str)
    # header text present
    assert 'VOS commands report' in out
    # colour codes (ANSI) should be present via colorama Fore constants
    # Look for escape sequences or the literal color names by checking for the
    # presence of the magenta header or ANSI CSI sequences (\x1b[)
    assert '\x1b[' in out or v.Fore.MAGENTA in out
    # Ensure implemented and simulated sections exist
    assert 'Implemented commands' in out
    assert 'Simulated (not implemented) commands' in out
