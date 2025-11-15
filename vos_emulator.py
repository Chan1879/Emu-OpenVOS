#!/usr/bin/env python3
"""
VOS Command Emulator - extended

This emulator provides a simulated command line interface that mimics a subset of the
VOS (Virtual Operating System) command set. It supports more than forty built-in
commands, colourises output, and allows prefix and glob matching (e.g. "show sy*",
"net sho inter*"). Commands may be extended or overridden via an optional YAML
command pack (commands.yaml). A simple history and tab completion are available
when the readline module is present. Additionally, a plain text file (vos_commands.txt)
may be supplied to bulk-register new commands; each will return a placeholder
string until a handler is implemented.

This revision adds support for argument parsing. You can now call commands with
parameters (e.g. ``create_file myfile.txt``) and the emulator will pass the
arguments to the appropriate handler. A handful of file-operation commands have
been implemented as examples: ``create_file``, ``copy_file``, ``move_file`` and
``compare_files``.
"""

import os
import sys
import time
import random
import shutil
import fnmatch
import textwrap
import stat
import difflib
import json  # for storing batch job information
from cmd import Cmd
from collections import OrderedDict
from typing import List, Tuple, Optional, Dict, Callable, Any

# optional module
try:
    import readline  # noqa: F401
except Exception:
    readline = None

try:
    from colorama import Fore, Style, init as colorama_init
except Exception:
    print("Please install 'colorama' (pip install colorama)")
    sys.exit(1)

try:
    import yaml  # for external commands pack
except Exception:
    yaml = None  # still works without it

# initialise colour support
colorama_init(autoreset=True)

# prompt and random generator
PROMPT = f"{Fore.GREEN}vos{Style.BRIGHT}{Fore.WHITE}>{Style.RESET_ALL} "
RAND = random.Random(42)

# Additional global settings for commands that modify state. These simulate
# VOS environment variables and configuration settings. They are stored in
# memory and reset when the emulator process exits. The defaults mimic
# reasonable initial values.

# Library paths for add/delete/list library path commands
LIBRARY_PATHS: List[str] = []
# Current language setting for set language command
CURRENT_LANGUAGE: str = "en"
# Current time zone setting for set time zone command
CURRENT_TIME_ZONE: str = "UTC"
# Line wrap width for set line wrap width command
LINE_WRAP_WIDTH: int = 80
# Current profile name for the profile command. The ``profile`` command shows or
# changes this value. Additional profiles can be created with ``add_profile``.
CURRENT_PROFILE: str = "default"

# A registry of user profiles. The initial list contains only the default
# profile. The ``profile`` command will update CURRENT_PROFILE, and
# ``add_profile`` can be used to add new entries. These do not persist
# beyond the lifetime of the emulator process.
PROFILES: List[str] = [CURRENT_PROFILE]

# Base state directory for all emulator data. The ``vos_state`` directory
# contains separate subdirectories for the file ``filesystem`` and for
# internal batch queues. This separation prevents batch files from
# interfering with ordinary file operations. The base directory can be
# changed at runtime via the ``set state_dir`` command.
BASE_STATE_DIR = os.path.join(os.getcwd(), "vos_state")
# Ensure the base state directory exists.
os.makedirs(BASE_STATE_DIR, exist_ok=True)
# Directory for file operations (the emulated filesystem). Files created
# via commands like ``create_file`` live under this directory.
STATE_DIR = os.path.join(BASE_STATE_DIR, "filesystem")
# Ensure the filesystem directory exists.
os.makedirs(STATE_DIR, exist_ok=True)

# Current directory within STATE_DIR for file operations.  This value is
# always relative to STATE_DIR.  An empty string means the root of
# STATE_DIR.  Commands such as ``change_current_dir`` will update this
# variable to point to a subdirectory of STATE_DIR.  All relative paths
# passed to file commands are resolved relative to this directory.
CURRENT_DIR: str = ""

# Track the last error produced by any command. Commands may update this
# variable via the helper functions. The ``display_error`` command returns
# its value to the user. If no error has occurred since startup, this is
# None.
LAST_ERROR: Optional[str] = None

# Collect system notices. Commands can append messages here; the
# ``display_notices`` command returns and clears this list. Currently, no
# command uses this facility, so it remains empty unless extended.
NOTICES: List[str] = []

# Keep track of stub commands registered via the bulk loader.  Any command
# name added to this set is considered unimplemented and will be listed
# separately by the ``show commands status`` command.  Stub functions
# created by ``register_bulk`` have a special attribute ``__vos_stub__``
# for introspection.
STUB_COMMANDS: set[str] = set()

# Directory for batch queues. All batch requests and state are stored under this
# path. Each queue name has its own subdirectory inside ``BATCH_ROOT``. Batch
# queues live in a separate ``vos_internals`` hierarchy under the base state
# directory so they do not interfere with the filesystem.
BATCH_ROOT = os.path.join(BASE_STATE_DIR, "vos_internals", "batches")
os.makedirs(BATCH_ROOT, exist_ok=True)


def _term_cols(default: int = 100) -> int:
    """Return the terminal width or a default value if it cannot be determined."""
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return default


# ---------- Utilities ----------
def c(text: Any, color: Fore = Fore.CYAN) -> str:
    """Colourise text for terminal display."""
    lines = str(text).splitlines() or [""]
    return "\n".join(f"{color}{ln}{Style.RESET_ALL}" for ln in lines)


def spinner(msg: str = "executing", seconds: float = 1.2) -> None:
    """Display a simple spinner for a given duration."""
    s = "|/-\\"
    end = time.time() + seconds
    i = 0
    w = _term_cols()
    while time.time() < end:
        sys.stdout.write(f"\r{Fore.YELLOW}{msg}... {s[i % 4]}{Style.RESET_ALL}")
        sys.stdout.flush()
        i += 1
        time.sleep(0.1)
    sys.stdout.write("\r" + " " * (w - 1) + "\r")
    sys.stdout.flush()


def table(headers: List[str], rows: List[Tuple[Any, ...]]) -> str:
    """Render a simple table with even column widths."""
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(f"{h:<{widths[i]}}" for i, h in enumerate(headers))
    body = "\n".join("  ".join(f"{str(cell):<{widths[i]}}" for i, cell in enumerate(r)) for r in rows)
    return f"{line}\n" + "-" * len(line) + ("\n" + body if body else "")


# ---------- Command Packs ----------
def now() -> str:
    """Return the current local time as a formatted string."""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _mode_to_str(mode: int) -> str:
    """Convert a file mode to a simplified ls -l style string."""
    is_dir = "d" if stat.S_ISDIR(mode) else "-"
    perms = [
        (stat.S_IRUSR, 'r'), (stat.S_IWUSR, 'w'), (stat.S_IXUSR, 'x'),
        (stat.S_IRGRP, 'r'), (stat.S_IWGRP, 'w'), (stat.S_IXGRP, 'x'),
        (stat.S_IROTH, 'r'), (stat.S_IWOTH, 'w'), (stat.S_IXOTH, 'x'),
    ]
    s = ''.join(ch if (mode & bit) else '-' for bit, ch in perms)
    return is_dir + s


def _dir_names() -> List[str]:
    """Return a sorted list of entries in the current directory."""
    try:
        return sorted(os.listdir("."))
    except Exception as e:
        return [f"[error listing dir: {e}]"]


# ---------- Argument helpers and file handlers ----------
def _split_cmd_args(line: str, commands_ci: Dict[str, Tuple[str, Any]]) -> Tuple[str, List[str]]:
    """Split a command line into the command head and its arguments.

    This helper will attempt to match the first two tokens as a single command if
    such a command exists in the case-insensitive command dictionary. Otherwise
    it falls back to a single token head.
    """
    parts = line.strip().split()
    if not parts:
        return "", []
    # try two-word head
    if len(parts) >= 2:
        two = f"{parts[0]} {parts[1]}".lower()
        if two in commands_ci:
            return parts[0] + " " + parts[1], parts[2:]
    # fallback to single word
    return parts[0], parts[1:]


def _ok(msg: str) -> str:
    return f"[OK] {msg}"


def _err(msg: str) -> str:
    global LAST_ERROR
    # record the last error message and return a formatted error string
    LAST_ERROR = msg
    return f"[ERR] {msg}"


def h_create_file(args: List[str]):
    """Create an empty file at the given path."""
    if not args or len(args) != 1:
        return _err("usage: create_file <path>")
    # resolve into the state directory unless absolute
    path = resolve_state_path(args[0])
    try:
        # ensure parent directory exists
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # "a" ensures the file is created if it doesn't exist
        with open(path, "a", encoding="utf-8"):
            pass
        return _ok(f"created file: {path}")
    except Exception as e:
        return _err(f"create_file: {e}")


def h_copy_file(args: List[str]):
    """Copy a file from source to destination."""
    if not args or len(args) != 2:
        return _err("usage: copy_file <src> <dst>")
    src, dst = args
    # resolve source and destination into state dir if relative
    src_path = resolve_state_path(src)
    dst_path = resolve_state_path(dst)
    try:
        # ensure destination directory exists
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        shutil.copy2(src_path, dst_path)
        return _ok(f"copied {src_path} -> {dst_path}")
    except Exception as e:
        return _err(f"copy_file: {e}")


def h_move_file(args: List[str]):
    """Move or rename a file from source to destination."""
    if not args or len(args) != 2:
        return _err("usage: move_file <src> <dst>")
    src, dst = args
    src_path = resolve_state_path(src)
    dst_path = resolve_state_path(dst)
    try:
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        shutil.move(src_path, dst_path)
        return _ok(f"moved {src_path} -> {dst_path}")
    except Exception as e:
        return _err(f"move_file: {e}")


def h_compare_files(args: List[str]):
    """Compare two files byte by byte and report the first difference."""
    if not args or len(args) != 2:
        return _err("usage: compare_files <a> <b>")
    a, b = args
    a_path = resolve_state_path(a)
    b_path = resolve_state_path(b)
    try:
        with open(a_path, "rb") as fa, open(b_path, "rb") as fb:
            ca, cb = fa.read(), fb.read()
        if ca == cb:
            return "Files are identical."
        # find the first differing byte offset
        lim = min(len(ca), len(cb))
        off = next((i for i in range(lim) if ca[i:i + 1] != cb[i:i + 1]), lim)
        return f"Files differ at byte {off}"
    except Exception as e:
        return _err(f"compare_files: {e}")


def h_delete_file(args: List[str]):
    """Delete a file at the given path."""
    if not args or len(args) != 1:
        return _err("usage: delete_file <path>")
    # resolve into state directory
    path = resolve_state_path(args[0])
    try:
        os.remove(path)
        return _ok(f"deleted {path}")
    except Exception as e:
        return _err(f"delete_file: {e}")


def h_create_dir(args: List[str]):
    """Create a directory recursively inside the state directory."""
    if not args or len(args) != 1:
        return _err("usage: create_dir <path>")
    path = resolve_state_path(args[0])
    try:
        os.makedirs(path, exist_ok=True)
        return _ok(f"created directory: {path}")
    except Exception as e:
        return _err(f"create_dir: {e}")


def h_copy_dir(args: List[str]):
    """Copy a directory recursively from source to destination."""
    if not args or len(args) != 2:
        return _err("usage: copy_dir <src> <dst>")
    src, dst = args
    src_path = resolve_state_path(src)
    dst_path = resolve_state_path(dst)
    try:
        shutil.copytree(src_path, dst_path, dirs_exist_ok=True)
        return _ok(f"copied directory {src_path} -> {dst_path}")
    except Exception as e:
        return _err(f"copy_dir: {e}")


def h_move_dir(args: List[str]):
    """Move or rename a directory from source to destination."""
    if not args or len(args) != 2:
        return _err("usage: move_dir <src> <dst>")
    src, dst = args
    src_path = resolve_state_path(src)
    dst_path = resolve_state_path(dst)
    try:
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        shutil.move(src_path, dst_path)
        return _ok(f"moved directory {src_path} -> {dst_path}")
    except Exception as e:
        return _err(f"move_dir: {e}")


def h_delete_dir(args: List[str]):
    """Delete a directory recursively inside the state directory."""
    if not args or len(args) != 1:
        return _err("usage: delete_dir <path>")
    path = resolve_state_path(args[0])
    try:
        shutil.rmtree(path)
        return _ok(f"deleted directory: {path}")
    except Exception as e:
        return _err(f"delete_dir: {e}")


def h_rename(args: List[str]):
    """Rename a file or directory inside the state directory."""
    if not args or len(args) != 2:
        return _err("usage: rename <src> <dst>")
    src, dst = args
    src_path = resolve_state_path(src)
    dst_path = resolve_state_path(dst)
    try:
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        os.rename(src_path, dst_path)
        return _ok(f"renamed {src_path} -> {dst_path}")
    except Exception as e:
        return _err(f"rename: {e}")


# Additional file operation commands

def h_list(args: List[str]):
    """List files in a directory relative to the state directory.

    If no path is specified, lists the contents of the current state directory. If a
    single path is provided, it is resolved into the state directory (unless
    absolute) and listed. Only one path argument is accepted.
    """
    if args and len(args) > 1:
        return _err("usage: list [path]")
    target = args[0] if args else ''
    # If no target specified, list the current working directory within STATE_DIR
    if not target:
        path = os.path.join(STATE_DIR, CURRENT_DIR) if CURRENT_DIR else STATE_DIR
    else:
        path = resolve_state_path(target)
    try:
        if not os.path.exists(path):
            return _err(f"path not found: {path}")
        if os.path.isdir(path):
            items = sorted(os.listdir(path))
            # Mark directories with a trailing slash for clarity
            display = []
            for item in items:
                full = os.path.join(path, item)
                if os.path.isdir(full):
                    display.append(item + '/')
                else:
                    display.append(item)
            return "\n".join(display) if display else "(empty)"
        # if it's a file, just return its name
        return os.path.basename(path)
    except Exception as e:
        return _err(f"list: {e}")


def h_set_state_dir(args: List[str]):
    """Set a new base state directory for the emulator.

    The provided directory becomes the new root under which all emulator
    state is stored. Within this base directory a ``filesystem`` subdirectory
    is created for file operations, and a ``vos_internals/batches`` hierarchy
    is created for batch queues. This ensures file operations and batch
    jobs remain separate. Both ``STATE_DIR`` and ``BATCH_ROOT`` are
    updated accordingly.
    """
    global BASE_STATE_DIR, STATE_DIR, BATCH_ROOT
    if not args or len(args) != 1:
        return _err("usage: set state_dir <directory>")
    new_dir = args[0]
    # Convert to an absolute path for consistency
    if not os.path.isabs(new_dir):
        new_dir = os.path.abspath(new_dir)
    try:
        # Set the new base directory and ensure it exists
        BASE_STATE_DIR = new_dir
        os.makedirs(BASE_STATE_DIR, exist_ok=True)
        # Create/assign the filesystem subdirectory
        STATE_DIR = os.path.join(BASE_STATE_DIR, "filesystem")
        os.makedirs(STATE_DIR, exist_ok=True)
        # Create/assign the batch root under the base directory
        BATCH_ROOT = os.path.join(BASE_STATE_DIR, "vos_internals", "batches")
        os.makedirs(BATCH_ROOT, exist_ok=True)
        return _ok(f"state directory set to {BASE_STATE_DIR}")
    except Exception as e:
        return _err(f"set state_dir: {e}")


def h_show_state_dir(args: List[str]):
    """Display the current base state directory."""
    return f"Current state directory: {BASE_STATE_DIR}"


def resolve_state_path(filename: str) -> str:
    """Resolve a VOS-style filename to an absolute path within STATE_DIR.

    Filenames in OpenVOS often use '>' as a directory separator, e.g.
    ``>Sales>Jones``.  This helper converts such VOS paths into host
    file system paths rooted at ``STATE_DIR`` while respecting the
    current working directory (CURRENT_DIR).  The rules are:

    - If ``filename`` is empty or None, the current working directory is
      returned (``STATE_DIR`` joined with ``CURRENT_DIR``).
    - Absolute host filesystem paths (those starting with ``/`` or a
      drive letter on Windows) are returned unchanged; this allows the
      caller to bypass the state directory explicitly.
    - If the filename contains '>' separators and starts with '>', it
      represents an absolute VOS path relative to ``STATE_DIR``; the
      leading '>' is stripped and the remaining components are joined
      directly onto ``STATE_DIR``.
    - If the filename contains '>' separators but does not begin with
      '>', it is considered relative to the current working directory;
      its segments are appended after ``CURRENT_DIR``.
    - All other paths (those without '>' separators) are treated as
      relative to the current working directory.  They are joined
      beneath ``STATE_DIR`` and ``CURRENT_DIR``.

    Parameters
    ----------
    filename : str
        A VOS-style or host filesystem pathname.

    Returns
    -------
    str
        The resolved absolute pathname on the host filesystem.
    """
    # Empty filename returns the current directory
    if not filename:
        return os.path.join(STATE_DIR, CURRENT_DIR) if CURRENT_DIR else STATE_DIR
    # Normalise whitespace
    fname = filename.strip()
    # Host absolute path: return as is
    if os.path.isabs(fname):
        return fname
    # Paths with VOS separators
    if '>' in fname:
        parts = [p for p in fname.split('>') if p]
        if fname.startswith('>'):
            # Absolute VOS path: ignore CURRENT_DIR
            return os.path.join(STATE_DIR, *parts)
        # Relative VOS path: prefix CURRENT_DIR if set
        base_parts = CURRENT_DIR.split(os.sep) if CURRENT_DIR else []
        return os.path.join(STATE_DIR, *(base_parts + parts))
    # Simple relative path: join under CURRENT_DIR
    base = os.path.join(STATE_DIR, CURRENT_DIR) if CURRENT_DIR else STATE_DIR
    return os.path.join(base, fname)


# ---------------------------------------------------------------------------
# Additional command handlers
#
# The following functions implement a selection of VOS-like commands beyond
# basic file creation and copying. Where possible, they operate on the
# emulator's state directory (STATE_DIR) to avoid altering the host file
# system outside of the designated sandbox. Many other commands are still
# simulated via stubs.

def h_display_file(args: List[str]):
    """Display the contents of a text file.

    Reads and returns the content of the specified file. The filename is
    resolved relative to STATE_DIR unless an absolute path is provided.
    Lines are wrapped according to the global LINE_WRAP_WIDTH setting. If
    the file is binary or cannot be decoded as UTF-8, undecodable bytes
    are replaced with the Unicode replacement character.
    Usage: display_file <path>
    """
    if not args or len(args) != 1:
        return _err("usage: display_file <path>")
    path = resolve_state_path(args[0])
    try:
        if os.path.isdir(path):
            return _err(f"display_file: {path} is a directory")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        # wrap each line to the configured width
        wrapped: List[str] = []
        width = max(1, LINE_WRAP_WIDTH)
        for ln in lines:
            # use textwrap.fill to wrap long lines; preserve empty lines
            if ln:
                wrapped.append(textwrap.fill(ln, width=width))
            else:
                wrapped.append("")
        return "\n".join(wrapped)
    except Exception as e:
        return _err(f"display_file: {e}")


def h_display_file_status(args: List[str]):
    """Display status information about a file.

    Shows basic metadata such as the file name, full path, type, size and
    modification time. Usage: display_file_status <path>
    """
    if not args or len(args) != 1:
        return _err("usage: display_file_status <path>")
    path = resolve_state_path(args[0])
    try:
        st = os.stat(path)
        name = os.path.basename(path)
        ftype = "directory" if stat.S_ISDIR(st.st_mode) else "file" if stat.S_ISREG(st.st_mode) else "other"
        size = st.st_size
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
        return (
            f"Name: {name}\n"
            f"Path: {path}\n"
            f"Type: {ftype}\n"
            f"Size: {size} bytes\n"
            f"Modified: {mtime}"
        )
    except Exception as e:
        return _err(f"display_file_status: {e}")


def h_display_dir_status(args: List[str]):
    """Display a summary of a directory's contents.

    Returns the number of files and subdirectories contained in the specified
    directory, along with their combined size in bytes. If no path is
    specified, STATE_DIR is used. Usage: display_dir_status [path]
    """
    if args and len(args) > 1:
        return _err("usage: display_dir_status [path]")
    target = args[0] if args else ''
    path = resolve_state_path(target) if target else STATE_DIR
    try:
        if not os.path.isdir(path):
            return _err(f"display_dir_status: {path} is not a directory")
        total_files = 0
        total_dirs = 0
        total_size = 0
        for root, dirs, files in os.walk(path):
            total_dirs += len(dirs)
            total_files += len(files)
            for fname in files:
                try:
                    fpath = os.path.join(root, fname)
                    total_size += os.path.getsize(fpath)
                except Exception:
                    pass
        return (
            f"Directory: {path}\n"
            f"Subdirectories: {total_dirs}\n"
            f"Files: {total_files}\n"
            f"Total size: {total_size} bytes"
        )
    except Exception as e:
        return _err(f"display_dir_status: {e}")


def h_display_disk_usage(args: List[str]):
    """Display disk usage information.

    Reports the total, used and free space for the given path (default
    STATE_DIR) in human readable form. Usage: display_disk_usage [path]
    """
    if args and len(args) > 1:
        return _err("usage: display_disk_usage [path]")
    target = args[0] if args else ''
    path = resolve_state_path(target) if target else STATE_DIR
    try:
        usage = shutil.disk_usage(path)

        def fmt(bytes_value: int) -> str:
            # convert bytes to a human-friendly string with units
            for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
                if bytes_value < 1024:
                    return f"{bytes_value:.2f} {unit}"
                bytes_value /= 1024
            return f"{bytes_value:.2f} PB"

        return (
            f"Path: {path}\n"
            f"Total: {fmt(usage.total)}\n"
            f"Used: {fmt(usage.used)}\n"
            f"Free: {fmt(usage.free)}"
        )
    except Exception as e:
        return _err(f"display_disk_usage: {e}")


def h_dump_file(args: List[str]):
    """Dump a file in a simple hexadecimal representation.

    Reads the specified file and returns its contents in hex with offsets.
    Usage: dump_file <path>
    """
    if not args or len(args) != 1:
        return _err("usage: dump_file <path>")
    path = resolve_state_path(args[0])
    try:
        if os.path.isdir(path):
            return _err(f"dump_file: {path} is a directory")
        with open(path, "rb") as f:
            data = f.read()
        lines: List[str] = []
        for offset in range(0, len(data), 16):
            chunk = data[offset:offset + 16]
            hex_bytes = ' '.join(f"{b:02x}" for b in chunk)
            # pad last line if fewer than 16 bytes
            hex_bytes = hex_bytes.ljust(16 * 3 - 1)
            ascii_bytes = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
            lines.append(f"{offset:08x}  {hex_bytes}  {ascii_bytes}")
        return "\n".join(lines)
    except Exception as e:
        return _err(f"dump_file: {e}")


def h_list_users(args: List[str]):
    """List users on the host system.

    Retrieves user names from /etc/passwd. On systems where this file is
    unavailable, it returns a fixed sample list. Usage: list_users
    """
    if args:
        return _err("usage: list_users")
    try:
        users: List[str] = []
        with open("/etc/passwd", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip() and not line.startswith("#"):
                    parts = line.split(":")
                    if parts:
                        users.append(parts[0])
        return "\n".join(sorted(set(users)))
    except Exception:
        # fallback sample users
        return "\n".join(["root", "guest", "user"])


def h_list_library_paths(args: List[str]):
    """List the currently configured library paths.

    Returns the list of library paths stored in LIBRARY_PATHS, one per line.
    If no paths have been added, returns a message indicating the list is empty.
    Usage: list_library_paths
    """
    if args:
        return _err("usage: list_library_paths")
    if not LIBRARY_PATHS:
        return "(no library paths configured)"
    return "\n".join(LIBRARY_PATHS)


def h_add_library_path(args: List[str]):
    """Add a directory to the library search path list.

    The specified path is added to the end of LIBRARY_PATHS if it is not
    already present. Usage: add_library_path <path>
    """
    if not args or len(args) != 1:
        return _err("usage: add_library_path <path>")
    path = args[0]
    if path not in LIBRARY_PATHS:
        LIBRARY_PATHS.append(path)
        return _ok(f"added library path: {path}")
    return _ok(f"library path already present: {path}")


def h_delete_library_path(args: List[str]):
    """Remove a directory from the library search path list.

    Removes the specified path from LIBRARY_PATHS if present. Usage:
    delete_library_path <path>
    """
    if not args or len(args) != 1:
        return _err("usage: delete_library_path <path>")
    path = args[0]
    try:
        LIBRARY_PATHS.remove(path)
        return _ok(f"removed library path: {path}")
    except ValueError:
        return _err(f"library path not found: {path}")


def h_set_language(args: List[str]):
    """Set the current language.

    Updates the CURRENT_LANGUAGE variable. This setting affects commands
    that might be localized. Usage: set_language <lang>
    """
    global CURRENT_LANGUAGE
    if not args or len(args) != 1:
        return _err("usage: set_language <language_code>")
    CURRENT_LANGUAGE = args[0]
    return _ok(f"language set to {CURRENT_LANGUAGE}")


def h_set_time_zone(args: List[str]):
    """Set the current time zone.

    Updates the CURRENT_TIME_ZONE variable. Usage: set_time_zone <zone>
    """
    global CURRENT_TIME_ZONE
    if not args or len(args) != 1:
        return _err("usage: set_time_zone <time_zone>")
    CURRENT_TIME_ZONE = args[0]
    return _ok(f"time zone set to {CURRENT_TIME_ZONE}")


def h_set_line_wrap_width(args: List[str]):
    """Set the width used for wrapping lines when displaying text.

    Accepts a positive integer. The LINE_WRAP_WIDTH controls how long lines
    are wrapped in commands like display_file. Usage: set_line_wrap_width <n>
    """
    global LINE_WRAP_WIDTH
    if not args or len(args) != 1:
        return _err("usage: set_line_wrap_width <number>")
    try:
        n = int(args[0])
        if n <= 0:
            return _err("line wrap width must be positive")
        LINE_WRAP_WIDTH = n
        return _ok(f"line wrap width set to {LINE_WRAP_WIDTH}")
    except ValueError:
        return _err("set_line_wrap_width: invalid number")


def h_profile(args: List[str]):
    """Show or change the current profile.

    With no arguments, displays the name of the current profile. With a single
    argument, sets the current profile to the given name (adding it to
    PROFILES if it does not already exist). Usage: profile [name]
    """
    global CURRENT_PROFILE
    if not args:
        return f"Current profile: {CURRENT_PROFILE}"
    if len(args) != 1:
        return _err("usage: profile [name]")
    name = args[0]
    if name not in PROFILES:
        PROFILES.append(name)
    CURRENT_PROFILE = name
    return _ok(f"profile set to {CURRENT_PROFILE}")


def h_add_profile(args: List[str]):
    """Create a new profile entry.

    Adds the specified profile name to the PROFILES list if it does not
    already exist. Usage: add_profile <name>
    """
    if not args or len(args) != 1:
        return _err("usage: add_profile <name>")
    name = args[0]
    if name in PROFILES:
        return _ok(f"profile already exists: {name}")
    PROFILES.append(name)
    return _ok(f"added profile: {name}")


def h_compare_dirs(args: List[str]):
    """Compare two directories recursively.

    Checks for differences in file names and sizes between two directories. If
    the contents match exactly (including subdirectories), returns a message
    indicating they are identical. Otherwise reports the first difference.
    Usage: compare_dirs <dir1> <dir2>
    """
    if not args or len(args) != 2:
        return _err("usage: compare_dirs <dir1> <dir2>")
    d1, d2 = resolve_state_path(args[0]), resolve_state_path(args[1])
    if not os.path.isdir(d1) or not os.path.isdir(d2):
        return _err("both arguments must be directories")

    def walk_dir(path: str) -> Dict[str, Optional[int]]:
        result: Dict[str, Optional[int]] = {}
        for root, dirs, files in os.walk(path):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), start=path)
                try:
                    result[rel] = os.path.getsize(os.path.join(root, f))
                except Exception:
                    result[rel] = None
        return result

    files1 = walk_dir(d1)
    files2 = walk_dir(d2)
    if files1 == files2:
        return "Directories are identical."
    # find differences
    only1 = set(files1) - set(files2)
    only2 = set(files2) - set(files1)
    diffs: List[str] = []
    if only1:
        diffs.append("Only in first: " + ", ".join(sorted(only1)))
    if only2:
        diffs.append("Only in second: " + ", ".join(sorted(only2)))
    # compare sizes for common files
    for rel in set(files1) & set(files2):
        if files1[rel] != files2[rel]:
            diffs.append(f"File size mismatch: {rel} (first: {files1[rel]}, second: {files2[rel]})")
    return "\n".join(diffs)


def h_clone_dir(args: List[str]):
    """Clone (copy) a directory recursively.

    This is an alias of copy_dir for convenience. Usage: clone_dir <src> <dst>
    """
    return h_copy_dir(args)


def h_clone_file(args: List[str]):
    """Clone (copy) a single file.

    Alias of copy_file. Usage: clone_file <src> <dst>
    """
    return h_copy_file(args)


def h_display_current_dir(args: List[str]):
    """Display the emulator's current working directory.

    Returns the current working directory in VOS notation.  The root
    directory is represented by a single '>' character.  For example, if
    the current directory is ``Sales/Jones`` under STATE_DIR, this
    command will return ``>Sales>Jones``.  Usage: display_current_dir
    """
    global CURRENT_DIR
    if args:
        return _err("usage: display_current_dir")
    if not CURRENT_DIR:
        return "Current directory: >"
    # Convert CURRENT_DIR into VOS format using '>' separators
    vos_path = '>' + '>'.join(CURRENT_DIR.split(os.sep))
    return f"Current directory: {vos_path}"


def h_display_current_module(args: List[str]):
    """Display the current module name.

    The emulator does not simulate modules, so this returns a fixed string.
    Usage: display_current_module
    """
    if args:
        return _err("usage: display_current_module")
    return "Current module: vos-emulator"


def h_display_date_time(args: List[str]):
    """Display the current date and time.

    Returns the current local time formatted as YYYY-MM-DD HH:MM:SS. Usage:
    display_date_time
    """
    if args:
        return _err("usage: display_date_time")
    return now()


def h_display_line(args: List[str]):
    """Display a line of text.

    Concatenates all arguments into a single string separated by spaces and
    returns it. Usage: display_line <text>
    """
    if not args:
        return _err("usage: display_line <text>")
    return " ".join(args)


def h_change_current_dir(args: List[str]):
    """Change the current working directory within STATE_DIR for file operations.

    This command updates the global CURRENT_DIR variable to point to a
    subdirectory of STATE_DIR. The argument may be a VOS-style path
    beginning with '>' (treated as absolute within STATE_DIR) or a
    relative path. If the target directory does not exist, it is
    created. Only a single argument is accepted. Usage:

        change_current_dir <path>

    """
    global CURRENT_DIR
    if not args or len(args) != 1:
        return _err("usage: change_current_dir <path>")
    target = args[0]
    # Resolve the path relative to STATE_DIR but ignoring CURRENT_DIR for absolute VOS paths
    new_path = resolve_state_path(target)
    # Ensure the directory exists; if not, create it
    try:
        #os.makedirs(new_path, exist_ok=True)
    # removed fil==older creation since it should not create dirs automatically
        if not os.path.isdir(new_path):
            return _err(f"change_current_dir: target directory does not exist: {new_path}")
    except Exception as e:
        return _err(f"change_current_dir: {e}")
    # Prevent changing outside of STATE_DIR (unless absolute host path provided)
    abs_state = os.path.abspath(STATE_DIR)
    abs_new = os.path.abspath(new_path)
    if not abs_new.startswith(abs_state):
        return _err(f"change_current_dir: target {new_path} is outside STATE_DIR")
    # Compute new CURRENT_DIR relative to STATE_DIR
    rel = os.path.relpath(abs_new, abs_state)
    CURRENT_DIR = '' if rel == '.' else rel
    return _ok(f"current directory set to {('>' + '>'.join(CURRENT_DIR.split(os.sep))) if CURRENT_DIR else '>'}")


def h_display_device_info(args: List[str]):
    """Display basic device (system) information.

    Returns details about the underlying host such as system name, node
    identifier, release, version, machine and processor. Usage:
    display_device_info
    """
    if args:
        return _err("usage: display_device_info")
    try:
        import platform
        info = platform.uname()
        return (
            f"System: {info.system}\n"
            f"Node: {info.node}\n"
            f"Release: {info.release}\n"
            f"Version: {info.version}\n"
            f"Machine: {info.machine}\n"
            f"Processor: {info.processor}"
        )
    except Exception as e:
        return _err(f"display_device_info: {e}")


def h_display_disk_info(args: List[str]):
    """Display information about root filesystem usage.

    Shows the total, used and free space of the root filesystem ('/').
    Usage: display_disk_info
    """
    if args:
        return _err("usage: display_disk_info")
    try:
        usage = shutil.disk_usage('/')

        def fmt(bytes_value: int) -> str:
            for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
                if bytes_value < 1024:
                    return f"{bytes_value:.2f} {unit}"
                bytes_value /= 1024
            return f"{bytes_value:.2f} PB"

        return (
            f"Filesystem: /\n"
            f"Total: {fmt(usage.total)}\n"
            f"Used: {fmt(usage.used)}\n"
            f"Free: {fmt(usage.free)}"
        )
    except Exception as e:
        return _err(f"display_disk_info: {e}")


def h_display_system_usage(args: List[str]):
    """Display simple system usage metrics.

    Shows load averages and memory usage on Unix-like systems. If the
    underlying operating system does not provide load averages or memory
    information, returns a simulated message. Usage: display_system_usage
    """
    if args:
        return _err("usage: display_system_usage")
    try:
        # load averages
        try:
            load1, load5, load15 = os.getloadavg()
        except Exception:
            load1 = load5 = load15 = None
        # memory
        mem_total = mem_free = mem_used = None
        if os.path.exists('/proc/meminfo'):
            try:
                with open('/proc/meminfo', 'r') as f:
                    meminfo: Dict[str, int] = {}
                    for line in f:
                        parts = line.split(':')
                        if len(parts) >= 2:
                            key = parts[0]
                            value = parts[1].strip().split()[0]
                            meminfo[key] = int(value)
                    if 'MemTotal' in meminfo and 'MemAvailable' in meminfo:
                        mem_total = meminfo['MemTotal'] * 1024
                        mem_free = meminfo['MemAvailable'] * 1024
                        mem_used = mem_total - mem_free
            except Exception:
                pass
        lines: List[str] = []
        if load1 is not None:
            lines.append(f"Load average (1m,5m,15m): {load1:.2f}, {load5:.2f}, {load15:.2f}")
        else:
            lines.append("Load average: not available")
        if mem_total is not None:
            def fmtmem(bytes_val: float) -> str:
                b = float(bytes_val)
                for u in ['B', 'KB', 'MB', 'GB', 'TB']:
                    if b < 1024:
                        return f"{b:.2f} {u}"
                    b /= 1024
                return f"{b:.2f} PB"
            lines.append(f"Memory total: {fmtmem(mem_total)}")
            lines.append(f"Memory used: {fmtmem(mem_used)}")
            lines.append(f"Memory free: {fmtmem(mem_free)}")
        else:
            lines.append("Memory usage: not available")
        return "\n".join(lines)
    except Exception as e:
        return _err(f"display_system_usage: {e}")


def h_display_error(args: List[str]):
    """Display the last error message reported by any command.

    Returns the value of LAST_ERROR or indicates that no error has occurred.
    Usage: display_error
    """
    if args:
        return _err("usage: display_error")
    if LAST_ERROR:
        return f"Last error: {LAST_ERROR}"
    return "No errors."


def h_display_notices(args: List[str]):
    """Display and clear accumulated system notices.

    Returns any messages stored in the NOTICES list and then clears it. If
    there are no notices, returns a message indicating so. Usage:
    display_notices
    """
    if args:
        return _err("usage: display_notices")
    if not NOTICES:
        return "(no notices)"
    notices = "\n".join(NOTICES)
    NOTICES.clear()
    return notices


def h_display_terminal_parameters(args: List[str]):
    """Display the terminal size parameters.

    Returns the number of columns and lines of the current terminal if
    available. Usage: display_terminal_parameters
    """
    if args:
        return _err("usage: display_terminal_parameters")
    try:
        size = shutil.get_terminal_size()
        return f"Columns: {size.columns}\nLines: {size.lines}"
    except Exception:
        return "Terminal parameters not available"


def _parse_size(size_str: str) -> Optional[int]:
    """Parse a size string with optional unit suffix and return bytes.

    Recognizes suffixes K, M, G, T for kilobytes, megabytes, gigabytes and
    terabytes. If no suffix is given, the value is assumed to be bytes.
    Returns None for invalid input.
    """
    try:
        if not size_str:
            return None
        s = size_str.strip().upper()
        unit = 1
        if s.endswith('K'):
            unit = 1024
            s = s[:-1]
        elif s.endswith('M'):
            unit = 1024 ** 2
            s = s[:-1]
        elif s.endswith('G'):
            unit = 1024 ** 3
            s = s[:-1]
        elif s.endswith('T'):
            unit = 1024 ** 4
            s = s[:-1]
        return int(float(s) * unit)
    except Exception:
        return None


def h_locate_files(args: List[str]):
    """Locate files whose names contain a substring.

    Recursively searches the state directory for files whose base name
    contains the given pattern (case-insensitive) and returns relative
    paths to those files. Usage: locate_files <pattern>
    """
    if not args or len(args) != 1:
        return _err("usage: locate_files <pattern>")
    pattern = args[0].lower()
    matches: List[str] = []
    for root, dirs, files in os.walk(STATE_DIR):
        for fname in files:
            if pattern in fname.lower():
                rel = os.path.relpath(os.path.join(root, fname), start=STATE_DIR)
                matches.append(rel)
    return "\n".join(matches) if matches else "(no matches)"


def h_locate_large_files(args: List[str]):
    """Locate files larger than a given size.

    Searches recursively under the state directory for files with size
    exceeding the specified threshold. The size may have optional unit
    suffixes K, M, G or T. Usage: locate_large_files <size>
    """
    if not args or len(args) != 1:
        return _err("usage: locate_large_files <size>")
    thresh = _parse_size(args[0])
    if thresh is None:
        return _err("locate_large_files: invalid size")
    matches: List[str] = []
    for root, dirs, files in os.walk(STATE_DIR):
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                sz = os.path.getsize(fpath)
                if sz > thresh:
                    rel = os.path.relpath(fpath, start=STATE_DIR)
                    matches.append(f"{rel} ({sz} bytes)")
            except Exception:
                pass
    return "\n".join(matches) if matches else "(no matches)"


def h_locate_large_dirs(args: List[str]):
    """Locate directories larger than a given cumulative size.

    Recursively examines directories under the state directory and
    returns those whose total file size exceeds the specified threshold.
    Usage: locate_large_dirs <size>
    """
    if not args or len(args) != 1:
        return _err("usage: locate_large_dirs <size>")
    thresh = _parse_size(args[0])
    if thresh is None:
        return _err("locate_large_dirs: invalid size")
    matches: List[str] = []
    for root, dirs, files in os.walk(STATE_DIR):
        total = 0
        for r, dnames, fnames in os.walk(root):
            for fname in fnames:
                try:
                    total += os.path.getsize(os.path.join(r, fname))
                except Exception:
                    pass
        if total > thresh:
            rel = os.path.relpath(root, start=STATE_DIR)
            matches.append(f"{rel} ({total} bytes)")
    return "\n".join(sorted(set(matches))) if matches else "(no matches)"


# ---------------------------------------------------------------------------
# Batch command handlers
#
# These handlers simulate the OpenVOS batch subsystem. Batch requests are stored
# as files under BATCH_ROOT. Each queue name corresponds to a subdirectory of
# BATCH_ROOT, and each batch request is stored as a JSON file containing
# information about the request.

def _derive_process_name(command_line: str) -> str:
    """Derive a default process name from a command line.

    The process name is taken from the first word of the command line. If the
    first word contains VOS path separators ('>'), the final component after
    the last '>' is used. Any file suffix after the last '.' is removed.
    Invalid characters (apostrophes) are removed. The name is truncated to
    32 characters. If the resulting name is empty, 'batch' is used.
    """
    if not command_line:
        return "batch"
    first_word = command_line.strip().split()[0]
    # Remove surrounding quotes
    first_word = first_word.strip("'\"")
    # If VOS path with '>' separators, take last component
    if '>' in first_word:
        first_word = first_word.split('>')[-1]
    # If path with os separators, take basename
    first_word = os.path.basename(first_word)
    # Remove extension
    if '.' in first_word:
        first_word = first_word.split('.')[0]
    # Remove apostrophes and other invalid chars
    name = ''.join(ch for ch in first_word if ch.isalnum() or ch in ('_', '-'))
    if not name:
        name = "batch"
    # Truncate to 32 characters
    return name[:32]


def _generate_unique_job_path(queue_name: str, process_name: str) -> str:
    """Generate a unique file path for a batch job.

    Jobs are stored under BATCH_ROOT/<queue_name>. The file name is based on
    the process_name. If a file with the same name exists, a numeric suffix
    is appended to make it unique. The file extension '.job' is used.
    """
    queue_dir = os.path.join(BATCH_ROOT, queue_name)
    os.makedirs(queue_dir, exist_ok=True)
    base = process_name
    filename = f"{base}.job"
    counter = 1
    while os.path.exists(os.path.join(queue_dir, filename)):
        filename = f"{base}_{counter}.job"
        counter += 1
    return os.path.join(queue_dir, filename)


def _parse_batch_arguments(args: List[str]) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    """Parse batch command arguments into a command line and options.

    Returns a tuple (command_line, options, error_message). If parsing fails,
    error_message is a non-empty string and command_line/options may be None.
    Recognised options include:
      -process_name <name>
      -output_path <path>
      -process_priority <n>
      -queue_priority <n>
      -privileged (flag)
      -no_restart (flag)
      -queue <queue_name>
      -module <module_name>
      -current_dir <path>
      -defer_until <datetime>
      -control <file_name>
      -after <process_name>
      -cpu_limit <hh:mm:ss>
      -notify (flag)
    Unknown options result in an error.
    """
    cmd_parts: List[str] = []
    opts: Dict[str, Any] = {}
    i = 0
    while i < len(args):
        token = args[i]
        if token.startswith('-'):
            # Boolean flags
            if token in ('-privileged', '-no_restart', '-notify'):
                key = token.lstrip('-').replace('-', '_')
                opts[key] = True
                i += 1
                continue
            # Options with values
            if token in ('-process_name', '-output_path', '-process_priority', '-queue_priority',
                         '-queue', '-module', '-current_dir', '-defer_until', '-control', '-after', '-cpu_limit'):
                if i + 1 >= len(args):
                    return None, {}, f"missing value for {token}"
                key = token.lstrip('-').replace('-', '_')
                value = args[i + 1]
                opts[key] = value
                i += 2
                continue
            return None, {}, f"unknown option {token}"
        else:
            cmd_parts.append(token)
            i += 1
    if not cmd_parts:
        return None, {}, "missing command line"
    command_line = " ".join(cmd_parts)
    return command_line, opts, None


def h_batch(args: List[str]):
    """Submit a command line as a batch request.

    Stores the batch request under the appropriate queue directory. Options
    are parsed according to the OpenVOS batch command syntax. Usage:
      batch <command_line> [options]
    """
    command_line, opts, err_msg = _parse_batch_arguments(args)
    if err_msg:
        return _err(f"batch: {err_msg}")
    if command_line is None:
        return _err("usage: batch <command_line> [options]")
    # Derive process_name
    process_name = opts.get('process_name') or _derive_process_name(command_line)
    # Determine queue name
    queue_name = opts.get('queue', 'normal')
    # Determine priorities
    try:
        process_priority = int(opts['process_priority']) if 'process_priority' in opts else None
    except Exception:
        return _err("batch: invalid process_priority")
    try:
        queue_priority = int(opts['queue_priority']) if 'queue_priority' in opts else 4
    except Exception:
        return _err("batch: invalid queue_priority")
    # Flags
    privileged = bool(opts.get('privileged'))
    restart = not bool(opts.get('no_restart'))
    notify = bool(opts.get('notify'))
    # Other options
    output_path = opts.get('output_path')
    module = opts.get('module')
    current_dir_opt = opts.get('current_dir')
    defer_until = opts.get('defer_until')
    control_file = opts.get('control')
    after = opts.get('after')
    cpu_limit = opts.get('cpu_limit')
    # Compose job info
    job_info: Dict[str, Any] = {
        'command_line': command_line,
        'process_name': process_name,
        'queue_name': queue_name,
        'process_priority': process_priority,
        'queue_priority': queue_priority,
        'privileged': privileged,
        'restart': restart,
        'notify': notify,
        'output_path': output_path,
        'module': module,
        'current_dir': current_dir_opt,
        'defer_until': defer_until,
        'control_file': control_file,
        'after': after,
        'cpu_limit': cpu_limit,
        'timestamp': int(time.time())
    }
    # Determine job file path
    job_path = _generate_unique_job_path(queue_name, process_name)
    try:
        # Ensure queue directory exists (handled in _generate_unique_job_path)
        with open(job_path, 'w', encoding='utf-8') as f:
            json.dump(job_info, f, indent=2)
        return _ok(f"queued batch request {process_name} in queue '{queue_name}'")
    except Exception as e:
        return _err(f"batch: failed to store job: {e}")


def h_display_batch_status(args: List[str]):
    """Display the status of batch requests.

    Shows all queued batch requests grouped by queue. If a queue name is
    provided, only that queue is displayed. Usage: display_batch_status [queue_name]
    """
    queue_filter: Optional[str] = args[0] if args else None
    rows: List[Tuple[str, str, int, int, str]] = []  # queue, process, qpri, ppri, status
    try:
        for queue_name in sorted(os.listdir(BATCH_ROOT)):
            if queue_filter and queue_name != queue_filter:
                continue
            queue_dir = os.path.join(BATCH_ROOT, queue_name)
            if not os.path.isdir(queue_dir):
                continue
            for fname in sorted(os.listdir(queue_dir)):
                if not fname.endswith('.job'):
                    continue
                job_file = os.path.join(queue_dir, fname)
                try:
                    with open(job_file, 'r', encoding='utf-8') as f:
                        job = json.load(f)
                    p_name = job.get('process_name', fname[:-4])
                    qpri = job.get('queue_priority', '')
                    ppri = job.get('process_priority', '')
                    status = 'pending'
                    rows.append((queue_name, p_name, qpri, ppri, status))
                except Exception:
                    rows.append((queue_name, fname[:-4], '', '', 'unreadable'))
        if not rows:
            return "(no batch requests)"
        return table(['Queue', 'Process', 'QueuePri', 'ProcPri', 'Status'], rows)
    except Exception as e:
        return _err(f"display_batch_status: {e}")


def h_list_batch_requests(args: List[str]):
    """List batch requests in the queues.

    Without arguments, lists all batch requests across all queues. With a queue
    name argument, lists only that queue. Usage: list_batch_requests [queue_name]
    """
    queue_filter: Optional[str] = args[0] if args else None
    rows: List[Tuple[str, str, int]] = []  # queue, process, queue_priority
    try:
        for queue_name in sorted(os.listdir(BATCH_ROOT)):
            if queue_filter and queue_name != queue_filter:
                continue
            queue_dir = os.path.join(BATCH_ROOT, queue_name)
            if not os.path.isdir(queue_dir):
                continue
            for fname in sorted(os.listdir(queue_dir)):
                if not fname.endswith('.job'):
                    continue
                job_file = os.path.join(queue_dir, fname)
                try:
                    with open(job_file, 'r', encoding='utf-8') as f:
                        job = json.load(f)
                    p_name = job.get('process_name', fname[:-4])
                    qpri = job.get('queue_priority', '')
                    rows.append((queue_name, p_name, qpri))
                except Exception:
                    rows.append((queue_name, fname[:-4], ''))
        if not rows:
            return "(no batch requests)"
        return table(['Queue', 'Process', 'QueuePri'], rows)
    except Exception as e:
        return _err(f"list_batch_requests: {e}")


def h_cancel_batch_requests(args: List[str]):
    """Cancel batch requests by process name or pattern.

    Accepts one or more process names or patterns. For each pattern, all
    matching jobs are removed from their queue directories. Usage:
      cancel_batch_requests <process_name or pattern> [...]
    """
    if not args:
        return _err("usage: cancel_batch_requests <process_name(s)>")
    patterns = args
    cancelled = 0
    try:
        for queue_name in os.listdir(BATCH_ROOT):
            queue_dir = os.path.join(BATCH_ROOT, queue_name)
            if not os.path.isdir(queue_dir):
                continue
            for fname in os.listdir(queue_dir):
                if not fname.endswith('.job'):
                    continue
                job_file = os.path.join(queue_dir, fname)
                try:
                    with open(job_file, 'r', encoding='utf-8') as f:
                        job = json.load(f)
                    p_name = job.get('process_name', fname[:-4])
                except Exception:
                    p_name = fname[:-4]
                # match patterns using fnmatch (case-insensitive)
                for pat in patterns:
                    if fnmatch.fnmatch(p_name.lower(), pat.lower()):
                        try:
                            os.remove(job_file)
                            cancelled += 1
                        except Exception:
                            pass
                        break
        return _ok(f"cancelled {cancelled} batch request(s)")
    except Exception as e:
        return _err(f"cancel_batch_requests: {e}")


def h_update_batch_requests(args: List[str]):
    """Update batch requests by name.

    Allows updating the queue_priority or process_priority of existing batch
    requests. Usage:
      update_batch_requests <process_name(s)> [-queue_priority <n>] [-process_priority <n>]
    At least one process name and one priority option must be supplied.
    """
    if not args:
        return _err("usage: update_batch_requests <process_name(s)> [options]")
    # Collect patterns until an option is encountered
    patterns: List[str] = []
    i = 0
    new_qpri: Optional[int] = None
    new_ppri: Optional[int] = None
    while i < len(args):
        if args[i].startswith('-'):
            opt = args[i]
            if opt == '-queue_priority':
                if i + 1 >= len(args):
                    return _err("update_batch_requests: missing value for -queue_priority")
                try:
                    new_qpri = int(args[i + 1])
                except Exception:
                    return _err("update_batch_requests: invalid queue_priority")
                i += 2
                continue
            if opt == '-process_priority':
                if i + 1 >= len(args):
                    return _err("update_batch_requests: missing value for -process_priority")
                try:
                    new_ppri = int(args[i + 1])
                except Exception:
                    return _err("update_batch_requests: invalid process_priority")
                i += 2
                continue
            return _err(f"update_batch_requests: unknown option {opt}")
        else:
            patterns.append(args[i])
            i += 1
    if not patterns or (new_qpri is None and new_ppri is None):
        return _err("update_batch_requests: specify process names and at least one priority option")
    updated = 0
    try:
        for queue_name in os.listdir(BATCH_ROOT):
            queue_dir = os.path.join(BATCH_ROOT, queue_name)
            if not os.path.isdir(queue_dir):
                continue
            for fname in os.listdir(queue_dir):
                if not fname.endswith('.job'):
                    continue
                job_file = os.path.join(queue_dir, fname)
                try:
                    with open(job_file, 'r', encoding='utf-8') as f:
                        job = json.load(f)
                    p_name = job.get('process_name', fname[:-4])
                except Exception:
                    continue
                match = False
                for pat in patterns:
                    if fnmatch.fnmatch(p_name.lower(), pat.lower()):
                        match = True
                        break
                if not match:
                    continue
                # Apply updates
                changed = False
                if new_qpri is not None and job.get('queue_priority') != new_qpri:
                    job['queue_priority'] = new_qpri
                    changed = True
                if new_ppri is not None and job.get('process_priority') != new_ppri:
                    job['process_priority'] = new_ppri
                    changed = True
                if changed:
                    try:
                        with open(job_file, 'w', encoding='utf-8') as f:
                            json.dump(job, f, indent=2)
                        updated += 1
                    except Exception:
                        pass
        return _ok(f"updated {updated} batch request(s)")
    except Exception as e:
        return _err(f"update_batch_requests: {e}")


# ---------------------------------------------------------------------------
# Command status reporting

def h_show_commands_status(args: List[str]):
    """Show which OpenVOS commands are implemented and which are simulated.

    This command reads the list of command names from ``vos_commands.txt`` and
    compares each to the internal registry of stub commands maintained by
    ``register_bulk``. Commands that have been stubbed are listed as
    "Not implemented" while the remainder are listed as "Implemented". If any
    arguments are supplied, an error is returned. Usage: show commands status
    """
    if args:
        return _err("usage: show commands status")
    # load the canonical list of commands from the text file
    names = load_txt_commands()
    # Instantiate a temporary shell to ensure the bulk registration has run
    # and STUB_COMMANDS is populated consistently. This keeps reporting
    # deterministic whether the function is called from the interactive
    # shell or directly from scripts.
    try:
        tmp_shell = VOShell()
    except Exception:
        # If shell instantiation fails for any reason, fall back to the
        # conservative approach: treat any name present in BUILTINS as
        # implemented; otherwise simulated.
        implemented = [n for n in names if n in BUILTINS]
        simulated = [n for n in names if n not in BUILTINS]
    else:
        implemented = [n for n in names if n not in STUB_COMMANDS]
        simulated = [n for n in names if n in STUB_COMMANDS]

    implemented.sort()
    simulated.sort()

    total = len(names)
    impl_count = len(implemented)
    sim_count = len(simulated)
    pct = (impl_count / total * 100) if total else 0.0

    # Build a richer, colourised report. Use colourama constants so the
    # interactive shell displays sections clearly. We still return a single
    # string so callers that print the result (or the shell wrapper) will
    # display the formatted output.
    lines: List[str] = []
    header = f"VOS commands report: {total} total, {impl_count} implemented, {sim_count} simulated ({pct:.1f}% implemented)"
    lines.append(f"{Fore.MAGENTA}{header}{Style.RESET_ALL}")
    lines.append("")
    lines.append(f"{Fore.CYAN}Implemented commands (name - short help):{Style.RESET_ALL}")
    if implemented:
        for name in implemented:
            help_text = HELP_TEXT.get(name, "(no help available)")
            short = help_text.splitlines()[0]
            if len(short) > 80:
                short = short[:77] + "..."
            lines.append(f"  {Fore.CYAN}{name}{Style.RESET_ALL} - {short}")
    else:
        lines.append(f"  {Fore.CYAN}(none){Style.RESET_ALL}")

    lines.append("")
    lines.append(f"{Fore.YELLOW}Simulated (not implemented) commands: (use 'help <name>' or implement handler){Style.RESET_ALL}")
    if simulated:
        per_row = 6
        for i in range(0, len(simulated), per_row):
            row = simulated[i:i+per_row]
            coloured = "  " + "  ".join(f"{Fore.LIGHTBLACK_EX}{r}{Style.RESET_ALL}" for r in row)
            lines.append(coloured)
    else:
        lines.append(f"  {Fore.LIGHTBLACK_EX}(none){Style.RESET_ALL}")

    return "\n".join(lines)


# Basic aliases (short -> full).  These provide shorthand for common commands
# but do not appear in the list of OpenVOS commands.  Note that 'ls' and 'll'
# map directly to the VOS command 'list' to avoid exposing the non-VOS 'dir'
ALIASES = {
    # simple aliases to existing commands
    "ls": "list",
    "ll": "list",
    # allow quit and q to map to the meta 'exit' command
    "quit": "exit",
    "q": "exit",
    # convenience alias: 'show commands report' -> 'show commands status'
    "show commands report": "show commands status",
}


# Built-in commands mapping (command -> output or callable)
BUILTINS: OrderedDict[str, Any] = OrderedDict({
    # misc utilities
    # directory and listing commands
    "list": h_list,

    # file operations (argument aware)
    "create_file": h_create_file,
    "copy_file": h_copy_file,
    "move_file": h_move_file,
    "compare_files": h_compare_files,
    "delete_file": h_delete_file,

    # directory operations (argument aware)
    "create_dir": h_create_dir,
    "copy_dir": h_copy_dir,
    "move_dir": h_move_dir,
    "delete_dir": h_delete_dir,
    "rename": h_rename,

    # extended file and directory commands
    "display_file": h_display_file,
    "display_file_status": h_display_file_status,
    "display_dir_status": h_display_dir_status,
    "display_disk_usage": h_display_disk_usage,
    "dump_file": h_dump_file,

    "list_users": h_list_users,
    "list_library_paths": h_list_library_paths,
    "add_library_path": h_add_library_path,
    "delete_library_path": h_delete_library_path,
    "set_language": h_set_language,
    "set_time_zone": h_set_time_zone,
    "set_line_wrap_width": h_set_line_wrap_width,
    "profile": h_profile,
    "add_profile": h_add_profile,
    "compare_dirs": h_compare_dirs,
    "clone_dir": h_clone_dir,
    "clone_file": h_clone_file,
    "display_current_dir": h_display_current_dir,
    "display_current_module": h_display_current_module,
    "display_date_time": h_display_date_time,
    "display_line": h_display_line,

    # system and device information
    "change_current_dir": h_change_current_dir,
    "display_device_info": h_display_device_info,
    "display_disk_info": h_display_disk_info,
    "display_system_usage": h_display_system_usage,
    "display_error": h_display_error,
    "display_notices": h_display_notices,
    "display_terminal_parameters": h_display_terminal_parameters,

    # locate commands
    "locate_files": h_locate_files,
    "locate_large_files": h_locate_large_files,
    "locate_large_dirs": h_locate_large_dirs,
    # batch commands
    "batch": h_batch,
    "display_batch_status": h_display_batch_status,
    "list_batch_requests": h_list_batch_requests,
    "cancel_batch_requests": h_cancel_batch_requests,
    "update_batch_requests": h_update_batch_requests,
    # command status
    "show commands status": h_show_commands_status,
    # alternative, more user-friendly name for the same report
    "show commands report": h_show_commands_status,
})


# Human-readable help strings
HELP_TEXT: Dict[str, str] = {
    # help for argument aware commands
    "create_file": "Create an empty file. Usage: create_file <path>",
    "copy_file": "Copy file. Usage: copy_file <src> <dst>",
    "move_file": "Move/rename file. Usage: move_file <src> <dst>",
    "compare_files": "Compare two files and report first difference. Usage: compare_files <a> <b>",
    "delete_file": "Delete a file. Usage: delete_file <path>",

    # directory operations help
    "create_dir": "Create a directory recursively within the state directory. Usage: create_dir <path>",
    "copy_dir": "Copy a directory recursively from source to destination within the state directory. Usage: copy_dir <src> <dst>",
    "move_dir": "Move or rename a directory within the state directory. Usage: move_dir <src> <dst>",
    "delete_dir": "Delete a directory and its contents within the state directory. Usage: delete_dir <path>",
    "rename": "Rename a file or directory within the state directory. Usage: rename <src> <dst>",

    # extended commands help
    "display_file": "Display the contents of a text file. Usage: display_file <path>",
    "display_file_status": "Show file metadata such as type, size and modification time. Usage: display_file_status <path>",
    "display_dir_status": "Show a summary of a directory's contents (counts and total size). Usage: display_dir_status [path]",
    "display_disk_usage": "Show total, used and free space for a directory. Usage: display_disk_usage [path]",
    "dump_file": "Dump a file in hex with offsets. Usage: dump_file <path>",
    "list_users": "List the names of users on the host system. Usage: list_users",
    "list_library_paths": "List the configured library paths. Usage: list_library_paths",
    "add_library_path": "Add a directory to the library path list. Usage: add_library_path <path>",
    "delete_library_path": "Remove a directory from the library path list. Usage: delete_library_path <path>",
    "set_language": "Set the current language. Usage: set_language <language_code>",
    "set_time_zone": "Set the current time zone. Usage: set_time_zone <time_zone>",
    "set_line_wrap_width": "Set line wrapping width for display commands. Usage: set_line_wrap_width <number>",
    "profile": "Show or set the current profile. Usage: profile [name]",
    "add_profile": "Add a new profile entry. Usage: add_profile <name>",
    "compare_dirs": "Compare two directories recursively. Usage: compare_dirs <dir1> <dir2>",
    "clone_dir": "Clone (copy) a directory recursively. Usage: clone_dir <src> <dst>",
    "clone_file": "Clone (copy) a file. Usage: clone_file <src> <dst>",
    "display_current_dir": "Display the current working directory in VOS format (e.g. >Sales>Jones). Usage: display_current_dir",
    "display_current_module": "Display the current module name. Usage: display_current_module",
    "display_date_time": "Display the current date and time. Usage: display_date_time",
    "display_line": "Display a line of text. Usage: display_line <text>",
    "change_current_dir": "Change the current working directory relative to STATE_DIR. The path may be VOS-style (e.g. >Sales>Jones) or relative. The directory will be created if it does not exist. Usage: change_current_dir <path>",
    "display_device_info": "Show basic host system information (OS, node, machine). Usage: display_device_info",
    "display_disk_info": "Show total, used and free space of the root filesystem. Usage: display_disk_info",
    "display_system_usage": "Show load averages and memory usage. Usage: display_system_usage",
    "display_error": "Show the last error message reported by any command. Usage: display_error",
    "display_notices": "Show and clear any stored system notices. Usage: display_notices",
    "display_terminal_parameters": "Show the terminal's columns and lines. Usage: display_terminal_parameters",
    "locate_files": "Search for files with names containing a substring. Usage: locate_files <pattern>",
    "locate_large_files": "List files larger than a specified size. Usage: locate_large_files <size>",
    "locate_large_dirs": "List directories whose contents exceed a specified size. Usage: locate_large_dirs <size>",
    # batch commands help
    "batch": (
        "Submit a command line as a batch request.\n"
        "Usage: batch <command_line> [options]. Supported options:\n"
        "  -process_name <name>         : set the process name\n"
        "  -output_path <path>          : redirect output to file\n"
        "  -process_priority <0-9>      : process priority (0 lowest, 9 highest)\n"
        "  -queue_priority <0-9>        : queue priority (0 lowest, 9 highest; default 4)\n"
        "  -privileged                  : run the batch process privileged\n"
        "  -no_restart                  : do not restart batch after a system restart\n"
        "  -queue <queue_name>          : specify queue name (default 'normal')\n"
        "  -module <module_name>        : specify target module\n"
        "  -current_dir <path>          : set current directory for the batch\n"
        "  -defer_until <datetime>      : defer start until date/time\n"
        "  -control <file_name>         : specify control (.batch) file\n"
        "  -after <process_name>        : run after specified process finishes\n"
        "  -cpu_limit <hh:mm:ss>        : limit CPU time of batch\n"
        "  -notify                      : notify when batch finishes"
    ),
    "display_batch_status": (
        "Display status of batch requests. Usage: display_batch_status [queue_name]. "
        "If a queue name is provided, only that queue is displayed."
    ),
    "list_batch_requests": (
        "List batch requests. Usage: list_batch_requests [queue_name]. "
        "Without a queue name, lists requests in all queues."
    ),
    "cancel_batch_requests": (
        "Cancel batch requests by name or pattern.\n"
        "Usage: cancel_batch_requests <process_name or pattern> [...]. "
        "Processes may be specified with wildcards."
    ),
    "update_batch_requests": (
        "Update existing batch requests.\n"
        "Usage: update_batch_requests <process_name(s)> [options]. Supported options:\n"
        "  -queue_priority <0-9>        : update queue priority\n"
        "  -process_priority <0-9>      : update process priority"
    ),
    # command status help
    "show commands status": "Show which commands are implemented versus simulated. Usage: show commands status",
    "show commands report": "Alias of 'show commands status' with richer reporting. Usage: show commands report",
}


# ---------- External packs & bulk registration ----------
def load_yaml_pack(path: str = "commands.yaml") -> OrderedDict:
    """Load an external YAML command pack if present."""
    if not yaml or not os.path.exists(path):
        return OrderedDict()
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    pack = OrderedDict()
    for k, v in data.items():
        pack[str(k)] = str(v)
    return pack


def load_txt_commands(path: str = "vos_commands.txt") -> List[str]:
    """Load additional command names from a plain text file."""
    cmds: List[str] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                cmds.append(ln)
    return cmds


def register_bulk(commands_dict: Dict[str, Any], names: List[str], suffix: str = " (simulated)") -> None:
    """Add stub commands to the mapping if not already defined.

    Instead of adding a plain string, we create a simple handler function for each
    unimplemented command. When invoked, the handler returns a message
    indicating that the command is simulated. Additionally, if the command
    does not already have a help entry, we add a generic help message
    pointing to the official VOS manual.
    """
    global HELP_TEXT  # allow modification of the help dictionary
    for name in names:
        key = name.strip()
        if key and key not in commands_dict:
            # define a stub function that captures the command name
            def _stub(args=None, _k=key, _suffix=suffix):
                """Return a simulated response for an unimplemented command."""
                return f"{_k}{_suffix}"

            # mark this stub as unimplemented and record its name
            _stub.__vos_stub__ = True
            commands_dict[key] = _stub
            # record the command name so we can report it later
            STUB_COMMANDS.add(key)
            # add a generic help message if none exists
            if key not in HELP_TEXT:
                HELP_TEXT[key] = (
                    "This command is simulated and not fully implemented. "
                    "Refer to the VOS Commands Reference Manual for details."
                )


# ---------- Shell ----------
class VOShell(Cmd):
    intro = c("VOS Emulator - type 'commands' to list, 'help <name>' for details.", Fore.MAGENTA)
    prompt = PROMPT

    def __init__(self):
        super().__init__()
        self.commands: OrderedDict[str, Any] = OrderedDict(BUILTINS)
        # external pack overrides/extends
        self.commands.update(load_yaml_pack())
        # bulk import from text list
        _txt = load_txt_commands()
        register_bulk(self.commands, _txt)
        # After loading built-ins and bulk commands, remove any commands
        # that are not present in the official VOS command list (vos_commands.txt).
        # Some internal commands (implemented in this emulator) are retained
        # even though they do not appear in the manual. These include file
        # operations and status introspection utilities.
        allowed_commands = set(_txt)
        # extend allowed_commands with emulator-specific commands that are
        # useful but not part of the VOS manual
        allowed_commands.update({
            # basic file and directory operations
            "list",
            "create_file", "copy_file", "move_file", "compare_files", "delete_file",
            "create_dir", "copy_dir", "move_dir", "delete_dir", "rename",
            # extended display operations
            "display_file", "display_file_status", "display_dir_status",
            "display_disk_usage", "dump_file",
            # library and user management commands
            "list_users", "list_library_paths", "add_library_path",
            "delete_library_path", "set_language", "set_time_zone",
            "set_line_wrap_width", "profile", "add_profile",
            # directory comparison and cloning
            "compare_dirs", "clone_dir", "clone_file",
            # display and utility commands
            "display_current_dir", "display_current_module", "display_date_time",
            "display_line", "change_current_dir", "display_device_info",
            "display_disk_info", "display_system_usage", "display_error",
            "display_notices", "display_terminal_parameters",
            # locate commands
            "locate_files", "locate_large_dirs", "locate_large_files",
            # batch commands
            "batch", "display_batch_status", "list_batch_requests", "cancel_batch_requests", "update_batch_requests",
            # command status
            "show commands status",
            "show commands report"
        })
        # Filter self.commands to include only allowed commands
        for k in list(self.commands.keys()):
            if k not in allowed_commands:
                # remove command
                del self.commands[k]
                # Remove from stub registry and help if present
                STUB_COMMANDS.discard(k)
                HELP_TEXT.pop(k, None)
        # Remove any commands that are identical to alias keys. Alias keys
        # are human-friendly mappings and should not appear in the internal
        # command registry; keeping them can cause ambiguous resolution when
        # the alias text also exists as a registered command name. Removing
        # them here prevents that ambiguity while still allowing the alias
        # to map to the target command.
        for a in list(ALIASES.keys()):
            if a in self.commands:
                del self.commands[a]
                STUB_COMMANDS.discard(a)
                HELP_TEXT.pop(a, None)
        # case-insensitive lookups & aliases
        self.aliases: Dict[str, str] = {k.lower(): v for k, v in ALIASES.items()}
        self.commands_ci: Dict[str, Tuple[str, Any]] = {k.lower(): (k, v) for k, v in self.commands.items()}
        self.index: List[str] = sorted(self.commands.keys())
        # persistent history if readline is available
        if readline:
            try:
                import atexit
                hist = os.path.expanduser("~/.vos_emulator_history")
                try:
                    readline.read_history_file(hist)
                except FileNotFoundError:
                    pass
                atexit.register(lambda: readline.write_history_file(hist))

                # install a custom completion display hook so that tab
                # completions show suggestions colour-coded. The hook
                # function will be defined below using a closure over
                # ``self`` and the global stub registry. The hook is
                # called by readline when multiple completions need to
                # be displayed. If readline is not available, this
                # installation will be skipped.
                def _display_matches_hook(substitution, matches, longest_match_length, self=self):
                    """Display completion matches with colour coding.

                    This hook is called by readline when more than one
                    completion candidate is available. It prints the
                    list of matches, colouring command names based on
                    whether they are implemented (cyan) or simulated
                    (dim grey). For path completions, no special
                    colouring is applied. The hook relies on the
                    instance attribute ``_last_completion_is_command``
                    (set by completenames and completedefault) to
                    determine which colouring scheme to use.
                    """
                    # Determine whether we are completing command names
                    completing_cmds = getattr(self, "_last_completion_is_command", False)
                    # When completing commands, use STUB_COMMANDS to pick colour
                    for m in matches:
                        if completing_cmds:
                            colour = Fore.LIGHTBLACK_EX if m in STUB_COMMANDS else Fore.CYAN
                            sys.stdout.write(f"{colour}{m}{Style.RESET_ALL}\n")
                        else:
                            # for path completions, just print the match
                            sys.stdout.write(f"{m}\n")
                    # after printing, re-issue prompt and current line
                    # to restore the command line state
                    sys.stdout.write(self.prompt + readline.get_line_buffer())
                    sys.stdout.flush()

                # register the hook
                readline.set_completion_display_matches_hook(_display_matches_hook)
            except Exception:
                pass
        # If readline is available, ensure the TAB key invokes completion.
        # Without these bindings, pressing TAB may insert a literal tab instead
        # of triggering the completer.  On systems that ship a libedit
        # read-line implementation (notably macOS), the binding syntax is
        # different.  We attempt both forms.  These calls have no effect on
        # platforms lacking readline support or where the bindings are
        # unavailable.  See: https://bugs.python.org/issue6755
        if readline:
            try:
                # Check if the readline implementation is libedit by
                # inspecting its docstring.  The libedit wrapper uses a
                # different syntax for key bindings.  If libedit is detected,
                # bind Ctrl-I (Tab) to rl_complete; otherwise bind Tab to
                # complete.
                doc = getattr(readline, '__doc__', '') or ''
                if 'libedit' in doc:
                    # libedit style: bind the tab (Ctrl-I) key to rl_complete
                    readline.parse_and_bind('bind \t rl_complete')
                else:
                    # GNU readline style: bind tab to complete
                    readline.parse_and_bind('tab: complete')
                # Optionally, show all matches immediately without a second Tab
                readline.parse_and_bind('set show-all-if-ambiguous on')
                # Ignore case for completions
                readline.parse_and_bind('set completion-ignore-case on')
            except Exception:
                # If any of the bindings fail (e.g. unsupported command), skip
                pass
        # By default, assume command name completion; this flag is used by
        # the readline display hook to determine colouring of matches. It
        # will be updated by ``completenames`` and ``completedefault``.
        self._last_completion_is_command = True

    # ---- helpers
    def _resolve(self, line: str):
        """Resolve a user input line into a command key, value and argument list.

        Handles alias expansion, argument parsing, prefix and glob matching, and
        fuzzy suggestions. Returns (key, handler/value, args) or (None, message, [])
        for ambiguous or unknown commands.
        """
        line = line.strip()
        if not line:
            return None, None, []
        # alias expansion on full line (case-insensitive)
        line_lc = line.lower()
        for a in sorted(self.aliases.keys(), key=len, reverse=True):
            if line_lc == a or line_lc.startswith(a + " "):
                mapped = self.aliases[a] + line[len(a):]
                line = mapped.strip()
                line_lc = line.lower()
                break
       # If the expanded full line exactly matches a registered command name,
        # return it immediately. This avoids ambiguous suggestions when an
        # alias text also appears in the command index.
        if line_lc in self.commands_ci:
            orig, val = self.commands_ci[line_lc]
            return orig, val, []

        # split command and args using current command dictionary
        head, args = _split_cmd_args(line, self.commands_ci)
        if not head:
            return None, None, []
        head_lc = head.lower()
        # exact hit
        if head_lc in self.commands_ci:
            orig, val = self.commands_ci[head_lc]
            return orig, val, args
        # prefix or glob search (case-insensitive) on head
        candidates = [k for k in self.commands if k.lower().startswith(head_lc) or fnmatch.fnmatch(k.lower(), head_lc)]
        if len(candidates) == 1:
            key = candidates[0]
            return key, self.commands[key], args
        if len(candidates) > 1:
            return None, "Ambiguous command. Did you mean:\n  - " + "\n  - ".join(candidates[:20]) + ("\n  ... (more)" if len(candidates) > 20 else ""), []
        # fuzzy suggestion across full command list
        sug = difflib.get_close_matches(head, self.index, n=5, cutoff=0.6)
        if sug:
            return None, "Unknown command. Closest matches:\n  - " + "\n  - ".join(sug), []
        return None, None, []

    def _execute(self, key: str, val, args: List[str]):
        """Execute a command handler or return its static value."""
        spinner(f"running '{key}'", 0.6)
        out = None
        if callable(val):
            try:
                # attempt to call with arguments if the handler expects them
                if val.__code__.co_argcount >= 1:
                    out = val(args)
                else:
                    out = val()
            except Exception:
                # fall back to zero-arg call
                try:
                    out = val()
                except Exception as e:
                    out = _err(str(e))
        else:
            out = val
        if out is None:
            return
        print(c(out, Fore.CYAN))

    # ---- core overrides
    def default(self, line: str):
        """Handle unknown or ambiguous commands with coloured suggestions.

        This override processes the resolver's messages to colour-code candidate
        commands: implemented commands appear in cyan, while stubbed ones
        appear in dim grey.  Other information (e.g. the header) is shown
        in yellow.  If the command is wholly unknown, a generic message is
        displayed.
        """
        line = line.strip()
        if not line:
            return
        key, val, args = self._resolve(line)
        if key:
            return self._execute(key, val, args)
        # If resolver returned a message about ambiguous or unknown commands,
        # parse and colour suggestions appropriately
        if isinstance(val, str) and (val.startswith("Ambiguous") or val.startswith("Unknown command.")):
            # Split into lines: first line is the header
            lines = val.splitlines()
            if lines:
                # Print header in yellow
                print(c(lines[0], Fore.YELLOW))
            for l in lines[1:]:
                stripped = l.lstrip()
                # Candidate suggestions begin with a hyphen after indentation
                if stripped.startswith("-"):
                    candidate = stripped[1:].strip()
                    colour = Fore.LIGHTBLACK_EX if candidate in STUB_COMMANDS else Fore.CYAN
                    print(f"  - {colour}{candidate}{Style.RESET_ALL}")
                elif stripped.startswith("..."):
                    # Truncation line
                    print(f"  {Fore.LIGHTBLACK_EX}{stripped}{Style.RESET_ALL}")
                else:
                    # Other informational lines
                    print(c(l, Fore.YELLOW))
            return
        # Generic unknown command case
        print(f"{Fore.RED}Unknown command: {line}{Style.RESET_ALL}")
        print(f"{Fore.YELLOW}Tip:{Style.RESET_ALL} use 'commands' or try a glob like 'show *status'.")

    # ---- built-in meta-commands
    def do_commands(self, arg):
        """List available commands, colour-coding based on implementation status.

        Commands with real handlers are shown in cyan; stub commands loaded
        via the bulk loader are shown in dim grey.  The list is formatted
        into columns based on the terminal width, similar to the default
        behaviour.
        """
        cols = _term_cols()
        items = sorted(self.commands.keys())
        # determine the widest command name
        colw = max(len(x) for x in items) + 2
        per_row = max(1, cols // colw)
        # header in magenta
        print(c(f"{len(items)} commands available:", Fore.MAGENTA))
        # print rows of commands
        for i in range(0, len(items), per_row):
            row_items: List[str] = []
            for name in items[i:i + per_row]:
                colour = Fore.LIGHTBLACK_EX if name in STUB_COMMANDS else Fore.CYAN
                padded = name.ljust(colw)
                row_items.append(f"{colour}{padded}{Style.RESET_ALL}")
            print("".join(row_items))

    def do_help(self, arg):
        arg = arg.strip()
        if not arg:
            print(c(
                "General help:\n  - 'commands' to list all\n  - 'help <name>' to see details\n  - globs/prefixes allowed (e.g., 'show sy*')\n  - aliases: " + ", ".join(
                    f"{k} -> {v}" for k, v in self.aliases.items()),
                Fore.YELLOW,
            ))
            return
        # try to resolve to a single key
        key, val, args = self._resolve(arg)
        if key:
            # look up detailed help; fall back to manual reference
            detail = HELP_TEXT.get(key) or HELP_TEXT.get(arg)
            if not detail:
                manual_url = (
                    "https://stratadoc.stratus.com/vos/19.3.0/r098-22/"
                    "wwhelp/wwhimpl/js/html/wwhelp.htm?context=r098-22&file=ch1r098-22.html"
                )
                detail = (
                    "No extra help available. For full details, please consult the VOS Commands "
                    f"Reference Manual at {manual_url}"
                )
            print(c(f"{key}\n  {detail}", Fore.CYAN))
            return
        print(c("No help found (or ambiguous). Try exact name from 'commands'.", Fore.RED))

    def complete_commands(self, text, line, begidx, endidx):
        return [k for k in self.index if k.startswith(text)]

    # quality-of-life
    def do_clear(self, arg):
        """Clear the screen."""
        print("\033c", end="")

    def do_exit(self, arg):
        """Exit emulator."""
        print(c("[SIM] Bye!", Fore.MAGENTA))
        return True

    def do_EOF(self, arg):
        print()
        return self.do_exit(arg)

    # tab completion for general commands
    def completenames(self, text, *ignored):
        # mark that we are completing command names so the display hook
        # can colour suggestions appropriately
        self._last_completion_is_command = True
        base = super().completenames(text, *ignored)
        extra = [k for k in self.index if k.startswith(text)]
        return sorted(set(base + extra))

    # ------------------------------------------------------------------
    # Tab completion helpers

    # Define sets of commands that operate on files or directories and
    # therefore should complete file system paths. When completing these
    # commands, the ``completedefault`` method will call ``_complete_path``
    # to suggest appropriate path completions.
    FILE_COMMANDS = {
        "create_file", "copy_file", "move_file", "delete_file", "compare_files",
        "create_dir", "copy_dir", "move_dir", "delete_dir", "rename",
        "display_file", "display_file_status", "display_dir_status",
        "dump_file", "list", "locate_files", "locate_large_dirs", "locate_large_files"
    }
    DIR_COMMANDS = {"change_current_dir"}

    def completedefault(self, text: str, line: str, begidx: int, endidx: int):
        """Provide custom completions for file and directory arguments.

        If the command in ``line`` is recognised as one of the file or
        directory commands, return path suggestions using the VOS-style
        separator ('>'). Otherwise, return an empty list so that the
        default completion behaviour applies.
        """
        # Split the line into tokens to identify the command name (may include alias)
        tokens = line.strip().split()
        if not tokens:
            return []
        # The command is the first token
        cmd_token = tokens[0]
        # Resolve the command token to a canonical command name
        resolved, _val, _args = self._resolve(cmd_token)
        if resolved in VOShell.FILE_COMMANDS or resolved in VOShell.DIR_COMMANDS:
            # mark that we are completing a file or directory path so the
            # display hook does not attempt to colour stub commands
            self._last_completion_is_command = False
            return self._complete_path(text)
        return []

    def _complete_path(self, text: str) -> List[str]:
        """Return a list of path completions for the given partial VOS path.

        The completions respect the current working directory (CURRENT_DIR)
        and the VOS path syntax using '>' separators. Both files and
        directories are suggested; directories are suffixed with a '>' to
        indicate that further completion is possible.  Case is not
        sensitive when matching.
        """
        import os
        # Determine the base directory and search pattern
        if not text:
            # No path typed yet: list the current working directory
            base_dir = os.path.join(STATE_DIR, CURRENT_DIR) if CURRENT_DIR else STATE_DIR
            prefix_str = ''
            pattern = ''
        else:
            # Determine the pattern (text after last '>') and prefix (text before pattern)
            # If the pattern is empty (path ends with '>'), prefix_str is the whole text
            parts = text.split('>')
            pattern = parts[-1]
            prefix_str = text[:-len(pattern)] if pattern else text
            # Determine parent directory for completions based on VOS semantics
            prefix_parts = parts[:-1]
            if text.startswith('>'):
                # Absolute VOS path: ignore CURRENT_DIR; remove empty leading segment
                parent_parts = [p for p in prefix_parts if p]
                base_dir = os.path.join(STATE_DIR, *parent_parts)
            else:
                # Relative VOS path: prepend CURRENT_DIR segments
                base_parts = CURRENT_DIR.split(os.sep) if CURRENT_DIR else []
                base_dir = os.path.join(STATE_DIR, *(base_parts + prefix_parts))
        suggestions: List[str] = []
        try:
            entries = sorted(os.listdir(base_dir))
        except Exception:
            return suggestions
        for entry in entries:
            # Case-insensitive match against the pattern
            if pattern and not entry.lower().startswith(pattern.lower()):
                continue
            full_path = os.path.join(base_dir, entry)
            if os.path.isdir(full_path):
                suggestions.append(prefix_str + entry + '>')
            else:
                suggestions.append(prefix_str + entry)
        return suggestions


# ---------- main ----------
def main():
    VOShell().cmdloop()


if __name__ == "__main__":
    main()