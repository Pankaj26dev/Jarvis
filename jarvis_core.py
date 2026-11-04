"""
jarvis_core.py — Core engine for the Jarvis offline assistant.

Responsibilities:
  - Load and expose configuration (config.json + env-var overrides).
  - Wake word detection via Porcupine.
  - Audio recording with noise-gate filtering.
  - Speech-to-text via Whisper.
  - LLM query via Ollama subprocess (streaming, async-compatible wrapper).
  - Text-to-speech via jarvis_tts.speak_text.
  - Conversation history management (sliding context window).
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import urllib.error
from urllib.parse import urlparse
from typing import List, Optional

import numpy as np
import pvporcupine
import pyaudio
import whisper

from jarvis_logger import get_logger
from jarvis_config import load_config
from jarvis_tts import speak_text

log = get_logger(__name__)
_ollama_start_lock = threading.Lock()
_ollama_process = None

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _get(cfg: dict, key: str, env_var: str, default):
    """Return env-var override → config value → hard default (in that order)."""
    env_val = os.getenv(env_var)
    if env_val is not None:
        return type(default)(env_val)
    return cfg.get(key, default)


# ---------------------------------------------------------------------------
# Noise filter
# ---------------------------------------------------------------------------

def is_speech_present(audio: np.ndarray, threshold: float, min_ratio: float) -> bool:
    """Return True when enough audio energy exceeds the silence threshold.

    Args:
        audio:      1-D float32 array in [-1, 1].
        threshold:  Amplitude level below which a sample is considered silent.
        min_ratio:  Minimum fraction of samples that must be above threshold.
    """
    loud_ratio = np.mean(np.abs(audio) > threshold)
    return float(loud_ratio) >= min_ratio


# ---------------------------------------------------------------------------
# JarvisCore class
# ---------------------------------------------------------------------------

class JarvisCore:
    """Encapsulates all pipeline stages as a single, testable object."""

    def __init__(self, cfg: dict, on_event=None):
        self.cfg = cfg
        self.on_event = on_event

        # Microphone / audio
        self.mic_gain: float = _get(cfg, "mic_gain", "JARVIS_MIC_GAIN", 3.0)
        self.record_duration: int = _get(cfg, "record_duration", "JARVIS_RECORD_DURATION", 5)
        self.samplerate: int = _get(cfg, "samplerate", "JARVIS_SAMPLERATE", 16000)
        self.speech_language = cfg.get("speech_language", "en")
        self.speech_start_timeout = max(0.5, float(cfg.get("speech_start_timeout_seconds", 3.0)))
        self.transcription_min_logprob = float(cfg.get("transcription_min_avg_logprob", -1.0))
        self.transcription_max_no_speech = float(cfg.get("transcription_max_no_speech_prob", 0.65))
        self._last_transcription_confident = True
        # None means "use system default"; set mic_device_index in config.json to pin a specific device
        raw_idx = cfg.get("mic_device_index", None)
        self.mic_device_index: Optional[int] = int(raw_idx) if raw_idx is not None else None

        # Noise filter
        nf = cfg.get("noise_filter", {})
        self.noise_filter_enabled: bool = nf.get("enabled", True)
        self.silence_threshold: float = nf.get("silence_threshold", 0.01)
        self.min_speech_ratio: float = nf.get("min_speech_ratio", 0.05)

        # LLM
        self.ollama_model: str = _get(cfg, "ollama_model", "JARVIS_OLLAMA_MODEL", "phi3:mini")
        self.ollama_url: str = str(cfg.get("ollama_url", "http://localhost:11434")).rstrip("/")
        self.ollama_autostart: bool = bool(cfg.get("ollama_autostart", True))
        self._ollama_ready = False
        self.ollama_max_tokens: int = int(cfg.get("ollama_max_tokens", 120))
        self.ollama_keep_alive = cfg.get("ollama_keep_alive", "30m")
        self.agent_enabled = bool(cfg.get("agent_enabled", True))
        self.agent_max_steps = max(1, int(cfg.get("agent_max_steps", 4)))
        self.agent_max_tool_calls = max(1, int(cfg.get("agent_max_tool_calls", 6)))

        # Conversation history  (list of {"role": ..., "content": ...} dicts)
        self.history_limit: int = _get(cfg, "conversation_history_limit", "JARVIS_HISTORY_LIMIT", 10)
        self._history: List[dict] = []

        # Wake word
        self.wake_word: str = _get(cfg, "wake_word", "JARVIS_WAKE_WORD", "jarvis")
        self.wake_word_sensitivity: float = _get(cfg, "wake_word_sensitivity", "JARVIS_WAKE_SENSITIVITY", 0.7)
        self.wake_word_delay: float = _get(cfg, "wake_word_delay", "JARVIS_WAKE_DELAY", 0.3)

        # Lazy-loaded heavy objects
        self._porcupine = None
        self._pyaudio = None
        self._stream = None
        self._active_mic_device_index = self.mic_device_index
        self._capture_rate = self.samplerate
        self._capture_decimation = 1
        self._capture_frame_length = 0
        self._capture_channels = 1
        self._whisper_model = None
        self.running = True
        self._stop_lock = threading.Lock()

    def _emit(self, event: str, value: str = ""):
        """Notify an optional UI without coupling the core to a GUI toolkit."""
        if self.on_event:
            try:
                self.on_event(event, value)
            except Exception:
                log.exception("UI event callback failed")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _open_input_stream(self):
        """Open the one shared microphone stream used by wake detection and VAD."""
        if self._stream is not None:
            try:
                self._stream.stop_stream()
            except Exception:
                pass
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None

        device_index = (
            self.mic_device_index
            if self.mic_device_index is not None
            else self._pyaudio.get_default_input_device_info()["index"]
        )
        requested_device_index = device_index
        device_info = self._pyaudio.get_device_info_by_index(device_index)

        # Windows often exposes the same microphone through MME, DirectSound,
        # WASAPI, and WDM-KS. Prefer its WASAPI endpoint when available; MME at
        # 16 kHz can produce noticeably worse/less stable capture on Realtek.
        if os.name == "nt":
            selected_name = re.sub(r"[^a-z0-9]", "", device_info["name"].lower())
            for candidate_index in range(self._pyaudio.get_device_count()):
                candidate = self._pyaudio.get_device_info_by_index(candidate_index)
                if candidate.get("maxInputChannels", 0) < 1:
                    continue
                host = self._pyaudio.get_host_api_info_by_index(candidate["hostApi"])
                candidate_name = re.sub(r"[^a-z0-9]", "", candidate["name"].lower())
                shared_prefix = min(len(selected_name), len(candidate_name))
                same_device = (
                    selected_name[:shared_prefix] == candidate_name[:shared_prefix]
                    and shared_prefix >= min(18, len(selected_name))
                )
                if "WASAPI" in host.get("name", "").upper() and same_device:
                    device_index = candidate_index
                    device_info = candidate
                    break

        native_rate = int(round(device_info.get("defaultSampleRate", self._porcupine.sample_rate)))
        decimation = native_rate // self._porcupine.sample_rate
        if decimation < 1 or native_rate % self._porcupine.sample_rate:
            # Keep PortAudio's supported-rate conversion for non-integral
            # rates such as 44.1 kHz rather than applying a bad resample.
            capture_rate = self._porcupine.sample_rate
            decimation = 1
        else:
            capture_rate = native_rate
        self._capture_rate = capture_rate
        self._capture_decimation = decimation
        self._capture_frame_length = self._porcupine.frame_length * decimation
        self._active_mic_device_index = device_index
        host_name = self._pyaudio.get_host_api_info_by_index(device_info["hostApi"])["name"]
        self._capture_channels = (
            min(2, int(device_info.get("maxInputChannels", 1)))
            if "WASAPI" in host_name.upper() else 1
        )

        pa_kwargs = dict(
            rate=self._capture_rate,
            channels=self._capture_channels,
            format=pyaudio.paInt16,
            input=True,
            frames_per_buffer=self._capture_frame_length,
        )
        pa_kwargs["input_device_index"] = device_index
        try:
            self._stream = self._pyaudio.open(**pa_kwargs)
        except Exception:
            if device_index == requested_device_index:
                raise
            log.exception("Preferred WASAPI mic failed to open; falling back to the configured input.")
            device_index = requested_device_index
            device_info = self._pyaudio.get_device_info_by_index(device_index)
            host_name = self._pyaudio.get_host_api_info_by_index(device_info["hostApi"])["name"]
            self._capture_rate = self._porcupine.sample_rate
            self._capture_decimation = 1
            self._capture_frame_length = self._porcupine.frame_length
            self._capture_channels = 1
            pa_kwargs.update(
                rate=self._capture_rate,
                channels=1,
                frames_per_buffer=self._capture_frame_length,
                input_device_index=device_index,
            )
            self._stream = self._pyaudio.open(**pa_kwargs)
            self._active_mic_device_index = device_index
        log.info(
            "Microphone capture: %s via %s at %s Hz, %s channel(s) (%sx downsample for recognition).",
            device_info["name"],
            host_name,
            self._capture_rate,
            self._capture_channels,
            self._capture_decimation,
        )
        return device_info["name"]

    def _read_audio_frame(self):
        """Read one Porcupine-sized 16 kHz frame from the active input stream."""
        raw = self._stream.read(
            self._capture_frame_length, exception_on_overflow=False
        )
        if self._capture_decimation == 1 and self._capture_channels == 1:
            return raw
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        samples = samples.reshape(-1, self._capture_channels).mean(axis=1)
        samples = samples[: self._porcupine.frame_length * self._capture_decimation]
        # Averaging each group provides a simple low-pass before decimation.
        samples = samples.reshape(self._porcupine.frame_length, self._capture_decimation)
        downsampled = np.rint(samples.mean(axis=1))
        return np.clip(downsampled, -32768, 32767).astype(np.int16).tobytes()

    def _recover_input_stream(self, reason):
        if not self.running:
            return False
        log.warning("Microphone stream needs recovery: %s", reason)
        self._emit("status", "Reconnecting microphone…")
        try:
            device_name = self._open_input_stream()
            log.info("Microphone stream recovered on '%s'.", device_name)
            return True
        except Exception:
            log.exception("Could not reopen microphone; will retry.")
            return False

    def start(self):
        """Load models and open audio stream."""
        log.info("Loading Whisper model (%s)…", _get(self.cfg, "whisper_model", "JARVIS_WHISPER_MODEL", "tiny"))
        self._whisper_model = whisper.load_model(
            _get(self.cfg, "whisper_model", "JARVIS_WHISPER_MODEL", "tiny")
        )
        log.info("Whisper ready.")

        self._porcupine = pvporcupine.create(
            keywords=[self.wake_word],
            sensitivities=[self.wake_word_sensitivity],
        )
        # Porcupine and Whisper both expect 16 kHz mono audio. Use the
        # Porcupine rate as the single source of truth for the shared stream.
        if self.samplerate != self._porcupine.sample_rate:
            log.warning(
                "Configured sample rate %s does not match Porcupine's %s; using %s.",
                self.samplerate, self._porcupine.sample_rate,
                self._porcupine.sample_rate,
            )
            self.samplerate = self._porcupine.sample_rate
        self._pyaudio = pyaudio.PyAudio()
        dev_name = self._open_input_stream()
        log.info("Wake word engine ready — listening for '%s' on '%s'.", self.wake_word, dev_name)
        self._warmup_model()

    def _warmup_model(self):
        """Send a minimal request to Ollama so the model is loaded into RAM.

        This runs once at startup. All subsequent requests hit the already-loaded
        model and respond much faster (no disk-to-RAM load delay).
        """
        log.info("Warming up model '%s' — loading into RAM…", self.ollama_model)
        try:
            if not self._ensure_ollama_server():
                log.warning(
                    "Ollama is unavailable at %s. Start Ollama or install its CLI; "
                    "Jarvis will retry when you speak.", self.ollama_url,
                )
                return
            payload = json.dumps({
                "model": self.ollama_model,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                "keep_alive": self.ollama_keep_alive,
                "options": {"num_predict": 1, "num_ctx": 1000},
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{self.ollama_url}/api/chat",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                resp.read()
            log.info("Model warm and ready in RAM.")
        except Exception as exc:
            if isinstance(exc, urllib.error.URLError):
                self._ollama_ready = False
            log.warning("Model warmup failed (will still work, just slower): %s", exc)

    def _ensure_ollama_server(self):
        """Check Ollama and start its local service when it is installed but stopped."""
        global _ollama_process
        # Avoid an HTTP health request on every conversation turn. A failed
        # inference request clears this cache and triggers recovery on demand.
        if self._ollama_ready:
            return True
        parsed = urlparse(self.ollama_url)
        local_hosts = {"localhost", "127.0.0.1"}
        is_local = parsed.hostname in local_hosts

        def is_ready():
            try:
                with urllib.request.urlopen(f"{self.ollama_url}/api/tags", timeout=1.5) as response:
                    return response.status == 200
            except (urllib.error.URLError, TimeoutError, OSError):
                return False

        if is_ready():
            self._ollama_ready = True
            return True
        if not is_local or not self.ollama_autostart:
            return False

        with _ollama_start_lock:
            # Another Jarvis request may have started it while we waited.
            if is_ready():
                self._ollama_ready = True
                return True

            executable = shutil.which("ollama") or shutil.which("ollama.exe")
            if not executable and os.name == "nt":
                local_app_data = os.environ.get("LOCALAPPDATA")
                if local_app_data:
                    candidate = os.path.join(local_app_data, "Programs", "Ollama", "ollama.exe")
                    if os.path.isfile(candidate):
                        executable = candidate
            if not executable:
                log.error("Ollama CLI not found; cannot start the local model service.")
                return False

            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            host = parsed.hostname or "127.0.0.1"
            env = os.environ.copy()
            env["OLLAMA_HOST"] = f"{host}:{port}"
            popen_options = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "env": env,
                "close_fds": True,
            }
            if os.name == "nt":
                popen_options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            else:
                popen_options["start_new_session"] = True
            try:
                _ollama_process = subprocess.Popen([executable, "serve"], **popen_options)
                log.info("Started local Ollama service (pid %s).", _ollama_process.pid)
            except OSError:
                log.exception("Could not start Ollama from %s.", executable)
                return False

            deadline = time.monotonic() + 25.0
            while time.monotonic() < deadline:
                if is_ready():
                    self._ollama_ready = True
                    log.info("Local Ollama service is ready at %s.", self.ollama_url)
                    return True
                if _ollama_process.poll() is not None:
                    log.error("Ollama exited during startup (code %s).", _ollama_process.returncode)
                    return False
                time.sleep(0.5)
            log.error("Ollama did not become ready at %s within 25 seconds.", self.ollama_url)
            return False

    def stop(self):
        """Release all audio and model resources."""
        with self._stop_lock:
            self.running = False
            stream, self._stream = self._stream, None
            audio, self._pyaudio = self._pyaudio, None
            porcupine, self._porcupine = self._porcupine, None
            if stream is not None:
                try:
                    stream.stop_stream()
                except Exception:
                    log.debug("Audio stream was already stopped", exc_info=True)
                try:
                    stream.close()
                except Exception:
                    log.debug("Audio stream close failed", exc_info=True)
            if audio is not None:
                try:
                    audio.terminate()
                except Exception:
                    log.exception("Failed to terminate PyAudio")
            if porcupine is not None:
                try:
                    porcupine.delete()
                except Exception:
                    log.exception("Failed to release Porcupine")
            log.info("JarvisCore stopped.")

    # ------------------------------------------------------------------
    # Wake word detection
    # ------------------------------------------------------------------

    def wait_for_wake_word(self):
        """Block until the wake word is detected on the audio stream."""
        log.info("Waiting for wake word…")
        self._emit("status", "Waiting for wake word")
        near_silent_frames = 0
        silent_recovery_frames = max(
            1, int(30 * self.samplerate / self._porcupine.frame_length)
        )
        while self.running:
            try:
                if self._stream is None or not self._stream.is_active():
                    if not self._recover_input_stream("input stream is inactive"):
                        time.sleep(1.0)
                    continue
                pcm = self._read_audio_frame()
            except Exception as exc:
                if not self.running:
                    raise InterruptedError("Jarvis is stopping") from None
                if not self._recover_input_stream(exc):
                    time.sleep(1.0)
                continue

            # Porcupine expects the microphone's original signed 16-bit PCM.
            # Applying the speech-recognition gain here clips samples and can
            # make wake detection both less reliable and more prone to false hits.
            pcm = np.frombuffer(pcm, dtype=np.int16)
            pcm_float = pcm.astype(np.float32)
            mic_rms = float(np.sqrt(np.mean(np.square(pcm_float)))) / 32768.0
            self._emit("audio_level", min(1.0, mic_rms * 18.0))
            if np.max(np.abs(pcm.astype(np.int32))) <= 1:
                near_silent_frames += 1
                if near_silent_frames >= silent_recovery_frames:
                    self._recover_input_stream(
                        "microphone produced digital silence for 30 seconds"
                    )
                    near_silent_frames = 0
                    continue
            else:
                near_silent_frames = 0
            if self._porcupine.process(tuple(int(sample) for sample in pcm)) >= 0:
                log.info("Wake word detected.")
                self._emit("status", "I’m listening")
                if self.wake_word_delay > 0:
                    time.sleep(self.wake_word_delay)
                return
        raise InterruptedError("Jarvis is stopping")

    # ------------------------------------------------------------------
    # Audio recording + noise gate
    # ------------------------------------------------------------------

    def record_audio(self) -> Optional[np.ndarray]:
        """Capture speech from the already-open wake-word stream.

        Reading Porcupine-sized frames from one persistent microphone stream
        avoids switching audio backends and reopening the input device for
        every short recording block.
        """
        if not self.running or self._stream is None or self._porcupine is None:
            return None

        device_name = self._pyaudio.get_device_info_by_index(
            self._active_mic_device_index
        )["name"]
        frame_length = self._porcupine.frame_length
        max_frames = max(1, int(self.record_duration * self.samplerate / frame_length))
        silence_limit = max(1, int(0.8 * self.samplerate / frame_length))
        speech_start_frames = max(1, int(self.speech_start_timeout * self.samplerate / frame_length))
        vad_threshold = float(self.cfg.get("vad_rms_threshold", 0.004))

        log.info("Listening on '%s' (speak now, stops on silence)…", device_name)
        self._emit("status", "Listening… speak naturally")

        chunks = []
        # Keep a short pre-roll so the first phoneme is not lost to VAD
        # startup, without feeding several seconds of room silence to Whisper.
        pre_roll = []
        pre_roll_frames = max(1, int(0.25 * self.samplerate / frame_length))
        silent_streak = 0
        speech_started = False

        for frame_number in range(max_frames):
            if not self.running:
                self._emit("audio_level", 0.0)
                return None
            try:
                pcm = self._read_audio_frame()
            except (OSError, IOError):
                if not self.running:
                    return None
                raise
            block = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            # Apply software gain without hard-clipping loud syllables.
            peak = float(np.max(np.abs(block))) if block.size else 0.0
            safe_gain = min(self.mic_gain, 0.98 / peak) if peak > 0 else self.mic_gain
            block = block * max(0.0, safe_gain)
            rms_level = float(np.sqrt(np.mean(np.square(block))))
            self._emit("audio_level", min(1.0, rms_level * 6.0))

            # RMS is less sensitive than peak amplitude to clicks and brief
            # background spikes, which otherwise start empty recordings.
            if self.noise_filter_enabled:
                is_loud = is_speech_present(
                    block, self.silence_threshold, self.min_speech_ratio
                )
            else:
                is_loud = rms_level > vad_threshold
            if is_loud:
                if not speech_started:
                    chunks.extend(pre_roll)
                    pre_roll.clear()
                chunks.append(block)
                speech_started = True
                silent_streak = 0
            else:
                silent_streak += 1
                if speech_started:
                    chunks.append(block)
                else:
                    pre_roll.append(block)
                    if len(pre_roll) > pre_roll_frames:
                        del pre_roll[0]

            if not speech_started and frame_number + 1 >= speech_start_frames:
                log.warning("No speech detected within %.1fs.", self.speech_start_timeout)
                self._emit("audio_level", 0.0)
                return None

            # Stop once speech has begun and then gone silent
            if speech_started and silent_streak >= silence_limit:
                break

        if not speech_started:
            log.warning("No speech detected.")
            self._emit("audio_level", 0.0)
            return None

        self._emit("audio_level", 0.0)
        audio = np.concatenate(chunks)
        capture_peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        capture_rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
        clipped_fraction = float(np.mean(np.abs(audio) >= 0.995)) if audio.size else 0.0
        log.info(
            "Recorded %.1fs of audio (RMS %.4f, peak %.3f, clipped %.2f%%).",
            len(audio) / self.samplerate, capture_rms, capture_peak,
            clipped_fraction * 100,
        )
        return audio

    # ------------------------------------------------------------------
    # Speech-to-text
    # ------------------------------------------------------------------

    def transcribe(self, audio: np.ndarray) -> str:
        """Run Whisper on a float32 audio array and return the transcript."""
        log.info("Transcribing…")
        options = {
            "fp16": False,
            "temperature": 0,
            "beam_size": 3,
            "condition_on_previous_text": False,
            "no_speech_threshold": 0.6,
            "logprob_threshold": -1.0,
        }
        if self.speech_language:
            options["language"] = self.speech_language
        result = self._whisper_model.transcribe(audio, **options)
        text = result["text"].strip()
        segments = result.get("segments", [])
        if segments:
            avg_logprob = sum(float(seg.get("avg_logprob", 0.0)) for seg in segments) / len(segments)
            max_no_speech = max(float(seg.get("no_speech_prob", 0.0)) for seg in segments)
            self._last_transcription_confident = (
                avg_logprob >= self.transcription_min_logprob
                and max_no_speech <= self.transcription_max_no_speech
            )
            log.debug(
                "ASR confidence stats: avg_logprob=%.2f max_no_speech=%.2f language=%s",
                avg_logprob, max_no_speech, result.get("language", self.speech_language),
            )
        else:
            self._last_transcription_confident = bool(text)
        log.debug("Transcript: %s", text)
        self._emit("user", text)
        return text

    # ------------------------------------------------------------------
    # Ollama LLM  (sync + async variants)
    # ------------------------------------------------------------------

    def _build_messages(self, user_text: str) -> list:
        """Build the messages list for Ollama's /api/chat endpoint.

        Uses a proper system role so there is zero session bleed between runs.
        """
        messages = [
            {
                "role": "system",
                "content": (
                    "You are Jarvis, a helpful offline voice assistant. "
                    "Reply concisely in plain spoken English. "
                    "Only use desktop tools when the user clearly asks for that action. "
                    "For a request with multiple safe actions, use tools step by step, read each result, "
                    "and continue only when the previous result supports it. Use the app catalog when "
                    "you need to discover installed apps. Never invent successful actions or claim "
                    "a tool succeeded unless its result confirms it. "
                    "Do not repeat the user's words back. "
                    "Do not include any labels like 'User:' or 'Jarvis:' in your reply."
                ),
            }
        ]
        for entry in self._history:
            messages.append({"role": entry["role"], "content": entry["content"]})
        messages.append({"role": "user", "content": user_text})
        return messages

    def _record_exchange(self, user_text: str, assistant_text: str):
        """Add the latest turn to history and trim to the configured limit."""
        self._history.append({"role": "user", "content": user_text})
        self._history.append({"role": "assistant", "content": assistant_text})
        max_entries = self.history_limit * 2
        if max_entries <= 0:
            self._history.clear()
        elif len(self._history) > max_entries:
            self._history = self._history[-max_entries:]

    def query_ollama_stream(self, user_text: str, _messages=None, _allow_tools=True,
                            _tool_depth=0, _tool_calls_used=0):
        """Stream tokens from Ollama and yield complete sentences as they form.

        Yields one sentence string at a time the moment a sentence boundary
        (. ! ?) is reached, so the caller can start TTS immediately without
        waiting for the full response.
        Also returns the complete response as the final StopIteration value
        via self._last_response (set before the generator exits).
        """
        from jarvis_tools import TOOL_SCHEMAS, execute_tool, should_offer_tools, tool_call_arguments

        url = f"{self.ollama_url}/api/chat"
        messages = _messages if _messages is not None else self._build_messages(user_text)
        started_at = time.monotonic()
        request_data = {
            "model": self.ollama_model,
            "messages": messages,
            "stream": True,
            "keep_alive": self.ollama_keep_alive,
            "options": {"num_predict": self.ollama_max_tokens, "num_ctx": 1000},
        }
        if (
            self.agent_enabled
            and _allow_tools
            and _tool_depth < self.agent_max_steps
            and _tool_calls_used < self.agent_max_tool_calls
            and should_offer_tools(user_text)
        ):
            request_data["tools"] = TOOL_SCHEMAS
            request_data["options"]["num_predict"] = max(
                self.ollama_max_tokens,
                int(self.cfg.get("agent_max_tokens", 128)),
            )
        payload = json.dumps(request_data).encode("utf-8")

        log.info("Streaming Ollama (%s)…", self.ollama_model)
        self._emit("status", "Thinking…")
        if not self._ensure_ollama_server():
            message = (
                "I can’t connect to Ollama. I tried to start its local service, "
                "but it isn’t responding. Check that Ollama is installed and try again."
            )
            log.error("Ollama is unavailable at %s.", self.ollama_url)
            yield message
            return
        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            log.error("Ollama returned HTTP %s: %s", exc.code, detail or exc.reason)
            if exc.code == 404 and self.ollama_model:
                yield f"Ollama is running, but the model {self.ollama_model} is not installed. Run `ollama pull {self.ollama_model}` and retry."
            else:
                yield f"Ollama returned an error (HTTP {exc.code}). Check the Jarvis log for details."
            return
        except urllib.error.URLError as exc:
            # A connection can die between turns (sleep, shutdown, or update).
            # Clear the cached state, restart/recheck once, then retry this turn.
            self._ollama_ready = False
            log.warning("Ollama connection failed at %s; attempting one recovery: %s", self.ollama_url, exc)
            if self._ensure_ollama_server():
                try:
                    resp = urllib.request.urlopen(req, timeout=60)
                except urllib.error.HTTPError as retry_exc:
                    detail = retry_exc.read().decode("utf-8", errors="replace").strip()
                    log.error("Ollama returned HTTP %s after recovery: %s", retry_exc.code, detail or retry_exc.reason)
                    if retry_exc.code == 404 and self.ollama_model:
                        yield f"Ollama is running, but the model {self.ollama_model} is not installed. Run `ollama pull {self.ollama_model}` and retry."
                    else:
                        yield f"Ollama returned an error (HTTP {retry_exc.code}). Check the Jarvis log for details."
                    return
                except urllib.error.URLError as retry_exc:
                    self._ollama_ready = False
                    log.error("Ollama is still unavailable at %s: %s", self.ollama_url, retry_exc)
                    yield "I can’t connect to Ollama right now. Check that the local service is running and try again."
                    return
            else:
                log.error("Ollama could not be restarted at %s.", self.ollama_url)
                yield "I can’t connect to Ollama right now. Check that the local service is running and try again."
                return

        buffer = ""
        full_response = ""
        tool_calls = []
        first_token_at = None
        response_metrics = {}
        sentence_ends = {".", "!", "?"}

        for raw_line in resp:
            line = raw_line.decode("utf-8").strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue

            token = chunk.get("message", {}).get("content", "")
            if token and first_token_at is None:
                first_token_at = time.monotonic()
            if chunk.get("done", False):
                response_metrics = {
                    key: chunk[key]
                    for key in ("prompt_eval_count", "prompt_eval_duration", "eval_count", "eval_duration")
                    if key in chunk
                }
            chunk_calls = chunk.get("message", {}).get("tool_calls", [])
            if chunk_calls:
                tool_calls.extend(chunk_calls)
            buffer += token
            full_response += token

            # Yield whenever we have a complete sentence
            while True:
                # Find earliest sentence boundary
                idx = -1
                for ch in sentence_ends:
                    pos = buffer.find(ch)
                    if pos != -1 and (idx == -1 or pos < idx):
                        idx = pos
                if idx == -1:
                    break
                sentence = buffer[: idx + 1].strip()
                buffer = buffer[idx + 1 :]
                if sentence:
                    print(f"Jarvis: {sentence}", flush=True)
                    self._emit("assistant", sentence)
                    self._emit("status", "Speaking…")
                    yield sentence

            if chunk.get("done", False):
                break

        resp.close()

        if first_token_at is not None:
            log.info(
                "Ollama first token %.2fs; stream %.2fs; metrics=%s",
                first_token_at - started_at, time.monotonic() - started_at,
                response_metrics,
            )

        if tool_calls and _allow_tools and _tool_depth < self.agent_max_steps:
            # Pass the assistant's tool request and the local tool result back
            # to Ollama. Keep tools enabled so it can plan a bounded next step.
            assistant_message = {"role": "assistant", "content": full_response,
                                 "tool_calls": tool_calls}
            followup_messages = list(messages) + [assistant_message]
            remaining_calls = max(0, self.agent_max_tool_calls - _tool_calls_used)
            executed_calls = 0
            for index, call in enumerate(tool_calls):
                function = call.get("function", {})
                name = function.get("name", "")
                arguments = tool_call_arguments(function.get("arguments", {}))
                if index >= remaining_calls:
                    result = "Jarvis stopped this action because the turn reached its safe tool-call limit."
                else:
                    self._emit("status", f"Using {name.replace('_', ' ')}…")
                    try:
                        result = execute_tool(name, arguments, self.cfg)
                    except Exception as exc:
                        log.exception("Tool %s failed", name)
                        result = f"The tool failed: {exc}"
                    executed_calls += 1
                followup_messages.append({"role": "tool", "tool_name": name,
                                          "content": str(result)})
            yield from self.query_ollama_stream(
                user_text, followup_messages, True, _tool_depth + 1,
                _tool_calls_used + executed_calls,
            )
            return

        if tool_calls:
            log.warning("Agent step limit reached for request: %s", user_text)
            message = "I reached my action limit for this request. I’ve stopped safely; tell me what to do next."
            print(f"Jarvis: {message}", flush=True)
            self._emit("assistant", message)
            self._emit("status", "Speaking…")
            yield message
            self._record_exchange(user_text, message)
            return

        # Yield any remaining text after the last sentence boundary
        leftover = buffer.strip()
        if leftover:
            print(f"Jarvis: {leftover}", flush=True)
            self._emit("assistant", leftover)
            self._emit("status", "Speaking…")
            yield leftover
            full_response_text = full_response.strip()
        else:
            full_response_text = full_response.strip()

        self._record_exchange(user_text, full_response_text)

    # kept for async fallback
    def query_ollama(self, user_text: str) -> str:
        """Non-streaming fallback — collects the full response before returning."""
        sentences = list(self.query_ollama_stream(user_text))
        return " ".join(sentences)

    async def query_ollama_async(self, user_text: str) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.query_ollama, user_text)

    # ------------------------------------------------------------------
    # TTS
    # ------------------------------------------------------------------

    @staticmethod
    def speak(text: str):
        """Delegate to jarvis_tts.speak_text."""
        speak_text(text)

    # ------------------------------------------------------------------
    # Convenience: full single turn
    # ------------------------------------------------------------------

    def process_turn(self) -> bool:
        """Run one full turn: record → transcribe → streaming LLM → TTS.

        Speaks each sentence as soon as it is generated rather than waiting
        for the full response — first audio plays within ~1s of asking.
        """
        import queue
        import threading

        audio = self.record_audio()
        if audio is None:
            self._emit("status", "Didn’t catch that — listening again")
            return False

        text = self.transcribe(audio)
        if not text:
            log.warning("Transcript is empty.")
            self._emit("status", "I didn’t catch that — please repeat")
            speak_text("Sorry, I didn’t catch that. Please say it again.", self.cfg)
            return False

        log.debug("User: %s", text)

        # Route clear desktop commands locally for immediate execution. The
        # Ollama tool-call path remains available for less direct requests.
        from jarvis_tools import execute_tool, match_voice_tool
        matched_tool = match_voice_tool(text)
        if matched_tool:
            if not self._last_transcription_confident:
                log.warning("Skipping desktop action because speech confidence was low.")
                self._emit("status", "I didn’t catch that — please repeat")
                speak_text("I’m not sure I heard that correctly. Please say the command again.", self.cfg)
                return False
            tool_name, arguments = matched_tool
            self._emit("status", f"Using {tool_name.replace('_', ' ')}…")
            try:
                reply = execute_tool(tool_name, arguments, self.cfg)
            except Exception as exc:
                log.exception("Local tool %s failed", tool_name)
                reply = f"I couldn’t complete that action: {exc}"
            self._record_exchange(text, reply)
            self._emit("assistant", reply)
            self._emit("status", "Speaking…")
            speak_text(
                reply, self.cfg,
                on_activity=lambda level: self._emit("output_level", level),
            )
            self._emit("status", "Your turn — go ahead")
            return True

        # Drain streamed sentences into a single TTS worker. Keeping Ollama's
        # response stream moving while audio plays avoids serializing model
        # generation behind speech playback.
        spoke_anything = False
        tts_queue = queue.Queue()

        def tts_worker():
            while True:
                sentence = tts_queue.get()
                try:
                    if sentence is None:
                        return
                    speak_text(
                        sentence, self.cfg,
                        on_activity=lambda level: self._emit("output_level", level),
                    )
                finally:
                    tts_queue.task_done()

        tts_thread = threading.Thread(target=tts_worker, daemon=True)
        tts_thread.start()

        try:
            for sentence in self.query_ollama_stream(
                text, _allow_tools=self._last_transcription_confident
            ):
                tts_queue.put(sentence)
                spoke_anything = True
        except Exception:
            # Do not strand a waiting TTS thread if network/stream parsing fails.
            tts_queue.put(None)
            tts_thread.join()
            raise
        else:
            # Wait for queued speech, then stop the sole TTS worker cleanly.
            tts_queue.join()
            tts_queue.put(None)
            tts_queue.join()
            tts_thread.join()

        if not spoke_anything:
            log.warning("Empty LLM response — skipping TTS.")
            return False

        self._emit("status", "Your turn — go ahead")

        return True
