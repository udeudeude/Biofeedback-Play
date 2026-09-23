# HeartMath emWave USB notes

Observed on the user's hardware on 2026-09-23:

- Product string: emWave Pulse Sensor
- Manufacturer string: QUANTUM INTECH
- USB vendor ID: 0x0E30
- USB product ID: 0x0002
- USB speed reported by macOS: 1.5 Mb/s
- HID usage page reported by hidapi: 0xFF00
- HID usage: 1
- Interface number: 0

The device is visible through hidapi on macOS.

No raw report format, command protocol, sample rate, or physiological scaling is assumed yet. Use the browser Diagnostics panel to make a short raw capture before implementing the emWave adapter.
