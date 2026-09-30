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


## 2026-09-30 S/T module comparison and timing audit

Two modules carrying different case markings, one **T** and one **S**, were captured for about five seconds each. They exposed the same USB identity and the same 8-byte stream format described above. The letter meaning is still unknown; nothing in these captures indicates a different data protocol.

Observed host delivery:

- T module: 307 reports in 5.005868 s; median host report interval 16.000 ms; longest interval 48.121 ms; zero packet-counter gaps.
- S module: 317 reports in 5.015175 s; median host report interval 15.995 ms; longest interval 55.901 ms; zero packet-counter gaps.

The important result is not the short-run report-count difference. Both captures contain uninterrupted packet counters while the host arrival intervals sometimes stretch to roughly three normal report periods and then catch up. USB/HID delivery on the computer is therefore bursty enough that report-arrival timestamps should not be treated as the sensor's own sample clock.

Biofeedback Play now reconstructs the six within-report sample times from packet order, keeps time reserved for any missing packet-counter steps, and slowly estimates each connected module's long-run waveform rate from many reports while starting from the documented 370 Hz nominal rate. Short USB scheduling bursts are deliberately prevented from becoming artificial beat-to-beat jitter.

Because identical emWave modules do not expose useful serial numbers, Biofeedback Play also preserves each HID path's session slot while it remains connected. Unplugging emWave 1 no longer silently renames emWave 2 as emWave 1. If a genuinely different path later takes a vacated slot, the old direct and derived history for that slot is cleared so two physical sensors are not mixed under one label.

Raw-capture summaries report counter gaps, host-timing spread, observed waveform delivery rate, and raw-value range to make transport problems visible during future device investigations.
