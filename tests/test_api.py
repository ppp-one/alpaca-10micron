"""The Alpaca HTTP API against the fake mount."""

import asyncio
import json
import socket
import time
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from alpaca_10micron.config import AppConfig, DeviceConfig, ServerConfig
from alpaca_10micron.device import format_utc
from alpaca_10micron.discovery import start_discovery
from alpaca_10micron.errors import (
    ACTION_NOT_IMPLEMENTED,
    DRIVER_ERROR,
    INVALID_VALUE,
    NOT_CONNECTED,
    NOT_IMPLEMENTED,
    VALUE_NOT_SET,
)
from alpaca_10micron.main import create_app
from alpaca_10micron.protocol import julian_date_to_datetime

BASE = "/api/v1/telescope/0"
SPEC = Path(__file__).resolve().parent.parent / "AlpacaDeviceAPI_v1.yaml"


def make_client(settings, connect_wait: float = 3.0) -> TestClient:
    config = AppConfig(
        mount=settings,
        server=ServerConfig(discovery=False),
        device=DeviceConfig(connect_wait=connect_wait),
    )
    return TestClient(create_app(config))


@pytest.fixture
def client(settings):
    with make_client(settings) as client:
        yield client


def get(client, member, **params):
    response = client.get(f"{BASE}/{member}", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def put(client, member, **data):
    response = client.put(f"{BASE}/{member}", data=data)
    assert response.status_code == 200, response.text
    return response.json()


def value(client, member, client_id=1, **params):
    body = get(client, member, ClientID=client_id, **params)
    assert body["ErrorNumber"] == 0, body
    return body["Value"]


def connect(client, client_id=1):
    body = put(client, "connected", Connected="True", ClientID=client_id)
    assert body["ErrorNumber"] == 0, body


def wait_until(condition, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def test_every_spec_endpoint_has_a_route(client):
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    expected = set()
    for path, operations in spec["paths"].items():
        prefix, _, member = path.rpartition("/")
        if prefix in ("/telescope/{device_number}", "/{device_type}/{device_number}"):
            for method in operations:
                if method in ("get", "put"):
                    expected.add((method.upper(), member))
    # The OpenAPI schema lists every route, whatever the FastAPI version does with app.routes.
    routes = {
        (method.upper(), path.rpartition("/")[2])
        for path, operations in client.app.openapi()["paths"].items()
        if path.startswith("/api/v1/telescope/{device_number}/")
        for method in operations
    }
    assert len(expected) > 90
    assert expected - routes == set()


def test_management_api(client):
    assert client.get("/management/apiversions").json()["Value"] == [1]
    description = client.get("/management/v1/description").json()["Value"]
    assert description["ManufacturerVersion"] == "0.1.0"
    devices = client.get("/management/v1/configureddevices").json()["Value"]
    assert devices == [
        {
            "DeviceName": "10Micron mount",
            "DeviceType": "Telescope",
            "DeviceNumber": 0,
            "UniqueID": devices[0]["UniqueID"],
        }
    ]
    assert len(devices[0]["UniqueID"]) == 36
    assert "10Micron Alpaca driver" in client.get("/setup/v1/telescope/0/setup").text


def test_parameter_names_are_not_case_sensitive(client):
    assert get(client, "name", ClientTransactionID=42)["ClientTransactionID"] == 42
    assert get(client, "name", clienttransactionid=7)["ClientTransactionID"] == 7
    assert get(client, "name", CLIENTTRANSACTIONID="bad")["ClientTransactionID"] == 0
    body = put(client, "connected", connected="true", clientid=3, ClientTransactionID=9)
    assert body["ErrorNumber"] == 0 and body["ClientTransactionID"] == 9
    assert value(client, "connected", client_id=3) is True


def test_server_transaction_ids_increase(client):
    first = get(client, "name")["ServerTransactionID"]
    second = get(client, "name")["ServerTransactionID"]
    assert second > first > 0


def test_members_need_a_connection(client):
    assert value(client, "name") == "10Micron mount"
    assert value(client, "interfaceversion") == 4
    assert value(client, "connected") is False
    for member in ("rightascension", "canslew", "devicestate", "trackingrates"):
        assert get(client, member, ClientID=1)["ErrorNumber"] == NOT_CONNECTED


def test_read_only_values(client):
    connect(client)
    assert value(client, "rightascension") == pytest.approx(12.3456789)
    assert value(client, "declination") == pytest.approx(45.1234567)
    assert value(client, "azimuth") == pytest.approx(180.1234567)
    assert value(client, "altitude") == pytest.approx(45.7654321)
    assert value(client, "siderealtime") == pytest.approx(12 + 34 / 60 + 56.78 / 3600)
    assert value(client, "sitelatitude") == pytest.approx(47 + 22 / 60 + 36 / 3600)
    assert value(client, "sitelongitude") == pytest.approx(8 + 32 / 60 + 24 / 3600)
    assert value(client, "siteelevation") == pytest.approx(408)
    assert value(client, "utcdate") == format_utc(julian_date_to_datetime(2460962.5))
    assert value(client, "sideofpier") == 1  # "W" -> pierWest
    assert value(client, "tracking") is True
    assert value(client, "slewing") is False
    assert value(client, "atpark") is False
    assert value(client, "athome") is False
    assert value(client, "alignmentmode") == 2  # German polar
    assert value(client, "equatorialsystem") == 1  # topocentric
    assert value(client, "trackingrate") == 0  # sidereal
    assert value(client, "trackingrates") == [0, 1, 2, 3]
    assert value(client, "guideraterightascension") == pytest.approx(7.52 / 3600)
    assert value(client, "slewsettletime") == 2
    assert value(client, "ispulseguiding") is False
    assert value(client, "doesrefraction") is True
    assert value(client, "rightascensionrate") == 0.0
    assert value(client, "canslew") is True
    assert value(client, "cansync") is False  # off unless device.allow_sync
    assert value(client, "cansyncaltaz") is False
    assert value(client, "canfindhome") is False  # GCFG says "h": no homing sensors
    assert value(client, "cansetpierside") is True  # German mount
    assert value(client, "axisrates", Axis=0) == [{"Minimum": 0.0, "Maximum": 15.0}]
    assert value(client, "axisrates", Axis=2) == []
    assert value(client, "canmoveaxis", Axis=1) is True
    assert value(client, "canmoveaxis", Axis=2) is False
    assert value(client, "description") == "10Micron GM2000HPS"
    names = {item["Name"] for item in value(client, "devicestate")}
    assert {"RightAscension", "Declination", "Slewing", "Tracking", "TimeStamp"} <= names


def test_invalid_values_and_unset_targets(client):
    connect(client)
    assert get(client, "axisrates", ClientID=1, Axis=5)["ErrorNumber"] == INVALID_VALUE
    assert get(client, "targetrightascension", ClientID=1)["ErrorNumber"] == VALUE_NOT_SET
    body = put(client, "targetrightascension", TargetRightAscension="25", ClientID=1)
    assert body["ErrorNumber"] == INVALID_VALUE
    body = put(client, "targetdeclination", TargetDeclination="-91", ClientID=1)
    assert body["ErrorNumber"] == INVALID_VALUE
    put(client, "targetrightascension", TargetRightAscension="5.5", ClientID=1)
    assert value(client, "targetrightascension") == 5.5


def test_not_implemented_members(client):
    connect(client)
    body = put(client, "synctoaltaz", Azimuth=1, Altitude=2, ClientID=1)
    assert body["ErrorNumber"] == NOT_IMPLEMENTED
    body = put(client, "synctocoordinates", RightAscension=1, Declination=2, ClientID=1)
    assert body["ErrorNumber"] == NOT_IMPLEMENTED  # sync is off by default
    assert put(client, "findhome", ClientID=1)["ErrorNumber"] == NOT_IMPLEMENTED
    body = put(client, "action", Action="x", Parameters="", ClientID=1)
    assert body["ErrorNumber"] == ACTION_NOT_IMPLEMENTED


def test_bad_requests_get_http_400(client):
    assert client.get("/api/v1/telescope/1/name").status_code == 400
    assert client.get("/api/v1/camera/0/name").status_code == 400
    assert client.get(f"{BASE}/nosuchmember").status_code == 400
    assert client.put(f"{BASE}/connected", data={"Connected": "maybe"}).status_code == 400
    assert client.put(f"{BASE}/connected", data={}).status_code == 400
    connect(client)
    response = client.get(f"{BASE}/axisrates", params={"ClientID": 1, "Axis": "x"})
    assert response.status_code == 400


def test_platform7_connect_and_disconnect(client):
    assert put(client, "connect", ClientID=5)["ErrorNumber"] == 0
    assert wait_until(lambda: value(client, "connecting", client_id=5) is False, 3)
    assert value(client, "connected", client_id=5) is True
    assert put(client, "disconnect", ClientID=5)["ErrorNumber"] == 0
    assert value(client, "connected", client_id=5) is False
    assert not client.app.state.telescope.mount.running


def test_one_client_disconnecting_does_not_affect_another(client):
    connect(client, client_id=1)
    connect(client, client_id=2)
    put(client, "connected", Connected="False", ClientID=1)
    assert get(client, "rightascension", ClientID=1)["ErrorNumber"] == NOT_CONNECTED
    assert value(client, "rightascension", client_id=2) == pytest.approx(12.3456789)
    put(client, "connected", Connected="False", ClientID=2)
    assert not client.app.state.telescope.mount.running


def test_lost_link_gives_driver_error_and_recovers(client, fake_mount):
    connect(client)
    fake_mount.silent.set()
    assert wait_until(
        lambda: get(client, "rightascension", ClientID=1)["ErrorNumber"] == DRIVER_ERROR, 4
    )
    body = get(client, "rightascension", ClientID=1)
    assert "reconnecting" in body["ErrorMessage"]
    fake_mount.silent.clear()
    # The client stays connected; values come back without a new Connect.
    assert wait_until(lambda: get(client, "rightascension", ClientID=1)["ErrorNumber"] == 0, 5)
    assert value(client, "connected") is True


def test_failed_connect_is_reported(settings):
    with make_client(replace(settings, port=1), connect_wait=0.5) as client:
        body = put(client, "connected", Connected="True", ClientID=1)
        assert body["ErrorNumber"] == DRIVER_ERROR
        assert "Could not connect" in body["ErrorMessage"]
        assert value(client, "connected") is False

        put(client, "connect", ClientID=2)
        assert wait_until(lambda: get(client, "connecting", ClientID=2)["ErrorNumber"] != 0, 3)
        assert value(client, "connecting", client_id=2) is False
        assert not client.app.state.telescope.mount.running


def test_discovery_reply():
    async def scenario():
        transport = await start_discovery(alpaca_port=11111, discovery_port=0)
        port = transport.get_extra_info("sockname")[1]

        def ask():
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.settimeout(2)
                sock.sendto(b"alpacadiscovery1", ("127.0.0.1", port))
                return sock.recvfrom(1024)[0]

        try:
            return await asyncio.to_thread(ask)
        finally:
            transport.close()

    assert json.loads(asyncio.run(scenario())) == {"AlpacaPort": 11111}


@pytest.mark.parametrize(
    ("firmware", "latitude", "expected"),
    [
        ("2.15.31", "-33:52:00.0", 0),  # old firmware, south: mount's "W" is really East
        ("2.15.32", "-33:52:00.0", 1),  # fixed firmware: no correction
        ("2.15.31", "+47:22:36.0", 1),  # north: no correction
    ],
)
def test_side_of_pier_southern_hemisphere_fix(settings, fake_mount, firmware, latitude, expected):
    fake_mount.state.firmware = firmware
    fake_mount.state.latitude = latitude
    with make_client(settings) as client:
        connect(client)
        assert value(client, "sideofpier") == expected
        state = {item["Name"]: item["Value"] for item in value(client, "devicestate")}
        assert state["SideOfPier"] == expected


def test_slew_settle_time_fits_in_16_bits(client, fake_mount):
    fake_mount.state.settle_time = "99999.000"
    connect(client)
    assert value(client, "slewsettletime") == 32767


def test_driver_version_is_major_minor(client):
    assert value(client, "driverversion") == "0.1"
