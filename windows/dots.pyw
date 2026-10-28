r"""dots for Windows: a glyph-only status of your Claude Code sessions, in a tiny always-on-top strip.

Same rules as linux/dotsd.py. Reads Claude Code's own session registry
(%USERPROFILE%\.claude\sessions\<pid>.json) and never writes to it. Only the *.json entries are
opened: the private *.key files, transcripts and sockets are left alone, and nothing is ever sent
to a session.

One glyph per project folder, left to right in the order of the `order` file:
    ◇  waiting for you (permission prompt or question)
    ◆  finished, and you haven't focused that project's editor window since
    ◦  working
    ?  unknown status (registry format changed by a Claude Code update?)
    •  finished, already seen

Drag the strip with the left mouse button; right-click it for the legend or to quit.

    pythonw dots.pyw          run it (double-clicking the file does the same)
    py dots.pyw --install     start it now and at every login
    py dots.pyw --uninstall   stop it, remove the login entry and its data
    py dots.pyw --legend      print which glyph is which project right now

Standard library only (tkinter, ctypes, winreg). Python 3.8+.
"""

import ctypes
import json
import ntpath
import os
import re
import subprocess
import sys
import time
import traceback
from types import SimpleNamespace

try:
    from ctypes import wintypes
except (ImportError, ValueError):  # only needed on Windows
    wintypes = None

REGISTRY = os.path.join(os.path.expanduser('~'), '.claude', 'sessions')
DATA = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'dots')
ORDER_FILE = os.path.join(DATA, 'order')
SEEN_FILE = os.path.join(DATA, 'seen.json')
PLACE_FILE = os.path.join(DATA, 'place.json')
QUIT_FILE = os.path.join(DATA, 'quit')
LOG_FILE = os.path.join(DATA, 'dots.log')
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_VALUE = 'dots'
MUTEX = 'Local\\claude-dots'

# Most urgent first: a folder with several sessions shows the most urgent state.
GLYPHS = {'waiting': '◇', 'new': '◆', 'working': '◦', 'unknown': '?', 'done': '•'}
LABELS = {'waiting': 'waiting for you', 'new': 'finished, not seen yet', 'working': 'working',
          'unknown': 'unknown status', 'done': 'finished, seen'}
URGENCY = list(GLYPHS)
SEPARATOR = '\u2009'  # thin space
EDITOR_EXE = re.compile(r'code|codium|cursor|windsurf', re.I)  # VS Code and its forks
INFRASTRUCTURE = ('daemon', 'daemon-worker')  # background session kinds that aren't conversations
FONT = ('Segoe UI Symbol', 10)
ORDER_HEADER = ('# Left-to-right order of the dots: one project folder per line.\n'
                '# Reorder freely; new projects are appended automatically.\n')

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 259
ERROR_ALREADY_EXISTS = 183
CLR_INVALID = 0xFFFFFFFF
SPI_GETWORKAREA = 0x0030
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79

_api = None
_mutex = None


def win32():
    """The Win32 functions used here, with their signatures, loaded on first use."""
    global _api
    if _api is None:
        w, ptr = wintypes, ctypes.POINTER
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)

        def fn(dll, name, restype, *argtypes):
            f = getattr(dll, name)
            f.restype, f.argtypes = restype, argtypes
            return f

        _api = SimpleNamespace(
            OpenProcess=fn(kernel32, 'OpenProcess', w.HANDLE, w.DWORD, w.BOOL, w.DWORD),
            CloseHandle=fn(kernel32, 'CloseHandle', w.BOOL, w.HANDLE),
            GetExitCodeProcess=fn(kernel32, 'GetExitCodeProcess', w.BOOL, w.HANDLE, ptr(w.DWORD)),
            GetProcessTimes=fn(kernel32, 'GetProcessTimes', w.BOOL, w.HANDLE, ptr(w.FILETIME),
                               ptr(w.FILETIME), ptr(w.FILETIME), ptr(w.FILETIME)),
            QueryFullProcessImageNameW=fn(kernel32, 'QueryFullProcessImageNameW', w.BOOL, w.HANDLE,
                                          w.DWORD, w.LPWSTR, ptr(w.DWORD)),
            CreateMutexW=fn(kernel32, 'CreateMutexW', w.HANDLE, w.LPVOID, w.BOOL, w.LPCWSTR),
            OpenMutexW=fn(kernel32, 'OpenMutexW', w.HANDLE, w.DWORD, w.BOOL, w.LPCWSTR),
            GetForegroundWindow=fn(user32, 'GetForegroundWindow', w.HWND),
            GetWindowThreadProcessId=fn(user32, 'GetWindowThreadProcessId', w.DWORD, w.HWND,
                                        ptr(w.DWORD)),
            GetWindowTextLengthW=fn(user32, 'GetWindowTextLengthW', ctypes.c_int, w.HWND),
            GetWindowTextW=fn(user32, 'GetWindowTextW', ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int),
            FindWindowW=fn(user32, 'FindWindowW', w.HWND, w.LPCWSTR, w.LPCWSTR),
            FindWindowExW=fn(user32, 'FindWindowExW', w.HWND, w.HWND, w.HWND, w.LPCWSTR, w.LPCWSTR),
            GetWindowRect=fn(user32, 'GetWindowRect', w.BOOL, w.HWND, ptr(w.RECT)),
            GetSystemMetrics=fn(user32, 'GetSystemMetrics', ctypes.c_int, ctypes.c_int),
            SystemParametersInfoW=fn(user32, 'SystemParametersInfoW', w.BOOL, w.UINT, w.UINT,
                                     w.LPVOID, w.UINT),
            SetWindowPos=fn(user32, 'SetWindowPos', w.BOOL, w.HWND, w.HWND, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT),
            GetDC=fn(user32, 'GetDC', w.HDC, w.HWND),
            ReleaseDC=fn(user32, 'ReleaseDC', ctypes.c_int, w.HWND, w.HDC),
            GetPixel=fn(gdi32, 'GetPixel', w.DWORD, w.HDC, ctypes.c_int, ctypes.c_int),
        )
    return _api


_registry_cache = {}  # path -> (mtime_ns, entry)


def read_registry():
    """Registry entries, re-parsing only the files that changed. None when there is no registry."""
    try:
        names = [name for name in os.listdir(REGISTRY) if name.endswith('.json')]  # never the *.key files
    except FileNotFoundError:
        return None
    fresh = {}
    for name in names:
        path = os.path.join(REGISTRY, name)
        try:
            mtime = os.stat(path).st_mtime_ns
            cached = _registry_cache.get(path)
            if cached and cached[0] == mtime:
                fresh[path] = cached
            else:
                with open(path, encoding='utf-8') as f:
                    fresh[path] = (mtime, json.load(f))
        except (OSError, ValueError):
            pass  # removed, locked or half-written: picked up on the next tick
    _registry_cache.clear()
    _registry_cache.update(fresh)
    return [entry for _, entry in fresh.values() if isinstance(entry, dict)]


def process_info(pid):
    """(creation time as a FILETIME, lowercase program name) of a running process, else None."""
    api = win32()
    handle = api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not api.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not api.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        size = wintypes.DWORD(1024)
        path = ctypes.create_unicode_buffer(size.value)
        ok = api.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size))
        created = times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
        return created, ntpath.basename(path.value if ok else '').lower()
    finally:
        api.CloseHandle(handle)


def same_process(entry, created, program):
    """Is this running process the one that wrote `entry`, or did another program get its PID?"""
    try:
        recorded = int(entry['procStartFt'])
    except (KeyError, TypeError, ValueError):
        recorded = None
    if recorded is not None and abs(recorded - created) <= 2 * 10**7:  # 2 s, in 100 ns ticks
        return True
    # Start time missing or in a format this port doesn't know: fall back to the program's name.
    return 'claude' in program or program in ('node.exe', 'bun.exe')


def live_sessions():
    """Entries whose Claude Code process is still running. None when there is no registry."""
    entries = read_registry()
    if entries is None:
        return None
    live = []
    for entry in entries:
        pid, cwd = entry.get('pid'), entry.get('cwd')
        if (not isinstance(pid, int) or not isinstance(cwd, str) or entry.get('spare') is True
                or entry.get('kind') in INFRASTRUCTURE):
            continue
        info = process_info(pid)
        if info is None or not same_process(entry, *info):
            continue  # closed session (its entry stays behind), or its PID went to another program
        live.append(entry)
    return live


def folder_name(cwd):
    return ntpath.basename(cwd.rstrip('\\/'))


def title_folders(title):
    """Folder names in an editor window title ("<file> - <folder> - Visual Studio Code")."""
    parts = title.split(' - ')
    return tuple(parts[1:-1] if len(parts) > 2 else parts[:1])


def focused_folders():
    """Folder names of the focused window, if it's a VS Code (or fork) window."""
    api = win32()
    hwnd = api.GetForegroundWindow()
    if not hwnd:
        return ()
    pid = wintypes.DWORD()
    api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    info = process_info(pid.value)
    if info is None or not EDITOR_EXE.search(info[1]):
        return ()
    length = api.GetWindowTextLengthW(hwnd)
    title = ctypes.create_unicode_buffer(length + 1)
    api.GetWindowTextW(hwnd, title, length + 1)
    return title_folders(title.value)


def session_key(session):
    return f"{session['pid']}:{session.get('procStartFt')}"


def project_states(sessions, seen, focused):
    """State per project folder (case-insensitive key). `seen` maps session -> last look (ms).

    `focused` holds the lowercase folder names of the focused editor window.
    """
    now = int(time.time() * 1000)
    states = {}
    for s in sessions:
        key, status = session_key(s), s.get('status')
        changed = s.get('statusUpdatedAt') or 0
        if key not in seen or (changed > seen[key] and folder_name(s['cwd']).lower() in focused):
            seen[key] = max(now, changed)  # first time we see it, or you are looking at its window
        if status in ('idle', 'shell'):  # 'shell': idle, with a shell command still running
            state = 'new' if changed > seen[key] else 'done'
        else:
            state = {'busy': 'working', 'waiting': 'waiting'}.get(status, 'unknown')
        folder = ntpath.normcase(s['cwd'])
        if folder not in states or URGENCY.index(state) < URGENCY.index(states[folder]):
            states[folder] = state
    for key in set(seen) - {session_key(s) for s in sessions}:
        del seen[key]
    return states


def project_order(sessions):
    """Folder keys in display order. New folders are appended to the order file, so dots keep their place."""
    try:
        with open(ORDER_FILE, encoding='utf-8') as f:
            order = [line.strip() for line in f if line.strip() and not line.lstrip().startswith('#')]
        first_run = False
    except FileNotFoundError:
        order, first_run = [], True
    known = {ntpath.normcase(cwd) for cwd in order}
    started = {}  # new folder -> (earliest session start, path as Claude Code wrote it)
    for s in sessions:
        folder, start = ntpath.normcase(s['cwd']), s.get('startedAt') or 0
        if folder not in known and (folder not in started or start < started[folder][0]):
            started[folder] = (start, s['cwd'])
    new = [cwd for _, cwd in sorted(started.values())]
    if new or first_run:
        os.makedirs(DATA, exist_ok=True)
        with open(ORDER_FILE, 'a', encoding='utf-8') as f:
            f.write((ORDER_HEADER if first_run else '') + ''.join(f'{cwd}\n' for cwd in new))
    return [ntpath.normcase(cwd) for cwd in order + new]


def dots_text(sessions, order, states):
    if sessions is None:
        return '?'  # no registry where it was expected
    return SEPARATOR.join(GLYPHS[states[key]] for key in order if key in states) or '·'


def legend_rows(sessions, order, states):
    """(glyph, folder name, meaning) for each dot, left to right."""
    names = {ntpath.normcase(s['cwd']): folder_name(s['cwd']) for s in sessions}
    return [(GLYPHS[states[key]], names[key], LABELS[states[key]]) for key in order if key in states]


def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_seen():
    seen = load_json(SEEN_FILE, {})
    return seen if isinstance(seen, dict) else {}


def write_json(path, value):
    os.makedirs(DATA, exist_ok=True)
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(value, f)
    os.replace(tmp, path)


def log_error():
    try:
        if os.path.getsize(LOG_FILE) > 64_000:
            os.remove(LOG_FILE)
    except OSError:
        pass
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + traceback.format_exc())


def load_place():
    """Saved right edge and top of the strip, if that spot is still on a screen."""
    try:
        right, top = (int(v) for v in load_json(PLACE_FILE, None))
    except (TypeError, ValueError):
        return None
    api = win32()
    x, y = api.GetSystemMetrics(SM_XVIRTUALSCREEN), api.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w, h = api.GetSystemMetrics(SM_CXVIRTUALSCREEN), api.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return (right, top) if x < right <= x + w and y <= top < y + h else None


def default_place(height):
    """Right edge and top for the first run: on the taskbar, just left of the notification area."""
    api = win32()
    bar, tray = wintypes.RECT(), wintypes.RECT()
    taskbar = api.FindWindowW('Shell_TrayWnd', None)
    if (taskbar and api.GetWindowRect(taskbar, ctypes.byref(bar))
            and bar.right - bar.left > bar.bottom - bar.top):
        top = bar.top + (bar.bottom - bar.top - height) // 2
        notify = api.FindWindowExW(taskbar, None, 'TrayNotifyWnd', None)
        if (notify and api.GetWindowRect(notify, ctypes.byref(tray))
                and bar.left + 200 < tray.left < bar.right):
            return tray.left - 12, top
        return bar.right - 320, top
    api.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(bar), 0)  # vertical or hidden taskbar
    return bar.right - 12, bar.bottom - height - 12


def screen_color(x, y):
    """Colour of the screen pixel at (x, y) as #rrggbb, or None."""
    api = win32()
    dc = api.GetDC(None)
    try:
        c = api.GetPixel(dc, x, y)
    finally:
        api.ReleaseDC(None, dc)
    return None if c == CLR_INVALID else f'#{c & 0xff:02x}{c >> 8 & 0xff:02x}{c >> 16 & 0xff:02x}'


def keep_on_top(root):
    """The taskbar raises itself above other topmost windows when clicked: take the top back."""
    win32().SetWindowPos(int(root.wm_frame(), 16), wintypes.HWND(-1), 0, 0, 0, 0,  # HWND_TOPMOST
                         SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)


def dpi_aware():
    """Physical pixels and crisp text on scaled screens (Tk is blurry otherwise)."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def single_instance():
    """Hold the named mutex for the life of the process; False if another strip already does."""
    global _mutex
    _mutex = win32().CreateMutexW(None, False, MUTEX)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def is_running():
    handle = win32().OpenMutexW(SYNCHRONIZE, False, MUTEX)
    if handle:
        win32().CloseHandle(handle)
    return bool(handle)


def stop_running(timeout=5.0):
    """Ask a running strip to quit (it checks every second). True once none is running."""
    if not is_running():
        return True
    os.makedirs(DATA, exist_ok=True)
    open(QUIT_FILE, 'w').close()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running():
            return True
        time.sleep(0.2)
    return False


class Strip:
    """The always-on-top strip: placement, dragging, right-click legend and the refresh every second."""

    def __init__(self, tk):
        self.tk = tk
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes('-topmost', True)
        self.label = tk.Label(self.root, text='·', font=FONT, padx=5, pady=1, bd=0)
        self.label.pack()
        self.label.bind('<ButtonPress-1>', self.press)
        self.label.bind('<B1-Motion>', self.drag)
        self.label.bind('<ButtonRelease-1>', self.release)
        self.label.bind('<Button-3>', self.menu)
        self.seen = load_seen()
        self.saved_seen = dict(self.seen)
        self.rows = []
        self.anchor = None  # right edge and top: the strip grows to the left
        self.grab = None
        self.ticks = 0
        self.place()
        self.tick()

    def place(self):
        self.root.update_idletasks()
        self.w, self.h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        if self.anchor is None:
            self.anchor = load_place() or default_place(self.h)
        self.x, self.y = self.anchor[0] - self.w, self.anchor[1]
        self.root.geometry(f'+{self.x}+{self.y}')

    def press(self, event):
        self.grab = (event.x_root - self.x, event.y_root - self.y)

    def drag(self, event):
        if self.grab:
            self.x, self.y = event.x_root - self.grab[0], event.y_root - self.grab[1]
            self.root.geometry(f'+{self.x}+{self.y}')

    def release(self, _event):
        if self.grab:
            self.grab = None
            self.anchor = (self.x + self.w, self.y)
            write_json(PLACE_FILE, list(self.anchor))
            self.recolor()

    def menu(self, event):
        menu = self.tk.Menu(self.root, tearoff=0)
        for glyph, name, label in self.rows:
            menu.add_command(label=f'{glyph}   {name}   ({label})')
        if self.rows:
            menu.add_separator()
        menu.add_command(label='Quit', command=self.root.destroy)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def recolor(self):
        """Blend in: take the colour of the screen just beside the strip, and a readable ink."""
        beside = self.x - 3 if self.x >= 3 else self.x + self.w + 2
        bg = screen_color(beside, self.y + self.h // 2) or '#202020'
        r, g, b = (int(bg[i:i + 2], 16) for i in (1, 3, 5))
        ink = '#f3f3f3' if 0.299 * r + 0.587 * g + 0.114 * b < 140 else '#1b1b1b'
        self.root.configure(bg=bg)
        self.label.configure(bg=bg, fg=ink)

    def tick(self):
        try:
            if os.path.exists(QUIT_FILE):
                os.remove(QUIT_FILE)
                self.root.destroy()
                return
            sessions = live_sessions()
            focused = {name.lower() for name in focused_folders()}
            states = project_states(sessions or [], self.seen, focused)
            order = project_order(sessions or [])
            self.rows = legend_rows(sessions or [], order, states)
            text = dots_text(sessions, order, states)
            if text != self.label.cget('text'):
                self.label.configure(text=text)
                if not self.grab:
                    self.place()
            if self.seen != self.saved_seen:
                write_json(SEEN_FILE, self.seen)
                self.saved_seen = dict(self.seen)
            if self.ticks % 30 == 0:
                self.recolor()
            keep_on_top(self.root)
        except Exception:
            log_error()
        self.ticks += 1
        self.root.after(1000, self.tick)


def widget():
    if not single_instance():
        return  # already running
    os.makedirs(DATA, exist_ok=True)
    if os.path.exists(QUIT_FILE):
        os.remove(QUIT_FILE)  # left over from an earlier stop request
    dpi_aware()
    import tkinter
    Strip(tkinter).root.mainloop()


def install():
    import winreg
    script = os.path.abspath(__file__)
    pythonw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, f'"{pythonw}" "{script}"')
    stop_running()  # an older copy, so that this one takes over
    subprocess.Popen([pythonw, script], close_fds=True,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    print(f'Installed: the dots start now and at every login, from {script}')
    print('Drag them anywhere; right-click them for the legend or to quit.')


def uninstall():
    import shutil
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        pass
    if not stop_running():
        sys.exit('The dots are still running: right-click them, choose Quit, then run this again.')
    shutil.rmtree(DATA, ignore_errors=True)
    print('Removed: the dots, their login entry and their data. This folder can go too.')


def legend():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    sessions = live_sessions()
    if sessions is None:
        sys.exit(f'No Claude Code session registry found in {REGISTRY}')
    states = project_states(sessions, load_seen(), set())
    rows = legend_rows(sessions, project_order(sessions), states)
    for i, (glyph, name, label) in enumerate(rows, 1):
        print(f'{i}  {glyph}  {name:<36}{label}')
    print('\n' + '   '.join(f'{GLYPHS[s]} {LABELS[s]}' for s in GLYPHS))


def main():
    if sys.platform != 'win32':
        sys.exit('This is the Windows version. On Linux, use linux/install.sh.')
    commands = {'--install': install, '--uninstall': uninstall, '--legend': legend}
    args = sys.argv[1:]
    if not args:
        widget()
    elif len(args) == 1 and args[0] in commands:
        commands[args[0]]()
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
