"""Stage 2: motion through the Alpaca API against the simulating fake mount.

Each test checks the ASCOM behavior and the commands that reach the mount. The safety
rules are in docs/safety-review.md, section 3.
"""

import time

import pytest
from fastapi.testclient import TestClient
from test_api import connect, get, put, value, wait_until

from alpaca_10micron.config import AppConfig, DeviceConfig, ServerConfig
from alpaca_10micron.errors import (
    INVALID_OPERATION,
    INVALID_VALUE,
    INVALID_WHILE_PARKED,
)
from alpaca_10micron.main import create_app


def make_client(settings, **device_options) -> TestClient:
    config = AppConfig(
        mount=settings,
        server=ServerConfig(discovery=False),
        device=DeviceConfig(connect_wait=3.0, **device_options),
    )
    return TestClient(create_app(config))


@pytest.fixture
def client(settings):
    with make_client(settings) as client:
        connect(client)
        yield client


def error(body) -> int:
    return body["ErrorNumber"]


def follows(commands: list[str], sequence: list[str]) -> bool:
    """True if ``sequence`` appears in ``commands`` without other commands between."""
    n = len(sequence)
    return any(commands[i : i + n] == sequence for i in range(len(commands) - n + 1))


def not_slewing(client) -> bool:
    return value(client, "slewing") is False


# --- Slews ---


def test_async_slew_sets_target_atomically_and_reports_slewing(client, fake_mount):
    body = put(client, "slewtocoordinatesasync", RightAscension=5.5, Declination=20, ClientID=1)
    assert error(body) == 0
    assert value(client, "slewing") is True  # ASCOM: True as soon as the call returns
    # :Sr, :Sd and :MS# are sent in one session, so nothing else can change the target.
    assert follows(fake_mount.commands, [":Sr05:30:00.00#", ":Sd+20*00:00.0#", ":MS#"])
    assert wait_until(lambda: not_slewing(client), 3)
    assert value(client, "rightascension") == pytest.approx(5.5)
    assert value(client, "declination") == pytest.approx(20)
    assert value(client, "targetrightascension") == 5.5


def test_sync_slew_returns_when_the_slew_ends(client):
    started = time.monotonic()
    body = put(client, "slewtocoordinates", RightAscension=6, Declination=-10, ClientID=1)
    assert error(body) == 0
    assert time.monotonic() - started >= 0.3  # the fake slew takes 0.3 s
    assert value(client, "slewing") is False or time.monotonic() - started < 1.5
    assert value(client, "declination") == pytest.approx(-10)


def test_slew_to_target(client):
    assert error(put(client, "slewtotargetasync", ClientID=1)) == INVALID_OPERATION
    put(client, "targetrightascension", TargetRightAscension=7.25, ClientID=1)
    put(client, "targetdeclination", TargetDeclination=33, ClientID=1)
    assert error(put(client, "slewtotarget", ClientID=1)) == 0
    assert value(client, "rightascension") == pytest.approx(7.25)


def test_tracking_rules_for_slews(client, fake_mount):
    # Review 3.3: equatorial slews need tracking on, alt-az slews need it off.
    assert error(put(client, "slewtoaltazasync", Azimuth=90, Altitude=45, ClientID=1)) == (
        INVALID_OPERATION
    )
    put(client, "tracking", Tracking="False", ClientID=1)
    assert value(client, "tracking") is False
    body = put(client, "slewtocoordinatesasync", RightAscension=1, Declination=2, ClientID=1)
    assert error(body) == INVALID_OPERATION
    assert error(put(client, "slewtoaltaz", Azimuth=90, Altitude=45, ClientID=1)) == 0
    assert follows(fake_mount.commands, [":Sz090*00:00.0#", ":Sa+45*00:00.0#", ":MA#"])


def test_mount_refuses_slew(client, fake_mount):
    fake_mount.state.slew_reply = "1Object Below Horizon            #"
    body = put(client, "slewtocoordinatesasync", RightAscension=1, Declination=-80, ClientID=1)
    assert error(body) == INVALID_OPERATION
    assert "below the lower altitude limit" in body["ErrorMessage"]
    assert not_slewing(client)


def test_invalid_slew_values(client, fake_mount):
    for ra, dec in ((24, 0), (-1, 0), (1, 91), (1, -90.5)):
        body = put(
            client, "slewtocoordinatesasync", RightAscension=ra, Declination=dec, ClientID=1
        )
        assert error(body) == INVALID_VALUE, (ra, dec)
    put(client, "tracking", Tracking="False", ClientID=1)
    body = put(client, "slewtoaltazasync", Azimuth=360, Altitude=10, ClientID=1)
    assert error(body) == INVALID_VALUE
    assert ":MS#" not in fake_mount.commands and ":MA#" not in fake_mount.commands


def test_abort_slew_always_sends_q(client, fake_mount):
    fake_mount.state.motion_time = 5
    put(client, "slewtocoordinatesasync", RightAscension=3, Declination=10, ClientID=1)
    assert value(client, "slewing") is True
    assert error(put(client, "abortslew", ClientID=1)) == 0
    assert ":Q#" in fake_mount.commands
    assert value(client, "slewing") is False
    # Also while just tracking (the vendor driver skips :Q# then; review 3.6).
    before = fake_mount.count(":Q#")
    assert error(put(client, "abortslew", ClientID=1)) == 0
    assert fake_mount.count(":Q#") == before + 1


# --- Park and home ---


def test_park_and_unpark(client, fake_mount):
    assert error(put(client, "park", ClientID=1)) == 0
    assert value(client, "slewing") is True  # asynchronous: moving to the park position
    assert wait_until(lambda: value(client, "atpark") is True, 3)
    assert value(client, "slewing") is False
    before = fake_mount.count(":PsX#")
    assert error(put(client, "park", ClientID=1)) == 0  # already parked: nothing to do
    assert fake_mount.count(":PsX#") == before

    # Review 3.1: motion while parked raises InvalidWhileParked.
    for member, params in (
        ("slewtocoordinatesasync", {"RightAscension": 1, "Declination": 2}),
        ("moveaxis", {"Axis": 0, "Rate": 1}),
        ("pulseguide", {"Direction": 0, "Duration": 100}),
        ("abortslew", {}),
        ("tracking", {"Tracking": "True"}),
    ):
        assert error(put(client, member, ClientID=1, **params)) == INVALID_WHILE_PARKED, member

    assert error(put(client, "unpark", ClientID=1)) == 0
    assert wait_until(lambda: value(client, "atpark") is False and not_slewing(client), 3)
    assert error(put(client, "unpark", ClientID=1)) == 0  # not parked: nothing to do


def test_park_falls_back_to_keypad_position(client, fake_mount):
    fake_mount.state.park_reply = "3"
    assert error(put(client, "park", ClientID=1)) == 0
    assert follows(fake_mount.commands, [":PsX#", ":hP#"])
    assert wait_until(lambda: value(client, "atpark") is True, 3)


def test_keypad_park_position_option(settings, fake_mount):
    with make_client(settings, use_keypad_park_position=True) as client:
        connect(client)
        assert value(client, "cansetpark") is False
        assert error(put(client, "park", ClientID=1)) == 0
        assert ":hP#" in fake_mount.commands and ":PsX#" not in fake_mount.commands


def test_set_park(client, fake_mount):
    assert value(client, "cansetpark") is True
    assert error(put(client, "setpark", ClientID=1)) == 0
    assert ":PyX#" in fake_mount.commands


def test_find_home_and_at_home(settings, fake_mount):
    fake_mount.state.gcfg = "E,G,N,H"  # homing sensors
    with make_client(settings) as client:
        connect(client)
        assert value(client, "canfindhome") is True
        assert error(put(client, "findhome", ClientID=1)) == 0
        assert value(client, "slewing") is True
        assert wait_until(lambda: value(client, "athome") is True, 5)
        # Review 3.10: AtHome becomes False after any movement.
        put(client, "tracking", Tracking="True", ClientID=1)
        put(client, "slewtocoordinatesasync", RightAscension=2, Declination=3, ClientID=1)
        assert value(client, "athome") is False


def test_find_home_failure(settings, fake_mount):
    fake_mount.state.gcfg = "E,G,N,H"
    fake_mount.state.home_result = "0"
    with make_client(settings) as client:
        connect(client)
        assert error(put(client, "findhome", ClientID=1)) == 0
        assert wait_until(lambda: not_slewing(client), 3)
        time.sleep(1.5)  # give the homing watcher time to read the result
        assert value(client, "athome") is False


# --- Unsafe mount states (review 3.12) ---


def test_unsafe_status_blocks_motion(client, fake_mount):
    fake_mount.state.status = 11
    time.sleep(0.2)  # next poll
    body = put(client, "slewtocoordinatesasync", RightAscension=1, Declination=2, ClientID=1)
    assert error(body) == INVALID_OPERATION
    assert "status 11" in body["ErrorMessage"]
    assert error(put(client, "moveaxis", Axis=0, Rate=1, ClientID=1)) == INVALID_OPERATION
    # Stopping is always allowed.
    assert error(put(client, "moveaxis", Axis=0, Rate=0, ClientID=1)) == 0


# --- MoveAxis (review 3.7) ---


def test_move_axis_turns_speed_correction_off_and_back_on(client, fake_mount):
    assert error(put(client, "moveaxis", Axis=0, Rate=2, ClientID=1)) == 0
    assert follows(fake_mount.commands, [":GSC#", ":SSC0#", ":RA02.000000#", ":Qe#", ":Mw#"])
    assert fake_mount.state.speed_correction == "0"
    assert value(client, "slewing") is True
    assert fake_mount.state.moving == {"w"}
    assert error(put(client, "moveaxis", Axis=0, Rate=-1.5, ClientID=1)) == 0
    assert follows(fake_mount.commands, [":RA01.500000#", ":Qw#", ":Me#"])
    assert error(put(client, "moveaxis", Axis=0, Rate=0, ClientID=1)) == 0
    assert follows(fake_mount.commands, [":Qe#", ":Qw#"])
    assert fake_mount.state.speed_correction == "1"
    assert value(client, "slewing") is False  # no grace period after a stop


def test_move_axis_limits(client):
    assert error(put(client, "moveaxis", Axis=1, Rate=15.1, ClientID=1)) == INVALID_VALUE
    assert error(put(client, "moveaxis", Axis=2, Rate=1, ClientID=1)) == INVALID_VALUE
    assert error(put(client, "moveaxis", Axis=1, Rate=-15, ClientID=1)) == 0
    assert error(put(client, "moveaxis", Axis=1, Rate=0, ClientID=1)) == 0


def test_gm4000_ra_axis_limit(settings, fake_mount):
    fake_mount.state.product = "10micron GM4000HPS"
    with make_client(settings) as client:
        connect(client)
        assert value(client, "axisrates", Axis=0) == [{"Minimum": 1 / 3600, "Maximum": 11.25}]
        assert value(client, "axisrates", Axis=1) == [{"Minimum": 1 / 3600, "Maximum": 15.0}]


def test_moving_axis_is_stopped_on_the_next_link(client, fake_mount):
    put(client, "moveaxis", Axis=0, Rate=2, ClientID=1)
    fake_mount.drop_connections()
    telescope = client.app.state.telescope
    assert wait_until(lambda: telescope.mount.links_opened >= 2 and telescope.mount.link_up, 5)
    # The new link stops all axes and restores speed correction first thing.
    assert follows(fake_mount.commands, [":Q#", ":Qe#", ":Qw#", ":Qn#", ":Qs#", ":SSC1#"])
    assert fake_mount.state.moving == set()
    assert fake_mount.state.speed_correction == "1"
    assert value(client, "slewing") is False


def test_moving_axis_is_stopped_when_the_last_client_disconnects(client, fake_mount):
    put(client, "moveaxis", Axis=1, Rate=3, ClientID=1)
    assert fake_mount.state.moving == {"n"}
    put(client, "connected", Connected="False", ClientID=1)
    assert fake_mount.state.moving == set()


# --- PulseGuide (review 3.8) ---


def test_pulse_guide(client, fake_mount):
    assert error(put(client, "pulseguide", Direction=0, Duration=500, ClientID=1)) == 0
    assert ":Mgn0500#" in fake_mount.commands
    assert value(client, "ispulseguiding") is True
    assert wait_until(lambda: value(client, "ispulseguiding") is False, 2)
    put(client, "pulseguide", Direction=3, Duration=9999, ClientID=1)
    assert ":Mgw9999#" in fake_mount.commands


def test_pulse_guide_invalid_values(client):
    for direction, duration in ((0, -1), (0, 10000), (4, 100)):
        body = put(client, "pulseguide", Direction=direction, Duration=duration, ClientID=1)
        assert error(body) == INVALID_VALUE, (direction, duration)


# --- Sync (review 3.9) ---


def test_sync(settings, fake_mount):
    with make_client(settings, allow_sync=True) as client:
        connect(client)
        assert value(client, "cansync") is True
        body = put(client, "synctocoordinates", RightAscension=1.5, Declination=-5, ClientID=1)
        assert error(body) == 0
        assert follows(fake_mount.commands, [":Sr01:30:00.00#", ":Sd-05*00:00.0#", ":CMCFG0#"])
        assert ":CM#" in fake_mount.commands
        time.sleep(0.2)
        assert value(client, "rightascension") == pytest.approx(1.5)
        put(client, "tracking", Tracking="False", ClientID=1)
        body = put(client, "synctocoordinates", RightAscension=1.5, Declination=-5, ClientID=1)
        assert error(body) == INVALID_OPERATION


# --- Tracking and rates (review 3.11) ---


@pytest.mark.parametrize(
    ("firmware", "expected"), [("2.15.20", [":AL#", ":AP#"]), ("3.2.5", [":AP#"])]
)
def test_tracking_on_with_restart_bug_workaround(settings, fake_mount, firmware, expected):
    fake_mount.state.firmware = firmware
    with make_client(settings) as client:
        connect(client)
        put(client, "tracking", Tracking="False", ClientID=1)
        fake_mount.commands.clear()
        assert error(put(client, "tracking", Tracking="True", ClientID=1)) == 0
        sent = [c for c in fake_mount.commands if c in (":AL#", ":AP#")]
        assert sent == expected
        assert value(client, "tracking") is True


def test_tracking_rates_and_offsets(client, fake_mount):
    assert error(put(client, "trackingrate", TrackingRate=1, ClientID=1)) == 0
    assert ":TL#" in fake_mount.commands
    assert value(client, "trackingrate") == 1
    assert error(put(client, "trackingrate", TrackingRate=7, ClientID=1)) == INVALID_VALUE

    put(client, "trackingrate", TrackingRate=0, ClientID=1)
    assert error(put(client, "rightascensionrate", RightAscensionRate=0.1, ClientID=1)) == 0
    assert value(client, "rightascensionrate") == 0.1
    # 0.1 s of RA per sidereal second = 0.1 x sidereal speed.
    assert fake_mount.state.rates == {"RR": "+000.100000", "RD": "+000.000000"}
    assert error(put(client, "declinationrate", DeclinationRate=-1.5, ClientID=1)) == 0
    assert fake_mount.state.rates["RD"] == "-000.099727"

    put(client, "rightascensionrate", RightAscensionRate=0, ClientID=1)
    put(client, "declinationrate", DeclinationRate=0, ClientID=1)
    put(client, "trackingrate", TrackingRate=3, ClientID=1)  # King: slightly slower
    assert fake_mount.state.rates == {"RR": "+000.000273", "RD": "+000.000000"}


def test_guide_rate(client, fake_mount):
    assert (
        error(put(client, "guideraterightascension", GuideRateRightAscension=0.002, ClientID=1))
        == 0
    )
    assert ":Rg07.200#" in fake_mount.commands
    assert value(client, "guideratedeclination") == pytest.approx(7.2 / 3600)
    for bad in (0, -0.001, 0.0042):  # 0.0042 deg/s is above sidereal speed
        body = put(client, "guideratedeclination", GuideRateDeclination=bad, ClientID=1)
        assert error(body) == INVALID_VALUE, bad


# --- Site, time and settings ---


def test_site_and_time_setters(client, fake_mount):
    assert error(put(client, "sitelongitude", SiteLongitude=8.5, ClientID=1)) == 0
    assert ":Sg-008*30:00.0#" in fake_mount.commands  # east is negative on the mount
    assert value(client, "sitelongitude") == pytest.approx(8.5)
    assert error(put(client, "sitelatitude", SiteLatitude=-33.5, ClientID=1)) == 0
    assert ":St-33*30:00.0#" in fake_mount.commands
    assert error(put(client, "siteelevation", SiteElevation=500, ClientID=1)) == 0
    assert ":Sev+0500.0#" in fake_mount.commands
    assert error(put(client, "utcdate", UTCDate="2025-10-13T12:00:00Z", ClientID=1)) == 0
    assert ":SJD2460962.00000000#" in fake_mount.commands
    assert value(client, "utcdate") == "2025-10-13T12:00:00.000Z"
    assert error(put(client, "doesrefraction", DoesRefraction="False", ClientID=1)) == 0
    assert value(client, "doesrefraction") is False
    assert error(put(client, "slewsettletime", SlewSettleTime=5, ClientID=1)) == 0
    assert value(client, "slewsettletime") == 5


def test_setter_ranges(client):
    for member, params in (
        ("sitelatitude", {"SiteLatitude": 91}),
        ("sitelongitude", {"SiteLongitude": -181}),
        ("siteelevation", {"SiteElevation": 10000}),
        ("slewsettletime", {"SlewSettleTime": -1}),
        ("utcdate", {"UTCDate": "not a date"}),
        ("sideofpier", {"SideOfPier": 2}),
    ):
        assert error(put(client, member, ClientID=1, **params)) == INVALID_VALUE, member


# --- Pointing state ---


def test_side_of_pier_flip(client, fake_mount):
    assert value(client, "sideofpier") == 1  # West
    assert error(put(client, "sideofpier", SideOfPier=1, ClientID=1)) == 0
    assert ":FLIP#" not in fake_mount.commands  # already there
    assert error(put(client, "sideofpier", SideOfPier=0, ClientID=1)) == 0
    assert ":FLIP#" in fake_mount.commands
    assert value(client, "slewing") is True
    assert wait_until(lambda: not_slewing(client), 3)
    assert value(client, "sideofpier") == 0
    fake_mount.state.flip_reply = "0"
    body = put(client, "sideofpier", SideOfPier=1, ClientID=1)
    assert error(body) == INVALID_OPERATION


def test_destination_side_of_pier(client, fake_mount):
    assert value(client, "destinationsideofpier", RightAscension=1, Declination=2) == 0
    assert ":GTsid#" in fake_mount.commands
    fake_mount.state.destination_side = "2"
    assert value(client, "destinationsideofpier", RightAscension=1, Declination=2) == 1


def test_destination_side_of_pier_southern_fix(settings, fake_mount):
    fake_mount.state.firmware = "2.15.31"
    fake_mount.state.latitude = "-33:52:00.0"
    with make_client(settings) as client:
        connect(client)
        # The mount says "East" (3), but this firmware reverses it in the south.
        assert value(client, "destinationsideofpier", RightAscension=1, Declination=2) == 1


def test_get_members_still_need_connection(settings):
    with make_client(settings) as client:
        assert error(get(client, "slewing", ClientID=9)) == 0x407
        assert error(put(client, "park", ClientID=9)) == 0x407


# --- Findings from the first ConformU run ---


def test_rate_offsets_only_with_sidereal_tracking(client):
    put(client, "rightascensionrate", RightAscensionRate=0.1, ClientID=1)
    put(client, "trackingrate", TrackingRate=1, ClientID=1)  # lunar resets the offsets
    assert value(client, "rightascensionrate") == 0.0
    body = put(client, "rightascensionrate", RightAscensionRate=0.1, ClientID=1)
    assert error(body) == INVALID_OPERATION
    body = put(client, "declinationrate", DeclinationRate=1.0, ClientID=1)
    assert error(body) == INVALID_OPERATION
    assert error(put(client, "declinationrate", DeclinationRate=0, ClientID=1)) == 0


def test_targets_are_reset_for_a_new_session(client):
    put(client, "targetrightascension", TargetRightAscension=3.0, ClientID=1)
    put(client, "connected", Connected="False", ClientID=1)  # last client: session ends
    connect(client)
    assert error(get(client, "targetrightascension", ClientID=1)) == 0x402


def test_site_values_round_trip_exactly(client):
    put(client, "sitelongitude", SiteLongitude=18.54, ClientID=1)
    assert value(client, "sitelongitude") == 18.54
    put(client, "sitelatitude", SiteLatitude=-33.123456, ClientID=1)
    assert value(client, "sitelatitude") == -33.123456
    assert error(put(client, "siteelevation", SiteElevation=-301, ClientID=1)) == INVALID_VALUE


def test_move_axis_minimum_rate_and_parked_stop(client):
    assert error(put(client, "moveaxis", Axis=0, Rate=0.0001, ClientID=1)) == INVALID_VALUE
    put(client, "park", ClientID=1)
    assert wait_until(lambda: value(client, "atpark") is True, 3)
    assert error(put(client, "moveaxis", Axis=0, Rate=0, ClientID=1)) == INVALID_WHILE_PARKED


def test_find_home_slewing_ends_with_athome_true(settings, fake_mount):
    fake_mount.state.gcfg = "E,G,N,H"
    with make_client(settings) as client:
        connect(client)
        put(client, "findhome", ClientID=1)
        assert wait_until(lambda: not_slewing(client), 5)
        assert value(client, "athome") is True  # no gap between the two


def test_targets_are_per_client(client):
    connect(client, client_id=2)
    put(client, "targetrightascension", TargetRightAscension=3.0, ClientID=1)
    put(client, "targetdeclination", TargetDeclination=10.0, ClientID=1)
    # Client 2 cannot read or use client 1's target.
    assert error(get(client, "targetrightascension", ClientID=2)) == 0x402
    assert error(put(client, "slewtotargetasync", ClientID=2)) == INVALID_OPERATION
    assert value(client, "targetrightascension", client_id=1) == 3.0
