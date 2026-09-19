#!/usr/bin/env python3
"""Manual verification of "Reload objects when their source file changes".

Drives the on-disk side of the feature (in-place overwrite, rename-into-place) against a
running OrcaSlicer and checks the outcome in OrcaSlicer's own log. The GUI steps that can't
be scripted (importing the model, toggling the two preferences) are prompted for.

    python3 scripts/test_auto_reload.py [options]

Run with --help for the full list of options (data dir, timeouts, pillar-grid height for the
mid-slice test, work dir for the generated model).

Requires OrcaSlicer's log severity at the default "info" level (Preferences > Log level).
"""

import argparse
import glob
import json
import os
import platform
import re
import sys
import threading
import time

RELOAD_MARK      = "source file(s) changed on disk, reloading"
SLICE_START_MARK = "will start print::process"
SLICE_DONE_MARK  = "on_process_completed:finished"
MISSING_SOURCE_MARK = "source file missing, skipping reload"
LOAD_FAILED_MARK = "failed to load"
# Logged once per reload_from_disk() call with the number of volumes it's about to reload --
# the targeted-reload path (only the volumes whose source actually changed) should log 1 here
# even when other objects are loaded, not the total volume count on the plate.
RELOADABLE_COUNT_RE = re.compile(r"reloadable volumes number is: (\d+)")

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


_MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
           "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
_LOG_NAME_RE = re.compile(r"debug_\w{3}_(\w{3})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d+)\.log")


def _log_launch_key(path):
    """Sorts by the launch timestamp embedded in OrcaSlicer's own log filename
    (debug_<Weekday>_<Month>_<DD>_<HH>_<MM>_<SS>_<PID>.log.0), not the file's mtime. If more
    than one OrcaSlicer instance is running -- e.g. an old build left open in the background --
    every one of them keeps its own log's mtime fresh via periodic autosave, so "newest mtime"
    is close to a coin flip between them; the launch time in the name is unambiguous.
    Falls back to mtime for a name that doesn't match (sorts before any recognized name)."""
    m = _LOG_NAME_RE.match(os.path.basename(path))
    if not m or m.group(1) not in _MONTHS:
        return (0, os.path.getmtime(path))
    month, day, hh, mm, ss, _pid = m.groups()
    return (1, _MONTHS[month], int(day), int(hh), int(mm), int(ss))


def newest_log(log_dir):
    logs = [p for p in glob.glob(os.path.join(log_dir, "debug_*.log*")) if not p.endswith(".enc")]
    return max(logs, key=_log_launch_key) if logs else None


def read_prefs(data_dir):
    try:
        with open(os.path.join(data_dir, "OrcaSlicer.conf"), encoding="utf-8") as f:
            text = f.read()
        # OrcaSlicer appends a trailing "# MD5 checksum ..." line after the closing brace --
        # not valid JSON, so parse just the leading object and ignore whatever follows it.
        config, _ = json.JSONDecoder().raw_decode(text)
        app = config.get("app", {})
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

    def has_seen(self, marker):
        self._read()
        return marker in self.buf

    def last_match(self, pattern):
        """Returns the last regex match's group(1) seen so far, or None."""
        self._read()
        matches = pattern.findall(self.buf)
        return matches[-1] if matches else None


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


def write_obj_cube(path, size):
    """A minimal ASCII OBJ cube with no material library. read_from_file()'s obj_color_fun
    fires for any .obj regardless of content, which is exactly what phase H checks."""
    s = float(size)
    v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0), (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    lines = ["o test"]
    for x, y, z in v:
        lines.append("v %g %g %g" % (x, y, z))
    for a, b, c in BOX_FACES:
        lines.append("f %d %d %d" % (a + 1, b + 1, c + 1))  # OBJ face indices are 1-based
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def write_truncated_stl(path):
    """A syntactically broken STL: cut off before any facet is complete. admesh's ASCII reader
    tolerantly accepts whatever complete facets it finds before a truncation point -- cutting
    mid-cube (as this used to) leaves several complete facets and a non-empty, non-failing mesh.
    Zero complete facets is what actually makes load_stl() report an empty mesh and
    Model::read_from_file() throw, for phase I's failed-reload check."""
    with open(path, "w") as f:
        f.write("solid test\n  facet normal 0 0 0\n    outer loop\n      vertex 0 0 0\n")


def directory_noise(dir_path, stop_event, interval=0.2):
    """Creates and immediately deletes a uniquely-named file every `interval` seconds until
    stop_event is set -- reliable directory-listing churn regardless of watcher backend, for
    phase K's debounce-cap check."""
    i = 0
    while not stop_event.is_set():
        p = os.path.join(dir_path, "noise_%d.tmp" % i)
        with open(p, "w") as f:
            f.write("x")
        os.remove(p)
        i += 1
        stop_event.wait(interval)


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


def require_prefs(data_dir, want_reload, want_slice):
    """Blocks until OrcaSlicer.conf shows the two preferences in the wanted state.

    Toggling a checkbox in Preferences saves the file immediately, so a mismatch here means
    the checkbox really is in the wrong state, not that the file is lagging.
    """
    wanted = {PREF_RELOAD: (PREF_RELOAD_LABEL, want_reload), PREF_SLICE: (PREF_SLICE_LABEL, want_slice)}
    while True:
        prefs = read_prefs(data_dir)
        if prefs is None:
            print("  Could not read OrcaSlicer.conf under %s, so the preferences can't be verified." % data_dir)
            if ask("  Continue anyway?"):
                return
            sys.exit(1)
        wrong = [(label, want) for key, (label, want) in wanted.items() if prefs[key] != want]
        if not wrong:
            print("  Preferences verified: %s=%s, %s=%s" % (PREF_RELOAD_LABEL, want_reload, PREF_SLICE_LABEL, want_slice))
            return
        for label, want in wrong:
            print("  '%s' must be %s but is %s" % (label, "ON" if want else "OFF", "OFF" if want else "ON"))
        input("  Fix it in Preferences, then press Enter to re-check... ")


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
    # Extra objects for phases G-L; written now so the file list printed below is complete,
    # imported later once each phase is reached. Each one is only ever shrunk afterward, never
    # grown: Auto Arrange only runs once, right after import, so it only ever sees an object at
    # its largest -- shrinking afterward can't grow one into whatever space arrange left for a
    # neighbor, the way growing did (confirmed: the slicer's overlap check flagged exactly that
    # during testing).
    stl_g_changed = os.path.join(work_dir, "second_a.stl")
    stl_g_missing = os.path.join(work_dir, "second_b.stl")
    write_cube_stl(stl_g_changed, 30)
    write_cube_stl(stl_g_missing, 8)
    # More extra objects, for phases H (.obj), I (corrupt write) and J (same-second rewrite).
    obj_path = os.path.join(work_dir, "cube.obj")
    flaky_stl = os.path.join(work_dir, "flaky.stl")
    quick_stl = os.path.join(work_dir, "quick.stl")
    write_obj_cube(obj_path, 22)
    write_cube_stl(flaky_stl, 26)
    write_cube_stl(quick_stl, 25)
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
    require_prefs(args.data_dir, want_reload=True, want_slice=False)

    # --- reload on, slice off -------------------------------------------------------------
    print("\n[A] In-place overwrite (20 -> 30 mm)")
    tail.mark(); time.sleep(1.5)
    write_cube_stl(stl, 30)
    ok = tail.wait_for(RELOAD_MARK, args.timeout)
    record("A1 reload after in-place overwrite", ok, "" if ok else "no reload line in log within %gs" % args.timeout)
    if ok:
        record("A2 no slice when '%s' is off" % PREF_SLICE_LABEL, tail.absent_after(SLICE_START_MARK, args.quiet_window))
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
    require_prefs(args.data_dir, want_reload=True, want_slice=True)
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

    print("\n[G] Two more objects: one's source changes, the other's vanishes")
    pause("Import both %s and %s as two NEW, separate objects (in addition to the existing one), "
          "then Auto Arrange so they don't overlap it or each other."
          % (stl_g_changed, stl_g_missing))
    tail.mark(); time.sleep(1.5)
    write_cube_stl(stl_g_changed, 24)
    os.remove(stl_g_missing)
    ok = tail.wait_for(RELOAD_MARK, args.timeout)
    record("G1 reload after the change", ok, "" if ok else "no reload line in log within %gs" % args.timeout)
    if ok:
        count = tail.last_match(RELOADABLE_COUNT_RE)
        record("G2 only the changed volume was selected for reload", count == "1",
               "reloadable volumes number is: %s (expected 1 -- reload is not targeted)" % count)
        no_missing_warning = not tail.has_seen(MISSING_SOURCE_MARK)
        record("G3 missing object's volume was never selected in the first place", no_missing_warning,
               "" if no_missing_warning else "reload_from_disk() logged a missing-source warning for it")
        record("G4 no dialog appeared for the missing source", ask("  No error/warning dialog popped up?"))
        record("G5 only the changed object updated",
               ask("  Did only %s shrink to 24 mm, with %s and %s both left exactly as they were?"
                   % (os.path.basename(stl_g_changed), os.path.basename(stl_g_missing), os.path.basename(stl))))

    print("\n[H] .obj source, overwritten -- must reload with no color-import dialog")
    pause("Import %s as a new object, then Auto Arrange so it doesn't overlap the others." % obj_path)
    tail.mark(); time.sleep(1.5)
    write_obj_cube(obj_path, 14)
    ok = tail.wait_for(RELOAD_MARK, args.timeout)
    record("H1 reload after .obj overwrite", ok, "" if ok else "no reload line in log within %gs" % args.timeout)
    if ok:
        record("H2 no color-import dialog appeared", ask("  No color/material-import dialog popped up?"))
        record("H3 model visibly updated", ask("  Did %s shrink to 14 mm?" % os.path.basename(obj_path)))

    print("\n[I] Overwrite with a truncated/corrupt file, then a valid one")
    pause("Import %s as a new object, then Auto Arrange so it doesn't overlap the others." % flaky_stl)
    tail.mark(); time.sleep(1.5)
    write_truncated_stl(flaky_stl)
    ok = tail.wait_for(RELOAD_MARK, args.timeout)
    record("I1 a reload was attempted for the corrupt write", ok,
           "" if ok else "no reload line in log within %gs" % args.timeout)
    if ok:
        failed = tail.wait_for(LOAD_FAILED_MARK, args.timeout)
        record("I2 the load failure was logged, not silently accepted", failed,
               "" if failed else "no '%s' warning within %gs" % (LOAD_FAILED_MARK, args.timeout))
        record("I3 no dialog appeared for the failed load", ask("  No error/warning dialog popped up?"))
        record("I4 the object is unchanged (still 26 mm)",
               ask("  Is %s still the original 26 mm cube?" % os.path.basename(flaky_stl)))
        tail.mark()
        write_cube_stl(flaky_stl, 12)
        ok2 = tail.wait_for(RELOAD_MARK, args.timeout)
        record("I5 a later valid write still reloads (the failed attempt didn't consume it)", ok2,
               "" if ok2 else "no reload line within %gs" % args.timeout)
        if ok2:
            record("I6 model visibly updated", ask("  Did %s shrink to 12 mm?" % os.path.basename(flaky_stl)))

    print("\n[J] Two overwrites landing close together, different sizes -- both must be picked up")
    pause("Import %s as a new object, then Auto Arrange so it doesn't overlap the others." % quick_stl)
    tail.mark(); time.sleep(1.5)
    write_cube_stl(quick_stl, 18)
    ok = tail.wait_for(RELOAD_MARK, args.timeout)
    record("J1 reload after the first write", ok, "" if ok else "no reload line in log within %gs" % args.timeout)
    if ok:
        tail.mark()
        # Written as soon as possible after J1's reload completes: the closer this lands to the
        # same wall-clock second as that reload committing its baseline, the more directly this
        # exercises comparing (mtime, size) rather than mtime alone. The fractional size is
        # deliberate, not cosmetic: write_cube_stl()'s "%g" formatting makes any two whole-number
        # sizes from 10-99mm serialize to the exact same byte count (e.g. 18 and 15 both produce a
        # 1471-byte file), which would silently turn this into the same-size case the design doc
        # documents as accepted-invisible, instead of the different-size case this phase means to
        # exercise. Shrinking (not growing) throughout, same reason as G/H/I: Auto Arrange only
        # sees this object at its largest, at import time.
        write_cube_stl(quick_stl, 14.5)
        ok2 = tail.wait_for(RELOAD_MARK, args.timeout)
        record("J2 reload after the second write", ok2,
               "" if ok2 else "no reload line within %gs -- a same-second rewrite may have been missed" % args.timeout)
        if ok2:
            record("J3 final geometry is the second write (14.5 mm, not 18)",
                   ask("  Is %s 14.5 mm?" % os.path.basename(quick_stl)))

    print("\n[K] Background directory noise while overwriting -- reload must still fire within the debounce cap")
    stop_noise = threading.Event()
    noise_thread = threading.Thread(target=directory_noise, args=(work_dir, stop_noise), daemon=True)
    tail.mark()
    noise_thread.start()
    time.sleep(0.5)
    write_cube_stl(stl_g_changed, 16)  # reuses the object imported in phase G, shrinking it further
    ok = tail.wait_for(RELOAD_MARK, 6.0)  # well under the noise's duration, comfortably above the ~2.5s cap
    stop_noise.set()
    noise_thread.join(timeout=2.0)
    record("K1 reload still fires despite directory noise", ok,
           "" if ok else "no reload line within 6s -- the debounce cap may not be holding")
    if ok:
        record("K2 model visibly updated", ask("  Did %s shrink to 16 mm?" % os.path.basename(stl_g_changed)))

    print("\n[L] Multi-plate: only the plate with the reloaded object should reslice")
    pause("Set up two plates for this check:\n"
          "  1. Make sure there are at least 2 plates (\"+\" in the plate list to add one if needed).\n"
          "  2. Move the object built from %s onto plate 2 (drag it there, or right-click > Move to plate).\n"
          "  3. Leave at least one other object on plate 1.\n"
          "  4. Click \"Slice all\" and wait for both plates to finish slicing." % stl_g_changed)
    tail.mark(); time.sleep(1.5)
    write_cube_stl(stl_g_changed, 10)
    ok = tail.wait_for(RELOAD_MARK, args.timeout)
    record("L1 reload after the change", ok, "" if ok else "no reload line in log within %gs" % args.timeout)
    if ok:
        started = tail.wait_for(SLICE_START_MARK, args.timeout)
        record("L2 slice started automatically", started, "" if started else "no slice-start line within %gs" % args.timeout)
        if started:
            done = tail.wait_for(SLICE_DONE_MARK, args.timeout * 3)
            record("L3 slice completed", done, "" if done else "no completion line within %gs" % (args.timeout * 3))
        record("L4 only plate 2 (with the reloaded object) resliced",
               ask("  Check both plates' previews: did only plate 2's update, with plate 1's slice result "
                   "left untouched (not marked as needing a re-slice)?"))
        record("L5 view returned to where it was before this check",
               ask("  Is the plate view back on whatever plate was showing before this check (not left on "
                   "plate 2, unless that's where you started)?"))

    # Deliberately last among the reload-on phases: the pillar grid is slow to slice by design
    # (that's the point, to give a real slice something to interrupt), which makes this the
    # slowest single phase in the whole script on a slow machine. Everything faster runs first.
    h1, h2 = args.slow_height, args.slow_height / 2
    print("\n[F] Change arriving mid-slice: cube -> %g mm pillar grid, then %g mm while that slices" % (h1, h2))
    pause("This phase grows %s into a ~94 x 94 mm pillar grid (write_pillars_stl()'s default "
          "footprint, independent of height), which will overlap the objects phases G-L added if "
          "they're all still sharing its plate. Move %s's object to its own NEW, empty plate now "
          "(drag it onto \"+\" in the plate list, or right-click > Move to new plate), then switch "
          "to that plate."
          % (os.path.basename(stl), os.path.basename(stl)))
    tail.mark(); time.sleep(1.5)
    write_pillars_stl(stl, h1)
    ok = tail.wait_for(RELOAD_MARK, args.timeout) and tail.wait_for(SLICE_START_MARK, args.timeout)
    record("F1 reload and slice start for the pillar grid", ok)
    if ok:
        time.sleep(args.mid_slice_delay)
        still_running = not tail.has_seen(SLICE_DONE_MARK)
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
    require_prefs(args.data_dir, want_reload=False, want_slice=True)
    print("\n[E] In-place overwrite with auto-reload off (pillars -> 35 mm cube)")
    tail.mark(); time.sleep(1.5)
    write_cube_stl(stl, 35)
    quiet = tail.absent_after(RELOAD_MARK, args.quiet_window)
    record("E1 no reload when '%s' is off" % PREF_RELOAD_LABEL, quiet, "" if quiet else "a reload happened anyway")
    if quiet:
        record("E2 model unchanged", ask("  Is %s still the pillar grid (not a cube)?" % os.path.basename(stl)))

    # --- summary --------------------------------------------------------------------------
    failed = [r for r in results if not r[1]]
    print("\n%d checks, %d failed" % (len(results), len(failed)))
    for name, _, detail in failed:
        print("  FAIL %s%s" % (name, (" -- " + detail) if detail else ""))
    if failed:
        print("Test files left in %s; log at %s" % (work_dir, log_path))
        sys.exit(1)
    os.remove(stl)
    os.remove(stl_g_changed)
    if os.path.exists(stl_g_missing):  # phase G deletes this one
        os.remove(stl_g_missing)
    os.remove(obj_path)
    os.remove(flaky_stl)
    os.remove(quick_stl)
    try:
        os.rmdir(work_dir)  # only if nothing else is in it
    except OSError:
        pass
    print("All passed. Remember to restore the two preferences to the values you want.")


if __name__ == "__main__":
    main()
