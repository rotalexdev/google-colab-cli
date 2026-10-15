# PyInstaller runtime hook: force UTF-8 on stdio.
#
# Windows consoles default to a legacy ANSI code page (charmap/cp1252).
# `typer.echo` raises UnicodeEncodeError when the CLI prints bundled docs
# containing non-ASCII characters -- skills/colab-operator/SKILL.md contains
# U+2192 (->). Verified on CI: "UnicodeEncodeError: 'charmap' codec can't
# encode character '\u2192'" while `colab readme` (pure ASCII) passed.
#
# Setting PYTHONUTF8=1 in the workflow env does NOT fix this -- the frozen
# interpreter still resolved stdout to charmap. And even if it did, it would
# only paper over the problem for CI: an end user running the shipped exe from
# cmd.exe without that variable would still crash. Embedding the fix is the
# only version that makes the artifact itself correct.
#
# runs before the application entry point, so sys.stdout still refers to the
# real stream and typer/click resolve it lazily at echo time.
#
# errors="replace" is deliberate: on a console that genuinely cannot render a
# codepoint, substituting is strictly better than dying with a traceback.

import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        # Stream may not be reconfigurable (already-detached, replaced, or a
        # stub). Never let the fix itself break startup.
        pass