-- Harden the pump relay (project sduzxijjvpbfgvlwcwpp / Netball-IQ).
-- Safe to run live: verified against the Pi poller and the pump web pages.
--   * Pi poller (pi_pump_poller.py) authenticates as the PUBLIC anon key and
--     does plain INSERT only (upsert/merge-duplicates was dropped 9 Sep 2026;
--     it never UPDATEs, DELETEs or TRUNCATEs).
--   * pump-station.html / pump-dashboard-2.html read pump_stats_v2 via plain
--     GET/SELECT only (supaFetch/supaFetchAll).
-- So anon needs ONLY select (site read) + insert (Pi write) on pump_stats_v2.
--
-- 1) pump_stats_v2 was the lone pump table with RLS OFF and anon holding full
--    write+truncate. Bring it in line with its working sibling pump_minute_stats.
drop policy if exists anon_read   on public.pump_stats_v2;
drop policy if exists anon_insert on public.pump_stats_v2;
create policy anon_read   on public.pump_stats_v2 for select to anon using (true);
create policy anon_insert on public.pump_stats_v2 for insert to anon with check (true);
alter table public.pump_stats_v2 enable row level security;
revoke update, delete, truncate, references, trigger on public.pump_stats_v2 from anon;

-- 2) The public anon key holds TRUNCATE on every pump table. TRUNCATE is NOT
--    governed by RLS, so anyone with the (public) anon key could wipe a table.
--    The Pi never truncates or deletes - strip both from anon. DELETE is already
--    RLS-blocked on these (no delete policy); this removes the stray grant too.
revoke delete, truncate on public.pump_minute_stats     from anon;
revoke delete, truncate on public.pump_events_hires     from anon;
revoke delete, truncate on public.pump_router_status    from anon;
revoke delete, truncate on public.monitor_state         from anon;
revoke delete, truncate on public.pump_config_snapshots from anon;
