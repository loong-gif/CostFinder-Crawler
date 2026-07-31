-- Derived, server-only SAG projection. Business tables remain the source of truth.
create schema if not exists sag;
create extension if not exists vector with schema extensions;

revoke all on schema sag from public, anon, authenticated;
alter default privileges in schema sag revoke all on tables from public, anon, authenticated;
alter default privileges in schema sag revoke all on functions from public, anon, authenticated;

create table if not exists sag.index_version (
  id uuid primary key default gen_random_uuid(),
  corpus_version text not null,
  llm_model text not null,
  embedding_model text not null,
  embedding_dimensions integer,
  status text not null default 'building'
    check (status in ('building', 'ready', 'failed', 'retired')),
  created_at timestamptz not null default now(),
  completed_at timestamptz,
  unique (corpus_version, llm_model, embedding_model)
);

create table if not exists sag.document (
  id uuid primary key default gen_random_uuid(),
  index_version_id uuid not null references sag.index_version(id) on delete cascade,
  record_type text not null check (record_type in ('clinic_service', 'clinic_membership', 'clinic_promotion')),
  record_id bigint not null,
  business_id bigint not null references public.master_business_info(business_id) on delete restrict,
  source_url text,
  source_updated_at timestamptz,
  content text not null,
  content_hash text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (index_version_id, record_type, record_id)
);
create index if not exists sag_document_version_business_idx on sag.document(index_version_id, business_id);
create index if not exists sag_document_content_hash_idx on sag.document(index_version_id, content_hash);

create table if not exists sag.chunk (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references sag.document(id) on delete cascade,
  chunk_index integer not null check (chunk_index >= 0),
  heading text,
  content text not null,
  content_hash text not null,
  created_at timestamptz not null default now(),
  unique (document_id, chunk_index)
);

create table if not exists sag.event (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references sag.document(id) on delete cascade,
  chunk_id uuid references sag.chunk(id) on delete set null,
  title text not null,
  summary text not null,
  content text not null,
  category text,
  source_references jsonb not null default '[]'::jsonb,
  extraction_version text not null,
  created_at timestamptz not null default now()
);
create index if not exists sag_event_document_idx on sag.event(document_id);

create table if not exists sag.entity (
  id uuid primary key default gen_random_uuid(),
  index_version_id uuid not null references sag.index_version(id) on delete cascade,
  entity_type text not null,
  name text not null,
  normalized_name text not null,
  value_json jsonb,
  created_at timestamptz not null default now(),
  unique (index_version_id, entity_type, normalized_name)
);
create index if not exists sag_entity_version_type_name_idx on sag.entity(index_version_id, entity_type, normalized_name);

create table if not exists sag.event_entity (
  event_id uuid not null references sag.event(id) on delete cascade,
  entity_id uuid not null references sag.entity(id) on delete cascade,
  role_description text,
  weight numeric(5,4) not null default 1.0 check (weight >= 0 and weight <= 1),
  confidence numeric(5,4) check (confidence >= 0 and confidence <= 1),
  primary key (event_id, entity_id)
);
create index if not exists sag_event_entity_entity_idx on sag.event_entity(entity_id, event_id);

create table if not exists sag.chunk_embedding (
  chunk_id uuid primary key references sag.chunk(id) on delete cascade,
  embedding extensions.vector not null,
  embedding_model text not null,
  created_at timestamptz not null default now()
);
create table if not exists sag.entity_embedding (
  entity_id uuid primary key references sag.entity(id) on delete cascade,
  embedding extensions.vector not null,
  embedding_model text not null,
  created_at timestamptz not null default now()
);
create table if not exists sag.event_embedding (
  event_id uuid primary key references sag.event(id) on delete cascade,
  embedding extensions.vector not null,
  embedding_model text not null,
  created_at timestamptz not null default now()
);

create table if not exists sag.index_job (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references sag.document(id) on delete cascade,
  operation text not null default 'upsert' check (operation in ('upsert', 'delete')),
  status text not null default 'pending' check (status in ('pending', 'running', 'succeeded', 'failed')),
  attempt_count integer not null default 0 check (attempt_count >= 0),
  locked_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (document_id, operation)
);
create index if not exists sag_index_job_status_idx on sag.index_job(status, created_at);
