"""Euroster 4040 schedule codec and serialized, response-driven week reader.

Only DP104 frames delivered by on_receive can satisfy a read. The generic
Tuya pending-write overlay is deliberately never used as evidence.
"""

import asyncio
from base64 import b64decode, b64encode
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from time import monotonic
from zoneinfo import ZoneInfo

PERIODS = {1: "night", 2: "day"}
MAX_AGE = 3600


def validate_periods(periods):
    if (
        not isinstance(periods, (list, tuple))
        or len(periods) != 48
        or any(p not in ("day", "night") for p in periods)
    ):
        raise ValueError("Exactly 48 day/night periods are required")


def temperature_bytes(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation as err:
        raise ValueError("Invalid program temperature") from err
    if (
        not number.is_finite()
        or not Decimal(5) <= number <= Decimal(35)
        or number * 10 != (number * 10).to_integral_value()
    ):
        raise ValueError("Temperature must be 5..35 in steps of 0.1")
    return bytes(divmod(int(number * 10), 10))


@dataclass(frozen=True)
class ScheduleDay:
    """One validated report. Unknown trailing bytes remain private and intact."""

    payload: bytes

    @classmethod
    def decode(cls, value: str):
        """Decode strict Tuya RAW/base64 transport, never a hex string."""
        if not isinstance(value, str):
            raise ValueError("Schedule report must be base64 text")
        try:
            raw = b64decode(value, validate=True)
        except (ValueError, TypeError) as err:
            raise ValueError("Invalid schedule base64") from err
        if len(raw) != 19 or not 1 <= raw[0] <= 7:
            raise ValueError("Invalid schedule length or weekday")
        if any(
            (byte >> shift) & 3 not in PERIODS
            for byte in raw[1:13]
            for shift in (6, 4, 2, 0)
        ):
            raise ValueError("Unsupported schedule period")
        for index in (13, 15):
            whole, fraction = raw[index : index + 2]
            if fraction > 9 or not 50 <= whole * 10 + fraction <= 350:
                raise ValueError("Invalid schedule temperature")
        return cls(raw)

    @property
    def day(self):
        return self.payload[0]

    @property
    def periods(self):
        return tuple(
            PERIODS[(byte >> shift) & 3]
            for byte in self.payload[1:13]
            for shift in (6, 4, 2, 0)
        )

    @property
    def night_temperature(self):
        return self.payload[13] + self.payload[14] / 10

    @property
    def day_temperature(self):
        return self.payload[15] + self.payload[16] / 10

    def edited(self, *, periods=None, day_temperature=None, night_temperature=None):
        """Patch known fields only; preserve the weekday and unknown bytes."""
        raw = bytearray(self.payload)
        if periods is not None:
            validate_periods(periods)
            raw[1:13] = bytes(
                sum(
                    (1 if period == "night" else 2) << (6 - 2 * j)
                    for j, period in enumerate(periods[i : i + 4])
                )
                for i in range(0, 48, 4)
            )
        for index, value in ((13, night_temperature), (15, day_temperature)):
            if value is not None:
                raw[index : index + 2] = temperature_bytes(value)
        return ScheduleDay.decode(b64encode(raw).decode())

    def encode(self):
        """Return validated RAW/base64 suitable for DP110."""
        encoded = b64encode(self.payload).decode()
        ScheduleDay.decode(encoded)
        return encoded


def program_at(days, now):
    """Period at an aware local time; ISO weekdays match the device."""
    if now.utcoffset() is None:
        raise ValueError("An aware time is required")
    return days[now.isoweekday()].periods[now.hour * 2 + now.minute // 30]


def next_transition(days, now):
    """Next actual period change, scanning real UTC instants across DST.

    Nonexistent local slots are skipped and repeated slots are visited twice.
    This is a calculation, not a device-reported end time.
    """
    if set(days) != set(range(1, 8)):
        return None
    current = program_at(days, now)
    timestamp = now.astimezone(UTC).timestamp()
    boundary = datetime.fromtimestamp((timestamp // 1800 + 1) * 1800, UTC)
    for offset in range(8 * 48 + 4):
        instant = (boundary + timedelta(minutes=30 * offset)).astimezone(now.tzinfo)
        period = program_at(days, instant)
        if period != current:
            return {"at": instant.isoformat(), "period": period}
    return None


class ScheduleReader:
    """One week cache and one request/response lane per thermostat."""

    def __init__(self, send, connected, *, timeout=8, attempts=2, max_age=MAX_AGE):
        self._send = send
        self._connected = connected
        self.timeout = timeout
        self.attempts = attempts
        self.max_age = max_age
        self.lock = asyncio.Lock()
        self._waiting = None
        self._requested_day = None
        self._days = {}
        self._reports = {}
        self._synced_at = None
        self._synced_mono = None
        self._invalidated = False
        self._refreshing = False
        self._error = None
        self.last_write = None

    def invalidate(self, message):
        """Retain evidence for display, but suppress all derived deadlines."""
        self._invalidated = True
        self._error = message

    def receive(self, dps):
        """Consume real device frames, independent of cached/anticipated DPS."""
        if "104" not in dps:
            return
        try:
            report = ScheduleDay.decode(dps["104"])
        except ValueError as err:
            self.invalidate("invalid_report")
            if self._waiting and not self._waiting.done():
                self._waiting.set_exception(err)
            return
        self._reports[report.day] = report
        if self._days.get(report.day) not in (None, report):
            self.invalidate("schedule_changed")
        if (
            report.day == self._requested_day
            and self._waiting
            and not self._waiting.done()
        ):
            self._waiting.set_result(report)

    async def read_day(self, day):
        """Caller owns lock; send one integer DP102 request at a time."""
        if type(day) is not int or not 1 <= day <= 7:
            raise ValueError("Weekday must be 1..7")
        for attempt in range(self.attempts):
            if not self._connected():
                raise ConnectionError("Thermostat unavailable")
            self._waiting = asyncio.get_running_loop().create_future()
            self._requested_day = day
            try:
                async with asyncio.timeout(self.timeout):
                    await self._send("102", day)
                    # Shield is unnecessary: timeout should cancel this request.
                    return await self._waiting
            except TimeoutError, ValueError:
                if attempt + 1 == self.attempts:
                    raise
                await asyncio.sleep(0.25)
            finally:
                if not self._waiting.done():
                    self._waiting.cancel()
                # Retrieve exceptions if the send itself failed after a frame.
                if not self._waiting.cancelled():
                    self._waiting.exception()
                self._waiting = None
                self._requested_day = None

    async def refresh(self):
        """Commit a complete coherent week, never a mixture of partial reads."""
        async with self.lock:
            await self._refresh_locked()

    async def _refresh_locked(self):
        """Caller owns lock; never reuse an old per-day cache as verification."""
        self._refreshing = True
        self.invalidate("refresh_in_progress")
        try:
            days = {}
            for day in range(1, 8):
                days[day] = await self.read_day(day)
            if any(self._reports.get(day) != report for day, report in days.items()):
                raise ValueError("Schedule changed during refresh")
            if len({d.payload[13:17] for d in days.values()}) != 1:
                raise ValueError("Inconsistent global schedule temperatures")
            self._days = days
            self._synced_at = datetime.now(UTC)
            self._synced_mono = monotonic()
            self._invalidated = False
            self._error = None
        except BaseException:
            self.invalidate("refresh_failed")
            raise
        finally:
            self._refreshing = False

    async def _write_day(self, expected):
        """One DP110, device DP104 acknowledgment, then explicit fresh readback."""
        self._requested_day = expected.day
        self._waiting = asyncio.get_running_loop().create_future()
        try:
            async with asyncio.timeout(self.timeout):
                await self._send("110", expected.encode())
                acknowledgment = await self._waiting
            if acknowledgment != expected:
                raise ValueError("Device did not acknowledge the expected schedule")
        finally:
            if not self._waiting.done():
                self._waiting.cancel()
            if not self._waiting.cancelled():
                self._waiting.exception()
            self._waiting = None
            self._requested_day = None
        reported = await self.read_day(expected.day)
        if reported != expected:
            raise ValueError("Schedule readback does not match requested configuration")
        self.last_write["confirmed_days"].append(expected.day)

    async def _apply(self, operation, prepare):
        """Serialize fresh read/patch/write/verify with explicit partial outcomes."""
        async with self.lock:
            self.last_write = {
                "operation": operation,
                "status": "pending",
                "confirmed_days": [],
                "at": datetime.now(UTC).isoformat(),
            }
            self.invalidate("write_in_progress")
            try:
                expected = await prepare()
                # Always refresh the entire week before publishing a success.
                await self._refresh_locked()
                if any(self._days[day] != report for day, report in expected.items()):
                    raise ValueError("Schedule changed during verification")
                self.last_write["status"] = "verified"
            except BaseException:
                self.last_write["status"] = "unconfirmed"
                self.invalidate("write_failed")
                raise

    async def set_day(self, day, periods, expected_periods):
        """Save a user's edit only if its displayed baseline is still current."""
        # Validate before sending even a read query.
        validate_periods(periods)
        validate_periods(expected_periods)

        async def prepare():
            current = await self.read_day(day)
            if current.periods != tuple(expected_periods):
                raise ValueError(
                    "Schedule changed since editing; refresh before saving"
                )
            expected = current.edited(periods=periods)
            if expected != current:
                await self._write_day(expected)
            return {day: expected}

        await self._apply("set_day", prepare)

    async def set_temperatures(self, *, day_temperature=None, night_temperature=None):
        """One full DP110; verify globals across all seven days, never fan out writes."""
        if day_temperature is None and night_temperature is None:
            raise ValueError("At least one program temperature is required")

        for value in (day_temperature, night_temperature):
            if value is not None:
                temperature_bytes(value)

        async def prepare():
            current = await self.read_day(1)
            expected = current.edited(
                day_temperature=day_temperature, night_temperature=night_temperature
            )
            if expected != current:
                await self._write_day(expected)
            return {1: expected}

        await self._apply("set_temperatures", prepare)

    async def copy_days(self, source_day, target_days):
        """Copy slots only, preserving every destination's temperatures/tail."""
        if (
            type(source_day) is not int
            or not 1 <= source_day <= 7
            or not isinstance(target_days, (list, tuple))
            or not target_days
            or any(
                type(day) is not int or not 1 <= day <= 7 or day == source_day
                for day in target_days
            )
            or len(set(target_days)) != len(target_days)
        ):
            raise ValueError("Select unique target weekdays different from source")

        async def prepare():
            source = await self.read_day(source_day)
            expected = {}
            for day in target_days:
                current = await self.read_day(day)
                expected[day] = current.edited(periods=source.periods)
                if expected[day] != current:
                    await self._write_day(expected[day])
            return expected

        await self._apply("copy_days", prepare)

    def snapshot(self, now, timezone):
        """Sanitized frontend contract. No raw frames or unknown bytes."""
        if not self._connected():
            self.invalidate("device_unavailable")
            status = "unavailable"
        elif self._refreshing:
            status = "refreshing"
        elif not self._days:
            status = "not_loaded"
        elif (
            self._invalidated
            or self._synced_mono is None
            or monotonic() - self._synced_mono >= self.max_age
        ):
            status = "stale"
        else:
            status = "ready"
        result = {
            "schema_version": 1,
            "last_write": {
                **self.last_write,
                "confirmed_days": list(self.last_write["confirmed_days"]),
            }
            if self.last_write
            else None,
            "status": status,
            "source": "device_dp104",
            "last_synced_at": self._synced_at.isoformat() if self._synced_at else None,
            "max_age_seconds": self.max_age,
            "error": self._error,
            "days": [
                {"day": day, "periods": list(report.periods)}
                for day, report in sorted(self._days.items())
            ],
            "day_temperature": next(iter(self._days.values())).day_temperature
            if self._days
            else None,
            "night_temperature": next(iter(self._days.values())).night_temperature
            if self._days
            else None,
            "current_period": None,
            "next_transition": None,
            "time_source": "calculated_from_schedule",
            "timezone": timezone,
        }
        if status == "ready":
            local = now.astimezone(ZoneInfo(timezone))
            result["current_period"] = program_at(self._days, local)
            result["next_transition"] = next_transition(self._days, local)
        return result


def schedule_for_device(device):
    """Create one lane per physical device, regardless of platform setup order."""
    reader = getattr(device, "_euroster_schedule_reader", None)
    if not isinstance(reader, ScheduleReader):
        reader = ScheduleReader(
            device.async_set_property, lambda: device.has_returned_state
        )
        device._euroster_schedule_reader = reader
    return reader
