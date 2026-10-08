import json
from pathlib import Path

import pytest

from scout.codex import CodexAgent, ResearchError
from scout.models import Discovery


@pytest.mark.parametrize("with_search", [True, False])
async def test_codex_transient_errors_and_no_search_result(settings, now, monkeypatch, with_search):
    captured = {}
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-private-token")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "123")
    monkeypatch.setattr("scout.codex.shutil.which", lambda value: "C:/fake/codex.exe")

    class Writer:
        def write(self, data):
            captured["prompt"] = data

        async def drain(self):
            pass

        def close(self):
            pass

    class Events:
        def __aiter__(self):
            return self.iterate()

        async def iterate(self):
            events = [{"type": "error", "message": "Reconnecting..."}]
            if with_search:
                events.append(
                    {
                        "type": "item.completed",
                        "item": {"type": "web_search", "results": [{"url": "https://finmarket.ru/news/1"}]},
                    }
                )
            events.append({"type": "turn.completed"})
            for event in events:
                yield (json.dumps(event) + "\n").encode()

    class Errors:
        async def read(self):
            return b"transient error recovered"

    class Process:
        stdin, stdout, stderr = Writer(), Events(), Errors()
        returncode = None

        async def wait(self):
            self.returncode = 0
            return 0

    async def create(*args, **kwargs):
        captured["env"] = kwargs["env"]
        Path(args[args.index("--output-last-message") + 1]).write_text(
            Discovery(as_of=now, market_context="", ideas=[], search_notes=[]).model_dump_json(),
            encoding="utf-8",
        )
        return Process()

    monkeypatch.setattr("scout.codex.asyncio.create_subprocess_exec", create)
    agent = CodexAgent(settings)
    if with_search:
        result = await agent.run("Проверяемый запрос", Discovery, "тест")
        assert not result.ideas
        assert agent.search_urls == ["https://finmarket.ru/news/1"]
    else:
        with pytest.raises(ResearchError, match="не выполнил веб-поиск"):
            await agent.run("Проверяемый запрос", Discovery, "тест")
    assert "TELEGRAM_BOT_TOKEN" not in captured["env"]
    assert "TELEGRAM_OWNER_ID" not in captured["env"]
