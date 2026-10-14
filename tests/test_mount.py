"""The mount link against the fake mount, including the failures that crashed the
vendor's COM driver (a silent mount and a dropped connection)."""

import asyncio
import time
from dataclasses import replace

import pytest

from alpaca_10micron.mount import Mount, MountLinkError
from alpaca_10micron.protocol import Reply


async def _wait_for(condition, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.02)
    return condition()


def test_connects_and_polls(fake_mount, settings):
    async def scenario():
        mount = Mount(settings)
        mount.start()
        assert await mount.wait_until_up(3)
        assert mount.info.model == "GM2000HPS"
        assert mount.info.firmware == (3, 2, 5)
        assert mount.info.geometry.german
        assert mount.state.ginfo.right_ascension == pytest.approx(12.3456789)
        assert mount.state.sidereal_time == pytest.approx(12 + 34 / 60 + 56.78 / 3600)
        assert mount.state.tracking
        # East longitudes are negative on the mount and positive in ASCOM.
        assert mount.site.longitude == pytest.approx(8 + 32 / 60 + 24 / 3600)
        assert mount.site.refraction
        first_update = mount.state.updated
        assert await _wait_for(lambda: mount.state.updated > first_update, 1)
        assert await mount.send(":GT#") == "60.164"
        await mount.stop()
        assert not mount.link_up

    asyncio.run(scenario())
    # Ultra precision is switched on during connect.
    assert ":U2#" in fake_mount.commands


def test_silent_mount_breaks_link_and_recovers(fake_mount, settings):
    async def scenario():
        mount = Mount(settings)
        mount.start()
        assert await mount.wait_until_up(3)
        fake_mount.silent.set()
        # The poller times out after io_timeout (1 s); the link is then marked down.
        assert await _wait_for(lambda: not mount.link_up, 3)
        assert "timeout" in mount.last_error
        # While the link is down, calls fail at once instead of waiting for a timeout.
        started = time.monotonic()
        with pytest.raises(MountLinkError):
            await mount.send(":GT#")
        assert time.monotonic() - started < 0.5
        fake_mount.silent.clear()
        assert await mount.wait_until_up(5)
        assert await mount.send(":GT#") == "60.164"
        await mount.stop()
        return mount.links_opened

    assert asyncio.run(scenario()) >= 2
    assert fake_mount.connections_accepted >= 2


def test_dropped_connection_reconnects(fake_mount, settings):
    async def scenario():
        mount = Mount(settings)
        mount.start()
        assert await mount.wait_until_up(3)
        fake_mount.drop_connections()
        assert await _wait_for(lambda: mount.links_opened >= 2 and mount.link_up, 5)
        assert await mount.send(":GT#") == "60.164"
        await mount.stop()

    asyncio.run(scenario())


def test_failed_client_exchange_closes_link(fake_mount, settings):
    """A timed-out command must not leave its late reply for the next command."""

    async def scenario():
        mount = Mount(settings)
        mount.start()
        assert await mount.wait_until_up(3)
        fake_mount.state.extra[":Gslow#"] = ""  # known command, never answered
        with pytest.raises(MountLinkError):
            await mount.send(":Gslow#")
        assert not mount.link_up
        assert await mount.wait_until_up(5)
        assert await mount.send(":GT#") == "60.164"
        await mount.stop()

    asyncio.run(scenario())


def test_stop_does_not_wait_for_a_blocked_read(fake_mount, settings):
    async def scenario():
        mount = Mount(replace(settings, io_timeout=10.0))
        mount.start()
        assert await mount.wait_until_up(3)
        fake_mount.silent.set()
        await asyncio.sleep(0.3)  # the poller is now blocked waiting for a reply
        started = time.monotonic()
        await mount.stop()
        return time.monotonic() - started

    # At most the 0.5 s best-effort :Suaf0# wait, never the 10 s read timeout.
    assert asyncio.run(scenario()) < 1.5


def test_mount_unreachable(settings):
    async def scenario():
        mount = Mount(replace(settings, port=1))  # nothing listens on port 1
        mount.start()
        up = await mount.wait_until_up(0.5)
        await mount.stop()
        return up, mount.last_error

    up, last_error = asyncio.run(scenario())
    assert not up
    assert last_error


def test_firmware_too_old(fake_mount, settings):
    fake_mount.state.firmware = "2.12.3"

    async def scenario():
        mount = Mount(settings)
        mount.start()
        up = await mount.wait_until_up(0.5)
        await mount.stop()
        return up, mount.last_error

    up, last_error = asyncio.run(scenario())
    assert not up
    assert "too old" in last_error


def test_send_many_reads_mixed_replies(fake_mount, settings):
    async def scenario():
        mount = Mount(settings)
        mount.start()
        assert await mount.wait_until_up(3)
        results = await mount.send_many(":GT#\x06:GREF#", [Reply.HASH, Reply.CHAR, Reply.CHAR])
        await mount.stop()
        return results

    assert asyncio.run(scenario()) == ["60.164", "P", "1"]


def test_unattended_flip_is_turned_off_on_connect_and_disconnect(fake_mount, settings):
    async def scenario():
        mount = Mount(settings)
        mount.start()
        assert await mount.wait_until_up(3)
        after_connect = fake_mount.commands.count(":Suaf0#")
        await mount.stop()
        await _wait_for(lambda: fake_mount.commands.count(":Suaf0#") > after_connect, 1)
        return after_connect, fake_mount.commands.count(":Suaf0#")

    after_connect, after_disconnect = asyncio.run(scenario())
    assert after_connect == 1
    assert after_disconnect == 2
    assert ":Suaf1#" not in fake_mount.commands


def test_virtual_mount_is_accepted(fake_mount, settings):
    fake_mount.state.firmware = "3.2.5SIMUL"

    async def scenario():
        mount = Mount(settings)
        mount.start()
        assert await mount.wait_until_up(3)
        await mount.stop()
        return mount.info

    info = asyncio.run(scenario())
    assert info.firmware == (3, 2, 5)
    assert info.simulator


def test_alert_status_is_logged(fake_mount, settings, caplog):
    fake_mount.state.status = 11

    async def scenario():
        mount = Mount(settings)
        mount.start()
        assert await mount.wait_until_up(3)
        await asyncio.sleep(0.2)
        await mount.stop()

    asyncio.run(scenario())
    warnings = [r.getMessage() for r in caplog.records if "Mount status 11" in r.getMessage()]
    assert len(warnings) == 1  # logged once, not on every poll
