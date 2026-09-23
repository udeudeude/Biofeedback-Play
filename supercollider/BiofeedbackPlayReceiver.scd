(
// Biofeedback Play -> SuperCollider starter receiver
// In Biofeedback Play, enable OSC and use 127.0.0.1:57120.

~biofeedback = (
    skinRaw: 0,
    pulseRaw: 0,
    emWavePulseRaw: 0,
    museTP9: 0.0,
    museFP1: 0.0,
    museFP2: 0.0,
    museTP10: 0.0,
    museAccelX: 0,
    museAccelY: 0,
    museAccelZ: 0
);

OSCdef(\biofeedbackSkin, { |msg|
    ~biofeedback[\skinRaw] = msg[1].asInteger;
}, '/biofeedback/lightstone/skin_raw');

OSCdef(\biofeedbackPulse, { |msg|
    ~biofeedback[\pulseRaw] = msg[1].asInteger;
}, '/biofeedback/lightstone/pulse_raw');

OSCdef(\biofeedbackEmWave, { |msg|
    ~biofeedback[\emWavePulseRaw] = msg[1].asInteger;
}, '/biofeedback/emwave/pulse_raw');

OSCdef(\museTP9, { |msg| ~biofeedback[\museTP9] = msg[1].asFloat; }, '/biofeedback/muse/eeg/tp9');
OSCdef(\museFP1, { |msg| ~biofeedback[\museFP1] = msg[1].asFloat; }, '/biofeedback/muse/eeg/fp1');
OSCdef(\museFP2, { |msg| ~biofeedback[\museFP2] = msg[1].asFloat; }, '/biofeedback/muse/eeg/fp2');
OSCdef(\museTP10, { |msg| ~biofeedback[\museTP10] = msg[1].asFloat; }, '/biofeedback/muse/eeg/tp10');
OSCdef(\museAccelX, { |msg| ~biofeedback[\museAccelX] = msg[1].asInteger; }, '/biofeedback/muse/accel/x');
OSCdef(\museAccelY, { |msg| ~biofeedback[\museAccelY] = msg[1].asInteger; }, '/biofeedback/muse/accel/y');
OSCdef(\museAccelZ, { |msg| ~biofeedback[\museAccelZ] = msg[1].asInteger; }, '/biofeedback/muse/accel/z');

"Biofeedback Play OSC receiver ready.".postln;
)

// Inspect current values whenever you want:
// ~biofeedback.postln;

// Stop listening later with:
// OSCdef(\biofeedbackSkin).free;
// OSCdef(\biofeedbackPulse).free;
// OSCdef(\biofeedbackEmWave).free;
