(
// Biofeedback Play -> SuperCollider starter receiver
// Enable OSC in Biofeedback Play and use 127.0.0.1:57120.
//
// This file only installs a useful starter subset. Every Biofeedback Play panel
// also shows its own OSC address, so any raw or derived signal can be added.

~biofeedback = (
    skinRaw: 0,
    lightstonePulseRaw: 0,
    lightstoneHeartRate: 0.0,
    lightstoneRMSSD: 0.0,
    lightstoneCoherence: 0.0,
    emWave1PulseRaw: 0,
    emWave1HeartRate: 0.0,
    emWave1RMSSD: 0.0,
    emWave1Coherence: 0.0,
    emWave2PulseRaw: 0,
    museTP9: 0.0,
    museFP1: 0.0,
    museFP2: 0.0,
    museTP10: 0.0,
    museAlphaPower: 0.0,
    museThetaPower: 0.0,
    museMotion: 0.0
);

OSCdef(\biofeedbackSkin, { |msg|
    ~biofeedback[\skinRaw] = msg[1].asInteger;
}, '/biofeedback/lightstone/skin_raw');

OSCdef(\lightstonePulse, { |msg|
    ~biofeedback[\lightstonePulseRaw] = msg[1].asInteger;
}, '/biofeedback/lightstone/pulse_raw');

OSCdef(\lightstoneHeartRate, { |msg|
    ~biofeedback[\lightstoneHeartRate] = msg[1].asFloat;
}, '/biofeedback/lightstone/heart_rate');

OSCdef(\lightstoneRMSSD, { |msg|
    ~biofeedback[\lightstoneRMSSD] = msg[1].asFloat;
}, '/biofeedback/lightstone/hrv_rmssd_ms');

OSCdef(\lightstoneCoherence, { |msg|
    ~biofeedback[\lightstoneCoherence] = msg[1].asFloat;
}, '/biofeedback/lightstone/coherence_ratio');

OSCdef(\emWave1Pulse, { |msg|
    ~biofeedback[\emWave1PulseRaw] = msg[1].asInteger;
}, '/biofeedback/emwave/pulse_raw');

OSCdef(\emWave1HeartRate, { |msg|
    ~biofeedback[\emWave1HeartRate] = msg[1].asFloat;
}, '/biofeedback/emwave/heart_rate');

OSCdef(\emWave1RMSSD, { |msg|
    ~biofeedback[\emWave1RMSSD] = msg[1].asFloat;
}, '/biofeedback/emwave/hrv_rmssd_ms');

OSCdef(\emWave1Coherence, { |msg|
    ~biofeedback[\emWave1Coherence] = msg[1].asFloat;
}, '/biofeedback/emwave/coherence_ratio');

OSCdef(\emWave2Pulse, { |msg|
    ~biofeedback[\emWave2PulseRaw] = msg[1].asInteger;
}, '/biofeedback/emwave/2/pulse_raw');

OSCdef(\museTP9, { |msg| ~biofeedback[\museTP9] = msg[1].asFloat; }, '/biofeedback/muse/eeg/tp9');
OSCdef(\museFP1, { |msg| ~biofeedback[\museFP1] = msg[1].asFloat; }, '/biofeedback/muse/eeg/fp1');
OSCdef(\museFP2, { |msg| ~biofeedback[\museFP2] = msg[1].asFloat; }, '/biofeedback/muse/eeg/fp2');
OSCdef(\museTP10, { |msg| ~biofeedback[\museTP10] = msg[1].asFloat; }, '/biofeedback/muse/eeg/tp10');

OSCdef(\museAlphaPower, { |msg|
    ~biofeedback[\museAlphaPower] = msg[1].asFloat;
}, '/biofeedback/muse/band/alpha');

OSCdef(\museThetaPower, { |msg|
    ~biofeedback[\museThetaPower] = msg[1].asFloat;
}, '/biofeedback/muse/band/theta');

OSCdef(\museMotion, { |msg|
    ~biofeedback[\museMotion] = msg[1].asFloat;
}, '/biofeedback/muse/motion_intensity');

"Biofeedback Play OSC receiver ready.".postln;
)

// Inspect current values whenever you want:
// ~biofeedback.postln;

// Individual listeners can be freed with, for example:
// OSCdef(\emWave1HeartRate).free;
