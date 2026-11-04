"""Manual microphone-level diagnostic; no recording occurs on import."""
import numpy as np
import sounddevice as sd


def main(duration=5, samplerate=16000, gain=3.0):
    print(f"[INFO] Recording for {duration}s at {gain}x gain — speak normally now...")
    audio = sd.rec(int(duration * samplerate), samplerate=samplerate,
                   channels=1, dtype="float32")
    sd.wait()
    audio = np.squeeze(np.clip(audio * gain, -1.0, 1.0))

    peak = float(np.max(np.abs(audio)))
    mean_amp = float(np.mean(np.abs(audio)))
    loud_01 = float(np.mean(np.abs(audio) > 0.01))
    loud_005 = float(np.mean(np.abs(audio) > 0.005))
    loud_001 = float(np.mean(np.abs(audio) > 0.001))

    print("\n=== Microphone Level Report ===")
    print(f"  Peak amplitude        : {peak:.4f}")
    print(f"  Mean amplitude        : {mean_amp:.4f}")
    print(f"  Ratio above 0.010     : {loud_01:.3f}")
    print(f"  Ratio above 0.005     : {loud_005:.3f}")
    print(f"  Ratio above 0.001     : {loud_001:.3f}")
    if loud_005 >= 0.02:
        print("Suggested sample threshold: 0.005")
    elif loud_001 >= 0.02:
        print("Suggested sample threshold: 0.001")
    else:
        print("Mic level is very low; check the selected input device and gain.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, sd.PortAudioError) as error:
        print(f"[ERROR] Microphone test failed: {error}", flush=True)
