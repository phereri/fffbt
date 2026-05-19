drop extension if exists "pg_net";

create schema if not exists "fffbt";

create sequence "public"."automation_task_logs_id_seq";


  create table "fffbt"."accounts" (
    "id" uuid not null default gen_random_uuid(),
    "created_at" timestamp with time zone not null default now(),
    "updated_at" timestamp with time zone not null default now(),
    "uid" text not null,
    "email" text,
    "token" text,
    "twofa" text,
    "platform" text,
    "status" text not null default 'active'::text,
    "comment" text,
    "geo" text,
    "proxy" text,
    "shop" text,
    "password" text
      );



  create table "fffbt"."crm_task_comments" (
    "id" uuid not null default gen_random_uuid(),
    "task_id" uuid not null,
    "body" text not null,
    "created_by" text,
    "created_at" timestamp with time zone not null default now()
      );



  create table "fffbt"."crm_task_lists" (
    "id" uuid not null default gen_random_uuid(),
    "title" text not null,
    "sort_order" integer not null default 0,
    "created_at" timestamp with time zone not null default now(),
    "updated_at" timestamp with time zone not null default now()
      );



  create table "fffbt"."crm_tasks" (
    "id" uuid not null default gen_random_uuid(),
    "title" text not null,
    "sort_order" integer not null default 0,
    "created_at" timestamp with time zone not null default now(),
    "updated_at" timestamp with time zone not null default now(),
    "list_id" uuid not null,
    "description" text,
    "due_at" timestamp with time zone,
    "is_priority" boolean not null default false
      );



  create table "fffbt"."device_heartbeats" (
    "host_id" text not null,
    "serial" text not null,
    "seen_at" timestamp with time zone not null default now(),
    "connection_type" text not null,
    "state" text not null,
    "ip" text,
    "model" text,
    "product" text,
    "device" text,
    "transport_id" integer
      );



  create table "fffbt"."finance_operators" (
    "email" text not null,
    "created_at" timestamp with time zone not null default now()
      );



  create table "fffbt"."finance_viewers" (
    "email" text not null,
    "created_at" timestamp with time zone not null default now()
      );



  create table "fffbt"."finances" (
    "id" uuid not null default gen_random_uuid(),
    "created_at" timestamp with time zone not null default now(),
    "updated_at" timestamp with time zone not null default now(),
    "amount" numeric not null,
    "status" text not null,
    "requisites" text,
    "comment" text,
    "category" text,
    "category_raw" text,
    "category_group" text,
    "direction" text,
    "attribution" text not null default 'General'::text,
    "hash" text
      );



  create table "fffbt"."videos" (
    "id" text not null,
    "created_at" timestamp with time zone not null default now(),
    "updated_at" timestamp with time zone not null default now(),
    "name" text not null,
    "platform" text not null,
    "category" text not null,
    "type" text not null default ''::text,
    "status" text not null,
    "link_drive" text not null,
    "link_platform" text,
    "posted_by" text,
    "caption" text,
    "published_at" timestamp with time zone
      );



  create table "public"."automation_accounts" (
    "id" uuid not null default extensions.uuid_generate_v4(),
    "platform" text not null,
    "username" text not null,
    "device_serial" text not null,
    "host_id" text,
    "is_active" boolean not null default true,
    "last_used_at" timestamp with time zone,
    "blocked_until" timestamp with time zone,
    "notes" text,
    "created_at" timestamp with time zone not null default now()
      );



  create table "public"."automation_task_logs" (
    "id" bigint not null default nextval('public.automation_task_logs_id_seq'::regclass),
    "task_id" uuid not null,
    "ts" timestamp with time zone not null default now(),
    "level" text not null default 'info'::text,
    "source" text,
    "message" text not null
      );



  create table "public"."automation_tasks" (
    "id" uuid not null default extensions.uuid_generate_v4(),
    "kind" text not null,
    "payload" jsonb not null default '{}'::jsonb,
    "device_serial" text not null,
    "host_id" text,
    "priority" smallint not null default 0,
    "status" text not null default 'queued'::text,
    "attempts" smallint not null default 0,
    "max_attempts" smallint not null default 1,
    "created_at" timestamp with time zone not null default now(),
    "claimed_at" timestamp with time zone,
    "started_at" timestamp with time zone,
    "finished_at" timestamp with time zone,
    "result" jsonb,
    "error" text
      );



  create table "public"."automation_trajectories" (
    "task_id" uuid not null,
    "host_path" text not null,
    "step_count" integer not null default 0,
    "has_gif" boolean not null default false,
    "summary" jsonb
      );



  create table "public"."device_heartbeats" (
    "host_id" text not null,
    "serial" text not null,
    "seen_at" timestamp with time zone not null default now(),
    "connection_type" text not null,
    "state" text not null,
    "ip" text,
    "model" text,
    "product" text,
    "device" text,
    "transport_id" integer
      );


alter sequence "public"."automation_task_logs_id_seq" owned by "public"."automation_task_logs"."id";

CREATE INDEX accounts_email_idx ON fffbt.accounts USING btree (email);

CREATE UNIQUE INDEX accounts_pkey ON fffbt.accounts USING btree (id);

CREATE INDEX accounts_uid_idx ON fffbt.accounts USING btree (uid);

CREATE UNIQUE INDEX crm_task_comments_pkey ON fffbt.crm_task_comments USING btree (id);

CREATE INDEX crm_task_comments_task_id_created_at_idx ON fffbt.crm_task_comments USING btree (task_id, created_at);

CREATE UNIQUE INDEX crm_task_lists_pkey ON fffbt.crm_task_lists USING btree (id);

CREATE INDEX crm_tasks_list_id_sort_order_idx ON fffbt.crm_tasks USING btree (list_id, sort_order);

CREATE UNIQUE INDEX crm_tasks_pkey ON fffbt.crm_tasks USING btree (id);

CREATE UNIQUE INDEX device_heartbeats_pkey ON fffbt.device_heartbeats USING btree (host_id, serial);

CREATE INDEX device_heartbeats_seen_at_idx ON fffbt.device_heartbeats USING btree (seen_at DESC);

CREATE INDEX device_heartbeats_serial_idx ON fffbt.device_heartbeats USING btree (serial);

CREATE UNIQUE INDEX finance_operators_pkey ON fffbt.finance_operators USING btree (email);

CREATE UNIQUE INDEX finance_viewers_pkey ON fffbt.finance_viewers USING btree (email);

CREATE UNIQUE INDEX finances_pkey ON fffbt.finances USING btree (id);

CREATE INDEX videos_created_at_desc_idx ON fffbt.videos USING btree (created_at DESC);

CREATE UNIQUE INDEX videos_pkey ON fffbt.videos USING btree (id);

CREATE INDEX videos_platform_category_idx ON fffbt.videos USING btree (platform, category);

CREATE UNIQUE INDEX automation_accounts_pkey ON public.automation_accounts USING btree (id);

CREATE UNIQUE INDEX automation_accounts_platform_username_key ON public.automation_accounts USING btree (platform, username);

CREATE UNIQUE INDEX automation_task_logs_pkey ON public.automation_task_logs USING btree (id);

CREATE UNIQUE INDEX automation_tasks_pkey ON public.automation_tasks USING btree (id);

CREATE UNIQUE INDEX automation_trajectories_pkey ON public.automation_trajectories USING btree (task_id);

CREATE UNIQUE INDEX device_heartbeats_pkey ON public.device_heartbeats USING btree (host_id, serial);

CREATE INDEX device_heartbeats_seen_at_idx ON public.device_heartbeats USING btree (seen_at DESC);

CREATE INDEX device_heartbeats_serial_idx ON public.device_heartbeats USING btree (serial);

CREATE INDEX idx_automation_accounts_device ON public.automation_accounts USING btree (device_serial);

CREATE INDEX idx_automation_task_logs_task_ts ON public.automation_task_logs USING btree (task_id, ts);

CREATE INDEX idx_automation_tasks_device ON public.automation_tasks USING btree (device_serial);

CREATE INDEX idx_automation_tasks_host ON public.automation_tasks USING btree (host_id);

CREATE INDEX idx_automation_tasks_status_priority ON public.automation_tasks USING btree (status, priority DESC, created_at);

alter table "fffbt"."accounts" add constraint "accounts_pkey" PRIMARY KEY using index "accounts_pkey";

alter table "fffbt"."crm_task_comments" add constraint "crm_task_comments_pkey" PRIMARY KEY using index "crm_task_comments_pkey";

alter table "fffbt"."crm_task_lists" add constraint "crm_task_lists_pkey" PRIMARY KEY using index "crm_task_lists_pkey";

alter table "fffbt"."crm_tasks" add constraint "crm_tasks_pkey" PRIMARY KEY using index "crm_tasks_pkey";

alter table "fffbt"."device_heartbeats" add constraint "device_heartbeats_pkey" PRIMARY KEY using index "device_heartbeats_pkey";

alter table "fffbt"."finance_operators" add constraint "finance_operators_pkey" PRIMARY KEY using index "finance_operators_pkey";

alter table "fffbt"."finance_viewers" add constraint "finance_viewers_pkey" PRIMARY KEY using index "finance_viewers_pkey";

alter table "fffbt"."finances" add constraint "finances_pkey" PRIMARY KEY using index "finances_pkey";

alter table "fffbt"."videos" add constraint "videos_pkey" PRIMARY KEY using index "videos_pkey";

alter table "public"."automation_accounts" add constraint "automation_accounts_pkey" PRIMARY KEY using index "automation_accounts_pkey";

alter table "public"."automation_task_logs" add constraint "automation_task_logs_pkey" PRIMARY KEY using index "automation_task_logs_pkey";

alter table "public"."automation_tasks" add constraint "automation_tasks_pkey" PRIMARY KEY using index "automation_tasks_pkey";

alter table "public"."automation_trajectories" add constraint "automation_trajectories_pkey" PRIMARY KEY using index "automation_trajectories_pkey";

alter table "public"."device_heartbeats" add constraint "device_heartbeats_pkey" PRIMARY KEY using index "device_heartbeats_pkey";

alter table "fffbt"."accounts" add constraint "accounts_status_check" CHECK ((status = ANY (ARRAY['active'::text, 'pause'::text, 'stop'::text, 'ban'::text]))) not valid;

alter table "fffbt"."accounts" validate constraint "accounts_status_check";

alter table "fffbt"."crm_task_comments" add constraint "crm_task_comments_body_check" CHECK ((length(TRIM(BOTH FROM body)) > 0)) not valid;

alter table "fffbt"."crm_task_comments" validate constraint "crm_task_comments_body_check";

alter table "fffbt"."crm_task_comments" add constraint "crm_task_comments_task_id_fkey" FOREIGN KEY (task_id) REFERENCES fffbt.crm_tasks(id) ON DELETE CASCADE not valid;

alter table "fffbt"."crm_task_comments" validate constraint "crm_task_comments_task_id_fkey";

alter table "fffbt"."crm_task_lists" add constraint "crm_task_lists_title_check" CHECK ((length(TRIM(BOTH FROM title)) > 0)) not valid;

alter table "fffbt"."crm_task_lists" validate constraint "crm_task_lists_title_check";

alter table "fffbt"."crm_tasks" add constraint "crm_tasks_list_id_fkey" FOREIGN KEY (list_id) REFERENCES fffbt.crm_task_lists(id) ON DELETE CASCADE not valid;

alter table "fffbt"."crm_tasks" validate constraint "crm_tasks_list_id_fkey";

alter table "fffbt"."crm_tasks" add constraint "crm_tasks_title_check" CHECK ((length(TRIM(BOTH FROM title)) > 0)) not valid;

alter table "fffbt"."crm_tasks" validate constraint "crm_tasks_title_check";

alter table "fffbt"."device_heartbeats" add constraint "device_heartbeats_connection_type_check" CHECK ((connection_type = ANY (ARRAY['usb'::text, 'tcpip'::text]))) not valid;

alter table "fffbt"."device_heartbeats" validate constraint "device_heartbeats_connection_type_check";

alter table "fffbt"."finances" add constraint "finances_direction_check" CHECK ((direction = ANY (ARRAY['income'::text, 'expense'::text, 'neutral'::text]))) not valid;

alter table "fffbt"."finances" validate constraint "finances_direction_check";

alter table "fffbt"."finances" add constraint "finances_status_check" CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text, 'paid'::text, 'cancelled'::text]))) not valid;

alter table "fffbt"."finances" validate constraint "finances_status_check";

alter table "fffbt"."videos" add constraint "videos_id_nonempty" CHECK ((length(TRIM(BOTH FROM id)) > 0)) not valid;

alter table "fffbt"."videos" validate constraint "videos_id_nonempty";

alter table "fffbt"."videos" add constraint "videos_status_check" CHECK ((status = ANY (ARRAY['new'::text, 'verify'::text, 'posted'::text, 'error'::text]))) not valid;

alter table "fffbt"."videos" validate constraint "videos_status_check";

alter table "public"."automation_accounts" add constraint "automation_accounts_platform_check" CHECK ((platform = ANY (ARRAY['instagram'::text, 'tiktok'::text]))) not valid;

alter table "public"."automation_accounts" validate constraint "automation_accounts_platform_check";

alter table "public"."automation_accounts" add constraint "automation_accounts_platform_username_key" UNIQUE using index "automation_accounts_platform_username_key";

alter table "public"."automation_task_logs" add constraint "automation_task_logs_task_id_fkey" FOREIGN KEY (task_id) REFERENCES public.automation_tasks(id) ON DELETE CASCADE not valid;

alter table "public"."automation_task_logs" validate constraint "automation_task_logs_task_id_fkey";

alter table "public"."automation_tasks" add constraint "automation_tasks_kind_check" CHECK ((kind = ANY (ARRAY['post_ig_trial_reel'::text, 'post_tiktok'::text, 'username_change'::text, 'set_mock_location'::text, 'custom'::text]))) not valid;

alter table "public"."automation_tasks" validate constraint "automation_tasks_kind_check";

alter table "public"."automation_tasks" add constraint "automation_tasks_status_check" CHECK ((status = ANY (ARRAY['queued'::text, 'claimed'::text, 'running'::text, 'success'::text, 'failed'::text, 'cancelled'::text]))) not valid;

alter table "public"."automation_tasks" validate constraint "automation_tasks_status_check";

alter table "public"."automation_trajectories" add constraint "automation_trajectories_task_id_fkey" FOREIGN KEY (task_id) REFERENCES public.automation_tasks(id) ON DELETE CASCADE not valid;

alter table "public"."automation_trajectories" validate constraint "automation_trajectories_task_id_fkey";

alter table "public"."device_heartbeats" add constraint "device_heartbeats_connection_type_check" CHECK ((connection_type = ANY (ARRAY['usb'::text, 'tcpip'::text]))) not valid;

alter table "public"."device_heartbeats" validate constraint "device_heartbeats_connection_type_check";

set check_function_bodies = off;

create or replace view "fffbt"."v_account_publish_recent" as  SELECT id,
    COALESCE(NULLIF(TRIM(BOTH FROM posted_by), ''::text), '(unassigned)'::text) AS account,
    status,
    name,
    category,
    platform,
    link_platform,
    "left"(caption, 160) AS caption_preview,
    updated_at AS publish_updated_at,
    created_at
   FROM fffbt.videos v
  WHERE ((status = ANY (ARRAY['verify'::text, 'posted'::text])) AND (posted_by IS NOT NULL) AND (TRIM(BOTH FROM posted_by) <> ''::text));


create or replace view "fffbt"."v_account_publish_stats" as  SELECT COALESCE(NULLIF(TRIM(BOTH FROM posted_by), ''::text), '(unassigned)'::text) AS account,
    status,
    count(*) AS video_count,
    count(*) FILTER (WHERE ((link_platform IS NOT NULL) AND (TRIM(BOTH FROM link_platform) <> ''::text))) AS with_link_count,
    max(updated_at) AS last_updated_at
   FROM fffbt.videos
  GROUP BY COALESCE(NULLIF(TRIM(BOTH FROM posted_by), ''::text), '(unassigned)'::text), status;


create or replace view "fffbt"."v_account_publish_summary" as  SELECT COALESCE(NULLIF(TRIM(BOTH FROM posted_by), ''::text), '(unassigned)'::text) AS account,
    count(*) FILTER (WHERE (status = 'new'::text)) AS new_count,
    count(*) FILTER (WHERE (status = 'verify'::text)) AS verify_count,
    count(*) FILTER (WHERE (status = 'posted'::text)) AS posted_count,
    count(*) FILTER (WHERE (status = 'error'::text)) AS error_count,
    count(*) FILTER (WHERE ((published_at IS NOT NULL) AND (published_at >= (date_trunc('day'::text, (now() AT TIME ZONE 'utc'::text)) AT TIME ZONE 'utc'::text)))) AS published_today_utc,
    count(*) FILTER (WHERE ((status = 'posted'::text) AND (link_platform IS NOT NULL) AND (TRIM(BOTH FROM link_platform) <> ''::text))) AS posted_with_link,
    count(*) FILTER (WHERE ((status = 'verify'::text) AND (published_at IS NOT NULL) AND ((link_platform IS NULL) OR (TRIM(BOTH FROM link_platform) = ''::text)))) AS verify_awaiting_link,
    max(published_at) AS last_publish_at
   FROM fffbt.videos
  GROUP BY COALESCE(NULLIF(TRIM(BOTH FROM posted_by), ''::text), '(unassigned)'::text);


create or replace view "public"."automation_device_overview" as  SELECT host_id,
    serial,
    connection_type,
    state,
    ip,
    model,
    seen_at,
    ( SELECT t.status
           FROM public.automation_tasks t
          WHERE (t.device_serial = h.serial)
          ORDER BY t.created_at DESC
         LIMIT 1) AS last_task_status,
    ( SELECT t.created_at
           FROM public.automation_tasks t
          WHERE (t.device_serial = h.serial)
          ORDER BY t.created_at DESC
         LIMIT 1) AS last_task_created_at
   FROM public.device_heartbeats h;


CREATE OR REPLACE FUNCTION public.claim_next_task(p_host_id text)
 RETURNS public.automation_tasks
 LANGUAGE plpgsql
AS $function$
declare
    claimed public.automation_tasks;
begin
    update public.automation_tasks t
       set status = 'claimed',
           claimed_at = now(),
           host_id = coalesce(t.host_id, p_host_id)
     where t.id = (
         select id from public.automation_tasks
          where status = 'queued'
            and (host_id is null or host_id = p_host_id)
          order by priority desc, created_at asc
          limit 1
          for update skip locked
     )
    returning * into claimed;
    return claimed;
end;
$function$
;

CREATE OR REPLACE FUNCTION public.finance_can_access_crm()
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'public', 'fffbt'
AS $function$
  select true;
$function$
;

CREATE OR REPLACE FUNCTION public.finance_can_write()
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'public', 'fffbt'
AS $function$
  select true;
$function$
;

CREATE OR REPLACE FUNCTION public.finance_user_is_operator()
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'public', 'fffbt'
AS $function$
  select true;
$function$
;

create or replace view "public"."poker_accounts" as  SELECT id,
    created_at,
    updated_at,
    uid,
    email,
    password,
    token,
    twofa,
    platform,
    status,
    comment,
    geo,
    proxy,
    shop
   FROM fffbt.accounts;


create or replace view "public"."poker_crm_task_comments" as  SELECT id,
    task_id,
    body,
    created_by,
    created_at
   FROM fffbt.crm_task_comments;


create or replace view "public"."poker_crm_task_lists" as  SELECT id,
    title,
    sort_order,
    created_at,
    updated_at
   FROM fffbt.crm_task_lists;


create or replace view "public"."poker_crm_tasks" as  SELECT id,
    list_id,
    title,
    description,
    due_at,
    is_priority,
    sort_order,
    created_at,
    updated_at
   FROM fffbt.crm_tasks;


create or replace view "public"."poker_devices" as  SELECT host_id,
    serial,
    connection_type,
    state,
    ip,
    model,
    product,
    device,
    transport_id,
    seen_at,
    ((now() - seen_at) > '00:01:00'::interval) AS is_stale
   FROM public.device_heartbeats;


create or replace view "public"."poker_finances" as  SELECT id,
    created_at,
    updated_at,
    amount,
    status,
    requisites,
    comment,
    category,
    category_raw,
    category_group,
    direction,
    attribution,
    hash
   FROM fffbt.finances;


create or replace view "public"."poker_publish_recent" as  SELECT id,
    account,
    status,
    name,
    category,
    platform,
    link_platform,
    caption_preview,
    publish_updated_at,
    created_at
   FROM fffbt.v_account_publish_recent;


create or replace view "public"."poker_publish_stats" as  SELECT account,
    status,
    video_count,
    with_link_count,
    last_updated_at
   FROM fffbt.v_account_publish_stats;


create or replace view "public"."poker_publish_summary" as  SELECT account,
    new_count,
    verify_count,
    posted_count,
    error_count,
    published_today_utc,
    posted_with_link,
    verify_awaiting_link,
    last_publish_at
   FROM fffbt.v_account_publish_summary;


create or replace view "public"."poker_videos" as  SELECT id,
    created_at,
    updated_at,
    name,
    platform,
    category,
    type,
    status,
    link_drive,
    link_platform,
    posted_by,
    caption,
    published_at
   FROM fffbt.videos;


CREATE OR REPLACE FUNCTION public.user_may_read_crm_financial_data()
 RETURNS boolean
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'public', 'fffbt'
AS $function$
  select true;
$function$
;

grant delete on table "fffbt"."accounts" to "anon";

grant insert on table "fffbt"."accounts" to "anon";

grant select on table "fffbt"."accounts" to "anon";

grant update on table "fffbt"."accounts" to "anon";

grant delete on table "fffbt"."accounts" to "authenticated";

grant insert on table "fffbt"."accounts" to "authenticated";

grant select on table "fffbt"."accounts" to "authenticated";

grant update on table "fffbt"."accounts" to "authenticated";

grant delete on table "fffbt"."crm_task_comments" to "anon";

grant insert on table "fffbt"."crm_task_comments" to "anon";

grant select on table "fffbt"."crm_task_comments" to "anon";

grant update on table "fffbt"."crm_task_comments" to "anon";

grant delete on table "fffbt"."crm_task_comments" to "authenticated";

grant insert on table "fffbt"."crm_task_comments" to "authenticated";

grant select on table "fffbt"."crm_task_comments" to "authenticated";

grant update on table "fffbt"."crm_task_comments" to "authenticated";

grant delete on table "fffbt"."crm_task_lists" to "anon";

grant insert on table "fffbt"."crm_task_lists" to "anon";

grant select on table "fffbt"."crm_task_lists" to "anon";

grant update on table "fffbt"."crm_task_lists" to "anon";

grant delete on table "fffbt"."crm_task_lists" to "authenticated";

grant insert on table "fffbt"."crm_task_lists" to "authenticated";

grant select on table "fffbt"."crm_task_lists" to "authenticated";

grant update on table "fffbt"."crm_task_lists" to "authenticated";

grant delete on table "fffbt"."crm_tasks" to "anon";

grant insert on table "fffbt"."crm_tasks" to "anon";

grant select on table "fffbt"."crm_tasks" to "anon";

grant update on table "fffbt"."crm_tasks" to "anon";

grant delete on table "fffbt"."crm_tasks" to "authenticated";

grant insert on table "fffbt"."crm_tasks" to "authenticated";

grant select on table "fffbt"."crm_tasks" to "authenticated";

grant update on table "fffbt"."crm_tasks" to "authenticated";

grant delete on table "fffbt"."device_heartbeats" to "anon";

grant insert on table "fffbt"."device_heartbeats" to "anon";

grant select on table "fffbt"."device_heartbeats" to "anon";

grant update on table "fffbt"."device_heartbeats" to "anon";

grant delete on table "fffbt"."device_heartbeats" to "authenticated";

grant insert on table "fffbt"."device_heartbeats" to "authenticated";

grant select on table "fffbt"."device_heartbeats" to "authenticated";

grant update on table "fffbt"."device_heartbeats" to "authenticated";

grant delete on table "fffbt"."finance_operators" to "anon";

grant insert on table "fffbt"."finance_operators" to "anon";

grant select on table "fffbt"."finance_operators" to "anon";

grant update on table "fffbt"."finance_operators" to "anon";

grant delete on table "fffbt"."finance_operators" to "authenticated";

grant insert on table "fffbt"."finance_operators" to "authenticated";

grant select on table "fffbt"."finance_operators" to "authenticated";

grant update on table "fffbt"."finance_operators" to "authenticated";

grant delete on table "fffbt"."finance_viewers" to "anon";

grant insert on table "fffbt"."finance_viewers" to "anon";

grant select on table "fffbt"."finance_viewers" to "anon";

grant update on table "fffbt"."finance_viewers" to "anon";

grant delete on table "fffbt"."finance_viewers" to "authenticated";

grant insert on table "fffbt"."finance_viewers" to "authenticated";

grant select on table "fffbt"."finance_viewers" to "authenticated";

grant update on table "fffbt"."finance_viewers" to "authenticated";

grant delete on table "fffbt"."finances" to "anon";

grant insert on table "fffbt"."finances" to "anon";

grant select on table "fffbt"."finances" to "anon";

grant update on table "fffbt"."finances" to "anon";

grant delete on table "fffbt"."finances" to "authenticated";

grant insert on table "fffbt"."finances" to "authenticated";

grant select on table "fffbt"."finances" to "authenticated";

grant update on table "fffbt"."finances" to "authenticated";

grant delete on table "fffbt"."videos" to "anon";

grant insert on table "fffbt"."videos" to "anon";

grant select on table "fffbt"."videos" to "anon";

grant update on table "fffbt"."videos" to "anon";

grant delete on table "fffbt"."videos" to "authenticated";

grant insert on table "fffbt"."videos" to "authenticated";

grant select on table "fffbt"."videos" to "authenticated";

grant update on table "fffbt"."videos" to "authenticated";

grant delete on table "public"."automation_accounts" to "anon";

grant insert on table "public"."automation_accounts" to "anon";

grant references on table "public"."automation_accounts" to "anon";

grant select on table "public"."automation_accounts" to "anon";

grant trigger on table "public"."automation_accounts" to "anon";

grant truncate on table "public"."automation_accounts" to "anon";

grant update on table "public"."automation_accounts" to "anon";

grant delete on table "public"."automation_accounts" to "authenticated";

grant insert on table "public"."automation_accounts" to "authenticated";

grant references on table "public"."automation_accounts" to "authenticated";

grant select on table "public"."automation_accounts" to "authenticated";

grant trigger on table "public"."automation_accounts" to "authenticated";

grant truncate on table "public"."automation_accounts" to "authenticated";

grant update on table "public"."automation_accounts" to "authenticated";

grant delete on table "public"."automation_accounts" to "service_role";

grant insert on table "public"."automation_accounts" to "service_role";

grant references on table "public"."automation_accounts" to "service_role";

grant select on table "public"."automation_accounts" to "service_role";

grant trigger on table "public"."automation_accounts" to "service_role";

grant truncate on table "public"."automation_accounts" to "service_role";

grant update on table "public"."automation_accounts" to "service_role";

grant delete on table "public"."automation_task_logs" to "anon";

grant insert on table "public"."automation_task_logs" to "anon";

grant references on table "public"."automation_task_logs" to "anon";

grant select on table "public"."automation_task_logs" to "anon";

grant trigger on table "public"."automation_task_logs" to "anon";

grant truncate on table "public"."automation_task_logs" to "anon";

grant update on table "public"."automation_task_logs" to "anon";

grant delete on table "public"."automation_task_logs" to "authenticated";

grant insert on table "public"."automation_task_logs" to "authenticated";

grant references on table "public"."automation_task_logs" to "authenticated";

grant select on table "public"."automation_task_logs" to "authenticated";

grant trigger on table "public"."automation_task_logs" to "authenticated";

grant truncate on table "public"."automation_task_logs" to "authenticated";

grant update on table "public"."automation_task_logs" to "authenticated";

grant delete on table "public"."automation_task_logs" to "service_role";

grant insert on table "public"."automation_task_logs" to "service_role";

grant references on table "public"."automation_task_logs" to "service_role";

grant select on table "public"."automation_task_logs" to "service_role";

grant trigger on table "public"."automation_task_logs" to "service_role";

grant truncate on table "public"."automation_task_logs" to "service_role";

grant update on table "public"."automation_task_logs" to "service_role";

grant delete on table "public"."automation_tasks" to "anon";

grant insert on table "public"."automation_tasks" to "anon";

grant references on table "public"."automation_tasks" to "anon";

grant select on table "public"."automation_tasks" to "anon";

grant trigger on table "public"."automation_tasks" to "anon";

grant truncate on table "public"."automation_tasks" to "anon";

grant update on table "public"."automation_tasks" to "anon";

grant delete on table "public"."automation_tasks" to "authenticated";

grant insert on table "public"."automation_tasks" to "authenticated";

grant references on table "public"."automation_tasks" to "authenticated";

grant select on table "public"."automation_tasks" to "authenticated";

grant trigger on table "public"."automation_tasks" to "authenticated";

grant truncate on table "public"."automation_tasks" to "authenticated";

grant update on table "public"."automation_tasks" to "authenticated";

grant delete on table "public"."automation_tasks" to "service_role";

grant insert on table "public"."automation_tasks" to "service_role";

grant references on table "public"."automation_tasks" to "service_role";

grant select on table "public"."automation_tasks" to "service_role";

grant trigger on table "public"."automation_tasks" to "service_role";

grant truncate on table "public"."automation_tasks" to "service_role";

grant update on table "public"."automation_tasks" to "service_role";

grant delete on table "public"."automation_trajectories" to "anon";

grant insert on table "public"."automation_trajectories" to "anon";

grant references on table "public"."automation_trajectories" to "anon";

grant select on table "public"."automation_trajectories" to "anon";

grant trigger on table "public"."automation_trajectories" to "anon";

grant truncate on table "public"."automation_trajectories" to "anon";

grant update on table "public"."automation_trajectories" to "anon";

grant delete on table "public"."automation_trajectories" to "authenticated";

grant insert on table "public"."automation_trajectories" to "authenticated";

grant references on table "public"."automation_trajectories" to "authenticated";

grant select on table "public"."automation_trajectories" to "authenticated";

grant trigger on table "public"."automation_trajectories" to "authenticated";

grant truncate on table "public"."automation_trajectories" to "authenticated";

grant update on table "public"."automation_trajectories" to "authenticated";

grant delete on table "public"."automation_trajectories" to "service_role";

grant insert on table "public"."automation_trajectories" to "service_role";

grant references on table "public"."automation_trajectories" to "service_role";

grant select on table "public"."automation_trajectories" to "service_role";

grant trigger on table "public"."automation_trajectories" to "service_role";

grant truncate on table "public"."automation_trajectories" to "service_role";

grant update on table "public"."automation_trajectories" to "service_role";

grant delete on table "public"."device_heartbeats" to "anon";

grant insert on table "public"."device_heartbeats" to "anon";

grant references on table "public"."device_heartbeats" to "anon";

grant select on table "public"."device_heartbeats" to "anon";

grant trigger on table "public"."device_heartbeats" to "anon";

grant truncate on table "public"."device_heartbeats" to "anon";

grant update on table "public"."device_heartbeats" to "anon";

grant delete on table "public"."device_heartbeats" to "authenticated";

grant insert on table "public"."device_heartbeats" to "authenticated";

grant references on table "public"."device_heartbeats" to "authenticated";

grant select on table "public"."device_heartbeats" to "authenticated";

grant trigger on table "public"."device_heartbeats" to "authenticated";

grant truncate on table "public"."device_heartbeats" to "authenticated";

grant update on table "public"."device_heartbeats" to "authenticated";

grant delete on table "public"."device_heartbeats" to "service_role";

grant insert on table "public"."device_heartbeats" to "service_role";

grant references on table "public"."device_heartbeats" to "service_role";

grant select on table "public"."device_heartbeats" to "service_role";

grant trigger on table "public"."device_heartbeats" to "service_role";

grant truncate on table "public"."device_heartbeats" to "service_role";

grant update on table "public"."device_heartbeats" to "service_role";


