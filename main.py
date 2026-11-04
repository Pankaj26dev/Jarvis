"""Launch the animated Jarvis desktop voice assistant."""
from jarvis_logger import setup_logging, get_logger
from jarvis_config import load_config


def create_core(cfg, on_event=None):
    """Import the audio/AI stack only after the orb is already on screen."""
    from jarvis_core import JarvisCore
    return JarvisCore(cfg, on_event=on_event)


def main():
    cfg = load_config()
    log_cfg = cfg.get("logging", {})
    setup_logging(
        level=log_cfg.get("level", "INFO"),
        log_file=log_cfg.get("file", "jarvis.log"),
        console=log_cfg.get("console", True),
    )
    log = get_logger(__name__)
    log.info("=== Jarvis desktop assistant starting ===")
    from jarvis_ui import JarvisWindow
    log.info("Loading Jarvis orb UI.")
    JarvisWindow(cfg, create_core, log).run()
    log.info("=== Jarvis desktop assistant stopped ===")


if __name__ == "__main__":
    main()
