"""MCP tools: shell commands and file management inside the container."""

import asyncio
import fnmatch
import json
import os
import re
import shutil
import signal
import stat
import time
from pathlib import Path

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}
SKIP_ROOTS = {"/proc", "/sys", "/dev"}


class _Capture:
    """Reads a process stream, keeping at most `limit` bytes but counting all of them."""

    def __init__(self, limit: int):
        self.limit = limit
        self.buf = bytearray()
        self.total = 0

    async def drain(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(65536):
            self.total += len(chunk)
            room = self.limit - len(self.buf)
            if room > 0:
                self.buf += chunk[:room]

    def text(self) -> str:
        s = self.buf.decode(errors="replace")
        if self.total > len(self.buf):
            s += f"\n... [обрезано, ещё {self.total - len(self.buf)} байт]"
        return s


def register_tools(
    mcp: FastMCP,
    *,
    workdir: Path,
    audit_log: Path,
    default_timeout: int,
    max_timeout: int,
    max_output: int,
    child_env: dict[str, str],
) -> None:
    def resolve(path: str) -> Path:
        p = Path(os.path.expanduser(path))
        return p if p.is_absolute() else workdir / p

    def audit(tool: str, **args) -> None:
        token = get_access_token()
        entry = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "client": token.client_id if token else None,
            "tool": tool,
            **{k: (v[:2000] if isinstance(v, str) else v) for k, v in args.items()},
        }
        audit_log.parent.mkdir(parents=True, exist_ok=True)
        with audit_log.open("a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @mcp.tool(annotations=ToolAnnotations(title="Run shell command", destructiveHint=True, openWorldHint=True))
    async def run_command(command: str, cwd: str | None = None, timeout: int = default_timeout) -> str:
        """Run a bash command on the server and return its exit code, stdout and stderr.

        Runs as root inside the container. `cwd` defaults to the workspace directory.
        `timeout` is in seconds. For long-running/background processes, detach them and
        redirect output, e.g. `nohup python app.py > app.log 2>&1 &`, then read the log.
        """
        audit("run_command", command=command, cwd=cwd)
        cwd_path = resolve(cwd) if cwd else workdir
        if not cwd_path.is_dir():
            return f"Ошибка: каталог {cwd_path} не существует"
        timeout = max(1, min(timeout, max_timeout))

        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-lc",
            command,
            cwd=cwd_path,
            env=child_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        out, err = _Capture(max_output), _Capture(max_output)
        readers = [asyncio.create_task(out.drain(proc.stdout)), asyncio.create_task(err.drain(proc.stderr))]
        status = ""
        try:
            await asyncio.wait_for(proc.wait(), timeout)
            status = f"exit code: {proc.returncode}"
        except TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            status = f"превышен таймаут {timeout} с, процесс убит"
        _, still_open = await asyncio.wait(readers, timeout=3)
        for task in still_open:
            task.cancel()
        if still_open:
            status += " (фоновый процесс держит вывод открытым — перенаправьте его вывод в файл)"

        parts = [status]
        if out.total:
            parts.append("--- stdout ---\n" + out.text())
        if err.total:
            parts.append("--- stderr ---\n" + err.text())
        return "\n".join(parts)

    @mcp.tool(annotations=ToolAnnotations(title="Read file", readOnlyHint=True))
    def read_file(path: str, offset: int = 1, limit: int = 2000) -> str:
        """Read a text file. Returns lines prefixed with line numbers.

        `offset` is the first line (1-based), `limit` the number of lines to return.
        Relative paths are resolved against the workspace directory.
        """
        audit("read_file", path=path, offset=offset, limit=limit)
        p = resolve(path)
        if not p.is_file():
            return f"Ошибка: файл {p} не найден"
        with p.open("rb") as f:
            if b"\0" in f.read(8192):
                return f"{p} — бинарный файл ({p.stat().st_size} байт). Используйте run_command (xxd, base64, file)."
        lines, size, total = [], 0, 0
        with p.open(encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f, 1):
                total = n
                if n < offset or len(lines) >= limit or size >= max_output:
                    continue
                line = line.rstrip("\n")
                if len(line) > 2000:
                    line = line[:2000] + "… [строка обрезана]"
                lines.append(f"{n:6}\t{line}")
                size += len(line)
        header = f"{p} — строки {offset}-{offset + len(lines) - 1} из {total}" if lines else f"{p} — {total} строк"
        return header + "\n" + "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(title="Write file", destructiveHint=True))
    def write_file(path: str, content: str, append: bool = False) -> str:
        """Create or overwrite a text file (or append to it). Parent directories are created."""
        audit("write_file", path=path, bytes=len(content.encode()), append=append)
        p = resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a" if append else "w", encoding="utf-8") as f:
            f.write(content)
        return f"{'Дописано' if append else 'Записано'} {len(content.encode())} байт в {p}"

    @mcp.tool(annotations=ToolAnnotations(title="Edit file", destructiveHint=True))
    def edit_file(path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
        """Replace an exact text fragment in a file.

        `old_text` must match exactly (including whitespace) and be unique unless
        `replace_all` is true.
        """
        audit("edit_file", path=path, old_text=old_text, new_text=new_text, replace_all=replace_all)
        p = resolve(path)
        if not p.is_file():
            return f"Ошибка: файл {p} не найден"
        text = p.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count == 0:
            return "Ошибка: old_text не найден в файле"
        if count > 1 and not replace_all:
            return f"Ошибка: old_text встречается {count} раз — уточните фрагмент или передайте replace_all=true"
        p.write_text(text.replace(old_text, new_text), encoding="utf-8")
        return f"Заменено вхождений: {count if replace_all else 1} в {p}"

    @mcp.tool(annotations=ToolAnnotations(title="List directory", readOnlyHint=True))
    def list_directory(path: str = ".", show_hidden: bool = False) -> str:
        """List a directory: type, size, modification time and name of each entry."""
        audit("list_directory", path=path)
        p = resolve(path)
        if not p.is_dir():
            return f"Ошибка: каталог {p} не найден"
        rows = []
        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        for e in entries[:1000]:
            if not show_hidden and e.name.startswith("."):
                continue
            try:
                st = e.lstat()
            except OSError:
                continue
            kind = "l" if stat.S_ISLNK(st.st_mode) else "d" if stat.S_ISDIR(st.st_mode) else "-"
            name = e.name + ("/" if kind == "d" else f" -> {os.readlink(e)}" if kind == "l" else "")
            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
            rows.append(f"{kind} {stat.filemode(st.st_mode)[1:]} {st.st_size:>12} {mtime}  {name}")
        more = f"\n... и ещё {len(entries) - 1000}" if len(entries) > 1000 else ""
        return f"{p}:\n" + ("\n".join(rows) or "(пусто)") + more

    @mcp.tool(annotations=ToolAnnotations(title="Search files", readOnlyHint=True))
    def search_files(
        pattern: str = "*", path: str = ".", contains: str | None = None, max_results: int = 200
    ) -> str:
        """Find files by name glob (e.g. `*.py`), optionally only those whose content matches
        the regex `contains`; then matching lines are returned as `file:line: text`.
        Skips .git, node_modules, __pycache__ and virtualenvs.
        """
        audit("search_files", pattern=pattern, path=path, contains=contains)
        root = resolve(path)
        regex = re.compile(contains) if contains else None
        results: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames if d not in SKIP_DIRS and os.path.join(dirpath, d) not in SKIP_ROOTS
            ]
            for name in filenames:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, root)
                if not (fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern)):
                    continue
                if regex is None:
                    results.append(full)
                elif os.path.isfile(full) and os.path.getsize(full) < 5_000_000:
                    try:
                        with open(full, encoding="utf-8") as f:
                            for n, line in enumerate(f, 1):
                                if regex.search(line):
                                    results.append(f"{full}:{n}: {line.rstrip()[:300]}")
                                    if len(results) >= max_results:
                                        break
                    except (UnicodeDecodeError, OSError):
                        pass
                if len(results) >= max_results:
                    return "\n".join(results) + f"\n... [показаны первые {max_results}]"
        return "\n".join(results) or "Ничего не найдено"

    @mcp.tool(annotations=ToolAnnotations(title="Move or rename", destructiveHint=True))
    def move_path(source: str, destination: str) -> str:
        """Move or rename a file or directory."""
        audit("move_path", source=source, destination=destination)
        src, dst = resolve(source), resolve(destination)
        if not src.exists():
            return f"Ошибка: {src} не существует"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(src, dst)
        return f"Перемещено: {src} -> {dst}"

    @mcp.tool(annotations=ToolAnnotations(title="Delete", destructiveHint=True))
    def delete_path(path: str, recursive: bool = False) -> str:
        """Delete a file, or a directory (non-empty directories need `recursive=true`)."""
        audit("delete_path", path=path, recursive=recursive)
        p = resolve(path)
        if len(p.resolve().parts) <= 2:
            return f"Отказано: {p} — каталог верхнего уровня, его удаление через этот инструмент запрещено"
        if p.is_symlink() or p.is_file():
            p.unlink()
        elif p.is_dir():
            if recursive:
                shutil.rmtree(p)
            else:
                p.rmdir()
        else:
            return f"Ошибка: {p} не существует"
        return f"Удалено: {p}"
