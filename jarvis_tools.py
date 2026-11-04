"""Explicit, allowlisted desktop tools for Jarvis voice commands.

These tools intentionally do not expose a shell or arbitrary command execution.
"""
import ctypes
import difflib
import json
import os
import platform
import re
import shutil
import subprocess
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from jarvis_logger import get_logger

log = get_logger(__name__)


_APP_COMMANDS = {
    "notepad": ["notepad.exe"],
    "calculator": ["calc.exe"],
    "calc": ["calc.exe"],
    "file explorer": ["explorer.exe"],
    "explorer": ["explorer.exe"],
}
_APP_SUFFIXES = {".exe", ".lnk", ".appref-ms"}
_BLOCKED_ITEM_SUFFIXES = {
    ".exe", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".py", ".pyw",
    ".msi", ".msp", ".scr", ".dll", ".lnk", ".appref-ms",
}
_SKIP_DIRS = {
    "appdata", "windows", "program files", "program files (x86)",
    "$recycle.bin", "system volume information", "node_modules", ".git",
    "__pycache__",
}
_START_APPS_CACHE = None
_START_APPS_CACHE_TIME = 0.0


def _normalize(value):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value).lower()).split())


def _expand_path(value):
    return Path(os.path.expandvars(str(value))).expanduser()


def _start_path(path):
    """Open a resolved file, shortcut, or folder with its registered handler."""
    path = Path(path)
    if os.name == "nt":
        os.startfile(str(path))
    else:
        subprocess.Popen([str(path)], close_fds=True)


def _app_alias(cfg, query):
    aliases = cfg.get("app_aliases", {})
    if not isinstance(aliases, dict):
        return None
    target = _normalize(query)
    for alias, path in aliases.items():
        if _normalize(alias) == target:
            candidate = _expand_path(path)
            if candidate.is_file() and candidate.suffix.lower() in _APP_SUFFIXES:
                return candidate
            return None
    return None


def _start_menu_roots():
    roots = []
    for env_name in ("APPDATA", "PROGRAMDATA"):
        base = os.environ.get(env_name)
        if base:
            root = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            if root.is_dir():
                roots.append(root)
    return roots


def _windows_start_apps():
    """Read Windows' installed-app catalog, including packaged Store apps.

    Get-StartApps returns app IDs for UWP/Store apps that have no normal .lnk
    file or classic uninstall executable entry (Spotify commonly lands here).
    Cache briefly so launching several apps doesn't start PowerShell each time.
    """
    global _START_APPS_CACHE, _START_APPS_CACHE_TIME
    if os.name != "nt":
        return []
    if _START_APPS_CACHE is not None and time.monotonic() - _START_APPS_CACHE_TIME < 60:
        return _START_APPS_CACHE

    command = (
        "$ErrorActionPreference='Stop'; "
        "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, check=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        data = json.loads(result.stdout) if result.stdout.strip() else []
        if isinstance(data, dict):
            data = [data]
        apps = [
            (str(item.get("Name", "")).strip(), str(item.get("AppID", "")).strip())
            for item in data
            if isinstance(item, dict) and item.get("Name") and item.get("AppID")
        ]
        _START_APPS_CACHE = apps
        _START_APPS_CACHE_TIME = time.monotonic()
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        log.warning("Could not read Windows installed-app catalog: %s", exc)
        # Cache the failure briefly to avoid repeatedly spawning PowerShell.
        _START_APPS_CACHE = []
        _START_APPS_CACHE_TIME = time.monotonic() - 50
    return _START_APPS_CACHE


def _find_windows_start_app(query):
    matches = []
    for display_name, app_id in _windows_start_apps():
        score = _score_name(query, display_name)
        if score:
            matches.append((score, display_name, app_id))
    matches.sort(key=lambda item: item[0], reverse=True)
    if not matches:
        return None, None
    best_score = matches[0][0]
    close = [item for item in matches if best_score - item[0] < 0.04]
    if len(close) > 1:
        names = "; ".join(item[1] for item in close[:4])
        return None, f"I found several installed apps matching that: {names}. Please say the full app name."
    return matches[0], None


def _try_open_windows_start_app(query):
    """Open a catalogued Windows app; return None when no app matches."""
    match, ambiguity = _find_windows_start_app(query)
    if ambiguity:
        return ambiguity
    if not match:
        return None
    _score, display_name, app_id = match
    explorer = shutil.which("explorer.exe") or "explorer.exe"
    try:
        subprocess.Popen(
            [explorer, f"shell:AppsFolder\\{app_id}"],
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return f"Opening {display_name}."
    except OSError as exc:
        log.warning("Could not launch Windows app %s (%s): %s", display_name, app_id, exc)
        return f"I found {display_name}, but Windows couldn’t launch it: {exc}"


def list_installed_apps(filter_text=""):
    """Return installed app names from Windows' app catalog and Start Menu."""
    needle = _normalize(filter_text)
    names = {name for name, _app_id in _windows_start_apps()}
    if needle:
        for _score, shortcut in _ranked_matches(
            filter_text, _start_menu_roots(), {".lnk"}, max_entries=5000, time_limit=1.5
        ):
            names.add(shortcut.stem)
    if needle:
        names = {name for name in names if needle in _normalize(name)}
    if not names:
        return "I couldn’t find matching installed apps in the Windows app catalog."
    return "Installed apps: " + ", ".join(sorted(names, key=str.casefold)[:80])


def _registered_app_matches(query):
    """Find installed desktop apps through Windows registration as well as shortcuts.

    Many Windows Store/desktop apps have no shortcut in the two Start Menu
    folders. Use uninstall registration only as a name-to-executable index;
    never execute a command line supplied by the registry.
    """
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []

    roots = (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    found = []
    seen = set()
    for hive, key_path in roots:
        try:
            with winreg.OpenKey(hive, key_path) as root:
                count = winreg.QueryInfoKey(root)[0]
                for index in range(count):
                    try:
                        sub_name = winreg.EnumKey(root, index)
                        with winreg.OpenKey(root, sub_name) as key:
                            display_name, _ = winreg.QueryValueEx(key, "DisplayName")
                            display_icon, _ = winreg.QueryValueEx(key, "DisplayIcon")
                    except (OSError, ValueError):
                        continue
                    score = _score_name(query, display_name)
                    if not score:
                        continue
                    # DisplayIcon commonly looks like: "C:\\App\\app.exe",0
                    raw_path = str(display_icon).strip()
                    match = re.search(
                        r'"([^"]+\.(?:exe|lnk))"|([^,]+?\.(?:exe|lnk))(?:\s*,|$)',
                        raw_path, re.I,
                    )
                    if not match:
                        continue
                    executable = Path(os.path.expandvars(match.group(1) or match.group(2)))
                    if not executable.is_file():
                        continue
                    key = str(executable).casefold()
                    if key not in seen:
                        seen.add(key)
                        found.append((score, executable))
        except OSError:
            continue
    return sorted(found, key=lambda item: item[0], reverse=True)


def _find_installed_app(query):
    """Resolve one unambiguous installed app from Start Menu or registration."""
    matches = _ranked_matches(query, _start_menu_roots(), {".lnk"},
                              max_entries=5000, time_limit=1.5)
    matches.extend(_registered_app_matches(query))
    # Collapse duplicate shortcut/registration results by normalized display path.
    unique = {}
    for score, path in matches:
        key = str(path).casefold()
        if key not in unique or score > unique[key][0]:
            unique[key] = (score, path)
    return _select_match(sorted(unique.values(), key=lambda item: item[0], reverse=True), "app")


def _known_user_folders():
    """Resolve Windows known folders, including OneDrive redirection."""
    names = {
        "desktop": ("Desktop", "Desktop"),
        "downloads": ("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"),
        "documents": ("Personal", "Documents"),
        "pictures": ("My Pictures", "Pictures"),
        "music": ("My Music", "Music"),
        "videos": ("My Video", "Videos"),
    }
    resolved = {}
    if os.name == "nt":
        try:
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                for name, (registry_value, fallback) in names.items():
                    try:
                        value, _kind = winreg.QueryValueEx(key, registry_value)
                        resolved[name] = Path(os.path.expandvars(value))
                    except OSError:
                        resolved[name] = Path.home() / fallback
        except OSError:
            pass
    for name, (_registry_value, fallback) in names.items():
        resolved.setdefault(name, Path.home() / fallback)
        resolved[f"my {name}"] = resolved[name]
    return resolved


def _score_name(query, candidate):
    query_norm = _normalize(query)
    candidate_norm = _normalize(candidate)
    if not query_norm or not candidate_norm:
        return 0.0
    if candidate_norm == query_norm:
        return 1.0
    if candidate_norm.startswith(query_norm):
        return 0.93
    if query_norm in candidate_norm:
        return 0.88
    ratio = difflib.SequenceMatcher(None, query_norm, candidate_norm).ratio()
    return ratio if ratio >= 0.82 else 0.0


def _ranked_matches(query, roots, extensions, include_directories=False,
                    max_entries=12000, time_limit=2.5):
    """Bounded filename search; never traverses system or hidden directories."""
    deadline = time.monotonic() + time_limit
    matches = []
    seen = set()
    visited = 0
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for current, dirs, files in os.walk(root, topdown=True, onerror=lambda _error: None):
            current_path = Path(current)
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in _SKIP_DIRS]
            names = list(files)
            if include_directories:
                names.extend(dirs)
            for name in names:
                visited += 1
                if visited > max_entries or time.monotonic() >= deadline:
                    return sorted(matches, key=lambda item: item[0], reverse=True)
                candidate = current_path / name
                suffix = candidate.suffix.lower()
                if extensions is not None and suffix not in extensions:
                    continue
                if candidate.name.startswith("."):
                    continue
                score = max(_score_name(query, candidate.stem),
                            _score_name(query, candidate.name))
                if score:
                    key = str(candidate).casefold()
                    if key not in seen:
                        seen.add(key)
                        matches.append((score, candidate))
    return sorted(matches, key=lambda item: item[0], reverse=True)


def _default_search_roots(cfg):
    roots = list(dict.fromkeys(_known_user_folders().values()))
    configured = cfg.get("tool_search_roots", [])
    if isinstance(configured, str):
        configured = [configured]
    if isinstance(configured, list):
        roots.extend(_expand_path(path) for path in configured if isinstance(path, str))
    return roots


def _ambiguity_message(matches, label):
    examples = [f"{path.name} in {path.parent.name}" for _, path in matches[:3]]
    return f"I found several {label} matches: " + "; ".join(examples) + ". Please say more of the name."


def _select_match(matches, label):
    if not matches:
        return None, None
    best_score = matches[0][0]
    close = [item for item in matches if best_score - item[0] < 0.04]
    if len(close) > 1:
        return None, _ambiguity_message(close, label)
    return matches[0][1], None


def _chrome_executable():
    found = shutil.which("chrome.exe") or shutil.which("chrome")
    if found:
        return found
    for env_name, suffix in (
        ("PROGRAMFILES", r"Google\Chrome\Application\chrome.exe"),
        ("PROGRAMFILES(X86)", r"Google\Chrome\Application\chrome.exe"),
        ("LOCALAPPDATA", r"Google\Chrome\Application\chrome.exe"),
    ):
        root = os.environ.get(env_name)
        if root:
            candidate = Path(root) / suffix
            if candidate.is_file():
                return str(candidate)
    return None


def open_url_in_chrome(url):
    """Open a URL in Chrome, using the default browser only as fallback."""
    chrome = _chrome_executable()
    if chrome:
        subprocess.Popen([chrome, "--new-window", url], close_fds=True)
        return "Opened in Chrome."
    webbrowser.open(url)
    return "Chrome wasn’t found, so I opened it in your default browser."


def open_app(app_name, cfg=None):
    """Open a known app or a matching Windows Start Menu shortcut."""
    cfg = cfg or {}
    name = " ".join(str(app_name).lower().split())
    if name in {"chrome", "google chrome", "chrome browser", "google chrome browser"}:
        chrome = _chrome_executable()
        if chrome:
            try:
                subprocess.Popen([chrome], close_fds=True)
                return "Opening Chrome."
            except OSError:
                # Try the user's Start Menu shortcut below.
                pass
    command = _APP_COMMANDS.get(name)
    if command:
        try:
            subprocess.Popen(command, close_fds=True)
            return f"Opening {name.title()}."
        except OSError:
            # Some Windows app aliases are not on PATH; continue to shortcut search.
            pass

    configured = _app_alias(cfg, app_name)
    if configured:
        try:
            _start_path(configured)
            return f"Opening {app_name}."
        except OSError as exc:
            return f"I couldn’t open {app_name}: {exc}"

    # This catalog covers packaged apps such as Spotify without requiring a
    # per-app alias or hard-coded executable name.
    catalog_result = _try_open_windows_start_app(app_name)
    if catalog_result is not None:
        return catalog_result

    path, ambiguity = _find_installed_app(app_name)
    if ambiguity:
        return ambiguity
    if path:
        try:
            _start_path(path)
            return f"Opening {path.stem}."
        except OSError as exc:
            return f"I found {path.stem}, but couldn’t launch it: {exc}"
    return (f"I couldn’t find an app called {app_name} in the Start Menu. "
            "You can add its executable or shortcut path to app_aliases in config.json.")


def open_local_item(query, kind="any", cfg=None):
    """Search user folders and open a matching ordinary file or folder."""
    cfg = cfg or {}
    query = " ".join(str(query).strip().split())
    kind = str(kind).lower().strip()
    if not query or len(query) > 200:
        return "Tell me a shorter name for the app, file, or folder."

    # Standard library folders are direct targets, not searches within
    # themselves. Registry lookup honors redirected/OneDrive locations.
    folder_name = _normalize(query)
    known_folders = _known_user_folders()
    if kind in {"any", "folder"} and folder_name in known_folders:
        folder = known_folders[folder_name]
        if folder.is_dir():
            try:
                _start_path(folder)
                return f"Opening your {folder_name} folder."
            except OSError as exc:
                return f"I found your {folder_name} folder but couldn’t open it: {exc}"

    # Exact paths and configured aliases are the fastest, most reliable route.
    aliases = cfg.get("item_aliases", {})
    if isinstance(aliases, dict):
        for alias, configured_path in aliases.items():
            if _normalize(alias) == _normalize(query):
                candidate = _expand_path(configured_path)
                if candidate.exists() and candidate.suffix.lower() not in _BLOCKED_ITEM_SUFFIXES:
                    try:
                        _start_path(candidate)
                        return f"Opening {candidate.name}."
                    except OSError as exc:
                        return f"I couldn’t open {candidate.name}: {exc}"
                return f"I couldn’t open the configured item for {alias}."

    candidate = _expand_path(query)
    if candidate.exists():
        if candidate.suffix.lower() in _BLOCKED_ITEM_SUFFIXES:
            return "I don’t open executable files or scripts through file search."
        try:
            _start_path(candidate)
            return f"Opening {candidate.name or str(candidate)}."
        except OSError as exc:
            return f"I found it, but couldn’t open it: {exc}"

    if kind in {"app", "application", "browser"}:
        return open_app(query, cfg)

    # A plain “open <name>” checks installed apps first, then personal files.
    if kind == "any":
        app_alias = _app_alias(cfg, query)
        if app_alias:
            try:
                _start_path(app_alias)
                return f"Opening {query}."
            except OSError as exc:
                return f"I found the app but couldn’t open it: {exc}"
        catalog_result = _try_open_windows_start_app(query)
        if catalog_result is not None:
            return catalog_result
        app_path, ambiguity = _find_installed_app(query)
        if ambiguity:
            return ambiguity
        if app_path:
            try:
                _start_path(app_path)
                return f"Opening {app_path.stem}."
            except OSError as exc:
                return f"I found the app but couldn’t launch it: {exc}"

    roots = _default_search_roots(cfg)
    extensions = None
    matches = _ranked_matches(
        query, roots, extensions, include_directories=kind in {"any", "folder"}
    )
    matches = [(score, path) for score, path in matches
               if path.suffix.lower() not in _BLOCKED_ITEM_SUFFIXES
               and (kind != "folder" or path.is_dir())
               and (kind not in {"file", "document"} or path.is_file())]
    path, ambiguity = _select_match(matches, "file or folder")
    if ambiguity:
        return ambiguity
    if path:
        try:
            _start_path(path)
            return f"Opening {path.name}."
        except OSError as exc:
            return f"I found {path.name}, but couldn’t open it: {exc}"
    return (f"I couldn’t find {query} in your Desktop, Documents, Downloads, "
            "Pictures, Music, Videos, or configured search folders.")


def get_pc_status():
    """Return basic, read-only system information."""
    cpu = platform.processor() or "the system processor"
    cores = os.cpu_count() or 0
    memory_text = ""
    try:
        import psutil  # optional; use it when already installed
        mem = psutil.virtual_memory()
        memory_text = f"{mem.available / (1024 ** 3):.1f} gigabytes of {mem.total / (1024 ** 3):.1f} gigabytes RAM available"
    except ImportError:
        if os.name == "nt":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                memory_text = f"{status.ullAvailPhys / (1024 ** 3):.1f} gigabytes of {status.ullTotalPhys / (1024 ** 3):.1f} gigabytes RAM available"
    root = Path.home().anchor or "C:\\"
    try:
        disk = shutil.disk_usage(root)
        disk_text = f"{disk.free / (1024 ** 3):.1f} gigabytes free on {root}"
    except OSError:
        disk_text = "disk space unavailable"
    parts = [f"You’re running {platform.system()} {platform.release()}.",
             f"Your processor is {cpu}, with {cores} logical cores."]
    if memory_text:
        parts.append(memory_text + ".")
    parts.append(disk_text + ".")
    return " ".join(parts)


def open_youtube(cfg, play_playlist=False):
    """Open YouTube or a configured playlist in Chrome."""
    url = "https://www.youtube.com/"
    if play_playlist:
        playlist_url = str(cfg.get("youtube_playlist_url", "")).strip()
        parsed = urlparse(playlist_url)
        if parsed.hostname not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            url = "https://www.youtube.com/feed/playlists"
            browser_status = open_url_in_chrome(url)
            return ("I opened your YouTube playlists. Add your playlist link to the "
                    "youtube_playlist_url setting in config.json and I can play it directly. "
                    + browser_status)
        playlist_id = parse_qs(parsed.query).get("list", [None])[0]
        if not playlist_id:
            return "The configured YouTube link doesn’t include a playlist ID."
        query = urlencode({"list": playlist_id, "autoplay": "1"})
        url = urlunparse(("https", "www.youtube.com", "/watch", "", query, ""))
    browser_status = open_url_in_chrome(url)
    if browser_status != "Opened in Chrome.":
        return f"{browser_status} " + ("I opened the playlist." if play_playlist else "I opened YouTube.")
    return "Playing your YouTube playlist." if play_playlist else "Opening YouTube in Chrome."


def execute_tool(name, arguments, cfg):
    """Run one named local tool with validated arguments."""
    if name == "open_app":
        app = str(arguments.get("app_name", "")).strip().lower()
        if len(app) > 40:
            return "That app name is too long."
        return open_app(app, cfg)
    if name == "open_local_item":
        query = str(arguments.get("query", "")).strip()
        kind = str(arguments.get("kind", "any")).strip().lower()
        if kind not in {"any", "app", "application", "browser", "file", "folder", "document"}:
            return "Choose an app, file, folder, document, or general item."
        return open_local_item(query, kind, cfg)
    if name == "open_youtube":
        return open_youtube(cfg, bool(arguments.get("play_playlist", False)))
    if name == "get_pc_status":
        return get_pc_status()
    if name == "list_installed_apps":
        filter_text = str(arguments.get("filter_text", "")).strip()
        if len(filter_text) > 80:
            return "That app search is too long."
        return list_installed_apps(filter_text)
    return "That tool isn’t available."


TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "open_app",
     "description": "Open any installed app by its visible Windows app name, using Windows' installed-app catalog, Start Menu shortcut, or a configured alias. Do not invent app names.",
     "parameters": {"type": "object", "properties": {"app_name": {"type": "string", "description": "Installed app name, such as Spotify or Notepad"}}, "required": ["app_name"]}}},
    {"type": "function", "function": {"name": "open_local_item",
     "description": "Search the user's common folders and configured search roots for a file or folder, then open it. It does not run executable files or scripts.",
     "parameters": {"type": "object", "properties": {
         "query": {"type": "string", "description": "The app, file, or folder name to find"},
         "kind": {"type": "string", "enum": ["any", "app", "browser", "file", "folder", "document"], "description": "Expected item type"}},
         "required": ["query", "kind"]}}},
    {"type": "function", "function": {"name": "open_youtube",
     "description": "Open YouTube or play the user's configured YouTube playlist.",
     "parameters": {"type": "object", "properties": {"play_playlist": {"type": "boolean"}}, "required": ["play_playlist"]}}},
    {"type": "function", "function": {"name": "get_pc_status",
     "description": "Read basic PC operating system, processor, memory, and disk status.",
     "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "list_installed_apps",
     "description": "List installed Windows app names, optionally filtered by a name fragment. Use this to discover app names before opening one.",
     "parameters": {"type": "object", "properties": {
         "filter_text": {"type": "string", "description": "Optional app name fragment"}}, "required": []}}},
]


def match_voice_tool(text):
    """Fast local routing for clear action requests; returns (tool, args) or None."""
    normalized = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    normalized = " ".join(normalized.split())
    # Let the agent plan compound requests instead of treating the entire
    # phrase as one app/file name and stopping after a single local action.
    if re.search(r"\b(?:and then|then|after that|and)\b", normalized) and re.search(
        r"\b(?:open|launch|start|play|find|locate|search)\b", normalized
    ):
        return None
    if ("playlist" in normalized and any(w in normalized for w in ("play", "start"))):
        return "open_youtube", {"play_playlist": True}
    if any(x in normalized for x in ("youtube", "you tube")) and any(w in normalized for w in ("open", "launch", "go to", "play")):
        return "open_youtube", {"play_playlist": False}
    if any(phrase in normalized for phrase in ("pc status", "computer status", "system info", "pc specs", "computer specs", "how much ram", "free memory", "free disk", "how much memory", "how much storage")):
        return "get_pc_status", {}
    match = re.search(r"\b(?:open|launch|start)\s+(chrome|notepad|calculator|calc|file explorer|explorer)\b", normalized)
    if match:
        return "open_app", {"app_name": match.group(1)}
    match = re.match(
        r"^(?:please\s+)?(?:open(?:\s+up)?|launch|start|find|locate|search for and open)\s+"
        r"(?:(?:the|a|an|my)\s+)?"
        r"(?:(app|application|browser|file|folder|document)\s+)?(.+)$",
        normalized,
    )
    if match:
        kind, query = match.groups()
        query = re.sub(r"^(?:(?:up|the|a|an|my|named|called)\s+)+", "", query.strip())
        trailing_kind = re.search(r"\s+(file|folder|document|app|application|browser)$", query)
        if trailing_kind:
            kind = kind or trailing_kind.group(1)
            query = query[:trailing_kind.start()].strip()
        if query:
            if kind in {"app", "application", "browser"}:
                return "open_app", {"app_name": query}
            item_kind = "file" if kind == "document" else (kind or "any")
            return "open_local_item", {"query": query, "kind": item_kind}
    return None


def should_offer_tools(text):
    """Keep tool definitions out of ordinary chat prompts to reduce overhead."""
    normalized = _normalize(text)
    if re.search(r"\b(open|launch|start|find|locate|search)\b", normalized):
        return True
    if any(phrase in normalized for phrase in (
        "installed app", "apps installed", "list apps", "show apps", "what apps",
    )):
        return True
    if re.search(r"\b(play|start)\b", normalized) and any(
        word in normalized for word in ("youtube", "you tube", "playlist")
    ):
        return True
    system_terms = ("pc", "computer", "system", "cpu", "ram", "memory", "disk", "storage")
    status_terms = ("status", "spec", "specs", "usage", "available", "free", "how much", "information", "info")
    return any(word in normalized.split() for word in system_terms) and any(
        phrase in normalized for phrase in status_terms
    )


def tool_call_arguments(value):
    """Normalize Ollama tool arguments, including JSON-string responses."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}
