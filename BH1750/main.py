# ===================== 1. Imports =====================
import network
import time
import ujson
try:
    import uasyncio as asyncio
except ImportError:
    import asyncio

from collections import deque
from machine import Pin, I2C
from microdot import Microdot, send_file

# ===================== 2. Global configuration =====================
AP_SSID = 'PicoW-BH1750'
AP_PASSWORD = '12345678'
AP_IP = '192.168.4.1'

# ------------------- BH1750 wiring (I2C0) ---------------------------------
#   BH1750 VCC  -> 3V3
#   BH1750 GND  -> GND
#   BH1750 SCL  -> GP17
#   BH1750 SDA  -> GP16
#   BH1750 ADDR -> GND (0x23)  or  VCC (0x5C)
I2C_ID      = 0
I2C_SDA_PIN = 16
I2C_SCL_PIN = 17
I2C_FREQ    = 400000

BH1750_ADDR_LOW  = 0x23
BH1750_ADDR_HIGH = 0x5C

# BH1750 instruction set
BH1750_POWER_DOWN  = 0x00
BH1750_POWER_ON    = 0x01
BH1750_RESET       = 0x07
BH1750_CONT_H_RES  = 0x10   # continuous, 1 lx, ~120 ms
BH1750_CONT_H_RES2 = 0x11   # continuous, 0.5 lx, ~120 ms
BH1750_CONT_L_RES  = 0x13   # continuous, 4 lx, ~16 ms
BH1750_ONE_H_RES   = 0x20   # one-shot, 1 lx
BH1750_ONE_H_RES2  = 0x21   # one-shot, 0.5 lx
BH1750_ONE_L_RES   = 0x23   # one-shot, 4 lx

# ------------------- Sensor mode (higher sampling rate) --------------------
# Continuous L-res mode refreshes the register every ~16 ms (~60 Hz),
# which is what allows a real high-rate stream.  The previous default
# (continuous H-res) refreshes only every ~120 ms (~8 Hz).
# If 1 lx resolution is more important than speed, change this back to
# BH1750_CONT_H_RES and increase MEASUREMENT_DELAY_MS to 180.
SENSOR_MODE          = BH1750_CONT_L_RES
MEASUREMENT_DELAY_MS = 30    # settling time after mode change / power-on

# Conversion (both H-res and L-res): lux = raw / 1.2
BH1750_LUX_DIVISOR = 1.2

# ------------------- Background sampling -----------------------------------
# Poll the sensor at this cadence; the effective update rate is bounded
# by the sensor's own refresh time (SENSOR_MODE above).  20 ms keeps the
# HTTP endpoint responsive without hammering the I2C bus.
SAMPLE_INTERVAL_MS = 20

# ------------------- Calibration -------------------------------------------
CAL_SAMPLES         = 5      # samples averaged during a calibration run
CAL_SAMPLE_DELAY_MS = 20     # wait between calibration samples (>= refresh)

# ------------------- Calibration state -------------------------------------
calib_offset = 0.0           # lux offset subtracted from every reading
calibrated   = False         # True once the user has run Calibrate

# ------------------- LED ---------------------------------------------------
led_pin   = None
led_state = False

# ------------------- Sensor / history --------------------------------------
i2c = None
bh_addr = None

# Bounded deque: append() automatically evicts the oldest item when full,
# avoiding list re-allocation churn on long runs.
lux_history = deque((), 300)
MAX_HISTORY = 300

# ------------------- Acquisition state -------------------------------------
collecting = False

# ------------------- Background sampler state ------------------------------
latest_lux  = None           # Newest filtered lux value
latest_time = None           # Timestamp of the newest sample (ms)
sampling_suspended = False   # Set while a calibration run owns the bus

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


# ===================== 5. BH1750 driver =====================
def init_sensor():
    """
    Scan the I2C bus, locate the BH1750 (0x23 or 0x5C), power it on
    and start continuous measurement in the mode configured above.
    Returns True on success, False otherwise.
    """
    global i2c, bh_addr

    i2c = I2C(I2C_ID, sda=Pin(I2C_SDA_PIN), scl=Pin(I2C_SCL_PIN), freq=I2C_FREQ)
    devices = i2c.scan()
    print("[I2C] Devices found:", [hex(d) for d in devices])

    if BH1750_ADDR_LOW in devices:
        bh_addr = BH1750_ADDR_LOW
    elif BH1750_ADDR_HIGH in devices:
        bh_addr = BH1750_ADDR_HIGH
    else:
        bh_addr = None
        print("[WARN] BH1750 not found - check SDA/SCL/ADDR wiring")
        return False

    try:
        i2c.writeto(bh_addr, bytes([BH1750_POWER_ON]))
        time.sleep_ms(10)
        i2c.writeto(bh_addr, bytes([SENSOR_MODE]))
        time.sleep_ms(MEASUREMENT_DELAY_MS)
    except Exception as e:
        print("[ERR] BH1750 init failed:", e)
        bh_addr = None
        return False

    mode_name = "L-res (~16 ms, 4 lx)" if SENSOR_MODE == BH1750_CONT_L_RES \
                else "H-res (~120 ms, 1 lx)"
    print("[OK] BH1750 initialized at 0x%02X (continuous %s)"
          % (bh_addr, mode_name))
    return True


def _bh_read_raw():
    """Read the 2-byte, MSB-first raw value from BH1750."""
    try:
        data = i2c.readfrom(bh_addr, 2)
        return (data[0] << 8) | data[1]
    except Exception as e:
        print("BH1750 read error:", e)
        return None


def _bh_read_lux():
    """One raw read converted to lux (offset not applied)."""
    raw = _bh_read_raw()
    if raw is None:
        return None
    return raw / BH1750_LUX_DIVISOR


def read_lux():
    """
    ★ Calibration function ★
    Current illuminance in lux, with the user offset applied:
        lux = raw / 1.2 - calib_offset
    Returns a float, or None on sensor error.
    """
    v = _bh_read_lux()
    if v is None:
        return None
    return v - calib_offset


def auto_init_check():
    """
    Read the sensor once at boot to confirm it responds.
    """
    if bh_addr is None:
        print("[INIT] BH1750 not initialized - skipping check")
        return False
    raw = _bh_read_raw()
    if raw is None:
        print("[INIT] BH1750 not responding")
        return False
    print("[INIT] BH1750 first reading: raw=%d  lux=%.2f"
          % (raw, raw / BH1750_LUX_DIVISOR))
    return True


# ===================== 6. Calibration =====================
async def calibrate_sensor(reference_lux, samples=CAL_SAMPLES):
    """
    ★ Calibration procedure (async) ★

    The user places the sensor under a light source whose intensity
    is known (e.g. measured with a reference lux meter) and enters
    that value.  We average several readings and store:

        calib_offset = mean(measured) - reference_lux

    Every subsequent reading subtracts calib_offset, so the displayed
    value matches the reference.  Enter 0 for relative-light mode
    (current ambient becomes the new zero).

    The background sampling task is suspended for the duration so
    both do not drive the I2C bus at the same time.
    """
    global calib_offset, calibrated, sampling_suspended

    if bh_addr is None:
        return None

    sampling_suspended = True
    try:
        vals = []
        for _ in range(samples):
            v = _bh_read_lux()
            if v is not None:
                vals.append(v)
            await asyncio.sleep_ms(CAL_SAMPLE_DELAY_MS)

        if len(vals) < 2:
            return None

        mean_lux = sum(vals) / len(vals)
        calib_offset = mean_lux - reference_lux
        calibrated = True

        result = {
            'ref_lux':   round(reference_lux, 2),
            'mean_lux':  round(mean_lux, 2),
            'offset':    round(calib_offset, 2),
            'final_lux': round(mean_lux - calib_offset, 2),
            'samples':   len(vals),
        }
        print("[CAL] Calibration done:", result)
        return result
    finally:
        sampling_suspended = False


# ===================== 7. History =====================
def update_lux_history(value):
    """Append to the bounded deque; auto-evicts when full."""
    if value is not None:
        lux_history.append(value)


def get_lux_history():
    return lux_history


# ===================== 7b. Background sampler =====================
async def sampling_task():
    """
    ★ Continuous high-rate sampler ★
    Polls the BH1750 at SAMPLE_INTERVAL_MS cadence and stores the
    newest reading in `latest_lux`.  While collecting, the value is
    also appended to the shared history.

    Because the sensor runs in continuous mode (L-res: refresh ~16 ms),
    each poll returns the latest conversion without any extra delay.
    """
    global latest_lux, latest_time

    while True:
        if sampling_suspended or bh_addr is None:
            await asyncio.sleep_ms(50)
            continue

        v = read_lux()
        if v is not None:
            latest_lux = v
            latest_time = time.ticks_ms()
            if collecting:
                update_lux_history(v)

        await asyncio.sleep_ms(SAMPLE_INTERVAL_MS)


# ===================== 8. Web routes =====================
def register_routes():
    """
    Register routes:
    - /              -> index.html
    - /lux           -> latest cached lux (JSON)
    - /history       -> full history (JSON array)
    - /start         -> start acquisition
    - /stop          -> pause acquisition
    - /status        -> current acquisition status
    - /tare          -> reset calibration offset to 0 (absolute mode)
    - /calibrate     -> run calibration (optional ?ref=500)
    - /static/<path> -> static files (e.g. chart.js)
    """

    @app.route('/')
    async def index(request):
        return send_file('index.html')

    @app.route('/lux')
    async def get_current_lux(request):
        """
        Return the newest value produced by the background sampling task.
        No I2C read here, so the HTTP round trip stays short and the
        sampling rate is not throttled by the browser.
        """
        v = latest_lux
        ts = latest_time if latest_time is not None else time.ticks_ms()
        if v is None:
            v = 0.0
        return (ujson.dumps({'lux': v, 'time': ts,
                             'collecting': collecting,
                             'calibrated': calibrated}), 200, JSON_HEADER)

    @app.route('/history')
    async def get_history(request):
        return ujson.dumps(list(lux_history)), 200, JSON_HEADER

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
                             'count':      len(lux_history),
                             'offset':     round(calib_offset, 2),
                             'calibrated': calibrated}),
                200, JSON_HEADER)

    @app.route('/tare')
    async def tare_route(request):
        """Reset the calibration offset to zero (absolute-light mode)."""
        global calib_offset, calibrated
        calib_offset = 0.0
        calibrated = False
        print("[TARE] Offset reset to 0 (absolute mode)")
        return (ujson.dumps({'ok': True, 'offset': 0.0}),
                200, JSON_HEADER)

    @app.route('/calibrate')
    async def calibrate_route(request):
        """
        Run light calibration.
        Usage: /calibrate?ref=500
        ref = reference illuminance in lux. Default 0.
        """
        ref_lux = 0.0
        raw = None
        try:
            if request.args:
                raw = request.args.get('ref')
        except Exception:
            raw = None

        if raw is not None:
            try:
                ref_lux = float(raw)
            except (ValueError, TypeError):
                return (ujson.dumps({'ok': False,
                                     'msg': 'Invalid reference lux value'}),
                        200, JSON_HEADER)

        if ref_lux < 0.0 or ref_lux > 200000.0:
            return (ujson.dumps({'ok': False,
                                 'msg': 'Reference lux out of range (0..200000)'}),
                    200, JSON_HEADER)

        result = await calibrate_sensor(ref_lux)
        if result is None:
            return (ujson.dumps({'ok': False,
                                 'msg': 'BH1750 not responding - check wiring'}),
                    200, JSON_HEADER)

        result['ok'] = True
        return ujson.dumps(result), 200, JSON_HEADER

    @app.route('/static/<path:path>')
    async def static_file(request, path):
        if '..' in path:
            return 'Not found', 404
        return send_file('static/' + path, max_age=86400)

    print("[OK] Routes registered (lux + tare + control + calibration + static)")


# ===================== 9. Web server =====================================
async def _serve():
    """Run the background sampler and the Microdot server together."""
    asyncio.create_task(sampling_task())
    await app.start_server(host='0.0.0.0', port=80, debug=False)


def start_web_server():
    print("[RUN] Starting web server (background sampling every %d ms)..."
          % SAMPLE_INTERVAL_MS)
    asyncio.run(_serve())


# ===================== 10. Main ==========================================
def main():
    print("\n[BOOT] Pico W BH1750 Light Monitoring System")

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
    led_blink(3, 0.3, 0.3)
    led_off()
    print("[OK] System ready (LED on = collecting)")

    # 4. Initialize BH1750 and do a quick sanity read
    init_sensor()
    auto_init_check()

    # 5. Register web routes
    register_routes()

    # 6. Start web server + background sampler
    start_web_server()


# ===================== 11. Entry point ==================================
if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    finally:
        if led_pin:
            led_pin.off()
        print("Program stopped")