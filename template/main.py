# ===================== 1. Imports =====================
import network          # WiFi
import time
import ujson
try:
    import uasyncio as asyncio       # MicroPython async
except ImportError:
    import asyncio

from machine import Pin, ADC         # GPIO and ADC
from microdot import Microdot, send_file

# ===================== 2. Global configuration =====================
AP_SSID = 'PicoW-Temp'        # WiFi AP SSID
AP_PASSWORD = '12345678'      # Password (>= 8 chars)
AP_IP = '192.168.4.1'         # Static IP

# ------------------- Temperature calibration parameters --------------------
ADC_VREF = 3.3                # ADC reference voltage (V)
CAL_REF_TEMP = 27.0           # Reference temperature (degC)
CAL_REF_VOLTAGE = 0.706       # Reference voltage at CAL_REF_TEMP (V)
CAL_SLOPE = 0.001721          # Temperature slope (V/degC)
CAL_SAMPLES = 64              # Number of samples during calibration

calib_offset = 0.0            # Calibration offset (degC), set by /calibrate

# ------------------- LED status (system indicator) --------------------
led_pin = None                # Onboard LED pin object
led_state = False             # True = on, False = off

# ------------------- Temperature sensor --------------------
temp_adc = None               # ADC object
temp_history = []             # Temperature history (degC)
MAX_HISTORY = 300             # Maximum history length

# ------------------- Acquisition state --------------------
collecting = False            # True = collecting, False = paused

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


# ===================== 5. Temperature sensor =====================
def init_temp_sensor():
    """Initialize the onboard temperature sensor (ADC channel 4)."""
    global temp_adc
    temp_adc = ADC(4)          # Onboard sensor uses ADC channel 4
    print("[OK] Temperature sensor initialized")


def adc_to_voltage(adc_value):
    """
    Convert raw ADC value to voltage (V).
    adc_value: 0 .. 65535
    """
    return (adc_value / 65535.0) * ADC_VREF


def voltage_to_temperature(voltage):
    """
    ★ Calibration function ★
    Voltage (V) -> Temperature (degC).

    Formula:
        T = CAL_REF_TEMP - (V - CAL_REF_VOLTAGE) / CAL_SLOPE
    Equivalent to: T = 27.0 - (voltage - 0.706) / 0.001721

    To change the sensor curve, edit this function or the
    calibration constants above.
    """
    return CAL_REF_TEMP - (voltage - CAL_REF_VOLTAGE) / CAL_SLOPE


def read_temperature_celsius():
    """
    Read ADC and convert to degC via the calibration function,
    then apply the calibration offset set by /calibrate.
    Returns a float (degC), or None on error.
    """
    if temp_adc is None:
        return None
    try:
        adc_value = temp_adc.read_u16()             # 0 .. 65535
        voltage = adc_to_voltage(adc_value)
        temperature = voltage_to_temperature(voltage) + calib_offset
        return round(temperature, 2)
    except Exception as e:
        print("Temperature read error:", e)
        return None


async def calibrate_sensor(reference_temp, samples=CAL_SAMPLES):
    """
    ★ Calibration procedure ★
    1. Sample the ADC `samples` times and average the voltage.
    2. Convert to raw temperature via voltage_to_temperature().
    3. Compute calib_offset = reference_temp - raw_temp.
       All subsequent readings will add this offset so that the
       displayed value equals the user-provided reference.

    reference_temp : true ambient temperature (degC) from the web UI
    samples        : number of samples (default CAL_SAMPLES)
    Returns a result dict, or None if the sensor is not initialized.
    """
    global calib_offset

    if temp_adc is None:
        return None

    total_voltage = 0.0
    for _ in range(samples):
        total_voltage += adc_to_voltage(temp_adc.read_u16())
        await asyncio.sleep_ms(10)          # yield CPU to keep web responsive

    avg_voltage = total_voltage / samples
    raw_temp = voltage_to_temperature(avg_voltage)
    calib_offset = reference_temp - raw_temp

    result = {
        'ref_temp': round(reference_temp, 2),
        'voltage': round(avg_voltage, 4),
        'raw_temp': round(raw_temp, 2),
        'offset': round(calib_offset, 4),
        'samples': samples,
        'final_temp': round(raw_temp + calib_offset, 2),
    }
    print("[CAL] Calibration done:", result)
    return result


def update_temp_history(value):
    """Append a new temperature value, keeping the maximum length."""
    global temp_history
    if value is not None:
        temp_history.append(value)
        if len(temp_history) > MAX_HISTORY:
            temp_history.pop(0)


def get_temp_history():
    """Return the full temperature history list."""
    return temp_history


# ===================== 6. Web routes =====================
def register_routes():
    """
    Register routes:
    - /              -> index.html
    - /temp          -> latest temperature (JSON), appended to history only when collecting
    - /history       -> full history (JSON array)
    - /start         -> start acquisition
    - /stop          -> pause acquisition
    - /status        -> current acquisition status
    - /calibrate     -> run calibration (optional ?ref=25.0)
    - /static/<path> -> static files (e.g. chart.js)
    """

    # ---------- Home page ----------
    @app.route('/')
    async def index(request):
        return send_file('index.html')

    # ---------- Temperature endpoints ----------
    @app.route('/temp')
    async def get_current_temp(request):
        """
        Read current temperature:
        - when collecting: append to history and return it
        - when paused   : return instantaneous value without appending
        Format: {"temp": 25.5, "time": 123456, "collecting": true}
        """
        temp = read_temperature_celsius()
        timestamp = time.ticks_ms()
        if collecting and temp is not None:
            update_temp_history(temp)
        if temp is None:
            temp = 0.0
        return (ujson.dumps({'temp': temp, 'time': timestamp,
                             'collecting': collecting}), 200, JSON_HEADER)

    @app.route('/history')
    async def get_history(request):
        """Return the temperature history (JSON array)."""
        return ujson.dumps(get_temp_history()), 200, JSON_HEADER

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
        """Return acquisition status, sample count and calibration offset."""
        return (ujson.dumps({'collecting': collecting,
                             'count': len(temp_history),
                             'offset': round(calib_offset, 4)}),
                200, JSON_HEADER)

    # ---------- Calibration ----------
    @app.route('/calibrate')
    async def calibrate_route(request):
        """
        Run sensor calibration.
        Usage: /calibrate?ref=25.0
        ref is the true ambient temperature (degC). Default 25.0.
        """
        ref_temp = 25.0
        raw = None
        try:
            if request.args:
                raw = request.args.get('ref')
        except Exception:
            raw = None

        if raw is not None:
            try:
                ref_temp = float(raw)
            except (ValueError, TypeError):
                return (ujson.dumps({'ok': False, 'msg': 'Invalid reference temperature'}),
                        200, JSON_HEADER)

        if ref_temp < -40.0 or ref_temp > 125.0:
            return (ujson.dumps({'ok': False,
                                 'msg': 'Reference temperature out of range (-40..125 C)'}),
                    200, JSON_HEADER)

        result = await calibrate_sensor(ref_temp)
        if result is None:
            return (ujson.dumps({'ok': False, 'msg': 'Temperature sensor not initialized'}),
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

    print("[OK] Routes registered (temp + control + calibration + static)")


# ===================== 7. Web server =====================
def start_web_server():
    """Start the Microdot server (blocking)."""
    print("[RUN] Starting web server...")
    app.run(host='0.0.0.0', port=80, debug=False)


# ===================== 8. Main =====================
def main():
    print("\n[BOOT] Pico W Temperature Monitoring System")

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

    # 4. Initialize temperature sensor
    init_temp_sensor()

    # 5. Register web routes
    register_routes()

    # 6. Start web server
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