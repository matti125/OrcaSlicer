#!/usr/bin/env python3
"""Manual verification of "Reload objects when their source file changes".

Drives the on-disk side of the feature (in-place overwrite, rename-into-place) against a
running OrcaSlicer and checks the outcome in OrcaSlicer's own log. The GUI steps that can't
be scripted (importing the model, toggling the two preferences) are prompted for.

    python3 scripts/test_auto_reload.py [--data-dir DIR] [--timeout SECONDS]

Requires OrcaSlicer's log severity at the default "info" level (Preferences > Log level).
"""

import argparse
import glob
import json
import os
import platform
import sys
import time

RELOAD_MARK      = "source file(s) changed on disk, reloading"
SLICE_START_MARK = "will start print::process"
SLICE_DONE_MARK  = "on_process_completed:finished"

PREF_RELOAD = "auto_reload_on_source_change"
PREF_SLICE  = "auto_slice_after_reload"
PREF_RELOAD_LABEL = 'Reload objects when their source file changes'
PREF_SLICE_LABEL  = 'Also slice after auto-reloading a model'


def default_data_dir():
    system = platform.system()
    if system == "Darwin":
        return os.path.expanduser("~/Library/Application Support/OrcaSlicer")
    if system == "Windows":
        return os.path.join(os.environ.get("APPDATA", ""), "OrcaSlicer")
    for candidate in ("~/.config/OrcaSlicer",
                      "~/.var/app/io.github.softfever.OrcaSlicer/config/OrcaSlicer"):
        if os.path.isdir(os.path.expanduser(candidate)):
            return os.path.expanduser(candidate)
    return os.path.expanduser("~/.config/OrcaSlicer")


def newest_log(log_dir):
    logs = [p for p in glob.glob(os.path.join(log_dir, "debug_*.log*")) if not p.endswith(".enc")]
    return max(logs, key=os.path.getmtime) if logs else None


def read_prefs(data_dir):
    try:
        with open(os.path.join(data_dir, "OrcaSlicer.conf"), encoding="utf-8") as f:
            app = json.load(f).get("app", {})
    except (OSError, ValueError):
        return None
    def truthy(v):
        return v is True or str(v).lower() in ("1", "true")
    return {PREF_RELOAD: truthy(app.get(PREF_RELOAD)), PREF_SLICE: truthy(app.get(PREF_SLICE))}


class LogTail:
    """Follows a log file from the moment it's opened; each test starts from a fresh mark."""

    def __init__(self, path):
        self.path = path
        self.pos = os.path.getsize(path)
        self.buf = ""

    def mark(self):
        self._read()
        self.buf = ""

    def _read(self):
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(self.pos)
            chunk = f.read()
            self.pos = f.tell()
        self.buf += chunk

    def wait_for(self, marker, timeout):
        deadline = time.monotonic() + timeout
        while True:
            self._read()
            if marker in self.buf:
                return True
            if time.monotonic() > deadline:
                return False
            time.sleep(0.25)

    def absent_after(self, marker, wait):
        time.sleep(wait)
        self._read()
        return marker not in self.buf


BOX_FACES = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]


def write_stl(path, boxes, atomic=False):
    """Writes an ASCII STL of axis-aligned boxes given as (x0, y0, z0, x1, y1, z1)."""
    lines = ["solid test"]
    for x0, y0, z0, x1, y1, z1 in boxes:
        v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
             (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
        for a, b, c in BOX_FACES:
            lines.append("  facet normal 0 0 0\n    outer loop")
            for i in (a, b, c):
                lines.append("      vertex %g %g %g" % v[i])
            lines.append("    endloop\n  endfacet")
    lines.append("endsolid test\n")
    data = "\n".join(lines)
    if atomic:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(data)
        os.replace(tmp, path)
    else:
        with open(path, "w") as f:
            f.write(data)


def write_cube_stl(path, size, atomic=False):
    s = float(size)
    write_stl(path, [(0, 0, 0, s, s, s)], atomic)


def write_pillars_stl(path, height, n=16, pitch=6.0, width=4.0):
    """An n x n grid of thin pillars: hundreds of islands per layer, so it slices slowly."""
    boxes = [(i * pitch, j * pitch, 0, i * pitch + width, j * pitch + width, float(height))
             for i in range(n) for j in range(n)]
    write_stl(path, boxes)


def ask(prompt):
    while True:
        answer = input(prompt + " [y/n] ").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def pause(text):
    print()
    print(text)
    input("Press Enter when done... ")


def check_prefs(data_dir, want_reload, want_slice):
    prefs = read_prefs(data_dir)
    if prefs is None:
        print("  (could not read OrcaSlicer.conf to cross-check the preferences)")
        return
    mismatches = []
    if prefs[PREF_RELOAD] != want_reload:
        mismatches.append("%s = %s" % (PREF_RELOAD_LABEL, prefs[PREF_RELOAD]))
    if prefs[PREF_SLICE] != want_slice:
        mismatches.append("%s = %s" % (PREF_SLICE_LABEL, prefs[PREF_SLICE]))
    if mismatches:
        print("  NOTE: OrcaSlicer.conf currently reads: " + "; ".join(mismatches))
        print("        (the file can lag behind a just-toggled checkbox; the tests below are what count)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default=default_data_dir(), help="OrcaSlicer data directory")
    parser.add_argument("--timeout", type=float, default=20.0, help="seconds to wait for a reload/slice (default 20)")
    parser.add_argument("--quiet-window", type=float, default=8.0,
                        help="seconds to wait when asserting that nothing happens (default 8)")
    parser.add_argument("--slow-height", type=float, default=60.0,
                        help="pillar height in mm for the mid-slice test; raise it if the slice finishes "
                             "before the second change lands (default 60)")
    parser.add_argument("--mid-slice-delay", type=float, default=3.0,
                        help="seconds into the slow slice at which the second change is written (default 3)")
    # Not a system temp dir: macOS file dialogs hide /var, where those live.
    parser.add_argument("--work-dir", default=os.path.expanduser("~/orca_autoreload_test"),
                        help="where to put the test model (default ~/orca_autoreload_test)")
    args = parser.parse_args()

    log_dir = os.path.join(args.data_dir, "log")
    if not os.path.isdir(log_dir):
        sys.exit("No log directory at %s -- pass --data-dir if OrcaSlicer stores its data elsewhere." % log_dir)

    work_dir = os.path.abspath(args.work_dir)
    os.makedirs(work_dir, exist_ok=True)
    stl = os.path.join(work_dir, "cube.stl")
    write_cube_stl(stl, 20)
    results = []

    def record(name, ok, detail=""):
        results.append((name, ok, detail))
        print("  %s  %s%s" % ("PASS" if ok else "FAIL", name, (" -- " + detail) if detail else ""))

    print("Test model: %s (20 mm cube)" % stl)
    pause("1. Start the OrcaSlicer build under test (a fresh, empty project).\n"
          "2. Preferences: ENABLE  '%s'\n"
          "                DISABLE '%s'\n"
          "3. Import the test model above (File > Import, or drag it onto the plate)." % (PREF_RELOAD_LABEL, PREF_SLICE_LABEL))

    log_path = newest_log(log_dir)
    if not log_path:
        sys.exit("No debug_*.log found under %s -- is OrcaSlicer running?" % log_dir)
    print("Following log: %s" % log_path)
    tail = LogTail(log_path)
    check_prefs(args.data_dir, want_reload=True, want_slice=False)

    # --- reload on, slice off -------------------------------------------------------------
    print("\n[A] In-place overwrite (20 -> 30 mm)")
    tail.mark(); time.sleep(1.5)
    write_cube_stl(stl, 30)
    ok = tail.wait_for(RELOAD_MARK, args.timeout)
    record("A1 reload after in-place overwrite", ok, "" if ok else "no reload line in log within %gs" % args.timeout)
    if ok:
        record("A2 no slice when '%s' is off" % PREF_SLICE_LABEL, tail.absent_after(SLICE_START_MARK, 4))
        record("A3 model visibly updated", ask("  Did the cube grow to 30 mm?"))

    print("\n[B] Rename-into-place, the temp-file-then-rename pattern most exporters use (30 -> 40 mm)")
    tail.mark(); time.sleep(1.5)
    write_cube_stl(stl, 40, atomic=True)
    ok = tail.wait_for(RELOAD_MARK, args.timeout)
    record("B1 reload after rename-into-place", ok, "" if ok else "no reload line in log within %gs" % args.timeout)
    if ok:
        record("B2 model visibly updated", ask("  Did the cube grow to 40 mm?"))

    print("\n[C] In-place overwrite again after the rename (40 -> 50 mm) -- checks the watch was re-armed")
    tail.mark(); time.sleep(1.5)
    write_cube_stl(stl, 50)
    ok = tail.wait_for(RELOAD_MARK, args.timeout)
    record("C1 reload after overwrite following a rename", ok, "" if ok else "no reload line in log within %gs" % args.timeout)

    # --- reload on, slice on --------------------------------------------------------------
    pause("Preferences: ENABLE '%s'.\nThen select the Prepare tab (not Preview)." % PREF_SLICE_LABEL)
    check_prefs(args.data_dir, want_reload=True, want_slice=True)
    print("\n[D] In-place overwrite with auto-slice on (50 -> 25 mm)")
    tail.mark(); time.sleep(1.5)
    write_cube_stl(stl, 25)
    ok = tail.wait_for(RELOAD_MARK, args.timeout)
    record("D1 reload", ok, "" if ok else "no reload line in log within %gs" % args.timeout)
    if ok:
        started = tail.wait_for(SLICE_START_MARK, args.timeout)
        record("D2 slice started automatically", started, "" if started else "no slice-start line within %gs" % args.timeout)
        if started:
            done = tail.wait_for(SLICE_DONE_MARK, args.timeout * 3)
            record("D3 slice completed", done, "" if done else "no completion line within %gs" % (args.timeout * 3))
        record("D4 stayed on the current tab", ask("  Is the Prepare tab still selected (no jump to Preview)?"))

    h1, h2 = args.slow_height, args.slow_height / 2
    print("\n[F] Change arriving mid-slice: cube -> %g mm pillar grid, then %g mm while that slices" % (h1, h2))
    tail.mark(); time.sleep(1.5)
    write_pillars_stl(stl, h1)
    ok = tail.wait_for(RELOAD_MARK, args.timeout) and tail.wait_for(SLICE_START_MARK, args.timeout)
    record("F1 reload and slice start for the pillar grid", ok)
    if ok:
        time.sleep(args.mid_slice_delay)
        tail._read()
        still_running = SLICE_DONE_MARK not in tail.buf
        record("F2 first slice still running when the second change is written", still_running,
               "" if still_running else "it already finished; raise --slow-height or lower --mid-slice-delay")
        tail.mark()
        write_pillars_stl(stl, h2)
        ok = tail.wait_for(RELOAD_MARK, args.timeout)
        record("F3 reload while slicing", ok, "" if ok else "no reload line within %gs" % args.timeout)
        if ok:
            restarted = tail.wait_for(SLICE_START_MARK, args.timeout)
            record("F4 slice restarted after the reload", restarted,
                   "" if restarted else "no second slice-start line within %gs" % args.timeout)
            if restarted:
                done = tail.wait_for(SLICE_DONE_MARK, args.timeout * 6)
                record("F5 restarted slice completed", done, "" if done else "no completion line within %gs" % (args.timeout * 6))
            record("F6 final geometry is the second change", ask("  Are the pillars %g mm tall (not %g)?" % (h2, h1)))

    # --- reload off -----------------------------------------------------------------------
    pause("Preferences: DISABLE '%s' (leave the slice option as it is)." % PREF_RELOAD_LABEL)
    check_prefs(args.data_dir, want_reload=False, want_slice=True)
    print("\n[E] In-place overwrite with auto-reload off (pillars -> 35 mm cube)")
    tail.mark(); time.sleep(1.5)
    write_cube_stl(stl, 35)
    quiet = tail.absent_after(RELOAD_MARK, args.quiet_window)
    record("E1 no reload when '%s' is off" % PREF_RELOAD_LABEL, quiet, "" if quiet else "a reload happened anyway")
    if quiet:
        record("E2 model unchanged", ask("  Is the model still the pillar grid (no cube)?"))

    # --- summary --------------------------------------------------------------------------
    failed = [r for r in results if not r[1]]
    print("\n%d checks, %d failed" % (len(results), len(failed)))
    for name, _, detail in failed:
        print("  FAIL %s%s" % (name, (" -- " + detail) if detail else ""))
    if failed:
        print("Test files left in %s; log at %s" % (work_dir, log_path))
        sys.exit(1)
    os.remove(stl)
    try:
        os.rmdir(work_dir)  # only if nothing else is in it
    except OSError:
        pass
    print("All passed. Remember to restore the two preferences to the values you want.")


if __name__ == "__main__":
    main()
