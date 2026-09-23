from __future__ import annotations

import glob
import re
import struct
import time
from dataclasses import dataclass
from typing import Callable

import serial
from serial.tools import list_ports


MUSE_EEG_RATE = 500.0
MUSE_ACCEL_RATE = 50.0
DEFAULT_AFE_GAIN = 1961.0
CHANNEL_NAMES = ("TP9", "FP1", "FP2", "TP10")


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

    for device in glob.glob("/dev/cu.*"):
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
    afe_gain: float = DEFAULT_AFE_GAIN


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
        self.serial: serial.Serial | None = None
        self.parser = MusePacketParser()
        self.status = MuseStatus()

    def _write_command(self, command: str) -> None:
        if self.serial is None:
            raise RuntimeError("Muse serial connection is not open")
        self.serial.write((command + "\r\n").encode("ascii"))
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

    def open_and_configure(self) -> MuseStatus:
        # A baud rate is required by pyserial. For a macOS Bluetooth SPP virtual
        # serial device the RFCOMM transport, not this value, determines the link.
        self.serial = serial.Serial(
            self.port,
            baudrate=115200,
            timeout=0.25,
            write_timeout=1.0,
        )
        self.serial.reset_input_buffer()

        self._write_command("h")
        time.sleep(0.25)

        self._write_command("v 2")
        self.status.version = self._read_text(0.8)

        # Interaxon's platform enum, as preserved in the clean-room implementation:
        # 1 iOS, 2 Android, 3 Windows, 4 Mac, 5 Linux.
        self._write_command("r 4")
        time.sleep(0.15)

        # Preset AD provides uncompressed 16-bit EEG plus accelerometer and battery.
        self._write_command("% AD")
        time.sleep(0.15)

        self._write_command("?")
        self.status.status_text = self._read_text(1.5)
        match = re.search(
            r"afe_gain:\s*([0-9]+(?:\.[0-9]+)?)",
            self.status.status_text,
            flags=re.IGNORECASE,
        )
        if match:
            self.status.afe_gain = float(match.group(1))

        if self.on_status:
            self.on_status(self.status)

        self._write_command("s")
        time.sleep(0.25)
        return self.status

    def run(self, should_stop: Callable[[], bool]) -> None:
        if self.serial is None:
            self.open_and_configure()

        assert self.serial is not None
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
                if kind == "eeg":
                    self.on_eeg(
                        decode_eeg_packet(packet, afe_gain=self.status.afe_gain)
                    )
                elif kind == "accelerometer":
                    self.on_accelerometer(decode_accelerometer_packet(packet))
                elif kind == "battery":
                    self.on_battery(decode_battery_packet(packet))

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
