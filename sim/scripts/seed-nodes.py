#!/usr/bin/env python3
"""Auto-seed all 6 simulation nodes into EdgeShell."""
import urllib.request, urllib.parse, urllib.error, time, sys

BASE = "http://localhost:5000"
COOKIE = None

def req(method, path, data=None):
    global COOKIE
    headers = {}
    if COOKIE: headers["Cookie"] = COOKIE
    body = urllib.parse.urlencode(data).encode() if data else None
    if body: headers["Content-Type"] = "application/x-www-form-urlencoded"
    r = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(r, timeout=10)
        sc = resp.headers.get("Set-Cookie")
        if sc: COOKIE = sc.split(";")[0]
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        sc = e.headers.get("Set-Cookie")
        if sc: COOKIE = sc.split(";")[0]
        if e.code in (301,302,303): return e.code, ""
        return e.code, e.read().decode()

def wait():
    print("Waiting for EdgeShell…")
    for i in range(40):
        try:
            s, _ = req("GET", "/api/v1/status")
            if s == 200: print("[OK] EdgeShell ready."); return True
        except: pass
        time.sleep(2); print(f"  {(i+1)*2}s…")
    print("[FAIL] Timed out."); return False

def login():
    req("GET", "/login")
    s, _ = req("POST", "/login", {"username":"admin","password":"admin123"})
    ok = s in (200,302,303)
    print("[OK] Logged in." if ok else f"[FAIL] Login failed: {s}")
    return ok

NODES = [
    # 32-bit nodes
    dict(name="Pi Factory 01 (32-bit)",   ip_address="10.10.0.21", ssh_port="22", username="pi", password="raspberry", description="Factory floor - ARMv7 32-bit", tags="factory,32bit,armv7"),
    dict(name="Pi Warehouse 02 (32-bit)",  ip_address="10.10.0.22", ssh_port="22", username="pi", password="raspberry", description="Warehouse gateway - ARMv7 32-bit", tags="warehouse,32bit,armv7"),
    dict(name="Pi Outdoor 03 (32-bit)",    ip_address="10.10.0.23", ssh_port="22", username="pi", password="raspberry", description="Outdoor sensor hub - ARMv7 32-bit", tags="outdoor,32bit,armv7"),
    # 64-bit nodes
    dict(name="Pi64 Control 04 (64-bit)",  ip_address="10.10.0.24", ssh_port="22", username="pi", password="raspberry", description="Control node - ARM64 64-bit", tags="control,64bit,aarch64"),
    dict(name="Pi64 Gateway 05 (64-bit)",  ip_address="10.10.0.25", ssh_port="22", username="pi", password="raspberry", description="Gateway node - ARM64 64-bit", tags="gateway,64bit,aarch64"),
    dict(name="Pi64 Edge 06 (64-bit)",     ip_address="10.10.0.26", ssh_port="22", username="pi", password="raspberry", description="Edge node - ARM64 64-bit", tags="edge,64bit,aarch64"),
]

def seed():
    print("\nSeeding nodes…")
    for n in NODES:
        s, _ = req("POST", "/nodes/add", n)
        sym = "[OK]" if s in (200,302,303) else "[SKIP]"
        print(f"  {sym} {n['name']} → {n['ip_address']}")

def summary():
    print("""
======================================================
  EdgeShell Fleet Simulation Ready!
======================================================
  UI         : http://localhost:5000
  Login      : admin / admin123

  32-bit nodes (ARMv7 / Raspberry Pi OS 32-bit):
    Pi Factory 01   10.10.0.21  SSH→2221  Node-RED→1881
    Pi Warehouse 02 10.10.0.22  SSH→2222  Node-RED→1882
    Pi Outdoor 03   10.10.0.23  SSH→2223  Node-RED→1883

  64-bit nodes (ARM64 / Raspberry Pi OS 64-bit):
    Pi64 Control 04 10.10.0.24  SSH→2224  Node-RED→1884
    Pi64 Gateway 05 10.10.0.25  SSH→2225  Node-RED→1885
    Pi64 Edge 06    10.10.0.26  SSH→2226  Node-RED→1886

  SSH (password: raspberry):
    ssh pi@localhost -p 2221   # 32-bit factory
    ssh pi@localhost -p 2224   # 64-bit control

  Portainer : http://localhost:9000
======================================================
""")

if __name__ == "__main__":
    if not wait(): sys.exit(1)
    if not login(): sys.exit(1)
    seed()
    summary()
