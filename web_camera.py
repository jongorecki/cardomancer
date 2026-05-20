# web_camera.py
# ---------------------------------------------------------------------------
# Camera management for the web server.
#
# The camera is a critical component — every hardware operation depends on
# it — so this module is written defensively. Goals:
#
#   * The capture thread NEVER dies. Any exception in cv2.VideoCapture.read()
#     is caught and handled; after N consecutive failures the device is
#     released and reopened automatically.
#   * A watchdog thread checks for stalled capture (no new frames for >N
#     seconds) and triggers a reconnect if things go quiet.
#   * MJPEG generators NEVER stop yielding. If real frames aren't available,
#     a "placeholder" frame with status text is generated instead. That
#     keeps the browser's MJPEG connection continuously fed so it doesn't
#     freeze on the last good frame.
#   * CAP_PROP_BUFFERSIZE is set to 1, so get_frame() always reflects
#     something close to the real current view instead of a stale buffered
#     frame from several reads ago.
#   * ArUco detection is serialized via a calibrator-level lock (the detector
#     itself is not thread-safe).
# ---------------------------------------------------------------------------

import logging
import threading
import time
import cv2
import numpy as np

logger = logging.getLogger(__name__)


class CameraManager:
    """
    Manages the webcam in a background thread with self-healing behavior.
    Provides thread-safe frame access and resilient MJPEG streaming.
    """

    # Tunables
    RECONNECT_AFTER_FAILURES = 20  # Consecutive read failures before reopen
    WATCHDOG_STALL_WARN = 5.0      # Seconds of no frames = warning
    WATCHDOG_STALL_RECONNECT = 10.0 # Seconds of no frames = force reconnect
    READ_FAILURE_SLEEP = 0.05      # How long to wait after a failed read
    LOW_FPS_WARN_THRESHOLD = 10.0  # Warn (and try MJPG fallback) below this
    DARK_FRAME_WARN_THRESHOLD = 15 # Mean brightness (0-255) below = warn

    # Capture format preferences — MJPG is essential on USB webcams or the
    # camera drops to 1-5fps because YUY2 eats USB bandwidth.
    # 1920x1080 confirmed supported by webcam with MJPG via DirectShow.
    TARGET_WIDTH = 1920
    TARGET_HEIGHT = 1080
    TARGET_FPS = 30

    def __init__(self, device_index=0, rotate=cv2.ROTATE_90_CLOCKWISE):
        self.device_index = device_index
        self.rotate = rotate
        self._cap = None
        self._frame = None
        self._lock = threading.Lock()
        # Serializes start()/stop() so concurrent callers (e.g. two MJPEG
        # endpoints auto-starting the camera at the same time) can't both
        # race into _open_capture() and clobber self._cap mid-init.
        self._lifecycle_lock = threading.Lock()
        self._thread = None
        self._watchdog_thread = None
        self._running = False

        # Stats + health
        self._frame_count = 0
        self._fps = 0.0
        self._last_fps_time = time.time()
        self._last_fps_count = 0
        self._last_good_frame_time = 0.0  # Wallclock of last successful read
        self._read_failures = 0            # Lifetime total
        self._reconnect_count = 0          # Lifetime total
        self._health = 'unknown'           # 'ok' | 'stalled' | 'dead' | 'unknown'
        self._last_error = ''

        # Health-change callbacks: called with (old_health, new_health)
        # whenever _health changes. Used by web_server to auto-pause
        # the sort worker if the camera freezes mid-session.
        self._health_listeners = []

        # Focus lock state
        self._focus_locked = False

    def add_health_listener(self, callback):
        """
        Register a callback that fires when camera health changes.
        Signature: callback(old_health, new_health). Safe to call from
        any thread. Exceptions in listeners are caught and logged.
        """
        if callback not in self._health_listeners:
            self._health_listeners.append(callback)

    def _set_health(self, new_health):
        """Set health and notify listeners on change."""
        old = self._health
        if old == new_health:
            return
        self._health = new_health
        for cb in list(self._health_listeners):
            try:
                cb(old, new_health)
            except Exception:
                logger.exception("health listener error")

    @property
    def is_active(self):
        return self._running and self._cap is not None and self._cap.isOpened()

    @property
    def fps(self):
        return self._fps

    @property
    def health(self):
        return self._health

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------

    def start(self):
        """Start the camera capture thread + watchdog.

        Serialized via _lifecycle_lock so concurrent callers (e.g. the
        Flask MJPEG feed endpoints both auto-starting on the same page
        load) can't both race into _open_capture() and clobber
        self._cap while the first open is mid-warmup.
        """
        with self._lifecycle_lock:
            # Double-check inside the lock — another caller may have
            # started the camera while we were waiting on the lock.
            if self._running:
                return True

            if not self._open_capture():
                return False

            self._running = True
            self._set_health('ok')
            self._last_good_frame_time = time.time()

            self._thread = threading.Thread(
                target=self._capture_loop, daemon=True,
                name='camera-capture')
            self._thread.start()

            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop, daemon=True,
                name='camera-watchdog')
            self._watchdog_thread.start()

            logger.info(f"Started on device {self.device_index} "
                        f"(buffer_size=1, watchdog on)")
            return True

    def stop(self):
        """Stop the camera, capture thread, and watchdog."""
        with self._lifecycle_lock:
            if not self._running and self._cap is None:
                return
            self._running = False
            # Both threads are daemons with short sleep intervals, so
            # they'll exit quickly on their own. Give them a moment to
            # notice. Capture these refs before joining — another thread
            # can't replace them while we hold the lifecycle lock.
            threads = (self._thread, self._watchdog_thread)
            self._thread = None
            self._watchdog_thread = None
            for t in threads:
                if t is not None:
                    t.join(timeout=2)
            self._release_capture()
            with self._lock:
                self._frame = None
            self._set_health('unknown')
            logger.info("Stopped")

    # -------------------------------------------------------------------
    # Capture device management (open / release / reconnect)
    # -------------------------------------------------------------------

    def _open_capture(self):
        """
        Open the VideoCapture with settings that guarantee a fast, reliable
        stream on USB webcams. The two things that *must* be set correctly
        or the camera will drop to 1-5 fps on Windows:

          1. FOURCC = MJPG. Without this, Windows opens YUY2 uncompressed
             which saturates USB bandwidth at anything above 640x480 and
             the camera throttles itself to 1-5fps. MJPG lets the camera
             stream compressed frames at 30fps.
          2. An explicit resolution. Leaving it on "default" can pick the
             camera's native mode (e.g. 1920x1080 or higher), which again
             hits bandwidth limits.

        Backend order: CAP_DSHOW first — DirectShow is the most stable
        backend for USB webcams on Windows in background threads. CAP_MSMF
        (the default CAP_ANY pick on modern Windows) can stall or drop
        frames when used from non-main threads, causing spurious reconnects.
        After opening, we do a brief warm-up read loop so auto-exposure has
        a chance to settle before the first real consumer sees a frame.
        """
        cap = None
        backends_tried = []

        def try_backend(backend, name):
            try:
                c = cv2.VideoCapture(self.device_index, backend)
                backends_tried.append(name)
                if c.isOpened():
                    return c
                try:
                    c.release()
                except Exception:
                    pass
            except Exception as e:
                logger.warning(f"{name} open exception: {e}")
            return None

        # DirectShow first — most stable for USB webcams on Windows.
        cap = try_backend(cv2.CAP_DSHOW, 'CAP_DSHOW')
        if cap is None:
            cap = try_backend(cv2.CAP_ANY, 'CAP_ANY')
        if cap is None:
            cap = try_backend(cv2.CAP_MSMF, 'CAP_MSMF')

        if cap is None or not cap.isOpened():
            self._last_error = (f'cv2.VideoCapture could not open device '
                                f'(tried: {", ".join(backends_tried)})')
            logger.error(self._last_error)
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
            self._cap = None
            return False

        # --- Force MJPG + explicit resolution + FPS ---
        # Order matters: set FOURCC first, then size, then FPS. Some drivers
        # ignore FPS if you try to set it before the format is locked in.
        try:
            cap.set(cv2.CAP_PROP_FOURCC,
                    cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))
        except Exception as e:
            logger.warning(f"could not set FOURCC=MJPG: {e}")

        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.TARGET_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.TARGET_HEIGHT)
        except Exception as e:
            logger.warning(f"could not set resolution: {e}")

        try:
            cap.set(cv2.CAP_PROP_FPS, self.TARGET_FPS)
        except Exception as e:
            logger.warning(f"could not set FPS: {e}")

        # Minimize internal buffering so get_frame() reflects the real
        # current view. Some backends silently ignore this.
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        # Read back what we actually got — useful diagnostic if things
        # misbehave. Not all backends report these accurately.
        try:
            actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            actual_fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
            fourcc_int = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
            fourcc_str = (chr(fourcc_int & 0xFF)
                          + chr((fourcc_int >> 8) & 0xFF)
                          + chr((fourcc_int >> 16) & 0xFF)
                          + chr((fourcc_int >> 24) & 0xFF))
        except Exception:
            actual_w = actual_h = 0
            actual_fps = 0.0
            fourcc_str = '????'

        logger.info(f"opened ({backends_tried[-1]}) "
                    f"{actual_w}x{actual_h}@{actual_fps:.0f}fps fourcc={fourcc_str}")

        # --- Warm-up read loop: discard the first few frames so auto-exposure
        # has a chance to settle. Without this, the first frame the sweep
        # sees is often nearly black even though the camera is working.
        warmup_reads = 0
        warmup_failures = 0
        warmup_deadline = time.time() + 2.5  # at most 2.5s of warm-up
        while time.time() < warmup_deadline and warmup_reads < 8:
            try:
                ok, _ = cap.read()
            except Exception:
                ok = False
            if ok:
                warmup_reads += 1
            else:
                warmup_failures += 1
                if warmup_failures > 10:
                    break
            time.sleep(0.05)
        logger.info(f"warmup: {warmup_reads} reads, "
                    f"{warmup_failures} failures")

        self._cap = cap
        self._last_error = ''
        return True

    def _release_capture(self):
        """Safely release the VideoCapture, swallowing any driver errors."""
        cap = self._cap
        self._cap = None
        if cap is not None:
            try:
                cap.release()
            except Exception as e:
                logger.warning(f"release error: {e}")

    def _reconnect(self, reason=''):
        """
        Tear down and reopen the capture device. Called by the capture loop
        after too many consecutive read failures, or by the watchdog after
        a stall. This runs on whatever thread noticed the problem.

        Single-flight via _lifecycle_lock with a non-blocking acquire —
        the watchdog fires every 1s, and _open_capture takes several
        seconds, so without this guard 2-3 reconnects stack up and race
        on self._cap, breaking the device entirely.
        """
        if not self._lifecycle_lock.acquire(blocking=False):
            # Another thread is already reconnecting (or start/stop is
            # in progress). Let that one finish — no point racing.
            return False
        try:
            logger.warning(f"reconnecting ({reason})")
            self._set_health('dead')
            self._focus_locked = False  # Reset — new device needs fresh AF
            self._release_capture()
            time.sleep(0.5)
            # Try up to 3 times, sleeping longer between attempts
            for attempt in range(3):
                if self._open_capture():
                    self._reconnect_count += 1
                    self._set_health('ok')
                    self._last_good_frame_time = time.time()
                    logger.info(f"reconnected on attempt {attempt + 1} "
                                f"(total reconnects: {self._reconnect_count})")
                    return True
                time.sleep(1.0 * (attempt + 1))
            logger.error("reconnect failed after 3 attempts")
            self._set_health('dead')
            return False
        finally:
            self._lifecycle_lock.release()

    # -------------------------------------------------------------------
    # Capture loop — runs in its own thread
    # -------------------------------------------------------------------

    def _capture_loop(self):
        """
        Continuously grab frames from the camera. Never dies:
        - Any exception during read() is caught.
        - After RECONNECT_AFTER_FAILURES consecutive failures, the device
          is reopened via _reconnect().
        """
        consecutive_failures = 0

        while self._running:
            if self._cap is None or not self._cap.isOpened():
                # Device is gone — try to bring it back.
                if not self._reconnect('capture device closed'):
                    time.sleep(1.0)
                continue

            # Read a frame, defensively.
            try:
                ret, frame = self._cap.read()
            except Exception as e:
                ret, frame = False, None
                self._last_error = f'read exception: {e}'
                logger.error(f"read exception: {e}")

            if not ret or frame is None:
                consecutive_failures += 1
                self._read_failures += 1
                if consecutive_failures == 1:
                    # First hit — don't panic yet, just log once.
                    logger.warning(f"read failure (failures={self._read_failures})")
                if consecutive_failures >= self.RECONNECT_AFTER_FAILURES:
                    self._reconnect(
                        f'{consecutive_failures} consecutive read failures')
                    consecutive_failures = 0
                time.sleep(self.READ_FAILURE_SLEEP)
                continue

            consecutive_failures = 0

            # Apply rotation. If rotate fails somehow (shouldn't), drop
            # the frame but keep the loop running.
            try:
                if self.rotate is not None:
                    frame = cv2.rotate(frame, self.rotate)
            except Exception:
                logger.exception("rotate exception")
                continue

            now = time.time()
            with self._lock:
                self._frame = frame
                self._frame_count += 1
                self._last_good_frame_time = now

            # FPS accounting + low-fps / dark-frame warnings. Once a
            # second we check:
            #   * Is the capture loop running well below what we asked for?
            #     (indicates USB bandwidth throttling, bad format, etc)
            #   * Is the frame mean brightness ridiculously low?
            #     (indicates dark scene, lens cap, or disconnected camera
            #     returning black buffers)
            if now - self._last_fps_time >= 1.0:
                prev_fps = self._fps
                self._fps = ((self._frame_count - self._last_fps_count) /
                             (now - self._last_fps_time))
                self._last_fps_time = now
                self._last_fps_count = self._frame_count

                if self._fps < self.LOW_FPS_WARN_THRESHOLD:
                    self._last_error = (f'low fps {self._fps:.1f} '
                                        f'(target {self.TARGET_FPS})')
                    if prev_fps >= self.LOW_FPS_WARN_THRESHOLD or prev_fps == 0:
                        logger.warning(f"capture fps={self._fps:.1f} "
                                       f"— check USB bandwidth / format")

                # Cheap dark-frame check: downsample + mean. Done at most
                # once per second to keep overhead negligible.
                try:
                    small = cv2.resize(frame, (64, 64))
                    mean_brightness = float(small.mean())
                    if mean_brightness < self.DARK_FRAME_WARN_THRESHOLD:
                        # Don't clobber a lower-priority error message
                        if 'low fps' not in (self._last_error or ''):
                            self._last_error = (f'dark frames '
                                                f'(mean={mean_brightness:.0f}) '
                                                f'— check lens/lighting')
                except Exception:
                    pass

            # Pace to ~30 FPS max — gives other threads room to run.
            time.sleep(0.03)

        logger.info("capture loop exiting")

    # -------------------------------------------------------------------
    # Watchdog — monitors capture loop health
    # -------------------------------------------------------------------

    def _watchdog_loop(self):
        """
        Runs once per second. If the capture loop stops producing frames
        for WATCHDOG_STALL_WARN seconds, sets health to 'stalled'. If it
        stays quiet for WATCHDOG_STALL_RECONNECT seconds, forces a
        reconnect. This catches the case where cap.read() silently blocks
        instead of raising.
        """
        while self._running:
            time.sleep(1.0)
            if not self._running:
                break
            if self._last_good_frame_time == 0:
                continue
            since = time.time() - self._last_good_frame_time
            if since > self.WATCHDOG_STALL_RECONNECT:
                logger.warning(f"watchdog: {since:.1f}s since last frame "
                               f"— forcing reconnect")
                self._reconnect(f'watchdog stall {since:.1f}s')
            elif since > self.WATCHDOG_STALL_WARN:
                if self._health != 'stalled':
                    logger.warning(f"watchdog: {since:.1f}s since last frame "
                                   f"(stalled)")
                self._set_health('stalled')
            else:
                if self._health != 'ok':
                    logger.info("watchdog: healthy again")
                self._set_health('ok')

    # -------------------------------------------------------------------
    # Frame access (thread-safe)
    # -------------------------------------------------------------------

    def flush_buffer(self, min_new_frames=2, timeout=0.5):
        """
        Wait until the capture loop has produced at least `min_new_frames`
        fresh frames since the current one. Non-destructive: consumers
        (like the MJPEG stream) see a continuous sequence of valid frames
        while this waits.

        Returns True if the target was reached in time, False if the
        timeout was hit (which generally means the capture loop is
        stalled or dead — the caller may want to skip this step).
        """
        with self._lock:
            start_count = self._frame_count
        target = start_count + max(1, int(min_new_frames))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._running:
                return False
            with self._lock:
                if self._frame_count >= target:
                    return True
            time.sleep(0.01)
        return False

    def get_frame(self):
        """Get a copy of the latest frame. Returns None if none available."""
        with self._lock:
            if self._frame is None:
                return None
            return self._frame.copy()

    def get_jpeg(self, quality=80):
        """Encode the latest frame as JPEG bytes. Returns None if none."""
        frame = self.get_frame()
        if frame is None:
            return None
        try:
            ret, jpeg = cv2.imencode('.jpg', frame,
                                      [cv2.IMWRITE_JPEG_QUALITY, quality])
        except Exception:
            return None
        if not ret:
            return None
        return jpeg.tobytes()

    # -------------------------------------------------------------------
    # Focus control
    # -------------------------------------------------------------------

    def lock_focus(self):
        """
        Lock the camera's autofocus at its current position.

        Call this after the first card is in focus on the staging platform.
        Switching from autofocus to manual (CAP_PROP_AUTOFOCUS=0) freezes
        the focus motor at whatever distance it last settled on. This
        prevents the camera from re-hunting between cards (which causes
        blurry frames and detection failures).

        Safe to call multiple times — subsequent calls are no-ops.
        """
        if self._focus_locked:
            return
        cap = self._cap
        if cap is None or not cap.isOpened():
            return
        try:
            # Disable autofocus — freezes the lens at current position
            cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)
            self._focus_locked = True
            logger.info("Focus LOCKED (autofocus disabled)")
        except Exception as e:
            logger.warning(f"Could not lock focus: {e}")

    def unlock_focus(self):
        """
        Re-enable autofocus. Call when the sort session ends so the camera
        can refocus for the ROI capture at the start of the next session.
        """
        if not self._focus_locked:
            return
        cap = self._cap
        if cap is None or not cap.isOpened():
            return
        try:
            cap.set(cv2.CAP_PROP_AUTOFOCUS, 1)
            self._focus_locked = False
            logger.info("Focus UNLOCKED (autofocus re-enabled)")
        except Exception as e:
            logger.warning(f"Could not unlock focus: {e}")

    @property
    def focus_locked(self):
        return self._focus_locked

    # -------------------------------------------------------------------
    # Blur detection
    # -------------------------------------------------------------------

    @staticmethod
    def laplacian_sharpness(frame):
        """
        Compute a sharpness score using the variance of the Laplacian.
        Higher = sharper. A blurry frame (motion blur or out-of-focus)
        scores low because edges have low second-derivative magnitude.

        Returns a float. Typical values:
          - Very blurry / motion blur: < 30
          - Slightly soft / AF hunting: 30-80
          - In-focus card on white platform: 100-400+
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return cv2.Laplacian(gray, cv2.CV_64F).var()

    def get_sharp_frame(self, min_sharpness=50.0, max_wait=2.0,
                        settle_frames=3):
        """
        Wait for a non-blurry frame. Grabs frames until the Laplacian
        variance exceeds `min_sharpness` for `settle_frames` consecutive
        frames (to confirm focus has stabilized, not just one lucky frame
        between AF oscillations).

        Returns (frame, sharpness) or (None, 0.0) if timeout is hit.

        :param min_sharpness: Minimum Laplacian variance to accept
        :param max_wait: Maximum seconds to wait
        :param settle_frames: Consecutive sharp frames required
        """
        deadline = time.time() + max_wait
        consecutive_sharp = 0
        best_frame = None
        best_sharpness = 0.0

        while time.time() < deadline:
            frame = self.get_frame()
            if frame is None:
                time.sleep(0.03)
                continue

            sharpness = self.laplacian_sharpness(frame)

            # Track the best frame we've seen regardless
            if sharpness > best_sharpness:
                best_sharpness = sharpness
                best_frame = frame

            if sharpness >= min_sharpness:
                consecutive_sharp += 1
                if consecutive_sharp >= settle_frames:
                    return frame, sharpness
            else:
                consecutive_sharp = 0

            time.sleep(0.03)  # ~30 fps polling

        # Timeout — return the best frame we saw (may still be usable)
        if best_frame is not None:
            logger.warning(f"get_sharp_frame timeout — best sharpness "
                           f"{best_sharpness:.1f} (threshold {min_sharpness})")
        return best_frame, best_sharpness

    # -------------------------------------------------------------------
    # Placeholder frame — keeps MJPEG streams alive when camera is down
    # -------------------------------------------------------------------

    def _make_placeholder(self, message, sub_message='', color=(30, 30, 90)):
        """
        Build a 640x360 synthetic frame with a status message. Yielded by
        the MJPEG generators when no real frame is available so the
        browser connection stays alive and the user can see why.
        """
        img = np.full((360, 640, 3), 10, dtype=np.uint8)
        # Colored diagonal stripe background so it's visually unmistakable
        for i in range(0, 360, 40):
            cv2.line(img, (0, i), (640, i + 40), color, 2)
        cv2.rectangle(img, (0, 0), (639, 359), (0, 0, 255), 4)
        cv2.putText(img, message, (30, 170),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
        if sub_message:
            cv2.putText(img, sub_message, (30, 210),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (220, 220, 220), 1)
        # Health footer
        footer = (f"health={self._health}  reconnects={self._reconnect_count}  "
                  f"read_failures={self._read_failures}  fps={self._fps:.1f}")
        cv2.putText(img, footer, (20, 340),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (180, 180, 180), 1)
        if self._last_error:
            err_txt = self._last_error[:80]
            cv2.putText(img, f"last_err: {err_txt}", (20, 320),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (150, 150, 255), 1)
        return img

    def _placeholder_jpeg(self, message, sub_message='', quality=60,
                          color=(30, 30, 90)):
        img = self._make_placeholder(message, sub_message, color=color)
        try:
            ret, jpeg = cv2.imencode('.jpg', img,
                                      [cv2.IMWRITE_JPEG_QUALITY, quality])
            if ret:
                return jpeg.tobytes()
        except Exception:
            pass
        return None

    # -------------------------------------------------------------------
    # MJPEG streaming
    # -------------------------------------------------------------------

    def _yield_jpeg(self, jpeg_bytes):
        return (b'--frame\r\n'
                b'Content-Type: image/jpeg\r\n\r\n'
                + jpeg_bytes + b'\r\n')

    def generate_mjpeg(self, quality=70, max_fps=15):
        """
        MJPEG generator for the plain live view. Never stops yielding —
        if no real frame is available, yields a placeholder instead.
        """
        interval = 1.0 / max_fps
        while True:
            try:
                frame = self.get_frame()
                if frame is None:
                    ph = self._placeholder_jpeg(
                        'WAITING FOR CAMERA',
                        'Capture thread has not produced a frame yet.')
                    if ph:
                        yield self._yield_jpeg(ph)
                    time.sleep(interval)
                    continue

                try:
                    ret, jpeg = cv2.imencode(
                        '.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
                    if ret:
                        yield self._yield_jpeg(jpeg.tobytes())
                except Exception:
                    pass
            except GeneratorExit:
                return
            except Exception:
                logger.exception("mjpeg generator error")
            time.sleep(interval)

    def generate_mjpeg_with_aruco(self, quality=70, max_fps=10):
        """
        MJPEG stream with live ArUco marker overlay. Drops to 10fps (from
        the default MJPEG stream's 15) to leave headroom for the sweep,
        which does its own detection in parallel.

        Never stops yielding — status badges for camera health are drawn
        on every frame, and placeholder frames are yielded when the
        capture thread is stalled or dead.
        """
        from web_calibration import calibrator
        interval = 1.0 / max_fps
        last_frame_count = -1
        stall_start = None

        while True:
            try:
                # Pick up the current frame + frame count atomically
                with self._lock:
                    frame = self._frame.copy() if self._frame is not None else None
                    frame_count = self._frame_count

                now = time.time()

                # Case A: no frame at all yet
                if frame is None:
                    ph = self._placeholder_jpeg(
                        'WAITING FOR CAMERA',
                        f'health={self._health}')
                    if ph:
                        yield self._yield_jpeg(ph)
                    time.sleep(interval)
                    continue

                # Case B: frame counter hasn't moved since last yield
                if frame_count == last_frame_count:
                    if stall_start is None:
                        stall_start = now
                    stalled_for = now - stall_start
                    if stalled_for > 1.5:
                        # Stream has been showing the same frame for too
                        # long. Yield a placeholder so the browser knows.
                        ph = self._placeholder_jpeg(
                            'CAMERA STALLED',
                            f'No new frames for {stalled_for:.1f}s',
                            color=(0, 40, 120))
                        if ph:
                            yield self._yield_jpeg(ph)
                        time.sleep(interval)
                        continue
                else:
                    stall_start = None
                    last_frame_count = frame_count

                # Case C: we have a fresh frame — detect + annotate.
                output = frame
                markers = []
                try:
                    markers = calibrator.detect_markers(frame)
                    output = calibrator.draw_markers_on_frame(frame, markers)
                except Exception as det_err:
                    try:
                        cv2.rectangle(output, (8, 8), (420, 38),
                                       (0, 0, 0), -1)
                        cv2.putText(output, f"Detect err: {det_err}",
                                    (14, 30), cv2.FONT_HERSHEY_SIMPLEX,
                                    0.5, (0, 0, 255), 1)
                    except Exception:
                        pass

                # Count badge (top-left)
                try:
                    count_text = f"Markers: {len(markers)}"
                    cv2.rectangle(output, (8, 8), (200, 38), (0, 0, 0), -1)
                    cv2.putText(output, count_text, (14, 30),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                                (0, 255, 0) if markers else (180, 180, 180),
                                2)
                except Exception:
                    pass

                # Health badge (top-right) — makes freezes diagnosable at a glance
                try:
                    h, w = output.shape[:2]
                    badge_color = {
                        'ok': (0, 200, 0),
                        'stalled': (0, 165, 255),
                        'dead': (0, 0, 200),
                        'unknown': (120, 120, 120),
                    }.get(self._health, (120, 120, 120))
                    health_text = f"{self._health} {self._fps:.0f}fps"
                    tw = len(health_text) * 11
                    cv2.rectangle(output, (w - tw - 20, 8),
                                   (w - 8, 38), (0, 0, 0), -1)
                    cv2.putText(output, health_text, (w - tw - 14, 30),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                                badge_color, 2)
                except Exception:
                    pass

                try:
                    ret, jpeg = cv2.imencode(
                        '.jpg', output, [cv2.IMWRITE_JPEG_QUALITY, quality])
                    if ret:
                        yield self._yield_jpeg(jpeg.tobytes())
                except Exception:
                    pass
            except GeneratorExit:
                return
            except Exception as e:
                # Last-resort safety net. Never let the stream die.
                logger.error(f"aruco mjpeg error: {e}")
                try:
                    ph = self._placeholder_jpeg(
                        'STREAM ERROR', str(e)[:60])
                    if ph:
                        yield self._yield_jpeg(ph)
                except Exception:
                    pass
            time.sleep(interval)

    # -------------------------------------------------------------------
    # Status
    # -------------------------------------------------------------------

    def get_status(self):
        """Return camera status dict (used by /api/camera/status)."""
        since_frame = (time.time() - self._last_good_frame_time
                       if self._last_good_frame_time else None)
        return {
            "active": self.is_active,
            "device_index": self.device_index,
            "frame_count": self._frame_count,
            "fps": round(self._fps, 1),
            "health": self._health,
            "reconnects": self._reconnect_count,
            "read_failures": self._read_failures,
            "seconds_since_frame": (round(since_frame, 2)
                                    if since_frame is not None else None),
            "last_error": self._last_error,
        }


# Module-level singleton
camera = CameraManager()
