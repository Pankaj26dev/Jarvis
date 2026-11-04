"""Backward-compatible launcher for the current Jarvis desktop app.

The previous file started audio/model setup during import and duplicated the
pipeline. Keep the old entry-point name, but route it through the maintained UI.
"""
from main import main


if __name__ == "__main__":
    main()
