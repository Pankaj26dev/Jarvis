import os
import subprocess

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None


def _make_engine(config=None):
    """Create and configure a fresh pyttsx3 engine instance."""
    tts_cfg = (config or {}).get("tts", {})
    engine = pyttsx3.init()
    engine.setProperty("rate", int(os.getenv("JARVIS_TTS_RATE", tts_cfg.get("rate", 175))))
    volume = float(os.getenv("JARVIS_TTS_VOLUME", tts_cfg.get("volume", 1.0)))
    engine.setProperty("volume", max(0.0, min(1.0, volume)))
    voice_id = os.getenv("JARVIS_TTS_VOICE", tts_cfg.get("voice_id"))
    if voice_id:
        engine.setProperty("voice", voice_id)
    else:
        # Prefer a locally installed female voice when one is available.
        for voice in engine.getProperty("voices"):
            gender = str(getattr(voice, "gender", "")).lower()
            if "female" in gender or any(
                name in voice.name.lower() for name in ("zira", "aria", "jenny", "hazel")
            ):
                engine.setProperty("voice", voice.id)
                break
    return engine


def _speak_with_windows_speech(text, config=None, on_activity=None):
    """Windows fallback that selects an installed female SAPI voice if possible."""
    tts_cfg = (config or {}).get("tts", {})
    rate = int(os.getenv("JARVIS_TTS_RATE", tts_cfg.get("rate", 185)))
    volume = float(os.getenv("JARVIS_TTS_VOLUME", tts_cfg.get("volume", 1.0)))
    # System.Speech uses -10..10 while pyttsx3 uses words-per-minute.
    sapi_rate = max(-10, min(10, round((rate - 175) / 12)))
    sapi_volume = max(0, min(100, round(volume * 100)))
    preferred = os.getenv("JARVIS_TTS_VOICE", tts_cfg.get("voice_id")) or ""
    preferred = str(preferred).replace("'", "''")
    powershell_script = (
        "Add-Type -AssemblyName System.Speech; "
        "$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$speaker.Rate = " + str(sapi_rate) + "; "
        "$speaker.Volume = " + str(sapi_volume) + "; "
        "$voices = $speaker.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo }; "
        "$chosen = $null; "
        "if ('" + preferred + "') { $chosen = $voices | Where-Object { $_.Name -like '*" + preferred + "*' -or $_.Id -like '*" + preferred + "*' } | Select-Object -First 1 }; "
        "if (-not $chosen) { $chosen = $voices | Where-Object { $_.Gender -eq [System.Speech.Synthesis.VoiceGender]::Female } | Select-Object -First 1 }; "
        "if ($chosen) { $speaker.SelectVoice($chosen.Name) }; "
        "$speaker.Speak([Console]::In.ReadToEnd()); $speaker.Dispose()"
    )
    if on_activity:
        on_activity(0.45)
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", powershell_script],
        input=text, text=True, check=True, capture_output=True,
    )
    if on_activity:
        on_activity(0.0)


def speak_text(text, config=None, on_activity=None):
    """Speak text through the default system voice.

    A fresh engine is created for every call because pyttsx3 on Windows
    enters a broken state after the first runAndWait() if the engine is reused.

    Returns True when speech completes and False when the TTS engine fails.
    """
    text = " ".join(str(text).split())
    if not text:
        return False

    try:
        if pyttsx3 is not None:
            engine = _make_engine(config)
            if on_activity:
                engine.connect("started-utterance", lambda _name: on_activity(0.35))
                engine.connect(
                    "started-word",
                    lambda _name, _location, _length: on_activity(0.9),
                )
                engine.connect(
                    "finished-utterance",
                    lambda _name, _completed: on_activity(0.0),
                )
            engine.say(text)
            engine.runAndWait()
            engine.stop()
        else:
            if os.name != "nt":
                raise RuntimeError("Install pyttsx3 to enable TTS on this platform.")
            _speak_with_windows_speech(text, config, on_activity)
        return True
    except Exception as error:
        # pyttsx3 may be installed but its SAPI COM driver can still fail to
        # initialize. Retry with Windows' own speech API before giving up.
        if os.name == "nt" and pyttsx3 is not None:
            try:
                _speak_with_windows_speech(text, config, on_activity)
                return True
            except Exception as fallback_error:
                error = fallback_error
        if on_activity:
            try:
                on_activity(0.0)
            except Exception:
                pass
        print(f"[ERROR] TTS failed: {error}", flush=True)
        return False


if __name__ == "__main__":
    message = os.getenv("JARVIS_TTS_TEST_TEXT", "Hello, I am Jarvis.")
    if not speak_text(message):
        raise SystemExit(1)
    print("[INFO] TTS test completed.", flush=True)
