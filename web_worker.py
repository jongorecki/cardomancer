# web_worker.py
# ---------------------------------------------------------------------------
# Background worker thread for the card sorter web server.
#
# All hardware operations (serial I/O) run in a single worker thread to
# avoid concurrency issues. Commands are sent via a thread-safe queue.
# Results and events are pushed back via a callback (SocketIO emit).
#
# State machine: disconnected -> idle -> sorting -> paused -> idle
# Emergency stop bypasses the queue entirely.
# ---------------------------------------------------------------------------

import os
import queue
import threading
import time
import cv2
import numpy as np
from PIL import Image

from web_motion_sim import motion_tracker


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

STATES = ('disconnected', 'idle', 'sorting', 'paused', 'estopped')


# ---------------------------------------------------------------------------
# Bin layout helper
# ---------------------------------------------------------------------------

def _apply_sort_bin_layout(worker, gcode_control, bin_count):
    """Apply physical bin positions for a custom sort session.

    Preserves existing calibrated bin positions (from ArUco / manual
    calibration) rather than overwriting them with evenly-spaced defaults.
    Only falls back to configure_bins() when no calibrated positions exist.

    Also logs a warning when the sort config requests more bins than are
    physically calibrated so the user knows upfront which bins won't have
    dedicated physical slots.
    """
    existing = gcode_control.get_bin_locations()
    dest_bins = {k: v for k, v in existing.items() if k > 0}

    if not dest_bins:
        # No calibrated positions — generate evenly-spaced defaults.
        gcode_control.configure_bins(bin_count)
        return

    max_cal = max(dest_bins)
    if bin_count <= max_cal:
        # All sort bins have calibrated positions — nothing to do.
        return

    # Warn: some bins lack calibrated positions.
    missing = list(range(max_cal + 1, bin_count + 1))
    worker.log(
        f"WARNING: Sort config uses {bin_count} bins but only bins 1–{max_cal} "
        f"are physically calibrated. Bins {missing} will use bin {max_cal}'s "
        f"physical position as fallback. Calibrate those bins or reduce the "
        f"sort config bin count to {max_cal}."
    )


class SortWorker:
    """
    Background worker that processes hardware commands from a queue.
    """

    # Max commands that can be queued before we start rejecting.
    # The worker usually processes one command at a time and most commands
    # are user-triggered, so this should never get near 50 in practice —
    # if it does, it's a sign of a stuck worker or runaway caller.
    MAX_QUEUE_SIZE = 50

    def __init__(self):
        self._queue = queue.Queue(maxsize=self.MAX_QUEUE_SIZE)
        self._thread = None
        self._running = False
        self._state = 'disconnected'
        self._emit_fn = None  # Set by web_server to SocketIO emit
        self._lock = threading.Lock()

        # Session state
        self.tracker = None
        self.sort_mode = None
        self.sort_config_obj = None
        self.scan_count = 0
        self.last_card_info = None
        self.last_card_bin = None
        self.last_card_method = None

        # Stack estimation
        self.empty_bin_z = None  # Calibrated Z when source bin is empty
        self.last_source_z = None  # Last probed Z at source
        self.card_thickness = 0.3  # mm per card
        self.sort_times = []  # Recent sort durations for speed estimate

        # Multi-source bin support (from ArUco calibration)
        self.source_bins = []     # List of source bin dicts from calibration
        self.source_empty = {}    # {bin_number: bool} — True if empty
        self.last_drop_x = None   # X position of last card drop (for nearest-source)

        # Continuous sort mode
        self.continuous_sorting = False
        self.continuous_delay = 0.1  # seconds between auto-detect cycles

        # Periodic re-home
        self.rehome_interval = 100  # re-home X every N cards
        self._cards_since_rehome = 0

        # No-detect retry tracking
        self._no_detect_retries = 0
        self._max_no_detect_retries = 3

        # Image capture
        self._scan_images_dir = None  # Session-specific image directory
        self._staging_snapshot_frame = None  # Snapshot for ROI drawing

        # Mid-sort undo
        self.last_scan_id = None  # DB scan ID for undo
        self.undo_available = False

        # Test scan
        self.test_scan_stop = False

        # Wishlist bin
        self.wishlist_bin = None  # Bin number reserved for wishlist matches

        # --- Priority bin (Phase 4.21) ---
        # Highest-precedence routing layer: if the scanned card's oracle_id
        # is in the configured Moxfield wishlist AND priority_bin is set,
        # route here — above override bins and regular sort-config bins.
        # priority_wishlist_source: "moxfield:<username>" | None
        # priority_oracle_ids:     pre-loaded set for O(1) lookup
        self.priority_bin = None
        self.priority_wishlist_source = None
        self.priority_oracle_ids = set()

        # Bin overflow / fullness tracking
        # overflow_map: logical_bin -> [physical_bin_1, physical_bin_2, ...]
        # When a physical bin fills up, cards route to the next bin in the chain.
        # If not configured, each logical bin maps to itself: {1: [1], 2: [2], ...}
        self.overflow_map = {}        # {int: [int, ...]}
        self.bin_card_counts = {}     # {physical_bin: int} — cards dropped this session
        self.bins_full = set()        # set of physical bin numbers at capacity
        self.bin_card_limit = 150     # max cards per physical bin before overflow

        # Abort flag for long-running multi-phase commands (e.g. new
        # hardware setup). Set from outside the worker thread via
        # `request_abort()`. The running command polls `self._abort_requested`
        # at its natural checkpoints and bails cleanly. This is separate
        # from the calibrator's own `running` flag so a single cancel
        # request can stop BOTH the sweep AND the phases that come after
        # it (applying positions, probing bins).
        self._abort_requested = False

        # Snapshot of session state captured at the moment of E-stop.
        # Used by _cmd_reset_after_estop to decide whether to drop
        # back into 'paused' (there was an active session — let the
        # user resume) or 'idle' (no session, start fresh).
        self._pre_estop_state = None

        # Staging background capture — synchronisation primitive.
        # The worker thread blocks on this event while waiting for
        # the user to confirm the staging platform is clear.
        self._staging_capture_event = threading.Event()

        # Focus lock confirmation — worker blocks on this while the user
        # checks the live feed is in focus on the first card.
        self._focus_confirm_event = threading.Event()

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, new_state):
        old = self._state
        self._state = new_state
        if self._emit_fn and old != new_state:
            self._emit_fn('sorter_state', {'state': new_state, 'previous': old})

    def set_emit(self, emit_fn):
        """Set the SocketIO emit callback."""
        self._emit_fn = emit_fn

    def emit(self, event, data=None):
        """Emit a SocketIO event if callback is set."""
        if self._emit_fn:
            self._emit_fn(event, data or {})

    def log(self, message):
        """Emit a log message."""
        print(f"[worker] {message}")
        self.emit('log_message', {'message': message, 'timestamp': time.time()})

    def start(self):
        """Start the worker thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the worker thread."""
        self._running = False
        # Drain a slot if full so the sentinel can fit, then send it.
        try:
            self._queue.put(None, timeout=1)
        except queue.Full:
            try:
                self._queue.get_nowait()  # make room
                self._queue.put_nowait(None)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)

    def enqueue(self, command, **kwargs):
        """
        Add a command to the queue. Returns True if enqueued, False if
        the queue is full (the worker may be stuck). We use put_nowait
        so callers never block — if the queue is full the request is
        rejected with a logged warning and the caller sees False.
        """
        try:
            self._queue.put_nowait((command, kwargs))
            return True
        except queue.Full:
            self.log(f"COMMAND QUEUE FULL — rejecting '{command}' "
                     f"(qsize={self._queue.qsize()})")
            try:
                self.emit('error', {
                    'message': 'command queue full; worker may be stuck',
                    'command': command,
                })
            except Exception:
                pass
            return False

    def queue_depth(self):
        """Return current queue depth (for health checks)."""
        try:
            return self._queue.qsize()
        except Exception:
            return -1

    def request_abort(self, reason=''):
        """
        Request that the currently-running command stop at its next
        checkpoint. Safe to call from any thread — just flips a flag.
        Used by the /api/calibration/cancel endpoint so cancellation
        doesn't have to wait behind a queued command.
        """
        self._abort_requested = True
        if reason:
            self.log(f"Abort requested: {reason}")

    def _abort_check(self):
        """
        Return True if an abort has been requested. Long-running command
        handlers should call this between phases and bail cleanly when
        it returns True.
        """
        return self._abort_requested

    def _clear_abort(self):
        """Reset the abort flag before starting a new long-running command."""
        self._abort_requested = False

    # --- Emergency Stop (bypasses queue) ---

    def emergency_stop(self):
        """
        EMERGENCY STOP — writes M112 directly to serial, bypasses queue.
        Kills all motion and pumps immediately.

        Session handling:
          The active session (tracker, sort_mode, sort_config_obj,
          bin counts, etc.) is PRESERVED across the e-stop so the user
          can press "Reset & Re-home" afterward and resume where they
          left off. Previously we ended the tracker here, which meant
          the reset endpoint had no session to resume into and the UI
          looked like nothing happened.

          The `_pre_estop_state` dict records whether a session was
          active so reset-after-estop knows which target state to
          transition to (paused vs idle).
        """
        import gcode_control
        self.log("!!! EMERGENCY STOP !!!")

        # Clear the command queue — we don't want stale motion commands
        # running after the reset.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

        # Set the abort flag so any currently-running command bails
        # at its next checkpoint. This is critical: without it, an
        # in-flight detect_and_sort will keep running on a halted
        # controller, getting serial errors, until it finishes.
        self._abort_requested = True

        # Send M112 directly to serial port
        if gcode_control.is_connected() and gcode_control.ser:
            try:
                gcode_control.ser.write(b'M112\n')
                time.sleep(0.1)
                # Kill pumps
                gcode_control.ser.write(b'M106 P0 S0\n')
                gcode_control.ser.write(b'M106 P1 S0\n')
            except Exception as e:
                self.log(f"E-stop serial write error: {e}")

        # Remember what we were doing so reset-after-estop can resume.
        # We do NOT end the tracker here — we want to be able to pick
        # the session back up after the user reset the machine.
        self._pre_estop_state = {
            'had_session': self.tracker is not None,
            'previous_state': self._state,
            'sort_mode': self.sort_mode,
            'continuous': self.continuous_sorting,
        }

        # Force continuous sort off — the user should have to explicitly
        # restart it after the reset. Otherwise it would auto-resume
        # the moment state flips back to 'sorting'.
        self.continuous_sorting = False

        motion_tracker.set_carrying(None)
        self.state = 'estopped'
        self.emit('estop_triggered', {
            'timestamp': time.time(),
            'had_session': self._pre_estop_state['had_session'],
        })

    # --- Worker loop ---

    def _run(self):
        """
        Main worker loop — pulls commands from queue and executes.

        Crash-resilience: the inner loop is wrapped in an outer try/except
        so a catastrophic failure (threading hiccup, queue corruption, OOM,
        BaseException from a command handler) won't silently kill the
        worker thread. If the inner loop dies, we log, emit a
        `worker_crashed` event, sleep briefly, and restart. Only a clean
        `stop()` (sets `_running` False + sentinel) actually exits the
        outer loop.
        """
        restart_count = 0
        while self._running:
            try:
                self._inner_run_loop()
                # Clean exit (sentinel received) — don't restart.
                break
            except BaseException as outer_exc:
                restart_count += 1
                import traceback
                tb = traceback.format_exc()
                print(f"[worker] CRITICAL: worker loop crashed ({outer_exc!r}) — "
                      f"restart #{restart_count}")
                print(tb)
                try:
                    self.emit('worker_crashed', {
                        'error': str(outer_exc),
                        'restart_count': restart_count,
                        'traceback': tb,
                    })
                except Exception:
                    pass
                # Safety: try to stop any active motion / pumps so the
                # machine is in a known state after a crash.
                try:
                    self._emergency_safety_reset('worker crash')
                except Exception:
                    pass
                # Short pause before restart to avoid tight crash loop.
                time.sleep(1.0)
                if restart_count >= 10:
                    print("[worker] FATAL: too many restarts — giving up")
                    try:
                        self.emit('worker_fatal', {
                            'message': 'worker crashed repeatedly, giving up',
                        })
                    except Exception:
                        pass
                    self._running = False
                    break

    def _inner_run_loop(self):
        """Pull commands from the queue and execute them until stop()."""
        while self._running:
            try:
                item = self._queue.get(timeout=1)
            except queue.Empty:
                continue

            if item is None:
                return  # Clean shutdown sentinel

            command, kwargs = item
            try:
                handler = getattr(self, f'_cmd_{command}', None)
                if handler:
                    handler(**kwargs)
                else:
                    self.log(f"Unknown command: {command}")
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                self.log(f"Error executing '{command}': {e}")
                print(tb)
                self.emit('error', {
                    'message': str(e),
                    'command': command,
                    'traceback': tb,
                })
                # Try to bring the machine back to a safe state so the
                # next command doesn't start from a half-finished move.
                try:
                    self._auto_safety_reset(command, e)
                except Exception as reset_err:
                    print(f"[worker] safety-reset failed: {reset_err}")

    def _auto_safety_reset(self, failed_command, exc):
        """
        Called after a command handler raises. Brings the hardware
        to a known-safe state so the next queued command doesn't
        start from an unknown position. We do NOT re-home (that would
        be disruptive mid-session); we just kill pumps and lift Z so
        nothing is pressed against the stack.
        """
        # Only bother if we're connected.
        try:
            import gcode_control
            if not gcode_control.is_connected():
                return
        except Exception:
            return

        # Sorting-state commands are the dangerous ones (moving head,
        # pumps engaged). Cal / probe / simple moves don't need this.
        risky_commands = (
            'detect_and_sort', 'start_continuous', 'probe_bin',
            'probe_all_bins', 'new_hardware_setup', 'test_scan',
            'undo_last_sort',
        )
        if failed_command not in risky_commands:
            return

        try:
            self.log(f"Auto safety-reset after '{failed_command}' failure")
            # Kill both pumps
            gcode_control._send_gcode('M106 P0 S0')
            gcode_control._send_gcode('M106 P1 S0')
            # Lift Z to safe height
            try:
                gcode_control.move_z(0)
            except Exception:
                pass
            # If we were in sorting state, drop back to paused so the
            # user has to explicitly resume.
            if self.state == 'sorting':
                self.state = 'paused'
                self.emit('session_paused', {
                    'reason': f'auto-paused after error: {exc}',
                })
        except Exception as e:
            print(f"[worker] _auto_safety_reset error: {e}")

    def _emergency_safety_reset(self, reason):
        """
        Hard safety reset used after catastrophic worker crashes.
        Writes directly to serial if possible, bypassing the queue.
        """
        try:
            import gcode_control
            if gcode_control.is_connected() and gcode_control.ser:
                try:
                    gcode_control.ser.write(b'M106 P0 S0\n')
                    gcode_control.ser.write(b'M106 P1 S0\n')
                except Exception:
                    pass
                self.log(f"Emergency safety reset: {reason}")
        except Exception:
            pass

    # --- Command handlers ---

    def _cmd_connect(self, **kwargs):
        """Connect to the control board."""
        import gcode_control
        gcode_control.connect_to_board()
        # Wire up bin fullness callback
        gcode_control.set_bin_fullness_callback(self._on_bin_fullness)
        if gcode_control.is_connected():
            self.state = 'idle'
            self.log("Connected to control board")
            self.emit('hardware_status', {
                'connected': True,
                'port': gcode_control.SERIAL_PORT,
            })
        else:
            self.log("Failed to connect to control board")
            self.emit('error', {'message': 'Failed to connect to control board'})

    def _cmd_disconnect(self, **kwargs):
        """Disconnect from the control board."""
        import gcode_control
        if self.tracker:
            self.tracker.end_session()
            self.tracker = None
        gcode_control.all_pumps_off()
        gcode_control.close_connection()
        self.state = 'disconnected'
        self.log("Disconnected from control board")
        self.emit('hardware_status', {'connected': False})

    def _cmd_home(self, axis=None, **kwargs):
        """Home axes."""
        import gcode_control
        if axis == 'x':
            self.log("Homing X axis...")
            gcode_control.home_x()
            motion_tracker.update_position(x=0)
        elif axis == 'z':
            self.log("Homing Z axis...")
            gcode_control.home_z()
            motion_tracker.update_position(z=gcode_control.Z_MAX)
        else:
            self.log("Homing all axes...")
            gcode_control.home_all()
            motion_tracker.update_position(x=0, z=gcode_control.Z_MAX)
        self.log("Homing complete")
        self.emit('motion_update', motion_tracker.get_state())

    def _cmd_move_to_detection(self, **kwargs):
        """Move to the detection position."""
        import gcode_control
        self.log("Moving to detection position...")
        motion_tracker.start_move(dest_z=gcode_control.Z_MAX, feedrate=gcode_control.Z_FEEDRATE)
        gcode_control.move_to_detection_position()
        motion_tracker.update_position(x=gcode_control.X_DETECTION_POSITION, z=gcode_control.Z_MAX)
        self.emit('motion_update', motion_tracker.get_state())

    def _cmd_move_x(self, x=0, **kwargs):
        """Move X to a position."""
        import gcode_control
        self.log(f"Moving X to {x}mm...")
        motion_tracker.start_move(dest_x=x, feedrate=gcode_control.X_FEEDRATE)
        gcode_control.move_x(x)
        gcode_control.wait_for_completion()
        motion_tracker.update_position(x=x)
        self.emit('motion_update', motion_tracker.get_state())

    def _cmd_move_z(self, z=None, **kwargs):
        """Move Z to a position."""
        import gcode_control
        if z is None:
            z = gcode_control.Z_MAX
        self.log(f"Moving Z to {z}mm...")
        motion_tracker.start_move(dest_z=z, feedrate=gcode_control.Z_FEEDRATE)
        gcode_control.move_z(z)
        gcode_control.wait_for_completion()
        motion_tracker.update_position(z=z)
        self.emit('motion_update', motion_tracker.get_state())

    def _cmd_test_bin(self, bin_number=1, **kwargs):
        """Move to a bin position for testing."""
        import gcode_control
        bin_locs = gcode_control.get_bin_locations()
        x = bin_locs.get(bin_number, 150.0)
        self.log(f"Testing bin {bin_number} at X={x}mm...")
        motion_tracker.start_move(dest_z=gcode_control.Z_MAX, feedrate=gcode_control.Z_FEEDRATE)
        gcode_control.move_to_bin(bin_number)
        gcode_control.wait_for_completion()
        motion_tracker.update_position(x=x, z=gcode_control.Z_MAX)
        self.log(f"At bin {bin_number}")
        self.emit('motion_update', motion_tracker.get_state())

    def _cmd_probe_bin(self, bin_number=0, **kwargs):
        """Probe Z height at a bin position."""
        import gcode_control
        bin_locs = gcode_control.get_bin_locations()
        x = bin_locs.get(bin_number, 150.0)
        self.log(f"Probing bin {bin_number} at X={x}mm...")

        # Move to bin
        gcode_control.move_to_bin(bin_number)
        gcode_control.wait_for_completion()

        # Verify the carriage actually arrived at the commanded X.
        # Marlin's firmware software endstops (X_MAX_POS) silently
        # clip far-bin moves with an "ok" response, so "Probing bin
        # 6 at X=980mm" could in fact probe at X=800mm and report
        # garbage for that bin's Z height. Catching the mismatch
        # here lets us skip the probe rather than cache a wrong Z
        # that will later cause a crash during real sorting.
        actual_x, ok = gcode_control.verify_x_position(x, tolerance=1.0)
        if not ok:
            err = (f"Bin {bin_number}: commanded X={x:.1f}mm but "
                   f"arrived at X={actual_x if actual_x is not None else '?'}. "
                   f"Skipping probe — firmware software endstop is "
                   f"clipping this move. Restart server (or run M211 "
                   f"S0) and re-probe.")
            self.log(f"PROBE SKIPPED: {err}")
            self.emit('probe_result', {
                'bin': bin_number,
                'x': x,
                'z': None,
                'error': err,
            })
            # Lift Z so we don't leave the carriage stuck mid-probe
            try:
                gcode_control.z_to_top()
                gcode_control.wait_for_completion()
            except Exception:
                pass
            return

        # Probe down
        gcode_control._probe_with_cache(x)
        z = gcode_control._get_current_z()

        # Lift back up
        gcode_control.z_to_top()
        gcode_control.wait_for_completion()

        self.log(f"Bin {bin_number} probe height: Z={z}")
        motion_tracker.update_position(x=x, z=gcode_control.Z_MAX)
        self.emit('probe_result', {
            'bin': bin_number,
            'x': x,
            'z': z,
        })
        self.emit('motion_update', motion_tracker.get_state())

    def _cmd_probe_all_bins(self, **kwargs):
        """Probe all bins sequentially."""
        import gcode_control
        bin_locs = gcode_control.get_bin_locations()
        results = {}
        for bin_num in sorted(bin_locs.keys()):
            if self.state == 'estopped':
                break
            self._cmd_probe_bin(bin_number=bin_num)
            # The probe result is emitted inside _cmd_probe_bin
            z = gcode_control._probe_z_cache.get(bin_locs[bin_num])
            results[bin_num] = z
        self.emit('probe_all_complete', {'results': results})

    def _cmd_configure_bins(self, bin_count=10, start_x=None, spacing=None, **kwargs):
        """Configure bin positions."""
        import gcode_control
        gcode_control.configure_bins(bin_count, start_x=start_x, spacing=spacing)
        bin_locs = gcode_control.get_bin_locations()
        self.log(f"Configured {bin_count} bins")
        self.emit('bins_configured', {
            'count': bin_count,
            'locations': {str(k): v for k, v in bin_locs.items()},
        })
        # Auto-save so config survives server restarts
        try:
            from web_server import _auto_save_bin_config
            _auto_save_bin_config()
        except Exception:
            pass

    def _cmd_start_session(self, mode='color', config_file=None,
                           config_lines=None, custom_queries=None,
                           overflow_map=None, notes=None, **kwargs):
        """Start a new sorting session.

        Every session now runs through a SortConfig — no more mode-specific
        dispatch. Priority order for building the SortConfig:

          1. ``config_lines`` — inline content from the editable UI table.
          2. ``config_file`` — path or basename of a sort_configs/*.txt file.
          3. ``custom_queries`` — legacy dict form (bin_count, queries, ...).
          4. ``mode`` — legacy mode name. Loads sort_configs/<mode>.txt.

        ``mode`` is retained only as a human-readable label shown in the UI
        and logs. It no longer controls bin assignment.
        """
        from scan_tracker import ScanTracker
        from sorting import set_sort_config
        from sort_config import SortConfig
        from config import SORT_CONFIGS_DIR
        import os
        import gcode_control

        # Home the machine BEFORE doing anything else. Any previous
        # activity (manual jogs, E-stop, aborted probe, calibration
        # sweep, or even a first-run cold start) can leave the
        # firmware with an unknown or stale position. Driving to a
        # bin or source pickup from an unknown position can crash
        # the head into a bin wall, so we always force a clean home
        # at session start. Skipped only if the hardware isn't
        # connected at all (simulation / bench test runs).
        if gcode_control.is_connected():
            self.log("Homing machine before starting sort session...")
            try:
                gcode_control.home_all()
                motion_tracker.update_position(
                    x=0, z=gcode_control.Z_MAX)
                self.emit('motion_update', motion_tracker.get_state())
                self.log("Homing complete — machine position verified")
            except Exception as e:
                self.log(f"ERROR: Homing failed at session start: {e}")
                self.emit('error', {
                    'message': (f'Homing failed at session start: {e}. '
                                f'Session NOT started — fix the hardware '
                                f'issue and try again.'),
                })
                return
        else:
            self.log("Hardware not connected — skipping home "
                     "(simulation / bench mode)")

        # --- Auto-capture staging background reference ---
        # The staging background image MUST match the current camera
        # resolution. If it was captured at a different resolution (or
        # doesn't exist), detection quality degrades catastrophically
        # because morphological operations and background subtraction
        # rely on pixel-accurate comparisons. Capture a fresh reference
        # at every session start — the platform should be empty at this
        # point since cards go in the source bin.
        cam = kwargs.get('camera')
        if gcode_control.is_connected() and cam is not None:
            self._capture_staging_background(gcode_control, cam)

        self.sort_mode = mode
        self.sort_config_obj = None
        self.scan_count = 0
        self.sort_times = []
        self.last_card_info = None

        # Resolve the SortConfig — unified priority chain. Every session now
        # runs through SortConfig; the legacy per-mode dispatch is gone.
        try:
            if config_lines is not None:
                # Inline content from the editable UI table.
                if isinstance(config_lines, str):
                    lines = config_lines.splitlines()
                else:
                    lines = list(config_lines)
                self.sort_config_obj = SortConfig.from_lines(lines)
                self.log("Sort config loaded from inline UI content:")
                self.log(self.sort_config_obj.describe())

            elif config_file:
                filepath = config_file
                if not os.path.isabs(filepath):
                    filepath = os.path.join(SORT_CONFIGS_DIR, filepath)
                self.sort_config_obj = SortConfig.from_file(filepath)
                self.log(
                    f"Sort config loaded from {os.path.basename(filepath)}:"
                )
                self.log(self.sort_config_obj.describe())

            elif custom_queries:
                # Legacy manual-queries path (kept for API compat until the
                # UI is fully migrated to inline config_lines posting).
                bin_count = custom_queries.get('bin_count', 10)
                fallback_bin = custom_queries.get('fallback_bin', bin_count)
                if fallback_bin > bin_count:
                    self.log(f"WARNING: Fallback bin {fallback_bin} > "
                             f"bin_count {bin_count}, clamping to {bin_count}")
                    fallback_bin = bin_count
                if fallback_bin < 1:
                    fallback_bin = 1
                lines = [f'bins: {bin_count}', f'fallback: {fallback_bin}']
                if custom_queries.get('bin_limit'):
                    lines.append('limit: ' + str(custom_queries['bin_limit']))
                if custom_queries.get('overrides'):
                    ov_str = ','.join(str(b) for b in custom_queries['overrides'])
                    lines.append(f'overrides: {ov_str}')
                for bin_num, query_str in custom_queries.get('queries', {}).items():
                    if query_str.strip():
                        lines.append(f'bin{bin_num}: {query_str}')
                self.log(f"Building sort config from {len(lines)} lines.")
                self.sort_config_obj = SortConfig.from_lines(lines)
                self.log(self.sort_config_obj.describe())

            else:
                # Legacy mode name — load the corresponding built-in file
                # (sort_configs/color.txt, mana_value.txt, etc.).
                legacy_file = os.path.join(SORT_CONFIGS_DIR, f"{mode}.txt")
                if os.path.exists(legacy_file):
                    self.sort_config_obj = SortConfig.from_file(legacy_file)
                    self.log(f"Built-in sort config '{mode}' loaded:")
                    self.log(self.sort_config_obj.describe())
                else:
                    raise FileNotFoundError(
                        f"No sort config: mode='{mode}' has no built-in "
                        f"file at {legacy_file}, and no config_file / "
                        f"config_lines / custom_queries were provided."
                    )

            # All paths converge — activate the config.
            set_sort_config(self.sort_config_obj)
            _apply_sort_bin_layout(self, gcode_control,
                                   self.sort_config_obj.bin_count)
        except Exception as e:
            self.log(f"ERROR loading sort config: {e}")
            self.emit('error', {
                'message': (f'Sort config load failed: {e}. Session NOT '
                            f'started — fix the config and try again.'),
            })
            return

        # Apply overflow map from the UI (custom manual mode sends
        # overflow_map alongside custom_queries when overflow bins are
        # configured). For non-custom modes this is typically None.
        #
        # Each chain is sorted so the overflow bin physically closest
        # to the primary bin comes first. This minimizes carriage
        # travel when a bin fills up — the sorter spills into the
        # nearest overflow rather than jumping across the machine.
        if overflow_map:
            bin_locs = gcode_control.get_bin_locations() or {}
            raw_map = {int(k): [int(b) for b in v]
                       for k, v in overflow_map.items()}
            self.overflow_map = {}
            for primary, chain in raw_map.items():
                primary_x = bin_locs.get(primary, 0.0)
                # Sort overflow targets (everything after the primary)
                # by physical distance from the primary bin.
                if len(chain) > 1:
                    head = chain[0]   # always the primary itself
                    rest = chain[1:]  # overflow targets
                    rest.sort(key=lambda b: abs(
                        bin_locs.get(b, 0.0) - primary_x))
                    self.overflow_map[primary] = [head] + rest
                else:
                    self.overflow_map[primary] = chain
            chains = len(self.overflow_map)
            total_overflow = sum(len(v) - 1 for v in self.overflow_map.values()
                                 if len(v) > 1)
            self.log(f"Overflow map set from session config: {chains} chain(s), "
                     f"{total_overflow} overflow bin(s)")
            for primary, chain in self.overflow_map.items():
                dists = [f"bin {b} ({abs(bin_locs.get(b, 0.0) - bin_locs.get(primary, 0.0)):.0f}mm)"
                         for b in chain[1:]]
                self.log(f"  Bin {primary} -> overflow: {', '.join(dists) or 'none'}")
        else:
            self.overflow_map = {}

        # Clean up old scan images before starting new session
        self._cleanup_old_scan_images()
        self._scan_images_dir = None  # Reset for new session

        # Clear Z-probe cache — stack heights change between sessions as
        # cards are added/removed, so any cached probe values are stale.
        try:
            import gcode_control
            gcode_control.clear_probe_cache()
            self.log("Cleared Z-probe cache for new session")
        except Exception:
            pass

        # Reset continuous sort / undo state
        self.continuous_sorting = False
        self.undo_available = False
        self.last_scan_id = None
        self._cards_since_rehome = 0

        # Reset bin fullness counts (overflow_map persists — it's physical config)
        self.bin_card_counts = {}
        self.bins_full = set()

        # Start tracker
        self.tracker = ScanTracker()
        config_name = config_file if config_file else None
        import gcode_control
        # Use sort config bin count if custom, otherwise use current hardware bin count
        if self.sort_config_obj:
            bin_count = self.sort_config_obj.bin_count
        else:
            bin_locs = gcode_control.get_bin_locations()
            bin_count = max(k for k in bin_locs.keys() if k > 0) if bin_locs else 10
        self.tracker.start_session(
            sort_mode=self.sort_mode,
            config_name=config_name,
            bin_count=bin_count,
            notes=notes,
        )

        self.state = 'sorting'
        self.log(f"Session started: mode={self.sort_mode}")
        self.emit('session_started', {
            'mode': self.sort_mode,
            'bin_count': bin_count,
        })

    # ------------------------------------------------------------------
    # Staging background capture (called during session start)
    # ------------------------------------------------------------------

    def _capture_staging_background(self, gcode_mod, cam):
        """
        Set up the staging platform for detection.

        Flow:
        1. Move the camera over the staging platform.
        2. Flush frames and let auto-exposure settle.
        3. Grab a snapshot and emit ``staging_roi_prompt`` with the
           snapshot URL so the user can draw the staging ROI.
        4. Block until the user submits ROI corners (or times out).
        5. Save the ROI and the background reference frame.
        """
        from detection import save_staging_bg, save_staging_roi

        # 1. Move camera over staging
        self.log("Moving camera over staging platform...")
        try:
            gcode_mod.move_to_camera_position()
        except Exception as e:
            self.log(f"ERROR moving to camera position: {e}")
            self.emit('staging_capture_result', {
                'ok': False, 'reason': 'motion_error',
                'message': str(e),
            })
            return

        # 2. Flush camera buffer for fresh exposure
        time.sleep(0.5)
        for _ in range(10):
            cam.get_frame()
            time.sleep(0.05)
        time.sleep(0.5)

        # 3. Get frame dimensions for the frontend overlay
        probe_frame = cam.get_frame()
        if probe_frame is None:
            self.log("ERROR: No frame for staging setup")
            self.emit('staging_capture_result', {
                'ok': False, 'reason': 'no_frame',
                'message': 'Camera returned no frame.',
            })
            return
        h, w = probe_frame.shape[:2]

        # 4. Prompt user to draw ROI on the live camera feed
        self._staging_capture_event.clear()
        self.log("Waiting for user to draw staging platform bounds...")
        self.emit('staging_roi_prompt', {
            'message': ('Clear the staging platform, then click the 4 '
                        'corners of the platform surface.'),
            'width': w,
            'height': h,
        })

        # 5. Wait for ROI submission (timeout 120s — user needs time to draw)
        confirmed = self._staging_capture_event.wait(timeout=120)
        if not confirmed:
            self.log("WARNING: Staging ROI timed out — skipping.")
            self.emit('staging_capture_result', {
                'ok': False, 'reason': 'timeout',
                'message': 'Staging setup skipped (timed out).',
            })
            return

        # ROI corners were saved by the API endpoint via set_staging_roi().
        # Grab a FRESH frame now for the background reference — the user
        # just confirmed the platform is clear, so this is the right moment.
        cam.flush_buffer()
        time.sleep(0.2)
        frame = cam.get_frame()
        if frame is None:
            frame = probe_frame  # fallback to earlier frame
        ok = save_staging_bg(frame)
        if ok:
            self.log(f"Staging background + ROI saved ({w}x{h})")
            self.emit('staging_capture_result', {
                'ok': True, 'width': w, 'height': h,
                'message': f'Staging setup complete ({w}x{h})',
            })
        else:
            self.log("ERROR: Failed to save staging background")
            self.emit('staging_capture_result', {
                'ok': False, 'reason': 'save_failed',
                'message': 'Failed to write staging background image.',
            })

    def set_staging_roi(self, corners):
        """
        Called from the API thread when the user submits ROI corners.
        Saves the ROI and unblocks the worker.

        :param corners: list of 4 [x, y] pairs in pixel coordinates
        """
        from detection import save_staging_roi
        import numpy as np
        arr = np.array(corners, dtype=np.float32)
        save_staging_roi(arr)
        self.log(f"Staging ROI set: {corners}")
        self._staging_capture_event.set()

    def confirm_staging_capture(self):
        """Legacy — called to skip ROI drawing and just confirm."""
        self._staging_capture_event.set()

    def confirm_focus_lock(self):
        """Called from the API when the user confirms focus looks good."""
        self._focus_confirm_event.set()

    def _cmd_detect_and_sort(self, camera=None, **kwargs):
        """
        Full sort cycle for one card, matching the real machine flow:

        1. Pick card from source bin
        2. Drop on staging platform
        3. Move camera over staging, capture frame
        4. Start identification in background
        5. While ID runs: pick card back up from staging
        6. Once ID done + card picked up: move to target bin, drop
        7. Return to source for next card

        Identification is pipelined with the staging pickup to save time.
        """
        if self.state not in ('sorting',):
            self.log("Cannot detect — not in sorting state")
            return

        import gcode_control
        import threading
        from card_detect import detect_card
        from cards import extract_card_info, CARD_DATA_BY_ID
        from card_identify_hybrid import identify_card, is_card_back
        from foil_detect import detect_foil
        from config import PHASH_DISTANCE_THRESHOLD, PHASH_CLOSE_MATCH_DIFF, EXCLUDED_SETS

        sort_start = time.time()

        if camera is None:
            self.log("No camera available")
            return

        # Preflight: if the serial port dropped between commands we'd
        # silently do nothing. Check now so the user sees the reason.
        if not gcode_control.is_connected():
            self.log("Cannot detect — serial not connected")
            self.state = 'paused'
            self.emit('session_paused', {'reason': 'serial disconnected'})
            return

        # Abort check — let pause/stop interrupt before any motion.
        if self._abort_check() or self.state != 'sorting':
            self.log("detect_and_sort: aborted before start")
            return

        # On the very first card, log the active sort config + bin positions
        # so the user can verify routing is set up correctly.
        if self.scan_count == 0:
            from sorting import get_sort_config
            sc = get_sort_config()
            if sc:
                self.log(f"[sort] Active config: {sc.describe()}")
            bin_locs = gcode_control.get_bin_locations()
            loc_str = ', '.join(
                f'bin{k}={v:.0f}mm' for k, v in sorted(bin_locs.items())
            )
            self.log(f"[sort] Bin positions: {loc_str}")
            self.log(f"[sort] Sort mode: {self.sort_mode}")

        # --- Periodic re-home ---
        self._cards_since_rehome += 1
        if self._cards_since_rehome >= self.rehome_interval:
            self.log(f"Re-homing X after {self.rehome_interval} cards...")
            gcode_control._send_and_wait(f"G0 Z{gcode_control.Z_MAX} F{gcode_control.Z_FEEDRATE}")
            gcode_control._send_and_wait("M400")
            gcode_control._send_and_wait("G28 X")
            gcode_control._send_and_wait("M400")
            self._cards_since_rehome = 0
            self.log("X re-homed")

        # --- Step 1: Pick card from source bin ---
        gcode_control.pick_from_position(gcode_control.X_SOURCE_BIN)
        if self._abort_check() or self.state != 'sorting':
            self.log("detect_and_sort: aborted after source pick")
            # Kill pumps so we drop whatever we were carrying safely.
            try:
                gcode_control._send_gcode('M106 P0 S0')
                gcode_control._send_gcode('M106 P1 S0')
            except Exception:
                pass
            return

        # --- Step 2: Drop on staging platform ---
        gcode_control.drop_on_staging(self.scan_count + 1)
        if self._abort_check() or self.state != 'sorting':
            self.log("detect_and_sort: aborted after staging drop")
            return

        # --- Step 3: Move camera over staging, capture frame ---
        gcode_control.move_to_camera_position()
        time.sleep(0.5)   # Let carriage vibration settle
        camera.flush_buffer()
        time.sleep(0.3)   # Let auto-exposure adjust after flush
        if self._abort_check() or self.state != 'sorting':
            self.log("detect_and_sort: aborted before frame capture")
            return

        # --- Focus lock: first card of session ---
        # On the first card, show the user the live feed and let them
        # confirm the image is in focus before we lock the autofocus.
        # This ensures the lens has settled on the card (not the
        # platform, not mid-hunt). Subsequent cards skip this — the
        # focus is already locked and the distance hasn't changed.
        if not camera.focus_locked:
            self._focus_confirm_event.clear()
            self.log("Waiting for user to confirm focus...")
            self.emit('focus_confirm_prompt', {
                'message': ('The first card is on the staging platform. '
                            'Check that the live feed is in focus, then '
                            'click "Lock Focus" to continue.'),
            })
            confirmed = self._focus_confirm_event.wait(timeout=60)
            if not confirmed:
                self.log("WARNING: Focus confirm timed out — locking anyway")
            camera.lock_focus()
            self.log("Focus locked for this session")

        # Wait for a sharp (non-blurry) frame. Motion blur from the
        # carriage move and autofocus hunting both produce soft frames
        # that break contour detection and degrade hash matching.
        frame, sharpness = camera.get_sharp_frame(
            min_sharpness=50.0, max_wait=2.0, settle_frames=3)
        if frame is None:
            self.log("Failed to grab camera frame")
            # Card is on staging — pick it up and put in fallback bin
            gcode_control.pick_from_staging()
            gcode_control.quick_drop(self._get_fallback_x())
            if self.continuous_sorting and self.state == 'sorting':
                time.sleep(self.continuous_delay)
                from web_camera import camera as cam
                self.enqueue('detect_and_sort', camera=cam)
            return

        self.log(f"Frame sharpness: {sharpness:.1f}")

        # --- Save scan image ---
        self._save_scan_image(frame)

        # --- Detect card contour (fast, ~50ms) ---
        card_img = detect_card(frame)
        if card_img is None:
            self._no_detect_retries += 1
            self.log(f"No card detected on staging "
                     f"(attempt {self._no_detect_retries}/"
                     f"{self._max_no_detect_retries})")
            self._save_no_detect_image(frame)
            self.emit('card_detected', {'recognized': False,
                                        'reason': 'no_card_in_frame'})

            if self._no_detect_retries >= self._max_no_detect_retries:
                self.log("Max no-detect retries reached — "
                         "source may be empty, stopping")
                self._no_detect_retries = 0
                self.continuous_sorting = False
                self.emit('continuous_sort_stopped', {'reason': 'no_card'})
                return

            # No card on staging — go back to source and try again.
            # Don't pick from staging (nothing to pick up).
            if self.continuous_sorting and self.state == 'sorting':
                self.log("Retrying from source...")
                time.sleep(self.continuous_delay)
                from web_camera import camera as cam
                self.enqueue('detect_and_sort', camera=cam)
            return

        # Card detected — reset no-detect retry counter
        self._no_detect_retries = 0

        # --- Step 4+5: Identify card while picking up from staging ---
        #
        # Run identification in a background thread concurrently with
        # the physical staging pickup (~1-2s of serial I/O). The image
        # pipeline is simple and fast:
        #   1. Card-back check (phash vs reference)
        #   2. identify_card() — hybrid phash + DINOv2
        id_result = {
            'is_card_back': False,
            'card_back_dist': None,
            'card_info': None,
            'card_data': None,
            'method': None,
            'hash_distance': None,
            'was_rotated': False,
            'is_foil': False,
            'foil_confidence': None,
        }

        def _process_and_identify(img):
            # 1) Card-back check
            try:
                back_hit, back_dist = is_card_back(img)
            except Exception as e:
                self.log(f"[identify] is_card_back error: {e}")
                back_hit, back_dist = False, None
            id_result['card_back_dist'] = back_dist
            if back_hit:
                id_result['is_card_back'] = True
                self._save_card_crop(img)
                self.log(f"Card back detected (dist={back_dist:.1f}) "
                         f"— routing to fallback")
                return

            # 2) Identify card (handles orientation internally —
            #    tries both 0° and 180°, picks the better match)
            try:
                card_id, dist, was_rotated, all_results = \
                    identify_card(img, threshold=PHASH_DISTANCE_THRESHOLD)
            except Exception as e:
                self.log(f"[identify] identify_card error: {e}")
                card_id, dist, was_rotated, all_results = None, 999.0, False, []

            id_result['was_rotated'] = was_rotated
            id_result['hash_distance'] = dist
            if was_rotated:
                self.log("Card was upside-down, rotated 180°")

            # Save the card crop (oriented version)
            oriented = img
            if was_rotated:
                oriented = cv2.rotate(img, cv2.ROTATE_180)
            self._save_card_crop(oriented)

            # Filter for paper-only, non-excluded sets
            filtered = []
            for cid, d in all_results:
                cdata = CARD_DATA_BY_ID.get(cid, {})
                if 'paper' not in cdata.get('games', []):
                    continue
                sc = cdata.get('set', '').lower()
                if sc not in EXCLUDED_SETS:
                    filtered.append((cid, d))

            # Pretty-print top candidates
            def _fmt_top(n):
                parts = []
                for cid, d in filtered[:n]:
                    cdata = CARD_DATA_BY_ID.get(cid, {}) or {}
                    nm = cdata.get('name', cid)
                    setc = cdata.get('set', '?')
                    parts.append(f"{nm}[{setc}]={d:.1f}")
                return ", ".join(parts)

            if filtered:
                top_id, top_dist = filtered[0]
                id_result['hash_distance'] = top_dist

                if top_dist <= PHASH_DISTANCE_THRESHOLD:
                    ci = extract_card_info(top_id)
                    id_result['card_info'] = ci
                    id_result['card_data'] = CARD_DATA_BY_ID.get(top_id)
                    id_result['method'] = "hash"

                    # Foil detection: compare scan against the matched
                    # reference image. Cheap (~10ms), runs inside the
                    # background ID thread so it doesn't block motion.
                    try:
                        fr = detect_foil(oriented, card_id=top_id)
                        id_result['is_foil'] = bool(fr.get('is_foil', False))
                        id_result['foil_confidence'] = fr.get('confidence')
                        if fr.get('is_foil'):
                            self.log(
                                f"[foil] DETECTED foil "
                                f"(conf={fr.get('confidence', 0):+.2f}, "
                                f"reason={fr.get('reason', '?')})"
                            )
                    except Exception as e:
                        self.log(f"[foil] detect_foil error: {e}")

                    # Log close matches for diagnostics
                    if len(filtered) > 1:
                        gap = filtered[1][1] - top_dist
                        if gap < PHASH_CLOSE_MATCH_DIFF:
                            t_name = (ci or {}).get('Name', '?')
                            s_cdata = CARD_DATA_BY_ID.get(filtered[1][0], {}) or {}
                            s_name = s_cdata.get('name', '?')
                            if t_name != s_name:
                                self.log(
                                    f"[identify] Close match (gap={gap:.1f}): "
                                    f"{t_name}={top_dist:.1f} vs "
                                    f"{s_name}={filtered[1][1]:.1f} — taking top"
                                )
                    self.log(f"[identify] MATCH dist={top_dist:.1f} "
                             f"top3: {_fmt_top(3)}")
                else:
                    self.log(
                        f"[identify] NO MATCH (top={top_dist:.1f} > "
                        f"thresh={PHASH_DISTANCE_THRESHOLD}) "
                        f"top5: {_fmt_top(5)}"
                    )
            else:
                self.log("[identify] NO MATCH — zero allowed candidates")

        id_thread = threading.Thread(target=_process_and_identify,
                                      args=(card_img,),
                                      daemon=True)
        id_thread.start()

        # Pick the card back up from staging while ID runs in background.
        gcode_control.pick_from_staging()

        # Wait for identification to finish (usually already done).
        id_thread.join()

        # Pull results
        card_info = id_result['card_info']
        card_data = id_result['card_data']
        method = id_result['method']
        hash_distance = id_result['hash_distance']

        # --- Step 6: Motion parameters (shared by card-back and normal paths) ---
        bin_locs = gcode_control.get_bin_locations()
        staging_x = gcode_control.X_STAGING_POSITION
        z_clear = gcode_control.Z_CLEAR_HEIGHT or gcode_control.Z_MAX
        drop_z = gcode_control.Z_MAX - gcode_control.Z_DROP_OFFSET
        source_x = bin_locs.get(0, gcode_control.X_SOURCE_BIN)
        fallback_x = self._get_fallback_x()

        # --- Card-back routing ---
        if id_result['is_card_back']:
            back_dist = id_result['card_back_dist']
            logical_bin = 10
            physical_bin = self.resolve_bin(logical_bin)
            target_x_bk = bin_locs.get(physical_bin, fallback_x)
            self.emit('card_detected', {
                'recognized': False,
                'reason': 'card_back',
                'hash_distance': back_dist,
                'bin': physical_bin,
                'logical_bin': logical_bin,
            })
            self.emit('motion_path', [
                {'x': staging_x, 'z': z_clear,
                 'feedrate': 0, 'carrying': 'CARD BACK', 'pause': 0},
                {'x': target_x_bk, 'z': z_clear,
                 'feedrate': gcode_control.X_FEEDRATE, 'carrying': 'CARD BACK'},
                {'x': target_x_bk, 'z': drop_z,
                 'feedrate': gcode_control.Z_FEEDRATE, 'carrying': 'CARD BACK'},
                {'x': target_x_bk, 'z': drop_z,
                 'feedrate': 0, 'carrying': None,
                 'pause': gcode_control.PRESSURE_ON_MS},
                {'x': target_x_bk, 'z': z_clear,
                 'feedrate': gcode_control.Z_FEEDRATE, 'carrying': None},
            ])
            self.emit('card_picked_up', {'name': 'CARD BACK'})
            gcode_control.quick_drop(target_x_bk)
            if self.tracker:
                self.tracker.record_scan(card_info=None, bin_num=physical_bin,
                                         hash_distance=back_dist)
            motion_tracker.set_carrying(None)
            motion_tracker.update_position(x=target_x_bk, z=z_clear)
            self.emit('card_dropped', {'name': 'CARD BACK', 'bin': physical_bin})
            self.emit('motion_update', motion_tracker.get_state())
            self._increment_bin_count(physical_bin)
            self.last_drop_x = target_x_bk
            sort_duration = time.time() - sort_start
            self.sort_times.append(sort_duration)
            if len(self.sort_times) > 50:
                self.sort_times = self.sort_times[-50:]
            self.scan_count += 1
            self._emit_session_stats()
            self.emit('motion_update', motion_tracker.get_state())
            if self.continuous_sorting and self.state == 'sorting':
                time.sleep(self.continuous_delay)
                from web_camera import camera as cam
                self.enqueue('detect_and_sort', camera=cam)
            return

        if card_info:
            card_name = card_info.get('Name', 'Unknown')
            # Unified routing: SortConfig for all modes (no more legacy
            # dispatch). get_bin() returns fallback for None/empty card_data,
            # which is the same behavior the old legacy helpers had for
            # malformed cards.
            if self.sort_config_obj is not None:
                logical_bin = self.sort_config_obj.get_bin(card_data)
            else:
                # Defensive: this shouldn't happen since start_session now
                # always produces a SortConfig, but guard against a race.
                self.log("WARNING: No sort_config_obj at routing time — "
                         "sending to bin 10")
                logical_bin = 10

            # Wishlist override
            if self.wishlist_bin is not None:
                try:
                    import collection_db
                    conn = collection_db.get_connection()
                    match = collection_db.check_wishlist_match(
                        conn, card_name,
                        set_code=card_info.get('Set', ''))
                    conn.close()
                    if match:
                        logical_bin = self.wishlist_bin
                        self.log(f"WISHLIST MATCH: {card_name} -> Wishlist bin {logical_bin}")
                        self.emit('wishlist_match', {
                            'name': card_name,
                            'bin': logical_bin,
                            'wishlist_item': match,
                        })
                except Exception as e:
                    self.log(f"Wishlist check error: {e}")

            # --- Priority bin (Phase 4.21) ---
            # Highest-precedence routing layer. If this card is in the
            # configured Moxfield wishlist AND a priority_bin is set,
            # it beats both override bins AND the regular sort config.
            # Runs *after* the legacy wishlist_bin block so it wins there
            # too.  Basic lands are already excluded at cache-fill time,
            # so the oracle_id set here never contains them.
            priority_hit = self._check_priority_match(card_data, card_info)
            if priority_hit is not None:
                logical_bin = self.priority_bin
                self.log(f"PRIORITY MATCH: {card_name} -> "
                         f"Priority bin {logical_bin}")
                self.emit('wishlist_match', priority_hit)

            # Overflow resolution
            physical_bin = self.resolve_bin(logical_bin)

            if physical_bin != logical_bin:
                self.log(f"Overflow: logical bin {logical_bin} -> "
                         f"physical bin {physical_bin}")

            self.log(f"Identified: {card_name} -> Bin {physical_bin} ({method})")

            self.last_card_info = card_info
            self.last_card_bin = physical_bin
            self.last_card_method = method
            self.undo_available = True

            if self.tracker:
                self.tracker.record_scan(
                    card_info=card_info, bin_num=physical_bin,
                    method=method, hash_distance=hash_distance,
                    card_data=card_data,
                    is_foil=id_result.get('is_foil', False),
                    foil_confidence=id_result.get('foil_confidence'),
                )

            self.emit('card_detected', {
                'recognized': True,
                'name': card_name,
                'set': card_info.get('Set', '?'),
                'colors': card_info.get('Colors', []),
                'types': card_info.get('Types', []),
                'cmc': card_info.get('CMC', 0),
                'price': card_info.get('Price', 'N/A'),
                'rarity': card_info.get('Rarity', '?'),
                'logical_bin': logical_bin,
                'bin': physical_bin,
                'method': method,
                'hash_distance': hash_distance,
                'is_foil': id_result.get('is_foil', False),
                'foil_confidence': id_result.get('foil_confidence'),
                'undo_available': True,
            })

            target_x = bin_locs.get(physical_bin, fallback_x)
            self.log(
                f"[sort] {card_name} → bin {physical_bin} "
                f"(logical={logical_bin}) X={target_x:.0f}mm"
            )

            self.emit('motion_path', [
                {'x': staging_x, 'z': z_clear,
                 'feedrate': 0, 'carrying': card_name, 'pause': 0},
                {'x': target_x, 'z': z_clear,
                 'feedrate': gcode_control.X_FEEDRATE, 'carrying': card_name},
                {'x': target_x, 'z': drop_z,
                 'feedrate': gcode_control.Z_FEEDRATE, 'carrying': card_name},
                {'x': target_x, 'z': drop_z,
                 'feedrate': 0, 'carrying': None,
                 'pause': gcode_control.PRESSURE_ON_MS},
                {'x': target_x, 'z': z_clear,
                 'feedrate': gcode_control.Z_FEEDRATE, 'carrying': None},
                {'x': source_x, 'z': z_clear,
                 'feedrate': gcode_control.X_FEEDRATE, 'carrying': None},
            ])

            self.emit('card_picked_up', {'name': card_name})
            gcode_control.quick_drop(target_x)

        else:
            # Unrecognized card — on suction head from staging pickup
            logical_bin = 10
            physical_bin = self.resolve_bin(logical_bin)

            card_name = 'UNKNOWN'
            self.log(f"Card unrecognized -> Bin {physical_bin}")
            self.undo_available = False
            if self.tracker:
                self.tracker.record_scan(card_info=None, bin_num=physical_bin,
                                         hash_distance=hash_distance)

            self.emit('card_detected', {
                'recognized': False,
                'reason': 'unrecognized',
                'logical_bin': logical_bin,
                'bin': physical_bin,
                'hash_distance': hash_distance,
            })

            target_x = bin_locs.get(physical_bin, fallback_x)

            self.emit('motion_path', [
                {'x': staging_x, 'z': z_clear,
                 'feedrate': 0, 'carrying': 'UNKNOWN', 'pause': 0},
                {'x': target_x, 'z': z_clear,
                 'feedrate': gcode_control.X_FEEDRATE, 'carrying': 'UNKNOWN'},
                {'x': target_x, 'z': drop_z,
                 'feedrate': gcode_control.Z_FEEDRATE, 'carrying': 'UNKNOWN'},
                {'x': target_x, 'z': drop_z,
                 'feedrate': 0, 'carrying': None,
                 'pause': gcode_control.PRESSURE_ON_MS},
                {'x': target_x, 'z': z_clear,
                 'feedrate': gcode_control.Z_FEEDRATE, 'carrying': None},
                {'x': source_x, 'z': z_clear,
                 'feedrate': gcode_control.X_FEEDRATE, 'carrying': None},
            ])

            self.emit('card_picked_up', {'name': 'UNKNOWN'})
            gcode_control.quick_drop(target_x)

        # Final position
        motion_tracker.set_carrying(None)
        motion_tracker.update_position(
            x=bin_locs.get(physical_bin, fallback_x),
            z=z_clear
        )
        self.emit('card_dropped', {'name': card_name, 'bin': physical_bin})
        self.emit('motion_update', motion_tracker.get_state())

        self._increment_bin_count(physical_bin)
        self.last_drop_x = bin_locs.get(physical_bin, staging_x)

        # Track sort timing
        sort_duration = time.time() - sort_start
        self.sort_times.append(sort_duration)
        if len(self.sort_times) > 50:
            self.sort_times = self.sort_times[-50:]
        self.scan_count += 1

        self._emit_session_stats()
        self.emit('motion_update', motion_tracker.get_state())

        # --- Continuous sort: queue next card ---
        if self.continuous_sorting and self.state == 'sorting':
            time.sleep(self.continuous_delay)
            from web_camera import camera as cam
            self.enqueue('detect_and_sort', camera=cam)

    # --- Continuous Sort Commands ---

    def _cmd_start_continuous(self, camera=None, delay=0.5, **kwargs):
        """Start continuous sort mode — auto-detect loop."""
        if self.state != 'sorting':
            self.log("Cannot start continuous sort — start a session first")
            return
        if self.continuous_sorting:
            # Already running — ignore duplicate start requests (e.g. from
            # rapid button clicks before the UI receives the confirmation).
            self.log("Continuous sort already running — ignoring duplicate start")
            return
        self.continuous_sorting = True
        self.continuous_delay = float(delay)
        self.log(f"Continuous sort started (delay={self.continuous_delay}s)")
        self.emit('continuous_sort_started', {'delay': self.continuous_delay})
        # Trigger first detection
        self.enqueue('detect_and_sort', camera=camera)

    def _cmd_stop_continuous(self, **kwargs):
        """Stop continuous sort mode (session stays active)."""
        self.continuous_sorting = False
        self.log("Continuous sort stopped")
        self.emit('continuous_sort_stopped', {'reason': 'user'})

    # --- Mid-Sort Undo ---

    def _cmd_undo_last_sort(self, camera=None, **kwargs):
        """
        Undo the last card sort: move the card from its destination bin
        back to the source bin. Only works for the most recent card.
        """
        if not self.undo_available:
            self.log("No sort to undo")
            return
        if self.last_card_bin is None:
            self.log("No last card info available")
            return

        import gcode_control
        was_continuous = self.continuous_sorting
        self.continuous_sorting = False  # Pause continuous during undo

        last_bin = self.last_card_bin
        last_name = self.last_card_info.get('Name', '?') if self.last_card_info else '?'
        self.log(f"UNDO: Retrieving '{last_name}' from bin {last_bin}")

        # Pick from the destination bin (where we just dropped it)
        bin_locs = gcode_control.get_bin_locations()
        dest_x = bin_locs.get(last_bin, 0)
        source_x = bin_locs.get(0, gcode_control.X_SOURCE_BIN)

        # Pick from destination bin
        gcode_control.pick_from_position(dest_x)
        # Drop back in source
        gcode_control.drop_on_surface(source_x)

        # Reverse the tracker/inventory
        if self.tracker and self.tracker.scans:
            self.tracker.scans.pop()
            self.tracker.scan_count -= 1
            # Remove from bin tracking
            bin_key = str(last_bin)
            if bin_key in self.tracker.bins and self.tracker.bins[bin_key]:
                self.tracker.bins[bin_key].pop()
            # Reverse inventory in DB
            try:
                import collection_db
                conn = collection_db.get_connection()
                # Find and decrement
                existing = conn.execute(
                    "SELECT id, quantity FROM inventory "
                    "WHERE name=? AND set_code=?",
                    (last_name,
                     self.last_card_info.get('Set', '') if self.last_card_info else '')
                ).fetchone()
                if existing:
                    if existing['quantity'] <= 1:
                        conn.execute("DELETE FROM inventory WHERE id=?",
                                     (existing['id'],))
                    else:
                        conn.execute("UPDATE inventory SET quantity = quantity - 1 "
                                     "WHERE id=?", (existing['id'],))
                    conn.commit()
                conn.close()
            except Exception as e:
                self.log(f"Undo DB error: {e}")

        # Decrement physical bin count (may un-mark it as full)
        self._decrement_bin_count(last_bin)

        self.undo_available = False
        self.scan_count = max(0, self.scan_count - 1)
        self.log(f"UNDO complete: '{last_name}' returned to source")
        self.emit('sort_undone', {
            'name': last_name,
            'from_bin': last_bin,
        })
        self._emit_session_stats()

        # Resume continuous if it was running
        if was_continuous:
            self.continuous_sorting = True
            from web_camera import camera as cam
            self.enqueue('detect_and_sort', camera=cam)

    # --- Image Capture ---

    def _save_scan_image(self, frame):
        """Save the raw camera frame for this scan."""
        if frame is None or self.tracker is None:
            return
        try:
            if self._scan_images_dir is None:
                self._scan_images_dir = os.path.join(
                    self.tracker.session_dir, "scan_images")
                os.makedirs(self._scan_images_dir, exist_ok=True)
            filename = f"scan_{self.scan_count + 1:04d}.jpg"
            filepath = os.path.join(self._scan_images_dir, filename)
            cv2.imwrite(filepath, frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        except Exception as e:
            pass  # Don't fail sorting over image save errors

    def _save_no_detect_image(self, frame):
        """Save the camera frame when no card contour was detected.

        Stored in a no_detect/ subdirectory so the user can review what
        the camera saw when detection failed.
        """
        if frame is None or self.tracker is None:
            return
        try:
            nd_dir = os.path.join(self.tracker.session_dir, "no_detect")
            os.makedirs(nd_dir, exist_ok=True)
            filename = (f"nodet_{self.scan_count + 1:04d}"
                        f"_attempt{self._no_detect_retries}.jpg")
            filepath = os.path.join(nd_dir, filename)
            cv2.imwrite(filepath, frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        except Exception:
            pass  # Don't fail sorting over image save errors

    def _save_card_crop(self, card_img):
        """Save the perspective-corrected card image for dataset building."""
        if card_img is None or self.tracker is None:
            return
        try:
            crop_dir = os.path.join(self.tracker.session_dir, "card_crops")
            os.makedirs(crop_dir, exist_ok=True)
            filename = f"card_{self.scan_count + 1:04d}.jpg"
            filepath = os.path.join(crop_dir, filename)
            cv2.imwrite(filepath, card_img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        except Exception:
            pass

    def _save_hash_diagnostics(self, card_img, top_candidates, card_layout):
        """
        Save per-channel hash distance diagnostics for a scan.

        DEPRECATED: This used the old v2 hashing module (CLAHE, 256-bit,
        layout-specific regions). The new v3 system in card_identify.py
        does not support these diagnostics. This method now no-ops.
        """
        # v3 detection system does not use the old hash diagnostic format.
        return
        if card_img is None or self.tracker is None:
            return
        try:
            from hashing import (crop_art_region, _hash_art_for_search,
                                 HASH_DB, PRECOMPUTED_HASHES_BY_LAYOUT)
            import imagehash
            from cards import CARD_DATA_BY_ID

            # Hash diagnostics require v2 DB format with p_r/p_g/p_b keys
            sample = next(iter(HASH_DB.values()), {}) if HASH_DB else {}
            if 'p_r' not in sample:
                return

            diag_dir = os.path.join(self.tracker.session_dir, "hash_diagnostics")
            os.makedirs(diag_dir, exist_ok=True)

            # Compute query hashes for the winning layout
            art_pil = crop_art_region(card_img, layout=card_layout)
            query = _hash_art_for_search(art_pil, hash_size=16)
            r_ph, g_ph, b_ph, r_dh, g_dh, b_dh = query

            # Build diagnostics for top 5 candidates
            candidates_diag = []
            for cid, combined_dist in top_candidates[:5]:
                entry = HASH_DB.get(cid)
                if entry is None:
                    continue
                try:
                    t_pr = imagehash.hex_to_hash(entry['p_r'])
                    t_pg = imagehash.hex_to_hash(entry['p_g'])
                    t_pb = imagehash.hex_to_hash(entry['p_b'])
                    t_dr = imagehash.hex_to_hash(entry['d_r'])
                    t_dg = imagehash.hex_to_hash(entry['d_g'])
                    t_db = imagehash.hex_to_hash(entry['d_b'])
                except (KeyError, ValueError):
                    continue

                cdata = CARD_DATA_BY_ID.get(cid, {})
                candidates_diag.append({
                    'card_id': cid,
                    'name': cdata.get('name', '?'),
                    'set': cdata.get('set', '?'),
                    'combined_distance': round(combined_dist, 2),
                    'db_layout': entry.get('layout', 'normal'),
                    'phash': {
                        'r': int(r_ph - t_pr),
                        'g': int(g_ph - t_pg),
                        'b': int(b_ph - t_pb),
                    },
                    'dhash': {
                        'r': int(r_dh - t_dr),
                        'g': int(g_dh - t_dg),
                        'b': int(b_dh - t_db),
                    },
                })

            diag = {
                'scan_num': self.scan_count + 1,
                'layout': card_layout,
                'candidates': candidates_diag,
            }

            filename = f"diag_{self.scan_count + 1:04d}.json"
            filepath = os.path.join(diag_dir, filename)
            with open(filepath, 'w', encoding='utf-8') as f:
                import json
                json.dump(diag, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.log(f"[diagnostics] save error: {e}")

    # --- Last setup persistence ---

    # Auto-save lives next to the Scripts dir as _last_setup.json and is
    # overwritten every time new hardware setup completes. It holds the
    # calibrated bin X locations, staging, camera offset, and probe
    # heights so a server restart can pick up exactly where the user
    # left off without re-running calibration.
    @property
    def _last_setup_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            '_last_setup.json')

    def _save_last_setup(self, setup):
        """Serialize the most recent hardware-setup result to disk."""
        import json
        path = self._last_setup_path
        payload = {
            'version': 1,
            'saved_at': time.time(),
            'locations': setup.get('locations') or {},
            'staging': setup.get('staging'),
            'source_bins': setup.get('source_bins') or [],
            'probe_results': {
                str(k): v for k, v in (setup.get('probe_results') or {}).items()
            },
            'camera_x_offset': setup.get('camera_x_offset'),
            'dest_bin_count': setup.get('dest_bin_count'),
            'source_bin_count': setup.get('source_bin_count'),
        }
        with open(path, 'w') as f:
            json.dump(payload, f, indent=2)

    def load_last_setup(self, path=None):
        """Apply a saved setup at server startup, or on demand.

        If `path` is None, loads the auto-save (_last_setup.json).
        Otherwise loads the given absolute path, which is used by the
        named-setup load endpoint to restore a `setup_<name>.json`
        file the user saved from the UI.

        Returns a short summary dict or None if nothing was loaded.
        Runs in whatever thread calls it so it stays simple and
        synchronous.
        """
        import json
        if path is None:
            path = self._last_setup_path
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r') as f:
                payload = json.load(f)
        except Exception as e:
            print(f"[worker] Failed to read last setup: {e}")
            return None

        import gcode_control

        locations = payload.get('locations') or {}
        if locations:
            gcode_control._active_bin_locations = {
                int(k): float(v) for k, v in locations.items()
            }

        staging = payload.get('staging')
        camera_x_offset = payload.get('camera_x_offset')
        if staging:
            try:
                gcode_control.set_machine_positions(
                    staging_x=float(staging['x']),
                    camera_x_offset=(float(camera_x_offset)
                                     if camera_x_offset is not None
                                     else None))
            except Exception as e:
                print(f"[worker] staging restore error: {e}")

        source_bins = payload.get('source_bins') or []
        if source_bins:
            primary = next((s for s in source_bins
                            if s.get('bin_number') == 0), None)
            if primary:
                try:
                    gcode_control.set_machine_positions(
                        source_x=float(primary['x']))
                except Exception as e:
                    print(f"[worker] source restore error: {e}")
            self.source_bins = source_bins

        # Restore probe cache so the first sort doesn't have to re-probe
        # every bin. These heights will naturally refresh as cards stack
        # up, but having the starting point avoids the slow discovery
        # pass.
        probe_results = payload.get('probe_results') or {}
        if probe_results and locations:
            try:
                for bin_key, z in probe_results.items():
                    if bin_key == 'staging':
                        x = gcode_control.X_STAGING_POSITION
                    else:
                        x = locations.get(str(bin_key))
                    if x is None:
                        continue
                    gcode_control._probe_z_cache[float(x)] = float(z)
            except Exception as e:
                print(f"[worker] probe cache restore error: {e}")

        summary = {
            'locations': locations,
            'staging': staging,
            'source_bins': source_bins,
            'dest_bin_count': payload.get('dest_bin_count'),
            'source_bin_count': payload.get('source_bin_count'),
            'saved_at': payload.get('saved_at'),
            'name': payload.get('name'),
        }
        print(f"[worker] Restored setup "
              f"{('(' + summary['name'] + ') ') if summary.get('name') else ''}"
              f"from {os.path.basename(path)}: "
              f"{summary['dest_bin_count']} dest, "
              f"{summary['source_bin_count']} source")
        return summary

    def _cleanup_old_scan_images(self):
        """Remove scan images from sessions older than the last 2."""
        try:
            from config import SCAN_LOGS_DIR
            if not os.path.exists(SCAN_LOGS_DIR):
                return
            sessions = sorted(os.listdir(SCAN_LOGS_DIR), reverse=True)
            # Keep last 2 sessions' images, delete older ones
            for session_name in sessions[2:]:
                img_dir = os.path.join(SCAN_LOGS_DIR, session_name, "scan_images")
                if os.path.isdir(img_dir):
                    import shutil
                    shutil.rmtree(img_dir, ignore_errors=True)
        except Exception:
            pass

    # --- Wishlist Bin ---

    def _cmd_set_wishlist_bin(self, bin_number=None, **kwargs):
        """Set which bin to use for wishlist matches (None to disable)."""
        if bin_number is not None:
            self.wishlist_bin = int(bin_number)
            self.log(f"Wishlist bin set to {self.wishlist_bin}")
        else:
            self.wishlist_bin = None
            self.log("Wishlist bin disabled")

    # --- Priority bin (Phase 4.21) ---

    def _cmd_set_priority_bin(self, bin_number=None, wishlist_source=None,
                              **kwargs):
        """
        Configure the highest-precedence priority-bin route.

        bin_number:      physical bin number, or None to disable.
        wishlist_source: "moxfield:<username>" identifier of a cached
                         Moxfield wishlist, or None to disable.

        Both must be set for the priority route to fire.  Pre-loads the
        set of oracle_ids from collection_db so per-card routing is O(1).
        """
        self.priority_bin = int(bin_number) if bin_number is not None else None
        self.priority_wishlist_source = wishlist_source or None
        self.priority_oracle_ids = set()

        if self.priority_bin is not None and self.priority_wishlist_source:
            try:
                import collection_db
                conn = collection_db.get_connection()
                self.priority_oracle_ids = \
                    collection_db.get_moxfield_wishlist_oracle_ids(
                        conn, self.priority_wishlist_source)
                conn.close()
                self.log(f"Priority bin = {self.priority_bin} "
                         f"(wishlist='{self.priority_wishlist_source}', "
                         f"{len(self.priority_oracle_ids)} cards)")
            except Exception as e:
                self.log(f"Priority bin load error: {e}")
                self.priority_oracle_ids = set()
        else:
            self.log("Priority bin disabled")

    def _check_priority_match(self, card_data, card_info):
        """
        Return a dict payload for `wishlist_match` if the scanned card is
        in the configured priority wishlist, else None.

        Payload shape:
            {oracle_id, name, image_uri, set, priority_bin}

        Safe to call with priority_bin / wishlist_source unset — returns
        None in that case.
        """
        if self.priority_bin is None:
            return None
        if not self.priority_oracle_ids:
            return None

        oracle_id = None
        if card_data:
            oracle_id = card_data.get('oracle_id')
        if not oracle_id:
            return None
        if oracle_id not in self.priority_oracle_ids:
            return None

        # Resolve image / set / name — prefer the cached wishlist entry
        # so alerts show the user's intended art, fall back to the scan.
        name = (card_info or {}).get('Name') if card_info else None
        set_code = (card_info or {}).get('Set') if card_info else None
        image_uri = None
        try:
            import collection_db
            conn = collection_db.get_connection()
            cached = collection_db.get_moxfield_wishlist_card(
                conn, self.priority_wishlist_source, oracle_id)
            conn.close()
            if cached:
                name = cached.get('name') or name
                set_code = cached.get('set_code') or set_code
                image_uri = cached.get('image_uri')
        except Exception as e:
            self.log(f"Priority cache lookup error: {e}")

        if not image_uri and card_data:
            # Fallback: Scryfall image URIs from enriched card_data
            imgs = card_data.get('image_uris') or {}
            image_uri = imgs.get('normal') or imgs.get('small')

        return {
            'oracle_id': oracle_id,
            'name': name or 'Unknown',
            'image_uri': image_uri,
            'set': set_code,
            'priority_bin': self.priority_bin,
        }

    # --- Bin Overflow / Fullness ---

    def _get_fallback_x(self):
        """
        Get the X position for the fallback/unrecognized bin.

        Uses the LAST configured destination bin (highest numbered).
        Falls back to the source bin if no destination bins are configured.
        This prevents cards from being dropped on the staging platform
        (which happens when using a non-existent bin number with a
        hardcoded default like 150.0mm).
        """
        import gcode_control
        bin_locs = gcode_control.get_bin_locations()
        # Get all destination bins (exclude source bin 0)
        dest_bins = {k: v for k, v in bin_locs.items() if k > 0}
        if dest_bins:
            last_bin = max(dest_bins.keys())
            return dest_bins[last_bin]
        # No destination bins configured — use source bin
        return bin_locs.get(0, gcode_control.X_SOURCE_BIN)

    def resolve_bin(self, logical_bin):
        """
        Resolve a logical bin to a physical bin using the overflow chain.

        Returns the first non-full physical bin in the chain.  If every bin
        in the chain is full, returns the *last* bin in the chain anyway
        (the card still needs somewhere to go — the UI will warn the user
        to empty that bin).  Never returns None.
        """
        chain = self.overflow_map.get(logical_bin, [logical_bin])
        for physical_bin in chain:
            if physical_bin not in self.bins_full:
                return physical_bin
        # All bins full — drop in the last bin of the chain and warn
        last_bin = chain[-1] if chain else logical_bin
        return last_bin

    def _increment_bin_count(self, physical_bin):
        """
        Increment the card count for a physical bin after a successful drop.
        Marks the bin as full and emits an event if the limit is reached.
        """
        count = self.bin_card_counts.get(physical_bin, 0) + 1
        self.bin_card_counts[physical_bin] = count
        if count >= self.bin_card_limit and physical_bin not in self.bins_full:
            self.bins_full.add(physical_bin)
            self.log(f"BIN {physical_bin} IS FULL ({count}/{self.bin_card_limit} cards)")
            self.emit('bin_full', {
                'bin': physical_bin,
                'count': count,
                'limit': self.bin_card_limit,
            })

    def _decrement_bin_count(self, physical_bin):
        """Decrement bin count (used by undo). Un-marks full if below limit."""
        count = self.bin_card_counts.get(physical_bin, 0)
        if count > 0:
            count -= 1
            self.bin_card_counts[physical_bin] = count
        if physical_bin in self.bins_full and count < self.bin_card_limit:
            self.bins_full.discard(physical_bin)

    def get_bin_fullness_status(self):
        """Return overflow and fullness status for API/UI."""
        return {
            'overflow_map': {str(k): v for k, v in self.overflow_map.items()},
            'bin_card_counts': {str(k): v for k, v in self.bin_card_counts.items()},
            'bins_full': sorted(self.bins_full),
            'bin_card_limit': self.bin_card_limit,
        }

    def _cmd_set_overflow_map(self, overflow_map=None, **kwargs):
        """Set the overflow chain mapping. {logical_bin: [physical_bins]}
        Each chain is auto-sorted so the closest overflow bin to the
        primary comes first, minimizing carriage travel on spill-over."""
        import gcode_control
        if overflow_map is None:
            self.overflow_map = {}
            self.log("Overflow map cleared")
        else:
            bin_locs = gcode_control.get_bin_locations() or {}
            raw_map = {int(k): [int(b) for b in v]
                       for k, v in overflow_map.items()}
            self.overflow_map = {}
            for primary, chain in raw_map.items():
                primary_x = bin_locs.get(primary, 0.0)
                if len(chain) > 1:
                    head = chain[0]
                    rest = chain[1:]
                    rest.sort(key=lambda b: abs(
                        bin_locs.get(b, 0.0) - primary_x))
                    self.overflow_map[primary] = [head] + rest
                else:
                    self.overflow_map[primary] = chain
            chains = len(self.overflow_map)
            total_overflow = sum(len(v) - 1 for v in self.overflow_map.values()
                                 if len(v) > 1)
            self.log(f"Overflow map set: {chains} chains, "
                     f"{total_overflow} overflow bin(s)")
        self.emit('overflow_map_updated', self.get_bin_fullness_status())

    def _cmd_set_bin_card_limit(self, limit=150, **kwargs):
        """Set the max cards per physical bin."""
        self.bin_card_limit = max(1, int(limit))
        # Recalculate which bins are full under new limit
        self.bins_full = {b for b, c in self.bin_card_counts.items()
                          if c >= self.bin_card_limit}
        self.log(f"Bin card limit set to {self.bin_card_limit}")
        self.emit('overflow_map_updated', self.get_bin_fullness_status())

    def _cmd_mark_bin_empty(self, bin_number=None, **kwargs):
        """Mark a physical bin as emptied — resets count and full status."""
        if bin_number is None:
            return
        bin_number = int(bin_number)
        old_count = self.bin_card_counts.get(bin_number, 0)
        self.bin_card_counts[bin_number] = 0
        self.bins_full.discard(bin_number)
        self.log(f"Bin {bin_number} marked empty (was {old_count} cards)")
        self.emit('bin_emptied', {
            'bin': bin_number,
            'old_count': old_count,
        })
        self.emit('overflow_map_updated', self.get_bin_fullness_status())

    def _on_bin_fullness(self, bin_number, probed_z, drop_z):
        """Callback from gcode_control mini-probe when a bin is physically near full."""
        remaining_mm = drop_z - probed_z
        self.log(f"PROBE WARNING: Bin {bin_number} physically near full! "
                 f"Only {remaining_mm:.1f}mm clearance remaining.")
        self.emit('bin_fullness_warning', {
            'bin': bin_number,
            'probed_z': probed_z,
            'drop_z': drop_z,
            'remaining_mm': remaining_mm,
        })
        # Also force-mark as full if not already (probe overrides count)
        if bin_number not in self.bins_full:
            self.bins_full.add(bin_number)
            count = self.bin_card_counts.get(bin_number, 0)
            self.emit('bin_full', {
                'bin': bin_number,
                'count': count,
                'limit': self.bin_card_limit,
                'reason': 'probe',
            })

    def _cmd_pause(self, **kwargs):
        """Pause the sorting session."""
        if self.state == 'sorting':
            self.state = 'paused'
            self.log("Session paused — add more cards and resume when ready")

    def _cmd_resume(self, **kwargs):
        """Resume the sorting session."""
        if self.state == 'paused':
            self.state = 'sorting'
            self.log("Session resumed")

    def _cmd_stop_session(self, **kwargs):
        """Stop the current sorting session.

        Uses try/finally so state=idle and continuous_sorting=False are
        ALWAYS applied even if the tracker cleanup throws an exception.
        Without this, a DB error during end_session() would leave the
        worker stuck in 'sorting' state with no way to recover except a
        server restart.
        """
        # Stop the continuous sort loop immediately. This prevents the
        # re-enqueue check at the end of detect_and_sort from extending
        # the loop after the session ends. Must be set before state='idle'
        # so that any in-flight detect_and_sort that checks
        # (continuous_sorting and state=='sorting') at its re-enqueue
        # point will NOT re-enqueue another cycle.
        self.continuous_sorting = False

        try:
            if self.tracker:
                try:
                    self.tracker.print_status()
                except Exception as e:
                    self.log(f"Warning: could not print session status: {e}")
                try:
                    self.tracker.end_session()
                    stats = {
                        'total_scans': self.tracker.scan_count,
                        'recognized': (self.tracker.scan_count
                                       - self.tracker.unrecognized_count),
                        'unrecognized': self.tracker.unrecognized_count,
                        'bins': self.tracker.get_all_bins(),
                    }
                    self.emit('session_ended', stats)
                except Exception as e:
                    self.log(f"Warning: error ending tracker session: {e}")
                    # Still notify the UI — it needs to reset its buttons
                    # even if the tracker teardown failed.
                    self.emit('session_ended', {
                        'total_scans': getattr(self.tracker, 'scan_count', 0),
                        'recognized': 0, 'unrecognized': 0, 'bins': {},
                    })
                finally:
                    self.tracker = None
        finally:
            self.sort_mode = None
            self.sort_config_obj = None
            self.state = 'idle'

        # Unlock camera focus so autofocus can re-settle for the next
        # session's ROI capture (the platform distance may differ if
        # the user adjusts the setup between sessions).
        try:
            from web_camera import camera
            camera.unlock_focus()
        except Exception as e:
            self.log(f"Warning: could not unlock camera focus: {e}")

        self.log("Session ended")

    def _cmd_reset_after_estop(self, **kwargs):
        """
        Reset after emergency stop — clear Marlin's halt, re-home, and
        either resume the pre-estop session in 'paused' state (so the
        user can press Resume / Detect) or fall back to 'idle' if no
        session was active.

        Key things this MUST do, or the UI will look dead afterward:
          1. Clear self._abort_requested. emergency_stop() sets it so
             any mid-flight detect_and_sort bails cleanly. If we don't
             clear it, the next detect_and_sort will early-out at its
             first _abort_check().
          2. Send M999 to clear the Marlin halt (M112 latches the
             firmware until M999).
          3. Re-send M211 S0 because M999 can restore firmware
             defaults and re-enable software endstops — we need them
             off so our own X_SAFE_MAX is authoritative.
          4. Re-home (carriage position is unknown after a halt).
          5. Flush any leftover motion commands that were on the
             serial line but never acked.
          6. Transition state based on _pre_estop_state so the correct
             UI buttons light up.
        """
        import gcode_control
        self.log("Resetting after E-stop...")

        # --- 1. Clear the abort flag from emergency_stop() ---
        # Without this, the next detect_and_sort bails at its first
        # abort check and the session looks frozen.
        self._clear_abort()

        # --- 2. M999 (clear halt) + 3. M211 S0 (keep endstops off) ---
        if gcode_control.is_connected() and gcode_control.ser:
            try:
                # Drain anything the firmware might still be saying from
                # before the halt so we don't confuse our own parser.
                try:
                    gcode_control.ser.reset_input_buffer()
                except Exception:
                    pass

                gcode_control.ser.write(b'M999\n')
                gcode_control.ser.flush()
                time.sleep(1.0)

                # Drain M999's ok response.
                try:
                    while True:
                        line = gcode_control.ser.readline()
                        if not line:
                            break
                        decoded = line.decode('utf-8', errors='replace').strip()
                        if decoded:
                            print(f"[worker] << {decoded}")
                except Exception:
                    pass

                # M999 can restore firmware defaults for some runtime
                # settings, so re-assert software endstops OFF. Our
                # X_SAFE_MAX is the authoritative X bound; without
                # this, moves past firmware X_MAX_POS would silently
                # clip to 800mm again.
                gcode_control.ser.write(b'M211 S0\n')
                gcode_control.ser.flush()
                time.sleep(0.2)
                try:
                    for _ in range(5):
                        line = gcode_control.ser.readline()
                        if not line:
                            break
                        decoded = line.decode('utf-8', errors='replace').strip()
                        if decoded:
                            print(f"[worker] << {decoded}")
                except Exception:
                    pass
            except Exception as e:
                self.log(f"M999/M211 error: {e}")

        # --- 4. Re-home. This also updates motion_tracker. ---
        try:
            self._cmd_home()
        except Exception as e:
            self.log(f"Re-home failed: {e}")
            # Even if homing failed we still want to clear the state
            # so the user can manually recover instead of being stuck.

        # --- 5. Reset sort-cycle bookkeeping that may be stale ---
        self._cards_since_rehome = 0
        # Whatever was on the suction head was dropped when M112 killed
        # the pumps. Forget about it — next detect will pick a fresh card.
        self.last_card_info = None
        self.last_card_bin = None
        self.last_card_method = None
        self.undo_available = False

        # --- 6. Decide resume target ---
        pre = self._pre_estop_state or {}
        had_session = pre.get('had_session', False) and self.tracker is not None

        if had_session:
            # Session survived (tracker was NOT ended during e-stop).
            # Land in 'paused' so the user must explicitly Resume to
            # continue sorting. This mirrors normal pause/resume UX.
            self.state = 'paused'
            self.log("Reset complete — session preserved, in paused state. "
                     "Press Resume or Detect to continue sorting.")
            self.emit('session_paused', {
                'reason': 'emergency_stop_recovered',
                'resumable': True,
            })
            self.emit('estop_reset_complete', {
                'resumed': True,
                'state': 'paused',
            })
        else:
            # No active session — straight to idle so Start Session
            # button becomes usable again.
            self.state = 'idle'
            self.log("Reset complete — machine re-homed, ready.")
            self.emit('estop_reset_complete', {
                'resumed': False,
                'state': 'idle',
            })

        # Clear the snapshot so a subsequent e-stop starts fresh.
        self._pre_estop_state = None

    # --- ArUco Calibration Commands ---

    def _cmd_run_calibration(self, camera=None, expected_sources=1,
                              expected_dests=10, expect_staging=True,
                              max_sweep_x=None, **kwargs):
        """Run automatic ArUco bin calibration sweep."""
        import gcode_control

        if not gcode_control.is_connected():
            self.log("Cannot calibrate — machine not connected")
            self.emit('error', {'message': 'Machine not connected'})
            return
        if self.state not in ('idle',):
            self.log(f"Cannot calibrate — must be idle (currently: {self.state})")
            return
        if camera is None:
            self.log("Camera required for calibration")
            return

        from web_calibration import calibrator

        # Clear abort flag so a stale cancel from a previous sweep doesn't
        # immediately kill this one, and guarantee state returns to idle
        # via the try/finally below.
        self._clear_abort()

        self.log(f"Starting calibration: {expected_sources} sources, "
                 f"{expected_dests} destinations"
                 + (", staging platform" if expect_staging else ""))

        try:
            result = calibrator.run_sweep_calibration(
                camera=camera,
                gcode_module=gcode_control,
                expected_sources=int(expected_sources),
                expected_dests=int(expected_dests),
                expect_staging=bool(expect_staging),
                max_sweep_x=float(max_sweep_x) if max_sweep_x else None,
            )
        finally:
            # Same guarantee as _cmd_new_hardware_setup: after any
            # calibration attempt (success, failure, or cancel) the
            # worker must be runnable again so the user can retry.
            if self.state not in ('disconnected', 'estopped'):
                self.state = 'idle'
            self._clear_abort()

        if result and 'error' not in result:
            # Apply the discovered bin configuration to gcode_control
            locations = result.get('locations', {})
            if locations:
                gcode_control._active_bin_locations = {
                    int(k): v for k, v in locations.items()
                }
                self.log(f"Applied {len(locations)} bin positions from calibration")

            # Apply staging platform position if detected
            staging = result.get('staging')
            if staging:
                gcode_control.set_machine_positions(staging_x=staging['x'])
                self.log(f"Staging platform: X={staging['x']}mm (marker {staging['marker_id']})")

            # Apply source bin 0 as source_x
            source_bins_result = result.get('source_bins', [])
            if source_bins_result:
                primary_source = next((s for s in source_bins_result if s['bin_number'] == 0), None)
                if primary_source:
                    gcode_control.set_machine_positions(source_x=primary_source['x'])
                    self.log(f"Source bin: X={primary_source['x']}mm")

            # Store source bins for dual-source optimization
            self.source_bins = source_bins_result
            self.source_empty = {}
            if len(self.source_bins) > 1:
                self.log(f"Multi-source mode: {len(self.source_bins)} source bins detected")
                for s in self.source_bins:
                    self.log(f"  Source bin {s['bin_number']}: X={s['x']}mm (marker {s['marker_id']})")

            self.emit('bins_configured', {
                'count': len(result.get('dest_bins', [])),
                'source_count': len(self.source_bins),
                'staging': staging,
                'locations': locations,
            })
        else:
            self.log(f"Calibration failed: {result.get('error', 'unknown')}")

    def _cmd_cancel_calibration(self, **kwargs):
        """Cancel a running calibration."""
        from web_calibration import calibrator
        calibrator.cancel()
        self.log("Calibration cancelled")

    def _cmd_new_hardware_setup(self, camera=None, expected_sources=1,
                                 expected_dests=10, expect_staging=True,
                                 max_sweep_x=None, **kwargs):
        """
        One-shot 'fresh install' routine for when bins/hardware change.

        Clears ALL cached state from the previous hardware layout, homes,
        runs the ArUco sweep, then probes every discovered bin.

        Steps:
          1. Clear Z-probe cache, source-empty state, bin counts, bin locations.
          2. Home all axes.
          3. Run ArUco calibration sweep (discovers bin X positions + staging).
          4. Probe Z at every discovered destination bin.
          5. Emit a completion event with the final layout.

        Cancellation:
          - `calibrator.cancel()` stops the sweep mid-run (called directly
            from /api/calibration/cancel, bypasses the worker queue).
          - `self._abort_requested` causes this method to bail cleanly
            between phases (also set by /api/calibration/cancel).
          - Either way, state is guaranteed to return to 'idle' via the
            try/finally block, so the user can re-run the setup
            immediately without a restart.
        """
        import gcode_control
        from web_calibration import calibrator

        # Connection / state preconditions. These mirror the guard in
        # /api/calibration/new-hardware-setup but we re-check here so a
        # command queued before a disconnect doesn't run on a dead port.
        if not gcode_control.is_connected():
            self.log("Cannot run setup — machine not connected")
            self.emit('hardware_setup_complete',
                      {'success': False, 'error': 'not connected'})
            return
        if self.state not in ('idle',):
            self.log(f"Cannot run setup — must be idle (currently: {self.state})")
            self.emit('hardware_setup_complete',
                      {'success': False,
                       'error': f'machine not idle ({self.state})'})
            return
        if camera is None:
            self.log("Camera required for new hardware setup")
            self.emit('hardware_setup_complete',
                      {'success': False, 'error': 'camera required'})
            return

        # Reset abort flag so a stale cancel from a previous run doesn't
        # immediately kill this one.
        self._clear_abort()

        def aborted(reason=''):
            """Helper: emit abort event and return."""
            msg = f"Setup aborted{': ' + reason if reason else ''}"
            self.log(msg)
            self.emit('hardware_setup_complete',
                      {'success': False, 'error': 'aborted',
                       'reason': reason})

        try:
            self.log("=== NEW HARDWARE SETUP ===")
            self.emit('hardware_setup_progress',
                      {'step': 'clearing', 'message': 'Clearing cached state'})

            # --- 1. Clear every scrap of cached hardware state ---
            try:
                gcode_control.clear_probe_cache()
            except Exception as e:
                self.log(f"Warning: could not clear probe cache: {e}")
            gcode_control._active_bin_locations = None
            self.source_bins = []
            self.source_empty = {}
            self.last_drop_x = None
            self.bin_card_counts = {}
            self.bins_full = set()
            self.empty_bin_z = None
            self.last_source_z = None
            self._cards_since_rehome = 0
            self.log("Cleared probe cache, bin locations, fullness counts, "
                     "source-empty state")

            if self._abort_check():
                return aborted('during clear phase')

            # --- 2. Home ---
            self.emit('hardware_setup_progress',
                      {'step': 'homing', 'message': 'Homing axes'})
            try:
                self._cmd_home()
            except Exception as e:
                self.log(f"Homing failed: {e}")
                self.emit('hardware_setup_complete',
                          {'success': False, 'error': f'homing failed: {e}'})
                return

            if self._abort_check():
                return aborted('after homing')

            # --- 3. ArUco sweep ---
            self.emit('hardware_setup_progress',
                      {'step': 'calibrating',
                       'message': f'Running ArUco sweep '
                                  f'({expected_sources} source, '
                                  f'{expected_dests} dest'
                                  + (', staging' if expect_staging else '') + ')'})

            result = calibrator.run_sweep_calibration(
                camera=camera,
                gcode_module=gcode_control,
                expected_sources=int(expected_sources),
                expected_dests=int(expected_dests),
                expect_staging=bool(expect_staging),
                max_sweep_x=float(max_sweep_x) if max_sweep_x else None,
            )

            # Cancellation check. The calibrator returns
            # {'cancelled': True, 'error': 'cancelled'} if the sweep
            # was interrupted. We also honor our own abort flag for
            # the case where cancel was requested between phases.
            if self._abort_check() or (result and result.get('cancelled')):
                return aborted('during sweep')

            if not result or 'error' in result:
                err = result.get('error', 'unknown') if result else 'no result'
                self.log(f"Calibration failed: {err}")
                self.emit('hardware_setup_complete',
                          {'success': False, 'error': err})
                return

            # --- 3b. Apply discovered positions ---
            locations = result.get('locations', {})
            if locations:
                gcode_control._active_bin_locations = {
                    int(k): v for k, v in locations.items()
                }
                self.log(f"Applied {len(locations)} bin positions")

            staging = result.get('staging')
            if staging:
                # Pass the calibrator's current camera_x_offset alongside
                # staging_x so gcode_control can recompute X_CAMERA_POSITION
                # to the right carriage position for "camera over staging".
                # Without this update the carriage moves to the stale
                # default camera X and the sort loop sees an empty frame
                # — causing the "sort does nothing" symptom.
                gcode_control.set_machine_positions(
                    staging_x=staging['x'],
                    camera_x_offset=calibrator.camera_x_offset)
                self.log(f"Staging platform: X={staging['x']}mm "
                         f"(marker {staging['marker_id']}, "
                         f"camera_x_offset={calibrator.camera_x_offset})")

            source_bins_result = result.get('source_bins', [])
            if source_bins_result:
                primary_source = next(
                    (s for s in source_bins_result if s['bin_number'] == 0), None)
                if primary_source:
                    gcode_control.set_machine_positions(
                        source_x=primary_source['x'])
                    self.log(f"Source bin: X={primary_source['x']}mm")
            self.source_bins = source_bins_result

            self.emit('bins_configured', {
                'count': len(result.get('dest_bins', [])),
                'source_count': len(self.source_bins),
                'staging': staging,
                'locations': locations,
            })

            if self._abort_check():
                return aborted('before probe phase')

            # --- 4. Probe every discovered location ---
            # This probes the source bin (bin 0), every destination bin,
            # AND the staging platform — everything the machine actually
            # drops onto or picks up from. The previous version only
            # probed dest bins, which meant the first real sort run had
            # to re-probe source and staging in the middle of operation.
            bin_locs = gcode_control.get_bin_locations() or {}
            dest_bins = sorted(b for b in bin_locs.keys() if b > 0)
            has_source = 0 in bin_locs
            has_staging = staging is not None

            total_probes = len(dest_bins) + (1 if has_source else 0) + (
                1 if has_staging else 0)
            self.emit('hardware_setup_progress',
                      {'step': 'probing',
                       'message': (f'Probing {total_probes} locations '
                                   f'({len(dest_bins)} dest'
                                   + (', 1 source' if has_source else '')
                                   + (', 1 staging' if has_staging else '')
                                   + ')')})

            probe_results = {}
            from gcode_control import _probe_z_cache

            # Probe source bin first (bin 0) if present
            if has_source:
                if self._abort_check():
                    return aborted('before probing source bin')
                try:
                    self.log("Probing source bin (bin 0)...")
                    self._cmd_probe_bin(bin_number=0)
                    x = bin_locs.get(0)
                    if x is not None and x in _probe_z_cache:
                        probe_results[0] = _probe_z_cache[x]
                except Exception as e:
                    self.log(f"Probe failed for source bin: {e}")

            # Probe every destination bin
            for bin_num in dest_bins:
                if self._abort_check():
                    return aborted(
                        f'during probe phase (bin {bin_num} of {len(dest_bins)})')
                try:
                    self._cmd_probe_bin(bin_number=bin_num)
                    x = bin_locs.get(bin_num)
                    if x is not None and x in _probe_z_cache:
                        probe_results[bin_num] = _probe_z_cache[x]
                except Exception as e:
                    self.log(f"Probe failed for bin {bin_num}: {e}")

            # Probe the staging platform last. It's not a bin number so
            # _cmd_probe_bin doesn't cover it — probe the X position
            # directly via gcode_control's probe helper.
            if has_staging:
                if self._abort_check():
                    return aborted('before probing staging')
                try:
                    staging_x = float(staging['x'])
                    self.log(f"Probing staging platform at X={staging_x:.1f}mm...")
                    # Move Z up, move X over staging, probe down, move Z up
                    gcode_control.z_to_top()
                    gcode_control.wait_for_completion()
                    gcode_control._send_and_wait(
                        f"G0 X{staging_x} F{gcode_control.X_FEEDRATE}")
                    gcode_control.wait_for_completion()
                    gcode_control._probe_with_cache(staging_x)
                    z = gcode_control._get_current_z()
                    gcode_control.z_to_top()
                    gcode_control.wait_for_completion()
                    self.log(f"Staging probe height: Z={z}")
                    probe_results['staging'] = z
                    self.emit('probe_result', {
                        'bin': 'staging',
                        'x': staging_x,
                        'z': z,
                    })
                except Exception as e:
                    self.log(f"Probe failed for staging: {e}")

            # --- 5. Done ---
            self.log(f"=== SETUP COMPLETE: {len(dest_bins)} destination bins, "
                     f"{len(self.source_bins)} source bins"
                     + (f", staging at X={staging['x']:.0f}mm" if staging else "")
                     + " ===")

            # Auto-save the full calibration result to disk so it
            # survives a server restart. The user can also save it
            # under a named file via the calibration UI, but this
            # "last setup" file is always overwritten and auto-loaded
            # on server startup.
            try:
                self._save_last_setup({
                    'locations': locations,
                    'staging': staging,
                    'source_bins': self.source_bins,
                    'probe_results': probe_results,
                    'camera_x_offset': calibrator.camera_x_offset,
                    'dest_bin_count': len(dest_bins),
                    'source_bin_count': len(self.source_bins),
                })
                self.log("Auto-saved last setup to _last_setup.json")
            except Exception as e:
                self.log(f"Warning: could not auto-save setup: {e}")

            self.emit('hardware_setup_complete', {
                'success': True,
                'dest_bin_count': len(dest_bins),
                'source_bin_count': len(self.source_bins),
                'staging': staging,
                'locations': locations,
                'probe_results': probe_results,
            })
        finally:
            # GUARANTEE: the state machine always ends up back in 'idle'
            # after this command finishes — whether by success, error, or
            # cancel. This is what was causing the UI to freeze: on
            # cancel, state was left in whatever it was, and subsequent
            # setup attempts were blocked by the state check at the top
            # of this method. Resetting here makes cancel fully recoverable.
            if self.state not in ('disconnected', 'estopped'):
                self.state = 'idle'
            # Also clear the abort flag so the next command starts fresh.
            self._clear_abort()

    def _cmd_check_source_empty(self, camera=None, **kwargs):
        """Check if source bin(s) are empty using ArUco detection."""
        if not self.source_bins:
            self.log("No calibrated source bins — run calibration first")
            return

        if camera is None:
            self.log("Camera required for empty check")
            return

        from web_calibration import calibrator
        import gcode_control

        results = calibrator.check_source_bins_empty(
            camera, gcode_control, self.source_bins)
        self.source_empty = results

        for bin_num, is_empty in results.items():
            status = "EMPTY" if is_empty else "has cards"
            self.log(f"Source bin {bin_num}: {status}")

        self.emit('source_bins_status', {
            'bins': [
                {'bin_number': bn, 'empty': empty}
                for bn, empty in results.items()
            ]
        })

    # --- Test Scan ---

    def _cmd_test_scan(self, camera=None, count=10, drop_bin=1, **kwargs):
        """
        Pick, stage, identify, and drop cards into one bin.

        Full flow per card (matches old test_staging_detection.py):
          1. Pick card from source bin
          2. Drop on staging platform (white background)
          3. Move suction head away so camera has clear view
          4. Capture frame, detect contour, identify card
          5. Pick card from staging
          6. Drop in drop bin
          7. Repeat
        """
        if self.state not in ('idle',):
            self.log("Cannot test scan — must be connected and idle (not in a sort session)")
            self.emit('test_scan_finished', {
                'total': 0, 'recognized': 0, 'results': [],
                'error': 'Must be connected and idle',
            })
            return

        import gcode_control
        from card_detect import detect_card
        from cards import extract_card_info, CARD_DATA_BY_ID
        from card_identify_hybrid import identify_card, is_card_back
        from config import PHASH_DISTANCE_THRESHOLD, PHASH_CLOSE_MATCH_DIFF, EXCLUDED_SETS

        if camera is None:
            self.log("No camera available for test scan")
            self.emit('test_scan_finished', {
                'total': 0, 'recognized': 0, 'results': [],
                'error': 'No camera available — start camera first',
            })
            return

        self.test_scan_stop = False
        self.log(f"Test scan starting: {count} cards -> bin {drop_bin}")
        self.emit('test_scan_started', {'count': count, 'drop_bin': drop_bin})

        bin_locs = gcode_control.get_bin_locations()
        source_x = bin_locs.get(0, gcode_control.X_SOURCE_BIN)
        staging_x = gcode_control.X_STAGING_POSITION
        detect_x = gcode_control.X_DETECTION_POSITION
        target_x = bin_locs.get(drop_bin, 150.0)
        z_clear = gcode_control.Z_CLEAR_HEIGHT or gcode_control.Z_MAX
        drop_z = gcode_control.Z_MAX - gcode_control.Z_DROP_OFFSET

        results = []

        try:
            # Home first
            self.log("Homing all axes...")
            gcode_control.home_all()

            # Stage the first card
            self.log("Picking first card from source...")
            gcode_control.pick_from_position(source_x)
            self.log("Dropping on staging platform...")
            gcode_control.drop_on_staging(1)
            self.log("Parking for camera view...")
            gcode_control.move_to_camera_position()
            time.sleep(0.5)   # Let carriage vibration settle
            camera.flush_buffer()
            time.sleep(0.3)   # Let auto-exposure adjust after flush

            for i in range(count):
                if self.test_scan_stop:
                    self.log(f"Test scan stopped by user after {i} cards")
                    break

                card_num = i + 1
                self.log(f"Test scan card {card_num}/{count}")
                self.emit('test_scan_progress', {
                    'current': card_num, 'total': count,
                })

                # --- Card is on staging, camera has clear view ---
                # Grab a sharp frame (wait for vibration/blur to settle)
                frame, sharpness = camera.get_sharp_frame(
                    min_sharpness=50.0, max_wait=2.0, settle_frames=3)
                if frame is None:
                    self.log(f"  Card {card_num}: failed to grab frame")
                    results.append({'index': card_num, 'recognized': False,
                                    'reason': 'no_frame'})
                    self.emit('test_scan_card', results[-1])
                    # Still need to pick up the card from staging
                    gcode_control.pick_from_staging()
                    gcode_control.drop_on_surface(target_x)
                    if card_num < count:
                        gcode_control.pick_from_position(source_x)
                        gcode_control.drop_on_staging(card_num + 1)
                        gcode_control.move_to_camera_position()
                        time.sleep(0.5)
                        camera.flush_buffer()
                        time.sleep(0.3)
                    continue

                card_img = detect_card(frame)
                if card_img is None:
                    self.log(f"  Card {card_num}: no card detected on staging")
                    results.append({'index': card_num, 'recognized': False,
                                    'reason': 'no_card'})
                    self.emit('test_scan_card', results[-1])
                    # Still pick up and move the card
                    gcode_control.pick_from_staging()
                    gcode_control.drop_on_surface(target_x)
                    if card_num < count:
                        gcode_control.pick_from_position(source_x)
                        gcode_control.drop_on_staging(card_num + 1)
                        gcode_control.move_to_camera_position()
                        time.sleep(0.5)
                        camera.flush_buffer()
                        time.sleep(0.3)
                    continue

                # Card-back check before orientation/layout (saves ~400 ms
                # if the card landed face-down).
                back_hit, back_dist = is_card_back(card_img)
                if back_hit:
                    self.log(f"  Card {card_num}: CARD BACK "
                             f"(dist={back_dist:.2f})")
                    result = {'index': card_num, 'recognized': False,
                              'reason': 'card_back',
                              'hash_distance': back_dist}
                    results.append(result)
                    self.emit('test_scan_card', result)
                    gcode_control.pick_from_staging()
                    gcode_control.drop_on_surface(target_x)
                    if card_num < count:
                        gcode_control.pick_from_position(source_x)
                        gcode_control.drop_on_staging(card_num + 1)
                        gcode_control.move_to_camera_position()
                        time.sleep(0.5)
                        camera.flush_buffer()
                        time.sleep(0.3)
                    continue

                # Identify using the new simple pipeline
                import threading
                id_result = [None, None, None]  # card_info, method, hash_distance

                def _identify(img):
                    try:
                        cid, dist, was_rotated, all_results = \
                            identify_card(img, threshold=PHASH_DISTANCE_THRESHOLD)
                    except Exception as e:
                        self.log(f"[identify] error: {e}")
                        return

                    # Filter for paper-only, non-excluded sets
                    filtered = []
                    for c, d in all_results:
                        cdata = CARD_DATA_BY_ID.get(c, {})
                        if 'paper' not in cdata.get('games', []):
                            continue
                        sc = cdata.get('set', '').lower()
                        if sc not in EXCLUDED_SETS:
                            filtered.append((c, d))

                    if filtered and filtered[0][1] <= PHASH_DISTANCE_THRESHOLD:
                        ci = extract_card_info(filtered[0][0])
                        id_result[0] = ci
                        id_result[1] = "hash"
                        id_result[2] = filtered[0][1]
                        self.log(f"[identify] MATCH dist={filtered[0][1]:.1f}")
                    elif filtered:
                        id_result[2] = filtered[0][1]
                        self.log(f"[identify] NO MATCH (top={filtered[0][1]:.1f})")

                id_thread = threading.Thread(target=_identify,
                                             args=(card_img,),
                                             daemon=True)
                id_thread.start()

                # Pick card from staging while identification runs
                gcode_control.pick_from_staging()

                # Wait for ID to finish (likely already done)
                id_thread.join()
                card_info, method, hash_distance = id_result

                if card_info:
                    card_name = card_info.get('Name', 'Unknown')
                    self.log(f"  Card {card_num}: {card_name} ({method})")
                    result = {'index': card_num, 'recognized': True,
                              'name': card_name, 'method': method,
                              'set': card_info.get('Set', '?'),
                              'hash_distance': hash_distance}
                else:
                    card_name = 'UNKNOWN'
                    self.log(f"  Card {card_num}: unrecognized"
                             + (f" (dist={hash_distance:.1f})" if hash_distance else ""))
                    result = {'index': card_num, 'recognized': False,
                              'reason': 'unrecognized',
                              'hash_distance': hash_distance}

                results.append(result)
                self.emit('test_scan_card', result)

                # Drop in drop bin
                gcode_control.drop_on_surface(target_x)

                # Stage next card (if not last)
                if card_num < count:
                    gcode_control.pick_from_position(source_x)
                    gcode_control.drop_on_staging(card_num + 1)
                    gcode_control.move_to_camera_position()
                    time.sleep(0.5)   # Let carriage vibration settle
                    camera.flush_buffer()
                    time.sleep(0.3)   # Let auto-exposure adjust

        except Exception as e:
            self.log(f"Test scan error: {e}")
        finally:
            # Safety: pumps off, Z up
            gcode_control.all_pumps_off()
            gcode_control.z_to_top()

        recognized = sum(1 for r in results if r.get('recognized'))
        self.log(f"Test scan complete: {recognized}/{len(results)} recognized")
        self.emit('test_scan_finished', {
            'total': len(results),
            'recognized': recognized,
            'results': results,
        })

    def get_nearest_source_x(self, current_x=None):
        """
        Get the X position of the nearest non-empty source bin.

        For dual-source optimization: after dropping a card, the sort cycle
        returns to whichever source bin is closer, reducing travel time.
        """
        if not self.source_bins or len(self.source_bins) <= 1:
            return None  # Single source — use default behavior

        from web_calibration import calibrator
        x = current_x if current_x is not None else (self.last_drop_x or 0)
        best = calibrator.get_nearest_source_bin(x, self.source_bins, self.source_empty)
        if best:
            return best['x']
        return None

    # --- Helpers ---

    def _emit_session_stats(self):
        """Emit current session statistics."""
        if not self.tracker:
            return
        stats = self.tracker.get_stats()
        # Add speed estimate
        avg_time = sum(self.sort_times) / len(self.sort_times) if self.sort_times else 0
        stats['avg_seconds_per_card'] = round(avg_time, 1)

        # Stack estimate
        if self.last_source_z is not None and self.empty_bin_z is not None:
            remaining_height = self.last_source_z - self.empty_bin_z
            est_cards = max(0, remaining_height / self.card_thickness)
            est_time = est_cards * avg_time if avg_time > 0 else 0
            stats['estimated_cards_remaining'] = int(est_cards)
            stats['estimated_time_remaining'] = round(est_time, 0)

        # Include full bin contents (card names, not just counts)
        stats['bin_contents_detail'] = self.tracker.get_all_bins()

        # Include bin fullness status so the session tab can show full bins
        stats['bin_fullness'] = self.get_bin_fullness_status()

        self.emit('session_stats', stats)

    def get_status(self):
        """Get the full worker status."""
        import gcode_control
        status = {
            'state': self.state,
            'connected': gcode_control.is_connected(),
            'port': gcode_control.SERIAL_PORT,
            'scan_count': self.scan_count,
            'sort_mode': self.sort_mode,
            'motion': motion_tracker.get_state(),
        }
        if self.tracker:
            status['session_stats'] = self.tracker.get_stats()
        if self.last_card_info:
            status['last_card'] = {
                'name': self.last_card_info.get('Name', '?'),
                'set': self.last_card_info.get('Set', '?'),
                'bin': self.last_card_bin,
                'method': self.last_card_method,
            }
        status['bin_fullness'] = self.get_bin_fullness_status()
        return status

    def get_bin_contents(self):
        """Get current session bin contents."""
        if self.tracker:
            return self.tracker.get_all_bins()
        return {}

    def get_source_bins_info(self):
        """Get information about calibrated source bins."""
        return {
            'source_bins': self.source_bins,
            'source_empty': self.source_empty,
            'multi_source': len(self.source_bins) > 1,
        }


# Module-level singleton
worker = SortWorker()
