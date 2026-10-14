"""The Alpaca telescope device: client connections, the mount state, and motion.

Connections are tracked per ClientID, so one program disconnecting (for example PHD2)
does not cut off another (for example NINA). The link to the mount is open while at
least one client is connected or connecting.

The safety rules for motion are in docs/safety-review.md (section 3); the comments
below refer to them as "review 3.x".
"""

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime

from alpaca_10micron.config import DeviceConfig
from alpaca_10micron.errors import (
    DRIVER_ERROR,
    INVALID_OPERATION,
    INVALID_VALUE,
    INVALID_WHILE_PARKED,
    AlpacaError,
    not_connected,
    not_implemented,
)
from alpaca_10micron.models import (
    AlignmentModes,
    DriveRates,
    GuideDirections,
    PierSide,
    TelescopeAxes,
)
from alpaca_10micron.mount import Mount, MountLinkError, MountSession, MountState, SiteState
from alpaca_10micron.protocol import (
    PIER_SIDE_FIXED_FIRMWARE,
    SLEW_OK,
    SLEW_PARKED,
    Ginfo,
    MountStatus,
    ProtocolError,
    Reply,
    datetime_to_julian_date,
    format_azimuth,
    format_declination,
    format_elevation,
    format_julian_date,
    format_longitude,
    format_right_ascension,
    format_sidereal_multiple,
    julian_date_to_datetime,
    parse_flag,
    parse_float,
    parse_slew_reply,
)


def format_utc(moment: datetime) -> str:
    """ISO 8601 in UTC with a 'Z' suffix, as Alpaca uses for UTCDate."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


logger = logging.getLogger(__name__)

# Cached values older than this are not returned. The poller normally refreshes them
# twice a second; they get old only while the link is failing.
MAX_STATE_AGE = 5.0
MAX_SITE_AGE = 60.0

# Review 3.12: mount states in which the driver refuses to move the mount.
UNSAFE_STATUSES = {
    MountStatus.MOTORS_INHIBITED: "the motors are inhibited (low temperature or pre-heating)",
    MountStatus.NEEDS_USER_OK: (
        "the mount suspects inconsistent data and waits for the user to allow movement "
        "(keypad or :USEROK#); its position may be wrong"
    ),
    MountStatus.DDS_WAITING_FOR_POWER: "the controller waits for the power to be enabled",
    MountStatus.DDS_MONITOR: "the controller is in monitor mode",
    MountStatus.DDS_AUTOTUNE: "the controller is in autotune mode",
    MountStatus.ERROR: "the mount reports an error",
}
# Statuses in which the mount moves on its own (slew, park, unpark, homing).
MOVING_STATUSES = {
    MountStatus.SLEWING,
    MountStatus.PARKING,
    MountStatus.UNPARKING,
    MountStatus.HOMING,
}

# Review 3.4: Slewing must be True as soon as an async slew returns. The mount's slew flag
# is read right after the command; this short window covers a mount that reacts later.
SLEW_GRACE = 1.0

SIDEREAL_RATE = 15.0410686  # arcseconds per SI second
# Target RA rates relative to sidereal, in arcsec/s. Positive: the target's RA increases,
# so the mount tracks slower than sidereal. Lunar and solar are the vendor driver's values.
# King (15.0369"/s) is 0.0041"/s slower than sidereal; the vendor driver uses -0.0041,
# which has the wrong sign compared with its own solar value (review 3.11).
DRIVE_RATE_OFFSETS = {
    DriveRates.SIDEREAL: 0.0,
    DriveRates.LUNAR: 0.549,
    DriveRates.SOLAR: 0.041,
    DriveRates.KING: 0.0041,
}
DIRECT_RATE_COMMANDS = {
    DriveRates.SIDEREAL: ":TQ#",
    DriveRates.LUNAR: ":TL#",
    DriveRates.SOLAR: ":TSOLAR#",
}
# Firmware that does not restart tracking with :AP# after :STOP# (protocol document, :AP#).
TRACKING_RESTART_BUG = ((2, 12, 26), (2, 15, 34))

PULSE_GUIDE_MAX_MS = 9999  # :MgXXXX# from firmware 2.10
GUIDE_LETTERS = {
    GuideDirections.NORTH: "n",
    GuideDirections.SOUTH: "s",
    GuideDirections.EAST: "e",
    GuideDirections.WEST: "w",
}
# MoveAxis: the lowest rate offered in AxisRates (1 arcsec/s). 0 stops the axis.
MIN_AXIS_RATE = 1 / 3600
# MoveAxis: (letter for a negative rate, letter for a positive rate), as the vendor driver.
AXIS_LETTERS = {TelescopeAxes.PRIMARY: ("e", "w"), TelescopeAxes.SECONDARY: ("s", "n")}
STOP_ALL_AXES = ":Qe#:Qw#:Qn#:Qs#"


def _expect_one(reply: str, command: str, refused: str) -> None:
    """Most set commands reply '1' (valid) or '0' (invalid)."""
    if reply == "1":
        return
    if reply == "0":
        raise AlpacaError(INVALID_VALUE, refused)
    raise AlpacaError(DRIVER_ERROR, f"Unexpected reply {reply!r} from the mount to {command}")


def _check_range(name: str, value: float, low: float, high: float, high_open=False) -> None:
    if value < low or value > high or (high_open and value == high):
        bracket = ")" if high_open else "]"
        raise AlpacaError(INVALID_VALUE, f"{name} {value} is outside [{low}, {high}{bracket}")


class TelescopeDevice:
    def __init__(self, mount: Mount, config: DeviceConfig):
        self.mount = mount
        self.config = config
        self._connected: set[int] = set()
        self._connecting: dict[int, asyncio.Task] = {}
        self._connect_errors: dict[int, str] = {}
        # TargetRightAscension/TargetDeclination per ClientID, so one program's target is
        # never used by another program's SlewToTarget or SyncToTarget.
        self._targets: dict[int, dict[str, float]] = {}
        # Motion state kept by the driver.
        self._slew_grace_until = 0.0
        self._pulse_until = 0.0
        self._moving_axes: dict[TelescopeAxes, float] = {}
        self._moving_link = 0  # links_opened when the axes started moving
        self._speed_correction_was_on: bool | None = None
        self._at_home = False
        self._home_link = 0
        self._home_task: asyncio.Task | None = None
        self._tracking_rate: DriveRates | None = None  # None: not set by a client yet
        self._ra_rate = 0.0  # seconds of RA per sidereal second (ASCOM units)
        self._dec_rate = 0.0  # arcseconds per SI second
        # Site values as the client set them, see _site_value().
        self._site_values: dict[str, float] = {}

    # --- Connection management ---

    def is_connected(self, client_id: int) -> bool:
        return client_id in self._connected

    def is_connecting(self, client_id: int) -> bool:
        """Platform 7 Connecting. A failed Connect is reported once, as an error here."""
        error = self._connect_errors.pop(client_id, None)
        if error is not None:
            raise AlpacaError(DRIVER_ERROR, error)
        return client_id in self._connecting

    def connect(self, client_id: int) -> None:
        """Platform 7 Connect: start connecting and return at once."""
        if client_id in self._connected or client_id in self._connecting:
            return
        self._connect_errors.pop(client_id, None)
        # ASCOM: a new connection starts without targets (reading them gives an error).
        self._targets.pop(client_id, None)
        if not self.mount.running:
            self._site_values.clear()
        self.mount.start()
        self._connecting[client_id] = asyncio.create_task(self._finish_connect(client_id))

    async def _finish_connect(self, client_id: int) -> None:
        try:
            if await self.mount.wait_until_up(self.config.connect_wait):
                self._connected.add(client_id)
                logger.info("Client %d connected", client_id)
            else:
                settings = self.mount.settings
                message = f"Could not connect to the mount at {settings.host}:{settings.port}"
                if self.mount.last_error:
                    message += f": {self.mount.last_error}"
                self._connect_errors[client_id] = message
                logger.warning("Client %d: %s", client_id, message)
        finally:
            self._connecting.pop(client_id, None)
            await self._stop_if_unused()

    async def set_connected(self, client_id: int, value: bool) -> None:
        """The Connected property setter: waits until the connection is made or fails."""
        if not value:
            await self.disconnect(client_id)
            return
        self.connect(client_id)
        task = self._connecting.get(client_id)
        if task is not None:
            await task
        error = self._connect_errors.pop(client_id, None)
        if error is not None:
            raise AlpacaError(DRIVER_ERROR, error)

    async def disconnect(self, client_id: int) -> None:
        task = self._connecting.pop(client_id, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._targets.pop(client_id, None)
        if client_id in self._connected:
            self._connected.discard(client_id)
            logger.info("Client %d disconnected", client_id)
        self._connect_errors.pop(client_id, None)
        await self._stop_if_unused()

    async def _stop_if_unused(self) -> None:
        if not self._connected and not self._connecting and self.mount.running:
            logger.info("No clients left; closing the link to the mount")
            await self._stop_axes_before_close()
            await self._cancel_home_watch()
            await self.mount.stop()

    async def shutdown(self) -> None:
        for client_id in list(self._connecting):
            await self.disconnect(client_id)
        self._connected.clear()
        await self._stop_axes_before_close()
        await self._cancel_home_watch()
        await self.mount.stop()

    def require_connected(self, client_id: int) -> None:
        if client_id not in self._connected:
            raise not_connected()

    # --- Mount access ---

    @contextlib.asynccontextmanager
    async def _session(self):
        """Hold the link for several exchanges. Link and format errors become ASCOM errors."""
        try:
            async with self.mount.session() as session:
                yield session
        except MountLinkError as exc:
            raise AlpacaError(DRIVER_ERROR, str(exc)) from exc
        except ProtocolError as exc:
            raise AlpacaError(DRIVER_ERROR, f"Unexpected reply from the mount: {exc}") from exc

    async def query(self, command: str, parse, reply: Reply = Reply.HASH):
        """Send one command and parse the reply. Link and format errors become ASCOM errors."""
        try:
            text = await self.mount.send(command, reply)
            return parse(text)
        except MountLinkError as exc:
            raise AlpacaError(DRIVER_ERROR, str(exc)) from exc
        except (ProtocolError, ValueError) as exc:
            raise AlpacaError(
                DRIVER_ERROR, f"Unexpected reply from the mount to {command}: {exc}"
            ) from exc

    # --- Cached mount state ---

    def _link_error(self) -> AlpacaError:
        return AlpacaError(DRIVER_ERROR, self.mount.down_message())

    def state(self) -> MountState:
        state = self.mount.state
        if not self.mount.link_up or state is None:
            raise self._link_error()
        if time.monotonic() - state.updated > MAX_STATE_AGE:
            raise AlpacaError(DRIVER_ERROR, "The mount position has not been updated recently")
        return state

    def site(self) -> SiteState:
        site = self.mount.site
        if not self.mount.link_up or site is None:
            raise self._link_error()
        if time.monotonic() - site.updated > MAX_SITE_AGE:
            raise AlpacaError(DRIVER_ERROR, "The site data has not been updated recently")
        return site

    def _info(self):
        info = self.mount.info
        if info is None:
            raise self._link_error()
        return info

    # --- Values derived from the cached state ---

    def alignment_mode(self) -> AlignmentModes:
        geometry = self._info().geometry
        if geometry.altazimuth:
            return AlignmentModes.ALT_AZ
        return AlignmentModes.GERMAN_POLAR if geometry.german else AlignmentModes.POLAR

    def is_german(self) -> bool:
        geometry = self._info().geometry
        return geometry.german and not geometry.altazimuth

    def side_of_pier(self) -> PierSide:
        return self._pier_side(self.state().ginfo)

    def _pier_side(self, ginfo: Ginfo) -> PierSide:
        # The mount's "East"/"West" pointing states match ASCOM pierEast/pierWest.
        side = {"E": PierSide.EAST, "W": PierSide.WEST}.get(ginfo.pointing_state, PierSide.UNKNOWN)
        return self._correct_southern_pier_side(side)

    def _correct_southern_pier_side(self, side: PierSide) -> PierSide:
        # Up to firmware 2.15.31 the mount reverses East and West in the southern hemisphere
        # (protocol document, :pS# and :GTsid#). Correct it, as the vendor driver does.
        info = self.mount.info
        if side is not PierSide.UNKNOWN and info and info.firmware < PIER_SIDE_FIXED_FIRMWARE:
            if self.site().latitude < 0:
                side = PierSide.WEST if side is PierSide.EAST else PierSide.EAST
        return side

    def utc_date(self) -> str:
        return format_utc(julian_date_to_datetime(self.state().ginfo.julian_date))

    def at_park(self) -> bool:
        return self.state().ginfo.status == MountStatus.PARKED

    def at_home(self) -> bool:
        # Review 3.10: True only after FindHome completed on this link, and while the mount
        # still stands where homing stopped it (status 1). Any motion command clears it.
        status = self.state().ginfo.status
        return (
            self._at_home
            and self._home_link == self.mount.links_opened
            and status == MountStatus.STOPPED
        )

    def _active_moving_axes(self) -> dict[TelescopeAxes, float]:
        if self._moving_axes and self._moving_link != self.mount.links_opened:
            # The link broke while axes moved. The new link stopped them (on_link_open).
            self._moving_axes.clear()
            self._speed_correction_was_on = None
        return self._moving_axes

    @staticmethod
    def _mount_moving(ginfo: Ginfo) -> bool:
        return ginfo.slewing or ginfo.status in MOVING_STATUSES

    def slewing(self) -> bool:
        ginfo = self.state().ginfo
        if self._home_task is not None and not self._home_task.done():
            # FindHome is asynchronous: it ends when the result is known, so AtHome is
            # already correct when Slewing turns False.
            return True
        if self._mount_moving(ginfo):
            # The mount reports the movement itself; from now on follow the mount, so
            # Slewing turns False as soon as the movement ends.
            self._slew_grace_until = 0.0
            return True
        return bool(self._active_moving_axes()) or time.monotonic() < self._slew_grace_until

    def device_state(self) -> list[tuple[str, object]]:
        """Platform 7 DeviceState: all values come from one consistent poll."""
        state = self.state()
        ginfo = state.ginfo
        return [
            ("Altitude", ginfo.altitude),
            ("AtHome", self.at_home()),
            ("AtPark", ginfo.status == MountStatus.PARKED),
            ("Azimuth", ginfo.azimuth),
            ("Declination", ginfo.declination),
            ("IsPulseGuiding", time.monotonic() < self._pulse_until),
            ("RightAscension", ginfo.right_ascension),
            ("SideOfPier", int(self._pier_side(ginfo))),
            ("SiderealTime", state.sidereal_time),
            ("Slewing", self.slewing()),
            ("Tracking", state.tracking),
            ("UTCDate", format_utc(julian_date_to_datetime(ginfo.julian_date))),
            ("TimeStamp", format_utc(datetime.now(UTC))),
        ]

    # --- Capabilities ---

    def can_find_home(self) -> bool:
        return self._info().geometry.has_homing_sensors

    def can_set_park(self) -> bool:
        return not self.config.use_keypad_park_position

    def can_set_pier_side(self) -> bool:
        return self.is_german()

    def can_sync(self) -> bool:
        return self.config.allow_sync

    def axis_rate_limit(self, axis: TelescopeAxes) -> float:
        """Review 3.7: the slew rate set in the mount; 75 % on the RA axis of a GM4000."""
        info = self._info()
        if axis is TelescopeAxes.TERTIARY:
            return 0.0
        factor = 0.75 if axis is TelescopeAxes.PRIMARY and info.model.startswith("GM4000") else 1
        return info.slew_rate * factor

    # --- Motion guards (review 3.1 and 3.12) ---

    @staticmethod
    def _check_can_move(state: MountState) -> None:
        status = state.ginfo.status
        if status == MountStatus.PARKED:
            raise AlpacaError(INVALID_WHILE_PARKED, "The mount is parked")
        if status == MountStatus.PARKING:
            raise AlpacaError(INVALID_OPERATION, "The mount is moving to the park position")
        if status == MountStatus.UNPARKING:
            raise AlpacaError(INVALID_OPERATION, "The mount is unparking")
        if status in UNSAFE_STATUSES:
            raise AlpacaError(
                INVALID_OPERATION,
                f"Mount status {status}: {UNSAFE_STATUSES[status]}. "
                "The driver does not move the mount in this state.",
            )

    async def _after_motion(self, session: MountSession, grace: bool = True) -> None:
        """Read the state now, so Slewing and positions are current when the call returns."""
        state = await session.refresh_state()
        # The grace window only covers a mount that reports the movement a moment later.
        if grace and not self._mount_moving(state.ginfo):
            self._slew_grace_until = time.monotonic() + SLEW_GRACE
        else:
            self._slew_grace_until = 0.0
        self._at_home = False

    @staticmethod
    def _check_slew_reply(reply: str) -> None:
        """Review 3.2."""
        code, message = parse_slew_reply(reply)
        if code == SLEW_OK:
            return
        if code == SLEW_PARKED:
            raise AlpacaError(INVALID_WHILE_PARKED, "The mount is parked")
        reasons = {
            1: "the target is below the lower altitude limit",
            2: "the target is above the high altitude limit",
            3: "the mount cannot perform this slew now",
            5: "the target is on the side of the meridian that is not allowed",
        }
        reason = reasons.get(code, message or f"code {code}")
        raise AlpacaError(INVALID_OPERATION, f"The mount refused the slew: {reason}")

    async def _set_mount_target(self, session: MountSession, ra: float, dec: float) -> None:
        """Review 3.5: the mount target is shared, so always set it inside the same session."""
        reply_ra, reply_dec = await session.send_many(
            f":Sr{format_right_ascension(ra)}#:Sd{format_declination(dec)}#",
            [Reply.CHAR, Reply.CHAR],
        )
        _expect_one(reply_ra, ":Sr", f"The mount refused right ascension {ra}")
        _expect_one(reply_dec, ":Sd", f"The mount refused declination {dec}")

    async def _wait_until(self, condition, timeout: float, interval: float = 0.1) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            await asyncio.sleep(interval)
        return condition()

    async def _wait_for_slew_end(self) -> None:
        if not await self._wait_until(lambda: not self.slewing(), self.config.slew_timeout, 0.2):
            raise AlpacaError(
                DRIVER_ERROR, f"The slew did not end within {self.config.slew_timeout:.0f} s"
            )

    # --- Slews (review 3.2 to 3.5) ---

    def target(self, client_id: int, name: str) -> float | None:
        """``name`` is "ra" or "dec"."""
        return self._targets.get(client_id, {}).get(name)

    def set_target(self, client_id: int, name: str, value: float) -> None:
        self._targets.setdefault(client_id, {})[name] = value

    async def slew_to_coordinates_async(self, ra: float, dec: float, client_id: int = 0) -> None:
        _check_range("RightAscension", ra, 0, 24, high_open=True)
        _check_range("Declination", dec, -90, 90)
        self.set_target(client_id, "ra", ra)
        self.set_target(client_id, "dec", dec)
        async with self._session() as session:
            state = await session.refresh_state()
            self._check_can_move(state)
            if not state.tracking:
                # Review 3.3: ASCOM requires tracking for equatorial slews.
                raise AlpacaError(INVALID_OPERATION, "Equatorial slews need Tracking to be on")
            await self._set_mount_target(session, ra, dec)
            self._check_slew_reply(await session.send(":MS#", Reply.CONDITIONAL))
            await self._after_motion(session)
        logger.info("Slewing to RA %.5f h, Dec %+.5f deg", ra, dec)

    async def slew_to_coordinates(self, ra: float, dec: float, client_id: int = 0) -> None:
        await self.slew_to_coordinates_async(ra, dec, client_id)
        await self._wait_for_slew_end()

    def _target(self, client_id: int) -> tuple[float, float]:
        ra, dec = self.target(client_id, "ra"), self.target(client_id, "dec")
        if ra is None or dec is None:
            raise AlpacaError(
                INVALID_OPERATION, "TargetRightAscension and TargetDeclination must be set first"
            )
        return ra, dec

    async def slew_to_target_async(self, client_id: int = 0) -> None:
        await self.slew_to_coordinates_async(*self._target(client_id), client_id)

    async def slew_to_target(self, client_id: int = 0) -> None:
        await self.slew_to_coordinates(*self._target(client_id), client_id)

    async def slew_to_alt_az_async(self, azimuth: float, altitude: float) -> None:
        _check_range("Azimuth", azimuth, 0, 360, high_open=True)
        _check_range("Altitude", altitude, -90, 90)
        async with self._session() as session:
            state = await session.refresh_state()
            self._check_can_move(state)
            if state.tracking:
                # Review 3.3: ASCOM requires tracking off for alt-az slews.
                raise AlpacaError(INVALID_OPERATION, "Alt-az slews need Tracking to be off")
            reply_az, reply_alt = await session.send_many(
                f":Sz{format_azimuth(azimuth)}#:Sa{format_declination(altitude)}#",
                [Reply.CHAR, Reply.CHAR],
            )
            _expect_one(reply_az, ":Sz", f"The mount refused azimuth {azimuth}")
            _expect_one(reply_alt, ":Sa", f"Altitude {altitude} is outside the slew limits")
            self._check_slew_reply(await session.send(":MA#", Reply.CONDITIONAL))
            await self._after_motion(session)
        logger.info("Slewing to Az %.4f deg, Alt %.4f deg", azimuth, altitude)

    async def slew_to_alt_az(self, azimuth: float, altitude: float) -> None:
        await self.slew_to_alt_az_async(azimuth, altitude)
        await self._wait_for_slew_end()

    async def abort_slew(self) -> None:
        """Review 3.6."""
        async with self._session() as session:
            state = await session.refresh_state()
            status = state.ginfo.status
            if status == MountStatus.PARKED:
                raise AlpacaError(INVALID_WHILE_PARKED, "The mount is parked")
            if status in (MountStatus.PARKING, MountStatus.UNPARKING, MountStatus.HOMING):
                # :Q# does not stop these; :STOP# also stops tracking.
                await session.send(":STOP#", Reply.NONE)
            # Always :Q#, also while tracking (the vendor driver skips it then), plus the
            # direction stops for MoveAxis.
            await session.send(":Q#" + STOP_ALL_AXES, Reply.NONE)
            self._moving_axes.clear()
            await self._restore_speed_correction(session)
            self._update_link_open_commands()
            self._slew_grace_until = 0.0
            await session.refresh_state()
        await self._cancel_home_watch()
        logger.info("Slew aborted")
        await self._wait_until(lambda: not self.slewing(), 5.0)

    # --- MoveAxis (review 3.7) ---

    async def move_axis(self, axis: TelescopeAxes, rate: float) -> None:
        if axis is TelescopeAxes.TERTIARY:
            raise AlpacaError(
                INVALID_VALUE, "MoveAxis supports only the primary and secondary axes"
            )
        limit = self.axis_rate_limit(axis)
        if abs(rate) > limit or 0 < abs(rate) < MIN_AXIS_RATE:
            raise AlpacaError(
                INVALID_VALUE,
                f"Rate {rate} deg/s is not 0 and not within {MIN_AXIS_RATE:.6f}..{limit}",
            )
        negative, positive = AXIS_LETTERS[axis]
        async with self._session() as session:
            state = await session.refresh_state()
            self._active_moving_axes()
            if state.ginfo.status == MountStatus.PARKED:
                raise AlpacaError(INVALID_WHILE_PARKED, "The mount is parked")
            if rate == 0:
                # Stopping is always allowed, also in an unsafe mount status.
                await session.send(f":Q{negative}#:Q{positive}#", Reply.NONE)
                self._moving_axes.pop(axis, None)
                if not self._moving_axes:
                    await self._restore_speed_correction(session)
            else:
                self._check_can_move(state)
                if axis is TelescopeAxes.PRIMARY and self._speed_correction_was_on is None:
                    # Speed correction multiplies RA speeds by 1/cos(dec): near the pole the
                    # axis would move far faster than asked. Turn it off while moving.
                    was_on = parse_flag(await session.send(":GSC#", Reply.CHAR))
                    if was_on:
                        _expect_one(
                            await session.send(":SSC0#", Reply.CHAR), ":SSC0#", "Cannot stop :SSC"
                        )
                    self._speed_correction_was_on = was_on
                rate_command = ":RA" if axis is TelescopeAxes.PRIMARY else ":RE"
                stop, move = (negative, positive) if rate > 0 else (positive, negative)
                await session.send(
                    f"{rate_command}{abs(rate):09.6f}#:Q{stop}#:M{move}#", Reply.NONE
                )
                if not self._moving_axes:
                    self._moving_link = self.mount.links_opened
                self._moving_axes[axis] = rate
            self._update_link_open_commands()
            await self._after_motion(session, grace=False)

    async def _restore_speed_correction(self, session: MountSession) -> None:
        if self._speed_correction_was_on:
            _expect_one(await session.send(":SSC1#", Reply.CHAR), ":SSC1#", "Cannot set :SSC")
        self._speed_correction_was_on = None

    def _update_link_open_commands(self) -> None:
        """If the link breaks while axes move, stop them first thing on the next link."""
        if self._moving_axes:
            commands: list[tuple[str, list[Reply]]] = [(":Q#" + STOP_ALL_AXES, [])]
            if self._speed_correction_was_on:
                commands.append((":SSC1#", [Reply.CHAR]))
            self.mount.on_link_open = commands
        else:
            self.mount.on_link_open = []

    async def _stop_axes_before_close(self) -> None:
        """When the last client disconnects, do not leave an axis moving."""
        if not self.mount.link_up or not self._active_moving_axes():
            return
        try:
            async with self._session() as session:
                await session.send(":Q#" + STOP_ALL_AXES, Reply.NONE)
                self._moving_axes.clear()
                await self._restore_speed_correction(session)
                self._update_link_open_commands()
                # The mount answers in order: this reply confirms it has the stop commands.
                await session.refresh_state()
            logger.info("Stopped the moving axes before closing the link")
        except AlpacaError as exc:
            logger.warning("Could not stop the moving axes before closing: %s", exc.message)

    # --- PulseGuide (review 3.8) ---

    async def pulse_guide(self, direction: GuideDirections, duration_ms: int) -> None:
        _check_range("Duration", duration_ms, 0, PULSE_GUIDE_MAX_MS)
        async with self._session() as session:
            self._check_can_move(await session.refresh_state())
            if duration_ms > 0:
                letter = GUIDE_LETTERS[direction]
                await session.send(f":Mg{letter}{duration_ms:04d}#", Reply.NONE)
                # The mount answers in order: this reply confirms it has the command.
                await session.refresh_state()
            self._pulse_until = max(self._pulse_until, time.monotonic() + duration_ms / 1000)
            self._at_home = False

    async def is_pulse_guiding(self) -> bool:
        if time.monotonic() < self._pulse_until:
            return True
        # :Gpgc#: 0 not guiding, 1 RA, 2 Dec, 3 both (also counts the ST-4 guider port).
        return await self.query(":Gpgc#", lambda text: int(text) != 0)

    # --- Sync (review 3.9) ---

    async def sync_to_coordinates(self, ra: float, dec: float, client_id: int = 0) -> None:
        if not self.config.allow_sync:
            raise not_implemented("Sync (enable it with device.allow_sync in the configuration)")
        _check_range("RightAscension", ra, 0, 24, high_open=True)
        _check_range("Declination", dec, -90, 90)
        self.set_target(client_id, "ra", ra)
        self.set_target(client_id, "dec", dec)
        async with self._session() as session:
            state = await session.refresh_state()
            self._check_can_move(state)
            if not state.tracking:
                raise AlpacaError(INVALID_OPERATION, "Sync needs Tracking to be on")
            await self._set_mount_target(session, ra, dec)
            # Offset mode: the sync corrects the axis offsets and does not add a model point.
            mode = await session.send(":CMCFG0#", Reply.HASH)
            if mode != "0":
                raise AlpacaError(DRIVER_ERROR, f"Unexpected reply {mode!r} to :CMCFG0#")
            reply = await session.send(":CM#", Reply.HASH)
            if not reply.startswith("Coordinates"):
                raise AlpacaError(
                    INVALID_OPERATION, f"The mount refused the sync: {reply.strip()}"
                )
            await session.refresh_state()
        logger.info("Synced to RA %.5f h, Dec %+.5f deg", ra, dec)

    async def sync_to_target(self, client_id: int = 0) -> None:
        if not self.config.allow_sync:
            raise not_implemented("Sync (enable it with device.allow_sync in the configuration)")
        await self.sync_to_coordinates(*self._target(client_id), client_id)

    # --- Park, unpark, home (review 3.10) ---

    async def park(self) -> None:
        """Asynchronous: returns when the mount has started to park. AtPark tells the end."""
        async with self._session() as session:
            state = await session.refresh_state()
            status = state.ginfo.status
            if status in (MountStatus.PARKED, MountStatus.PARKING):
                return
            if status in UNSAFE_STATUSES or status == MountStatus.UNPARKING:
                self._check_can_move(state)
            if self.config.use_keypad_park_position:
                await session.send(":hP#", Reply.NONE)
            else:
                reply = await session.send(":PsX#", Reply.HASH)
                if reply == "3":
                    # No usable saved park position: use the keypad-defined one.
                    await session.send(":hP#", Reply.NONE)
                elif reply not in ("0", "4"):
                    reasons = {"1": "below the lower limit", "2": "above the high limit"}
                    reason = reasons.get(reply, f"reply {reply!r}")
                    raise AlpacaError(INVALID_OPERATION, f"The park position is {reason}")
            self._moving_axes.clear()
            self._update_link_open_commands()
            await self._after_motion(session)
        logger.info("Parking")

    async def unpark(self) -> None:
        async with self._session() as session:
            state = await session.refresh_state()
            if state.ginfo.status not in (MountStatus.PARKED, MountStatus.PARKING):
                return
            await session.send(":PO#", Reply.NONE)
            await self._after_motion(session)
        logger.info("Unparking")

    async def set_park(self) -> None:
        if self.config.use_keypad_park_position:
            raise not_implemented("SetPark (the keypad-defined park position is in use)")
        reply = await self.query(":PyX#", str, Reply.CHAR)
        if reply != "1":
            raise AlpacaError(
                DRIVER_ERROR, f"The mount did not save the park position ({reply!r})"
            )
        logger.info("Saved the current position as park position")

    async def find_home(self) -> None:
        """Asynchronous: returns when homing has started. Slewing is True while it runs."""
        if not self.can_find_home():
            raise not_implemented("FindHome (this mount has no homing sensors)")
        async with self._session() as session:
            self._check_can_move(await session.refresh_state())
            await session.send(":hF#", Reply.NONE)
            await self._after_motion(session)
            started = session_state_is_homing(self.mount.state)
        await self._cancel_home_watch()
        self._home_task = asyncio.create_task(self._watch_homing(started))
        logger.info("Homing started")

    async def _watch_homing(self, seen_progress: bool) -> None:
        """Wait until :h?# reports the result. The vendor driver gets this wrong (review 3.10).

        ``seen_progress``: the state read right after :hF# already showed homing.
        """
        started = time.monotonic()
        try:
            while time.monotonic() - started < self.config.home_timeout:
                await asyncio.sleep(0.25)
                result = await self.mount.send(":h?#", Reply.CHAR)
                if result == "2" or session_state_is_homing(self.mount.state):
                    seen_progress = True
                    continue
                # Before homing shows as in progress, an old result may still be reported.
                if not seen_progress and time.monotonic() - started < 5:
                    continue
                if result == "1":
                    self._at_home = True
                    self._home_link = self.mount.links_opened
                    logger.info("Homing completed")
                    return
                if result == "0":
                    logger.error("Homing failed (the mount reports that the home was not found)")
                    return
            logger.error("Homing did not finish within %.0f s", self.config.home_timeout)
        except MountLinkError as exc:
            logger.warning("Stopped waiting for homing: %s", exc)

    async def _cancel_home_watch(self) -> None:
        task, self._home_task = self._home_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # --- Tracking (review 3.11) ---

    async def set_tracking(self, value: bool) -> None:
        async with self._session() as session:
            state = await session.refresh_state()
            if value:
                self._check_can_move(state)
                low, high = TRACKING_RESTART_BUG
                firmware = self._info().firmware
                # This firmware does not restart tracking after :STOP# with :AP# alone.
                command = ":AL#:AP#" if low <= firmware <= high else ":AP#"
            else:
                command = ":AL#"
            await session.send(command, Reply.NONE)
            await session.refresh_state()
        logger.info("Tracking %s", "on" if value else "off")

    async def tracking_rate(self) -> DriveRates:
        if self._tracking_rate is not None:
            return self._tracking_rate
        return drive_rate_from_gt(await self.query(":GT#", parse_float))

    async def set_tracking_rate(self, rate: DriveRates) -> None:
        self._tracking_rate = rate
        if rate != DriveRates.SIDEREAL:
            # ASCOM: RA/Dec rate offsets apply only to sidereal tracking.
            self._ra_rate = self._dec_rate = 0.0
        await self._apply_rates()

    def ra_rate(self) -> float:
        return self._ra_rate

    def dec_rate(self) -> float:
        return self._dec_rate

    async def _check_offsets_allowed(self, name: str, value: float) -> None:
        if value != 0 and await self.tracking_rate() != DriveRates.SIDEREAL:
            raise AlpacaError(
                INVALID_OPERATION, f"{name} can only be set while TrackingRate is sidereal"
            )

    async def set_ra_rate(self, value: float) -> None:
        await self._check_offsets_allowed("RightAscensionRate", value)
        self._ra_rate = value
        await self._apply_rates()

    async def set_dec_rate(self, value: float) -> None:
        await self._check_offsets_allowed("DeclinationRate", value)
        self._dec_rate = value
        await self._apply_rates()

    async def _apply_rates(self) -> None:
        rate = self._tracking_rate if self._tracking_rate is not None else DriveRates.SIDEREAL
        async with self._session() as session:
            if self._ra_rate == 0 and self._dec_rate == 0 and rate in DIRECT_RATE_COMMANDS:
                await session.send(DIRECT_RATE_COMMANDS[rate], Reply.NONE)
                # The mount answers in order: this reply confirms it has the command.
                await session.refresh_state()
                return
            # :RR and :RD take the target's rate in multiples of sidereal speed, added to
            # sidereal tracking. ASCOM RightAscensionRate is in seconds of RA per sidereal
            # second (x 15.0410686 gives arcsec per SI second).
            ra_offset = (DRIVE_RATE_OFFSETS[rate] + self._ra_rate * SIDEREAL_RATE) / SIDEREAL_RATE
            dec_offset = self._dec_rate / SIDEREAL_RATE
            for name, value in (
                ("RightAscensionRate", ra_offset),
                ("DeclinationRate", dec_offset),
            ):
                if abs(value) >= 999:
                    raise AlpacaError(INVALID_VALUE, f"{name} is too large for the mount")
            await session.send(":TQ#", Reply.NONE)
            reply_ra, reply_dec = await session.send_many(
                f":RR{format_sidereal_multiple(ra_offset)}#"
                f":RD{format_sidereal_multiple(dec_offset)}#",
                [Reply.CHAR, Reply.CHAR],
            )
            _expect_one(reply_ra, ":RR", "The mount refused the RA rate")
            _expect_one(reply_dec, ":RD", "The mount refused the Dec rate")

    async def set_guide_rate(self, degrees_per_second: float) -> None:
        """One guide rate for both axes. The protocol limits it to sidereal speed."""
        arcsec = degrees_per_second * 3600
        if not 0 < arcsec <= SIDEREAL_RATE:
            raise AlpacaError(
                INVALID_VALUE,
                f"Guide rate {degrees_per_second} deg/s is outside "
                f"(0, {SIDEREAL_RATE / 3600:.6f}]",
            )
        async with self._session() as session:
            await session.send(f":Rg{arcsec:06.3f}#", Reply.NONE)
            await session.refresh_site()

    # --- Site and time ---

    async def _set_site_value(self, command: str, refused: str) -> None:
        async with self._session() as session:
            _expect_one(await session.send(command, Reply.CHAR), command, refused)
            await session.refresh_site()

    def _site_value(self, name: str, mount_value: float, tolerance: float) -> float:
        """The value the client set, while the mount still reports it within its precision.

        The mount stores 0.1" (or 0.1 m), so a value read back differs a little from the
        value written. Like the vendor driver does for targets, return the client's value
        while the difference is only this rounding.
        """
        client_value = self._site_values.get(name)
        if client_value is not None and abs(client_value - mount_value) <= tolerance:
            return client_value
        return mount_value

    def site_latitude(self) -> float:
        return self._site_value("latitude", self.site().latitude, 0.15 / 3600)

    def site_longitude(self) -> float:
        return self._site_value("longitude", self.site().longitude, 0.15 / 3600)

    def site_elevation(self) -> float:
        return self._site_value("elevation", self.site().elevation, 0.06)

    async def set_site_latitude(self, value: float) -> None:
        _check_range("SiteLatitude", value, -90, 90)
        await self._set_site_value(f":St{format_declination(value)}#", "Latitude refused")
        self._site_values["latitude"] = value

    async def set_site_longitude(self, value: float) -> None:
        _check_range("SiteLongitude", value, -180, 180)
        # The mount counts east longitudes as negative.
        await self._set_site_value(f":Sg{format_longitude(-value)}#", "Longitude refused")
        self._site_values["longitude"] = value

    async def set_site_elevation(self, value: float) -> None:
        # ASCOM allows -300..10000 m; the mount allows -1000..9999.9 m.
        _check_range("SiteElevation", value, -300, 9999.9)
        await self._set_site_value(f":Sev{format_elevation(value)}#", "Elevation refused")
        self._site_values["elevation"] = value

    async def set_utc_date(self, moment: datetime) -> None:
        julian_date = datetime_to_julian_date(moment)
        async with self._session() as session:
            reply = await session.send(f":SJD{format_julian_date(julian_date)}#", Reply.CHAR)
            _expect_one(reply, ":SJD", f"The mount refused the date {format_utc(moment)}")
            await session.refresh_state()

    async def set_does_refraction(self, value: bool) -> None:
        await self._set_site_value(f":SREF{int(value)}#", "Refraction setting refused")

    async def set_slew_settle_time(self, seconds: int) -> None:
        _check_range("SlewSettleTime", seconds, 0, 32767)
        reply = await self.query(f":Sstm{seconds:09.3f}#", str, Reply.CHAR)
        _expect_one(reply, ":Sstm", f"The mount refused settle time {seconds}")

    # --- Pointing state ---

    async def set_side_of_pier(self, side: PierSide) -> None:
        if not self.is_german():
            raise not_implemented("SideOfPier (set) on a mount that is not German equatorial")
        if side not in (PierSide.EAST, PierSide.WEST):
            raise AlpacaError(INVALID_VALUE, f"SideOfPier must be 0 or 1, not {int(side)}")
        async with self._session() as session:
            state = await session.refresh_state()
            self._check_can_move(state)
            if self._pier_side(state.ginfo) == side:
                return
            reply = await session.send(":FLIP#", Reply.CHAR)
            if reply == "0":
                raise AlpacaError(INVALID_OPERATION, "The mount cannot flip at this position")
            if reply != "1":
                raise AlpacaError(DRIVER_ERROR, f"Unexpected reply {reply!r} to :FLIP#")
            await self._after_motion(session)
        logger.info("Meridian flip started")

    async def destination_side_of_pier(self, ra: float, dec: float) -> PierSide:
        if not self.is_german():
            raise not_implemented("DestinationSideOfPier on a mount that is not German equatorial")
        _check_range("RightAscension", ra, 0, 24, high_open=True)
        _check_range("Declination", dec, -90, 90)
        async with self._session() as session:
            await self._set_mount_target(session, ra, dec)
            reply = await session.send(":GTsid#", Reply.CHAR)
        # 2: after the slew :pS# says West; 3: East; 0: no target or not reachable.
        side = {"2": PierSide.WEST, "3": PierSide.EAST, "0": PierSide.UNKNOWN}.get(reply)
        if side is None:
            raise AlpacaError(DRIVER_ERROR, f"Unexpected reply {reply!r} to :GTsid#")
        return self._correct_southern_pier_side(side)


def session_state_is_homing(state: MountState | None) -> bool:
    return state is not None and state.ginfo.status == MountStatus.HOMING


def drive_rate_from_gt(value: float) -> DriveRates:
    """Map :GT# (four times the tracking rate in arcsec/s) to the nearest ASCOM drive rate."""
    rates = {
        DriveRates.SIDEREAL: 4 * SIDEREAL_RATE,
        DriveRates.LUNAR: 4 * (SIDEREAL_RATE - DRIVE_RATE_OFFSETS[DriveRates.LUNAR]),
        DriveRates.SOLAR: 4 * (SIDEREAL_RATE - DRIVE_RATE_OFFSETS[DriveRates.SOLAR]),
        DriveRates.KING: 4 * (SIDEREAL_RATE - DRIVE_RATE_OFFSETS[DriveRates.KING]),
    }
    return min(rates, key=lambda rate: abs(rates[rate] - value))
