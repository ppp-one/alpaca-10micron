# alpaca-10micron

> [!WARNING]  
> USE AT YOUR OWN RISK - UNTESTED ON A REAL MOUNT. See [docs/safety-review.md](docs/safety-review.md).

An ASCOM Alpaca telescope driver for 10Micron mounts, written in Python with FastAPI.

The driver talks to the mount over TCP (port 3490 or 3492) with the 10Micron command
protocol, and offers the mount to Alpaca clients such as NINA. If the network to the mount
fails, the driver reconnects by itself and your programs keep their connection. With the
vendor's COM driver, the same failure crashes the driver process (`0x800706BA`).

## Status

**Stage 2: full telescope control**, tested against a simulated mount only. Slews
(equatorial and alt-az), abort, MoveAxis, pulse guiding, park/unpark, set park, find home
(mounts with homing sensors), tracking and tracking rates, RA/Dec rate offsets, guide
rates, site, date and refraction settings, meridian flip (SideOfPier) and
DestinationSideOfPier all work. Sync is off unless you turn it on (see Configuration).

It passes **ConformU 4.5.0** (the ASCOM conformance checker) against the simulated mount
with no errors, warnings or issues; see [docs/conformu/](docs/conformu/).

**Before you use it at night**, do the daylight checks in
[docs/safety-review.md, section 6](docs/safety-review.md#6-verify-on-the-real-mount),
and run ConformU against the mount.

Requirements: firmware 2.14.9 or newer, Python 3.11 or newer. The target platform is
Windows; the driver also runs on macOS and Linux.

## Install (Windows)

1. Install [uv](https://docs.astral.sh/uv/) (it also installs Python):
   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```
2. Get the code and install the dependencies:
   ```powershell
   git clone <repository URL> alpaca-10micron
   cd alpaca-10micron
   uv sync
   ```
3. Copy `config.example.yaml` to `config.yaml`, and set `mount.host` to the IP address of
   the mount.

## Run

```powershell
uv run alpaca-10micron --config config.yaml --log-file alpaca-10micron.log
```

The first time, Windows Firewall asks whether Python may accept connections. Allow it for
the networks your clients use. The driver uses TCP 11111 (HTTP) and UDP 32227 (discovery).

Open `http://localhost:11111/setup` to see the link status, the mount model, the firmware
and the last error.

## Connect a client

- **NINA**: Equipment → Telescope → choose the 10Micron device from the Alpaca devices
  (found by discovery), then connect.
- **Other ASCOM programs** (for example PHD2): in the ASCOM Platform 7 Chooser, turn on
  Alpaca discovery and select the device. The Chooser creates an Alpaca Dynamic Client,
  and the program sees it as a normal ASCOM telescope.

Several programs can connect at the same time. Each one connects and disconnects on its
own; the link to the mount closes when the last program disconnects.

## Configuration

See [config.example.yaml](config.example.yaml). The main settings:

| Setting | Meaning |
|---|---|
| `mount.host`, `mount.port` | Address of the mount. |
| `mount.io_timeout` | Seconds without a reply before the link counts as broken (default 10). |
| `server.host` | `0.0.0.0` accepts clients from the network. `127.0.0.1` accepts only programs on this PC. Alpaca has no login, so anyone who can reach this port can use the mount. |
| `server.port` | HTTP port (default 11111). |
| `device.allow_sync` | `false` (default): Sync is not offered. A wrong sync damages the mount's pointing, so turn it on only if your software needs it. |
| `device.use_keypad_park_position` | `true`: park at the position set on the keypad, and do not offer SetPark. |
| `device.slew_timeout`, `device.home_timeout` | Seconds a synchronous slew or a homing run may take (default 600). |
| `device.aperture_*`, `device.focal_length` | Optional optics values, in metres. |

## Safety

The rules, and how the vendor driver compares, are in
[docs/safety-review.md](docs/safety-review.md). In short:

- **No login.** Alpaca has no authentication: anyone who can reach the server port can
  move the mount. Use `server.host: 127.0.0.1` when all clients run on this PC, and keep
  the port closed to other networks.
- The mount's unattended meridian flip is turned **off** when the driver connects and when
  the last client disconnects. The driver never turns it on.
- The driver refuses to move the mount while it reports status 8 (motors inhibited),
  11 (needs user OK; the position may be wrong), 12-14 (special controller modes) or
  99 (error). Stopping an axis is always allowed.
- While parked, all motion raises InvalidWhileParked.
- If the link breaks while an axis moves (MoveAxis), the axes are stopped first thing on
  the next link. When the last client disconnects, moving axes are stopped too.
- Raw command pass-through (`CommandBlind/Bool/String`) is disabled.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

The fake mount also runs on its own, in a realistic mode (PC clock, computed sidereal time
and Alt/Az). Use it to try a client or to run ConformU without hardware:

```bash
uv run python tests/fake_mount.py --port 3490 --homing
```

The tests use a fake mount (`tests/fake_mount.py`) and need no hardware. It simulates
slews, parking, homing, tracking and guiding. The tests include the failures that crashed
the vendor driver (a mount that stops answering, and a dropped connection) and the motion
safety rules (`tests/test_motion.py`). `AlpacaDeviceAPI_v1.yaml` is the official Alpaca API description (MIT license,
ASCOM Initiative); a test checks that every telescope endpoint in it exists.

## Project layout

| Path | Contents |
|---|---|
| `src/alpaca_10micron/protocol.py` | Reply parsing for the 10Micron protocol (no I/O). |
| `src/alpaca_10micron/mount.py` | The TCP link: one command at a time, poller, automatic reconnect. |
| `src/alpaca_10micron/device.py` | Client connections and the values derived from the mount state. |
| `src/alpaca_10micron/api/` | Alpaca endpoints: common, telescope, management and setup. |
| `src/alpaca_10micron/discovery.py` | Alpaca discovery (UDP). |
| `docs/safety-review.md` | Comparison with the vendor driver, the motion safety rules, and the checks to do on the real mount. |
