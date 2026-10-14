# ===================== 1. Imports =====================
import network
import time
import ujson
try:
    import uasyncio as asyncio
except ImportError:
    import asyncio

from machine import Pin
from microdot import Microdot, send_file

# ===================== 2. Global configuration =====================
AP_SSID = 'PicoW-LoadCell'
AP_PASSWORD = '12345678'
AP_IP = '192.168.4.1'

# ------------------- HX711 wiring ------------------------------------------
#   HX711 VCC -> 3V3
#   HX711 GND -> GND
#   HX711 DT  -> GP16   (DOUT)
#   HX711 SCK -> GP17   (SCK)
HX711_DOUT_PIN = 16
HX711_SCK_PIN  = 17

# ------------------- Physical constants ------------------------------------
GRAVITY = 9.80665                # m/s^2, used only for mass->force conversion

# Fallback scale before a span calibration is performed (counts per Newton).
# A typical 1 kg load cell at 3.3 V excitation gives ~1.4e5 counts/N.
DEFAULT_SCALE = 100000.0

# ------------------- Filtering / sampling ----------------------------------
READ_MEDIAN_N = 3                # median-of-N sliding window on the raw stream

# Background sampling: the task calls hx.read_raw() back-to-back, so the
# effective rate is the native HX711 output rate (10 SPS or 80 SPS).
# SAMPLE_INTERVAL_MS is just a small cooperative yield between reads.
SAMPLE_INTERVAL_MS = 5

# ------------------- Web-triggered calibration (Calibrate button) ----------
CAL_SAMPLES = 40                 # raw samples per calibration run
CAL_DISCARD = 5                  # drop the first N (settling) samples

# ------------------- Auto tare (initial instruction calibration) -----------
AUTO_TARE_ENABLE     = True
AUTO_TARE_SAMPLES    = 40        # raw samples per attempt
AUTO_TARE_DISCARD    = 5         # drop the first N (settling) samples
AUTO_TARE_STABLE_STD = 2000      # max std (counts) to accept the tare
AUTO_TARE_TRIES      = 5         # retries if the cell looks noisy

# ------------------- Zero tracking -----------------------------------------
# While NOT collecting, if the reading is inside the dead-band the zero
# offset is nudged toward zero by ZERO_TRACK_ALPHA of the residual.
ZERO_TRACK_ENABLE = True
ZERO_TRACK_BAND_N = 0.05         # dead-band (N)
ZERO_TRACK_ALPHA  = 0.05         # fraction of residual applied per update

# ------------------- Calibration state -------------------------------------
calib_offset = 0.0               # tare offset, in raw counts
calib_scale  = DEFAULT_SCALE     # counts per Newton
tared        = False

# ------------------- LED ---------------------------------------------------
led_pin   = None
led_state = False

# ------------------- Load cell / history -----------------------------------
hx = None
force_history = []
MAX_HISTORY = 300

# ------------------- Acquisition state -------------------------------------
collecting = False

# ------------------- Background sampler state ------------------------------
latest_force = None              # Newest filtered force value (N)
latest_time  = None              # Timestamp of the newest sample (ms)
raw_win_buf  = []                # Sliding window of recent raw counts
sampling_suspended = False       # Set while a calibration run owns the bus

# ------------------- Web app -----------------------------------------------
app = Microdot()
JSON_HEADER = {'Content-Type': 'application/json'}


# ===================== 3. WiFi AP =====================
def start_wifi_ap():
    ap = network.WLAN(network.AP_IF)
    ap.active(False)
    time.sleep(0.2)
    ap.config(essid=AP_SSID, password=AP_PASSWORD, channel=6)
    ap.ifconfig((AP_IP, '255.255.255.0', AP_IP, AP_IP))
    ap.active(True)

    t0 = time.time()
    while not ap.active():
        time.sleep(0.2)
        if time.time() - t0 > 10:
            raise RuntimeError("AP start timeout")

    ip = ap.ifconfig()[0]
    print("[OK] WiFi AP started")
    print("     SSID    : " + AP_SSID)
    print("     Password: " + AP_PASSWORD)
    print("     URL     : http://" + ip)
    return ip


# ===================== 4. LED helpers =====================
def init_led():
    global led_pin, led_state
    led_pin = Pin("LED", Pin.OUT)
    led_pin.off()
    led_state = False
    print("[OK] LED initialized")


def led_on():
    global led_pin, led_state
    if led_pin:
        led_pin.on()
        led_state = True


def led_off():
    global led_pin, led_state
    if led_pin:
        led_pin.off()
        led_state = False


def led_blink(count=3, on_time=0.2, off_time=0.2):
    for _ in range(count):
        led_on()
        time.sleep(on_time)
        led_off()
        time.sleep(off_time)


# ===================== 5. HX711 driver =====================
class HX711:
    """
    Bit-banged HX711 driver (channel A, gain 128).
    The very first conversion after power-up / first call is stale,
    so it is discarded automatically before the first real value.
    """

    def __init__(self, dout_pin, sck_pin):
        self.dout = Pin(dout_pin, Pin.IN)
        self.sck  = Pin(sck_pin, Pin.OUT)
        self.sck.value(0)
        self._warmed_up = False

    def _wait_ready(self, timeout_ms=250):
        t0 = time.ticks_ms()
        while self.dout.value() == 1:
            if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms:
                return False
            time.sleep_us(20)
        return True

    def _read_once(self, timeout_ms=250):
        if not self._wait_ready(timeout_ms):
            return None

        value = 0
        for _ in range(24):
            self.sck.value(1)
            time.sleep_us(1)
            value = (value << 1) | self.dout.value()
            self.sck.value(0)
            time.sleep_us(1)

        # 25th pulse -> next conversion uses channel A, gain 128
        self.sck.value(1)
        time.sleep_us(1)
        self.sck.value(0)
        time.sleep_us(1)

        if value & 0x800000:
            value -= 0x1000000
        return value

    def read_raw(self, timeout_ms=250):
        # Discard the very first (stale) conversion once.
        if not self._warmed_up:
            self._read_once(timeout_ms)
            self._warmed_up = True
        return self._read_once(timeout_ms)


def init_load_cell():
    global hx
    hx = HX711(HX711_DOUT_PIN, HX711_SCK_PIN)
    print("[OK] HX711 initialized (DOUT=GP%d, SCK=GP%d)"
          % (HX711_DOUT_PIN, HX711_SCK_PIN))

    raw = hx.read_raw()
    if raw is None:
        print("[WARN] HX711 not responding - check wiring / power")
    else:
        print("[OK] HX711 first raw reading:", raw)


# ===================== 6. Signal processing =====================
def _collect_raw(samples, discard=0):
    """Collect raw samples synchronously, drop the first `discard`."""
    vals = []
    for _ in range(samples):
        v = hx.read_raw()
        if v is not None:
            vals.append(v)
    if discard and len(vals) > discard:
        vals = vals[discard:]
    return vals


def _trimmed_mean(vals, trim_ratio=0.10):
    """Mean after trimming trim_ratio from both tails (sorted)."""
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    lo = int(n * trim_ratio)
    hi = n - lo
    core = s[lo:hi] if hi > lo else s
    return sum(core) / len(core)


def _std(vals, mean):
    if not vals:
        return 0.0
    var = sum((x - mean) ** 2 for x in vals) / len(vals)
    return var ** 0.5


def counts_to_force(counts):
    """
    ★ Calibration function ★
    Raw counts -> force (Newtons).
        F = (counts - calib_offset) / calib_scale
    """
    scale = calib_scale if calib_scale else DEFAULT_SCALE
    return (counts - calib_offset) / scale


# ===================== 7. Auto tare (initial instruction cal.) =============
def auto_tare(samples=AUTO_TARE_SAMPLES,
              discard=AUTO_TARE_DISCARD,
              stable_std=AUTO_TARE_STABLE_STD,
              tries=AUTO_TARE_TRIES):
    """
    ★ Initial instruction calibration (auto-tare) ★

    Assumes the cell is UNLOADED at boot.  Repeatedly samples the raw
    counts; if the trimmed mean is stable enough (std < stable_std)
    that mean becomes the zero offset, so the display reports ~0.00 N.

    Returns True on success, False otherwise.
    """
    global calib_offset, tared

    if hx is None:
        print("[TARE] HX711 not initialized - skipping auto-tare")
        return False

    print("[TARE] Auto-tare started (assume cell is unloaded)...")

    for attempt in range(1, tries + 1):
        vals = _collect_raw(samples, discard=discard)
        if len(vals) < 10:
            print("[TARE] attempt %d/%d: no response from HX711"
                  % (attempt, tries))
            time.sleep(0.3)
            continue

        avg = _trimmed_mean(vals, trim_ratio=0.10)
        if avg is None:
            continue
        std = _std(vals, avg)

        print("[TARE] attempt %d/%d  avg=%.0f  std=%.0f  n=%d"
              % (attempt, tries, avg, std, len(vals)))

        if std < stable_std:
            calib_offset = avg
            tared = True
            print("[TARE] OK - zero offset = %.0f counts" % calib_offset)
            return True

        time.sleep(0.3)

    print("[TARE] Unstable after %d tries - keeping offset %.0f"
          % (tries, calib_offset))
    return False


# ===================== 8. Web-triggered calibration ========================
async def calibrate_sensor(reference_mass_g, samples=CAL_SAMPLES):
    """
    ★ Calibration procedure (async) ★

    reference_mass_g == 0  -> TARE: store the trimmed mean as offset
    reference_mass_g  > 0  -> SPAN: convert the reference mass to Newtons
                              and set calib_scale = counts / N

    Uses CAL_SAMPLES samples, drops the first CAL_DISCARD, trims 10%
    from each tail.  Yields to the event loop so the web UI stays
    responsive.

    The background sampling task is suspended for the duration of the
    calibration so both do not drive the HX711 bus at the same time.
    """
    global calib_offset, calib_scale, tared, sampling_suspended

    if hx is None:
        return None

    sampling_suspended = True
    try:
        # ---- gather raw samples, yielding between reads ----
        vals = []
        for _ in range(samples):
            v = hx.read_raw()
            if v is not None:
                vals.append(v)
            await asyncio.sleep_ms(5)

        if len(vals) < 10:
            return None

        if len(vals) > CAL_DISCARD:
            vals = vals[CAL_DISCARD:]

        avg_counts = _trimmed_mean(vals, trim_ratio=0.10)
        if avg_counts is None:
            return None

        # Snapshot before modifying state
        old_offset = calib_offset
        old_scale  = calib_scale if calib_scale else DEFAULT_SCALE
        raw_force  = (avg_counts - old_offset) / old_scale

        if abs(reference_mass_g) < 1e-6:
            # ----- TARE -----
            calib_offset = avg_counts
            if calib_scale <= 0:
                calib_scale = DEFAULT_SCALE
            tared = True
            final_force = 0.0
            ref_force = 0.0
        else:
            # ----- SPAN -----
            ref_force = reference_mass_g * GRAVITY / 1000.0   # g -> N
            delta = avg_counts - calib_offset
            if abs(delta) < 1e-9 or ref_force == 0:
                return {
                    'ok': False,
                    'msg': 'No signal change - tare first, then apply the load.',
                    'ref_mass': round(reference_mass_g, 2),
                    'ref_force': 0.0,
                    'counts': avg_counts,
                    'raw_force': round(raw_force, 4),
                    'offset': calib_offset,
                    'scale': calib_scale,
                    'final_force': 0.0,
                    'samples': len(vals),
                }
            calib_scale = delta / ref_force
            final_force = ref_force

        result = {
            'ref_mass':    round(reference_mass_g, 2),
            'ref_force':   round(ref_force, 4),
            'counts':      avg_counts,
            'raw_force':   round(raw_force, 4),
            'offset':      calib_offset,
            'scale':       calib_scale,
            'final_force': round(final_force, 4),
            'samples':     len(vals),
        }
        print("[CAL] Calibration done:", result)
        return result
    finally:
        # Make sure the flag is cleared even if an exception occurs.
        sampling_suspended = False
        # Drop the pre-calibration raw buffer so stale counts (taken
        # with the old offset/scale) do not bleed into the new stream.
        raw_win_buf.clear()


# ===================== 9. History =========================================
def update_force_history(value):
    global force_history
    if value is not None:
        force_history.append(value)
        if len(force_history) > MAX_HISTORY:
            force_history.pop(0)


def get_force_history():
    return force_history


# ===================== 9b. High-rate background sampler ====================
async def sampling_task():
    """
    ★ Continuous high-rate sampler ★
    Reads the HX711 back-to-back (as fast as the module's native output
    rate allows), maintains a sliding-window median over the last
    READ_MEDIAN_N raw counts for jitter rejection, applies the
    calibration curve, optional slow zero tracking, and stores the
    result in `latest_force`.  While collecting, the value is also
    appended to the shared history.

    A small `asyncio.sleep_ms(SAMPLE_INTERVAL_MS)` at the end of each
    iteration yields to the event loop so HTTP requests stay responsive.
    """
    global latest_force, latest_time, calib_offset

    while True:
        if sampling_suspended:
            # A calibration run currently owns the HX711 bus.
            await asyncio.sleep_ms(20)
            continue

        if hx is None:
            await asyncio.sleep_ms(100)
            continue

        raw = hx.read_raw()
        if raw is not None:
            # Sliding-window median on the raw counts
            raw_win_buf.append(raw)
            if len(raw_win_buf) > READ_MEDIAN_N:
                raw_win_buf.pop(0)
            s = sorted(raw_win_buf)
            med = s[len(s) // 2]

            force = counts_to_force(med)

            # ----- gentle zero tracking (only when not collecting) -----
            if (ZERO_TRACK_ENABLE and not collecting
                    and abs(force) < ZERO_TRACK_BAND_N):
                sc = calib_scale if calib_scale else DEFAULT_SCALE
                calib_offset += force * sc * ZERO_TRACK_ALPHA
                force = counts_to_force(med)

            latest_force = force
            latest_time = time.ticks_ms()

            if collecting:
                update_force_history(force)

        await asyncio.sleep_ms(SAMPLE_INTERVAL_MS)


# ===================== 10. Web routes =====================================
def register_routes():

    @app.route('/')
    async def index(request):
        return send_file('index.html')

    @app.route('/force')
    async def get_current_force(request):
        """
        Return the newest value produced by the background sampling task.
        No extra sensor read here, so the HTTP round trip stays short and
        the sampling rate is not throttled by the browser.
        """
        f = latest_force
        ts = latest_time if latest_time is not None else time.ticks_ms()
        if f is None:
            f = 0.0
        return (ujson.dumps({'force': f, 'time': ts,
                             'collecting': collecting,
                             'tared': tared}), 200, JSON_HEADER)

    @app.route('/history')
    async def get_history(request):
        return ujson.dumps(get_force_history()), 200, JSON_HEADER

    @app.route('/start')
    async def start_collect(request):
        global collecting
        collecting = True
        led_on()
        print("[RUN] Acquisition started")
        return ujson.dumps({'collecting': True}), 200, JSON_HEADER

    @app.route('/stop')
    async def stop_collect(request):
        global collecting
        collecting = False
        led_off()
        print("[PAUSE] Acquisition paused")
        return ujson.dumps({'collecting': False}), 200, JSON_HEADER

    @app.route('/status')
    async def get_status(request):
        return (ujson.dumps({'collecting': collecting,
                             'count':  len(force_history),
                             'offset': round(calib_offset, 2),
                             'scale':  round(calib_scale, 4),
                             'tared':  tared}),
                200, JSON_HEADER)

    @app.route('/tare')
    async def tare_route(request):
        """Convenience route: re-run automatic tare (no reference input)."""
        result = await calibrate_sensor(0.0)
        if result is None:
            return (ujson.dumps({'ok': False,
                                 'msg': 'HX711 not responding - check wiring'}),
                    200, JSON_HEADER)
        result['ok'] = True
        return ujson.dumps(result), 200, JSON_HEADER

    @app.route('/calibrate')
    async def calibrate_route(request):
        ref_mass = 0.0
        raw = None
        try:
            if request.args:
                raw = request.args.get('ref')
        except Exception:
            raw = None

        if raw is not None:
            try:
                ref_mass = float(raw)
            except (ValueError, TypeError):
                return (ujson.dumps({'ok': False,
                                     'msg': 'Invalid reference mass'}),
                        200, JSON_HEADER)

        if ref_mass < -1000000.0 or ref_mass > 1000000.0:
            return (ujson.dumps({'ok': False,
                                 'msg': 'Reference mass out of range (-1e6..1e6 g)'}),
                    200, JSON_HEADER)

        result = await calibrate_sensor(ref_mass)
        if result is None:
            return (ujson.dumps({'ok': False,
                                 'msg': 'HX711 not responding - check wiring'}),
                    200, JSON_HEADER)

        if result.get('ok') is False:
            return ujson.dumps(result), 200, JSON_HEADER

        result['ok'] = True
        return ujson.dumps(result), 200, JSON_HEADER

    @app.route('/static/<path:path>')
    async def static_file(request, path):
        if '..' in path:
            return 'Not found', 404
        return send_file('static/' + path, max_age=86400)

    print("[OK] Routes registered (force + tare + control + calibration + static)")


# ===================== 11. Web server =====================================
async def _serve():
    """Run the background sampler and the Microdot server together."""
    asyncio.create_task(sampling_task())
    await app.start_server(host='0.0.0.0', port=80, debug=False)


def start_web_server():
    print("[RUN] Starting web server (continuous sampling)...")
    asyncio.run(_serve())


# ===================== 12. Main ===========================================
def main():
    print("\n[BOOT] Pico W Load Cell Monitoring System")

    init_led()

    try:
        ip = start_wifi_ap()
    except Exception as e:
        print("[ERR] WiFi start failed:", e)
        led_blink(10, 0.1, 0.1)
        return

    led_blink(3, 0.3, 0.3)
    led_off()
    print("[OK] System ready (LED on = collecting)")

    init_load_cell()

    # ★ Automatic initial instruction calibration (auto-tare) ★
    # Runs synchronously, before the sampling task starts, so there is
    # no contention on the HX711 bus during this step.
    if AUTO_TARE_ENABLE:
        auto_tare()
    else:
        print("[TARE] Auto-tare disabled by config")

    register_routes()
    start_web_server()


# ===================== 13. Entry point ====================================
if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    finally:
        if led_pin:
            led_pin.off()
        print("Program stopped")