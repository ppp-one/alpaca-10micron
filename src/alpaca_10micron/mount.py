"""The link to the mount: one TCP connection, one exchange at a time, automatic reconnect.

All traffic goes through ``Mount._exchange`` under one lock, because the mount answers
commands strictly in order on a connection. If an exchange fails or is interrupted, the
byte stream is out of sync (a late reply would be read as the answer to the next command),
so the link is closed and the supervisor task opens a new one with a growing delay.
A failure never ends the process; it only makes calls fail until the link is back.
"""

import asyncio
import contextlib
import logging
import socket
import sys
import time
from dataclasses import dataclass

from alpaca_10micron.protocol import (
    GCFG_MIN_FIRMWARE,
    MIN_FIRMWARE,
    Ginfo,
    MountGeometry,
    MountStatus,
    Reply,
    geometry_from_product,
    is_simulator_version,
    parse_flag,
    parse_float,
    parse_gcfg,
    parse_ginfo,
    parse_product,
    parse_sexagesimal,
    parse_tracking_ack,
    parse_version,
)

logger = logging.getLogger(__name__)


class MountLinkError(Exception):
    """The link to the mount is down, or an exchange with the mount failed."""


# :Gstat# codes that need the user's attention. The vendor driver does not report them.
_ALERT_STATUSES = {
    MountStatus.MOTORS_INHIBITED: "motors inhibited (low temperature or pre-heating)",
    MountStatus.NEEDS_USER_OK: (
        "the mount suspects inconsistent data and waits for the user to allow movement; "
        "do not trust its position or alignment until this is checked"
    ),
    MountStatus.ERROR: "the mount reports an error",
}


@dataclass(frozen=True)
class MountSettings:
    host: str
    port: int = 3490
    io_timeout: float = 10.0
    connect_timeout: float = 5.0
    poll_interval: float = 0.5
    site_poll_interval: float = 10.0
    reconnect_initial_delay: float = 1.0
    reconnect_max_delay: float = 30.0


@dataclass(frozen=True)
class MountInfo:
    firmware: tuple[int, int, int]
    model: str
    geometry: MountGeometry
    simulator: bool = False  # 10Micron's virtual mount
    slew_rate: float = 0.0  # degrees per second, from :GMs# (the rate set in the mount)


@dataclass(frozen=True)
class MountState:
    """Fast-changing values, read by the poller. ``updated`` is a time.monotonic() value."""

    ginfo: Ginfo
    sidereal_time: float  # hours
    tracking: bool
    updated: float


@dataclass(frozen=True)
class SiteState:
    """Slow-changing values, read by the poller less often."""

    latitude: float  # degrees, north positive
    longitude: float  # degrees, EAST positive (ASCOM). The mount itself uses west positive.
    elevation: float  # metres
    guide_rate: float  # arcseconds per second
    refraction: bool
    updated: float


class MountSession:
    """Exchanges inside Mount.session(): the link lock is already held."""

    def __init__(self, mount: "Mount"):
        self._mount = mount

    async def send(self, command: str, reply: Reply = Reply.HASH) -> str:
        replies = [] if reply is Reply.NONE else [reply]
        results = await self._mount._exchange(command, replies)
        return results[0] if results else ""

    async def send_many(self, commands: str, replies: list[Reply]) -> list[str]:
        return await self._mount._exchange(commands, replies)

    async def refresh_state(self) -> MountState:
        """Poll the position and status now, for example right after a motion command."""
        await self._mount._poll_state()
        return self._mount.state

    async def refresh_site(self) -> SiteState:
        await self._mount._poll_site()
        return self._mount.site


def _describe(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "no reply from the mount (timeout)"
    if isinstance(exc, asyncio.IncompleteReadError):
        return "the mount closed the connection"
    if isinstance(exc, asyncio.CancelledError):
        return "exchange interrupted"
    return f"{type(exc).__name__}: {exc}"


def _configure_socket(sock: socket.socket) -> None:
    """Turn on TCP keepalive, so a dead link is found even when no reply is pending."""
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if sys.platform == "win32":
            # (on, idle time ms, probe interval ms)
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 5000, 1000))
        else:
            for name, value in (("TCP_KEEPIDLE", 5), ("TCP_KEEPINTVL", 1), ("TCP_KEEPCNT", 5)):
                if hasattr(socket, name):
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
    except OSError as exc:
        logger.warning("Could not set TCP keepalive: %s", exc)


class Mount:
    def __init__(self, settings: MountSettings):
        self.settings = settings
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._link_up = False
        self._link_event = asyncio.Event()
        self._supervisor: asyncio.Task | None = None
        self._reconnect_delay = settings.reconnect_initial_delay
        self.info: MountInfo | None = None
        self.state: MountState | None = None
        self.site: SiteState | None = None
        self.last_error: str | None = None
        self.links_opened = 0
        # Exchanges (command, replies) sent once, first thing on the next new link: for
        # example to stop axes that were moving when the old link broke. Set by the device.
        self.on_link_open: list[tuple[str, list[Reply]]] = []

    @property
    def link_up(self) -> bool:
        return self._link_up

    @property
    def running(self) -> bool:
        return self._supervisor is not None and not self._supervisor.done()

    def start(self) -> None:
        """Start keeping a link to the mount open. Returns at once."""
        if not self.running:
            self._reconnect_delay = self.settings.reconnect_initial_delay
            self._supervisor = asyncio.create_task(self._supervise(), name="mount-supervisor")

    async def stop(self) -> None:
        """Close the link and stop reconnecting."""
        task, self._supervisor = self._supervisor, None
        await self._disable_unattended_flip_before_close()
        # Closing the socket first wakes any exchange that is waiting for a reply,
        # so stop() does not wait for the read timeout.
        self._mark_broken(None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _disable_unattended_flip_before_close(self) -> None:
        """Best effort, as the vendor driver does on disconnect. Gives up after 0.5 s."""
        if not self._link_up:
            return
        try:
            await asyncio.wait_for(self._lock.acquire(), 0.5)
        except TimeoutError:
            return  # an exchange is stuck on a dead link
        try:
            if self._link_up and self._writer is not None:
                # No reply, so the stream stays in sync even if this write fails.
                self._writer.write(b":Suaf0#")
                await asyncio.wait_for(self._writer.drain(), 0.5)
        except (OSError, TimeoutError) as exc:
            logger.debug("Could not send :Suaf0# before closing: %s", exc)
        finally:
            self._lock.release()

    async def wait_until_up(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._link_event.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def send(self, command: str, reply: Reply = Reply.HASH) -> str:
        """Send one command and return its reply without the '#' ("" for Reply.NONE)."""
        replies = [] if reply is Reply.NONE else [reply]
        results = await self.send_many(command, replies)
        return results[0] if results else ""

    async def send_many(self, commands: str, replies: list[Reply]) -> list[str]:
        """Send several commands in one write and read their replies in order.

        Raises MountLinkError at once while the link is down, and also when the
        exchange fails (the link is then closed and the supervisor reconnects).
        """
        async with self._lock:
            if not self._link_up:
                raise MountLinkError(self.down_message())
            return await self._exchange(commands, replies)

    @contextlib.asynccontextmanager
    async def session(self):
        """Hold the link for several exchanges, so no other command can come between them.

        The mount has one target for all its interfaces, so for example :Sr, :Sd and :MS#
        must be sent in one session.
        """
        async with self._lock:
            if not self._link_up:
                raise MountLinkError(self.down_message())
            yield MountSession(self)

    def down_message(self) -> str:
        message = "The connection to the mount is down; the driver is reconnecting."
        if self.last_error:
            message += f" Last error: {self.last_error}"
        return message

    async def _exchange(self, commands: str, replies: list[Reply]) -> list[str]:
        # The caller must hold self._lock.
        timeout = self.settings.io_timeout
        try:
            if self._writer is None or self._reader is None:
                raise MountLinkError("not connected")
            self._writer.write(commands.encode("latin-1"))
            await asyncio.wait_for(self._writer.drain(), timeout)
            results = []
            for reply in replies:
                if reply is Reply.HASH:
                    data = (await asyncio.wait_for(self._reader.readuntil(b"#"), timeout))[:-1]
                else:
                    data = await asyncio.wait_for(self._reader.readexactly(1), timeout)
                    if reply is Reply.CONDITIONAL and data != b"0":
                        rest = await asyncio.wait_for(self._reader.readuntil(b"#"), timeout)
                        data += rest[:-1]
                results.append(data.decode("latin-1"))
            return results
        except BaseException as exc:
            reason = f"mount communication failed on {commands!r}: {_describe(exc)}"
            self._mark_broken(reason)
            if isinstance(exc, Exception) and not isinstance(exc, MountLinkError):
                raise MountLinkError(reason) from exc
            raise

    def _mark_broken(self, reason: str | None) -> None:
        """Close the link. ``reason`` is recorded as last_error; None means a normal stop."""
        if self._link_up and reason:
            logger.warning("Link to mount lost: %s", reason)
        if reason:
            self.last_error = reason
        self._link_up = False
        self._link_event.clear()
        self.state = None
        self.site = None
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            writer.close()

    async def _supervise(self) -> None:
        while True:
            try:
                await self._open_link()
                await self._poll_forever()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._mark_broken(str(exc) if isinstance(exc, MountLinkError) else _describe(exc))
                delay = self._reconnect_delay
                logger.warning("Mount link error: %s - retrying in %.0f s", self.last_error, delay)
                await asyncio.sleep(delay)
                self._reconnect_delay = min(delay * 2, self.settings.reconnect_max_delay)

    async def _open_link(self) -> None:
        settings = self.settings
        loop = asyncio.get_running_loop()
        async with self._lock:
            addresses = await loop.getaddrinfo(
                settings.host, settings.port, type=socket.SOCK_STREAM
            )
            family, kind, proto, _, address = addresses[0]
            sock = socket.socket(family, kind, proto)
            try:
                sock.setblocking(False)
                _configure_socket(sock)
                await asyncio.wait_for(loop.sock_connect(sock, address), settings.connect_timeout)
                self._reader, self._writer = await asyncio.open_connection(sock=sock)
            except BaseException:
                sock.close()
                raise

            # A single '#' clears the mount's receive buffer.
            (version,) = await self._exchange("#:GVN#", [Reply.HASH])
            firmware = parse_version(version)
            if firmware < MIN_FIRMWARE:
                raise MountLinkError(
                    f"firmware {version} is too old; this driver needs "
                    f"{'.'.join(map(str, MIN_FIRMWARE))} or newer"
                )
            # Ultra precision gives one reply format, whatever the emulation mode is.
            await self._exchange(":U2#", [])
            # Safety: unattended flip lets the mount slew on its own at the meridian. Like the
            # vendor driver, keep it off whenever this driver connects; nothing here turns it on.
            await self._exchange(":Suaf0#", [])
            if self.on_link_open:
                # Cleared only after all were sent: if this link fails too, the next one retries.
                for command, replies in list(self.on_link_open):
                    logger.warning("New link: sending %r first", command)
                    await self._exchange(command, replies)
                self.on_link_open = []
            (product,) = await self._exchange(":GVP#", [Reply.HASH])
            model = parse_product(product)
            if firmware >= GCFG_MIN_FIRMWARE:
                (config,) = await self._exchange(":GCFG#", [Reply.HASH])
                geometry = parse_gcfg(config)
            else:
                (latitude,) = await self._exchange(":Gt#", [Reply.HASH])
                geometry = geometry_from_product(model, parse_sexagesimal(latitude))
            (slew_rate,) = await self._exchange(":GMs#", [Reply.HASH])
            self.info = MountInfo(
                firmware=firmware,
                model=model,
                geometry=geometry,
                simulator=is_simulator_version(version),
                slew_rate=parse_float(slew_rate),
            )

            await self._poll_site()
            await self._poll_state()
            self._link_up = True
            self._link_event.set()
            self.links_opened += 1
        logger.info(
            "Connected to 10Micron %s, firmware %s, at %s:%d",
            model,
            version,
            settings.host,
            settings.port,
        )

    async def _poll_forever(self) -> None:
        settings = self.settings
        next_site_poll = time.monotonic() + settings.site_poll_interval
        while True:
            await asyncio.sleep(settings.poll_interval)
            async with self._lock:
                if not self._link_up:
                    # A client exchange failed and closed the link.
                    raise MountLinkError(self.last_error or "link closed")
                await self._poll_state()
                if time.monotonic() >= next_site_poll:
                    await self._poll_site()
                    next_site_poll = time.monotonic() + settings.site_poll_interval
            # A full cycle worked, so the next failure starts with a short delay again.
            self._reconnect_delay = settings.reconnect_initial_delay

    async def _poll_state(self) -> None:
        # The caller must hold self._lock. <ACK> (0x06) has a one-character reply.
        ginfo, sidereal_time, ack = await self._exchange(
            ":Ginfo#:GS#\x06", [Reply.HASH, Reply.HASH, Reply.CHAR]
        )
        previous = self.state
        self.state = MountState(
            ginfo=parse_ginfo(ginfo),
            sidereal_time=parse_sexagesimal(sidereal_time),
            tracking=parse_tracking_ack(ack),
            updated=time.monotonic(),
        )
        status = self.state.ginfo.status
        if previous is None or previous.ginfo.status != status:
            if status in _ALERT_STATUSES:
                logger.warning("Mount status %d: %s", status, _ALERT_STATUSES[status])
            elif previous is not None and previous.ginfo.status in _ALERT_STATUSES:
                logger.info("Mount status is now %d (back to normal)", status)

    async def _poll_site(self) -> None:
        # The caller must hold self._lock. :GREF# has a one-character reply.
        latitude, longitude, elevation, guide_rate, refraction = await self._exchange(
            ":Gt#:Gg#:Gev#:Ggui#:GREF#",
            [Reply.HASH, Reply.HASH, Reply.HASH, Reply.HASH, Reply.CHAR],
        )
        self.site = SiteState(
            latitude=parse_sexagesimal(latitude),
            longitude=-parse_sexagesimal(longitude),
            elevation=parse_float(elevation),
            guide_rate=parse_float(guide_rate),
            refraction=parse_flag(refraction),
            updated=time.monotonic(),
        )
