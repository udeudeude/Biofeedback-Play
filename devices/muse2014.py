from __future__ import annotations

import glob
import re
import struct
import sys
import time
from dataclasses import dataclass
from typing import Callable

import serial
from serial.tools import list_ports


MUSE_EEG_RATE = 500.0
MUSE_ACCEL_RATE = 50.0
DEFAULT_AFE_GAIN = 1961.0
CHANNEL_NAMES = ("TP9", "FP1", "FP2", "TP10")

try:
    if sys.platform == "darwin":
        import IOBluetooth
        import objc
        from CoreFoundation import CFRunLoopRunInMode, kCFRunLoopDefaultMode
        from Foundation import NSObject

        HAVE_NATIVE_MAC_BLUETOOTH = True
    else:
        HAVE_NATIVE_MAC_BLUETOOTH = False
except ImportError:
    HAVE_NATIVE_MAC_BLUETOOTH = False


def to_signed_10bit(value: int) -> int:
    value &= 0x3FF
    return value - 0x400 if value & 0x200 else value


def decode_eeg_packet(packet: bytes, afe_gain: float = DEFAULT_AFE_GAIN) -> dict:
    if len(packet) != 9 or packet[0] != 0xE0:
        raise ValueError("Not a Muse uncompressed EEG packet")

    raw = struct.unpack(">HHHH", packet[1:])
    gain = afe_gain if afe_gain > 0 else DEFAULT_AFE_GAIN

    # This follows the clean-room Muse 2014 implementation by Matthew Piercey.
    # The original author describes the microvolt scaling as approximate.
    scale = (3.3 / 65535.0) * (1_000_000.0 / gain) * (65535.0 / 24552.0)
    microvolts = tuple(value * scale for value in raw)
    return {"raw": raw, "microvolts": microvolts}


def decode_accelerometer_packet(packet: bytes) -> tuple[int, int, int]:
    if len(packet) != 5 or packet[0] != 0xA0:
        raise ValueError("Not a Muse accelerometer packet")

    word = int.from_bytes(packet[1:], byteorder="little")
    x = to_signed_10bit(word)
    y = to_signed_10bit(word >> 10)
    z = to_signed_10bit(word >> 20)
    return x, y, z


def decode_battery_packet(packet: bytes) -> dict:
    if len(packet) != 9 or packet[0] != 0xB0:
        raise ValueError("Not a Muse battery packet")

    percentage_x100, fuel_gauge_mv, adc_mv, temperature = struct.unpack(
        ">HHHH", packet[1:]
    )
    return {
        "percentage": percentage_x100 / 100.0,
        "fuel_gauge_mv": fuel_gauge_mv,
        "adc_mv": adc_mv,
        "temperature": temperature,
    }


class MusePacketParser:
    """Parse the Muse 2014 preset-AD byte stream.

    Supported packets:
      E0 + 8 bytes: uncompressed four-channel EEG
      A0 + 4 bytes: accelerometer
      B0 + 8 bytes: battery
      FF FF AA 55: sync marker
    """

    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[tuple[str, bytes]]:
        self.buffer.extend(data)
        packets: list[tuple[str, bytes]] = []

        while self.buffer:
            if len(self.buffer) >= 4 and self.buffer[:4] == b"\xff\xff\xaa\x55":
                del self.buffer[:4]
                continue

            marker = self.buffer[0]
            if marker == 0xE0:
                length = 9
                kind = "eeg"
            elif marker == 0xA0:
                length = 5
                kind = "accelerometer"
            elif marker == 0xB0:
                length = 9
                kind = "battery"
            else:
                del self.buffer[0]
                continue

            if len(self.buffer) < length:
                break

            packet = bytes(self.buffer[:length])
            del self.buffer[:length]
            packets.append((kind, packet))

        if len(self.buffer) > 32768:
            del self.buffer[:-64]

        return packets


def list_muse_serial_ports() -> list[dict]:
    found: dict[str, dict] = {}

    for port in list_ports.comports():
        device = port.device
        description = port.description or ""
        hwid = port.hwid or ""
        text = f"{device} {description} {hwid}".lower()
        likely = "muse" in text
        found[device] = {
            "device": device,
            "description": description,
            "hwid": hwid,
            "likely_muse": likely,
        }

    for pattern in ("/dev/cu.*", "/dev/tty.*"):
        for device in glob.glob(pattern):
            if device in found:
                continue
            likely = "muse" in device.lower()
            found[device] = {
                "device": device,
                "description": "macOS serial port",
                "hwid": "",
                "likely_muse": likely,
            }

    return sorted(
        found.values(),
        key=lambda item: (
            0 if item["likely_muse"] else 1,
            item["device"].lower(),
        ),
    )


@dataclass
class MuseStatus:
    version: str = ""
    status_text: str = ""
    afe_gain: float | None = None
    stage: str = "Not started"
    transport: str = ""
    rfcomm_services: str = ""
    rfcomm_channel: int | None = None
    passive_probe: str = ""


if HAVE_NATIVE_MAC_BLUETOOTH:

    class _RFCOMMDelegate(NSObject):
        def init(self):
            self = objc.super(_RFCOMMDelegate, self).init()
            self.buffer = bytearray()
            self.closed = False
            return self

        def rfcommChannelData_data_length_(self, channel, data, length):
            payload = bytes(data)
            self.buffer.extend(payload[: int(length)])

        def rfcommChannelClosed_(self, channel):
            self.closed = True

        def rfcommChannelOpenComplete_status_(self, channel, status):
            # The callback is intentionally present. IOBluetooth's synchronous
            # open behaves more reliably when a delegate can receive it.
            pass


def _pump_mac_run_loop(seconds: float) -> None:
    if not HAVE_NATIVE_MAC_BLUETOOTH:
        return

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.05, False)


class NativeMacRFCOMM:
    """Serial-like wrapper around macOS IOBluetooth RFCOMM.

    macOS may create /dev/cu.Muse-* for the paired MU-01 without keeping an
    actual RFCOMM data session alive. Opening the RFCOMM channel through
    IOBluetooth explicitly creates the baseband link and the channel instead
    of relying on that virtual serial node.
    """

    SPP_UUID16 = 0x1101

    def __init__(
        self,
        muse_name: str,
        timeout: float = 0.25,
        channel_id: int | None = None,
    ) -> None:
        if not HAVE_NATIVE_MAC_BLUETOOTH:
            raise RuntimeError("Native macOS Bluetooth support is not installed")

        self.timeout = timeout
        self.device = self._find_paired_device(muse_name)
        self.delegate = _RFCOMMDelegate.alloc().init()
        self.channel = None
        self.channel_id = channel_id
        self.service_name = ""
        self._open()

    @staticmethod
    def _find_paired_device(muse_name: str):
        paired = IOBluetooth.IOBluetoothDevice.pairedDevices() or []
        exact = []
        loose = []

        wanted = muse_name.lower()
        for device in paired:
            name = str(device.name() or "")
            if name.lower() == wanted:
                exact.append(device)
            elif wanted in name.lower() or (
                wanted.startswith("muse-") and name.lower().startswith("muse-")
            ):
                loose.append(device)

        candidates = exact or loose
        if not candidates:
            names = [str(device.name() or "") for device in paired]
            raise RuntimeError(
                f"No paired classic-Bluetooth Muse matching {muse_name!r}. "
                f"Paired devices visible to IOBluetooth: {names}"
            )
        return candidates[0]

    def _ensure_baseband(self, reset: bool = False) -> None:
        if reset:
            try:
                self.device.closeConnection()
            except Exception:
                pass
            _pump_mac_run_loop(0.4)

        if reset or not bool(self.device.isConnected()):
            status = int(self.device.openConnection())
            if status != 0:
                raise RuntimeError(
                    "macOS could not open a Bluetooth baseband connection to the Muse "
                    f"(IOReturn 0x{status & 0xFFFFFFFF:08x})"
                )
            _pump_mac_run_loop(1.0)

    @staticmethod
    def _service_records(device):
        try:
            device.performSDPQuery_(None)
            _pump_mac_run_loop(2.0)
        except Exception:
            pass

        for accessor in ("services", "getServices"):
            try:
                value = getattr(device, accessor)
                records = value() if callable(value) else value
                if records is not None:
                    return list(records)
            except Exception:
                continue
        return []

    @classmethod
    def discover_rfcomm_services(cls, muse_name: str) -> list[dict]:
        """Return every SDP service on the paired Muse that exposes RFCOMM.

        Historic muse-io connected by Bluetooth device name rather than a
        pre-created tty. Enumerating every RFCOMM SDP record lets us reproduce
        that behavior instead of assuming the generic Serial Port Profile
        record is the right application channel.
        """
        if not HAVE_NATIVE_MAC_BLUETOOTH:
            return [{"channel": 1, "name": "fallback"}]

        device = cls._find_paired_device(muse_name)

        try:
            if not bool(device.isConnected()):
                status = int(device.openConnection())
                if status == 0:
                    _pump_mac_run_loop(1.0)
        except Exception:
            pass

        found: list[dict] = []
        seen: set[int] = set()

        for record in cls._service_records(device):
            try:
                result = record.getRFCOMMChannelID_(None)
                if not (isinstance(result, tuple) and len(result) == 2):
                    continue
                status, channel_id = result
                if int(status) != 0:
                    continue
                channel_id = int(channel_id)
                if channel_id in seen:
                    continue

                try:
                    service_name = str(record.getServiceName() or "")
                except Exception:
                    service_name = ""

                seen.add(channel_id)
                found.append(
                    {
                        "channel": channel_id,
                        "name": service_name or "unnamed RFCOMM service",
                    }
                )
            except Exception:
                continue

        # Prefer an explicitly named serial service, then unnamed channels,
        # while preserving the actual channel IDs advertised by the headset.
        found.sort(
            key=lambda item: (
                0 if "serial" in item["name"].lower() else 1,
                item["channel"],
            )
        )

        if 1 not in seen:
            found.append({"channel": 1, "name": "channel 1 fallback"})

        return found

    def _resolve_rfcomm_channel(self) -> int:
        if self.channel_id is not None:
            return int(self.channel_id)

        services = self.discover_rfcomm_services(str(self.device.name() or "Muse"))
        return int(services[0]["channel"]) if services else 1

    def _open_once(self, channel_id: int):
        delegate = _RFCOMMDelegate.alloc().init()
        result = self.device.openRFCOMMChannelSync_withChannelID_delegate_(
            None, channel_id, delegate
        )
        if isinstance(result, tuple) and len(result) == 2:
            status, channel = result
        else:
            raise RuntimeError(
                "Unexpected result from macOS IOBluetooth RFCOMM open"
            )
        if int(status) != 0 or channel is None:
            return int(status), None, delegate
        return 0, channel, delegate

    def _open(self) -> None:
        last_status = None

        for reset in (False, True):
            self._ensure_baseband(reset=reset)
            channel_id = self._resolve_rfcomm_channel()
            self.channel_id = channel_id
            status, channel, delegate = self._open_once(channel_id)
            last_status = status
            if status == 0 and channel is not None:
                self.channel = channel
                self.delegate = delegate
                _pump_mac_run_loop(0.3)
                return

            try:
                if channel is not None:
                    channel.closeChannel()
            except Exception:
                pass
            _pump_mac_run_loop(0.5)

        raise RuntimeError(
            "macOS found the paired Muse but could not open its RFCOMM serial "
            f"channel (IOReturn 0x{(last_status or 0) & 0xFFFFFFFF:08x})"
        )

    @property
    def in_waiting(self) -> int:
        _pump_mac_run_loop(0.02)
        return len(self.delegate.buffer)

    def reset_input_buffer(self) -> None:
        self.delegate.buffer.clear()
        _pump_mac_run_loop(0.02)

    def flush(self) -> None:
        return

    def write(self, data: bytes) -> int:
        if self.channel is None or self.delegate.closed:
            raise RuntimeError("Muse RFCOMM channel is closed")

        payload = bytes(data)
        status = int(self.channel.writeSync_length_(payload, len(payload)))
        if status != 0:
            raise RuntimeError(
                "Muse RFCOMM write failed "
                f"(IOReturn 0x{status & 0xFFFFFFFF:08x})"
            )
        return len(payload)

    def read(self, size: int) -> bytes:
        deadline = time.monotonic() + self.timeout
        while not self.delegate.buffer and not self.delegate.closed:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _pump_mac_run_loop(min(0.05, remaining))

        if not self.delegate.buffer and self.delegate.closed:
            raise RuntimeError("Muse closed the RFCOMM channel")

        out = bytes(self.delegate.buffer[:size])
        del self.delegate.buffer[:size]
        return out

    def close(self) -> None:
        try:
            if self.channel is not None:
                self.channel.closeChannel()
                _pump_mac_run_loop(0.15)
        except Exception:
            pass
        self.channel = None

        try:
            if self.device is not None and bool(self.device.isConnected()):
                self.device.closeConnection()
                _pump_mac_run_loop(0.15)
        except Exception:
            pass


class Muse2014SerialClient:
    def __init__(
        self,
        port: str,
        on_eeg: Callable[[dict], None],
        on_accelerometer: Callable[[tuple[int, int, int]], None],
        on_battery: Callable[[dict], None],
        on_status: Callable[[MuseStatus], None] | None = None,
    ) -> None:
        self.port = port
        self.on_eeg = on_eeg
        self.on_accelerometer = on_accelerometer
        self.on_battery = on_battery
        self.on_status = on_status
        self.serial = None
        self.parser = MusePacketParser()
        self.status = MuseStatus()
        self.command_terminator = b"\r"
        self.passive_stream_active = False

    def _notify_status(self, stage: str) -> None:
        self.status.stage = stage
        if self.on_status:
            self.on_status(self.status)

    def _write_command(self, command: str) -> None:
        if self.serial is None:
            raise RuntimeError("Muse connection is not open")
        self.serial.write(command.encode("ascii") + self.command_terminator)
        self.serial.flush()

    def _read_text(self, seconds: float) -> str:
        if self.serial is None:
            return ""

        end = time.monotonic() + seconds
        chunks: list[bytes] = []
        while time.monotonic() < end:
            waiting = self.serial.in_waiting
            if waiting:
                chunks.append(self.serial.read(waiting))
            else:
                time.sleep(0.03)
        return b"".join(chunks).decode("utf-8", errors="ignore").strip()

    def _dispatch_packet(self, kind: str, packet: bytes) -> None:
        if kind == "eeg":
            self.on_eeg(
                decode_eeg_packet(
                    packet,
                    afe_gain=self.status.afe_gain or DEFAULT_AFE_GAIN,
                )
            )
        elif kind == "accelerometer":
            self.on_accelerometer(decode_accelerometer_packet(packet))
        elif kind == "battery":
            self.on_battery(decode_battery_packet(packet))

    def _probe_passive_stream(self, seconds: float = 3.0) -> bool:
        """Listen before sending any command.

        Contemporary documentation of a working 2014 Muse on macOS reports
        that opening the Muse-RN-iAP serial endpoint at 115200 immediately
        produced a data stream. Probe for that behavior before sending 'h',
        because stopping a stream first may destroy the most useful evidence.
        """
        if self.serial is None:
            return False

        self._notify_status("Listening for Muse data before sending commands")
        end = time.monotonic() + seconds
        captured = bytearray()
        counts = {"eeg": 0, "accelerometer": 0, "battery": 0}

        while time.monotonic() < end and len(captured) < 65536:
            try:
                data = self.serial.read(4096)
            except Exception:
                break
            if not data:
                continue

            captured.extend(data)
            for kind, packet in self.parser.feed(data):
                counts[kind] += 1
                self._dispatch_packet(kind, packet)

        preview = bytes(captured[:32]).hex(" ") if captured else "none"
        self.status.passive_probe = (
            f"{len(captured)} bytes before commands; "
            f"EEG packets {counts['eeg']}, accelerometer {counts['accelerometer']}, "
            f"battery {counts['battery']}; first bytes: {preview}"
        )
        self._notify_status("Passive Muse serial probe complete")

        recognized = sum(counts.values())
        if recognized:
            self.passive_stream_active = True
            self.status.version = "Legacy Mac stream active; version handshake not required"
            self._notify_status("Muse data arrived before configuration")
            return True
        return False

    @staticmethod
    def _muse_name_from_port(port: str) -> str:
        name = port.rsplit("/", 1)[-1]
        for prefix in ("cu.", "tty."):
            if name.startswith(prefix):
                name = name[len(prefix) :]
        return name

    @staticmethod
    def _mac_serial_candidates(port: str) -> list[str]:
        """Return likely macOS Muse serial endpoints in useful order.

        Historical MU-01 Mac setups used paths such as /dev/tty.Muse-RN-iAP
        at 115200 baud. Modern macOS may present a differently named paired
        Muse endpoint, so prefer tty endpoints but probe both tty and cu names.
        """
        candidates: list[str] = []

        base = port.rsplit("/", 1)[-1]
        if base.startswith("cu."):
            sibling = "/dev/tty." + base[3:]
            if sibling not in candidates:
                candidates.append(sibling)
        elif base.startswith("tty."):
            if port not in candidates:
                candidates.append(port)
            sibling = "/dev/cu." + base[4:]
            if sibling not in candidates:
                candidates.append(sibling)

        for pattern in ("/dev/tty.Muse*", "/dev/cu.Muse*"):
            for candidate in sorted(glob.glob(pattern)):
                if candidate not in candidates:
                    candidates.append(candidate)

        if port not in candidates:
            candidates.append(port)

        return candidates

    def _open_virtual_serial_transport(self, port: str | None = None) -> None:
        target = port or self.port
        self.status.transport = f"macOS Bluetooth serial: {target}"
        self._notify_status(f"Opening Bluetooth serial port {target}")
        self.serial = serial.Serial(
            target,
            baudrate=115200,
            timeout=0.25,
            write_timeout=1.0,
        )

    def _version_handshake(
        self,
        attempts: int = 3,
        terminator: bytes = b"\r",
    ) -> str:
        self.command_terminator = terminator
        # The RFCOMM channel can report open before the physical headset has
        # finished settling. Give it a moment before configuration commands.
        self._notify_status("Waiting for Bluetooth link")
        time.sleep(1.0)
        self.serial.reset_input_buffer()

        self._notify_status("Stopping any previous Muse stream")
        self._write_command("h")
        time.sleep(1.0)
        self.serial.reset_input_buffer()

        for attempt in range(1, attempts + 1):
            self._notify_status(f"Version handshake {attempt}/{attempts}")
            self._write_command("v 2")
            version = self._read_text(1.2)
            if version:
                return version
            time.sleep(0.5)
        return ""

    def _open_mac_serial_with_handshake(self) -> str:
        """Try the serial endpoints used by historic Mac Muse workflows first."""
        candidates = self._mac_serial_candidates(self.port)
        tried: list[str] = []

        for candidate in candidates:
            if not glob.glob(candidate):
                continue

            for terminator, label in ((b"\r", "CR"), (b"\r\n", "CRLF")):
                tried.append(f"{candidate} ({label})")
                self.status.transport = f"macOS Bluetooth serial: {candidate}"
                self._notify_status(
                    f"Trying legacy Muse serial path {candidate} ({label})"
                )

                try:
                    self._open_virtual_serial_transport(candidate)

                    # Historic Mac MU-01 setups could begin streaming as soon
                    # as the tty endpoint opened. Listen first, before 'h'.
                    if self._probe_passive_stream(seconds=3.0):
                        return self.status.version

                    version = self._version_handshake(
                        attempts=2,
                        terminator=terminator,
                    )
                    if version:
                        return version
                except Exception:
                    pass

                if self.serial is not None:
                    try:
                        self.serial.close()
                    except Exception:
                        pass
                    self.serial = None
                time.sleep(0.3)

        raise RuntimeError(
            "No macOS Muse serial endpoint answered the version handshake. "
            "Tried: " + ", ".join(tried)
        )

    def _open_native_mac_with_handshake(self) -> str:
        muse_name = self._muse_name_from_port(self.port)
        self.status.transport = "Native macOS IOBluetooth RFCOMM"

        services = NativeMacRFCOMM.discover_rfcomm_services(muse_name)
        self.status.rfcomm_services = ", ".join(
            f"{item['channel']} ({item['name']})" for item in services
        )
        self._notify_status(
            f"Found {len(services)} RFCOMM candidate"
            + ("" if len(services) == 1 else "s")
        )

        tried: list[str] = []
        for item in services:
            channel_id = int(item["channel"])
            service_name = str(item["name"])
            tried.append(f"{channel_id} ({service_name})")
            self.status.rfcomm_channel = channel_id
            self._notify_status(
                f"Trying RFCOMM channel {channel_id}: {service_name}"
            )

            try:
                self.serial = NativeMacRFCOMM(
                    muse_name,
                    timeout=0.25,
                    channel_id=channel_id,
                )
                version = self._version_handshake(attempts=2)
                if version:
                    return version
            except Exception:
                pass

            if self.serial is not None:
                try:
                    self.serial.close()
                except Exception:
                    pass
                self.serial = None
            time.sleep(0.4)

        self.status.rfcomm_channel = None
        self._notify_status("No advertised RFCOMM channel answered as Muse")
        raise RuntimeError(
            "The paired Muse advertises RFCOMM service(s), but none answered "
            "the Muse version handshake. Tried: " + ", ".join(tried)
        )

    def open_and_configure(self) -> MuseStatus:
        if sys.platform == "darwin":
            serial_error = ""
            try:
                version = self._open_mac_serial_with_handshake()
            except Exception as exc:
                serial_error = str(exc)
                version = ""

            if not version and HAVE_NATIVE_MAC_BLUETOOTH:
                self._notify_status(
                    "Legacy Mac serial path did not answer; trying raw RFCOMM"
                )
                try:
                    version = self._open_native_mac_with_handshake()
                except Exception as exc:
                    raise RuntimeError(
                        serial_error + " | Native RFCOMM fallback: " + str(exc)
                    ) from exc
        else:
            self._open_virtual_serial_transport()
            version = self._version_handshake(attempts=3, terminator=b"\r\n")

        self.status.version = version
        if self.passive_stream_active:
            return self.status

        if not version:
            self._notify_status("RFCOMM opened, but Muse did not answer")
            raise RuntimeError(
                "The Bluetooth RFCOMM channel opened, but the Muse did not answer "
                "the version handshake."
            )

        self._notify_status("Muse answered version handshake")

        # Interaxon's platform enum: iOS 1, Android 2, Windows 3, Mac 4, Linux 5.
        self._write_command("r 4")
        time.sleep(0.2)

        # Preset AD provides uncompressed 16-bit EEG plus accelerometer and battery.
        self._notify_status("Loading raw EEG preset")
        self._write_command("% AD")
        time.sleep(0.2)

        self._notify_status("Requesting Muse status")
        self._write_command("?")
        self.status.status_text = self._read_text(2.0)
        match = re.search(
            r"afe_gain:\s*([0-9]+(?:\.[0-9]+)?)",
            self.status.status_text,
            flags=re.IGNORECASE,
        )
        if match:
            self.status.afe_gain = float(match.group(1))

        self._notify_status("Starting Muse stream")
        self._write_command("s")
        time.sleep(1.0)
        self._notify_status("Waiting for Muse data")
        return self.status

    def run(self, should_stop: Callable[[], bool]) -> None:
        if self.serial is None:
            self.open_and_configure()

        next_keepalive = time.monotonic() + 5.0

        while not should_stop():
            now = time.monotonic()
            if now >= next_keepalive:
                self._write_command("k")
                next_keepalive = now + 5.0

            data = self.serial.read(4096)
            if not data:
                continue

            for kind, packet in self.parser.feed(data):
                self._dispatch_packet(kind, packet)

    def close(self) -> None:
        if self.serial is None:
            return
        try:
            self._write_command("h")
            time.sleep(0.1)
        except Exception:
            pass
        try:
            self.serial.close()
        finally:
            self.serial = None
