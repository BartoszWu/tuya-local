# Euroster 4040 Smart: schedule backend

The heating-only `euroster4040_thermostat` profile keeps the same climate,
number and button unique IDs. Power on now reports `heat`, not `heat_cool`;
Program remains a preset, independent of heating activity. Consumers testing
for the old HVAC state must accept `heat` before deployment. This does not
reconfigure the physical thermostat's installer settings.

## Protocol evidence

The user's sanitized app captures establish the 19-byte DP104/DP110 format:
weekday (1=Monday..7=Sunday), 12 schedule bytes, night whole/tenths, day
whole/tenths, two unknown bytes. Each byte contains four half-hour periods,
MSB first: 01=night, 10=day. The two temperatures are global, not per weekday.
Unknown slot codes, malformed base64, lengths, day numbers and temperatures
are rejected. Temperature support stays within the existing profile's 5..35°C.

Historical capture fixtures in `tests/test_euroster_weekly.py` are not a live
schedule. No device identifiers, credentials or raw diagnostics are included.
The [manufacturer manual](https://euroster.pl/assets/files/euroster-4040-smart-instrukcja-obslugi.pdf),
sections 6.3–6.5, describes temporary override ending with the next program,
global temperature levels and half-hour periods.

## Read-only stage

Call `tuya_local.refresh_schedule` targeting this climate entity. The service
is rejected for other profiles. It requests DP102 integer days 1..7 serially,
waiting for a real DP104 frame with the matching first byte each time. The
old hidden Fetch schedule button keeps its ID, but now sends the confirmed
integer 1 rather than a boolean; it reads only Monday. Use the service for
all seven days.

The reader uses `on_receive`, not `get_property`: Tuya's anticipated-write
cache is not confirmation. Each request has an 8-second deadline including
sending, and at most two attempts (250 ms between attempts). Concurrent
refreshes serialize. Cancellation cleans up the waiter and releases the lock.
No polling loop or automatic startup command is added. Refresh explicitly
when opening the schedule; a complete cache is considered stale after one hour.

A refresh atomically publishes a complete week only if all seven reports agree
on global temperatures. Partial failures retain the previous week as stale.
Detected external changes, invalid frames or observed unavailability invalidate
the cache. Unobserved app edits cannot be detected without another read; the
one-hour age is a cache policy, not a device revision guarantee.

The existing climate entity exposes:

```text
weekly_schedule:
  schema_version: 1
  status: ready | refreshing | stale | not_loaded | unavailable
  source: device_dp104
  last_synced_at: ISO-8601 UTC | null
  max_age_seconds: 3600
  error: stable reason code | null
  days: [{ day: 1..7, periods: [48 × "night" | "day"] }]
  day_temperature: number | null
  night_temperature: number | null
  current_period: night | day | null
  next_transition: { at: ISO-8601 with offset, period: night | day } | null
  time_source: calculated_from_schedule
  timezone: Home Assistant configured time zone

temporary_override_until: ISO-8601 with offset | null
temporary_override_until_source: calculated_from_schedule
```

No raw payload or unknown bytes enter the frontend contract. Derived values
are suppressed unless the full cache is ready. Adjacent identical periods do
not create transitions. Scanning real UTC instants against the local schedule
handles midnight, Sunday/Monday and the skipped/repeated hour in Europe/Warsaw.
A uniform week has no next transition. The override deadline is calculated,
not independently reported or hardware-confirmed. The DST model must still be
compared with the actual firmware/app; unknown device-clock drift cannot be
corrected from these reports. Hold has no calculated end.

## Integration boundary and validation

`euroster_schedule.py` owns decoding, the week cache, response correlation and
time calculations. `euroster.py` adapts the existing climate through a factory;
other device profiles retain the normal climate class. The current stage adds
no DP110 command, preset change or initialization write. Existing program
number writes from main are unchanged in this stage; their verified-write
replacement belongs to the following PR.

Run the root AGENTS.md checks and `uv run pytest`. Tests cover the supplied
captures, noon slot evidence, invalid responses, sequential/concurrent IO,
timeouts, cancellation, inconsistent globals, stale/disconnected caches,
midnight/week boundaries, both DST changes and actual platform/service wiring.
No tests connect to hardware.

PR preparation does not install the integration, restart HA or authorize
configuration writes. After separately approved installation, validate a
read-only week against the official app before relying on the derived deadline.
Schedule writes and app/DST behavior still require hardware validation.
