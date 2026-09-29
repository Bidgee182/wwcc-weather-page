-- Applied to Supabase project sduzxijjvpbfgvlwcwpp (Netball-IQ / pump feed), 30 Sep 2026.
-- One-row-per-day rollup used by scripts/pump_cost_summary.py to build accurate,
-- outage-proof pump energy/cost. Returns the day's cumulative energy counters
-- (start/end sys_energy_kwh + per-pump kWh) and the integrated time-of-use split
-- from per-minute power (peak 17-20 wkdy, off-peak 22-07 + weekends, else shoulder).
-- SECURITY DEFINER so the anon role can call it without SELECT on the base table
-- (it only ever returns aggregates). Grant execute to anon/authenticated.
create or replace function public.pump_cost_day(d date)
returns table(
  day date, mins integer,
  start_counter bigint, end_counter bigint,
  p1_end integer, p2_end integer, p3_end integer, jockey_end integer,
  peak_kwh numeric, shoulder_kwh numeric, off_kwh numeric, integ_total numeric,
  vol_m3 numeric
) language sql stable security definer set search_path = public as $$
  with r as (
    select
      coalesce(power_avg_kw,0)::numeric * coalesce(samples,60) / 3600.0 as e,
      extract(dow  from ts at time zone 'Australia/Sydney') as dow,
      extract(hour from ts at time zone 'Australia/Sydney') as hr,
      sys_energy_kwh, p1_kwh, p2_kwh, p3_kwh, jockey_kwh,
      coalesce(flow_avg,0)::numeric * coalesce(samples,60) / 60.0 as vol
    from pump_minute_stats
    where (ts at time zone 'Australia/Sydney')::date = d
  )
  select d, count(*)::int,
    min(sys_energy_kwh), max(sys_energy_kwh),
    max(p1_kwh), max(p2_kwh), max(p3_kwh), max(jockey_kwh),
    coalesce(sum(e) filter (where dow not in (0,6) and hr>=17 and hr<20), 0),
    coalesce(sum(e) filter (where dow not in (0,6) and not (hr>=17 and hr<20) and not (hr>=22 or hr<7)), 0),
    coalesce(sum(e) filter (where (dow in (0,6)) or (hr>=22 or hr<7)), 0),
    coalesce(sum(e), 0),
    coalesce(sum(vol), 0)
  from r;
$$;
grant execute on function public.pump_cost_day(date) to anon, authenticated;
notify pgrst, 'reload schema';
