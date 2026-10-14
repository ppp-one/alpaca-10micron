"""A fake 10Micron mount: a threaded TCP server that speaks the ultra-precision protocol for
the commands the driver uses, with a small simulation of slews, parking, homing, tracking
and guiding. It can also go silent or drop its connections, to copy the network failures
that crashed the vendor's COM driver.

Two modes:

- fixed (default, for unit tests): time, sidereal time, Alt/Az and the pointing state are
  fixed values, so tests can check exact numbers.
- realistic: time comes from the PC clock; sidereal time, Alt/Az and the pointing state
  are computed. Use it for ConformU or to try a client. Run it on its own with:

      uv run python tests/fake_mount.py --port 3490
"""

import argparse
import math
import re
import socket
import socketserver
import threading
import time
from dataclasses import dataclass, field

_JD_UNIX_EPOCH = 2440587.5


def _parse_dms(text: str) -> float:
    match = re.match(r"^([+-]?)(\d+)[*:](\d+):(\d+(?:\.\d+)?)$", text)
    if not match:
        raise ValueError(text)
    sign, whole, minutes, seconds = match.groups()
    value = int(whole) + int(minutes) / 60 + float(seconds) / 3600
    return -value if sign == "-" else value


def _format_hms(hours: float) -> str:
    hundredths = round((hours % 24) * 360000) % (24 * 360000)
    h, rest = divmod(hundredths, 360000)
    m, rest = divmod(rest, 6000)
    return f"{h:02d}:{m:02d}:{rest // 100:02d}.{rest % 100:02d}"


def _altaz(ra: float, dec: float, lst: float, lat: float) -> tuple[float, float]:
    """Equatorial (hours, degrees) to (altitude, azimuth from north through east)."""
    ha, dec_r, lat_r = math.radians((lst - ra) * 15), math.radians(dec), math.radians(lat)
    sin_alt = math.sin(lat_r) * math.sin(dec_r) + math.cos(lat_r) * math.cos(dec_r) * math.cos(ha)
    alt = math.asin(max(-1.0, min(1.0, sin_alt)))
    az = math.atan2(
        -math.cos(dec_r) * math.sin(ha),
        math.sin(dec_r) * math.cos(lat_r) - math.cos(dec_r) * math.sin(lat_r) * math.cos(ha),
    )
    return math.degrees(alt), math.degrees(az) % 360


def _radec(alt: float, az: float, lst: float, lat: float) -> tuple[float, float]:
    """(altitude, azimuth) to equatorial (hours, degrees)."""
    alt_r, az_r, lat_r = math.radians(alt), math.radians(az), math.radians(lat)
    sin_dec = math.sin(lat_r) * math.sin(alt_r) + math.cos(lat_r) * math.cos(alt_r) * math.cos(
        az_r
    )
    dec = math.asin(max(-1.0, min(1.0, sin_dec)))
    ha = math.atan2(
        -math.sin(az_r) * math.cos(alt_r),
        math.sin(alt_r) * math.cos(lat_r) - math.cos(alt_r) * math.sin(lat_r) * math.cos(az_r),
    )
    return (lst - math.degrees(ha) / 15) % 24, math.degrees(dec)


@dataclass
class FakeMountState:
    realistic: bool = False
    # Identity and configuration
    firmware: str = "3.2.5"
    product: str = "10micron GM2000HPS"
    gcfg: str = "E,G,N,h"
    slew_rate: str = "15"
    # Position and status (:Ginfo#). In fixed mode: azimuth, altitude, julian_date,
    # sidereal_time and pointing are reported as set here.
    ra: float = 12.3456789  # used while tracking
    dec: float = 45.1234567
    ha: float = 0.0  # hour angle, used while not tracking (the sky moves, the mount not)
    pointing: str = "W"
    azimuth: float = 180.1234567
    altitude: float = 45.7654321
    julian_date: str = "2460962.5000000"
    jd_offset: float = 0.0  # realistic mode: days added to the PC clock (:SJD)
    status: int = 0
    tracking: bool = True
    sidereal_time: str = "12:34:56.78"
    # Site and settings
    latitude: str = "+47:22:36.0"
    longitude: str = "-008:32:24.0"  # east is negative on the mount
    elevation: str = "+0408.0"
    guide_rate: str = "7.52"
    tracking_rate: str = "60.164"
    settle_time: str = "00002.000"
    refraction: str = "1"
    speed_correction: str = "1"
    unattended_flip: str = "0"
    # Simulation
    motion_time: float = 0.3  # seconds a slew, park, unpark or homing takes
    slew_reply: str = "0"  # set to e.g. "1Object Below Horizon #" to refuse slews
    park_reply: str = "0"
    flip_reply: str = "1"
    destination_side: str = "3"  # fixed mode only
    home_result: str = "1"
    homing: str = "1"  # :h?# reply
    target_ra: float | None = None
    target_dec: float | None = None
    target_alt: float | None = None
    target_az: float | None = None
    moving: set[str] = field(default_factory=set)  # directions moved with :Me# etc.
    rates: dict[str, str] = field(default_factory=dict)  # :RR, :RD, :RA, :RE values
    guiding_until: float = 0.0
    last_drift: float = 0.0  # realistic mode: when the rate offsets were last applied
    motion: dict | None = None  # the movement in progress, see FakeMount._start
    extra: dict[str, str] = field(default_factory=dict)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class FakeMount:
    def __init__(self, host: str = "127.0.0.1", port: int = 0, realistic: bool = False):
        self.state = FakeMountState(realistic=realistic)
        self.silent = threading.Event()
        self.connections_accepted = 0
        self.commands: list[str] = []
        self._clients: list[socket.socket] = []
        self._lock = threading.RLock()
        if realistic:
            # Start tracking near the meridian, 30 degrees from the pole.
            lat = _parse_dms(self.state.latitude)
            self.state.ra = self._lst()
            self.state.dec = lat - 30 if lat >= 0 else lat + 30
            self.state.pointing = self._side_for_hour_angle(0.1)
        fake = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                fake._serve(self.request)

        self._server = _Server((host, port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self.drop_connections()

    def drop_connections(self):
        with self._lock:
            clients, self._clients = self._clients, []
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()

    def count(self, command: str) -> int:
        return self.commands.count(command)

    # --- Connection handling ---

    def _serve(self, sock: socket.socket):
        with self._lock:
            self.connections_accepted += 1
            self._clients.append(sock)
        command = ""
        try:
            while True:
                data = sock.recv(1024)
                if not data:
                    return
                for char in data.decode("latin-1"):
                    if char == "\x06":
                        with self._lock:
                            self._update()
                            self.commands.append("<ACK>")
                            reply = "P" if self.state.tracking else "L"
                        self._reply(sock, reply)
                    elif char == ":" and not command:
                        command = ":"  # values such as :Sr05:30:00.00# contain ':' too
                    elif command:
                        command += char
                        if char == "#":
                            with self._lock:
                                self.commands.append(command)
                                self._update()
                                reply = self._command(command)
                            if reply is not None:
                                self._reply(sock, reply)
                            command = ""
                    # A '#' outside a command clears the buffer: nothing to do.
        except OSError:
            return

    def _reply(self, sock: socket.socket, text: str):
        if not self.silent.is_set():
            sock.sendall(text.encode("latin-1"))

    # --- Sky and time ---

    def _julian_date(self) -> float:
        s = self.state
        if not s.realistic:
            return float(s.julian_date)
        return time.time() / 86400 + _JD_UNIX_EPOCH + s.jd_offset

    def _lst(self) -> float:
        s = self.state
        if not s.realistic:
            return _parse_dms(s.sidereal_time.replace(":", "*", 1))
        gmst = 18.697374558 + 24.06570982441908 * (self._julian_date() - 2451545.0)
        return (gmst - _parse_dms(s.longitude) / 15) % 24

    def _latitude(self) -> float:
        return _parse_dms(self.state.latitude)

    def _position(self) -> tuple[float, float, float]:
        """(ra, dec, lst). RA stays fixed while tracking; otherwise the hour angle does."""
        s = self.state
        lst = self._lst()
        ra = s.ra if s.tracking else (lst - s.ha) % 24
        return ra, s.dec, lst

    def _set_tracking(self, on: bool):
        s = self.state
        ra, _, lst = self._position()
        if on:
            s.ra = ra
        else:
            s.ha = (lst - ra) % 24
        s.tracking = on

    def _set_radec(self, ra: float, dec: float):
        s = self.state
        s.ra, s.dec = ra, dec
        s.ha = (self._lst() - ra) % 24

    @staticmethod
    def _side_for_hour_angle(ha: float) -> str:
        # Target west of the meridian: telescope on the east side, looking west ("East").
        return "E" if ha % 24 < 12 else "W"

    # --- Simulation ---

    def _start(self, moving_status, status, tracking, radec=None, altaz=None, homing=None):
        """Start a movement that ends after motion_time with the given result.

        In realistic mode a slew to coordinates also takes 1 s per 5 degrees.
        """
        s = self.state
        duration = s.motion_time
        if s.realistic and radec is not None and None not in radec:
            ra, dec, _ = self._position()
            distance = math.hypot(
                (radec[0] - ra) * 15 * math.cos(math.radians(dec)), radec[1] - dec
            )
            duration += distance / 5
        s.status = moving_status
        s.motion = {
            "end": time.monotonic() + duration,
            "status": status,
            "tracking": tracking,
            "radec": radec,
            "altaz": altaz,
            "homing": homing,
        }

    def _drift(self):
        """Realistic mode: move the position by the :RR/:RD rate offsets while tracking."""
        s = self.state
        now = time.monotonic()
        dt, s.last_drift = now - s.last_drift if s.last_drift else 0.0, now
        if not s.realistic or not s.tracking or s.motion is not None:
            return
        rr, rd = float(s.rates.get("RR", 0)), float(s.rates.get("RD", 0))
        # :RR in seconds of RA per sidereal second, :RD in multiples of sidereal speed.
        s.ra = (s.ra + rr * dt * 1.00273791 / 3600) % 24
        s.dec = max(-90.0, min(90.0, s.dec + rd * 15.0410686 * dt / 3600))

    def _update(self):
        self._drift()
        s = self.state
        motion = s.motion
        if not motion or time.monotonic() < motion["end"]:
            return
        s.motion = None
        if motion["radec"] is not None:
            self._set_radec(*motion["radec"])
        elif motion["altaz"] is not None and s.realistic:
            self._set_radec(*_radec(*motion["altaz"], self._lst(), self._latitude()))
        if motion["radec"] is not None or motion["altaz"] is not None:
            if s.realistic:
                ra, _, lst = self._position()
                s.pointing = self._side_for_hour_angle(lst - ra)
        self._set_tracking(motion["tracking"])
        s.status = motion["status"]
        if motion["homing"] is not None:
            s.homing = motion["homing"]

    def _ginfo(self) -> str:
        s = self.state
        ra, dec, lst = self._position()
        if s.realistic:
            alt, az = _altaz(ra, dec, lst, self._latitude())
            julian_date = f"{self._julian_date():.8f}"
        else:
            alt, az, julian_date = s.altitude, s.azimuth, s.julian_date
        slewing = s.motion is not None and s.status in (2, 3, 4, 6)
        return (
            f"{ra:.7f},{dec:+.7f},{s.pointing},{az:.7f},{alt:+.7f},"
            f"{julian_date},{s.status},{1 if slewing else 0}#"
        )

    def _below_horizon(self, ra: float | None, dec: float | None) -> bool:
        if not self.state.realistic or ra is None or dec is None:
            return False
        return _altaz(ra, dec, self._lst(), self._latitude())[0] < 0

    def _command(self, command: str) -> str | None:
        s = self.state
        if command in s.extra:
            return s.extra[command]
        exact = {
            ":GVN#": s.firmware + "#",
            ":GVP#": s.product + "#",
            ":GCFG#": s.gcfg + "#",
            ":GMs#": s.slew_rate + "#",
            ":Ginfo#": self._ginfo(),
            ":GS#": (_format_hms(self._lst()) if s.realistic else s.sidereal_time) + "#",
            ":Gt#": s.latitude + "#",
            ":Gg#": s.longitude + "#",
            ":Gev#": s.elevation + "#",
            ":Ggui#": s.guide_rate + "#",
            ":GT#": s.tracking_rate + "#",
            ":Gstm#": s.settle_time + "#",
            ":Gpgc#": ("1" if time.monotonic() < s.guiding_until else "0") + "#",
            ":Gstat#": f"{s.status}#",
            ":GREF#": s.refraction,
            ":GTRK#": "1" if s.tracking else "0",
            ":GSC#": s.speed_correction,
            ":h?#": s.homing,
            ":PyX#": "1",
            ":CMCFG0#": "0#",
        }
        if command in exact:
            return exact[command]
        if command in (":U2#", ":Suaf0#", ":Suaf1#"):
            if command.startswith(":Suaf"):
                s.unattended_flip = command[5]
            return None
        if command == ":GTsid#":
            if not s.realistic:
                return s.destination_side
            if s.target_ra is None or self._below_horizon(s.target_ra, s.target_dec):
                return "0"
            side = self._side_for_hour_angle(self._lst() - s.target_ra)
            return "3" if side == "E" else "2"
        if command in (":MS#", ":MA#"):
            if s.status == 5:
                return "4Mount Parked                   #"
            if s.slew_reply != "0":
                return s.slew_reply
            if command == ":MS#":
                if self._below_horizon(s.target_ra, s.target_dec):
                    return "1Object Below Horizon           #"
                self._start(6, 0, True, radec=(s.target_ra, s.target_dec))
            else:
                self._start(6, 7, False, altaz=(s.target_alt, s.target_az))
            return "0"
        if command == ":CM#":
            self._set_radec(s.target_ra, s.target_dec)
            return "Coordinates     matched        #"
        if command == ":Q#":
            if s.status == 6:
                s.motion = None
                s.status = 0 if s.tracking else 7
            s.moving.clear()
            return None
        if command in (":Qe#", ":Qw#", ":Qn#", ":Qs#"):
            s.moving.discard(command[2])
            return None
        if command == ":STOP#":
            s.motion = None
            s.moving.clear()
            self._set_tracking(False)
            s.status = 5 if s.status in (2, 5) else 1
            return None
        if command == ":AP#":
            if s.status != 5:
                self._set_tracking(True)
                s.status = 0
            return None
        if command == ":AL#":
            self._set_tracking(False)
            if s.status not in (5, 2, 3, 4, 6):
                s.status = 7
            return None
        if command in (":PsX#", ":hP#"):
            if s.status == 5:
                return "4#" if command == ":PsX#" else None
            if command == ":PsX#" and s.park_reply != "0":
                return s.park_reply + "#"
            self._set_tracking(False)
            # Park position: pointing at the celestial pole.
            lat = self._latitude()
            park = (abs(lat), 0.0 if lat >= 0 else 180.0) if s.realistic else None
            self._start(2, 5, False, altaz=park)
            return "0#" if command == ":PsX#" else None
        if command == ":PO#":
            if s.status == 5:
                self._start(3, 7, False)
            return None
        if command == ":hF#":
            s.homing = "2"
            self._start(4, 1, False, homing=s.home_result)
            return None
        if command == ":FLIP#":
            if s.flip_reply == "1":
                s.pointing = "E" if s.pointing == "W" else "W"
                self._start(6, 0 if s.tracking else 7, s.tracking)
            return s.flip_reply
        if command in (":Me#", ":Mw#", ":Mn#", ":Ms#"):
            s.moving.add(command[2])
            return None
        if command in (":TQ#", ":TL#", ":TSOLAR#"):
            s.tracking_rate = {":TQ#": "60.164", ":TL#": "57.97", ":TSOLAR#": "60.000"}[command]
            s.rates.pop("RR", None)
            s.rates.pop("RD", None)
            return None
        if command.startswith(":SSC"):
            s.speed_correction = command[4]
            return "1"
        if command.startswith(":Mg"):
            duration = int(command[4:8]) / 1000
            s.guiding_until = time.monotonic() + duration
            if s.realistic:
                # Move by guide rate x duration (arcsec), in the given direction.
                step = float(s.guide_rate) * duration / 3600  # degrees
                ra, dec, _ = self._position()
                direction = command[3]
                if direction in "ns":
                    dec += step if direction == "n" else -step
                else:
                    ra += (step if direction == "e" else -step) / 15
                self._set_radec(ra % 24, max(-90.0, min(90.0, dec)))
            return None
        for prefix in (":RR", ":RD"):
            if command.startswith(prefix):
                s.rates[prefix[1:]] = command[3:-1]
                return "1"
        for prefix in (":RA", ":RE"):
            if command.startswith(prefix):
                s.rates[prefix[1:]] = command[3:-1]
                return None
        if command.startswith(":Rg"):
            s.guide_rate = f"{float(command[3:-1]):.2f}"
            return None
        return self._set_command(command)

    def _set_command(self, command: str) -> str | None:
        """:S... commands reply '1' (valid) or '0' (invalid)."""
        s = self.state
        value = command[:-1]
        try:
            if value.startswith(":Sr"):
                hours, minutes, seconds = value[3:].split(":")
                s.target_ra = int(hours) + int(minutes) / 60 + float(seconds) / 3600
            elif value.startswith(":Sd"):
                s.target_dec = _parse_dms(value[3:])
            elif value.startswith(":Sz"):
                s.target_az = _parse_dms(value[3:])
            elif value.startswith(":Sa"):
                altitude = _parse_dms(value[3:])
                if s.realistic and altitude < 0:
                    return "0"  # out of the slew range
                s.target_alt = altitude
            elif value.startswith(":Sev"):
                s.elevation = f"{float(value[4:]):+07.1f}"
            elif value.startswith(":St"):
                _parse_dms(value[3:])
                s.latitude = value[3:].replace("*", ":")
            elif value.startswith(":Sg"):
                _parse_dms(value[3:])
                s.longitude = value[3:].replace("*", ":")
            elif value.startswith(":SJD"):
                if s.realistic:
                    s.jd_offset = float(value[4:]) - (time.time() / 86400 + _JD_UNIX_EPOCH)
                else:
                    s.julian_date = value[4:]
            elif value.startswith(":SREF"):
                s.refraction = value[5]
            elif value.startswith(":Sstm"):
                s.settle_time = value[5:]
            else:
                return None  # unknown command: no reply
        except ValueError:
            return "0"
        return "1"


def main():
    parser = argparse.ArgumentParser(description="Run the fake 10Micron mount (realistic mode).")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3490)
    parser.add_argument("--motion-time", type=float, default=2.0, help="Seconds per slew.")
    parser.add_argument("--homing", action="store_true", help="Report homing sensors.")
    args = parser.parse_args()
    mount = FakeMount(args.host, args.port, realistic=True)
    mount.state.motion_time = args.motion_time
    if args.homing:
        mount.state.gcfg = "E,G,N,H"
    print(f"Fake 10Micron mount (realistic) listening on {args.host}:{mount.port}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        mount.close()


if __name__ == "__main__":
    main()
