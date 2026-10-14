import pytest
from fake_mount import FakeMount

from alpaca_10micron.mount import MountSettings


@pytest.fixture
def fake_mount():
    mount = FakeMount()
    yield mount
    mount.close()


@pytest.fixture
def settings(fake_mount) -> MountSettings:
    """Short timeouts so failure tests run in seconds, not minutes."""
    return MountSettings(
        host="127.0.0.1",
        port=fake_mount.port,
        io_timeout=1.0,
        connect_timeout=1.0,
        poll_interval=0.05,
        site_poll_interval=0.5,
        reconnect_initial_delay=0.1,
        reconnect_max_delay=0.5,
    )
