#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import subprocess
import xml.etree.ElementTree as ET
import paho.mqtt.client as mqtt
import threading
import queue
from contextlib import contextmanager

# ============ Settings (env overrides supported) ============
MQTT_BROKER      = os.getenv("MQTT_BROKER", "192.168.1.40")
MQTT_PORT        = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER        = os.getenv("MQTT_USER", "user")
MQTT_PASS        = os.getenv("MQTT_PASS", "password")
MQTT_TOPIC_BASE  = os.getenv("MQTT_TOPIC_BASE", "byd/app")

POLL_SECONDS     = int(os.getenv("POLL_SECONDS", "60"))
DEVICE_ID        = os.getenv("BYD_DEVICE_ID", "byd-app-phone")
DEVICE_NAME      = os.getenv("BYD_DEVICE_NAME", "BYD App Bridge")

# ---- Concurrency primitives ----
ADB_LOCK = threading.RLock()       # serialize all ADB calls
CMD_Q = queue.Queue()              # incoming MQTT button commands

# ============ UI selectors (resource-ids) ============

# Home screen mappings (as seen in your dumps)
# Add or adjust here if BYD updates the app
XML_MAP = {
    "com.byd.bydautolink:id/h_km_tv":              "range_km",
    "com.byd.bydautolink:id/tv_batter_percentage": "battery_soc",
    "com.byd.bydautolink:id/tv_charging_tip":      "charging_tip",
    "com.byd.bydautolink:id/tv_banner":            "warning",
    "com.byd.bydautolink:id/car_inner_temperature":"cabin_temp",
    "com.byd.bydautolink:id/tem_tv":               "climate_target_temp_c",  # home card setpoint
    "com.byd.bydautolink:id/h_car_name_tv":        "car_status",
    "com.byd.bydautolink:id/tv_update_time":       "last_update",
}

# Regex helpers
BOUNDS_RX   = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
PSI_RX      = re.compile(r"^\s*(\d{2}\.\d)\s*psi\s*$", re.IGNORECASE)
KM_RX       = re.compile(r"^\s*([\d,]+)\s*km\s*$", re.IGNORECASE)
KWH100_RX   = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*kW\s*·?\s*h/100km\s*$", re.IGNORECASE)
LATLON_RX   = re.compile(r"(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)")

# ============ MQTT Discovery Logic ============

def publish_discovery(client):
    """
    Publish Home Assistant MQTT Discovery for all metrics AND action buttons.
    Safe to call multiple times (retained configs).
    """
    print("[MQTT] Sending Home Assistant Discovery payloads...")
    
    device = {
        "identifiers": [DEVICE_ID],
        "name": DEVICE_NAME,
        "manufacturer": "BYD (via UI scrape)",
        "model": "Autolink",
    }

    # ------- Sensors (read-only state we publish on {MQTT_TOPIC_BASE}/...) -------
    def disc_sensor(name, uniq, stat_t, unit=None, dev_cla=None, val_tpl=None, icon=None):
        cfg = {
            "name": name,
            "uniq_id": uniq,
            "stat_t": stat_t,                                 # state_topic
            "dev": device,
            "avty_t": f"{MQTT_TOPIC_BASE}/availability",      # availability_topic
            "pl_avail": "online",
            "pl_not_avail": "offline",
        }
        if dev_cla is not None:
            cfg["dev_cla"] = dev_cla
        if val_tpl is not None:
            cfg["val_tpl"] = val_tpl
        if icon is not None:
            cfg["ic"] = icon
        if unit is not None:
            cfg["unit_of_meas"] = unit

        topic = f"homeassistant/sensor/{uniq}/config"
        client.publish(topic, json.dumps(cfg), retain=True)

    # ------- Stateless buttons (publish to {MQTT_TOPIC_BASE}/cmd/... on press) -------
    def disc_button(name, uniq, cmd_t, payload="press", icon=None):
        cfg = {
            "name": name,
            "uniq_id": uniq,
            "cmd_t": cmd_t,                                   # command_topic
            "pl_prs": payload,                                # payload on press
            "dev": device,
            "avty_t": f"{MQTT_TOPIC_BASE}/availability",
            "pl_avail": "online",
            "pl_not_avail": "offline",
        }
        if icon is not None:
            cfg["ic"] = icon

        topic = f"homeassistant/button/{uniq}/config"
        client.publish(topic, json.dumps(cfg), retain=True)

    # -------- Core sensors --------
    disc_sensor("BYD Range",           "byd_range_km",          f"{MQTT_TOPIC_BASE}/range_km", unit="km", dev_cla="distance")
    disc_sensor("BYD Battery SoC",     "byd_battery_soc",       f"{MQTT_TOPIC_BASE}/battery_soc", unit="%", dev_cla="battery")
    disc_sensor("BYD Car Status",      "byd_car_status",        f"{MQTT_TOPIC_BASE}/car_status")
    disc_sensor("BYD Last Update",     "byd_last_update",       f"{MQTT_TOPIC_BASE}/last_update")

    # Climate (home card & A/C page)
    disc_sensor("BYD A/C Target Temp", "byd_ac_target_temp",    f"{MQTT_TOPIC_BASE}/climate_target_temp_c", unit="°C", dev_cla="temperature")
    disc_sensor("BYD A/C PAST Temp",   "byd_ac_prev_temp",      f"{MQTT_TOPIC_BASE}/climate_prev_temp_c", unit="°C", dev_cla="temperature")
    disc_sensor("BYD A/C NEXT Temp",   "byd_ac_next_temp",      f"{MQTT_TOPIC_BASE}/climate_next_temp_c", unit="°C", dev_cla="temperature")
    disc_sensor("BYD A/C Power",       "byd_ac_power",          f"{MQTT_TOPIC_BASE}/climate_power")

    # Tyres (psi)
    for axle in ("fl","fr","rl","rr"):
        disc_sensor(f"BYD Tyre {axle.upper()} (psi)", f"byd_tire_{axle}", f"{MQTT_TOPIC_BASE}/tire_pressure_{axle}", unit="psi")

    # Doors/Windows/Bonnet/Boot & statuses
    disc_sensor("BYD Doors",           "byd_doors",             f"{MQTT_TOPIC_BASE}/doors")
    disc_sensor("BYD Windows",         "byd_windows",           f"{MQTT_TOPIC_BASE}/windows")
    disc_sensor("BYD Bonnet",          "byd_front_bonnet",      f"{MQTT_TOPIC_BASE}/front_bonnet")
    disc_sensor("BYD Boot",            "byd_boot",              f"{MQTT_TOPIC_BASE}/boot")
    disc_sensor("BYD Whole Vehicle",   "byd_whole_vehicle",     f"{MQTT_TOPIC_BASE}/whole_vehicle_status")
    disc_sensor("BYD Driving Status",  "byd_driving_status",    f"{MQTT_TOPIC_BASE}/driving_status")
    disc_sensor("BYD Odometer",        "byd_odometer",          f"{MQTT_TOPIC_BASE}/odometer", unit="km", dev_cla="distance")

    # GPS (raw JSON payload; template in HA if you want lat/lon entities)
    disc_sensor("BYD GPS JSON",        "byd_gps_json",          f"{MQTT_TOPIC_BASE}/location", val_tpl="{{ value | default('') }}", icon="mdi:map-marker")

    # -------- Action buttons (stateless) --------
    # Home page actions
    disc_button("BYD A/C Temp ▲",     "byd_cmd_ac_up",           f"{MQTT_TOPIC_BASE}/cmd/ac_up",          icon="mdi:thermometer-plus")
    disc_button("BYD A/C Temp ▼",     "byd_cmd_ac_down",         f"{MQTT_TOPIC_BASE}/cmd/ac_down",        icon="mdi:thermometer-minus")
    disc_button("BYD Unlock",          "byd_cmd_unlock",          f"{MQTT_TOPIC_BASE}/cmd/unlock",         icon="mdi:lock-open-variant")
    disc_button("BYD Lock",            "byd_cmd_lock",            f"{MQTT_TOPIC_BASE}/cmd/lock",           icon="mdi:lock")

    # Climate page quick actions
    disc_button("BYD Rapid Heat",      "byd_cmd_climate_heat",    f"{MQTT_TOPIC_BASE}/cmd/climate_rapid_heat", icon="mdi:fire")
    disc_button("BYD Rapid Vent",      "byd_cmd_climate_vent",    f"{MQTT_TOPIC_BASE}/cmd/climate_rapid_vent", icon="mdi:fan")
    disc_button("BYD A/C Switch On",   "byd_cmd_climate_on",      f"{MQTT_TOPIC_BASE}/cmd/climate_switch_on",  icon="mdi:power")
    
    print("[MQTT] Discovery payloads sent.")

# ============ MQTT Connection Setup ============

def on_connect(client, userdata, flags, rc):
    print(f"[MQTT] Connected with result code {rc}")
    # Mark online
    client.publish(f"{MQTT_TOPIC_BASE}/availability", "online", retain=True)
    # Send Discovery EVERY time we connect, to ensure HA sees it
    publish_discovery(client)

def connect_mqtt():
    client = mqtt.Client()
    if MQTT_USER and MQTT_PASS:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    
    # Last Will so HA shows offline if we die unexpectedly
    client.will_set(f"{MQTT_TOPIC_BASE}/availability", "offline", retain=True)
    
    client.on_connect = on_connect
    client.on_disconnect = lambda c, u, rc: print(f"⚠️ MQTT disconnected (rc={rc})")
    
    print(f"[MQTT] Connecting to {MQTT_BROKER}...")
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()
    return client

# Initialize Client
client = connect_mqtt()

# ============ MQTT command handling (home page + climate page) ============
def on_mqtt_message(client, userdata, msg):
    base = f"{MQTT_TOPIC_BASE}/cmd/"
    topic = msg.topic
    payload = (msg.payload or b"").decode("utf-8","ignore").strip().lower()
    if not topic.startswith(base):
        return

    sub = topic[len(base):]
    print(f"[cmd] {sub}  payload={payload!r}")

    # Map command topics to callables (no args). Keep these lightweight.
    COMMANDS = {
        "ac_up":              ac_temp_up,
        "ac_down":            ac_temp_down,
        "unlock":             unlock_car,
        "lock":               lock_car,
        "climate_rapid_heat": ac_rapid_heat,      # you added these earlier
        "climate_rapid_vent": ac_rapid_cool,      # (rapid cool == ventilation)
        "climate_switch_on":  ac_switch_on,
    }
    fn = COMMANDS.get(sub)
    if fn:
        CMD_Q.put(fn)   # non-blocking enqueue; worker will run it ASAP
    else:
        print(f"[cmd] Unknown command: {sub}")

client.on_message = on_mqtt_message
client.subscribe(f"{MQTT_TOPIC_BASE}/cmd/#")

# ============ ADB helpers ============

def adb(cmd: str, wait: float = 0.5):
    """Run an adb shell command under a global lock so nothing else can touch the phone concurrently."""
    with ADB_LOCK:
        subprocess.run(["adb", "shell"] + cmd.split(), check=False)
    if wait:
        time.sleep(wait)

def dump_xml(remote="/sdcard/_byd_ui.xml", local="/app/_byd_ui.xml", wait=0.7):
    adb(f"uiautomator dump {remote}", wait)
    subprocess.run(["adb", "pull", remote, local],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return local

def _bounds_str_to_tuple(s: str):
    m = BOUNDS_RX.search(s or "")
    return tuple(map(int, m.groups())) if m else None

def _center(b):
    x1,y1,x2,y2 = b
    return ((x1+x2)//2, (y1+y2)//2)

def _text(n):
    return (n.attrib.get("text") or "").strip()

def extract_values_from_xml(local_xml_path):
    """Parse a single dumped XML (home card) for simple values."""
    vals = {}
    try:
        root = ET.parse(local_xml_path).getroot()
    except Exception:
        return vals

    for node in root.iter("node"):
        rid = node.attrib.get("resource-id", "")
        txt = (node.attrib.get("text") or "").strip()
        if not txt:
            continue
        if rid in XML_MAP:
            key = XML_MAP[rid]
            if key == "range_km":
                m = KM_RX.match(txt)
                vals[key] = m.group(1).replace(",", "") if m else txt
            elif key == "battery_soc":
                vals[key] = txt  # numeric string already
            elif key == "climate_target_temp_c":
                # number like "26"
                try:
                    vals[key] = float(txt)
                except ValueError:
                    pass
            else:
                vals[key] = txt
    return vals

def gather_items(local_xml_path):
    items = []
    try:
        root = ET.parse(local_xml_path).getroot()
    except Exception:
        return items
    for n in root.iter("node"):
        t = (n.attrib.get("text") or "").strip()
        if t:
            items.append((t, n.attrib.get("bounds") or ""))
    return items

def tap_by_label(label: str, local="/app/_byd_home.xml", remote="/sdcard/_byd_home.xml"):
    """
    Find a visible label (TextView text) and tap its tappable container:
    1) nearest ancestor with resource-id containing 'content_group'
    2) else nearest clickable ancestor
    3) else the label's own bounds
    """
    dump_xml(remote, local, wait=0.5)
    try:
        root = ET.parse(local).getroot()
    except Exception:
        print("tap_by_label: no UI dump")
        return False

    target = []
    path = []

    def walk(n):
        nonlocal target
        path.append(n)
        if _text(n) == label:
            target = path.copy()
            path.pop(); return
        for c in n:
            walk(c)
            if target: break
        path.pop()

    walk(root)
    if not target:
        print(f"tap_by_label: '{label}' not found")
        return False

    # Prefer ancestor with resource-id containing "content_group", else first clickable ancestor, else the label itself
    b = None
    for n in reversed(target[:-1]):
        if "content_group" in (n.attrib.get("resource-id") or ""):
            b = _bounds_str_to_tuple(n.attrib.get("bounds")); 
            if b: break
    if not b:
        for n in reversed(target[:-1]):
            if n.attrib.get("clickable") == "true":
                b = _bounds_str_to_tuple(n.attrib.get("bounds")); 
                if b: break
    if not b:
        b = _bounds_str_to_tuple(target[-1].attrib.get("bounds"))

    if not b:
        print("tap_by_label: no bounds")
        return False

    x,y = _center(b)
    print(f"tap_by_label: '{label}' -> tap {x},{y} from {b}")
    adb(f"input tap {x} {y}", 0.1)
    return True

def find_by_id_center(root, rid: str):
    for n in root.iter("node"):
        if n.attrib.get("resource-id") == rid:
            b = _bounds_str_to_tuple(n.attrib.get("bounds") or "")
            if b:
                return _center(b)
    return None

# ============ PIN detection & entry ============

# BYD_PIN is taken from environment only (no baked default).
PIN_CODE = os.getenv("BYD_PIN", "").strip()

# Your calibrated keypad coordinates (device-specific)
PIN_TAPS = {
    "1": (170,  680), "2": (360,  680), "3": (550,  680),
    "4": (170,  870), "5": (360,  870), "6": (550,  870),
    "7": (170, 1060), "8": (360, 1060), "9": (550, 1060),
    "0": (360, 1255),
}

def _ui_dump(local="/app/byd_ui.xml", remote="/sdcard/byd_ui.xml"):
    adb(f"uiautomator dump {remote}", 0.2)
    subprocess.run(["adb","pull",remote,local],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return ET.parse(local).getroot()
    except Exception:
        return None

def pin_prompt_present() -> bool:
    """Detect BYD PIN dialog via title node text (works even if EditText is hidden)."""
    root = _ui_dump(local="/app/byd_pin_check.xml", remote="/sdcard/byd_pin_check.xml")
    if root is None: 
        return False
    for n in root.iter("node"):
        rid = n.attrib.get("resource-id","")
        txt = (n.attrib.get("text","") or "").strip().lower()
        if rid == "com.byd.bydautolink:id/id_verify_title" and "password" in txt:
            return True
    return False

def _tap_xy(x:int, y:int, delay:float=0.12):
    adb(f"input tap {x} {y}", delay)

def enter_pin_via_coords(pin: str | None = None):
    """Tap the numeric keypad using fixed coordinates. Requires BYD_PIN set."""
    s = (pin or PIN_CODE).strip()
    if not s:
        print("⚠️ BYD_PIN not set; cannot enter PIN")
        return
    for ch in s:
        xy = PIN_TAPS.get(ch)
        if not xy:
            print(f"[PIN] Unknown digit: {ch}")
            continue
        _tap_xy(xy[0], xy[1])

def ensure_pin_if_needed(timeout_s: float = 6.0) -> bool:
    """If PIN prompt is up, enter BYD_PIN and wait for it to disappear."""
    if not pin_prompt_present():
        return True
    # small nudge near keypad area (harmless)
    _tap_xy(360, 1245, 0.08)
    enter_pin_via_coords(PIN_CODE)
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if not pin_prompt_present():
            return True
        time.sleep(0.2)
    return True

# ============ Home-page controls (A/C temp up/down, Lock/Unlock) ============

def _bounds_to_center(bounds: str):
    m = BOUNDS_RX.search(bounds or "")
    if not m:
        return None
    x1,y1,x2,y2 = map(int, m.groups())
    return ( (x1+x2)//2, (y1+y2)//2 )

def _dump_home(local="/app/_byd_home.xml", remote="/sdcard/_byd_home.xml"):
    return dump_xml(remote=remote, local=local, wait=0.5)

def ac_temp_up():
    root_path = _dump_home()
    try:
        root = ET.parse(root_path).getroot()
    except Exception:
        print("AC ▲: no UI dump"); return
    for n in root.iter("node"):
        if n.attrib.get("resource-id","") == "com.byd.bydautolink:id/btn_temperature_plus":
            c = _bounds_to_center(n.attrib.get("bounds",""))
            if c:
                adb(f"input tap {c[0]} {c[1]}", 0.08)
                return
    print("AC ▲ not found on home; ensure 'My car' is visible.")

def ac_temp_down():
    root_path = _dump_home()
    try:
        root = ET.parse(root_path).getroot()
    except Exception:
        print("AC ▼: no UI dump"); return
    for n in root.iter("node"):
        if n.attrib.get("resource-id","") == "com.byd.bydautolink:id/btn_temperature_reduce":
            c = _bounds_to_center(n.attrib.get("bounds",""))
            if c:
                adb(f"input tap {c[0]} {c[1]}", 0.08)
                return
    print("AC ▼ not found on home; ensure 'My car' is visible.")

def _quick_control_tiles():
    """Return quick-control tiles sorted left→right as list of (x,y)."""
    root_path = _dump_home()
    try:
        root = ET.parse(root_path).getroot()
    except Exception:
        return []
    tiles = []
    for n in root.iter("node"):
        if n.attrib.get("resource-id","") == "com.byd.bydautolink:id/btn_ble_control_layout":
            c = _bounds_to_center(n.attrib.get("bounds",""))
            if c:
                tiles.append(c)
    tiles.sort(key=lambda p: p[0])
    return tiles

def unlock_car():
    tiles = _quick_control_tiles()
    if not tiles:
        print("Unlock: no quick-control tiles found."); return
    x,y = tiles[0]   # first tile = Unlock on your layout
    adb(f"input tap {x} {y}", 0.08)
    ensure_pin_if_needed()

def lock_car():
    tiles = _quick_control_tiles()
    if len(tiles) < 2:
        print("Lock: not enough quick-control tiles found."); return
    x,y = tiles[1]   # second tile = Lock on your layout
    adb(f"input tap {x} {y}", 0.08)
    ensure_pin_if_needed()

# ============ Vehicle status (two pages) ============

def _center(b):
    x1,y1, x2,y2 = b
    return ((x1+x2)//2, (y1+y2)//2)

def _find_text_positions(local_xml):
    """Return list of (text, bounds) from a dumped xml, preserving duplicates."""
    pairs = []
    try:
        root = ET.parse(local_xml).getroot()
    except Exception:
        return pairs
    for n in root.iter("node"):
        t = (n.attrib.get("text") or "").strip()
        if t:
            pairs.append((t, n.attrib.get("bounds") or ""))
    return pairs

def nearest_value_above(items, label_text, value_regex=None):
    """
    Based on your dumps, labels like 'Front-left' appear above their values.
    We find the first match and choose the nearest numeric text below it.
    """
    cand = []
    label_bounds = None
    for t,b in items:
        if t == label_text:
            m = BOUNDS_RX.search(b or "")
            if m:
                x1,y1,x2,y2 = map(int, m.groups())
                label_bounds = (x1,y1,x2,y2)
                break
    if not label_bounds:
        return None
    _,_, lx2, ly2 = label_bounds
    for t,b in items:
        m = BOUNDS_RX.search(b or "")
        if not m: 
            continue
        x1,y1,x2,y2 = map(int, m.groups())
        if y1 >= ly2:  # below the label
            if value_regex:
                if not value_regex.match(t):
                    continue
            cand.append((y1, x1, t))
    if cand:
        cand.sort()
        return cand[0][2]
    return None

def dump_vehicle_status_two_pages():
    """Open Vehicle status, dump top and bottom, leaving the page open."""
    # Try to ensure home/top, then scroll a little to reveal cards
    adb("input swipe 333 316 333 948 250", 0.2)  # small down
    adb("input swipe 333 948 333 316 350", 0.5)  # scroll up to show cards

    if not tap_by_label("Vehicle status"):
        return None, None

    top_xml    = dump_xml("/sdcard/byd_status_top.xml",    "/app/byd_status_top.xml")
    adb("input swipe 333 948 333 316 450", 0.6)  # scroll within status page
    bottom_xml = dump_xml("/sdcard/byd_status_bottom.xml", "/app/byd_status_bottom.xml")
    return top_xml, bottom_xml

def parse_vehicle_status_two_pages(top_xml, bottom_xml):
    vals = {}
    items = []
    items.extend(gather_items(top_xml))
    items.extend(gather_items(bottom_xml))

    # Tyres by on-screen position (two rows, left→right)
    tyres = []
    for t,b in items:
        m = PSI_RX.match(t)
        if m:
            v = m.group(1)
            tyres.append( (b, v) )
    # Sort two rows (by Y, then X)
    tyre_pos = []
    for b,v in tyres:
        m = BOUNDS_RX.search(b or "")
        if not m: 
            continue
        x1,y1,x2,y2 = map(int, m.groups())
        tyre_pos.append((y1, x1, v))
    tyre_pos.sort()
    # Expect 4 tyre values
    if len(tyre_pos) >= 4:
        # first row (front): left→right
        vals["tire_pressure_fl"] = tyre_pos[0][2]
        vals["tire_pressure_fr"] = tyre_pos[1][2]
        # second row (rear): left→right
        vals["tire_pressure_rl"] = tyre_pos[2][2]
        vals["tire_pressure_rr"] = tyre_pos[3][2]

    # Doors/Windows/Bonnet/Boot, Whole Vehicle Status, Driving Status, Odometer
    # We match by label text and read the nearest numeric/value below each label.
    label_map = {
        "Front hood":        "front_bonnet",
        "Door":              "doors",
        "Window":            "windows",
        "Trunk":             "boot",
        "Whole vehicle":     "whole_vehicle_status",
        "Driving status":    "driving_status",
        "Odometer":          "odometer",
        "Recent energy":     "recent_energy_value",
        "Total energy":      "total_energy_value",
    }

    # Recent/Total energy formats vary. Try generic capture too.
    for label, key in label_map.items():
        regex = None
        if key in ("recent_energy_value", "total_energy_value"):
            regex = KWH100_RX
        v = nearest_value_above(items, label, value_regex=regex)
        if v is not None:
            if key == "odometer":
                mv = KM_RX.match(v)
                vals[key] = mv.group(1).replace(",", "") if mv else v
            else:
                vals[key] = v

    return vals

# ============ A/C page parsing (for prev/next setpoints & power) ============

def open_ac_page():
    # On your home page the climate card has visible label text "Seats" and "Vehicle position" below it.
    # The climate card is the left tile of the widgets row. We can just tap the center of that tile by id.
    # But to avoid brittle id, use the explicit known id on your dump:
    # com.byd.bydautolink:id/c_air_item_rl_2  (container for the setpoint row)
    local = dump_xml("/sdcard/byd_home.xml", "/app/byd_home.xml")
    try:
        root = ET.parse(local).getroot()
    except Exception:
        return False
    # Find the setpoint container and tap its center to open full A/C page
    for n in root.iter("node"):
        if n.attrib.get("resource-id") == "com.byd.bydautolink:id/c_air_item_rl_2":
            b = _bounds_str_to_tuple(n.attrib.get("bounds") or "")
            if b:
                x, y = _center(b)
                adb(f"input tap {x} {y}", 0.2)
                return True
    # Fallback: do a small tap where the setpoint sits
    adb("input tap 190 1005", 0.2)
    return True

# ============ Climate page controls (Rapid heat / Rapid vent / Power) ============

def _tap_by_id_on_current_dump(resource_id: str, remote="/sdcard/byd_ac.xml", local="/app/byd_ac.xml", delay=0.08) -> bool:
    """
    Re-dump the current screen (assumes Climate page is open), find a node by
    resource-id, and tap its center. Returns True if a tap was sent.
    """
    dump_xml(remote, local, wait=0.35)
    try:
        root = ET.parse(local).getroot()
    except Exception:
        print(f"[climate] could not parse dump {local}")
        return False

    for n in root.iter("node"):
        if n.attrib.get("resource-id", "") == resource_id:
            m = BOUNDS_RX.search(n.attrib.get("bounds", "") or "")
            if not m:
                continue
            x1, y1, x2, y2 = map(int, m.groups())
            x, y = ((x1 + x2) // 2, (y1 + y2) // 2)
            adb(f"input tap {x} {y}", delay)
            return True

    print(f"[climate] resource-id not found: {resource_id}")
    return False


def ac_rapid_heat():
    """
    Open Climate page → tap 'Rapid heating' → back.
    """
    if not open_ac_page():
        print("[climate] cannot open A/C page")
        return
    ok = _tap_by_id_on_current_dump("com.byd.bydautolink:id/c_air_item_heat_btn")
    if not ok:
        print("[climate] Rapid heating button not found")
    time.sleep(0.3)
    adb("input keyevent 4", 0.4)


def ac_rapid_cool():
    """
    Open Climate page → tap 'Rapid cooling' (aka ventilation) → back.
    """
    if not open_ac_page():
        print("[climate] cannot open A/C page")
        return
    ok = _tap_by_id_on_current_dump("com.byd.bydautolink:id/c_air_item_cool_btn")
    if not ok:
        print("[climate] Rapid cooling button not found")
    time.sleep(0.3)
    adb("input keyevent 4", 0.4)


def ac_switch_on():
    """
    Open Climate page → tap 'Switch on' → auto-enter PIN if prompted → publish power → back.
    """
    if not open_ac_page():
        print("[climate] cannot open A/C page")
        return
    ok = _tap_by_id_on_current_dump("com.byd.bydautolink:id/c_air_item_power_btn")
    if not ok:
        print("[climate] Switch On button not found")
        adb("input keyevent 4", 0.3)
        return

    # If BYD pops the PIN dialog, enter BYD_PIN (env) and wait for it to disappear
    ensure_pin_if_needed()

    # Optional confirmation publish (read label 'switch on/off' under the power tile)
    try:
        dump_xml("/sdcard/byd_ac.xml", "/app/byd_ac.xml", wait=0.5)
        root = ET.parse("/app/byd_ac.xml").getroot()
        power = None
        for n in root.iter("node"):
            if n.attrib.get("resource-id") == "com.byd.bydautolink:id/c_air_item_power_btn":
                for m in n.iter("node"):
                    low = (m.attrib.get("text") or "").strip().lower()
                    if low in ("switch on", "switch off"):
                        power = "on" if "on" in low else "off"
                        break
                break
        if power:
            client.publish(f"{MQTT_TOPIC_BASE}/climate_power", power)
            print(f"➡️ Publish {MQTT_TOPIC_BASE}/climate_power -> {power}")
    except Exception:
        pass

    adb("input keyevent 4", 0.4)

def _as_float(s):
    try:
        return float(s)
    except Exception:
        return None

def publish_values(vals: dict):
    if not vals:
        return
    for k, v in vals.items():
        client.publish(f"{MQTT_TOPIC_BASE}/{k}", v if isinstance(v, str) else json.dumps(v))
        print(f"➡️ Publish {MQTT_TOPIC_BASE}/{k} -> {v}")

# ============ Command worker (executes queued MQTT button commands) ============

def _command_worker():
    print("[worker] Command worker started")
    while True:
        fn = CMD_Q.get()   # waits for a callable enqueued by on_mqtt_message
        try:
            # If the PIN prompt is up, enter it before running any action
            ensure_pin_if_needed()
            fn()  # run the action (e.g., ac_temp_up / lock_car / ac_rapid_heat)
        except Exception as e:
            print(f"[worker] Command error: {e}")
        finally:
            CMD_Q.task_done()

def main_loop():
    while True:
        cycle = {}

        # --- HOME: dump & parse the basics (incl. A/C set temp on the card)
        home_xml = dump_xml("/sdcard/byd_home.xml", "/app/byd_home.xml")
        home_vals = extract_values_from_xml(home_xml)
        publish_values(home_vals)
        cycle.update(home_vals)

        # --- VEHICLE STATUS: open, dump TOP+BOTTOM, parse, publish, back
        top_xml, bottom_xml = dump_vehicle_status_two_pages()
        if top_xml and bottom_xml:
            status_vals = parse_vehicle_status_two_pages(top_xml, bottom_xml)
            publish_values(status_vals)
            cycle.update(status_vals)
            adb("input keyevent 4", 0.5)  # back to home

        # --- GPS: open, read coords from text node near [83,153] trick from earlier, publish, back
        if tap_by_label("Vehicle position"):
            gps_xml = dump_xml("/sdcard/byd_gps.xml", "/app/byd_gps.xml", wait=0.6)
            # Extract lat/lon directly from any node text containing "lat,lon"
            try:
                root = ET.parse(gps_xml).getroot()
                for n in root.iter("node"):
                    txt = (n.attrib.get("text") or "").strip()
                    m = LATLON_RX.search(txt)
                    if m:
                        lat = float(m.group(1)); lon = float(m.group(2))
                        payload = {"latitude": lat, "longitude": lon, "gps_accuracy": 10, "status": "driving"}
                        client.publish(f"{MQTT_TOPIC_BASE}/location", json.dumps(payload))
                        print(f"➡️ Publish {MQTT_TOPIC_BASE}/location -> {payload}")
                        break
            except Exception:
                pass
            adb("input keyevent 4", 0.5)  # back

        # --- A/C page: open, parse prev/next and power label, publish, back
        if open_ac_page():
            ac_xml = dump_xml("/sdcard/byd_ac.xml", "/app/byd_ac.xml")
            try:
                root = ET.parse(ac_xml).getroot()
            except Exception:
                root = None
            ac_vals = {}
            # Current setpoint already exposed from home; prev/next are optional
            if root is not None:
                # Current setpoint text (same id)
                for n in root.iter("node"):
                    rid = n.attrib.get("resource-id") or ""
                    if rid == "com.byd.bydautolink:id/tem_tv":
                        txt = (n.attrib.get("text") or "").strip()
                        try:
                            ac_vals["climate_target_temp_c"] = float(txt)
                        except ValueError:
                            pass
                        break

                # Previous/next temps (optional)
                for n in root.iter("node"):
                    rid = n.attrib.get("resource-id") or ""
                    if rid == "com.byd.bydautolink:id/tem_tv_last":
                        t = (n.attrib.get("text") or "").strip()
                        if t.isdigit():
                            ac_vals["climate_prev_temp_c"] = float(t)
                    elif rid == "com.byd.bydautolink:id/tem_tv_next":
                        t = (n.attrib.get("text") or "").strip()
                        if t.isdigit():
                            ac_vals["climate_next_temp_c"] = float(t)

                # Power (from label within c_air_item_power_btn)
                power_label = None
                for n in root.iter("node"):
                    if n.attrib.get("resource-id") == "com.byd.bydautolink:id/c_air_item_power_btn":
                        for m in n.iter("node"):
                            low = (m.attrib.get("text") or "").strip().lower()
                            if low in ("switch on", "switch off"):
                                power_label = low
                                break
                        break

                if power_label:
                    ac_vals["climate_power"] = "on" if "on" in power_label else "off"

            if ac_vals:
                publish_values(ac_vals)
                cycle.update(ac_vals)
            adb("input keyevent 4", 0.5)
        else:
            print("⚠️ Could not open A/C page")

        # Done this cycle
        time.sleep(POLL_SECONDS)

# ============ Entrypoint ============

if __name__ == "__main__":
    try:
        # Start command worker so MQTT button presses are executed promptly
        threading.Thread(target=_command_worker, daemon=True).start()

        main_loop()
    except KeyboardInterrupt:
        print("🚪 Exiting.")
    finally:
        try:
            client.publish(f"{MQTT_TOPIC_BASE}/availability", "offline", retain=True)
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass