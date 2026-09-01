import json
import subprocess
from pathlib import Path
import time

CUA_PATH = str(Path.home() / "AppData" / "Local" / "Programs" / "Cua" / "cua-driver" / "bin" / "cua-driver.exe")

def ensure_daemon():
    status = subprocess.run([CUA_PATH, "status"], capture_output=True, text=True, encoding="utf-8")
    if "is running" not in (status.stdout or ""):
        print("Starting daemon...")
        subprocess.Popen([CUA_PATH, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)

def call_cua(tool, args):
    proc = subprocess.run([CUA_PATH, "call", tool, json.dumps(args)], capture_output=True, text=True, encoding="utf-8")
    text = proc.stdout.strip()
    if text:
        try:
            return json.loads(text)
        except:
            return {"raw": text}
    return {"error": proc.stderr.strip() if proc.stderr else "empty output"}

ensure_daemon()

print("Launching calculator...")
subprocess.Popen(["calc.exe"])
time.sleep(3)

print("Getting window state of active window (no args)...")
state = call_cua("get_window_state", {"capture_mode": "ax"})
print(f"Element count: {state.get('element_count')}")
if state.get('element_count'):
    print("Success!")
else:
    print(state.get("error", state))
