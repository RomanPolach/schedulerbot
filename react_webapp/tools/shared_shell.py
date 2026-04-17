from __future__ import annotations

import os
import re
from typing import List


def strip_wrapping_quotes(value: str) -> str:
    v = (value or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in {"'", '"'}:
        inner = v[1:-1]
        if v[0] == '"':
            return inner.replace('\\"', '"')
        return inner.replace("\\'", "'")
    return v


def strip_redundant_cmd_wrappers(command: str) -> str:
    c = (command or "").strip()
    # Unwrap nested cmd wrappers like:
    # cmd /c "...", cmd /v:on /c "...", cmd.exe /d /s /c "..."
    pattern = re.compile(r"^(?:cmd(?:\.exe)?)(?:\s+/[^\s]+)*\s+/c\s+(.+)$", flags=re.IGNORECASE)
    while True:
        match = pattern.match(c)
        if not match:
            break
        c = strip_wrapping_quotes(match.group(1).strip())
    return c


def parse_python_c_command(command: str) -> tuple[List[str], str] | None:
    c = strip_redundant_cmd_wrappers(command)
    c = strip_wrapping_quotes(c)
    # Supports: py -c ..., py -3.12 -c ..., python -c ...
    match = re.match(r"^(py(?:thon)?(?:\s+-[0-9.]+)?)\s+-c\s+(.+)$", c, flags=re.IGNORECASE)
    if not match:
        return None
    exe_args = re.split(r"\s+", match.group(1).strip())
    code = strip_wrapping_quotes(match.group(2).strip())
    return exe_args, code


def python_runner_candidates() -> List[List[str]]:
    configured = (os.getenv("PYTHON_EXECUTABLE", "") or "").strip()
    if configured:
        return [re.split(r"\s+", configured)]
    if os.name == "nt":
        return [["py", "-3.12"], ["py"], ["python"]]
    return [["python3"], ["python"]]

