"""
Terminal colour helpers for ise_sync output.

Uses raw ANSI codes (no external deps).  Colours are ON by default.
Set the NO_COLOR=1 environment variable to disable them (e.g. when
redirecting output to a log file or piping into another tool).
"""
from __future__ import annotations
import os

_ENABLED = os.environ.get("NO_COLOR", "") == ""


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _ENABLED else text


def green(t: str)   -> str: return _c("32", t)
def yellow(t: str)  -> str: return _c("33", t)
def red(t: str)     -> str: return _c("31", t)
def cyan(t: str)    -> str: return _c("36", t)
def bold(t: str)    -> str: return _c("1",  t)
def dim(t: str)     -> str: return _c("2",  t)
def magenta(t: str) -> str: return _c("35", t)
