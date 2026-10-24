"""Persistent per-home/per-room Tado schedule profiles and live comparison."""
from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.event import async_track_time_interval

_LOGGER = logging.getLogger(__name__)

DEFAULT_PROFILES = ("Normal", "Alternative", "Eco", "Not in use")
DEFAULT_SUFFIX = " default"


def default_name(profile):
    return f"{profile}{DEFAULT_SUFFIX}"


def base_default_name(name):
    base = name[:-len(DEFAULT_SUFFIX)] if name.endswith(DEFAULT_SUFFIX) else None
    return base if base in DEFAULT_PROFILES else None

DAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")

# Virtual defaults: applied only where a room has no saved version of a profile.
# A room-specific saved profile always takes precedence.
NORMAL_DAY = (("00:00", "07:30", 18), ("07:30", "22:00", 20), ("22:00", "24:00", 18))
ALTERNATIVE_WEEKDAY = (("00:00", "06:30", 18), ("06:30", "08:30", 20),
                       ("08:30", "16:30", 18), ("16:30", "22:00", 20),
                       ("22:00", "24:00", 18))
ECO_DAY = (("00:00", "07:30", 17), ("07:30", "22:00", 18), ("22:00", "24:00", 17))
NOT_IN_USE_DAY = (("00:00", "24:00", 14),)


def default_snapshot(profile: str) -> dict:
    """Build a fresh Home/Away profile for a room with no stored profile."""
    schedule = []
    for day in DAYS:
        if profile == "Eco":
            periods = ECO_DAY
        elif profile == "Not in use":
            periods = NOT_IN_USE_DAY
        elif profile == "Alternative" and day not in ("SATURDAY", "SUNDAY"):
            periods = ALTERNATIVE_WEEKDAY
        else:
            periods = NORMAL_DAY
        for start, end, temperature in periods:
            schedule.append({
                "dayType": day, "start": start, "end": end,
                "geolocationOverride": False,
                "setting": {"power": "ON", "temperature": {"value": temperature}},
            })
    return {
        "schedule": schedule,
        "away": {"mode": "ON", "awayTemperatureCelsius": 14.0,
                 "preheatingLevel": "BALANCE"},
    }



def canonical(blocks: list[dict]) -> tuple:
    """Compare exact time boundaries, power and temperatures, not JSON formatting."""
    if not isinstance(blocks, list):
        raise HomeAssistantError("Schedule must be a list")
    result = []
    for b in blocks:
        try:
            day = b["dayType"]
            start, end = b["start"], b["end"]
            setting = b["setting"]
            power = setting["power"]
            temp = setting.get("temperature")
            value = None if temp is None else str(Decimal(str(temp["value"])).normalize())
            result.append((day, start, end, power, value))
        except (KeyError, TypeError, ValueError, ArithmeticError) as err:
            raise HomeAssistantError(f"Invalid schedule block: {err}") from err
    return tuple(sorted(result, key=lambda b: (DAYS.index(b[0]) if b[0] in DAYS else 99, b[1], b[2])))


def validate_week(blocks, validate_day):
    if not isinstance(blocks, list):
        raise HomeAssistantError("Schedule must contain a list of blocks")
    for day in DAYS:
        day_blocks = [b for b in blocks if isinstance(b, dict) and b.get("dayType") == day]
        if not day_blocks:
            raise HomeAssistantError(f"Missing schedule for {day}")
        validate_day(day, sorted(day_blocks, key=lambda b: b.get("start", "")))
    if len(blocks) != sum(1 for b in blocks if isinstance(b, dict) and b.get("dayType") in DAYS):
        raise HomeAssistantError("Unexpected dayType in schedule")


class ProfileManager:
    def __init__(self, hass, coordinator, validate_day):
        self.hass = hass
        self.coordinator = coordinator
        self.validate_day = validate_day
        self.store = Store(hass, 1, f"tado_x_schedule_profiles_{coordinator.home_id}")
        self.profiles: dict[str, dict[str, dict]] = {}
        self.home_defaults: dict[str, dict] = {}
        self._default_lock = asyncio.Lock()
        self.matches: dict[str, list[str]] = {}
        self.status: dict[str, str] = {}
        self.last_applied: dict[str, str] = {}
        self.listeners = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._unsubscribe = None
        self.poll_enabled = False
        self.poll_interval = 3600
        self._snapshots: dict[str, dict] = {}

    async def load(self):
        data = await self.store.async_load() or {}
        self.profiles = data.get("profiles", {})
        self.home_defaults = data.get("home_defaults", {})
        self.last_applied = data.get("last_applied", {})

    async def initialize_normal_profiles(self):
        """Persist each room's current Home/Away settings as Normal once.

        Run during config-entry setup before room selectors are created. Existing
        Normal snapshots are never replaced, including when other profiles exist.
        A failed read leaves the room untouched so the next startup can retry.
        """
        rooms = tuple(self.coordinator.data.rooms) if self.coordinator.data else ()
        semaphore = asyncio.Semaphore(4)

        async def capture(room_id):
            key = str(room_id)
            async with semaphore:
                async with self._lock(room_id):
                    if "Normal" in self.profiles.get(key, {}):
                        return
                    try:
                        snapshot = await self._snapshot(room_id)
                        validate_week(snapshot["schedule"], self.validate_day)
                        snapshot["away"] = self.coordinator.api._normalize_away_configuration(
                            snapshot["away"], room_id
                        )
                        self.profiles.setdefault(key, {})["Normal"] = snapshot
                        try:
                            await self.persist()
                        except Exception:
                            # Do not claim Normal was saved if disk persistence failed.
                            del self.profiles[key]["Normal"]
                            if not self.profiles[key]:
                                del self.profiles[key]
                            raise
                        _LOGGER.debug("Saved initial Normal profile for home %s room %s",
                                      self.coordinator.home_id, room_id)
                    except Exception as err:
                        _LOGGER.warning(
                            "Could not save initial Normal profile for home %s room %s: %s",
                            self.coordinator.home_id, room_id, err,
                        )

        await asyncio.gather(*(capture(room_id) for room_id in rooms))

    def effective_profiles(self, room_id):
        """Saved profiles override built-in defaults; no automatic disk writes."""
        saved = self.profiles.get(str(room_id), {})
        return {name: saved[name] if name in saved else self.home_default(name)
                for name in self.names(room_id)}

    def home_default(self, profile):
        """Return the per-home template, falling back to the factory version."""
        return deepcopy(self.home_defaults.get(profile, default_snapshot(profile)))

    def selector_names(self, room_id):
        """Operational profiles plus explicit home-default commands."""
        return self.names(room_id) + [default_name(p) for p in DEFAULT_PROFILES]

    async def save_home_default(self, room_id, profile):
        """Save this room's live Home/Away settings as a home-wide template."""
        if profile not in DEFAULT_PROFILES:
            raise HomeAssistantError(f"Unknown home default: {profile}")
        async with self._default_lock:
            async with self._lock(room_id):
                snapshot = await self._snapshot(room_id)
                validate_week(snapshot["schedule"], self.validate_day)
                snapshot["away"] = self.coordinator.api._normalize_away_configuration(
                    snapshot["away"], room_id
                )
                old = self.home_defaults.get(profile)
                self.home_defaults[profile] = deepcopy(snapshot)
                try:
                    await self.persist()
                except Exception:
                    if old is None:
                        self.home_defaults.pop(profile, None)
                    else:
                        self.home_defaults[profile] = old
                    raise
                # Re-evaluate known snapshots without new API requests.
                await self.recheck_home()

    async def reset_home_default(self, profile):
        """Discard a customised template, revealing its built-in factory version."""
        if profile not in DEFAULT_PROFILES:
            raise HomeAssistantError(f"Unknown home default: {profile}")
        async with self._default_lock:
            old = self.home_defaults.pop(profile, None)
            try:
                await self.persist()
            except Exception:
                if old is not None:
                    self.home_defaults[profile] = old
                raise
            await self.recheck_home()

    async def recheck_home(self):
        for room_id in tuple(self.coordinator.data.rooms) if self.coordinator.data else ():
            try:
                snapshot = self._snapshots.get(str(room_id))
                if snapshot is not None:
                    self._update_match(room_id, snapshot)
            except Exception as err:
                _LOGGER.debug("Cannot recheck schedule for room %s: %s", room_id, err)
        self.notify()

    def names(self, room_id):
        saved = self.profiles.get(str(room_id), {})
        return list(DEFAULT_PROFILES) + sorted(n for n in saved if n not in DEFAULT_PROFILES)

    def notify(self):
        for callback in tuple(self.listeners):
            callback()

    def listen(self, callback):
        self.listeners.add(callback)
        return lambda: self.listeners.discard(callback)

    def _lock(self, room_id):
        return self._locks.setdefault(str(room_id), asyncio.Lock())

    async def persist(self):
        await self.store.async_save({"profiles": self.profiles, "last_applied": self.last_applied,
                                     "home_defaults": self.home_defaults})

    async def _snapshot(self, room_id, home=None):
        """Read both Home and Away settings, or fail closed."""
        if home is None:
            home = await self.coordinator.api.get_zone_schedule(room_id)
        if not isinstance(home, dict) or not isinstance(home.get("schedule"), list):
            raise HomeAssistantError("Tado did not return a Home schedule")
        away = await self.coordinator.api.get_zone_away_configuration(room_id)
        snapshot = {"schedule": deepcopy(home["schedule"]), "away": deepcopy(away)}
        self._snapshots[str(room_id)] = deepcopy(snapshot)
        return snapshot

    @staticmethod
    def _equal(left, right):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        try:
            return (canonical(left["schedule"]) == canonical(right["schedule"])
                    and left["away"] == right["away"])
        except (KeyError, TypeError, ValueError, HomeAssistantError):
            return False

    def _update_match(self, room_id, snapshot):
        key = str(room_id)
        matches = [name for name, saved in self.effective_profiles(room_id).items() if self._equal(saved, snapshot)]
        if not self.profiles.get(key):
            matches = ["Normal"]
        self.matches[key] = matches
        self.status[key] = "Matched" if matches else "Custom"
        self.notify()
        return matches

    async def check(self, room_id, live=None):
        key = str(room_id)
        try:
            snapshot = await self._snapshot(room_id, live)
            return self._update_match(room_id, snapshot)
        except Exception:
            self.status[key] = "Unavailable"
            self.notify()
            raise

    def selected(self, room_id):
        key = str(room_id)
        if not self.profiles.get(key):
            return "Normal"
        if self.status.get(key) != "Matched":
            return "Custom"
        matches = self.matches.get(key, [])
        previous = self.last_applied.get(key)
        return previous if previous in matches else next((x for x in self.names(room_id) if x in matches), "Custom")

    async def save(self, room_id, profile):
        key = str(room_id)
        async with self._lock(room_id):
            snapshot = await self._snapshot(room_id)
            validate_week(snapshot["schedule"], self.validate_day)
            self.profiles.setdefault(key, {})[profile] = snapshot
            # The saved snapshot now matches the live settings; identify it as
            # the operational profile in both room selectors. No implicit Normal
            # backup is created by this operation.
            self.last_applied[key] = profile
            await self.persist()
            self._update_match(room_id, snapshot)

    async def restore(self, room_id, profile):
        default_profile = base_default_name(profile)
        key = str(room_id)
        async with self._lock(room_id):
            available = self.effective_profiles(room_id)
            if default_profile is None and profile not in available:
                raise HomeAssistantError(f"Unknown profile '{profile}' for room {room_id}")
            # Preserve the real live Home/Away settings as Normal before
            # applying another profile. Never overwrite a saved Normal.
            if "Normal" not in self.profiles.get(key, {}):
                current_normal = await self._snapshot(room_id)
                validate_week(current_normal["schedule"], self.validate_day)
                self.coordinator.api._normalize_away_configuration(
                    current_normal["away"], room_id
                )
                self.profiles.setdefault(key, {})["Normal"] = deepcopy(current_normal)
                await self.persist()
                if profile == "Normal":
                    self.last_applied[key] = "Normal"
                    await self.persist()
                    self._update_match(room_id, current_normal)
                    return
                available = self.effective_profiles(room_id)
            snapshot = (self.home_default(default_profile) if default_profile is not None
                        else deepcopy(available[profile]))
            if not isinstance(snapshot, dict) or "away" not in snapshot:
                raise HomeAssistantError(
                    "This profile predates Away-setting backups. Re-save it from Tado before restoring."
                )
            blocks = snapshot["schedule"]
            validate_week(blocks, self.validate_day)
            # Reject malformed or legacy Away snapshots before any Home schedule writes.
            snapshot_away = self.coordinator.api._normalize_away_configuration(snapshot["away"], room_id)
            snapshot = {"schedule": blocks, "away": snapshot_away}
            previous = await self._snapshot(room_id)
            validate_week(previous["schedule"], self.validate_day)
            changed = []
            try:
                for day in DAYS:
                    desired = sorted((b for b in blocks if b["dayType"] == day), key=lambda b: b["start"])
                    current = [b for b in previous["schedule"] if b["dayType"] == day]
                    if canonical(desired) == canonical(current):
                        continue
                    await self.coordinator.api.set_zone_schedule(room_id, day, desired)
                    changed.append(day)
                if snapshot["away"] != previous["away"]:
                    await self.coordinator.api.set_zone_away_configuration(room_id, snapshot["away"])
            except Exception as err:
                # Best effort rollback of days already changed; never conceal failures.
                rollback_errors = []
                for day in reversed(changed):
                    try:
                        original = [b for b in previous["schedule"] if b["dayType"] == day]
                        await self.coordinator.api.set_zone_schedule(room_id, day, original)
                    except Exception as rollback_err:
                        rollback_errors.append(f"{day}: {rollback_err}")
                try:
                    await self.coordinator.api.set_zone_away_configuration(room_id, previous["away"])
                except Exception as rollback_err:
                    rollback_errors.append(f"AWAY: {rollback_err}")
                self.status[key] = "Unavailable"
                self.notify()
                raise HomeAssistantError(f"Restore failed after {changed}: {err}. Rollback errors: {rollback_errors}") from err
            try:
                verified = await self._snapshot(room_id)
                matches = self._update_match(room_id, verified)
            except Exception as err:
                raise HomeAssistantError(f"Schedule uploaded but verification failed: {err}") from err
            if default_profile is None and profile not in matches:
                raise HomeAssistantError("Schedule uploaded but read-back differs from saved profile")
            # A direct default restore may differ from a room-specific override.
            # Do not expose the command as an operational profile.
            if default_profile is not None:
                if not self._equal(snapshot, verified):
                    raise HomeAssistantError("Default schedule uploaded but read-back differs")
            self.last_applied[key] = (profile if default_profile is None else
                                      (default_profile if default_profile in matches else "Custom"))
            await self.persist()
            self.notify()
            # The coordinator's room-state payload contains nextScheduleChange.
            # Refresh it now rather than waiting for the next polling interval.
            # This updates all room sensors and climate attributes together.
            try:
                await self.coordinator.async_request_refresh()
            except Exception as err:
                _LOGGER.warning(
                    "Profile %s restored for home %s room %s, but immediate "
                    "room-state refresh failed: %s",
                    profile, self.coordinator.home_id, room_id, err,
                )

    async def delete(self, room_id, profile):
        if profile in DEFAULT_PROFILES:
            raise HomeAssistantError("Default profile names cannot be deleted")
        key = str(room_id)
        if profile not in self.profiles.get(key, {}):
            raise HomeAssistantError("Profile does not exist")
        del self.profiles[key][profile]
        await self.persist()
        snapshot = self._snapshots.get(key)
        if snapshot is not None:
            self._update_match(room_id, snapshot)

    async def poll(self, _now=None):
        # Two requests per room per check (Home and Away); failures must not become Custom.
        if not self.poll_enabled:
            return
        for room_id in tuple(self.coordinator.data.rooms) if self.coordinator.data else ():
            try:
                async with self._lock(room_id):
                    await self.check(room_id)
            except Exception:
                pass

    def configure_polling(self, enabled: bool, interval: int):
        """Apply per-home polling options, including changes without restart."""
        interval = max(900, min(86400, int(interval)))
        self.stop()
        self.poll_enabled = bool(enabled)
        self.poll_interval = interval
        if self.poll_enabled:
            self._unsubscribe = async_track_time_interval(
                self.hass, self.poll, timedelta(seconds=interval)
            )

    def start(self):
        self.configure_polling(self.poll_enabled, self.poll_interval)

    def stop(self):
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
