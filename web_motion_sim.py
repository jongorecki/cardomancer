# web_motion_sim.py
# ---------------------------------------------------------------------------
# Motion state tracker and simulation engine for the web server.
#
# Tracks the current and destination X/Z positions of the suction head.
# Estimates intermediate positions based on feedrate and elapsed time.
# In simulation mode, calculates motion timing without serial hardware.
# ---------------------------------------------------------------------------

import time
import threading
import random

from gcode_control import (
    Z_MAX, Z_CLEAR_HEIGHT, Z_DROP_OFFSET, X_SOURCE_BIN,
    X_FEEDRATE, Z_FEEDRATE, Z_PROBE_FEEDRATE,
    X_DETECTION_POSITION, X_STAGING_POSITION,
    get_bin_locations, VACUUM_ON_DELAY_MS, PRESSURE_ON_MS,
)


class MotionTracker:
    """
    Tracks the suction head position in real-time.
    Updates are pushed by the worker thread as G-code commands are sent.
    The frontend polls or receives SocketIO events with interpolated positions.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.x = 0.0
        self.z = Z_MAX
        self.dest_x = 0.0
        self.dest_z = Z_MAX
        self.x_feedrate = X_FEEDRATE  # mm/min
        self.z_feedrate = Z_FEEDRATE
        self.moving = False
        self._move_start_time = 0.0
        self._move_start_x = 0.0
        self._move_start_z = 0.0
        self._move_duration = 0.0
        # Suction head payload
        self.carrying_card = None  # None="EMPTY", str=card name, "UNKNOWN"=picked but not scanned

    def update_position(self, x=None, z=None):
        """Called when a move completes — set actual position."""
        with self._lock:
            if x is not None:
                self.x = x
                self.dest_x = x
            if z is not None:
                self.z = z
                self.dest_z = z
            self.moving = False

    def start_move(self, dest_x=None, dest_z=None, feedrate=None):
        """Called when a move command is sent — start interpolation."""
        with self._lock:
            self._move_start_time = time.time()
            self._move_start_x = self.x
            self._move_start_z = self.z

            if dest_x is not None:
                self.dest_x = dest_x
            if dest_z is not None:
                self.dest_z = dest_z

            # Calculate move duration from distance and feedrate
            dx = abs(self.dest_x - self.x)
            dz = abs(self.dest_z - self.z)
            # Use the appropriate feedrate (mm/min -> mm/sec)
            fr = (feedrate or X_FEEDRATE) / 60.0
            distance = max(dx, dz)  # Moves are sequential, not simultaneous
            self._move_duration = distance / fr if fr > 0 else 0
            self.moving = distance > 0.1

    def set_carrying(self, card_name):
        """Update what the suction head is carrying."""
        with self._lock:
            self.carrying_card = card_name

    def get_state(self):
        """Get the current interpolated state."""
        with self._lock:
            if self.moving and self._move_duration > 0:
                elapsed = time.time() - self._move_start_time
                t = min(elapsed / self._move_duration, 1.0)
                est_x = self._move_start_x + t * (self.dest_x - self._move_start_x)
                est_z = self._move_start_z + t * (self.dest_z - self._move_start_z)
                if t >= 1.0:
                    self.x = self.dest_x
                    self.z = self.dest_z
                    self.moving = False
                return {
                    "x": round(est_x, 1),
                    "z": round(est_z, 1),
                    "dest_x": round(self.dest_x, 1),
                    "dest_z": round(self.dest_z, 1),
                    "moving": t < 1.0,
                    "carrying": self.carrying_card,
                }
            else:
                return {
                    "x": round(self.x, 1),
                    "z": round(self.z, 1),
                    "dest_x": round(self.dest_x, 1),
                    "dest_z": round(self.dest_z, 1),
                    "moving": False,
                    "carrying": self.carrying_card,
                }


class SortSimulator:
    """
    Simulates a sorting run without hardware.
    Pulls random cards from the database, assigns bins, and generates
    motion events as if the machine were running.
    """

    def __init__(self):
        self.running = False
        self.results = []
        self.bin_contents = {}
        self.progress = 0
        self.total = 0
        self._thread = None

    def start(self, card_count, sort_mode, sort_config=None, emit_fn=None):
        """Start a simulated sort run in a background thread."""
        if self.running:
            return False
        self.running = True
        self.results = []
        self.bin_contents = {}
        self.progress = 0
        self.total = card_count
        self._thread = threading.Thread(
            target=self._run_simulation,
            args=(card_count, sort_mode, sort_config, emit_fn),
            daemon=True,
        )
        self._thread.start()
        return True

    def _run_simulation(self, card_count, sort_mode, sort_config, emit_fn):
        """Run the simulation in a background thread."""
        try:
            import os
            from cards import CARD_DATA_BY_ID, extract_card_info, card_is_allowed
            from sorting import set_sort_config, get_sort_config
            from sort_config import SortConfig
            from config import EXCLUDED_SETS, SORT_CONFIGS_DIR

            # Unified routing: the simulation needs a SortConfig just like
            # the real worker. If the caller didn't supply one, fall back to
            # loading sort_configs/<sort_mode>.txt (built-in modes now ship
            # as .txt files).
            if sort_config:
                set_sort_config(sort_config)
            elif get_sort_config() is None and sort_mode:
                candidate = os.path.join(SORT_CONFIGS_DIR, f"{sort_mode}.txt")
                if os.path.exists(candidate):
                    try:
                        set_sort_config(SortConfig.from_file(candidate))
                    except Exception as e:
                        if emit_fn:
                            emit_fn('error', {
                                'message': f'Simulation SortConfig load failed: {e}',
                            })
                        self.running = False
                        return

            # Get pool of allowed cards
            allowed_ids = [
                cid for cid in CARD_DATA_BY_ID
                if card_is_allowed(cid)
            ]

            if not allowed_ids:
                if emit_fn:
                    emit_fn('error', {'message': 'No cards available for simulation'})
                self.running = False
                return

            # Pick random cards
            sample_ids = random.choices(allowed_ids, k=card_count)

            bin_locs = get_bin_locations()
            source_x = bin_locs.get(0, X_SOURCE_BIN)
            z_clear = Z_CLEAR_HEIGHT
            drop_z = Z_MAX - Z_DROP_OFFSET
            current_x = 0.0
            current_z = Z_MAX

            for i, card_id in enumerate(sample_ids):
                if not self.running:
                    break

                card_data = CARD_DATA_BY_ID.get(card_id, {})
                card_info = extract_card_info(card_id)
                _cfg = get_sort_config()
                bin_num = _cfg.get_bin(card_data) if _cfg is not None else 10
                target_x = bin_locs.get(bin_num, 150.0)

                card_name = card_info.get('Name', 'Unknown') if card_info else 'Unknown'

                # Simulate motion sequence timing
                # Mirrors real workflow: pick from source -> detect -> sort to bin
                detect_x = X_DETECTION_POSITION
                steps = [
                    # 1) Z to clear
                    {"dest_x": current_x, "dest_z": z_clear,
                     "feedrate": Z_FEEDRATE, "carrying": None},
                    # 2) X to source
                    {"dest_x": source_x, "dest_z": z_clear,
                     "feedrate": X_FEEDRATE, "carrying": None},
                    # 3) Z probe down (simulate picking card)
                    {"dest_x": source_x, "dest_z": Z_MAX - 100,
                     "feedrate": Z_PROBE_FEEDRATE, "carrying": None},
                    # 4) Pick up — vacuum on, card attached (identity unknown)
                    {"dest_x": source_x, "dest_z": Z_MAX - 100,
                     "feedrate": 0, "carrying": "UNKNOWN",
                     "delay": VACUUM_ON_DELAY_MS / 1000.0},
                    # 5) Z to clear (carrying unknown card)
                    {"dest_x": source_x, "dest_z": z_clear,
                     "feedrate": Z_FEEDRATE, "carrying": "UNKNOWN"},
                    # 6) X to detection position (camera scans card here)
                    {"dest_x": detect_x, "dest_z": z_clear,
                     "feedrate": X_FEEDRATE, "carrying": "UNKNOWN"},
                    # 7) Detection pause — card identified
                    {"dest_x": detect_x, "dest_z": z_clear,
                     "feedrate": 0, "carrying": card_name,
                     "delay": 0.5},
                    # 8) X to target bin
                    {"dest_x": target_x, "dest_z": z_clear,
                     "feedrate": X_FEEDRATE, "carrying": card_name},
                    # 9) Z to drop height
                    {"dest_x": target_x, "dest_z": drop_z,
                     "feedrate": Z_FEEDRATE, "carrying": card_name},
                    # 10) Drop — pressure on, card released
                    {"dest_x": target_x, "dest_z": drop_z,
                     "feedrate": 0, "carrying": None,
                     "delay": PRESSURE_ON_MS / 1000.0},
                    # 11) Z lift to clear after drop
                    {"dest_x": target_x, "dest_z": z_clear,
                     "feedrate": Z_FEEDRATE, "carrying": None},
                    # 12) X return to detection position
                    {"dest_x": detect_x, "dest_z": z_clear,
                     "feedrate": X_FEEDRATE, "carrying": None},
                ]

                # Emit motion_path for smooth frontend animation
                if emit_fn:
                    motion_waypoints = []
                    for step in steps:
                        wp = {
                            'x': round(step['dest_x'], 1),
                            'z': round(step['dest_z'], 1),
                            'feedrate': step['feedrate'],
                            'carrying': step['carrying'],
                        }
                        if 'delay' in step and step['delay'] > 0:
                            wp['pause'] = int(step['delay'] * 1000)  # s -> ms
                        motion_waypoints.append(wp)
                    emit_fn('motion_path', motion_waypoints)

                # Simulate timing for pacing
                total_sim_time = 0
                for step in steps:
                    if not self.running:
                        break
                    if step["feedrate"] > 0:
                        dx = abs(step["dest_x"] - current_x)
                        dz = abs(step["dest_z"] - current_z)
                        dist = max(dx, dz)
                        duration = dist / (step["feedrate"] / 60.0) if step["feedrate"] > 0 else 0
                        total_sim_time += duration
                    elif "delay" in step:
                        total_sim_time += step["delay"]
                    current_x = step["dest_x"]
                    current_z = step["dest_z"]
                # Wait proportional to real time (10x speed)
                time.sleep(min(total_sim_time / 10.0, 1.0))

                # Record result
                result = {
                    "card_name": card_name,
                    "set": card_info.get('Set', '?') if card_info else '?',
                    "colors": card_info.get('Colors', []) if card_info else [],
                    "bin": bin_num,
                }
                self.results.append(result)

                # Update bin contents
                bin_key = str(bin_num)
                if bin_key not in self.bin_contents:
                    self.bin_contents[bin_key] = []
                self.bin_contents[bin_key].append(result)

                self.progress = i + 1

                if emit_fn:
                    emit_fn('sim_card_sorted', result)
                    emit_fn('sim_progress', {
                        "progress": self.progress,
                        "total": self.total,
                        "bin_contents": {k: len(v) for k, v in self.bin_contents.items()},
                    })

        except Exception as e:
            import traceback
            traceback.print_exc()
            if emit_fn:
                emit_fn('error', {'message': f'Simulation error: {e}'})
        finally:
            self.running = False
            if emit_fn:
                emit_fn('sim_complete', {
                    "total": len(self.results),
                    "bin_contents": {k: len(v) for k, v in self.bin_contents.items()},
                    "results": self.results,
                })

    def stop(self):
        """Stop a running simulation."""
        self.running = False

    def get_status(self):
        """Get current simulation status."""
        return {
            "running": self.running,
            "progress": self.progress,
            "total": self.total,
            # Return all results when complete, last 10 while running
            "results": self.results if not self.running else self.results[-10:],
            "bin_counts": {k: len(v) for k, v in self.bin_contents.items()},
        }


# Module-level singletons
motion_tracker = MotionTracker()
simulator = SortSimulator()
