"""
SAG Knowledge Engine wrapper for CostFinder.

Encapsulates zleap-sag DataEngine configuration, lifecycle, and common operations
(ingest page content, extract event-entity index, search knowledge base).

Usage:
    from utils.sag_knowledge_engine import SagKnowledgeEngine

    engine = SagKnowledgeEngine()
    await engine.start()
    await engine.ingest_page_content(content, source_url=url, domain=domain, name=name)
    await engine.extract()
    results = await engine.search("Botox price Tucson", strategy="multi", top_k=5)
    await engine.close()

Or as async context manager:
    async with SagKnowledgeEngine() as engine:
        await engine.ingest_page_content(content, source_url=url, domain=domain)
        ...
"""
from __future__ import annotations

import os
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from utils.logger import log

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Default data directory for SAG local storage (SQLite + LanceDB)
SAG_DATA_DIR = PROJECT_ROOT / ".sag_data"


def _resolve_llm_base_url() -> str:
    """Resolve OpenAI-compatible base URL from environment.

    SAG expects a base URL like https://host/v1 (without /chat/completions).
    The project's LLM_API_URL may include the full path; we strip it.
    """
    raw = os.getenv("SAG_LLM_BASE_URL", "").strip()
    if raw:
        return raw.rstrip("/")

    # Fallback: derive from existing LLM_API_URL
    api_url = os.getenv("LLM_API_URL", "").strip()
    if api_url:
        # Strip trailing endpoint paths to get the base
        for suffix in ("/chat/completions", "/completions", "/embeddings"):
            if api_url.endswith(suffix):
                api_url = api_url[: -len(suffix)]
                break
        return api_url.rstrip("/")

    # Final fallback: Gemini OpenAI-compatible endpoint
    return "https://generativelanguage.googleapis.com/v1beta/openai"


def _resolve_llm_api_key() -> str:
    """Resolve LLM API key from environment."""
    return (
        os.getenv("SAG_LLM_API_KEY", "").strip()
        or os.getenv("LLM_API_KEY", "").strip()
        or os.getenv("GEMINI_API_KEY", "").strip()
        or os.getenv("OPENAI_API_KEY", "").strip()
    )


def _resolve_llm_model() -> str:
    """Resolve LLM model name from environment."""
    return (
        os.getenv("SAG_LLM_MODEL", "").strip()
        or os.getenv("LLM_MODEL", "").strip()
        or "gemini-2.0-flash"
    )


def _resolve_embedding_model() -> str:
    """Resolve embedding model name from environment."""
    return os.getenv("SAG_EMBEDDING_MODEL", "").strip() or "text-embedding-004"


def build_engine_config(
    *,
    llm_model: Optional[str] = None,
    embedding_model: Optional[str] = None,
):
    """Build a zleap-sag EngineConfig from environment variables.

    Environment variables (with fallbacks):
        SAG_LLM_BASE_URL / LLM_API_URL  -> OpenAI-compatible base URL
        SAG_LLM_API_KEY / LLM_API_KEY / GEMINI_API_KEY -> API key
        SAG_LLM_MODEL / LLM_MODEL       -> LLM model name
        SAG_EMBEDDING_MODEL             -> Embedding model name
        SAG_LANGUAGE                    -> Language (default: en)
    """
    from zleap.sag import EngineConfig
    from zleap.sag.config import EmbeddingConfig, LLMConfig

    llm_api_key = _resolve_llm_api_key()
    llm_base_url = _resolve_llm_base_url()
    if not llm_api_key:
        raise RuntimeError(
            "Missing SAG LLM API key. Set SAG_LLM_API_KEY, LLM_API_KEY, "
            "GEMINI_API_KEY, or OPENAI_API_KEY."
        )
    if not llm_base_url:
        raise RuntimeError("Missing SAG LLM base URL.")

    llm_config = LLMConfig(
        api_key=llm_api_key,
        base_url=llm_base_url,
        model=llm_model or _resolve_llm_model(),
    )

    embedding_config = EmbeddingConfig(
        model=embedding_model or _resolve_embedding_model(),
        # When api_key/base_url are omitted, embedding reuses the LLM endpoint.
    )

    language = os.getenv("SAG_LANGUAGE", "en").strip() or "en"

    return EngineConfig(
        llm=llm_config,
        embedding=embedding_config,
        language=language,
    )


class SagKnowledgeEngine:
    """High-level wrapper around zleap-sag DataEngine for CostFinder knowledge indexing.

    Manages engine lifecycle and provides domain-specific methods for ingesting
    crawled page content and searching the accumulated knowledge base.
    """

    def __init__(
        self,
        *,
        data_dir: Optional[Path] = None,
        auto_extract: bool = True,
        llm_model: Optional[str] = None,
        embedding_model: Optional[str] = None,
    ):
        """Initialize the SAG knowledge engine.

        Args:
            data_dir: Directory for SAG local storage. Defaults to PROJECT_ROOT/.sag_data
            auto_extract: If True, automatically run event-entity extraction after ingest.
        """
        self._data_dir = data_dir or SAG_DATA_DIR
        self._auto_extract = auto_extract
        self._llm_model = llm_model
        self._embedding_model = embedding_model
        self._engine = None
        self._started = False
        self._ingest_count = 0
        self._extract_pending = False

    async def start(self) -> None:
        """Initialize the DataEngine and create local storage schema."""
        if self._started:
            return

        load_dotenv(PROJECT_ROOT / ".env")

        from zleap.sag import DataEngine

        config = build_engine_config(
            llm_model=self._llm_model,
            embedding_model=self._embedding_model,
        )
        self._data_dir.mkdir(parents=True, exist_ok=True)

        # Change to data dir so SAG creates .zleap/ there
        original_cwd = os.getcwd()
        os.chdir(self._data_dir)
        try:
            self._engine = DataEngine(config)
            await self._engine.start()
        finally:
            os.chdir(original_cwd)

        self._started = True
        log.info(
            "SAG knowledge engine started: data_dir={dir}, llm={model}, embedding={emb}".format(
                dir=self._data_dir,
                model=self._llm_model or _resolve_llm_model(),
                emb=self._embedding_model or _resolve_embedding_model(),
            )
        )

    async def close(self) -> None:
        """Close the engine and release resources."""
        if self._engine and self._started:
            await self._engine.aclose()
            self._started = False
            self._engine = None
            log.info(
                "SAG knowledge engine closed: total_ingested={n}".format(n=self._ingest_count)
            )

    async def __aenter__(self) -> "SagKnowledgeEngine":
        await self.start()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def ingest_count(self) -> int:
        return self._ingest_count

    async def ingest_markdown_file(
        self,
        path: Path,
        *,
        extract: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Ingest one existing Markdown file through SAG's path-only SDK API.

        The installed zleap-sag ``DataEngine.ingest`` validates that its argument
        is a real file. Keeping that boundary explicit prevents raw page text from
        being mistaken for a path and makes the returned source/chunk IDs available
        for provenance mapping.
        """
        if not self._started or not self._engine:
            raise RuntimeError("SAG engine not started. Call start() first.")

        file_path = Path(path).resolve()
        if not file_path.is_file():
            raise ValueError("SAG ingest requires an existing Markdown file: {path}".format(path=file_path))

        original_cwd = os.getcwd()
        os.chdir(self._data_dir)
        try:
            result = await self._engine.ingest(file_path)
        finally:
            os.chdir(original_cwd)

        self._ingest_count += 1
        self._extract_pending = True
        chunk_ids = list(getattr(result, "chunk_ids", []) or [])
        ingest_result = {
            "status": "ok",
            "path": str(file_path),
            "source_id": getattr(result, "source_id", ""),
            "source_config_id": getattr(result, "source_config_id", ""),
            "chunk_count": getattr(result, "chunk_count", 0),
            "chunk_ids": chunk_ids,
        }

        # DataEngine keeps only the latest ingest's chunk IDs. Extracting here is
        # deliberate: deferring a batch extraction would otherwise extract only the
        # final page's chunks.
        should_extract = extract if extract is not None else self._auto_extract
        if should_extract:
            ingest_result["extract"] = await self.extract()
        return ingest_result

    async def ingest_page_content(
        self,
        content: str,
        *,
        source_url: str = "",
        domain: str = "",
        name: str = "",
        extract: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Ingest a crawled page's content into the SAG knowledge base.

        Args:
            content: The page text/markdown content to ingest.
            source_url: Original URL of the page (for tracing).
            domain: Domain name of the source site.
            name: Business/site name.
            extract: Override auto_extract setting for this call.

        Returns:
            Dict with ingest statistics (chunk_count, etc.)
        """
        if not self._started or not self._engine:
            raise RuntimeError("SAG engine not started. Call start() first.")

        if not content or not content.strip():
            return {"status": "skipped", "reason": "empty_content", "chunk_count": 0}

        # Prepend metadata header for better entity extraction.
        header_parts = []
        if name:
            header_parts.append(f"Business: {name}")
        if domain:
            header_parts.append(f"Domain: {domain}")
        if source_url:
            header_parts.append(f"Source: {source_url}")

        if header_parts:
            enriched_content = "\n".join(header_parts) + "\n\n" + content
        else:
            enriched_content = content

        materialized_dir = self._data_dir / "documents"
        materialized_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(enriched_content.encode("utf-8")).hexdigest()
        materialized_path = materialized_dir / "sag_page_{digest}.md".format(digest=digest)
        materialized_path.write_text(enriched_content, encoding="utf-8")
        result = await self.ingest_markdown_file(materialized_path, extract=extract)

        chunk_count = result.get("chunk_count", 0)
        log.debug(
            "SAG ingest: domain={domain}, url={url}, chunks={chunks}".format(
                domain=domain, url=source_url, chunks=chunk_count
            )
        )

        return {
            **result,
            "source_url": source_url,
            "domain": domain,
        }

    async def ingest_batch(
        self,
        pages: List[Dict[str, Any]],
        *,
        extract_after_all: bool = True,
    ) -> List[Dict[str, Any]]:
        """Ingest multiple pages, optionally extracting once at the end.

        Args:
            pages: List of dicts with keys: content, source_url, domain, name
            extract_after_all: Retained for caller compatibility. The installed
                SDK only extracts the most recently ingested chunk IDs, so every
                page is extracted immediately regardless of this value.

        Returns:
            List of per-page ingest results.
        """
        results = []
        for page in pages:
            result = await self.ingest_page_content(
                page.get("content", ""),
                source_url=page.get("source_url", ""),
                domain=page.get("domain", ""),
                name=page.get("name", ""),
                # The SDK extracts only its latest ingested chunk IDs; extract
                # immediately to cover every page in the batch.
                extract=True,
            )
            results.append(result)

        return results

    async def extract(self) -> Dict[str, Any]:
        """Run event-entity extraction on all ingested but unextracted content.

        Returns:
            Dict with extraction statistics (event_count, entity_count).
        """
        if not self._started or not self._engine:
            raise RuntimeError("SAG engine not started. Call start() first.")

        if not self._extract_pending:
            return {"status": "skipped", "reason": "no_pending_content"}

        original_cwd = os.getcwd()
        os.chdir(self._data_dir)
        try:
            result = await self._engine.extract()
        finally:
            os.chdir(original_cwd)

        self._extract_pending = False

        event_count = getattr(result, "event_count", 0) if result else 0
        log.info("SAG extract complete: events={events}".format(events=event_count))

        return {"status": "ok", "event_count": event_count}

    async def search(
        self,
        query: str,
        *,
        strategy: str = "multi",
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """Search the knowledge base using SAG retrieval.

        Args:
            query: Natural language query.
            strategy: "vector" (fast) or "multi" (precise, uses entity expansion).
            top_k: Number of results to return.

        Returns:
            List of result sections with content and metadata.
        """
        if not self._started or not self._engine:
            raise RuntimeError("SAG engine not started. Call start() first.")

        original_cwd = os.getcwd()
        os.chdir(self._data_dir)
        try:
            result = await self._engine.search(query, strategy=strategy, top_k=top_k)
        finally:
            os.chdir(original_cwd)

        sections = []
        if result and hasattr(result, "sections"):
            for section in result.sections:
                if isinstance(section, dict):
                    sections.append(section)
                else:
                    sections.append(
                        {
                            "source_id": getattr(section, "source_id", ""),
                            "chunk_id": getattr(section, "chunk_id", ""),
                            "score": getattr(section, "score", None),
                            "heading": getattr(section, "heading", ""),
                            "content": getattr(section, "content", str(section)),
                        }
                    )

        log.debug(
            "SAG search: query={q!r}, strategy={s}, results={n}".format(
                q=query[:80], s=strategy, n=len(sections)
            )
        )
        return sections


def is_sag_enabled() -> bool:
    """Check if SAG knowledge indexing is enabled via environment.

    Set SAG_ENABLED=true in .env to activate SAG integration in the crawl pipeline.
    """
    return os.getenv("SAG_ENABLED", "").strip().lower() in ("true", "1", "yes")
