# Safety review: vendor COM driver compared with this Alpaca driver

This review compares the safety behavior of the vendor's ASCOM COM driver (10Micron
driver 1.7.2.0, local server and Telescope DLL, read as decompiled code) with this
driver. It also checks this driver against the ASCOM Telescope interface (ITelescopeV4).
The review describes behavior only. No vendor code is copied into this project.

The review was made at **stage 1** (read-only). Section 3 lists the rules for motion;
**stage 2 implements all of them**. Section 3.0 shows where each rule is implemented
and tested, and section 6 lists what must still be checked on the real mount.

Status labels:

- **Done**: implemented and covered by a test (against the simulating fake mount).
- **Different on purpose**: this driver behaves differently, for the reason given.
- **Verify on the mount**: the fake mount cannot prove it (section 6).

## 1. Fixed during this review

| # | Vendor behavior | Gap found in this driver | Fix |
|---|---|---|---|
| 1 | Sends `:Suaf0#` (unattended flip off) on every connect, and again on disconnect. | Did not touch the flip setting. Another program or the keypad could leave it on, and the mount could then slew on its own. | Sends `:Suaf0#` each time the link opens. On the final disconnect it sends `:Suaf0#` again (best effort, at most 0.5 s). The driver never sends `:Suaf1#`. |
| 2 | Swaps East/West for firmware below 2.15.32 when the site latitude is negative (a mount bug, see `:pS#` in the protocol document). | Not corrected, and firmware 2.14.9 to 2.15.31 can connect. SideOfPier would be wrong on the southern hemisphere. | The same correction is in `SideOfPier` and `DeviceState`. |
| 3 | Accepts firmware versions that end in `SIMUL` (10Micron virtual mount). | The version parser rejected them, so the virtual mount could not be used for tests. | The suffix is accepted and recorded (`MountInfo.simulator`). |
| 4 | Limits SlewSettleTime to 32767. | No limit. The mount allows up to 99999 s, and ASCOM uses a 16-bit integer. | Limited to 32767. |
| 5 | DriverVersion is "major.minor". | Returned "0.1.0". ASCOM requires "n.n". | Returns "0.1". The full version stays in DriverInfo and the management API. |
| 6 | Ignores mount status 8 (motors inhibited), 11 (needs user OK) and 99 (error). | Also ignored them. Status 11 means the mount's position or alignment may be wrong. | Logs a warning once when the mount enters one of these states, and an info line when it leaves. Stage 2 must also refuse motion in these states (see 3.12). |

## 2. Already correct in stage 1

| Topic | Vendor driver | This driver |
|---|---|---|
| Network failure | One read timeout in the poller thread ends the whole server process (the crash that caused `0x800706BA`). | A failure closes the link and reconnects with a growing delay. Calls fail with `DriverException` (0x500) while the link is down, then work again. **Done** |
| Stale data after a failed command | The socket stays open, so a late reply can be read as the answer to the next command. | Any failed or interrupted exchange closes the link. The next link starts with a clean byte stream. **Done** |
| Stale cached values | Cache entries expire after 1.25 s. | Cached values are dropped when the link breaks, and values older than 5 s are refused. **Done** |
| Product check | Refuses a mount whose `:GVP#` reply does not start with "10micron". | Same. **Done** |
| Firmware check | Supports very old firmware through fallback paths. | Requires 2.14.9 or newer (`:Ginfo#` format), and says so in the error. **Different on purpose**: fewer code paths to get wrong. |
| Reply format | Ultra precision (`:U2#`) from 2.10. | Ultra precision always. **Done** |
| Raw commands | `CommandBlind/Bool/String` pass only commands from a validation table, unless the user allows unchecked commands. | All three return NotImplemented. No raw command reaches the mount. **Done** |
| NotConnected | Most members raise NotConnected when not connected. | All members except the identity and connection members raise NotConnected (0x407). **Done** |
| One client disconnecting | Reference count across clients in one process. | Connection state per ClientID. One client disconnecting does not affect another. The link closes when the last client disconnects. **Done** |
| Optics values | Default to 1 mm when not configured, which gives wrong values. | NotImplemented unless set in the configuration. **Different on purpose** |

## 3. Motion: rules and implementation

These are the vendor's safety checks that matter once the driver can move the mount, plus
the places where the vendor driver is wrong and must not be copied.

### 3.0 Implementation status (stage 2)

All tests are in `tests/test_motion.py` unless noted. The code is in
`src/alpaca_10micron/device.py`; its comments refer to these section numbers.

| Rule | Status | Tests |
|---|---|---|
| 3.1 Park state guards | **Done** | `test_park_and_unpark` |
| 3.2 Slew results | **Done** | `test_mount_refuses_slew` |
| 3.3 Tracking state and slews | **Done** (ASCOM rule) | `test_tracking_rules_for_slews` |
| 3.4 Slewing true at once | **Done**: state is read right after the command; a 1 s grace window covers a mount that reports the movement late, and ends as soon as the mount reports it | `test_async_slew_sets_target_atomically_and_reports_slewing`, `test_park_and_unpark` |
| 3.5 Shared mount target | **Done**: `:Sr`, `:Sd` and `:MS#` in one locked session | same test as 3.4 |
| 3.6 AbortSlew | **Done**: always `:Q#`; `:STOP#` while parking, unparking or homing | `test_abort_slew_always_sends_q` |
| 3.7 MoveAxis | **Done**, plus: axes moving when the link breaks are stopped first thing on the next link, and when the last client disconnects | `test_move_axis_*`, `test_gm4000_ra_axis_limit`, `test_moving_axis_is_stopped_*` |
| 3.8 PulseGuide | **Done**; durations above 9999 ms give InvalidValue | `test_pulse_guide*` |
| 3.9 Sync | **Done**; off unless `device.allow_sync: true` | `test_sync`, `test_api.py::test_not_implemented_members` |
| 3.10 Park, Unpark, FindHome, AtHome | **Done**; Park, Unpark and FindHome are asynchronous (Platform 7); AtHome needs a completed FindHome on this link and status 1 | `test_park_*`, `test_find_home_*`, `test_set_park` |
| 3.11 Settings | **Done**; King rate offset sign corrected (see 3.11) | `test_tracking_*`, `test_guide_rate`, `test_site_and_time_setters`, `test_setter_ranges`, `test_side_of_pier_flip` |
| 3.12 Unsafe mount status | **Done**; stopping an axis is always allowed | `test_unsafe_status_blocks_motion` |
| 3.13 Unattended flip | **Done** (stays off) | `test_mount.py::test_unattended_flip_*` |
| 3.14 Network exposure | Documented in the README | - |

### 3.1 Park state guards

- Status 5 (parked): Slew\*, Sync\*, MoveAxis, PulseGuide, FindHome and AbortSlew must
  raise **InvalidWhileParked (0x408)**. The vendor driver does this for all of them.
- Status 2 (parking) or 3 (unparking): raise **InvalidOperation (0x40B)**. AbortSlew is the
  exception: it must stop the movement with `:STOP#`.

### 3.2 Slew results

`:MS#`, `:MSnf#` and `:MA#` reply `0` on success. Map the error codes as follows:

| Reply | Meaning | ASCOM error |
|---|---|---|
| 1 | Below the lower limit | InvalidOperation (0x40B) |
| 2 | Above the high limit | InvalidOperation (0x40B) |
| 3 | Cannot perform the slew | InvalidOperation (0x40B) |
| 4 | Mount parked | InvalidWhileParked (0x408) |
| 5 | Other side of the meridian not allowed | InvalidOperation (0x40B) |

### 3.3 Tracking state and slews

ASCOM: SlewToCoordinates/SlewToTarget with `Tracking = False`, and SlewToAltAz with
`Tracking = True`, must raise InvalidOperation. By default the vendor driver changes the
slew type instead (setting "slew in wrong tracking state" = 2). **Different on purpose**:
follow the ASCOM rule, because ConformU checks it.

### 3.4 Slewing must be true at once

ASCOM: `Slewing` must be `True` as soon as an async slew method returns. The vendor driver
drops its cache after each motion command. This driver must poll the state under the same
lock, right after the motion command, before the method returns.

### 3.5 Mount target is shared

The protocol document warns that the mount has one target for all interfaces (keypad,
serial, LAN). Send `:Sr`/`:Sd` and `:MS#` together under one lock. Keep TargetRightAscension
and TargetDeclination in the driver (as stage 1 does now); do not read them back from the
mount. DestinationSideOfPier (`:GTsid#`) also overwrites the mount target.

### 3.6 AbortSlew

- Parking or unparking: send `:STOP#`.
- Parked: raise InvalidWhileParked.
- Otherwise: **always** send `:Q#`. The vendor driver skips `:Q#` when the status is 0
  (tracking), so a MoveAxis movement may not stop. Do not copy this.
- Clear the MoveAxis state, then wait for the mount to stop (up to 5 s) without blocking
  the event loop.

### 3.7 MoveAxis

- Axis 2 (tertiary) is not supported: CanMoveAxis(2) = False and InvalidValue.
- Limit the rate to `:GMsb#` (maximum slew rate). For GM4000 mounts the vendor uses 75 % of
  it on the RA axis. Larger values raise InvalidValue (0x401).
- The vendor turns off speed correction (`:GSC#`, `:SSC0#`) during MoveAxis and restores it
  afterwards. Do the same.
- Rate 0 stops the axis. The vendor sends only `:Qe#` (stop eastward) for the RA axis, in
  both directions. Check this on the mount; sending `:Qe#:Qw#` (and `:Qn#:Qs#`) is safer.
- `Slewing` must be `True` while an axis moves.

### 3.8 PulseGuide

- Firmware 2.10 or newer accepts up to 9999 ms (`:MgnXXXX#`). The vendor splits pulses
  longer than 999 ms with timers; this is not needed with firmware 2.14.9 or newer.
- A negative duration must raise InvalidValue. The vendor sets it to 0 without an error.
- `IsPulseGuiding` = a driver timer **or** `:Gpgc#` is not 0.

### 3.9 Sync

- Off by default (vendor default too), because a wrong sync damages the alignment model.
  Add a configuration setting `allow_sync`, default `false`.
- Needs tracking on, and not parked.
- Send `:CMCFG0#` (offset mode), then `:CM#`. Check for the exact reply
  `Coordinates     matched        `; anything else means the sync failed.
- "Sync as alignment point" (`:CMS#`) changes the model. Keep it off unless configured.

### 3.10 Park, Unpark, FindHome, AtHome

- Park: if already parked, return. Use `:PsX#` (park position saved with SetPark). If it
  replies 3, fall back to `:hP#` (keypad park position). Afterwards, check that the status
  is 5. The vendor waits in a busy loop that uses a full CPU core; wait with `asyncio.sleep`
  instead.
- Unpark: `:PO#`, then check that the status is no longer 5.
- FindHome: only when `:GCFG#` reports homing sensors. **The vendor implementation is
  wrong**: it returns while homing is still in progress (`:h?#` = 2), and it loops forever
  if `:h?#` stays 1. Implement it as: send `:hF#`, wait until `:h?#` is 1 (done) or 0
  (failed), with a timeout.
- AtHome: the vendor returns `:h?#` = 1 ("home search found"). That value stays 1 after the
  mount moves away, which breaks the ASCOM rule that AtHome is False after any movement.
  Keep a driver flag: True after FindHome completes, False after any motion command.

### 3.11 Settings that change the mount

| Member | Check |
|---|---|
| Tracking (set) | `:AP#` / `:AL#`. Firmware 2.12.26 to 2.15.34 does not restart tracking after `:STOP#`; send `:AL#:AP#` there. |
| TrackingRate | Sidereal, lunar, solar, King. Validate the enum value. **King**: the vendor driver uses an RA offset of -0.0041"/s. By its own convention (solar = +0.041"/s, because the Sun's RA increases and the mount tracks slower), King (15.0369"/s, slower than sidereal) needs **+0.0041**. This driver uses +0.0041. |
| GuideRate\* | The protocol limits the guide rate to sidereal speed (about 15.041"/s). The vendor driver does not check; this driver must raise InvalidValue for values outside 0 to sidereal speed. |
| SiteLatitude / SiteLongitude | -90..90 and -180..180. Longitude is negated for the mount (east is negative there). |
| SiteElevation | The protocol allows -1000.0 to 9999.9 m. The vendor driver accepts -300 to 9000. |
| UTCDate | `:SJD`. Reply 0 means invalid. |
| SideOfPier (set) | German mounts only. `:FLIP#` reply 0 means InvalidValue ("cannot flip here"). |
| DoesRefraction | `:SREF0#`/`:SREF1#`; reply must be 1. |

### 3.12 Mount status before motion

Refuse motion commands with InvalidOperation and a clear message while the status is 8
(motors inhibited), 11 (needs user OK) or 99 (error). The vendor driver does not check this.
For status 11, the protocol document says not to trust the position or alignment.

### 3.13 Unattended flip

Keep it off, as now. If a later version offers it, copy the vendor's limit: the user must
confirm it, and the confirmation is valid for 10 minutes only.

### 3.14 Network exposure

Alpaca has no authentication. Once motion is implemented, anyone who can reach the server
port can move the mount. Document this, and suggest `server.host: 127.0.0.1` when all
clients run on the same PC.

## 4. ASCOM compliance (stage 1 and stage 2)

| Requirement | Status |
|---|---|
| Parameter names not case-sensitive; PUT body before query string | **Done** (tested) |
| ClientTransactionID echoed; missing or invalid counts as 0 | **Done** (tested) |
| ServerTransactionID increases, unsigned 32-bit, never 0 | **Done** (tested) |
| ASCOM errors as HTTP 200 with ErrorNumber/ErrorMessage | **Done** (tested) |
| Bad device number, unknown member or bad parameter value: HTTP 400 with text | **Done** (tested) |
| Every telescope and common endpoint in the Alpaca spec exists | **Done** (tested against `AlpacaDeviceAPI_v1.yaml`) |
| InterfaceVersion 4 with Connect, Disconnect, Connecting, DeviceState | **Done** (tested) |
| DriverVersion "n.n" | **Done** (tested) |
| NotConnected for all members except identity and connection members | **Done** (tested); confirm with ConformU |
| A failed async Connect is reported (here: Connecting raises the error once) | **Done** (tested); confirm with ConformU |
| `Can*` flags match the implemented members (sync, homing, set park and set pier side depend on the configuration and the mount) | **Done** (tested) |
| AxisRates: one range 0..slew rate for axes 0 and 1, empty for axis 2; invalid axis gives InvalidValue | **Done** (tested) |
| InvalidValue for out-of-range coordinates, rates, durations, enums and site values | **Done** (tested) |
| InvalidWhileParked for motion while parked | **Done** (tested) |
| Park, Unpark and FindHome asynchronous, with Slewing True while they run | **Done** (tested); confirm with ConformU |
| RightAscensionRate/DeclinationRate combined with a non-sidereal TrackingRate (allowed here, as in the vendor driver) | Confirm with ConformU |
| TrackingRates contains sidereal and the current TrackingRate | **Done** (tested) |
| UTCDate as ISO 8601 UTC with "Z" | **Done** (tested) |
| Target RA/Dec: ValueNotSet before they are set; range checks | **Done** (tested) |
| Management API and setup pages | **Done** (tested) |
| Discovery on UDP 32227 | **Done** (IPv4 tested; IPv6 not implemented) |
| ConformU conformance run | **Not done yet** (stage 3) |

## 5. Vendor features left out on purpose

- J2000 conversion: the driver reports the mount's own system (topocentric, JNow).
  Clients such as NINA convert.
- Raw command pass-through and the proprietary actions (model building, buffers).
- Refraction updates from a file.
- The user slew-rate cap (`:Sw`) and the choice of keypad park position. These can come
  later as configuration settings.

## 6. Verify on the real mount

The fake mount follows the protocol document, so it cannot show where the document and
the real mount differ. Check these points in daylight, with a clear area around the mount:

1. **RA rate sign.** Set `RightAscensionRate` to a small positive value (for example 0.01)
   and watch the reported RA: it must increase. This confirms the `:RR` sign convention
   that the driver shares with the vendor driver.
2. **King rate.** Select King; the tracking rate (`:GT#`) must be slightly below sidereal.
3. **MoveAxis direction and stop.** A positive rate on axis 0 must move west, and on axis 1
   north (vendor convention). Rate 0 must stop the axis (`:Qe#`+`:Qw#`, `:Qn#`+`:Qs#`).
4. **SetPark reply.** The protocol document is unclear about the `:PyX#` reply; the driver
   expects `1`, as the vendor driver does. If SetPark reports an error, check the log.
5. **Alt-az target format.** The driver sends `:SzDDD*MM:SS.S#` as in the protocol
   document; the vendor driver uses `:` as the first separator. Check one SlewToAltAz.
6. **Slewing during park and homing.** `Slewing` must stay True until `AtPark` is True,
   and until homing ends.
7. **Link loss while MoveAxis runs.** Pull the network cable while an axis moves at a slow
   rate, then reconnect: the log must show the stop commands on the new link.
8. **ConformU** against the real mount (it slews to many positions).
