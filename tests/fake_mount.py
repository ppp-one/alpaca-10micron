"""A fake 10Micron mount for tests: a threaded TCP server that speaks the ultra-precision
protocol for the commands the driver uses, with a small simulation of slews, parking,
homing, tracking and guiding. It can also go silent or drop its connections, to copy the
network failures that crashed the vendor's COM driver.
"""

import re
import socket
import socketserver
import threading
import time
from dataclasses import dataclass, field


def _parse_dms(text: str) -> float:
    match = re.match(r"^([+-]?)(\d+)[*:](\d+):(\d+(?:\.\d+)?)$", text)
    if not match:
        raise ValueError(text)
    sign, whole, minutes, seconds = match.groups()
    value = int(whole) + int(minutes) / 60 + float(seconds) / 3600
    return -value if sign == "-" else value


@dataclass
class FakeMountState:
    # Identity and configuration
    firmware: str = "3.2.5"
    product: str = "10micron GM2000HPS"
    gcfg: str = "E,G,N,h"
    slew_rate: str = "15"
    # Position and status (:Ginfo#)
    ra: float = 12.3456789
    dec: float = 45.1234567
    pointing: str = "W"
    azimuth: float = 180.1234567
    altitude: float = 45.7654321
    julian_date: str = "2460962.5000000"
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
    destination_side: str = "3"
    home_result: str = "1"
    homing: str = "1"  # :h?# reply
    target_ra: float | None = None
    target_dec: float | None = None
    moving: set[str] = field(default_factory=set)  # directions moved with :Me# etc.
    rates: dict[str, str] = field(default_factory=dict)  # :RR, :RD, :RA, :RE values
    guiding_until: float = 0.0
    # (end time, status after, tracking after, new ra, new dec, homing after)
    motion: tuple | None = None
    extra: dict[str, str] = field(default_factory=dict)


class FakeMount:
    def __init__(self):
        self.state = FakeMountState()
        self.silent = threading.Event()
        self.connections_accepted = 0
        self.commands: list[str] = []
        self._clients: list[socket.socket] = []
        self._lock = threading.RLock()
        fake = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                fake._serve(self.request)

        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
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

    # --- Simulation ---

    def _update(self):
        s = self.state
        if s.motion and time.monotonic() >= s.motion[0]:
            _, status, tracking, ra, dec, homing = s.motion
            s.motion = None
            s.status, s.tracking = status, tracking
            if ra is not None:
                s.ra, s.dec = ra, dec
            if homing is not None:
                s.homing = homing

    def _start(self, moving_status, status, tracking, ra=None, dec=None, homing=None):
        s = self.state
        s.status = moving_status
        s.motion = (time.monotonic() + s.motion_time, status, tracking, ra, dec, homing)

    def _stop_motion(self, status=None):
        s = self.state
        if s.motion:
            s.motion = None
        s.status = status if status is not None else (0 if s.tracking else 7)

    def _command(self, command: str) -> str | None:
        s = self.state
        if command in s.extra:
            return s.extra[command]
        slewing = s.motion is not None and s.status in (2, 3, 4, 6)
        exact = {
            ":GVN#": s.firmware + "#",
            ":GVP#": s.product + "#",
            ":GCFG#": s.gcfg + "#",
            ":GMs#": s.slew_rate + "#",
            ":Ginfo#": (
                f"{s.ra:.7f},{s.dec:+.7f},{s.pointing},{s.azimuth:.7f},{s.altitude:+.7f},"
                f"{s.julian_date},{s.status},{1 if slewing else 0}#"
            ),
            ":GS#": s.sidereal_time + "#",
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
            ":GTsid#": s.destination_side,
            ":PyX#": "1",
            ":CMCFG0#": "0#",
        }
        if command in exact:
            return exact[command]
        if command in (":U2#", ":Suaf0#", ":Suaf1#"):
            if command.startswith(":Suaf"):
                s.unattended_flip = command[5]
            return None
        if command in (":MS#", ":MA#"):
            if s.status == 5:
                return "4Mount Parked                   #"
            if s.slew_reply != "0":
                return s.slew_reply
            if command == ":MS#":
                self._start(6, 0, True, s.target_ra, s.target_dec)
            else:
                self._start(6, 7, False)
            return "0"
        if command == ":CM#":
            s.ra, s.dec = s.target_ra, s.target_dec
            return "Coordinates     matched        #"
        if command == ":Q#":
            if s.status == 6:
                self._stop_motion()
            s.moving.clear()
            return None
        if command in (":Qe#", ":Qw#", ":Qn#", ":Qs#"):
            s.moving.discard(command[2])
            return None
        if command == ":STOP#":
            s.motion = None
            s.moving.clear()
            s.tracking = False
            s.status = 5 if s.status in (2, 5) else 1
            return None
        if command == ":AP#":
            if s.status != 5:
                s.tracking, s.status = True, 0
            return None
        if command == ":AL#":
            s.tracking = False
            if s.status not in (5, 2, 3, 4, 6):
                s.status = 7
            return None
        if command in (":PsX#", ":hP#"):
            if s.status == 5:
                return "4#" if command == ":PsX#" else None
            if command == ":PsX#" and s.park_reply != "0":
                return s.park_reply + "#"
            s.tracking = False
            self._start(2, 5, False)
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
            s.guiding_until = time.monotonic() + int(command[4:8]) / 1000
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
                _parse_dms(value[3:])
            elif value.startswith(":Sa"):
                _parse_dms(value[3:])
            elif value.startswith(":Sev"):
                s.elevation = f"{float(value[4:]):+07.1f}"
            elif value.startswith(":St"):
                _parse_dms(value[3:])
                s.latitude = value[3:].replace("*", ":")
            elif value.startswith(":Sg"):
                _parse_dms(value[3:])
                s.longitude = value[3:].replace("*", ":")
            elif value.startswith(":SJD"):
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
