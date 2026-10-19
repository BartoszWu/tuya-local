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
After the climate entity is added and the device first returns state, one
background refresh reads the seven days. If the initial status lacks DP103,
it first sends DP101=True (the existing Update sensors action) once and waits
up to 8 seconds for a real DP103 report. An acknowledgment alone does not count
as sensor data. If that report times out, the week is still read and subsequent
reports do not cause a retry storm. When DP103 is already cached, no DP101 is sent.
This fills the missing current temperature, active target and preset after a
restart without changing their values. The background read does not block HA startup or send
DP110, a temperature or a preset command. Removing the entity cancels the task
and its outstanding waiter. After a failed batch, use the explicit refresh
action; subsequent reports do not restart it in a loop. A complete cache is
considered stale after one hour. Refresh explicitly when revisiting the schedule;
there is no periodic poller.

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
no automatic DP110, temperature or preset write. Program number writes use
the verified-write operations below.

Run the root AGENTS.md checks and `uv run pytest`. Tests cover the supplied
captures, noon slot evidence, invalid responses, sequential/concurrent IO,
timeouts, cancellation, inconsistent globals, stale/disconnected caches,
midnight/week boundaries, both DST changes and actual platform/service wiring.
No tests connect to hardware.

PR preparation does not install the integration, restart HA or authorize
configuration writes. After separately approved installation, validate a
read-only week against the official app before relying on the derived deadline.
Schedule writes and app/DST behavior still require hardware validation.

## Verified-write stage (stacked after the read-only PR)

The following entity-targeted actions are available only for this profile:

| Action | Inputs | Behavior |
| --- | --- | --- |
| `tuya_local.set_schedule` | `day`, 48 `periods`, 48 `expected_periods` | Fresh-read that day; reject if the displayed baseline changed; edit slots only. |
| `tuya_local.copy_schedule` | `source_day`, distinct `target_days` | Fresh-read source and each destination; copy only slots, preserving destination global temperatures and unknown bytes. |
| `tuya_local.set_program_temperatures` | optional `day_temperature`, `night_temperature` (at least one) | Fresh-read Monday; send at most one full DP110; verify that all seven days report the requested global levels. |

Periods use `day` / `night`, in local half-hour order, with exactly 48 values.
Temperatures must be finite 5..35°C in exact 0.1°C steps. Invalid requests fail
before configuration writes. Existing day/night number entity IDs also remain
usable through `number.set_value`: their factory now routes writes to the same
reader and operation lock as the climate actions, regardless of setup order.
Other number profiles keep their original implementation.

Each actual change follows:

```text
fresh DP102 → DP104
  → patch only intended fields, keep weekday and unknown tail
  → one DP110
  → wait for matching real DP104 acknowledgment
  → explicit DP102 → compare complete DP104 bytes
  → read complete week again → validate coherence and expected changes
  → publish verified outcome
```

The application layer never retries DP110 or silently restores old data. The
existing lower-level Tuya transport retains its connection retry behavior.
Acknowledgment alone is insufficient; an API send return or anticipated DPS
is not evidence. DP102 reads may retry within the bounded read policy. If a
firmware does not return a DP104 acknowledgment after DP110, this conservative
path times out rather than claiming success, even if the device changed.

There is no seven-write fallback for global levels. If the other six reports
do not prove global scope, the action fails and must be reviewed on hardware.
Copying is explicitly per destination and is not atomic across multiple days.
If a later destination fails, earlier confirmed changes remain; there is no
automatic rollback. `weekly_schedule.last_write` contains:

```text
operation: set_day | copy_days | set_temperatures
status: pending | verified | unconfirmed
confirmed_days: [weekday numbers with a matching explicit readback]
at: ISO-8601 UTC
```

An unconfirmed outcome invalidates the cache and suppresses calculated
transitions. `confirmed_days` is evidence about those readbacks, not a guarantee
that a later app edit has not changed them. Refresh and inspect the thermostat
before retrying. A no-op performs verification but sends no DP110.

The frontend must show the scope and summary before the user submits, retain
`expected_periods` from the displayed day, handle service errors, and render
only observed results. It must not encode Tuya bytes. These APIs prepare the
backend contract; dashboard PR #64 does not yet expose the week editor.

Tests add encode/decode identity on all provided captures, the captured noon
slot edit, fresh-read unknown-byte preservation, stale edit conflicts, one-write
global scope, mismatched/missing acknowledgments, readback mismatch, no-op,
partial copy, cancellation, shared climate/number locking and service dispatch.
All transport scenarios are synthetic; this code has not been installed or
used to write a real thermostat. Read-only hardware comparison precedes any
separately approved live-write test, per the requested staged rollout.

## Fork releases and installation consistency

A merged PR and an installed HACS release are separate states. The old fork
workflow reused the latest upstream tag and skipped publication whenever that
release already existed. Consequently a successful workflow could leave HACS
installing code from before the program-temperature and weekly-schedule PRs.

The fork now publishes `YYYY.M.patch.N`, where `N` is the number of commits
reachable from the release commit but not from the included upstream release.
For a fixed upstream base, later descendant commits increase `N`; a new upstream
version starts a new base. Existing upstream/fork tags remain unchanged. Only
the current `main` tip is published, and reruns leave an existing release intact.

HACS downloads the release asset `tuya_local.zip`. Its root contains the
integration files (including the Euroster adapter, reader, services and profile).
The archive is built from tracked Git objects at the release SHA, not from the
working directory. Its manifest has the fork version; the source manifest keeps
the upstream version for future merges. `fork_release.json` records the source
commit, upstream tag and installed-file SHA256 checksums. Rebuilding the same
source produces identical bytes. No private runtime files are packaged.

After merging this PR, check that Release fork publishes the new asset, then
separately approve/install that release in HACS and restart HA. Check the installed
manifest and `fork_release.json`, the existing day/night entities, `heat` mode,
and a ready seven-day `weekly_schedule`. This PR does not perform installation,
restart, thermostat writes or physical validation. Keep the pre-installation
runtime backup for rollback. Legacy releases without the ZIP asset are not
backfilled; restoring an older deployment can require that backup or its source
archive rather than selecting an old assetless release with ZIP mode enabled.

HACS documents [release ZIP metadata](https://www.hacs.dev/docs/publish/start/)
and [release selection](https://www.hacs.dev/docs/publish/integration/).
