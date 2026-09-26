"""Wait for the end of pass 1, stop campaign.py, smoke-test campaign2.py (synchronised C2/C3), then run passes 2-3."""

import json
import os
import subprocess
import sys
import time

S = os.path.dirname(os.path.abspath(__file__))
CAMP_LOG = os.path.join(S, "camp-night", "campaign.log")
END_AT = time.mktime(time.strptime(time.strftime("%Y-%m-%d") + " 06:34:58", "%Y-%m-%d %H:%M:%S"))
if END_AT < time.time():
    END_AT += 24 * 3600  # already past midnight: today's date is right; otherwise tomorrow's
FW = r"D:\programming\MeshtasticFirmware"
out = open(os.path.join(S, "switch.log"), "a", encoding="utf-8")


def log(m):
    out.write(f"{time.strftime('%H:%M:%S')} {m}\n")
    out.flush()


def kill_campaign():
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'campaign.py' -and $_.CommandLine -notmatch 'campaign2' } "
          "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
    log(f"killed campaign.py: {r.stdout.split()}")


log(f"waiting for the end of pass 1; deadline {time.strftime('%H:%M:%S', time.localtime(END_AT))}")
while True:
    try:
        if "counters after p1 LONG_FAST" in open(CAMP_LOG, encoding="utf-8").read():
            break
        if "CAMPAIGN DONE" in open(CAMP_LOG, encoding="utf-8").read():
            log("campaign.py ended before the pass-1 marker")
            break
    except FileNotFoundError:
        pass
    time.sleep(10)
kill_campaign()
time.sleep(8)

# flash the eiq image (inverted-IQ emit) on all four nodes, if it was built in time
zipf = os.path.join(S, "knobs13eiq.zip")
if os.path.exists(zipf):
    for sn in ("43C2192F2DFEE099", "69DCAE3411236A3E", "C4BB0930D953D0A3", "06E3F87E62341649"):
        r = subprocess.run([sys.executable, os.path.join(S, "flash.py"), sn, zipf], cwd=FW, capture_output=True, text=True, timeout=600)
        log(f"flash {sn}: {r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-200:]}")
    time.sleep(15)
else:
    log("no knobs13eiq.zip - C1iq will not confirm eiq=1")

smoke_dir = os.path.join(S, "camp-p23-smoke")
log("campaign2 smoke")
subprocess.run([sys.executable, "-u", os.path.join(S, "campaign2.py"), smoke_dir, "1", "smoke"], cwd=FW,
               stdout=open(os.path.join(S, "camp-p23-smoke.out"), "w"), stderr=subprocess.STDOUT, timeout=2700)
ok = False
try:
    rs = [json.loads(l) for l in open(os.path.join(smoke_dir, "records.jsonl"), encoding="utf-8")]
    c2 = [r for r in rs if r["kind"] == "C2s"]
    c3 = [r for r in rs if r["kind"] == "C3s"]
    trig = sum(r.get("triggered") for r in c2)
    iq = [r for r in rs if r["kind"] == "C1iq"]
    iq_armed = sum(r.get("armed") and r.get("emitted") for r in iq)
    log(f"smoke: C2s {len(c2)} rounds, {trig} triggered on all senders; C3s {len(c3)} rounds; C1iq {iq_armed}/{len(iq)} armed and emitted")
    ok = len(c2) >= 4 and trig >= len(c2) // 2 and len(c3) >= 2 and iq_armed >= len(iq) // 2
except Exception as e:
    log(f"smoke check failed: {e}")
log("smoke OK, launching passes 2-3" if ok else "SMOKE NOT OK - launching passes 2-3 anyway; C2s/C3s need a look")
time.sleep(5)
p = subprocess.Popen([sys.executable, "-u", os.path.join(S, "campaign2.py"), os.path.join(S, "camp-night2"), "9", "full", str(END_AT)],
                     cwd=FW, stdout=open(os.path.join(S, "camp-night2.out"), "w"), stderr=subprocess.STDOUT)
log(f"campaign2 PID {p.pid}")
