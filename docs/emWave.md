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


## Observed streaming format

A 5.013 second raw HID capture produced 310 reports with no initialization command from Biofeedback Play.

The observed 8-byte reports have the form:

    01 CC S0 S1 S2 S3 S4 S5

Current interpretation:

- 01 is a constant HID report marker in the captured stream.
- CC is an 8-bit packet counter that increments modulo 256.
- S0 through S5 are six consecutive unsigned 8-bit pulse-waveform samples.

The 310 reports in 5.013 seconds correspond to approximately 61.84 reports/second. Six samples per report correspond to approximately 371 samples/second over that capture. The live adapter currently uses 375 Hz as a nominal sample rate for display timing, while preserving the original 8-bit samples unchanged.

This interpretation is experimental and capture-derived. Do not treat the nominal sample rate as a manufacturer specification unless independently documented.
