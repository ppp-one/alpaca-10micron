"""Parsing and formatting for the 10Micron mount command protocol (document for firmware 3.2.5).

The driver always switches the session to ultra precision with ``:U2#``, so only the
ultra-precision reply formats need to be parsed. All functions here are pure: no I/O.
"""

import enum
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# :Ginfo# has its documented format from 2.14.9. Ultra precision exists from 2.10.
MIN_FIRMWARE = (2, 14, 9)
# :GCFG# (mount geometry) exists from 3.2.5. Older firmware: derive it from the product name.
GCFG_MIN_FIRMWARE = (3, 2, 5)
# Up to 2.15.31, :pS# and the :Ginfo# pointing state are reversed in the southern hemisphere.
PIER_SIDE_FIXED_FIRMWARE = (2, 15, 32)
# 10Micron's virtual mount appends this to its :GVN# firmware version.
SIMULATOR_SUFFIX = "SIMUL"

_JD_UNIX_EPOCH = 2440587.5


class ProtocolError(ValueError):
    """A reply from the mount does not have the expected format."""


class Reply(enum.Enum):
    """How the mount ends its reply to a command."""

    NONE = "none"  # the command has no reply
    CHAR = "char"  # exactly one character, no terminator
    HASH = "hash"  # characters up to and including '#'
    # One character; if it is not '0', a message up to '#' follows (:MS#, :MA#, :MaX#).
    CONDITIONAL = "conditional"


class MountStatus(enum.IntEnum):
    """Status codes from :Gstat# (and field 7 of :Ginfo#)."""

    TRACKING = 0
    STOPPED = 1
    PARKING = 2
    UNPARKING = 3
    HOMING = 4
    PARKED = 5
    SLEWING = 6
    NOT_TRACKING = 7
    MOTORS_INHIBITED = 8
    OUTSIDE_TRACKING_LIMITS = 9
    SATELLITE_TRACKING = 10
    NEEDS_USER_OK = 11
    DDS_WAITING_FOR_POWER = 12
    DDS_MONITOR = 13
    DDS_AUTOTUNE = 14
    UNKNOWN = 98
    ERROR = 99


@dataclass(frozen=True)
class Ginfo:
    """The reply to :Ginfo#. Coordinates are topocentric, equinox of date (JNow)."""

    right_ascension: float  # hours
    declination: float  # degrees
    pointing_state: str  # "E", "W" or "" (see "Notes on pointing state")
    azimuth: float  # degrees
    altitude: float  # degrees
    julian_date: float  # UTC
    leap_second: bool
    status: int  # a MountStatus value; kept as int so new codes do not break parsing
    slewing: bool  # True while :D# would report a slew (includes the settle time)


@dataclass(frozen=True)
class MountGeometry:
    """Mount type, from :GCFG# or (older firmware) from the product name."""

    altazimuth: bool
    german: bool
    northern: bool
    has_homing_sensors: bool


def is_simulator_version(text: str) -> bool:
    return text.strip().endswith(SIMULATOR_SUFFIX)


def parse_version(text: str) -> tuple[int, int, int]:
    """Parse :GVN# ("maj.min[.fix]", "SIMUL" appended by the virtual mount) into a tuple."""
    parts = text.strip().removesuffix(SIMULATOR_SUFFIX).split(".")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        raise ProtocolError(f"invalid firmware version {text!r}")
    numbers = [int(p) for p in parts] + [0]
    return numbers[0], numbers[1], numbers[2]


_SEXAGESIMAL = re.compile(r"^([+-]?)(\d+)[:*\xdf](\d+)(?::(\d+(?:\.\d*)?))?$")


def parse_sexagesimal(text: str) -> float:
    """Parse "sDD:MM:SS.S", "HH:MM:SS.SS" or "sDDD:MM:SS.S" into decimal units.

    The unit of the result is the unit of the first field (hours or degrees).
    '*' and 0xDF are accepted as the first separator, as in the lower precisions.
    """
    match = _SEXAGESIMAL.match(text.strip())
    if not match:
        raise ProtocolError(f"invalid sexagesimal value {text!r}")
    sign, whole, minutes, seconds = match.groups()
    value = int(whole) + int(minutes) / 60 + (float(seconds) if seconds else 0.0) / 3600
    return -value if sign == "-" else value


def parse_float(text: str) -> float:
    try:
        return float(text.strip())
    except ValueError as exc:
        raise ProtocolError(f"invalid number {text!r}") from exc


def parse_julian_date(text: str) -> tuple[float, bool]:
    """Parse a :GJD2#-style Julian date. A trailing 'L' marks a leap second."""
    text = text.strip()
    leap = text.endswith("L")
    return parse_float(text.removesuffix("L")), leap


def julian_date_to_datetime(julian_date: float) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(days=julian_date - _JD_UNIX_EPOCH)


def parse_ginfo(text: str) -> Ginfo:
    """Parse :Ginfo#. The protocol may add fields at the end, so extra fields are ignored."""
    fields = text.strip().split(",")
    if len(fields) < 8:
        raise ProtocolError(f"invalid :Ginfo# reply {text!r}")
    julian_date, leap = parse_julian_date(fields[5])
    try:
        status = int(fields[6])
    except ValueError as exc:
        raise ProtocolError(f"invalid status in :Ginfo# reply {text!r}") from exc
    if fields[7] not in ("0", "1"):
        raise ProtocolError(f"invalid slew flag in :Ginfo# reply {text!r}")
    return Ginfo(
        right_ascension=parse_float(fields[0]),
        declination=parse_float(fields[1]),
        pointing_state=fields[2] if fields[2] in ("E", "W") else "",
        azimuth=parse_float(fields[3]),
        altitude=parse_float(fields[4]),
        julian_date=julian_date,
        leap_second=leap,
        status=status,
        slewing=fields[7] == "1",
    )


def parse_gcfg(text: str) -> MountGeometry:
    """Parse :GCFG# ("A|E,G|F,N|S,H|h", more fields may follow)."""
    fields = text.strip().split(",")
    if (
        len(fields) < 4
        or fields[0] not in ("A", "E")
        or fields[1] not in ("G", "F")
        or fields[2] not in ("N", "S")
        or fields[3] not in ("H", "h")
    ):
        raise ProtocolError(f"invalid :GCFG# reply {text!r}")
    return MountGeometry(
        altazimuth=fields[0] == "A",
        german=fields[1] == "G",
        northern=fields[2] == "N",
        has_homing_sensors=fields[3] == "H",
    )


def parse_product(text: str) -> str:
    """Parse :GVP# ("10micron GM2000HPS") into the model name ("GM2000HPS")."""
    text = text.strip()
    prefix = "10micron "
    if not text.startswith(prefix):
        raise ProtocolError(f"not a 10Micron product name: {text!r}")
    return text[len(prefix) :]


def geometry_from_product(model: str, latitude: float | None = None) -> MountGeometry:
    """Best guess of the mount geometry for firmware without :GCFG#.

    AZ models are altazimuth, GM models are German equatorial. Only the GM4000QCI
    and AZ2000 have homing sensors (see :hS# / :hF#).
    """
    altazimuth = model.startswith("AZ")
    return MountGeometry(
        altazimuth=altazimuth,
        german=not altazimuth,
        northern=latitude is None or latitude >= 0,
        has_homing_sensors=model.startswith(("GM4000QCI", "AZ2000")),
    )


def parse_tracking_ack(char: str) -> bool:
    """Parse the reply to <ACK> (0x06): 'P' means tracking, 'L' means not tracking."""
    if char not in ("P", "L"):
        raise ProtocolError(f"invalid <ACK> reply {char!r}")
    return char == "P"


def parse_flag(char: str) -> bool:
    """Parse a one-character "0"/"1" reply such as :GREF# or :GTRK#."""
    if char not in ("0", "1"):
        raise ProtocolError(f"invalid flag reply {char!r}")
    return char == "1"


# --- Formatting of command parameters ---


def _split_sexagesimal(value: float, units_per_whole: int) -> tuple[int, int, int]:
    """Split an absolute value into whole, minutes and sub-second units, rounding once."""
    total = round(abs(value) * 3600 * units_per_whole)
    whole, rest = divmod(total, 3600 * units_per_whole)
    minutes, sub_seconds = divmod(rest, 60 * units_per_whole)
    return whole, minutes, sub_seconds


def format_right_ascension(hours: float) -> str:
    """HH:MM:SS.SS, for :Sr. The value is wrapped into 0..24 h."""
    whole, minutes, hundredths = _split_sexagesimal(hours % 24, 100)
    whole %= 24
    return f"{whole:02d}:{minutes:02d}:{hundredths // 100:02d}.{hundredths % 100:02d}"


def _format_dms(degrees: float, digits: int, signed: bool) -> str:
    whole, minutes, tenths = _split_sexagesimal(degrees, 10)
    sign = ("-" if degrees < 0 and (whole or minutes or tenths) else "+") if signed else ""
    return f"{sign}{whole:0{digits}d}*{minutes:02d}:{tenths // 10:02d}.{tenths % 10}"


def format_declination(degrees: float) -> str:
    """sDD*MM:SS.S, for :Sd, :Sa and :St."""
    return _format_dms(degrees, 2, signed=True)


def format_azimuth(degrees: float) -> str:
    """DDD*MM:SS.S, for :Sz. The value is wrapped into 0..360 degrees."""
    text = _format_dms(degrees % 360, 3, signed=False)
    return "000" + text[3:] if text.startswith("360") else text


def format_longitude(degrees_west_positive: float) -> str:
    """sDDD*MM:SS.S, for :Sg. The mount counts west longitudes as positive."""
    return _format_dms(degrees_west_positive, 3, signed=True)


def format_elevation(metres: float) -> str:
    """sXXXX.X, for :Sev."""
    return f"{metres:+07.1f}"


def format_julian_date(julian_date: float) -> str:
    """JJJJJJJ.JJJJJJJJ, for :SJD."""
    return f"{julian_date:.8f}"


def format_sidereal_multiple(value: float) -> str:
    """sXXX.XXXXXX (multiples of sidereal speed), for :RR and :RD."""
    return f"{value:+011.6f}"


def datetime_to_julian_date(moment: datetime) -> float:
    return (moment.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds() / 86400 + (
        _JD_UNIX_EPOCH
    )


# Replies of :MS#, :MSnf#, :MA# and :MaX#. The text after the code is padded with spaces.
SLEW_OK = 0
SLEW_BELOW_HORIZON = 1
SLEW_ABOVE_HIGH_LIMIT = 2
SLEW_NOT_POSSIBLE = 3
SLEW_PARKED = 4
SLEW_WRONG_SIDE = 5


def parse_slew_reply(text: str) -> tuple[int, str]:
    """Return (code, message). Code 0 means the slew started."""
    if not text or not text[0].isdigit():
        raise ProtocolError(f"invalid slew reply {text!r}")
    return int(text[0]), text[1:].strip()
