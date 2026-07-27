from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from utils.sag_knowledge_engine import SagKnowledgeEngine, build_engine_config


class _Result:
    source_id = "source-1"
    source_config_id = "config-1"
    chunk_count = 1
    chunk_ids = ["chunk-1"]


class _ExtractResult:
    event_count = 1


class _FakeEngine:
    def __init__(self) -> None:
        self.ingested_paths: list[Path] = []
        self.extract_calls = 0

    async def ingest(self, path: Path) -> _Result:
        assert Path(path).is_file()
        self.ingested_paths.append(Path(path))
        return _Result()

    async def extract(self) -> _ExtractResult:
        self.extract_calls += 1
        return _ExtractResult()


def test_build_engine_config_honors_explicit_model_overrides(monkeypatch):
    monkeypatch.setenv("SAG_LLM_API_KEY", "test-key")
    monkeypatch.setenv("SAG_LLM_MODEL", "environment-model")
    config = build_engine_config(
        llm_model="gemini-3.5-flash-lite",
        embedding_model="text-embedding-004",
    )
    assert config.llm.model == "gemini-3.5-flash-lite"
    assert config.embedding.model == "text-embedding-004"


def test_build_engine_config_requires_a_key(monkeypatch):
    for name in ("SAG_LLM_API_KEY", "LLM_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="Missing SAG LLM API key"):
        build_engine_config()


def test_ingest_batch_extracts_each_page(tmp_path: Path):
    engine = SagKnowledgeEngine(data_dir=tmp_path / "index", auto_extract=False)
    engine._data_dir.mkdir(parents=True)
    fake = _FakeEngine()
    engine._engine = fake
    engine._started = True

    results = asyncio.run(
        engine.ingest_batch(
            [
                {"content": "# One\n\nBotox and filler special.", "source_url": "https://a.example"},
                {"content": "# Two\n\nBotox and filler special.", "source_url": "https://b.example"},
            ]
        )
    )

    assert len(results) == 2
    assert fake.extract_calls == 2
    assert len(fake.ingested_paths) == 2
    assert all(path.suffix == ".md" for path in fake.ingested_paths)


@pytest.mark.skipif(
    os.getenv("RUN_SAG_LIVE") != "1",
    reason="requires configured Gemini/OpenAI-compatible SAG credentials",
)
def test_live_sag_ingests_extracts_and_searches_two_documents(tmp_path: Path):
    first = tmp_path / "one.md"
    second = tmp_path / "two.md"
    first.write_text("# Alpha\n\nAlpha offers Botox specials.", encoding="utf-8")
    second.write_text("# Beta\n\nBeta offers filler specials.", encoding="utf-8")

    async def run() -> None:
        async with SagKnowledgeEngine(
            data_dir=tmp_path / "index",
            llm_model="gemini-3.5-flash-lite",
            embedding_model="text-embedding-004",
        ) as engine:
            first_result = await engine.ingest_markdown_file(first, extract=True)
            second_result = await engine.ingest_markdown_file(second, extract=True)
            assert first_result["source_id"]
            assert second_result["source_id"]
            assert first_result["extract"]["event_count"] >= 0
            assert second_result["extract"]["event_count"] >= 0
            sections = await engine.search("Botox and filler specials", strategy="multi", top_k=5)
            assert all(section.get("source_id") for section in sections)

    asyncio.run(run())
