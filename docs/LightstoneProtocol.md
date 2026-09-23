# Wild Divine Lightstone notes

Observed device:

- Vendor ID: 0x14FA
- Product ID: 0x0001
- Manufacturer: Wild Divine
- Product: ST7 RS232 USB BIOFBK
- USB class: HID
- Interrupt input endpoint: 0x81
- Interrupt output endpoint: 0x02
- HID reports are 8 bytes

## Report framing

For the reports observed on macOS through hidapi:

- byte 0 is the count of valid payload bytes
- bytes 1 through count belong to a continuous text stream
- successive reports must be concatenated

The stream contains frames of the form:

    <RAW>XXXX YYYY<\RAW>

XXXX and YYYY are hexadecimal integers.

## Channel identification

Physical sensor-removal experiments on this specific Lightstone established:

- first field: skin-conductance channel
- second field: pulse / blood-volume waveform channel

Removing one of the two skin electrodes drove the first field to 0000 while the second continued to vary.

This repository intentionally preserves the raw values. Calibration, normalization, heart-rate estimation, and other derived physiology should be implemented above this transport layer rather than silently altering the source data.
