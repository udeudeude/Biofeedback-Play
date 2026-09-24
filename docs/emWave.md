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

The device streams directly through HID without an initialization command in the tested configuration. Biofeedback Play preserves the raw waveform and treats its amplitude as uncalibrated.


## Observed streaming format

A 5.013 second raw HID capture produced 310 reports with no initialization command from Biofeedback Play.

The observed 8-byte reports have the form:

    01 CC S0 S1 S2 S3 S4 S5

Current interpretation:

- 01 is a constant HID report marker in the captured stream.
- CC is an 8-bit packet counter that increments modulo 256.
- S0 through S5 are six consecutive unsigned 8-bit pulse-waveform samples.

The 310 reports in 5.013 seconds correspond to approximately 61.84 reports/second. Six samples per report correspond to approximately 371 samples/second over that capture.

HeartMath's emWave Pro Plus feature documentation specifies a 370 Hz pulse-wave sample rate, which closely matches the observed capture. Biofeedback Play therefore uses 370 Hz as the nominal sample rate while preserving the original 8-bit samples unchanged.

Reference:
https://cdn.heartmath.com/manuals/emWave%20Pro%20Plus%20Features%20Sheet.pdf

The exact HID framing interpretation remains capture-derived.


## Multiple modules

Biofeedback Play can open up to four simultaneously connected emWave USB modules by HID path.

The first four current-session slots are named emWave 1 through emWave 4. Because the modules do not expose a useful serial number in the observed HID descriptor, these slot numbers should not be treated as permanent physical identities across unplug/replug cycles.

When two pulse sources are live, Biofeedback Play can derive pairwise:

- heart-rate difference
- median nearest-beat timing offset
- recent waveform correlation
- relative raw pulse amplitude

HeartMath itself documents simultaneous dual-sensor comparison using one sensor on each earlobe. Biofeedback Play extends that idea to direct raw-waveform comparisons.

Timing offsets are experimental. Separate USB devices are not hardware-synchronized, so the offset must not be interpreted as clinical pulse-transit time.
