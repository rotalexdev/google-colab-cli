#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Post-build smoke test for the frozen google-colab-cli binary.

A green PyInstaller build is NOT evidence of a working binary. This script
executes the artifact and asserts on real stdout/stderr.

This is a Typer CLI, not a long-running server, so every check is one
sequential ``subprocess.run`` with a timeout. No reader thread, no queue.

Checks performed:
  1. ``--help``  -- the Typer group loaded and registered its subcommands.
  2. ``version`` -- the command table is wired up and the entry point works.
  3. ``readme``  -- ``colab_cli/README.md`` was bundled as package data.
  4. ``skill``   -- ``colab_cli/SKILL.md`` was bundled as package data.

Checks 3 and 4 are the whole point. ``colab_cli/commands/utility.py``
resolves those files via ``importlib.resources.files("colab_cli").joinpath(
filename)`` -- a *dynamic* filename PyInstaller cannot detect statically -- so
only ``--collect-data colab_cli`` puts them in the bundle. Drop that flag and
the binary still builds, still starts, and fails only at ``colab readme`` with
a single stderr line. That is exactly the class of bug a build-only pipeline
ships, so it is asserted on content here, not on exit code.

Exit code 0 only if every check passes.

Usage:
    python .github/smoke_test.py dist/colab-cli
"""

import json
import os
import platform
import subprocess
import sys
import tempfile
import time

# Generous: a PyInstaller onefile cold start unpacks the bundle and then
# imports jupyter_kernel_client / prompt_toolkit / rich, which is slow.
TIMEOUT_SECONDS = 90

# Substring of the real README.md that must appear on the stdout of
# `colab readme`. Kept short on purpose: it exists to prove the file was
# *bundled*, not to pin README wording. Any sensible README edit keeps it.
README_MARKER = "Activity-Based Liveness"

# Same reasoning, from skills/colab-operator/SKILL.md.
SKILL_MARKER = "Skill: Colab Session Operator"

# Visible (non-hidden) subcommands. `whoami`, `auth`, `README` and `SKILL` are
# registered hidden and are deliberately absent from this list.
EXPECTED_COMMANDS = [
    "console",
    "download",
    "drivemount",
    "edit",
    "exec",
    "help",
    "install",
    "log",
    "ls",
    "new",
    "pay",
    "readme",
    "repl",
    "restart-kernel",
    "rm",
    "run",
    "sessions",
    "skill",
    "ssh",
    "status",
    "stop",
    "update",
    "upload",
    "url",
    "usage",
    "version",
]

# Emitted by _print_resource() in commands/utility.py when a package data file
# is missing from the bundle.
MISSING_DATA_MARKER = "content not available"

MISSING_DATA_HINT = (
    "hint: a packaged data file is missing from the bundle. It is loaded via "
    'importlib.resources.files("colab_cli").joinpath(<dynamic filename>), '
    "which PyInstaller cannot detect statically. Two things are required: "
    "(1) `uv sync --frozen --no-editable`, because README.md and SKILL.md do "
    "not exist under src/colab_cli/ -- hatchling force-includes them into the "
    "wheel; (2) the --collect-data colab_cli flag on the pyinstaller call."
)


class CheckFailure(Exception):
    """A check failed. Carries enough detail for the diagnostic block."""

    def __init__(self, reason, hints=()):
        super().__init__(reason)
        self.reason = reason
        self.hints = list(hints)


# --------------------------------------------------------------------------
# Update-check kill switch
# --------------------------------------------------------------------------


def _settings_path():
    return os.path.join(os.path.expanduser("~"), ".config", "colab-cli",
                        "settings.json")


def seed_update_check_kill_switch():
    """Disable the PyPI update probe before the binary is ever invoked.

    ``colab_cli/cli.py`` runs ``auto_update.run_background_check()`` on every
    invocation whose subcommand is not in its small suppression set (which does
    not cover the commands exercised here). That check does an HTTPS GET to
    pypi.org with a 5s timeout and writes to ``~/.config/colab-cli``. The
    master switch is ``enable_update_check`` in ``~/.config/colab-cli/
    settings.json``, read by ``SettingsStore.load()``.

    Seeding it unconditionally makes the network path structurally impossible
    and keeps the test hermetic. A failure to seed degrades to a loud warning
    rather than failing the test: on a runner with a broken home directory the
    update check is the least interesting reason this script could fail.

    Returns ``(settings_path, previous_contents, existed_before)``.
    """
    settings_path = _settings_path()
    settings_dir = os.path.dirname(settings_path)
    backup = None
    had_previous = False
    try:
        os.makedirs(settings_dir, exist_ok=True)
        if os.path.exists(settings_path):
            with open(settings_path, "r", encoding="utf-8") as handle:
                backup = handle.read()
            had_previous = True

        payload = json.dumps(
            {"enable_update_check": False, "last_check": None}, indent=2
        )

        # Write to a sibling temp file and os.replace() it into position, so a
        # concurrent reader never observes a half-written file. The handle is
        # closed in the finally block BEFORE the replace and before any
        # cleanup: on Windows an open handle makes removal raise
        # PermissionError (WinError 32), which masks the real error.
        fd, tmp_path = tempfile.mkstemp(
            dir=settings_dir, prefix="settings.json.", suffix=".tmp"
        )
        handle = os.fdopen(fd, "w", encoding="utf-8")
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(tmp_path, settings_path)

        print("[setup] seeded %s with enable_update_check=false" % settings_path)
        return settings_path, backup, had_previous
    except Exception as exc:  # noqa: BLE001 - deliberate broad guard
        print("")
        print("!" * 72)
        print("!! WARNING: could not seed the update-check kill switch.")
        print("!! The binary may reach out to pypi.org during these checks.")
        print("!! path  : %s" % settings_path)
        print("!! error : %r" % (exc,))
        print("!" * 72)
        print("")
        return None, None, False


def restore_update_check_kill_switch(settings_path, backup, had_previous):
    """Undo the seeding so a local run does not clobber real user settings."""
    if settings_path is None:
        return
    try:
        if had_previous:
            with open(settings_path, "w", encoding="utf-8") as handle:
                handle.write(backup)
        else:
            os.remove(settings_path)
        print("[setup] restored %s" % settings_path)
    except OSError as exc:
        print("[setup] WARNING: could not restore %s: %r" % (settings_path, exc))


# --------------------------------------------------------------------------
# Invocation
# --------------------------------------------------------------------------


def run_cli(binary, args):
    """Run the binary once. Returns ``(returncode, stdout, stderr)``."""
    env = dict(os.environ)
    # Deterministic output: no ANSI escapes, no terminal control bytes, and a
    # width wide enough that no command name in the --help panel wraps.
    env["TERM"] = "dumb"
    env["COLUMNS"] = "120"
    env["NO_COLOR"] = "1"
    completed = subprocess.run(
        [binary] + list(args),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=TIMEOUT_SECONDS,
        env=env,
        universal_newlines=True,
        errors="replace",
    )
    return completed.returncode, completed.stdout, completed.stderr


# rich frames its help panels with characters from the Unicode Box Drawing
# block (U+2500..U+257F), e.g. U+256D "╭", U+2500 "─", U+2502 "│", U+2570 "╰".
# Testing the range is deliberately used instead of enumerating specific
# codepoints: an earlier version of this file listed square corners (U+250C)
# instead of the rounded ones rich actually emits, and the parser silently
# found no Commands panel.
_BOX_FIRST = 0x2500
_BOX_LAST = 0x257F
_BOX_RULE = "\u2500\u2501\u2550\u2504\u2508"


def _unwrap_box_row(raw_line):
    """Strip rich's panel scaffolding, leaving the row's text content."""
    return "".join(
        " " if _BOX_FIRST <= ord(ch) <= _BOX_LAST else ch for ch in raw_line
    ).strip()


def extract_command_names(help_text):
    """Best-effort extraction of subcommand names from the ``--help`` panel.

    Typer renders help through rich, so subcommands live in a bordered panel
    with one per row, name first::

        ╭─ Commands ─────────────────────────╮
        │ console  Connect to a raw TTY ...   │
        ╰─────────────────────────────────────╯

    Returns an empty list when the layout is not recognised, in which case the
    caller falls back to substring matching -- a rich format change must
    degrade the check, never silently pass it.
    """
    names = []
    in_commands = False
    for raw_line in help_text.splitlines():
        line = _unwrap_box_row(raw_line)
        # Drop the panel rule that rich draws around the title row too, so the
        # title reads as "Commands", not "─ Commands ─".
        body = line.lstrip(_BOX_RULE + " ").strip()
        if not in_commands:
            in_commands = body.startswith("Commands")
            continue
        if not body:
            break  # bottom border: a row made only of rules
        token = body.split()[0]
        if token.startswith("-") or not token[0].islower():
            break  # next panel header, e.g. "Options"
        names.append(token)
    return names


# --------------------------------------------------------------------------
# Assertions
# --------------------------------------------------------------------------


def assert_help(code, out, binary):
    if code != 0:
        raise CheckFailure("--help exited %d, expected 0" % code)

    prog_name = os.path.basename(binary)
    accepted = [prog_name]
    if prog_name.lower().endswith(".exe"):
        # Click strips the .exe suffix when detecting the program name on
        # Windows; accept either form instead of guessing.
        accepted.append(prog_name[:-4])
    if not any(name in out for name in accepted):
        raise CheckFailure(
            "--help stdout does not contain the program name %r" % (prog_name,)
        )

    detected = extract_command_names(out)
    if detected:
        print("[check] --help detected commands: %s" % " ".join(sorted(detected)))
        missing = [n for n in EXPECTED_COMMANDS if n not in detected]
        if missing:
            raise CheckFailure(
                "--help command panel is missing expected subcommands: %s"
                % ", ".join(missing)
            )
    else:
        print("[check] --help command panel not parsed; falling back to "
              "substring match")
        missing = [n for n in EXPECTED_COMMANDS if n not in out]
        if missing:
            raise CheckFailure(
                "--help output is missing expected subcommands: %s"
                % ", ".join(missing)
            )


def assert_version(code, out, _err):
    if code != 0:
        raise CheckFailure("version exited %d, expected 0" % code)
    # In a frozen binary the version may degrade to "unknown" or a git short
    # hash, because importlib.metadata degrades gracefully. Assert on the
    # stable prefix only -- never on a semver.
    if "Version:" not in out:
        raise CheckFailure(
            "version stdout lacks the stable prefix 'Version:'; got %r"
            % out.strip()[:200]
        )
    print("[check] version output: %s" % out.strip())


def assert_bundled_resource(subcommand, code, out, err, marker, source):
    hints = []
    if MISSING_DATA_MARKER in err:
        hints.append(MISSING_DATA_HINT)
    if code != 0:
        raise CheckFailure(
            "%s exited %d, expected 0" % (subcommand, code), hints=hints
        )
    if not out.strip():
        hints.append("stdout was empty.")
        raise CheckFailure(
            "%s produced no stdout" % subcommand, hints=hints
        )
    if marker not in out:
        hints.append(
            "expected marker %r from %s was not found in stdout."
            % (marker, source)
        )
        raise CheckFailure(
            "%s stdout does not contain the expected content marker" % subcommand,
            hints=hints,
        )
    print("[check] %s printed %d bytes containing %r"
          % (subcommand, len(out), marker))


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def build_checks(binary):
    return [
        ("--help", ["--help"], lambda c, o, e: assert_help(c, o, binary)),
        ("version", ["version"], assert_version),
        ("readme", ["readme"],
         lambda c, o, e: assert_bundled_resource(
             "readme", c, o, e, README_MARKER, "README.md")),
        ("skill", ["skill"],
         lambda c, o, e: assert_bundled_resource(
             "skill", c, o, e, SKILL_MARKER, "skills/colab-operator/SKILL.md")),
    ]


def report_failure(name, command, failure, code, out, err):
    # stdout is block-buffered when piped while stderr is not; flush first so
    # the diagnostic block does not interleave out of order in CI logs.
    sys.stdout.flush()
    print("")
    print("=" * 72)
    print("SMOKE TEST FAILED: %s" % name)
    print("=" * 72)
    print("command   : %s" % command)
    print("exit code : %s" % code)
    print("reason    : %s" % failure.reason)
    for hint in failure.hints:
        print("           %s" % hint)
    print("-" * 72)
    print("stdout (%d bytes):" % len(out or ""))
    print((out or "<empty>")[:4000])
    print("-" * 72)
    print("stderr (%d bytes):" % len(err or ""))
    print((err or "<empty>")[:4000])
    print("=" * 72)


def main():
    # stdout is block-buffered when piped while stderr is unbuffered, which
    # interleaves the failure diagnostic out of order in CI logs. Line-buffer
    # stdout so progress, failures and the diagnostic block stay in sequence.
    try:
        sys.stdout.reconfigure(line_buffering=True)  # Python 3.7+
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    if len(sys.argv) != 2:
        print("usage: %s <path-to-binary>" % sys.argv[0], file=sys.stderr)
        return 2

    binary = sys.argv[1]
    print("=" * 72)
    print("colab-cli binary smoke test")
    print("binary    : %s" % os.path.abspath(binary))
    print("platform  : %s %s" % (platform.system(), platform.machine()))
    print("python    : %s" % sys.version.split()[0])
    print("=" * 72)

    if not os.path.isfile(binary):
        print(
            "FATAL: no binary at %s -- the PyInstaller step produced no "
            "artifact, so there is nothing to test." % binary,
            file=sys.stderr,
        )
        return 1
    if not os.access(binary, os.X_OK):
        print("FATAL: %s exists but is not executable." % binary,
              file=sys.stderr)
        return 1

    settings_path, backup, had_previous = seed_update_check_kill_switch()
    try:
        for name, args, assertions in build_checks(binary):
            started = time.time()
            try:
                code, out, err = run_cli(binary, args)
            except subprocess.TimeoutExpired:
                print("[FAIL] %s timed out after %ds"
                      % (name, TIMEOUT_SECONDS), file=sys.stderr, flush=True)
                return 1
            except OSError as exc:
                print("[FAIL] %s could not be executed: %r" % (name, exc),
                      file=sys.stderr, flush=True)
                return 1

            try:
                assertions(code, out, err)
            except CheckFailure as failure:
                print("[FAIL] %s (%.1fs) -- %s"
                      % (name, time.time() - started, failure.reason),
                      file=sys.stderr, flush=True)
                report_failure(name, "%s %s" % (binary, " ".join(args)),
                               failure, code, out, err)
                return 1
            print("[PASS] %s (%.1fs)" % (name, time.time() - started))
        return 0
    finally:
        restore_update_check_kill_switch(settings_path, backup, had_previous)


if __name__ == "__main__":
    sys.exit(main())