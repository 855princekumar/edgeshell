"""Hardware identity of a node, read over SSH with the metrics (ADR 0015).

Each part is read separately so EdgeShell can tell *which* part changed:

  board    the board serial: device tree (Raspberry Pi and other ARM boards),
           /proc/cpuinfo, or the DMI product serial (x86, when readable)
  storage  the disk holding /: an SD or eMMC card's CID, else the disk serial
  network  the MAC address of the first physical network interface
  os       /etc/machine-id, which a fresh OS image changes

plus descriptive text (board model, card name and size, OS name) and the
Raspberry Pi power flags (vcgencmd get_throttled). Only plain reads, no root
needed, no packages; anything missing is left empty and never counts as a
change. Values come from the node, so they are reduced to a safe character
set and length before use.
"""
import hashlib
import json
import re

HW_CMD = (
    "m=$(tr -d '\\0' 2>/dev/null </proc/device-tree/model); "
    "[ -n \"$m\" ] || m=$(cat /sys/class/dmi/id/product_name 2>/dev/null); echo \"model=$m\"; "
    "b=$(tr -d '\\0' 2>/dev/null </sys/firmware/devicetree/base/serial-number); "
    "[ -n \"$b\" ] || b=$(awk -F': *' '/^Serial/{print $2; exit}' /proc/cpuinfo 2>/dev/null); "
    "[ -n \"$b\" ] || b=$(cat /sys/class/dmi/id/product_serial 2>/dev/null); echo \"board=$b\"; "
    "d=$(lsblk -no PKNAME \"$(findmnt -no SOURCE / 2>/dev/null)\" 2>/dev/null | head -1); "
    "if [ -n \"$d\" ]; then "
    "s=$(cat /sys/block/$d/device/cid 2>/dev/null); [ -n \"$s\" ] || s=$(lsblk -dno SERIAL /dev/$d 2>/dev/null); "
    "n=$(cat /sys/block/$d/device/name 2>/dev/null); [ -n \"$n\" ] || n=$(lsblk -dno MODEL /dev/$d 2>/dev/null); "
    "echo \"storage=$s\"; echo \"storage_model=$n $(lsblk -dbno SIZE /dev/$d 2>/dev/null)\"; fi; "
    "i=''; for p in /sys/class/net/*; do [ -e \"$p/device\" ] && { i=${p##*/}; break; }; done; "
    "if [ -z \"$i\" ]; then for p in /sys/class/net/*; do q=${p##*/}; case $q in "
    "lo|docker*|br-*|veth*|tailscale*|wg*|tun*|tap*|virbr*) ;; *) i=$q; break;; esac; done; fi; "
    "[ -n \"$i\" ] && echo \"iface=$i\" && echo \"network=$(cat /sys/class/net/$i/address 2>/dev/null)\"; "
    "echo \"os=$(cat /etc/machine-id 2>/dev/null)\"; "
    "echo \"os_name=$(grep -m1 '^PRETTY_NAME=' /etc/os-release 2>/dev/null | cut -d= -f2-)\"; "
    "command -v vcgencmd >/dev/null 2>&1 && echo \"throttled=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)\"; "
    "echo @@"
)

DETECTED_KINDS = ('board', 'storage', 'network', 'os')
_UNSAFE = re.compile(r'[^A-Za-z0-9 ._:/()+,#-]')
_EMPTY = re.compile(r'^[0:. -]*$')          # all-zero serials and MACs mean "none"
UNDERVOLTAGE_NOW, UNDERVOLTAGE_SINCE_BOOT = 0x1, 0x10000


def clean(value, length=128):
    v = _UNSAFE.sub('', (value or '').strip())[:length].strip()
    return '' if _EMPTY.match(v) else v


def parse(section):
    """The HW_CMD output (one key=value per line) as a dict of clean values."""
    raw = {}
    for line in (section or '').splitlines():
        key, sep, value = line.partition('=')
        if sep:
            raw[key.strip()] = value
    size = ''
    model_parts = (raw.get('storage_model') or '').split()
    if model_parts and model_parts[-1].isdigit():
        size = _size(int(model_parts.pop()))
    throttled = None
    t = (raw.get('throttled') or '').strip().lower()
    if re.fullmatch(r'0x[0-9a-f]{1,8}', t):
        throttled = int(t, 16)
    return {
        'model': clean(raw.get('model')),
        'board': clean(raw.get('board')),
        'storage': clean(raw.get('storage')),
        'storage_model': clean(' '.join(model_parts + ([size] if size else []))),
        'network': clean(raw.get('network'), 32).lower(),
        'iface': clean(raw.get('iface'), 32),
        'os': clean(raw.get('os'), 64),
        'os_name': clean(raw.get('os_name')),
        'throttled': throttled,
    }


def _size(n):
    for unit, step in (('TB', 1e12), ('GB', 1e9), ('MB', 1e6)):
        if n >= step:
            return f'{n / step:.0f} {unit}' if n >= 10 * step else f'{n / step:.1f} {unit}'
    return ''


def identity(hw):
    """{kind: identifier} for the parts that could be read."""
    return {k: hw[k] for k in DETECTED_KINDS if hw.get(k)}


def model_of(kind, hw):
    return {'board': hw.get('model'), 'storage': hw.get('storage_model'),
            'network': hw.get('iface'), 'os': hw.get('os_name')}.get(kind) or None


def fingerprint(hw):
    """Changes whenever any part or its description changes."""
    ident = identity(hw)
    blob = json.dumps([sorted(ident.items()), [model_of(k, hw) for k in sorted(ident)]])
    return hashlib.sha256(blob.encode()).hexdigest()


def undervoltage(hw):
    t = hw.get('throttled')
    return bool(t is not None and t & (UNDERVOLTAGE_NOW | UNDERVOLTAGE_SINCE_BOOT))
