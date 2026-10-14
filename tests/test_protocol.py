from datetime import UTC, datetime

import pytest

from alpaca_10micron.protocol import (
    MountStatus,
    ProtocolError,
    geometry_from_product,
    is_simulator_version,
    julian_date_to_datetime,
    parse_gcfg,
    parse_ginfo,
    parse_julian_date,
    parse_product,
    parse_sexagesimal,
    parse_tracking_ack,
    parse_version,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2.15.35", (2, 15, 35)),
        ("2.16", (2, 16, 0)),
        ("3.2.5", (3, 2, 5)),
        ("2.16.4SIMUL", (2, 16, 4)),
    ],
)
def test_parse_version(text, expected):
    assert parse_version(text) == expected


def test_is_simulator_version():
    assert is_simulator_version("2.16.4SIMUL")
    assert not is_simulator_version("2.16.4")


@pytest.mark.parametrize("text", ["", "3", "3.x", "1.2.3.4"])
def test_parse_version_rejects(text):
    with pytest.raises(ProtocolError):
        parse_version(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("12:30:36.00", 12.51),  # :GR# / :GS# ultra precision
        ("+45:30:00.0", 45.5),  # :GD# ultra precision
        ("-08:30:00.0", -8.5),
        ("-008:32:24.0", -(8 + 32 / 60 + 24 / 3600)),  # :Gg#
        ("+47*22", 47 + 22 / 60),  # lower precision forms are also accepted
        ("+47\xdf22:30", 47 + 22 / 60 + 30 / 3600),
    ],
)
def test_parse_sexagesimal(text, expected):
    assert parse_sexagesimal(text) == pytest.approx(expected)


def test_parse_sexagesimal_negative_zero_degrees():
    # "-00:30:00.0" must stay negative even though the degrees field is 0.
    assert parse_sexagesimal("-00:30:00.0") == pytest.approx(-0.5)


@pytest.mark.parametrize("text", ["", "12", "12:3x:00", "abc"])
def test_parse_sexagesimal_rejects(text):
    with pytest.raises(ProtocolError):
        parse_sexagesimal(text)


def test_parse_ginfo():
    ginfo = parse_ginfo(
        "12.3456789,+45.1234567,W,180.1234567,+45.7654321,2460962.50000000,5,1,extra"
    )
    assert ginfo.right_ascension == pytest.approx(12.3456789)
    assert ginfo.declination == pytest.approx(45.1234567)
    assert ginfo.pointing_state == "W"
    assert ginfo.azimuth == pytest.approx(180.1234567)
    assert ginfo.altitude == pytest.approx(45.7654321)
    assert ginfo.julian_date == pytest.approx(2460962.5)
    assert not ginfo.leap_second
    assert ginfo.status == MountStatus.PARKED
    assert ginfo.slewing


def test_parse_ginfo_leap_second():
    ginfo = parse_ginfo("1.0,+2.0,E,3.0,+4.0,2457204.50000000L,0,0")
    assert ginfo.leap_second
    assert ginfo.julian_date == pytest.approx(2457204.5)


@pytest.mark.parametrize(
    "text",
    ["", "1,2,E,3,4,5,0", "x,2,E,3,4,5,0,0", "1,2,E,3,4,5,zero,0", "1,2,E,3,4,5,0,2"],
)
def test_parse_ginfo_rejects(text):
    with pytest.raises(ProtocolError):
        parse_ginfo(text)


def test_parse_julian_date():
    assert parse_julian_date("2457204.50000579L") == (pytest.approx(2457204.50000579), True)
    assert julian_date_to_datetime(2440587.5) == datetime(1970, 1, 1, tzinfo=UTC)


def test_parse_gcfg():
    geometry = parse_gcfg("A,F,S,H")
    assert geometry.altazimuth and not geometry.german
    assert not geometry.northern and geometry.has_homing_sensors
    with pytest.raises(ProtocolError):
        parse_gcfg("X,G,N,h")


def test_product_and_geometry_fallback():
    assert parse_product("10micron GM4000QCI 48V") == "GM4000QCI 48V"
    with pytest.raises(ProtocolError):
        parse_product("LX200")
    assert geometry_from_product("AZ2000HPS").altazimuth
    gm = geometry_from_product("GM4000QCI", latitude=-30)
    assert gm.german and gm.has_homing_sensors and not gm.northern


def test_parse_tracking_ack():
    assert parse_tracking_ack("P") is True
    assert parse_tracking_ack("L") is False
    with pytest.raises(ProtocolError):
        parse_tracking_ack("0")
