"""
Terminal colour helpers for ise_sync output.

Uses raw ANSI codes (no external deps).  Colours are ON by default when
stdout is a terminal.  Set NO_COLOR=1 to force them off, or FORCE_COLOR=1
to force them on (e.g. in CI logs that render ANSI).
"""
from __future__ import annotations
import os
import sys

_ENABLED = (
    "NO_COLOR" not in os.environ or os.environ["NO_COLOR"] == ""
) and (bool(os.environ.get("FORCE_COLOR")) or sys.stdout.isatty())


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _ENABLED else text


def green(t: str)   -> str: return _c("32", t)
def yellow(t: str)  -> str: return _c("33", t)
def red(t: str)     -> str: return _c("31", t)
def cyan(t: str)    -> str: return _c("36", t)
def bold(t: str)    -> str: return _c("1",  t)
def dim(t: str)     -> str: return _c("2",  t)
def magenta(t: str) -> str: return _c("35", t)
