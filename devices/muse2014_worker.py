from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict

from devices.muse2014 import Muse2014SerialClient


class MuseWorkerEmitter:
    """Emit low-overhead JSON lines from a standalone Muse process.

    IOBluetooth behaves reliably when its connection lifecycle runs on the
    Python process main thread. Biofeedback Play therefore launches this helper
    as a subprocess on macOS instead of calling IOBluetooth from its background
    acquisition thread.
    """

    def __init__(self) -> None:
        self.eeg: list[dict] = []
        self.accelerometer: list[tuple[int, int, int]] = []
        self.battery: list[dict] = []
        self.last_flush = time.monotonic()

    @staticmethod
    def emit(payload: dict) -> None:
        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def flush_samples(self, force: bool = False) -> None:
        if not (self.eeg or self.accelerometer or self.battery):
            return

        now = time.monotonic()
        if not force and len(self.eeg) < 20 and now - self.last_flush < 0.05:
            return

        self.emit(
            {
                "type": "samples",
                "eeg": self.eeg,
                "accelerometer": self.accelerometer,
                "battery": self.battery,
            }
        )
        self.eeg = []
        self.accelerometer = []
        self.battery = []
        self.last_flush = now

    def on_eeg(self, sample: dict) -> None:
        self.eeg.append(sample)
        self.flush_samples()

    def on_accelerometer(self, values: tuple[int, int, int]) -> None:
        self.accelerometer.append(values)
        self.flush_samples()

    def on_battery(self, battery: dict) -> None:
        self.battery.append(battery)
        self.flush_samples()

    def on_status(self, status) -> None:
        self.flush_samples(force=True)
        self.emit({"type": "status", "status": asdict(status)})


def run(port: str) -> int:
    emitter = MuseWorkerEmitter()
    client = Muse2014SerialClient(
        port=port,
        on_eeg=emitter.on_eeg,
        on_accelerometer=emitter.on_accelerometer,
        on_battery=emitter.on_battery,
        on_status=emitter.on_status,
    )

    try:
        client.open_and_configure()
        emitter.emit({"type": "ready"})
        client.run(lambda: False)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        emitter.flush_samples(force=True)
        emitter.emit({"type": "error", "message": str(exc)})
        return 1
    finally:
        emitter.flush_samples(force=True)
        client.close()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Muse 2014 acquisition helper")
    parser.add_argument("--port", required=True)
    args = parser.parse_args()
    return run(args.port)


if __name__ == "__main__":
    raise SystemExit(main())
