import pvporcupine
import pyaudio
import struct
import os
import numpy as np

def listen_for_wake_word(keyword="jarvis"):
    """Block until the configured Porcupine wake word is detected."""
    gain = float(os.getenv("JARVIS_MIC_GAIN", "3.0"))
    porcupine = pvporcupine.create(keywords=[keyword])
    pa = pyaudio.PyAudio()
    stream = None

    try:
        stream = pa.open(
            rate=porcupine.sample_rate,
            channels=1,
            format=pyaudio.paInt16,
            input=True,
            frames_per_buffer=porcupine.frame_length,
        )
        print(
            f"[INFO] Listening for '{keyword}' with {gain:.1f}x microphone gain...",
            flush=True,
        )

        while True:
            pcm = stream.read(
                porcupine.frame_length,
                exception_on_overflow=False,
            )
            pcm = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
            pcm = np.clip(pcm * gain, -32768, 32767).astype(np.int16)
            pcm = tuple(pcm)
            if porcupine.process(pcm) >= 0:
                print(f"[INFO] Wake word detected: {keyword}", flush=True)
                return
    finally:
        if stream is not None:
            stream.stop_stream()
            stream.close()
        pa.terminate()
        porcupine.delete()


if __name__ == "__main__":
    try:
        listen_for_wake_word()
    except KeyboardInterrupt:
        print("[INFO] Listener stopped.", flush=True)
    except (OSError, IOError) as error:
        print(f"[ERROR] Listener audio error: {error}", flush=True)
