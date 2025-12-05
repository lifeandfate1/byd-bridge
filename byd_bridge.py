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

# --- Page selection flags (0/1). HOME is forced to 1 ---
# Set these to 0 in your docker-compose or env to skip pages
POLL_VEHICLE_STATUS    = int(os.getenv("POLL_VEHICLE_STATUS", "1"))
POLL_AC                = int(os.getenv("POLL_AC", "1"))
POLL_VEHICLE_POSITION  = int(os.getenv("POLL_VEHICLE_POSITION", "1"))

# Normalized map for easy lookup
PAGES_ENABLED = {
    "home": True,
    "vehicle_status": bool(POLL_VEHICLE_STATUS),
    "ac": bool(POLL_AC),
    "vehicle_position": bool(POLL_VEHICLE_POSITION),
}

# ---- Concurrency primitives ----
ADB_LOCK = threading.RLock()       # serialize all ADB calls
CMD_Q = queue.Queue()              # incoming MQTT button commands

# ============ UI selectors (resource-ids) ============

# Home screen mappings (as seen in your dumps)
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

# ============ MQTT Discovery Logic (Smart) ============

def publish_discovery(client):
    """
    Publish Home Assistant MQTT Discovery.
    - Creates configs for enabled pages.
    - PURGES configs (sends empty payload) for disabled pages.
    """
    print("[MQTT] Sending Smart Home Assistant Discovery payloads...")
    
    device = {
        "identifiers": [DEVICE_ID],
        "name": DEVICE_NAME,
        "manufacturer": "BYD (via UI scrape)",
        "model": "Autolink",
    }

    # Helpers
    def _sensor_cfg(name, uniq, stat_t, unit=None, dev_cla=None, val_tpl=None, icon=None):
        cfg = {
            "name": name,
            "uniq_id": uniq,
            "stat_t": stat_t,
            "dev": device,
            "avty_t": f"{MQTT_TOPIC_BASE}/availability",
            "pl_avail": "online",
            "pl_not_avail": "offline",
        }
        if dev_cla: cfg["dev_cla"] = dev_cla
        if val_tpl: cfg["val_tpl"] = val_tpl
        if icon:    cfg["ic"] = icon
        if unit:    cfg["unit_of_meas"] = unit
        return cfg

    def _publish_config(uniq, cfg):
        topic = f"homeassistant/sensor/{uniq}/config"
        client.publish(topic, json.dumps(cfg), retain=True)

    def _purge_config(uniq):
        topic = f"homeassistant/sensor/{uniq}/config"
        client.publish(topic, "", retain=True) # Empty payload removes entity

    # Registry of sensors mapped to their "page"
    registry = [
        # HOME (always enabled)
        ("home", "BYD Range",           "byd_range_km",         f"{MQTT_TOPIC_BASE}/range_km",             "km", "distance", None, None),
        ("home", "BYD Battery SoC",     "byd_battery_soc",      f"{MQTT_TOPIC_BASE}/battery_soc",          "%",  "battery",  None, None),
        ("home", "BYD Car Status",      "byd_car_status",       f"{MQTT_TOPIC_BASE}/car_status",           None, None,       None, None),
        ("home", "BYD Last Update",     "byd_last_update",      f"{MQTT_TOPIC_BASE}/last_update",          None, None,       None, None),
        ("home", "BYD A/C Target Temp", "byd_ac_target_temp",   f"{MQTT_TOPIC_BASE}/climate_target_temp_c","°C", "temperature", None, None),

        # A/C PAGE (Optional)
        ("ac",   "BYD A/C PAST Temp",   "byd_ac_prev_temp",     f"{MQTT_TOPIC_BASE}/climate_prev_temp_c",  "°C", "temperature", None, None),
        ("ac",   "BYD A/C NEXT Temp",   "byd_ac_next_temp",     f"{MQTT_TOPIC_BASE}/climate_next_temp_c",  "°C", "temperature", None, None),
        ("ac",   "BYD A/C Power",       "byd_ac_power",         f"{MQTT_TOPIC_BASE}/climate_power",        None, None,       None, None),

        # VEHICLE STATUS (Optional)
        ("vehicle_status", "BYD Tyre FL (psi)", "byd_tire_pressure_fl", f"{MQTT_TOPIC_BASE}/tire_pressure_fl", "psi", None, None, None),
        ("vehicle_status", "BYD Tyre FR (psi)", "byd_tire_pressure_fr", f"{MQTT_TOPIC_BASE}/tire_pressure_fr", "psi", None, None, None),
        ("vehicle_status", "BYD Tyre RL (psi)", "byd_tire_pressure_rl", f"{MQTT_TOPIC_BASE}/tire_pressure_rl", "psi", None, None, None),
        ("vehicle_status", "BYD Tyre RR (psi)", "byd_tire_pressure_rr", f"{MQTT_TOPIC_BASE}/tire_pressure_rr", "psi", None, None, None),
        ("vehicle_status", "BYD Doors",         "byd_doors",             f"{MQTT_TOPIC_BASE}/doors",            None, None, None, None),
        ("vehicle_status", "BYD Windows",       "byd_windows",           f"{MQTT_TOPIC_BASE}/windows",          None, None, None, None),
        ("vehicle_status", "BYD Bonnet",        "byd_front_bonnet",      f"{MQTT_TOPIC_BASE}/front_bonnet",     None, None, None, None),
        ("vehicle_status", "BYD Boot",          "byd_boot",              f"{MQTT_TOPIC_BASE}/boot",             None, None, None, None),
        ("vehicle_status", "BYD Whole Vehicle", "byd_whole_vehicle",     f"{MQTT_TOPIC_BASE}/whole_vehicle_status", None, None, None, None),
        ("vehicle_status", "BYD Driving Status","byd_driving_status",    f"{MQTT_TOPIC_BASE}/driving_status",   None, None, None, None),
        ("vehicle_status", "BYD Odometer",      "byd_odometer",          f"{MQTT_TOPIC_BASE}/odometer",         "km", "distance", None, None),

        # VEHICLE POSITION (Optional)
        ("vehicle_position","BYD GPS JSON",     "byd_gps_json",          f"{MQTT_TOPIC_BASE}/location",         None, None, "{{ value | default('') }}", "mdi:map-marker"),
    ]

    # Process Registry
    for page, name, uniq, stat_t, unit, dev_cla, val_tpl, icon in registry:
        # Check if this page is enabled in settings
        if PAGES_ENABLED.get(page, False):
            # Publish Config
            cfg = _sensor_cfg(name, uniq, stat_t, unit, dev_cla, val_tpl, icon)
            _publish_config(uniq, cfg)
        else:
            # Purge Config (cleanup)
            _purge_config(uniq)

    # ------- Buttons (Always Publish) -------
    # These are stateless and don't hurt to have even if polling is off
    def disc_button(name, uniq, cmd_t, payload="press", icon=None):
        cfg = {
            "name": name,
            "uniq_id": uniq,
            "cmd_t": cmd_t,                                   
            "pl_prs": payload,                                
            "dev": device,
            "avty_t": f"{MQTT_TOPIC_BASE}/availability",
            "pl_avail": "online",
            "pl_not_avail": "offline",
        }
        if icon: cfg["ic"] = icon
        topic = f"homeassistant/button/{uniq}/config"
        client.publish(topic, json.dumps(cfg), retain=True)

    disc_button("BYD A/C Temp ▲",     "byd_cmd_ac_up",           f"{MQTT_TOPIC_BASE}/cmd/ac_up",          icon="mdi:thermometer-plus")
    disc_button("BYD A/C Temp ▼",     "byd_cmd_ac_down",         f"{MQTT_TOPIC_BASE}/cmd/ac_down",        icon="mdi:thermometer-minus")
    disc_button("BYD Unlock",          "byd_cmd_unlock",          f"{MQTT_TOPIC_BASE}/cmd/unlock",         icon="mdi:lock-open-variant")
    disc_button("BYD Lock",            "byd_cmd_lock",            f"{MQTT_TOPIC_BASE}/cmd/lock",           icon="mdi:lock")
    disc_button("BYD Rapid Heat",      "byd_cmd_climate_heat",    f"{MQTT_TOPIC_BASE}/cmd/climate_rapid_heat", icon="mdi:fire")
    disc_button("BYD Rapid Vent",      "byd_cmd_climate_vent",    f"{MQTT_TOPIC_BASE}/cmd/climate_rapid_vent", icon="mdi:fan")
    disc_button("BYD A/C Switch On",   "byd_cmd_climate_on",      f"{MQTT_TOPIC_BASE}/cmd/climate_switch_on",  icon="mdi:power")
    
    print("[MQTT] Discovery refresh complete.")

# ============ MQTT Connection Setup ============

def on_connect(client, userdata, flags, rc):
    print(f"[MQTT] Connected with result code {rc}")
    # Mark online
    client.publish(f"{MQTT_TOPIC_BASE}/availability", "online", retain=True)
    # Send Discovery EVERY time we connect (Robustness fix)
    publish_discovery(client)

def connect_mqtt():
    client = mqtt.Client()
    if MQTT_USER and MQTT_PASS:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    
    # Last Will
    client.will_set(f"{MQTT_TOPIC_BASE}/availability", "offline", retain=True)
    
    client.on_connect = on_connect
    client.on_disconnect = lambda c, u, rc: print(f"⚠️ MQTT disconnected (rc={rc})")
    
    print(f"[MQTT] Connecting to {MQTT_BROKER}...")
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()
    return client

# Initialize Client
client = connect_mqtt()

# ============ MQTT command handling ============
def on_mqtt_message(client, userdata, msg):
    base = f"{MQTT_TOPIC_BASE}/cmd/"
    topic = msg.topic
    payload = (msg.payload or b"").decode("utf-8","ignore").strip().lower()
    if not topic.startswith(base):
        return

    sub = topic[len(base):]
    print(f"[cmd] {sub}  payload={payload!r}")

    COMMANDS = {
        "ac_up":              ac_temp_up,
        "ac_down":            ac_temp_down,
        "unlock":             unlock_car,
        "lock":               lock_car,
        "climate_rapid_heat": ac_rapid_heat,
        "climate_rapid_vent": ac_rapid_cool,
        "climate_switch_on":  ac_switch_on,
    }
    fn = COMMANDS.get(sub)
    if fn:
        CMD_Q.put(fn)
    else:
        print(f"[cmd] Unknown command: {sub}")

client.on_message = on_mqtt_message
client.subscribe(f"{MQTT_TOPIC_BASE}/cmd/#")

# ============ ADB helpers ============

def adb(cmd: str, wait: float = 0.5):
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
                vals[key] = txt
            elif key == "climate_target_temp_c":
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

PIN_CODE = os.getenv("BYD_PIN", "").strip()
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
    if not pin_prompt_present():
        return True
    _tap_xy(360, 1245, 0.08)
    enter_pin_via_coords(PIN_CODE)
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if not pin_prompt_present():
            return True
        time.sleep(0.2)
    return True

# ============ Home-page controls ============

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
    x,y = tiles[0]
    adb(f"input tap {x} {y}", 0.08)
    ensure_pin_if_needed()

def lock_car():
    tiles = _quick_control_tiles()
    if len(tiles) < 2:
        print("Lock: not enough quick-control tiles found."); return
    x,y = tiles[1]
    adb(f"input tap {x} {y}", 0.08)
    ensure_pin_if_needed()

# ============ Vehicle status (two pages) ============

def nearest_value_above(items, label_text, value_regex=None):
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
        if y1 >= ly2:
            if value_regex:
                if not value_regex.match(t):
                    continue
            cand.append((y1, x1, t))
    if cand:
        cand.sort()
        return cand[0][2]
    return None

def dump_vehicle_status_two_pages():
    adb("input swipe 333 316 333 948 250", 0.2)
    adb("input swipe 333 948 333 316 350", 0.5)

    if not tap_by_label("Vehicle status"):
        return None, None

    top_xml    = dump_xml("/sdcard/byd_status_top.xml",    "/app/byd_status_top.xml")
    adb("input swipe 333 948 333 316 450", 0.6)
    bottom_xml = dump_xml("/sdcard/byd_status_bottom.xml", "/app/byd_status_bottom.xml")
    return top_xml, bottom_xml

def parse_vehicle_status_two_pages(top_xml, bottom_xml):
    vals = {}
    items = []
    items.extend(gather_items(top_xml))
    items.extend(gather_items(bottom_xml))

    tyres = []
    for t,b in items:
        m = PSI_RX.match(t)
        if m:
            v = m.group(1)
            tyres.append( (b, v) )
    tyre_pos = []
    for b,v in tyres:
        m = BOUNDS_RX.search(b or "")
        if not m: continue
        x1,y1,x2,y2 = map(int, m.groups())
        tyre_pos.append((y1, x1, v))
    tyre_pos.sort()
    if len(tyre_pos) >= 4:
        vals["tire_pressure_fl"] = tyre_pos[0][2]
        vals["tire_pressure_fr"] = tyre_pos[1][2]
        vals["tire_pressure_rl"] = tyre_pos[2][2]
        vals["tire_pressure_rr"] = tyre_pos[3][2]

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

# ============ A/C page parsing ============

def open_ac_page():
    local = dump_xml("/sdcard/byd_home.xml", "/app/byd_home.xml")
    try:
        root = ET.parse(local).getroot()
    except Exception:
        return False
    for n in root.iter("node"):
        if n.attrib.get("resource-id") == "com.byd.bydautolink:id/c_air_item_rl_2":
            b = _bounds_str_to_tuple(n.attrib.get("bounds") or "")
            if b:
                x, y = _center(b)
                adb(f"input tap {x} {y}", 0.2)
                return True
    adb("input tap 190 1005", 0.2)
    return True

# ============ Climate page controls ============

def _tap_by_id_on_current_dump(resource_id: str, remote="/sdcard/byd_ac.xml", local="/app/byd_ac.xml", delay=0.08) -> bool:
    dump_xml(remote, local, wait=0.35)
    try:
        root = ET.parse(local).getroot()
    except Exception:
        print(f"[climate] could not parse dump {local}")
        return False

    for n in root.iter("node"):
        if n.attrib.get("resource-id", "") == resource_id:
            m = BOUNDS_RX.search(n.attrib.get("bounds", "") or "")
            if not m: continue
            x1, y1, x2, y2 = map(int, m.groups())
            x, y = ((x1 + x2) // 2, (y1 + y2) // 2)
            adb(f"input tap {x} {y}", delay)
            return True
    return False

def ac_rapid_heat():
    if not open_ac_page(): return
    _tap_by_id_on_current_dump("com.byd.bydautolink:id/c_air_item_heat_btn")
    time.sleep(0.3)
    adb("input keyevent 4", 0.4)

def ac_rapid_cool():
    if not open_ac_page(): return
    _tap_by_id_on_current_dump("com.byd.bydautolink:id/c_air_item_cool_btn")
    time.sleep(0.3)
    adb("input keyevent 4", 0.4)

def ac_switch_on():
    if not open_ac_page(): return
    ok = _tap_by_id_on_current_dump("com.byd.bydautolink:id/c_air_item_power_btn")
    if not ok:
        adb("input keyevent 4", 0.3)
        return
    ensure_pin_if_needed()
    # Confirm state
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
    except Exception:
        pass
    adb("input keyevent 4", 0.4)

def publish_values(vals: dict):
    if not vals:
        return
    for k, v in vals.items():
        client.publish(f"{MQTT_TOPIC_BASE}/{k}", v if isinstance(v, str) else json.dumps(v))
        print(f"➡️ Publish {MQTT_TOPIC_BASE}/{k} -> {v}")

# ============ Command worker ============

def _command_worker():
    print("[worker] Command worker started")
    while True:
        fn = CMD_Q.get()
        try:
            ensure_pin_if_needed()
            fn()
        except Exception as e:
            print(f"[worker] Command error: {e}")
        finally:
            CMD_Q.task_done()

def main_loop():
    while True:
        cycle = {}

        # --- HOME: Always polled ---
        home_xml = dump_xml("/sdcard/byd_home.xml", "/app/byd_home.xml")
        home_vals = extract_values_from_xml(home_xml)
        publish_values(home_vals)
        cycle.update(home_vals)

        # --- VEHICLE STATUS (Optional) ---
        if PAGES_ENABLED["vehicle_status"]:
            top_xml, bottom_xml = dump_vehicle_status_two_pages()
            if top_xml and bottom_xml:
                status_vals = parse_vehicle_status_two_pages(top_xml, bottom_xml)
                publish_values(status_vals)
                cycle.update(status_vals)
                adb("input keyevent 4", 0.5)  # back to home

        # --- GPS (Optional) ---
        if PAGES_ENABLED["vehicle_position"]:
            if tap_by_label("Vehicle position"):
                gps_xml = dump_xml("/sdcard/byd_gps.xml", "/app/byd_gps.xml", wait=0.6)
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

        # --- A/C (Optional) ---
        if PAGES_ENABLED["ac"]:
            if open_ac_page():
                ac_xml = dump_xml("/sdcard/byd_ac.xml", "/app/byd_ac.xml")
                try:
                    root = ET.parse(ac_xml).getroot()
                except Exception:
                    root = None
                ac_vals = {}
                if root is not None:
                    # Target temp
                    for n in root.iter("node"):
                        if n.attrib.get("resource-id") == "com.byd.bydautolink:id/tem_tv":
                            try:
                                ac_vals["climate_target_temp_c"] = float(n.attrib.get("text"))
                            except: pass
                    # Prev/Next
                    for n in root.iter("node"):
                        rid = n.attrib.get("resource-id") or ""
                        txt = (n.attrib.get("text") or "").strip()
                        if rid == "com.byd.bydautolink:id/tem_tv_last" and txt.isdigit():
                            ac_vals["climate_prev_temp_c"] = float(txt)
                        elif rid == "com.byd.bydautolink:id/tem_tv_next" and txt.isdigit():
                            ac_vals["climate_next_temp_c"] = float(txt)
                    # Power status
                    power_label = None
                    for n in root.iter("node"):
                        if n.attrib.get("resource-id") == "com.byd.bydautolink:id/c_air_item_power_btn":
                            for m in n.iter("node"):
                                low = (m.attrib.get("text") or "").strip().lower()
                                if low in ("switch on", "switch off"):
                                    power_label = low; break
                            break
                    if power_label:
                        ac_vals["climate_power"] = "on" if "on" in power_label else "off"

                if ac_vals:
                    publish_values(ac_vals)
                    cycle.update(ac_vals)
                adb("input keyevent 4", 0.5)
            else:
                print("⚠️ Could not open A/C page")

        time.sleep(POLL_SECONDS)

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