-- 促销/会员身份与写入规则的数据库硬约束。
-- 正则与口径与 utils/source_url.py、utils/promotion_rules.py 保持一致（tests/test_pipeline_guards.py 校验）。

-- 来源 URL 身份键：小写 host（去 www. 与默认端口）+ 小写 path（去末尾斜杠）+ 排序后的非跟踪 query 参数
create or replace function public.fn_source_url_key(p_url text)
returns text
language sql
immutable
strict
parallel safe
set search_path to ''
as $$
  with m as (
    select regexp_match(btrim(p_url), '^(?:[a-zA-Z][a-zA-Z0-9+.-]*://)?([^/?#]*)([^?#]*)(?:\?([^#]*))?') as p
  ), parts as (
    select regexp_replace(regexp_replace(lower(p[1]), '^www\.', ''), ':(80|443)$', '') as host,
           regexp_replace(lower(coalesce(p[2], '')), '/+$', '') as path,
           p[3] as query
    from m
  ), params as (
    select string_agg(kv, '&' order by kv collate "C") as query
    from parts, regexp_split_to_table(coalesce(parts.query, ''), '&') as kv
    where kv <> ''
      and split_part(kv, '=', 1) !~* '^(utm_.*|fbclid|gclid|msclkid|mc_cid|mc_eid|_ga|_gl|hsa_.*)$'
  )
  select parts.host || parts.path || coalesce('?' || params.query, '') from parts, params
$$;

-- URL 路径中是否有促销关键词路径段
create or replace function public.fn_url_path_segments(p_url text)
returns text[]
language sql
immutable
strict
parallel safe
set search_path to ''
as $$
  select array_remove(string_to_array(
    (regexp_match(btrim(p_url), '^(?:[a-zA-Z][a-zA-Z0-9+.-]*://)?[^/?#]*([^?#]*)'))[1], '/'), '')
$$;

create or replace function public.fn_is_promo_path(p_url text)
returns boolean
language sql
immutable
strict
parallel safe
set search_path to ''
as $$
  select exists (
    select 1 from unnest(public.fn_url_path_segments(p_url)) as s
    where s ~* '(^|[^a-z0-9])(specials?|promos?|promotions?|offers?|deals?)([^a-z0-9]|$)'
  )
$$;

-- hub 子页面：除最后一段外存在促销关键词路径段，应写入 hub URL
create or replace function public.fn_is_promotion_hub_child(p_url text)
returns boolean
language sql
immutable
strict
parallel safe
set search_path to ''
as $$
  select exists (
    select 1
    from unnest(public.fn_url_path_segments(p_url)) with ordinality as t(s, i)
    where t.i < cardinality(public.fn_url_path_segments(p_url))
      and t.s ~* '(^|[^a-z0-9])(specials?|promos?|promotions?|offers?|deals?)([^a-z0-9]|$)'
  )
$$;

create or replace function public.fn_has_discount_evidence(p_text text)
returns boolean
language sql
immutable
strict
parallel safe
set search_path to ''
as $$
  select p_text ~* '([0-9]{1,3}(\.[0-9]+)? ?%|\$ ?[0-9][0-9,]*(\.[0-9]+)?) ?off([^a-z]|$)|(^|[^a-z])(save|saves|saving|savings|discount|discounted|bogo|free(?! consult)|half off|half price|special price|sale price|promo price|promotional price|limited time)([^a-z]|$)|(^|[^a-z])(was|reg|regular|regularly|originally|retail|value|valued at|normally|usually|now)[.: ]*(price[: ]*)?\$ ?[0-9]|~~ ?\$ ?[0-9]|(^|[^a-z])buy [0-9a-z]+[^.]{0,30}get([^a-z]|$)'
$$;

create or replace function public.fn_campaign_today()
returns date
language sql
stable
set search_path to ''
as $$
  select (now() at time zone 'America/Los_Angeles')::date
$$;

create or replace function public.fn_guard_clinic_promotion()
returns trigger
language plpgsql
set search_path to 'public'
as $$
declare
  v_title text := lower(btrim(regexp_replace(coalesce(new.promotion_title, ''), '\s+', ' ', 'g')));
  v_identity_changed boolean := tg_op = 'INSERT'
    or new.source_url is distinct from old.source_url
    or new.promotion_title is distinct from old.promotion_title
    or new.promotion_content is distinct from old.promotion_content;
  v_activation_changed boolean := v_identity_changed
    or new.is_active is distinct from old.is_active
    or new.campaign_end_date is distinct from old.campaign_end_date;
begin
  if v_identity_changed then
    if btrim(regexp_replace(coalesce(new.source_url, ''), '\s+', ' ', 'g')) = '' then
      raise exception 'promotion_rejected:empty_source_url' using errcode = '23514';
    end if;
    if public.fn_is_promotion_hub_child(new.source_url) then
      raise exception 'promotion_rejected:hub_child_url: % must be written as its promotion hub URL', new.source_url
        using errcode = '23514';
    end if;
    if v_title = '' then
      raise exception 'promotion_rejected:empty_title: %', new.source_url using errcode = '23514';
    end if;
  end if;

  if new.is_active and v_activation_changed then
    if new.campaign_end_date is not null and new.campaign_end_date < public.fn_campaign_today() then
      raise exception 'promotion_rejected:expired_campaign: % ended %', new.source_url, new.campaign_end_date
        using errcode = '23514';
    end if;
    if not public.fn_is_promo_path(new.source_url)
       and not public.fn_has_discount_evidence(coalesce(new.promotion_title, '') || E'\n' ||
             coalesce((select string_agg(l, E'\n') from jsonb_array_elements_text(
               case when jsonb_typeof(new.promotion_content) = 'array' then new.promotion_content else '[]'::jsonb end) as l), '')) then
      raise exception 'promotion_rejected:price_page_without_discount: %', new.source_url using errcode = '23514';
    end if;
  end if;
  return new;
end;
$$;

drop trigger if exists trg_guard_clinic_promotion on public.clinic_promotions;
create trigger trg_guard_clinic_promotion
  before insert or update on public.clinic_promotions
  for each row execute function public.fn_guard_clinic_promotion();

-- 每日由标准流程 deactivate-expired 调用；下游 offer 由 trg_cascade_promotion_active_to_offers 停用
create or replace function public.fn_deactivate_expired_promotions()
returns setof bigint
language sql
set search_path to 'public'
as $$
  update public.clinic_promotions
  set is_active = false, updated_at = now()
  where is_active and campaign_end_date < public.fn_campaign_today()
  returning promotion_id
$$;

-- 服务名称枚举的只读函数：监控主机经 REST rpc 读取，offer 抽取 schema 在运行时使用
create or replace function public.fn_clinic_service_names()
returns setof text
language sql
stable
set search_path to ''
as $$
  select e.enumlabel::text
  from pg_catalog.pg_enum e join pg_catalog.pg_type t on t.oid = e.enumtypid
  where t.typname = 'clinic_service_name_enum'
  order by e.enumsortorder
$$;

create unique index if not exists uq_clinic_promotions_business_source_key
  on public.clinic_promotions (business_id, public.fn_source_url_key(source_url));

create unique index if not exists uq_clinic_memberships_business_source_name
  on public.clinic_memberships (business_id, public.fn_source_url_key(source_url), lower(btrim(membership_name)));
