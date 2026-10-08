import asyncio
import json
import os
import shutil
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from scout.config import Settings

T = TypeVar("T", bound=BaseModel)


def strict_schema(model: type[BaseModel]) -> dict:
    schema = model.model_json_schema()

    def visit(node):
        if isinstance(node, dict):
            node.pop("default", None)
            # Pydantic emits URI formats that Codex Structured Outputs does not accept.
            # URL/date validation still runs locally after the model returns JSON.
            node.pop("format", None)
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(schema)
    return schema


class ResearchError(RuntimeError):
    pass


class CodexAgent:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.search_urls: list[str] = []

    def command(self, schema: Path, output: Path, workdir: Path) -> list[str]:
        binary = shutil.which(self.settings.codex_bin)
        if not binary:
            raise ResearchError("Codex CLI не найден. Установите CLI и выполните codex login.")
        if binary.lower().endswith((".cmd", ".bat", ".ps1")):
            raise ResearchError("CODEX_BIN должен указывать на исполняемый codex, а не shell-скрипт.")
        args = [
            binary,
            "-a",
            "never",
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "-c",
            'web_search="live"',
            "--json",
            "--color",
            "never",
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(output),
            "-C",
            str(workdir),
        ]
        for feature in ("shell_tool", "apps", "remote_plugin", "hooks", "multi_agent"):
            args.extend(["--disable", feature])
        if self.settings.codex_model:
            args.extend(["--model", self.settings.codex_model])
        return [*args, "-"]

    async def run(self, prompt: str, model: type[T], label: str, require_search: bool = True) -> T:
        root = self.settings.data_dir / "runs" / datetime.now(self.settings.tz).strftime("%Y%m%d-%H%M%S-%f")
        root.mkdir(parents=True, exist_ok=False)
        schema = root / "schema.json"
        output = root / "answer.json"
        schema.write_text(json.dumps(strict_schema(model), ensure_ascii=False), encoding="utf-8")
        (root / "prompt.txt").write_text(prompt, encoding="utf-8")
        # Telegram credentials are never inherited by the research subprocess.
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("TELEGRAM_")}
        kwargs = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        proc = await asyncio.create_subprocess_exec(
            *self.command(schema, output, root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=root,
            limit=4 * 1024 * 1024,
            **kwargs,
        )
        searches = 0
        failed = False
        self.search_urls = []

        async def consume():
            nonlocal searches, failed
            total = 0
            with (root / "events.jsonl").open("wb") as events:
                async for line in proc.stdout:
                    total += len(line)
                    if total > 25 * 1024 * 1024:
                        raise ResearchError("Превышен лимит журнала исследования")
                    events.write(line)
                    events.flush()
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if event.get("type") == "turn.failed":
                        failed = True
                    item = event.get("item", {})
                    if event.get("type") == "item.completed" and item.get("type") == "web_search":
                        searches += 1
                        for result in item.get("results", []):
                            url = result.get("url")
                            if isinstance(url, str) and url not in self.search_urls:
                                self.search_urls.append(url)

        async def communicate():
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            _, stderr = await asyncio.gather(consume(), proc.stderr.read())
            (root / "stderr.log").write_bytes(stderr[-1_000_000:])
            await proc.wait()

        try:
            await asyncio.wait_for(communicate(), timeout=self.settings.codex_timeout_seconds)
        except BaseException:
            if proc.returncode is None:
                if os.name == "nt":
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill",
                        "/PID",
                        str(proc.pid),
                        "/T",
                        "/F",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await killer.wait()
                else:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                await proc.wait()
            raise
        if proc.returncode != 0 or failed or not output.exists():
            raise ResearchError(f"Codex: этап {label} завершился с ошибкой. Журнал: {root}")
        if require_search and searches == 0:
            raise ResearchError("Codex не выполнил веб-поиск; результат без поиска отклонён")
        try:
            return model.model_validate_json(output.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ResearchError(f"Codex вернул некорректную структуру на этапе {label}") from exc
