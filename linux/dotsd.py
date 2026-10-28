#!/usr/bin/python3
"""dotsd: a glyph-only summary of the local Claude Code sessions, for the MATE panel.

Reads Claude Code's own session registry (~/.claude/sessions/<pid>.json) and never writes
to it. Only the *.json entries are opened: the private *.key files, transcripts and sockets
are left alone, and nothing is ever sent to a session.

One glyph per project folder, left to right in the order of the `order` file:
    ◇  waiting for you (permission prompt or question)
    ◆  finished, and you haven't focused that project's editor window since
    ◦  working
    ?  unknown status (registry format changed by a Claude Code update?)
    •  finished, already seen

The line goes to $XDG_RUNTIME_DIR/dots/line in the [Command] format of the MATE "Command"
applet, which runs the `dots` script next to this file every second.

    dotsd.py            run the watcher (normally started by `dots`)
    dotsd.py --legend   print which glyph is which project right now
"""

import ctypes
import fcntl
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
import traceback

HERE = os.path.dirname(os.path.realpath(__file__))
ORDER_FILE = os.path.join(HERE, 'order')
REGISTRY = os.path.expanduser('~/.claude/sessions')
RUN = os.path.join(os.environ.get('XDG_RUNTIME_DIR') or '/tmp', 'dots')
LINE_FILE = os.path.join(RUN, 'line')
SEEN_FILE = os.path.join(RUN, 'seen.json')
LOG_FILE = os.path.join(RUN, 'dotsd.log')

# Most urgent first: a folder with several sessions shows the most urgent state.
GLYPHS = {'waiting': '◇', 'new': '◆', 'working': '◦', 'unknown': '?', 'done': '•'}
LABELS = {'waiting': 'waiting for you', 'new': 'finished, not seen yet', 'working': 'working',
          'unknown': 'unknown status', 'done': 'finished, seen'}
URGENCY = list(GLYPHS)
SEPARATOR = '\u2009'  # thin space
EDITOR_CLASS = re.compile(r'code|codium|cursor|windsurf', re.I)  # VS Code and its forks
INFRASTRUCTURE = ('daemon', 'daemon-worker')  # background session kinds that aren't conversations
ORDER_HEADER = ('# Left-to-right order of the dots: one project folder per line.\n'
                '# Reorder freely; new projects are appended automatically.\n')

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
                with open(path) as f:
                    fresh[path] = (mtime, json.load(f))
        except (OSError, ValueError):
            pass  # removed or half-written: picked up on the next tick
    _registry_cache.clear()
    _registry_cache.update(fresh)
    return [entry for _, entry in fresh.values() if isinstance(entry, dict)]


def read_proc(pid, name):
    try:
        with open(f'/proc/{pid}/{name}') as f:
            return f.read()
    except OSError:
        return None


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
        stat = read_proc(pid, 'stat')
        if stat is None:
            continue  # closed session: its entry stays behind
        # The pid may now belong to another program: compare start times, as Claude Code does.
        started = stat.rsplit(')', 1)[1].split()[19]
        if 'procStart' in entry:
            if str(entry['procStart']) != started:
                continue
        elif (read_proc(pid, 'comm') or '').strip() != 'claude':
            continue
        live.append(entry)
    return live


def window_folders(wid):
    """Folder names in an editor window title ("<file> - <folder> - Visual Studio Code")."""
    try:
        out = subprocess.run(['xprop', '-id', wid, 'WM_CLASS', '_NET_WM_NAME'], capture_output=True,
                             encoding='utf-8', errors='replace', timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    cls = re.search(r'^WM_CLASS\(STRING\) = (.*)$', out, re.M)
    title = re.search(r'^_NET_WM_NAME\(UTF8_STRING\) = "(.*)"$', out, re.M)
    if not cls or not EDITOR_CLASS.search(cls.group(1)) or not title:
        return ()
    parts = title.group(1).split(' - ')
    return tuple(parts[1:-1] if len(parts) > 2 else parts[:1])


class Focus:
    """Follows the focused X11 window with `xprop -spy`; `folders` names its VS Code folder."""

    def __init__(self):
        self.folders = ()
        self.proc = None
        self.pending = b''
        self.retry_at = 0.0

    def streams(self):
        if self.proc is None and time.monotonic() >= self.retry_at:
            try:
                self.proc = subprocess.Popen(['xprop', '-root', '-spy', '_NET_ACTIVE_WINDOW'],
                                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                os.set_blocking(self.proc.stdout.fileno(), False)
            except OSError:
                self.retry_at = time.monotonic() + 60
        return [self.proc.stdout] if self.proc else []

    def read(self):
        try:
            data = os.read(self.proc.stdout.fileno(), 4096)
        except BlockingIOError:
            return
        if not data:  # xprop exited (display gone?): try again later
            self.proc.wait()
            self.proc, self.pending, self.folders = None, b'', ()
            self.retry_at = time.monotonic() + 30
            return
        *lines, self.pending = (self.pending + data).split(b'\n')
        if lines:
            wid = re.search(rb'0x[0-9a-f]+', lines[-1])
            self.folders = window_folders(wid.group().decode()) if wid else ()

    def stop(self):
        if self.proc:
            self.proc.kill()


def session_key(session):
    return f"{session['pid']}:{session.get('procStart')}"


def project_states(sessions, seen, focused):
    """State per project folder. `seen` maps session -> last time (ms) its result was looked at."""
    now = int(time.time() * 1000)
    states = {}
    for s in sessions:
        key, status = session_key(s), s.get('status')
        changed = s.get('statusUpdatedAt') or 0
        if key not in seen or (changed > seen[key] and os.path.basename(s['cwd']) in focused):
            seen[key] = max(now, changed)  # first time we see it, or you are looking at its window
        if status in ('idle', 'shell'):  # 'shell': idle, with a shell command still running
            state = 'new' if changed > seen[key] else 'done'
        else:
            state = {'busy': 'working', 'waiting': 'waiting'}.get(status, 'unknown')
        current = states.get(s['cwd'])
        if current is None or URGENCY.index(state) < URGENCY.index(current):
            states[s['cwd']] = state
    for key in set(seen) - {session_key(s) for s in sessions}:
        del seen[key]
    return states


def taskbar_order(folders):
    """Sort folders like their VS Code windows (creation order, which the taskbar follows)."""
    try:
        out = subprocess.run(['xprop', '-root', '_NET_CLIENT_LIST'], capture_output=True, text=True,
                             timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return folders
    rank = {}
    for i, wid in enumerate(re.findall(r'0x[0-9a-f]+', out)):
        for name in window_folders(wid):
            rank.setdefault(name, i)
    return sorted(folders, key=lambda cwd: rank.get(os.path.basename(cwd), float('inf')))


def project_order(sessions):
    """Folders in display order. New folders are appended to the order file, so dots keep their place."""
    try:
        with open(ORDER_FILE) as f:
            order = [line.strip() for line in f if line.strip() and not line.lstrip().startswith('#')]
        first_run = False
    except FileNotFoundError:
        order, first_run = [], True
    started = {}
    for s in sessions:
        started[s['cwd']] = min(started.get(s['cwd'], float('inf')), s.get('startedAt') or 0)
    new = sorted(set(started) - set(order), key=started.get)
    if first_run:
        new = taskbar_order(new)
    if new or first_run:
        with open(ORDER_FILE, 'a') as f:
            f.write((ORDER_HEADER if first_run else '') + ''.join(f'{cwd}\n' for cwd in new))
    return order + new


def render(sessions, order, states):
    if sessions is None:
        return '[Command]\nOutput=?\n'
    glyphs = SEPARATOR.join(GLYPHS[states[cwd]] for cwd in order if cwd in states)
    return f'[Command]\nOutput={glyphs or SEPARATOR}\n'


def load_seen():
    try:
        with open(SEEN_FILE) as f:
            seen = json.load(f)
        return seen if isinstance(seen, dict) else {}
    except (OSError, ValueError):
        return {}


def write_atomic(path, text):
    tmp = f'{path}.tmp'
    with open(tmp, 'w') as f:
        f.write(text)
    os.replace(tmp, path)


def log_error():
    try:
        if os.path.getsize(LOG_FILE) > 64_000:
            os.remove(LOG_FILE)
    except OSError:
        pass
    with open(LOG_FILE, 'a') as f:
        f.write(time.strftime('%F %T ') + traceback.format_exc())


def applet_running():
    """True while a MATE Command applet process exists (the one that runs `dots`)."""
    return any(pid.isdigit() and (read_proc(pid, 'comm') or '').strip() == 'command-applet'
               for pid in os.listdir('/proc'))


def main():
    os.makedirs(RUN, exist_ok=True)
    lock = open(os.path.join(RUN, 'lock'), 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # another dotsd is running
    try:
        ctypes.CDLL(None).prctl(15, b'dotsd', 0, 0, 0)  # PR_SET_NAME: how `dots` recognises us
    except (OSError, AttributeError):
        pass
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    write_atomic(os.path.join(RUN, 'pid'), f'{os.getpid()}\n')
    seen, saved_seen, line = load_seen(), None, None
    focus = Focus()
    applet_last_seen = next_applet_check = time.monotonic()
    try:
        while True:
            streams = focus.streams()
            if select.select(streams, [], [], 1.0)[0]:
                focus.read()
            try:
                sessions = live_sessions()
                states = project_states(sessions or [], seen, focus.folders)
                text = render(sessions, project_order(sessions or []), states)
                if text != line:
                    write_atomic(LINE_FILE, text)
                    line = text
                if seen != saved_seen:
                    write_atomic(SEEN_FILE, json.dumps(seen))
                    saved_seen = dict(seen)
            except Exception:
                log_error()
            now = time.monotonic()
            if now >= next_applet_check:
                next_applet_check = now + 60
                if applet_running():
                    applet_last_seen = now
                elif now - applet_last_seen > 150:
                    return  # applet removed from the panel, or session over: stop as well
    finally:
        focus.stop()


def legend():
    sessions = live_sessions()
    if sessions is None:
        sys.exit('No Claude Code session registry found.')
    states = project_states(sessions, load_seen(), ())
    shown = [cwd for cwd in project_order(sessions) if cwd in states]
    for i, cwd in enumerate(shown, 1):
        print(f'{i}  {GLYPHS[states[cwd]]}  {os.path.basename(cwd):<36}{LABELS[states[cwd]]}')
    print('\n' + '   '.join(f'{GLYPHS[s]} {LABELS[s]}' for s in GLYPHS))


if __name__ == '__main__':
    legend() if sys.argv[1:] == ['--legend'] else main()
