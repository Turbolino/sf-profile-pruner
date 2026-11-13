# -*- coding: utf-8 -*-
"""Console UI: timestamped logging and the live multi-thread progress board."""

import os
import shutil
import sys
import threading
import time
from datetime import datetime


def timestamp():
    return datetime.now().strftime('%H:%M:%S')


class Logger:
    def __init__(self, quiet, log_path=None):
        self.quiet = quiet
        self.console = not quiet
        self.log_path = log_path
        self._lock = threading.Lock()

    def log(self, msg):
        line = '[%s] %s' % (timestamp(), msg)
        with self._lock:
            if self.console and not self.quiet:
                print(line, flush=True)
            if self.log_path:
                with open(self.log_path, 'a', encoding='utf-8') as f:
                    f.write(line + '\n')


class StatusBoard:
    """One live-updating line per worker thread on the console, plus a summary
    line with the elapsed time and the tag counters (done / running / todo).

    Renders N thread lines + 1 summary line and redraws them in place on every
    update. Falls back to no-op when stdout is not a TTY (e.g. piped) or when
    --quiet is set.

    Stop lifecycle:
      - freeze(): stop the live redraw, leaving the current frame on screen
        (the rows stay visible instead of being wiped).
      - finalize_stopped(): mark non-idle rows "stopped by user" and redraw the
        frame in place, then leave it on screen.
    """

    COL_FMT = '%-7s %-9s %-24s %-18s deleted:%-7d remaining:%-7d initial:%-7d'

    def __init__(self, n_threads, quiet, progress=None, t0=None):
        self.n = n_threads
        self.progress = progress
        self.t0 = t0 if t0 is not None else time.time()
        self.state = [dict(status='idle', tag='-', op='-', initial=0, deleted=0)
                      for _ in range(n_threads)]
        self.rows = [self._fmt_row(t, **self.state[t]) for t in range(n_threads)]
        self.lock = threading.Lock()
        self.lines_on_screen = 0
        self.active = (not quiet) and bool(getattr(sys.stdout, 'isatty', lambda: False)())
        self.width = 0
        self.height = 0
        if self.active:
            if os.name == 'nt':
                self.active = self._enable_ansi()
            if self.active:
                self.width, self.height = self._terminal_size()
        self._was_active = self.active
        # A background ticker re-renders the board once a second so the
        # `elapsed` clock keeps moving even when no worker posts an update.
        self._stop_ticker = threading.Event()
        self._ticker = None
        if self.active:
            self._start_ticker()

    def _start_ticker(self):
        if self._ticker is not None:
            return
        self._ticker = threading.Thread(target=self._ticker_loop, daemon=True)
        self._ticker.start()

    def _stop_ticker_thread(self):
        self._stop_ticker.set()

    def _ticker_loop(self):
        while not self._stop_ticker.wait(1.0):
            self.tick()

    def tick(self):
        """Re-render the board (clock keeps ticking between worker updates)."""
        with self.lock:
            if self.active and self.lines_on_screen:
                self._render()

    @staticmethod
    def _enable_ansi():
        """Enable VT processing on the console so ANSI cursor/erase codes
        actually move the cursor. Uses the real stdout handle (robust when
        stdout was attached to a console buffer) and falls back to
        STD_OUTPUT_HANDLE. Returns False when VT cannot be enabled."""
        try:
            import ctypes
            import msvcrt
            k = ctypes.windll.kernel32
            handle = None
            try:
                fd = sys.stdout.fileno()
                if fd is not None and fd >= 0:
                    handle = msvcrt.get_osfhandle(fd)
            except Exception:
                handle = None
            if not handle:
                handle = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            if not k.GetConsoleMode(handle, ctypes.byref(mode)):
                return False
            return bool(k.SetConsoleMode(handle, mode.value | 0x0004))
        except Exception:
            return False

    @staticmethod
    def _terminal_size():
        """Current console size as (columns, lines); (0, 0) when unknown."""
        try:
            size = shutil.get_terminal_size((120, 24))
            return size.columns, size.lines
        except Exception:
            return 0, 0

    def _clip(self, text):
        """Trim `text` so it never exceeds the console width (prevents the
        line from wrapping, which would break the in-place cursor accounting)."""
        if self.width and len(text) >= self.width:
            return text[:self.width - 1]
        return text

    def _fmt_row(self, tid, status, tag, op, initial, deleted):
        # `deleted` = entries discarded so far; `remaining` = entries still to
        # process = initial - deleted. There is no per-entry "processed" counter:
        # each dry-run tests the whole remaining chunk at once, so the only real
        # monotonic quantities are discarded / remaining / initial.
        remaining = max(0, initial - deleted)
        return self.COL_FMT % ('[T%d]' % tid, status, tag or '-', op or '-',
                               deleted, remaining, initial)

    @staticmethod
    def _fmt_elapsed(seconds):
        s = max(0, int(seconds))
        return '%02d:%02d:%02d' % (s // 3600, (s % 3600) // 60, s % 60)

    def set(self, tid, status, tag='', op='', initial=0, deleted=0):
        with self.lock:
            self.state[tid] = dict(status=status, tag=tag, op=op,
                                   initial=initial, deleted=deleted)
            self.rows[tid] = self._fmt_row(tid, **self.state[tid])
            if self.active:
                self._render()

    def _summary(self):
        elapsed = self._fmt_elapsed(time.time() - self.t0)
        if self.progress is None:
            return 'elapsed %s' % elapsed
        done, running, todo, _failed = self.progress.snapshot()
        return 'elapsed %s | done: %d | running: %d | todo: %d' % (elapsed, done, running, todo)

    def _clear_above(self, n):
        if n <= 0:
            return
        out = ['\x1b[%dA' % n]
        for _ in range(n):
            out.append('\x1b[2K\n')
        sys.stdout.write(''.join(out))
        sys.stdout.flush()

    def _visible_rows(self):
        """Number of worker rows to render so the frame never touches the
        bottom of the terminal.

        The frame is N worker rows + 1 summary line, and every line ends with
        '\n', so after a redraw the cursor sits one line below the summary. We
        keep TWO blank lines below that cursor: the last terminal row is where
        the shell prompt sits in many setups, and the line above it is the
        scroll margin — writing into either of them makes the terminal scroll on
        the next newline and the board reprints instead of redrawing in place.
        Total: N rows + 1 summary + 1 trailing cursor + 2 blank margin
        <= height, i.e. rows <= height - 4."""
        if self.height and self.height > 1:
            return max(1, self.height - 4)
        return self.n

    def _render(self):
        # Refresh the window size on every render so a terminal resize mid-run
        # is picked up instead of using a stale height/width captured at startup.
        try:
            self.width, self.height = self._terminal_size()
        except Exception:
            pass
        visible = self._visible_rows()
        rows = self.rows if self.n <= visible else self.rows[:visible]
        out = []
        if self.lines_on_screen:
            out.append('\x1b[%dA' % self.lines_on_screen)
        else:
            # First draw: clear the screen and home the cursor so the frame
            # starts at the top. Otherwise the banner and startup logs above it
            # push the frame past the bottom edge and clip the last rows.
            out.append('\x1b[2J\x1b[H')
        for row in rows:
            out.append('\x1b[2K' + self._clip(row) + '\n')
        out.append('\x1b[2K' + self._clip(self._summary()) + '\n')
        sys.stdout.write(''.join(out))
        sys.stdout.flush()
        self.lines_on_screen = len(rows) + 1

    def freeze(self):
        """Stop live redraw; the last rendered frame stays on screen."""
        with self.lock:
            self._stop_ticker_thread()
            self.active = False

    def finalize_stopped(self):
        """Mark non-idle rows 'stopped by user' and redraw them in place."""
        with self.lock:
            self._stop_ticker_thread()
            self._mark_stopped_locked()
            if self._was_active and self.lines_on_screen:
                self._render()
            self.active = False
            self.lines_on_screen = 0

    def _mark_stopped_locked(self):
        for tid, s in enumerate(self.state):
            if s['status'] not in ('idle', 'stopped'):
                s['status'] = 'stopped'
                s['op'] = 'stopped by user'
                self.rows[tid] = self._fmt_row(tid, **s)

    def mark_stopped(self):
        with self.lock:
            self._mark_stopped_locked()

    def _mark_completed_locked(self):
        for tid, s in enumerate(self.state):
            if s['status'] in ('idle', 'running', 'start'):
                s['status'] = 'completed'
                s['op'] = 'no more work'
                self.rows[tid] = self._fmt_row(tid, **s)

    def finalize_finished(self):
        """Leave the final frame on screen after a successful run.

        Stops the elapsed ticker, marks any still-idle/running rows as
        'completed', renders one last frame and keeps it visible (rows +
        summary with the frozen elapsed clock) instead of wiping it."""
        with self.lock:
            self._stop_ticker_thread()
            self._mark_completed_locked()
            if self._was_active and self.lines_on_screen:
                self._render()
            self.active = False
            self.lines_on_screen = 0

    def finish(self):
        self._stop_ticker_thread()
        if not self.active:
            return
        with self.lock:
            if self.lines_on_screen:
                self._clear_above(self.lines_on_screen)
            self.lines_on_screen = 0
            self.active = False


class PrefetchStatus:
    """Single in-place status line for the metadata pre-fetch phase.

    Shows ``message | elapsed HH:MM:SS`` on one line that updates in place,
    without colliding with the later worker board (it stops and newlines before
    the board takes over). No-op when stdout is not a TTY or --quiet is set, so
    the per-type messages still reach stdout through the regular logger."""

    def __init__(self, quiet, t0=None):
        self.message = ''
        self.t0 = t0 if t0 is not None else time.time()
        self.active = (not quiet) and bool(getattr(sys.stdout, 'isatty', lambda: False)())
        self.width = 0
        if self.active:
            if os.name == 'nt':
                self.active = StatusBoard._enable_ansi()
            if self.active:
                self.width, _ = StatusBoard._terminal_size()
        self._stop = threading.Event()
        self._thread = None

    def _line(self):
        m = self.message or 'preparing'
        return '%s | elapsed %s' % (m, StatusBoard._fmt_elapsed(time.time() - self.t0))

    def _draw(self):
        text = self._line()
        if self.width:
            text = text[:self.width - 1]
        sys.stdout.write('\x1b[2K\r' + text)
        sys.stdout.flush()

    def update(self, message):
        self.message = message or ''
        if self.active:
            self._draw()

    def _loop(self):
        while not self._stop.wait(1.0):
            self._draw()

    def start(self):
        if not self.active:
            return
        self._draw()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self.active:
            self._draw()
            sys.stdout.write('\n')
            sys.stdout.flush()


class Control:
    """Stop signal shared between the main thread and the workers."""

    def __init__(self):
        self.stop = threading.Event()


class Progress:
    """Thread-safe counters for the tag-level progress (done / running / todo)."""

    def __init__(self, total):
        self.lock = threading.Lock()
        self.total = total
        self.done = 0
        self.failed = 0
        self.running = 0

    def start(self):
        with self.lock:
            self.running += 1

    def finish(self, ok):
        with self.lock:
            self.running -= 1
            if ok:
                self.done += 1
            else:
                self.failed += 1

    def snapshot(self):
        with self.lock:
            todo = self.total - self.done - self.failed - self.running
            return self.done, self.running, todo, self.failed
