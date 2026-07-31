"""Persistent Supabase projection for the structured SAG pilot."""
from __future__ import annotations

import json
import os
import re
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import requests
from dotenv import load_dotenv

from utils.offer_extraction_llm import OpenAICompatibleClient
from utils.sag_structured_corpus import StructuredCorpusDocument

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORPUS_VERSION = "structured-v1"
EXTRACTION_VERSION = "sag-structured-v1"
_NORMALIZE_ENTITY = re.compile(r"\s+")

EVENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "events": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "content": {"type": "string"},
                    "category": {"type": "string"},
                    "entities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "type": {"type": "string"},
                                "name": {"type": "string"},
                                "role_description": {"type": "string"},
                                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            },
                            "required": ["type", "name", "role_description", "confidence"],
                        },
                    },
                },
                "required": ["title", "summary", "content", "category", "entities"],
            },
        }
    },
    "required": ["events"],
}

QUERY_ENTITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"entities": {"type": "array", "items": {"type": "string"}, "maxItems": 8}},
    "required": ["entities"],
}


@dataclass(frozen=True)
class ExtractedEntity:
    entity_type: str
    name: str
    role_description: str
    confidence: float


@dataclass(frozen=True)
class ExtractedEvent:
    title: str
    summary: str
    content: str
    category: str
    entities: tuple[ExtractedEntity, ...]


def normalize_entity_name(value: Any) -> str:
    return _NORMALIZE_ENTITY.sub(" ", str(value or "").strip().lower())


def vector_literal(vector: Sequence[float]) -> str:
    if not vector:
        raise ValueError("Embedding vector must not be empty")
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def parse_events(payload: Mapping[str, Any]) -> tuple[ExtractedEvent, ...]:
    events: list[ExtractedEvent] = []
    for item in payload.get("events") or []:
        if not isinstance(item, Mapping):
            continue
        title = str(item.get("title") or "").strip()
        summary = str(item.get("summary") or "").strip()
        content = str(item.get("content") or "").strip()
        if not (title and summary and content):
            continue
        entities: list[ExtractedEntity] = []
        for entity in item.get("entities") or []:
            if not isinstance(entity, Mapping):
                continue
            entity_type = str(entity.get("type") or "concept").strip().lower() or "concept"
            name = str(entity.get("name") or "").strip()
            if not name:
                continue
            entities.append(
                ExtractedEntity(
                    entity_type=entity_type[:50],
                    name=name[:500],
                    role_description=str(entity.get("role_description") or "")[:2000],
                    confidence=max(0.0, min(1.0, float(entity.get("confidence") or 0))),
                )
            )
        events.append(
            ExtractedEvent(
                title=title[:255],
                summary=summary,
                content=content,
                category=str(item.get("category") or "")[:100],
                entities=tuple(entities),
            )
        )
    return tuple(events)


class OpenAICompatibleEmbeddingClient:
    """Small OpenAI-compatible embedding client used by the internal pilot only."""

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout: int = 90):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def embed(self, text: str) -> list[float]:
        if not text.strip():
            raise ValueError("Cannot embed empty text")
        response = requests.post(
            f"{self.base_url}/embeddings",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "input": text},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        values = ((payload.get("data") or [{}])[0].get("embedding") or [])
        if not values:
            raise RuntimeError("Embedding response was empty")
        return [float(value) for value in values]


def build_pilot_clients(
    *,
    llm_model: str,
    embedding_model: str,
    llm_base_url: str,
    embedding_base_url: str,
    api_key: str = "local",
) -> tuple[OpenAICompatibleClient, OpenAICompatibleEmbeddingClient]:
    """Build explicit local OpenAI-compatible clients; no Gemini fallback is permitted."""
    normalized_key = api_key.strip().strip("'\"") or "local"
    normalized_llm_url = llm_base_url.rstrip("/")
    normalized_embedding_url = embedding_base_url.rstrip("/")
    if not normalized_llm_url or not normalized_embedding_url:
        raise RuntimeError("Local LLM and embedding base URLs are required for persistent SAG pilot")
    return (
        OpenAICompatibleClient(
            api_url=f"{normalized_llm_url}/chat/completions",
            api_key=normalized_key,
            model=llm_model,
        ),
        OpenAICompatibleEmbeddingClient(
            base_url=normalized_embedding_url,
            api_key=normalized_key,
            model=embedding_model,
        ),
    )


def extract_document_events(llm: Any, document: StructuredCorpusDocument) -> tuple[ExtractedEvent, ...]:
    payload = llm.create_json_response(
        [
            {
                "role": "system",
                "content": (
                    "Extract only facts explicitly present in this CostFinder structured record. "
                    "Return compact events and typed entities. Do not infer missing prices, services, or discounts."
                ),
            },
            {"role": "user", "content": document.content},
        ],
        json_schema=EVENT_SCHEMA,
    )
    return parse_events(payload)


def extract_query_entities(llm: Any, query: str) -> list[str]:
    payload = llm.create_json_response(
        [
            {"role": "system", "content": "Extract concise named entities and service terms from the query."},
            {"role": "user", "content": query},
        ],
        json_schema=QUERY_ENTITY_SCHEMA,
    )
    return [str(item).strip() for item in payload.get("entities") or [] if str(item).strip()]


class SagPilotRepository:
    """Direct, server-only PostgreSQL repository for the unexposed ``sag`` schema."""

    def __init__(self, db_url: str):
        self.db_url = db_url

    @classmethod
    def from_env(cls) -> "SagPilotRepository":
        load_dotenv(PROJECT_ROOT / ".env")
        db_url = (
            os.getenv("SAG_DATABASE_URL")
            or os.getenv("SUPABASE_DB_URL")
            or os.getenv("DATABASE_URL")
            or os.getenv("DB_URL")
        )
        if not db_url:
            raise RuntimeError(
                "Missing SAG_DATABASE_URL, SUPABASE_DB_URL, DATABASE_URL, or DB_URL for SAG persistence"
            )
        return cls(db_url)

    def _connect(self):
        import psycopg

        return psycopg.connect(self.db_url)

    def validate_connection(self) -> None:
        """Fail before fetching source rows or making Gemini calls when worker access is invalid."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("select 1")
            cur.fetchone()

    def ensure_index_version(self, *, llm_model: str, embedding_model: str) -> str:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into sag.index_version (corpus_version, llm_model, embedding_model)
                values (%s, %s, %s)
                on conflict (corpus_version, llm_model, embedding_model)
                do update set status = case when sag.index_version.status = 'failed' then 'building' else sag.index_version.status end
                returning id
                """,
                (CORPUS_VERSION, llm_model, embedding_model),
            )
            return str(cur.fetchone()[0])

    def upsert_documents(
        self, index_version_id: str, documents: Iterable[StructuredCorpusDocument]
    ) -> list[tuple[str, StructuredCorpusDocument]]:
        changed: list[tuple[str, StructuredCorpusDocument]] = []
        with self._connect() as conn, conn.cursor() as cur:
            for document in documents:
                content_hash = hashlib.sha256(document.content.encode("utf-8")).hexdigest()
                cur.execute(
                    """
                    select id, content_hash from sag.document
                    where index_version_id = %s and record_type = %s and record_id = %s
                    """,
                    (index_version_id, document.record_type, int(document.record_id)),
                )
                existing = cur.fetchone()
                if existing and existing[1] == content_hash:
                    continue
                if existing:
                    document_id = str(existing[0])
                    cur.execute(
                        """
                        update sag.document
                        set business_id = %s, source_url = %s, source_updated_at = nullif(%s, '')::timestamptz,
                            content = %s, content_hash = %s, updated_at = now()
                        where id = %s
                        """,
                        (int(document.business_id), document.source_url or None, document.updated_at, document.content, content_hash, document_id),
                    )
                else:
                    cur.execute(
                        """
                        insert into sag.document (
                          index_version_id, record_type, record_id, business_id, source_url,
                          source_updated_at, content, content_hash
                        ) values (%s, %s, %s, %s, %s, nullif(%s, '')::timestamptz, %s, %s)
                        returning id
                        """,
                        (
                            index_version_id, document.record_type, int(document.record_id), int(document.business_id),
                            document.source_url or None, document.updated_at, document.content, content_hash,
                        ),
                    )
                    document_id = str(cur.fetchone()[0])
                cur.execute(
                    """
                    insert into sag.index_job (document_id, operation, status, last_error, updated_at)
                    values (%s, 'upsert', 'pending', null, now())
                    on conflict (document_id, operation)
                    do update set status = 'pending', locked_at = null, last_error = null, updated_at = now()
                    """,
                    (document_id,),
                )
                changed.append((document_id, document))
        return changed

    def mark_job(self, document_id: str, *, status: str, error: str | None = None) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update sag.index_job
                set status = %s, attempt_count = attempt_count + case when %s = 'running' then 1 else 0 end,
                    locked_at = case when %s = 'running' then now() else locked_at end,
                    last_error = %s, updated_at = now()
                where document_id = %s and operation = 'upsert'
                """,
                (status, status, status, error, document_id),
            )

    def set_embedding_dimensions(self, index_version_id: str, dimensions: int) -> None:
        if dimensions <= 0 or dimensions > 2000:
            raise ValueError(f"Unsupported pgvector dimension: {dimensions}")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("select embedding_dimensions from sag.index_version where id = %s for update", (index_version_id,))
            existing = cur.fetchone()
            if not existing:
                raise RuntimeError("SAG index version was not found")
            if existing[0] not in (None, dimensions):
                raise RuntimeError(f"Embedding dimension mismatch: expected {existing[0]}, got {dimensions}")
            if existing[0] is None:
                for table in ("chunk_embedding", "entity_embedding", "event_embedding"):
                    cur.execute(
                        f"alter table sag.{table} alter column embedding type extensions.vector({dimensions}) using embedding::extensions.vector({dimensions})"
                    )
                cur.execute("update sag.index_version set embedding_dimensions = %s where id = %s", (dimensions, index_version_id))
                for table in ("chunk_embedding", "entity_embedding", "event_embedding"):
                    cur.execute(
                        f"create index if not exists sag_{table}_embedding_hnsw on sag.{table} using hnsw (embedding extensions.vector_cosine_ops)"
                    )

    def persist_document_result(
        self,
        *,
        index_version_id: str,
        document_id: str,
        document: StructuredCorpusDocument,
        events: Sequence[ExtractedEvent],
        embedding_client: OpenAICompatibleEmbeddingClient,
        embedding_model: str,
    ) -> None:
        chunk_vector = embedding_client.embed(document.content)
        self.set_embedding_dimensions(index_version_id, len(chunk_vector))
        with self._connect() as conn, conn.cursor() as cur:
            # Replace dependent materialization in this one transaction.  If extraction
            # persistence or any embedding write fails, the previous searchable graph
            # remains intact and the index job records the failure.
            cur.execute("delete from sag.event where document_id = %s", (document_id,))
            cur.execute("delete from sag.chunk where document_id = %s", (document_id,))
            cur.execute(
                """
                delete from sag.entity entity
                where entity.index_version_id = %s
                  and not exists (
                    select 1 from sag.event_entity edge where edge.entity_id = entity.id
                  )
                """,
                (index_version_id,),
            )
            cur.execute(
                """
                insert into sag.chunk (document_id, chunk_index, heading, content, content_hash)
                values (%s, 0, %s, %s, %s) returning id
                """,
                (
                    document_id,
                    "CostFinder structured record",
                    document.content,
                    hashlib.sha256(document.content.encode("utf-8")).hexdigest(),
                ),
            )
            chunk_id = str(cur.fetchone()[0])
            cur.execute(
                "insert into sag.chunk_embedding (chunk_id, embedding, embedding_model) values (%s, %s::extensions.vector, %s)",
                (chunk_id, vector_literal(chunk_vector), embedding_model),
            )
            entity_ids: dict[tuple[str, str], str] = {}
            for event in events:
                cur.execute(
                    """
                    insert into sag.event (document_id, chunk_id, title, summary, content, category, source_references, extraction_version)
                    values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s) returning id
                    """,
                    (
                        document_id,
                        chunk_id,
                        event.title,
                        event.summary,
                        event.content,
                        event.category or None,
                        json.dumps([{"document_id": document_id, "record_type": document.record_type, "record_id": document.record_id}]),
                        EXTRACTION_VERSION,
                    ),
                )
                event_id = str(cur.fetchone()[0])
                event_vector = embedding_client.embed(f"{event.title}\n{event.summary}\n{event.content}")
                cur.execute(
                    "insert into sag.event_embedding (event_id, embedding, embedding_model) values (%s, %s::extensions.vector, %s)",
                    (event_id, vector_literal(event_vector), embedding_model),
                )
                for entity in event.entities:
                    key = (entity.entity_type, normalize_entity_name(entity.name))
                    entity_id = entity_ids.get(key)
                    if entity_id is None:
                        cur.execute(
                            """
                            insert into sag.entity (index_version_id, entity_type, name, normalized_name)
                            values (%s, %s, %s, %s)
                            on conflict (index_version_id, entity_type, normalized_name)
                            do update set name = excluded.name
                            returning id
                            """,
                            (index_version_id, entity.entity_type, entity.name, key[1]),
                        )
                        entity_id = str(cur.fetchone()[0])
                        entity_ids[key] = entity_id
                        entity_vector = embedding_client.embed(entity.name)
                        cur.execute(
                            """
                            insert into sag.entity_embedding (entity_id, embedding, embedding_model)
                            values (%s, %s::extensions.vector, %s)
                            on conflict (entity_id) do update set embedding = excluded.embedding, embedding_model = excluded.embedding_model
                            """,
                            (entity_id, vector_literal(entity_vector), embedding_model),
                        )
                    cur.execute(
                        """
                        insert into sag.event_entity (event_id, entity_id, role_description, confidence)
                        values (%s, %s, %s, %s)
                        on conflict (event_id, entity_id) do update set role_description = excluded.role_description, confidence = excluded.confidence
                        """,
                        (event_id, entity_id, entity.role_description or None, entity.confidence),
                    )
        self.mark_job(document_id, status="succeeded")

    def vector_search(self, *, index_version_id: str, vector: Sequence[float], top_k: int) -> list[dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select d.id as document_id, d.business_id, d.record_type, d.record_id, d.source_url, c.id as chunk_id,
                       1 - (ce.embedding <=> %s::extensions.vector) as score, c.heading, c.content
                from sag.chunk_embedding ce
                join sag.chunk c on c.id = ce.chunk_id
                join sag.document d on d.id = c.document_id
                where d.index_version_id = %s
                order by ce.embedding <=> %s::extensions.vector
                limit %s
                """,
                (vector_literal(vector), index_version_id, vector_literal(vector), top_k),
            )
            columns = [item.name for item in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def multi_search(self, *, index_version_id: str, vectors: Sequence[Sequence[float]], top_k: int) -> list[dict[str, Any]]:
        if not vectors:
            return []
        literals = [vector_literal(vector) for vector in vectors]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                with query_vectors as (
                  select vector_text
                  from unnest(%s::text[]) as query(vector_text)
                ), entity_candidates as (
                  select candidate.entity_id, max(candidate.score) as score
                  from query_vectors query
                  cross join lateral (
                    select embedding.entity_id,
                           1 - (embedding.embedding <=> query.vector_text::extensions.vector) as score
                    from sag.entity_embedding embedding
                    join sag.entity entity on entity.id = embedding.entity_id
                    where entity.index_version_id = %s
                    order by embedding.embedding <=> query.vector_text::extensions.vector
                    limit 12
                  ) candidate
                  group by candidate.entity_id
                ), seed_businesses as (
                  select d.business_id, max(candidate.score) as seed_score
                  from entity_candidates candidate
                  join sag.event_entity edge on edge.entity_id = candidate.entity_id
                  join sag.event event on event.id = edge.event_id
                  join sag.document d on d.id = event.document_id
                  where d.index_version_id = %s
                  group by d.business_id
                ), event_scores as (
                  select event.id as event_id,
                         max(1 - (embedding.embedding <=> query.vector_text::extensions.vector)) as event_score
                  from query_vectors query
                  join sag.event_embedding embedding on true
                  join sag.event event on event.id = embedding.event_id
                  join sag.document d on d.id = event.document_id
                  where d.index_version_id = %s
                  group by event.id
                )
                select d.id as document_id, d.business_id, d.record_type, d.record_id, d.source_url, event.id as event_id,
                       chunk.id as chunk_id, event.title, event.summary, chunk.content,
                       0.65 * seed.seed_score + 0.35 * coalesce(scores.event_score, 0) as score
                from sag.document d
                join sag.event event on event.document_id = d.id
                join sag.chunk chunk on chunk.id = event.chunk_id
                join seed_businesses seed on seed.business_id = d.business_id
                left join event_scores scores on scores.event_id = event.id
                where d.index_version_id = %s
                order by score desc, event.created_at desc
                limit %s
                """,
                (
                    literals,
                    index_version_id,
                    index_version_id,
                    index_version_id,
                    index_version_id,
                    top_k,
                ),
            )
            columns = [item.name for item in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def finish_index(self, index_version_id: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update sag.index_version set status = case
                  when exists (select 1 from sag.index_job j join sag.document d on d.id = j.document_id where d.index_version_id = %s and j.status = 'failed') then 'failed'
                  else 'ready' end,
                  completed_at = now()
                where id = %s
                """,
                (index_version_id, index_version_id),
            )
