-- Supabase RPC powering the Pump Station Report on admin.html.
-- SECURITY DEFINER so the anon key can call it without exposing the raw
-- pump_minute_stats / pump_events_hires tables (anon SELECT on those is revoked).
-- Returns per-day: night-irrigation pressure (6pm-8am, while pumping), jockey-pump
-- short-start counts (run <60s / <30s = leak signature) with pairing done from the
-- hi-res event log, telemetry minutes (gap detection, <1440 = station offline part
-- of the day). Daily WaterNSW flow is added client-side from water_register_daily.json.
-- Applied to project sduzxijjvpbfgvlwcwpp (hosts the WWCC pump relay).

create or replace function public.pump_daily_report(p_from date, p_to date)
returns table(
  day date, p_avg real, p_low real, p_high real, night_min integer,
  jockey_total integer, jockey_lt60 integer, jockey_lt30 integer, tel_minutes integer
)
language sql
security definer
set search_path = public
as $$
  with prn as (
    select (ts at time zone 'Australia/Sydney')::date d,
           avg(pressure_avg)::real pa, min(pressure_min)::real pl, max(pressure_max)::real ph,
           count(*)::int nmin
    from pump_minute_stats
    where (ts at time zone 'Australia/Sydney')::date between p_from and p_to
      and (extract(hour from (ts at time zone 'Australia/Sydney')) >= 18
           or extract(hour from (ts at time zone 'Australia/Sydney')) < 8)
      and flow_avg > 0
    group by 1
  ),
  tel as (
    select (ts at time zone 'Australia/Sydney')::date d, count(*)::int tm
    from pump_minute_stats
    where (ts at time zone 'Australia/Sydney')::date between p_from and p_to
    group by 1
  ),
  ev as (
    select ts, event_type,
           lead(ts) over (order by ts) nts,
           lead(event_type) over (order by ts) ntp
    from pump_events_hires
    where pump_label = 'Jockey' and event_type in ('pump_start','pump_stop')
      and ts >= ((p_from::timestamp) at time zone 'Australia/Sydney') - interval '6 hours'
      and ts <  (((p_to + 1)::timestamp) at time zone 'Australia/Sydney') + interval '6 hours'
  ),
  jk as (
    select (ts at time zone 'Australia/Sydney')::date d,
           count(*) filter (where event_type='pump_start' and ntp='pump_stop')::int tot,
           count(*) filter (where event_type='pump_start' and ntp='pump_stop' and nts-ts < interval '60 seconds')::int lt60,
           count(*) filter (where event_type='pump_start' and ntp='pump_stop' and nts-ts < interval '30 seconds')::int lt30
    from ev
    where (ts at time zone 'Australia/Sydney')::date between p_from and p_to
    group by 1
  ),
  days as (select generate_series(p_from, p_to, interval '1 day')::date d)
  select days.d, prn.pa, prn.pl, prn.ph, coalesce(prn.nmin,0),
    coalesce(jk.tot,0), coalesce(jk.lt60,0), coalesce(jk.lt30,0), coalesce(tel.tm,0)
  from days
  left join prn on prn.d = days.d
  left join jk  on jk.d  = days.d
  left join tel on tel.d = days.d
  order by days.d;
$$;

grant execute on function public.pump_daily_report(date, date) to anon;
