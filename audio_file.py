import sounddevice as sd
import soundfile as sf
import numpy as np
import os


def record_test_audio(
	output_file="test.wav",
	duration=5,
	samplerate=16000,
	gain=None,
):
	"""Record a short microphone sample and save it for device troubleshooting."""
	if gain is None:
		gain = float(os.getenv("JARVIS_MIC_GAIN", "3.0"))

	device = sd.default.device[0]
	device_name = sd.query_devices(device, "input")["name"]
	print(f"[INFO] Microphone: {device_name}", flush=True)
	print(f"[INFO] Software microphone gain: {gain:.1f}x", flush=True)
	print("[INFO] Speak now...", flush=True)
	sd.check_input_settings(samplerate=samplerate, channels=1, dtype="float32")
	audio = sd.rec(
		int(duration * samplerate),
		samplerate=samplerate,
		channels=1,
		dtype="float32",
	)
	sd.wait()
	audio = np.clip(audio * gain, -1.0, 1.0)
	sf.write(output_file, audio, samplerate)
	print(f"[INFO] Saved recording to {output_file}", flush=True)


if __name__ == "__main__":
	try:
		record_test_audio()
	except (OSError, sd.PortAudioError) as error:
		print(f"[ERROR] Microphone test failed: {error}", flush=True)
