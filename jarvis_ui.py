"""Small bottom-centered, audio-reactive sphere overlay for Jarvis."""
import base64
import math
import queue
import struct
import threading
import time
import tkinter as tk
import zlib


class JarvisWindow:
    KEY = "#010203"
    SIZE = 180
    SPHERE_RADIUS = 53
    TICK_MS = 50
    TRANSITION_SECONDS = 0.8

    PALETTES = {
        "idle": ((124, 92, 255), (74, 144, 226)),
        "standby": ((124, 92, 255), (74, 144, 226)),
        "listening": ((0, 212, 255), (40, 130, 255)),
        "thinking": ((168, 85, 247), (236, 72, 153)),
        "speaking": ((224, 231, 255), (147, 197, 253)),
    }
    BRIGHTNESS = {"idle": 0.14, "standby": 0.14, "listening": 0.90, "thinking": 0.76, "speaking": 0.96}
    EFFECTS = {
        "idle": (0.0, 0.0, 0.0),
        "standby": (0.12, 0.0, 0.0),
        "listening": (1.0, 0.0, 0.0),
        "thinking": (0.0, 1.0, 0.0),
        "speaking": (0.0, 0.0, 1.0),
    }

    def __init__(self, cfg, core_factory, logger):
        self.cfg = cfg
        self.core_factory = core_factory
        self.log = logger
        self.events = queue.Queue()
        self.root = tk.Tk()
        self.root.title("JARVIS")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-transparentcolor", self.KEY)
        except tk.TclError:
            self.root.attributes("-alpha", 0.94)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{self.SIZE}x{self.SIZE}+{(sw-self.SIZE)//2}+{sh-self.SIZE-52}")
        self.root.configure(bg=self.KEY)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.canvas = tk.Canvas(self.root, width=self.SIZE, height=self.SIZE,
                                bg=self.KEY, highlightthickness=0)
        self.canvas.pack()

        self.closing = False
        self.core = None
        self.started_at = time.monotonic()
        self.orb_state = "idle"
        self.transition_from_palette = self.PALETTES["idle"]
        self.transition_to_palette = self.PALETTES["idle"]
        self.transition_from_brightness = self.BRIGHTNESS["idle"]
        self.transition_to_brightness = self.BRIGHTNESS["idle"]
        self.transition_from_effects = self.EFFECTS["idle"]
        self.transition_to_effects = self.EFFECTS["idle"]
        self.transition_started = self.started_at
        self.mic_level = 0.0
        self.tts_level = 0.0
        self.orb_photo = None
        self.geometry_rows = self._build_sphere_geometry()
        self.orb_image = self.canvas.create_image(self.SIZE//2, self.SIZE//2)

        # The native audio-level events below are the Tkinter equivalent of
        # connecting a browser getUserMedia analyser to an orb.
        self.root.after(40, self._animate)
        self.root.after(60, self._drain_events)
        self.worker = threading.Thread(target=self._run_assistant, daemon=True)
        self.worker.start()

    @staticmethod
    def _smoothstep(value):
        value = max(0.0, min(1.0, value))
        return value * value * (3.0 - 2.0 * value)

    @staticmethod
    def _mix(a, b, amount):
        return tuple(round(x + (y - x) * amount) for x, y in zip(a, b))

    def _transition_values(self, now):
        amount = self._smoothstep((now - self.transition_started) / self.TRANSITION_SECONDS)
        palette = tuple(self._mix(self.transition_from_palette[i],
                                  self.transition_to_palette[i], amount)
                        for i in range(2))
        brightness = (self.transition_from_brightness +
                      (self.transition_to_brightness - self.transition_from_brightness) * amount)
        effects = tuple(x + (y-x)*amount for x, y in
                        zip(self.transition_from_effects, self.transition_to_effects))
        return palette, brightness, effects

    def _set_state(self, state):
        if state == self.orb_state:
            return
        now = time.monotonic()
        palette, brightness, effects = self._transition_values(now)
        self.transition_from_palette = palette
        self.transition_to_palette = self.PALETTES[state]
        self.transition_from_brightness = brightness
        self.transition_to_brightness = self.BRIGHTNESS[state]
        self.transition_from_effects = effects
        self.transition_to_effects = self.EFFECTS[state]
        self.transition_started = now
        self.orb_state = state

    def _state_for_status(self, status):
        value = status.lower()
        if "waiting for" in value and "wake word" in value:
            return "standby"
        if "thinking" in value or "using " in value:
            return "thinking"
        if "speaking" in value:
            return "speaking"
        if any(part in value for part in ("listening", "your turn", "didn’t catch", "didn't catch")):
            return "listening"
        return "idle"

    def connectMicToOrb(self, level):
        """Receive normalized live microphone RMS from JarvisCore's PyAudio stream."""
        try:
            self.mic_level = max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            self.mic_level = 0.0

    def connectTtsToOrb(self, level):
        """Receive TTS utterance/word activity for speaking animation."""
        try:
            self.tts_level = max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            self.tts_level = 0.0

    def _emit(self, event, value=""):
        self.events.put((event, value))

    def _drain_events(self):
        if self.closing:
            return
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == "status":
                    self._set_state(self._state_for_status(value))
                elif event == "audio_level":
                    self.connectMicToOrb(value)
                elif event == "output_level":
                    self.connectTtsToOrb(value)
                elif event == "error":
                    self.log.error("Jarvis stopped: %s", value)
        except queue.Empty:
            pass
        self.root.after(60, self._drain_events)

    @staticmethod
    def _png_chunk(kind, data):
        payload = kind + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload) & 0xffffffff)

    def _build_sphere_geometry(self):
        """Cache static lighting math so animation frames stay inexpensive."""
        radius = self.SPHERE_RADIUS
        rows = []
        light = (-0.38, -0.48, 0.79)
        half = (-0.23, -0.29, 0.93)
        norm = math.sqrt(sum(v*v for v in half))
        half = tuple(v / norm for v in half)
        for y in range(self.SIZE):
            row = []
            ny = (y - self.SIZE/2) / radius
            for x in range(self.SIZE):
                nx = (x - self.SIZE/2) / radius
                r2 = nx*nx + ny*ny
                if r2 <= 1.0:
                    nz = math.sqrt(max(0.0, 1.0-r2))
                    diffuse = max(0.0, nx*light[0] + ny*light[1] + nz*light[2])
                    ndoth = max(0.0, nx*half[0] + ny*half[1] + nz*half[2])
                    row.append((1, nx, ny, math.sqrt(r2), math.atan2(ny, nx),
                                max(0.0, min(1.0, 0.5+nx*0.34+ny*0.25)),
                                diffuse, ndoth**36,
                                math.exp(-((nx+0.16)**2+(ny+0.26)**2)/0.16),
                                (1.0-nz)**3*0.20))
                elif r2 <= 2.35:
                    distance = math.sqrt(r2)
                    row.append((2, math.exp(-((distance-1.02)/0.37)**2)))
                else:
                    row.append(None)
            rows.append(row)
        return rows

    def _render_sphere(self, now, palette, brightness, effects):
        size = self.SIZE
        radius = self.SPHERE_RADIUS
        mic_ripple, thinking_swirl, speaking_wave = effects
        idle_weight = max(0.0, 1.0 - mic_ripple - thinking_swirl - speaking_wave)
        breath = 0.90 + 0.10 * (0.5 + 0.5 * math.sin(2 * math.pi * (now - self.started_at) / 4.0))
        mic = self.mic_level
        voice = self.tts_level
        rows = bytearray()
        halo_color = self._mix(palette[0], palette[1], 0.5)
        for y, geometry_row in enumerate(self.geometry_rows):
            rows.append(0)  # PNG filter: None
            for sample in geometry_row:
                if sample is None:
                    rows.extend((0, 0, 0, 0))
                elif sample[0] == 1:
                    (_kind, nx, ny, distance, angle, gradient,
                     diffuse, specular, inner_glow, rim) = sample
                    c0 = self._mix(palette[0], palette[1], gradient)
                    ripple = math.sin(distance*37.0-now*11.0)*0.10*mic_ripple*(0.25+mic) if mic_ripple else 0.0
                    swirl = math.sin(angle*3.0+now*1.8+distance*9.0)*0.08*thinking_swirl if thinking_swirl else 0.0
                    fluid = (math.sin(distance*31.0-now*15.0+math.sin(angle*2.0)*0.8)*
                             0.15*speaking_wave*(0.20+voice)) if speaking_wave else 0.0
                    glow = max(0.0, min(0.32, inner_glow*0.17 + rim + ripple + swirl + fluid))
                    light_gain = max(0.0, min(1.45, brightness * breath * (0.38 + 0.72*diffuse) + glow))
                    rows.extend((
                        min(255, round(c0[0]*light_gain + 255*specular*0.60)),
                        min(255, round(c0[1]*light_gain + 255*specular*0.64)),
                        min(255, round(c0[2]*light_gain + 255*specular*0.72)),
                        255,
                    ))
                else:
                    _kind, halo = sample
                    state_power = max(mic_ripple, thinking_swirl, speaking_wave)
                    alpha = round(72 * halo * (0.24 + 0.76 * state_power) *
                                  (1.0 if not idle_weight else 0.58))
                    rows.extend((halo_color[0], halo_color[1], halo_color[2], alpha))

        header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
        png = (b"\x89PNG\r\n\x1a\n" + self._png_chunk(b"IHDR", header) +
               self._png_chunk(b"IDAT", zlib.compress(bytes(rows), 1)) +
               self._png_chunk(b"IEND", b""))
        return tk.PhotoImage(data=base64.b64encode(png).decode("ascii"), format="png")

    def _draw_thinking_particles(self, now, strength):
        self.canvas.delete("particles")
        if strength < 0.04:
            return
        cx = cy = self.SIZE / 2
        for index in range(9):
            angle = now*0.62 + index*(2*math.pi/9)
            radius = 65 + 7*math.sin(now*1.3 + index)
            x = cx + radius*math.cos(angle)
            y = cy + radius*0.58*math.sin(angle)
            dot = 1.4 + 1.7*(0.5 + 0.5*math.sin(now*2.2 + index))
            alpha_color = "#EC4899" if index % 2 else "#A855F7"
            self.canvas.create_oval(x-dot, y-dot, x+dot, y+dot,
                                    fill=alpha_color, outline="", tags="particles")

    def _animate(self):
        if self.closing:
            return
        now = time.monotonic()
        palette, brightness, effects = self._transition_values(now)
        self.tts_level *= 0.78
        self.orb_photo = self._render_sphere(now, palette, brightness, effects)
        self.canvas.itemconfigure(self.orb_image, image=self.orb_photo)
        self._draw_thinking_particles(now, effects[1])
        self.root.after(self.TICK_MS, self._animate)

    def _run_assistant(self):
        core = None
        try:
            core = self.core_factory(self.cfg, on_event=self._emit)
            self.core = core
            follow_up_turns = max(1, int(self.cfg.get("conversation_follow_up_turns", 5)))
            timeout_turns = max(1, int(self.cfg.get("conversation_timeout_turns", 2)))
            core.start()
            self._emit("status", "Waiting for wake word")
            while core.running:
                try:
                    core.wait_for_wake_word()
                except InterruptedError:
                    break
                silent_turns = 0
                for _ in range(follow_up_turns):
                    if not core.running:
                        break
                    if core.process_turn():
                        silent_turns = 0
                    else:
                        silent_turns += 1
                        if silent_turns >= timeout_turns:
                            break
                self._emit("status", "Waiting for wake word")
        except Exception as exc:
            self.log.exception("Jarvis orb worker stopped: %s", exc)
            self._emit("error", str(exc))
        finally:
            if core is not None:
                core.stop()

    def close(self):
        self.closing = True
        if self.core is not None:
            self.core.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()
