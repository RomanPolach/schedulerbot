from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Any

from langchain.tools import tool

from tools.shared_shell import python_runner_candidates


def create_run_python_code_tool() -> Any:
    @tool
    def run_python_code(code: str, timeout_seconds: int = 30) -> str:
        """Run Python code from a temporary script file.

        Required args:
        - code: Python source code to execute.

        Optional args:
        - timeout_seconds: hard timeout in seconds (1..180).

        Returns:
        - interpreter used, exit code, stdout, and stderr (or timeout message with partial output).

        Examples:
        - run_python_code(code="print('hello')")
        - run_python_code(code="import sys; print(sys.version)", timeout_seconds=20)
        """
        snippet = (code or "").strip()
        if not snippet:
            return "No Python code provided."

        timeout_seconds = max(1, min(timeout_seconds, 180))
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        temp_path = ""
        interpreter_label = ""
        process = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".py",
                prefix="agent_py_",
                delete=False,
                encoding="utf-8",
                newline="\n",
            ) as temp_file:
                temp_file.write(snippet)
                temp_path = temp_file.name

            last_not_found = ""
            for candidate in python_runner_candidates():
                try:
                    process = subprocess.Popen(
                        [*candidate, temp_path],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        creationflags=creationflags,
                    )
                    interpreter_label = " ".join(candidate)
                    break
                except FileNotFoundError as exc:
                    last_not_found = str(exc)
                    continue

            if process is None:
                return f"No Python interpreter found. Last error: {last_not_found or 'not found'}"

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
                    f"Python timed out after {timeout_seconds} seconds.\n"
                    f"interpreter={interpreter_label or '(unknown)'}\n"
                    f"partial_stdout:\n{stdout[:4000].strip() or '(empty)'}\n\n"
                    f"partial_stderr:\n{stderr[:2000].strip() or '(empty)'}"
                )

            output = (
                f"interpreter={interpreter_label or '(unknown)'}\n"
                f"exit_code={process.returncode}\n"
                f"stdout:\n{stdout.strip() or '(empty)'}\n\n"
                f"stderr:\n{stderr.strip() or '(empty)'}"
            )
            return output[:12000]
        except Exception as exc:
            return f"Python execution error: {exc}"
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    return run_python_code
