"""Curate through a local coding-agent CLI, using the subscription it is signed in with.

Each request runs the agent once, non-interactively, in an empty temporary
directory with no tools that can change anything: Claude Code with no tools,
MCP servers, or settings; Codex in its read-only sandbox. The answer is
untrusted text that the curator parses and validates like any model reply.
"""

from __future__ import annotations

from collections.abc import Sequence
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Literal, get_args

Agent = Literal["claude", "codex", "command"]
AGENTS: tuple[str, ...] = get_args(Agent)
# Where agent CLIs and their runtimes (such as node) are commonly installed. Timers
# start with a minimal PATH, and Node-based agents fail without one that finds node.
_EXTRA_PATHS = ("~/.local/bin", "~/.npm-global/bin", "~/.bun/bin", "~/.volta/bin", "~/.cargo/bin",
                "/usr/local/bin", "/usr/bin", "/bin")


def _search_path(*first: str) -> str:
    parts = [*first, *os.environ.get("PATH", "").split(os.pathsep), *(str(Path(p).expanduser()) for p in _EXTRA_PATHS)]
    return os.pathsep.join(dict.fromkeys(part for part in parts if part))


class AgentError(OSError):
    """The agent could not be started, failed, or returned no answer."""


def find_executable(name: str) -> str:
    if os.sep in name:
        return str(Path(name).expanduser())
    found = shutil.which(name, path=_search_path())
    if not found:
        raise AgentError(f"Agent command not found: {name}")
    return found


def _run(argv: Sequence[str], stdin: str, cwd: Path, timeout: float) -> str:
    # The agent's own directory comes first, so a script finds the runtime installed beside it.
    environment = {**os.environ, "PATH": _search_path(str(Path(argv[0]).parent))}
    try:
        result = subprocess.run(list(argv), input=stdin, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                                env=environment)
    except subprocess.TimeoutExpired as exc:
        raise AgentError(f"{Path(argv[0]).name} did not answer within {timeout:.0f} seconds") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        raise AgentError(f"{Path(argv[0]).name} failed: " + " ".join(detail))
    return result.stdout


def _claude(prompt: str, image: bytes | None, model: str, work: Path, timeout: float) -> str:
    content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
    if image is not None:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(image).decode("ascii")}})
    message = json.dumps({"type": "user", "message": {"role": "user", "content": content}})
    argv = [find_executable("claude"), "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--tools", "", "--strict-mcp-config", "--setting-sources", "",
            "--no-session-persistence", *(["--model", model] if model else [])]
    for line in _run(argv, message + "\n", work, timeout).splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            if event.get("is_error") or not isinstance(event.get("result"), str):
                raise AgentError(f"claude returned an error: {str(event.get('result'))[:300]}")
            return str(event["result"])
    raise AgentError("claude returned no result")


def _codex(prompt: str, image: bytes | None, model: str, work: Path, timeout: float) -> str:
    answer = work / "answer.txt"
    argv = [find_executable("codex"), "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only",
            "--color", "never", "-C", str(work), "-o", str(answer), *(["--model", model] if model else [])]
    if image is not None:
        sheet = work / "contact-sheet.jpg"
        sheet.write_bytes(image)
        argv += ["--image", str(sheet)]
    _run([*argv, "-"], prompt, work, timeout)
    if not answer.is_file() or not answer.read_text(encoding="utf-8").strip():
        raise AgentError("codex returned no answer")
    return answer.read_text(encoding="utf-8")


def _command(prompt: str, image: bytes | None, command: Sequence[str], work: Path, timeout: float) -> str:
    """Any CLI: the prompt arrives on stdin, {image} names the contact sheet, and stdout is the answer."""
    if not command:
        raise AgentError("Set [llm].agent_command to use a custom agent")
    sheet = work / "contact-sheet.jpg"
    sheet.write_bytes(image or b"")
    argv = [part.replace("{image}", str(sheet)).replace("{workdir}", str(work)) for part in command]
    return _run([find_executable(argv[0]), *argv[1:]], prompt, work, timeout)


def ask_agent(agent: str, prompt: str, image: bytes | None, *, model: str = "",
              command: Sequence[str] = (), timeout: float = 600) -> str:
    """Run one curation request through an agent CLI and return its final text answer."""
    with tempfile.TemporaryDirectory(prefix="artwall-agent-") as directory:
        work = Path(directory)
        if agent == "claude":
            return _claude(prompt, image, model, work, timeout)
        if agent == "codex":
            return _codex(prompt, image, model, work, timeout)
        if agent == "command":
            return _command(prompt, image, command, work, timeout)
    raise AgentError(f"Unknown agent: {agent} (choose from {', '.join(AGENTS)})")
