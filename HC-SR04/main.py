# ===================== 1. Imports =====================
import network          # WiFi
import time
import ujson
try:
    import uasyncio as asyncio       # MicroPython async
except ImportError:
    import asyncio

from machine import Pin, time_pulse_us   # GPIO and echo pulse timing
from microdot import Microdot, send_file

# ===================== 2. Global configuration =====================
AP_SSID = 'PicoW-Dist'        # WiFi AP SSID
AP_PASSWORD = '12345678'      # Password (>= 8 chars)
AP_IP = '192.168.4.1'         # Static IP

# ------------------- Ultrasonic sensor pins --------------------
TRIG_PIN = 16                  # GPIO for HC-SR04 TRIG
ECHO_PIN = 17                  # GPIO for HC-SR04 ECHO (use a divider!)

# ------------------- Distance calibration parameters --------------------
SOUND_SPEED_CM_PER_US = 0.0343   # Speed of sound in air at 20 degC (cm/us)
ECHO_TIMEOUT_US = 30000          # 30 ms -> about 5 m max range
CAL_SAMPLES = 16                 # Number of samples during calibration
CAL_INTERVAL_MS = 60             # Delay between calibration samples (ms)

calib_offset = 0.0            # Calibration offset (cm, internal), set by /calibrate

# ------------------- Filter parameters (noise / jitter suppression) ----
# 1) Median filter on raw distance: rejects isolated outliers (multi-path
#    echoes, cross-talk) without smearing genuine steps.
MEDIAN_WINDOW = 5             # Buffer length for the median filter
# 2) EMA on the median-filtered distance: removes residual jitter while
#    keeping the response quick. alpha -> 1.0 = no smoothing.
DIST_EMA_ALPHA = 0.35
# 3) Speed is estimated by differentiating the smoothed distance over a
#    window of SPEED_WINDOW samples (~100 ms at 20 ms sampling), which
#    averages out short-term noise that a per-sample derivative amplifies.
SPEED_WINDOW = 5              # Number of (time, distance) samples in window
# 4) EMA on the raw speed: tames the remaining fluctuation of the reading.
SPEED_EMA_ALPHA = 0.25

# ------------------- Unit conversion (SI output) --------------------
# All internal maths is done in centimetres (and cm/s) for numeric
# convenience; the web API exposes SI units: metres and metres/second.
CM_PER_M = 100.0

# ------------------- LED status (system indicator) --------------------
led_pin = None                # Onboard LED pin object
led_state = False             # True = on, False = off

# ------------------- Sensor objects and history --------------------
trig_pin = None               # TRIG pin object
echo_pin = None               # ECHO pin object

dist_history = []             # Distance history (cm, internal)
speed_history = []            # Speed history (cm/s, internal)
MAX_HISTORY = 300             # Maximum history length

# ------------------- Acquisition state --------------------
collecting = False            # True = collecting, False = paused
latest_dist = None            # Latest smoothed distance (cm, internal)
latest_speed = 0.0            # Latest smoothed speed (cm/s, internal)

# ------------------- Filter runtime state --------------------
dist_median_buf = []          # Ring buffer of recent raw distances
dist_ema = None               # EMA-smoothed distance (cm)
speed_ema = None              # EMA-smoothed speed (cm/s)
speed_buf = []                # Ring buffer of (timestamp_us, smoothed_dist)

SAMPLE_INTERVAL_MS = 20       # Background sampling period (ms) -> ~50 Hz

# ------------------- Web app --------------------
app = Microdot()
JSON_HEADER = {'Content-Type': 'application/json'}


# ===================== 3. WiFi helpers =====================
def start_wifi_ap():
    """
    Start WiFi in AP mode and return IP address.
    Raises an exception if the AP fails to start.
    """
    ap = network.WLAN(network.AP_IF)
    ap.active(False)          # Deactivate first to reset configuration
    time.sleep(0.2)
    ap.config(essid=AP_SSID, password=AP_PASSWORD, channel=6)
    ap.ifconfig((AP_IP, '255.255.255.0', AP_IP, AP_IP))
    ap.active(True)

    # Wait until the AP is fully up (timeout 10 s)
    start_time = time.time()
    while not ap.active():
        time.sleep(0.2)
        if time.time() - start_time > 10:
            raise RuntimeError("AP start timeout")

    ip = ap.ifconfig()[0]
    print("[OK] WiFi AP started")
    print("     SSID    : " + AP_SSID)
    print("     Password: " + AP_PASSWORD)
    print("     URL     : http://" + ip)
    return ip


# ===================== 4. LED helpers =====================
def init_led():
    """Initialize the onboard LED and turn it off."""
    global led_pin, led_state
    led_pin = Pin("LED", Pin.OUT)
    led_pin.off()
    led_state = False
    print("[OK] LED initialized")


def led_on():
    """Turn the LED on."""
    global led_pin, led_state
    if led_pin:
        led_pin.on()
        led_state = True


def led_off():
    """Turn the LED off."""
    global led_pin, led_state
    if led_pin:
        led_pin.off()
        led_state = False


def led_blink(count=3, on_time=0.2, off_time=0.2):
    """
    Blink the LED a given number of times.
    count   : number of blink cycles (on-off)
    on_time : on duration (s)
    off_time: off duration (s)
    """
    for _ in range(count):
        led_on()
        time.sleep(on_time)
        led_off()
        time.sleep(off_time)


# ===================== 5. Ultrasonic sensor =====================
def init_sensor():
    """Initialize TRIG (output) and ECHO (input) pins."""
    global trig_pin, echo_pin
    trig_pin = Pin(TRIG_PIN, Pin.OUT, value=0)
    echo_pin = Pin(ECHO_PIN, Pin.IN)
    print("[OK] Ultrasonic sensor initialized (TRIG=GP%d, ECHO=GP%d)"
          % (TRIG_PIN, ECHO_PIN))


def duration_to_distance(duration_us):
    """
    ★ Calibration function ★
    Echo pulse duration (us) -> distance (cm, internal).

    Formula:
        d = duration * SOUND_SPEED_CM_PER_US / 2
    where the division by 2 accounts for the round trip.

    To change the sensor curve (e.g. temperature-compensated speed
    of sound), edit this function or SOUND_SPEED_CM_PER_US above.
    """
    return duration_us * SOUND_SPEED_CM_PER_US / 2.0


def measure_distance_cm():
    """
    Trigger one ultrasonic measurement and return the raw distance
    in cm (WITHOUT the calibration offset). Internal use only.
    Returns None on timeout or error.
    """
    if trig_pin is None or echo_pin is None:
        return None
    try:
        # Send a 10 us pulse on TRIG
        trig_pin.low()
        time.sleep_us(2)
        trig_pin.high()
        time.sleep_us(10)
        trig_pin.low()

        # Measure the high-level width of the ECHO pulse
        duration = time_pulse_us(echo_pin, 1, ECHO_TIMEOUT_US)
        if duration < 0:
            return None

        return duration_to_distance(duration)
    except Exception as e:
        print("Distance read error:", e)
        return None


def read_distance_cm():
    """
    Read one distance measurement and apply the calibration offset
    (set by /calibrate). Returns a float (cm), or None on error.
    Internal use only -- the API layer converts to metres.
    """
    raw = measure_distance_cm()
    if raw is None:
        return None
    return round(raw + calib_offset, 2)


# ------------------- Filter helpers -------------------
def _median(buf):
    """Return the median of a small list (buf must be non-empty)."""
    s = sorted(buf)
    n = len(s)
    if n == 0:
        return None
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def reset_filters():
    """
    Clear all filter state. Called on boot and after calibration, so
    that the first sample after a scale change does not produce a
    spurious transient in distance or speed.
    """
    global dist_median_buf, dist_ema, speed_ema, speed_buf
    dist_median_buf = []
    dist_ema = None
    speed_ema = None
    speed_buf = []


def filter_distance(raw_cm):
    """
    Combine median filter (outlier rejection) and EMA (jitter
    smoothing) on a new raw distance sample. Returns the smoothed
    distance in cm, or None if the sample was rejected.
    """
    global dist_median_buf, dist_ema

    if raw_cm is None:
        return None

    dist_median_buf.append(raw_cm)
    if len(dist_median_buf) > MEDIAN_WINDOW:
        dist_median_buf.pop(0)
    d_med = _median(dist_median_buf)
    if d_med is None:
        return None

    if dist_ema is None:
        dist_ema = d_med
    else:
        dist_ema = DIST_EMA_ALPHA * d_med + (1.0 - DIST_EMA_ALPHA) * dist_ema

    return dist_ema


def compute_speed(smoothed_cm, now_us):
    """
    Estimate instantaneous speed (cm/s, internal) from a time series
    of smoothed distances.

    Instead of differentiating two adjacent samples (very sensitive
    to noise), we differentiate the oldest and the newest sample in
    a short window (SPEED_WINDOW points, ~100 ms). The EMA then
    removes the residual fluctuation of this estimate.
    Returns the smoothed speed in cm/s, or None if not enough data.
    """
    global speed_buf, speed_ema

    speed_buf.append((now_us, smoothed_cm))
    if len(speed_buf) > SPEED_WINDOW:
        speed_buf.pop(0)

    if len(speed_buf) < 2:
        return None

    t0, d0 = speed_buf[0]
    t1, d1 = speed_buf[-1]
    dt_s = time.ticks_diff(t1, t0) / 1000000.0
    if dt_s <= 0.001:
        return speed_ema if speed_ema is not None else 0.0

    v_raw = (d1 - d0) / dt_s       # cm/s

    if speed_ema is None:
        speed_ema = v_raw
    else:
        speed_ema = SPEED_EMA_ALPHA * v_raw + (1.0 - SPEED_EMA_ALPHA) * speed_ema

    return speed_ema


async def calibrate_sensor(reference_dist_m, samples=CAL_SAMPLES):
    """
    ★ Calibration procedure ★
    1. Take `samples` raw distance measurements, average them.
    2. Compute calib_offset = reference_dist - average.
       All subsequent readings will add this offset so that the
       displayed value equals the user-provided reference.

    reference_dist_m : true distance in METRES (SI) from the web UI
    samples          : number of samples (default CAL_SAMPLES)
    Returns a result dict (all values in metres), or None on failure.
    """
    global calib_offset

    if trig_pin is None or echo_pin is None:
        return None

    total = 0.0
    valid = 0
    for _ in range(samples):
        d = measure_distance_cm()
        if d is not None:
            total += d
            valid += 1
        await asyncio.sleep_ms(CAL_INTERVAL_MS)

    if valid == 0:
        return None

    avg_dist_cm = total / valid
    # Convert the SI reference (m) into the internal cm domain
    ref_dist_cm = reference_dist_m * CM_PER_M
    calib_offset = ref_dist_cm - avg_dist_cm

    # The distance scale just changed: drop all filter state so the
    # next samples restart cleanly (no artificial step in speed).
    reset_filters()

    result = {
        'ref_dist':   round(ref_dist_cm / CM_PER_M, 4),        # m
        'raw_dist':   round(avg_dist_cm / CM_PER_M, 4),        # m
        'offset':     round(calib_offset / CM_PER_M, 4),       # m
        'samples':    valid,
        'final_dist': round((avg_dist_cm + calib_offset) / CM_PER_M, 4),  # m
    }
    print("[CAL] Calibration done (SI):", result)
    return result


def update_dist_history(value, speed):
    """
    Append a new (distance, speed) pair, keeping the maximum length.
    Values are stored in the internal cm / cm/s domain.
    dist_history and speed_history are always kept the same length,
    so the two front-end charts stay index-aligned.
    """
    global dist_history, speed_history
    if value is None:
        return

    dist_history.append(value)
    speed_history.append(speed if speed is not None else 0.0)

    if len(dist_history) > MAX_HISTORY:
        dist_history.pop(0)
        speed_history.pop(0)


def get_dist_history():
    """Return the full distance history list (internal cm)."""
    return dist_history


def get_speed_history():
    """Return the full speed history list (internal cm/s)."""
    return speed_history


def _history_si():
    """
    Serialise the histories in SI units: metres and metres/second.
    Rounding is kept fine enough (0.1 mm / 0.1 mm/s) so no visible
    detail is lost on the charts or in the CSV export.
    """
    d = [round(v / CM_PER_M, 4) for v in dist_history]
    s = [round(v / CM_PER_M, 4) for v in speed_history]
    return d, s


# ===================== 5b. High-rate background sampler =====================
async def sampling_task():
    """
    ★ Fast sampling loop with filtering + speed estimation ★
    For every iteration:
      1. read the raw distance (with calibration offset applied)
      2. median filter  -> remove isolated outliers
      3. EMA filter     -> smooth residual jitter
      4. windowed diff  -> speed with EMA smoothing
      5. while collecting, push both values to the histories
    All values are kept in cm / cm/s internally and converted to
    SI (m, m/s) only at the API boundary.
    """
    global latest_dist, latest_speed

    while True:
        raw = read_distance_cm()
        now_us = time.ticks_us()

        d_sm = filter_distance(raw)
        if d_sm is not None:
            v_sm = compute_speed(d_sm, now_us)

            latest_dist = round(d_sm, 2)          # cm
            if v_sm is not None:
                latest_speed = v_sm               # cm/s

            if collecting:
                update_dist_history(latest_dist, round(latest_speed, 2))

        await asyncio.sleep_ms(SAMPLE_INTERVAL_MS)


# ===================== 6. Web routes =====================
def register_routes():
    """
    Register routes (all numeric payloads in SI units: m, m/s):
    - /              -> index.html
    - /dist          -> latest distance + speed (JSON)
    - /history       -> full history {"dist": [...], "speed": [...]}
    - /start         -> start acquisition
    - /stop          -> pause acquisition
    - /status        -> current acquisition status
    - /calibrate     -> run calibration (optional ?ref=0.5, in metres)
    - /static/<path> -> static files (e.g. chart.js)
    """

    # ---------- Home page ----------
    @app.route('/')
    async def index(request):
        return send_file('index.html')

    # ---------- Distance + speed endpoints ----------
    @app.route('/dist')
    async def get_current_dist(request):
        """
        Return the newest values produced by the background sampling
        task (no extra sensor read here, so the HTTP round trip stays
        short and the sampling rate is not throttled by the browser).
        Units: metres and metres/second.
        Format: {"dist": 0.425, "speed": -0.123, "time": 123456,
                 "collecting": true}
        """
        timestamp = time.ticks_ms()
        if latest_dist is None:
            dist_m = 0.0
        else:
            dist_m = round(latest_dist / CM_PER_M, 4)
        speed_ms = round(latest_speed / CM_PER_M, 4)

        return (ujson.dumps({'dist': dist_m,
                             'speed': speed_ms,
                             'time': timestamp,
                             'collecting': collecting}),
                200, JSON_HEADER)

    @app.route('/history')
    async def get_history(request):
        """Return the distance and speed history in SI units (m, m/s)."""
        d_si, s_si = _history_si()
        return (ujson.dumps({'dist': d_si, 'speed': s_si}),
                200, JSON_HEADER)

    # ---------- Acquisition control ----------
    @app.route('/start')
    async def start_collect(request):
        """Start acquisition: allow history writes, LED on."""
        global collecting
        collecting = True
        led_on()
        print("[RUN] Acquisition started")
        return ujson.dumps({'collecting': True}), 200, JSON_HEADER

    @app.route('/stop')
    async def stop_collect(request):
        """Pause acquisition: stop history writes, LED off."""
        global collecting
        collecting = False
        led_off()
        print("[PAUSE] Acquisition paused")
        return ujson.dumps({'collecting': False}), 200, JSON_HEADER

    @app.route('/status')
    async def get_status(request):
        """Return acquisition status, sample count and calibration offset (m)."""
        return (ujson.dumps({'collecting': collecting,
                             'count': len(dist_history),
                             'offset': round(calib_offset / CM_PER_M, 4)}),
                200, JSON_HEADER)

    # ---------- Calibration ----------
    @app.route('/calibrate')
    async def calibrate_route(request):
        """
        Run sensor calibration.
        Usage: /calibrate?ref=0.5
        ref is the true distance in METRES (SI). Default 0.5 m.
        """
        ref_dist_m = 0.5
        raw = None
        try:
            if request.args:
                raw = request.args.get('ref')
        except Exception:
            raw = None

        if raw is not None:
            try:
                ref_dist_m = float(raw)
            except (ValueError, TypeError):
                return (ujson.dumps({'ok': False, 'msg': 'Invalid reference distance'}),
                        200, JSON_HEADER)

        # Acceptable calibration range: 2 cm .. 4 m
        if ref_dist_m < 0.02 or ref_dist_m > 4.0:
            return (ujson.dumps({'ok': False,
                                 'msg': 'Reference distance out of range (0.02..4.0 m)'}),
                    200, JSON_HEADER)

        result = await calibrate_sensor(ref_dist_m)
        if result is None:
            return (ujson.dumps({'ok': False,
                                 'msg': 'No valid echo received during calibration'}),
                    200, JSON_HEADER)

        result['ok'] = True
        return ujson.dumps(result), 200, JSON_HEADER

    # ---------- Static files ----------
    @app.route('/static/<path:path>')
    async def static_file(request, path):
        """Serve files from the static/ directory (e.g. chart.js)."""
        if '..' in path:
            return 'Not found', 404
        return send_file('static/' + path, max_age=86400)

    print("[OK] Routes registered (SI units: m, m/s)")


# ===================== 7. Web server =====================
async def _serve():
    """Run the background sampler and the Microdot server together."""
    asyncio.create_task(sampling_task())
    await app.start_server(host='0.0.0.0', port=80, debug=False)


def start_web_server():
    """Start the web server + high-rate sampler (blocking)."""
    print("[RUN] Starting web server (sampling every %d ms)..."
          % SAMPLE_INTERVAL_MS)
    asyncio.run(_serve())


# ===================== 8. Main =====================
def main():
    print("\n[BOOT] Pico W Ultrasonic Distance & Speed Monitoring System (SI units)")

    # 1. Initialize LED (system indicator)
    init_led()

    # 2. Start WiFi AP
    try:
        ip = start_wifi_ap()
    except Exception as e:
        print("[ERR] WiFi start failed:", e)
        led_blink(10, 0.1, 0.1)
        return

    # 3. WiFi OK: blink three times, then keep LED off
    #    (LED on = acquisition running)
    led_blink(3, 0.3, 0.3)
    led_off()
    print("[OK] System ready (LED on = collecting)")

    # 4. Initialize ultrasonic sensor
    init_sensor()

    # 5. Make sure filters start clean
    reset_filters()

    # 6. Register web routes
    register_routes()

    # 7. Start web server + background sampler
    start_web_server()


# ===================== 9. Entry point =====================
if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    finally:
        if led_pin:
            led_pin.off()
        print("Program stopped")