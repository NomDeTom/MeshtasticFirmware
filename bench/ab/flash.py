import sys, time, subprocess
from pathlib import Path
from serial.tools import list_ports
serial_no, pkg = sys.argv[1], sys.argv[2]
def port():
    for p in list_ports.comports():
        if p.serial_number == serial_no: return p.device
p0 = port(); print("app port", p0, flush=True)
subprocess.run([sys.executable, "-m", "meshtastic", "--port", p0, "--enter-dfu"], timeout=40, capture_output=True)
time.sleep(6)
t = time.time()
while time.time() - t < 60 and not port(): time.sleep(1)
p1 = port(); print("dfu port", p1, flush=True)
nrf = str(Path.home() / ".platformio/packages/tool-adafruit-nrfutil/adafruit-nrfutil.py")
r = subprocess.run([sys.executable, nrf, "dfu", "serial", "--package", pkg, "-p", p1, "-b", "115200", "--singlebank"], capture_output=True, text=True, timeout=300)
out = r.stdout + r.stderr
print("programmed" if "Device programmed" in out else "FAILED\n" + out[-800:], flush=True)
time.sleep(8)
t = time.time()
while time.time() - t < 60 and not port(): time.sleep(1)
print("back on", port(), flush=True)
