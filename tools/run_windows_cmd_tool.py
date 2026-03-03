from __future__ import annotations

import os
import re
import subprocess
from typing import Any

from langchain.tools import tool

from tools.shared_shell import parse_python_c_command


def create_run_windows_cmd_tool() -> Any:
    @tool
    def run_windows_cmd(command: str, timeout_seconds: int = 25) -> str:
        """Run a Windows CMD command (cmd /c) and return stdout/stderr."""
        timeout_seconds = max(1, min(timeout_seconds, 120))
        sanitized_command = re.sub(r"\|\s*(more|less)(\s+.*)?$", "", command, flags=re.IGNORECASE).strip()
        command_to_run = sanitized_command or command
        try:
            py_c = parse_python_c_command(command_to_run)
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            if py_c:
                exe_args, code = py_c
                process = subprocess.Popen(
                    [*exe_args, "-c", code],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags,
                )
            else:
                process = subprocess.Popen(
                    command_to_run,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=True,
                    executable=os.getenv("COMSPEC", "cmd.exe"),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags,
                )
            try:
                stdout, stderr = process.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                except Exception:
                    process.kill()
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except Exception:
                    stdout, stderr = "", ""

                return (
                    f"Command timed out after {timeout_seconds} seconds.\n"
                    f"partial_stdout:\n{stdout[:4000].strip() or '(empty)'}\n\n"
                    f"partial_stderr:\n{stderr[:2000].strip() or '(empty)'}"
                )

            output = (
                f"exit_code={process.returncode}\n"
                f"stdout:\n{stdout.strip() or '(empty)'}\n\n"
                f"stderr:\n{stderr.strip() or '(empty)'}"
            )
            return output[:12000]
        except Exception as exc:
            return f"Command error: {exc}"

    return run_windows_cmd

