# -*- coding: utf-8 -*-
"""Console UI: timestamped logging and the live multi-thread progress board."""

import os
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

    COL_FMT = '%-7s %-9s %-28s %-22s remaining:%-9d deleted:%-9d initial:%-8d'

    def __init__(self, n_threads, quiet, progress=None, t0=None):
        self.n = n_threads
        self.progress = progress
        self.t0 = t0 if t0 is not None else time.time()
        self.state = [dict(status='idle', tag='-', op='-', initial=0, remaining=0)
                      for _ in range(n_threads)]
        self.rows = [self._fmt_row(t, **self.state[t]) for t in range(n_threads)]
        self.lock = threading.Lock()
        self.lines_on_screen = 0
        self.active = (not quiet) and bool(getattr(sys.stdout, 'isatty', lambda: False)())
        if self.active and os.name == 'nt':
            self.active = self._enable_ansi()
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
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            k.GetConsoleMode(h, ctypes.byref(mode))
            k.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            return True
        except Exception:
            return False

    def _fmt_row(self, tid, status, tag, op, initial, remaining):
        removed = max(0, initial - remaining)
        return self.COL_FMT % ('[T%d]' % tid, status, tag or '-', op or '-',
                               remaining, removed, initial)

    @staticmethod
    def _fmt_elapsed(seconds):
        s = max(0, int(seconds))
        return '%02d:%02d:%02d' % (s // 3600, (s % 3600) // 60, s % 60)

    def set(self, tid, status, tag='', op='', initial=0, remaining=0):
        with self.lock:
            self.state[tid] = dict(status=status, tag=tag, op=op,
                                   initial=initial, remaining=remaining)
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

    def _render(self):
        out = []
        if self.lines_on_screen:
            out.append('\x1b[%dA' % self.lines_on_screen)
        for row in self.rows:
            out.append('\x1b[2K' + row + '\n')
        out.append('\x1b[2K' + self._summary() + '\n')
        sys.stdout.write(''.join(out))
        sys.stdout.flush()
        self.lines_on_screen = self.n + 1

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

    def finish(self):
        self._stop_ticker_thread()
        if not self.active:
            return
        with self.lock:
            if self.lines_on_screen:
                self._clear_above(self.lines_on_screen)
            self.lines_on_screen = 0
            self.active = False


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
