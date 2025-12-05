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
from datetime import datetime, timedelta

# ============ Settings (env overrides supported) ============
MQTT_BROKER      = os.getenv("MQTT_BROKER", "192.168.1.40")
MQTT_PORT        = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER        = os.getenv("MQTT_USER", "user")
MQTT_PASS        = os.getenv("MQTT_PASS", "password")
MQTT_TOPIC_BASE  = os.getenv("MQTT_TOPIC_BASE", "byd/app")

DEVICE_ID        = os.getenv("BYD_DEVICE_ID", "byd-app-phone")
DEVICE_NAME      = os.getenv("BYD_DEVICE_NAME", "BYD App Bridge")

# --- Polling Intervals (Adaptive) ---
POLL_ACTIVE_SECONDS   = int(os.getenv("POLL_SECONDS", "60"))
POLL_CHARGING_SECONDS = int(os.getenv("POLL_CHARGING_SECONDS", "30"))
POLL_IDLE_SECONDS     = int(os.getenv("POLL_IDLE_SECONDS", "600"))
IDLE_TIMEOUT_MINS     = 15

# --- Page selection flags ---
POLL_VEHICLE_STATUS    = int(os.getenv("POLL_VEHICLE_STATUS", "1"))
POLL_AC                = int(os.getenv("POLL_AC", "1"))
POLL_VEHICLE_POSITION  = int(os.getenv("POLL_VEHICLE_POSITION", "1"))

PAGES_ENABLED = {
    "home": True,
    "vehicle_status": bool(POLL_VEHICLE_STATUS),
    "ac": bool(POLL_AC),
    "vehicle_position": bool(POLL_VEHICLE_POSITION),
}

# ---- State Tracking ----
ADB_LOCK = threading.RLock()
SEQUENCE_LOCK = threading.RLock()
CMD_Q = queue.Queue()
WAKE_EVENT = threading.Event()

GLOBAL_STATE = {
    "last_command_time": time.time(), # Start in ACTIVE mode
    "is_charging": False,
    "consecutive_errors": 0,
    "last_valid_data_time": time.time(),
    "app_running": True
}

# ============ UI selectors (resource-ids) ============
XML_MAP = {
    "com.byd.bydautolink:id/h_km_tv":              "range_km",
    "com.byd.bydautolink:id/tv_batter_percentage": "battery_soc",
    "com.byd.bydautolink:id/tv_charging_tip":      "charging_tip",
    "com.byd.bydautolink:id/tv_banner":            "warning",
    "com.byd.bydautolink:id/car_inner_temperature":"cabin_temp",
    "com.byd.bydautolink:id/tem_tv":               "climate_target_temp_c",
    "com.byd.bydautolink:id/h_car_name_tv":        "car_status",
    "com.byd.bydautolink:id/tv_update_time":       "last_update",
}

BOUNDS_RX   = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
PSI_RX      = re.compile(r"^\s*(\d{2}\.\d)\s*psi\s*$", re.IGNORECASE)
KM_RX       = re.compile(r"^\s*([\d,]+)\s*km\s*$", re.IGNORECASE)
KWH100_RX   = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*kW\s*·?\s*h/100km\s*$", re.IGNORECASE)
LATLON_RX   = re.compile(r"(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)")

# ============ ADB & App Management ============

def adb(cmd: str, wait: float = 0.5):
    with ADB_LOCK:
        subprocess.run(["adb", "shell"] + cmd.split(), check=False)
    if wait:
        time.sleep(wait)

def restart_app():
    print("🚑 [Self-Healing] App seems stuck. Restarting BYD App...")
    adb("am force-stop com.byd.bydautolink")
    time.sleep(2)
    adb("monkey -p com.byd.bydautolink -c android.intent.category.LAUNCHER 1")
    time.sleep(15)
    GLOBAL_STATE["consecutive_errors"] = 0
    print("🚑 [Self-Healing] Restart command sent.")

def dump_xml(remote="/sdcard/_byd_ui.xml", local="/app/_byd_ui.xml", wait=0.8):
    with ADB_LOCK:
        if os.path.exists(local): os.remove(local)
        subprocess.run(["adb", "shell", "uiautomator", "dump", remote], 
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.2)
        subprocess.run(["adb", "pull", remote, local],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if os.path.exists(local) and os.path.getsize(local) > 0:
        try:
            ET.parse(local)
            GLOBAL_STATE["consecutive_errors"] = 0
            GLOBAL_STATE["last_valid_data_time"] = time.time()
            return local
        except ET.ParseError:
            pass 
    
    GLOBAL_STATE["consecutive_errors"] += 1
    err_count = GLOBAL_STATE["consecutive_errors"]
    print(f"⚠️ UI Dump failed (Attempt {err_count}/3)")
    
    if err_count >= 3:
        restart_app()
    return None

def is_command_pending():
    return not CMD_Q.empty()

# ============ MQTT Discovery ============

def publish_discovery(client):
    print("[MQTT] Sending Smart Home Assistant Discovery payloads...")
    device = {
        "identifiers": [DEVICE_ID],
        "name": DEVICE_NAME,
        "manufacturer": "BYD",
        "model": "Autolink",
    }

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
        client.publish(f"homeassistant/sensor/{uniq}/config", json.dumps(cfg), retain=True)

    def _purge_config(uniq):
        client.publish(f"homeassistant/sensor/{uniq}/config", "", retain=True)

    registry = [
        ("home", "BYD Range",           "byd_range_km",         f"{MQTT_TOPIC_BASE}/range_km",             "km", "distance", None, None),
        ("home", "BYD Battery SoC",     "byd_battery_soc",      f"{MQTT_TOPIC_BASE}/battery_soc",          "%",  "battery",  None, None),
        ("home", "BYD Car Status",      "byd_car_status",       f"{MQTT_TOPIC_BASE}/car_status",           None, None,       None, None),
        ("home", "BYD Last Update",     "byd_last_update",      f"{MQTT_TOPIC_BASE}/last_update",          None, None,       None, None),
        ("home", "BYD A/C Target Temp", "byd_ac_target_temp",   f"{MQTT_TOPIC_BASE}/climate_target_temp_c","°C", "temperature", None, None),
        ("ac",   "BYD A/C PAST Temp",   "byd_ac_prev_temp",     f"{MQTT_TOPIC_BASE}/climate_prev_temp_c",  "°C", "temperature", None, None),
        ("ac",   "BYD A/C NEXT Temp",   "byd_ac_next_temp",     f"{MQTT_TOPIC_BASE}/climate_next_temp_c",  "°C", "temperature", None, None),
        ("ac",   "BYD A/C Power",       "byd_ac_power",         f"{MQTT_TOPIC_BASE}/climate_power",        None, None,       None, None),
        ("vehicle_status", "BYD Tyre FL", "byd_tire_pressure_fl", f"{MQTT_TOPIC_BASE}/tire_pressure_fl", "psi", None, None, None),
        ("vehicle_status", "BYD Tyre FR", "byd_tire_pressure_fr", f"{MQTT_TOPIC_BASE}/tire_pressure_fr", "psi", None, None, None),
        ("vehicle_status", "BYD Tyre RL", "byd_tire_pressure_rl", f"{MQTT_TOPIC_BASE}/tire_pressure_rl", "psi", None, None, None),
        ("vehicle_status", "BYD Tyre RR", "byd_tire_pressure_rr", f"{MQTT_TOPIC_BASE}/tire_pressure_rr", "psi", None, None, None),
        ("vehicle_status", "BYD Doors",   "byd_doors",             f"{MQTT_TOPIC_BASE}/doors",            None, None, None, None),
        ("vehicle_status", "BYD Windows", "byd_windows",           f"{MQTT_TOPIC_BASE}/windows",          None, None, None, None),
        ("vehicle_status", "BYD Bonnet",  "byd_front_bonnet",      f"{MQTT_TOPIC_BASE}/front_bonnet",     None, None, None, None),
        ("vehicle_status", "BYD Boot",    "byd_boot",              f"{MQTT_TOPIC_BASE}/boot",             None, None, None, None),
        ("vehicle_status", "BYD Whole Vehicle", "byd_whole_vehicle", f"{MQTT_TOPIC_BASE}/whole_vehicle_status", None, None, None, None),
        ("vehicle_status", "BYD Driving Status","byd_driving_status",f"{MQTT_TOPIC_BASE}/driving_status",   None, None, None, None),
        ("vehicle_status", "BYD Odometer",      "byd_odometer",      f"{MQTT_TOPIC_BASE}/odometer",         "km", "distance", None, None),
        ("vehicle_status", "BYD Recent Energy", "byd_recent_energy", f"{MQTT_TOPIC_BASE}/recent_energy_value", "kWh/100km", None, None, None),
        ("vehicle_status", "BYD Total Energy",  "byd_total_energy",  f"{MQTT_TOPIC_BASE}/total_energy_value",  "kWh/100km", None, None, None),
        ("vehicle_position","BYD GPS JSON",     "byd_gps_json",      f"{MQTT_TOPIC_BASE}/location",         None, None, "{{ value | default('') }}", "mdi:map-marker"),
    ]

    for page, name, uniq, stat_t, unit, dev_cla, val_tpl, icon in registry:
        if PAGES_ENABLED.get(page, False):
            _publish_config(uniq, _sensor_cfg(name, uniq, stat_t, unit, dev_cla, val_tpl, icon))
        else:
            _purge_config(uniq)

    def disc_button(name, uniq, cmd_t, payload="press", icon=None):
        cfg = {"name": name, "uniq_id": uniq, "cmd_t": cmd_t, "pl_prs": payload, "dev": device,
               "avty_t": f"{MQTT_TOPIC_BASE}/availability", "pl_avail": "online", "pl_not_avail": "offline"}
        if icon: cfg["ic"] = icon
        client.publish(f"homeassistant/button/{uniq}/config", json.dumps(cfg), retain=True)

    disc_button("BYD A/C Temp ▲", "byd_cmd_ac_up", f"{MQTT_TOPIC_BASE}/cmd/ac_up", icon="mdi:thermometer-plus")
    disc_button("BYD A/C Temp ▼", "byd_cmd_ac_down", f"{MQTT_TOPIC_BASE}/cmd/ac_down", icon="mdi:thermometer-minus")
    disc_button("BYD Unlock", "byd_cmd_unlock", f"{MQTT_TOPIC_BASE}/cmd/unlock", icon="mdi:lock-open-variant")
    disc_button("BYD Lock", "byd_cmd_lock", f"{MQTT_TOPIC_BASE}/cmd/lock", icon="mdi:lock")
    disc_button("BYD Rapid Heat", "byd_cmd_climate_heat", f"{MQTT_TOPIC_BASE}/cmd/climate_rapid_heat", icon="mdi:fire")
    disc_button("BYD Rapid Vent", "byd_cmd_climate_vent", f"{MQTT_TOPIC_BASE}/cmd/climate_rapid_vent", icon="mdi:fan")
    disc_button("BYD A/C Switch On", "byd_cmd_climate_on", f"{MQTT_TOPIC_BASE}/cmd/climate_switch_on", icon="mdi:power")
    print("[MQTT] Discovery refresh complete.")

# ============ MQTT Connection ============

def on_connect(client, userdata, flags, rc):
    print(f"[MQTT] Connected with result code {rc}")
    client.publish(f"{MQTT_TOPIC_BASE}/availability", "online", retain=True)
    publish_discovery(client)

def connect_mqtt():
    client = mqtt.Client()
    if MQTT_USER and MQTT_PASS:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.will_set(f"{MQTT_TOPIC_BASE}/availability", "offline", retain=True)
    client.on_connect = on_connect
    client.on_disconnect = lambda c, u, rc: print(f"⚠️ MQTT disconnected (rc={rc})")
    print(f"[MQTT] Connecting to {MQTT_BROKER}...")
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()
    return client

client = connect_mqtt()

# ============ MQTT command handling ============

def on_mqtt_message(client, userdata, msg):
    base = f"{MQTT_TOPIC_BASE}/cmd/"
    topic = msg.topic
    if not topic.startswith(base): return
    sub = topic[len(base):]
    COMMANDS = {
        "ac_up":              ac_temp_up,
        "ac_down":            ac_temp_down,
        "unlock":             unlock_car,
        "lock":               lock_car,
        "climate_rapid_heat": ac_rapid_heat,
        "climate_rapid_vent": ac_rapid_cool,
        "climate_switch_on":  ac_switch_on,
    }
    if sub in COMMANDS:
        CMD_Q.put(COMMANDS[sub])
    else:
        print(f"[cmd] Unknown: {sub}")

client.on_message = on_mqtt_message
client.subscribe(f"{MQTT_TOPIC_BASE}/cmd/#")

# ============ Parsing & Helpers ============

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
        
        # Ghosting Protection
        if not txt or txt == "--" or txt == "---":
            if rid != "com.byd.bydautolink:id/c_air_item_power_btn":
                continue

        if rid in XML_MAP:
            key = XML_MAP[rid]
            if key == "charging_tip":
                if "charging" in txt.lower() or "remaining" in txt.lower():
                    GLOBAL_STATE["is_charging"] = True
                else:
                    GLOBAL_STATE["is_charging"] = False

            if key == "range_km":
                m = KM_RX.match(txt)
                vals[key] = m.group(1).replace(",", "") if m else txt
            elif key == "battery_soc":
                vals[key] = txt
            elif key == "climate_target_temp_c":
                try:
                    vals[key] = float(txt)
                except ValueError: pass
            else:
                vals[key] = txt

        if rid == "com.byd.bydautolink:id/tem_tv_last" and txt.isdigit():
             vals["climate_prev_temp_c"] = float(txt)
        elif rid == "com.byd.bydautolink:id/tem_tv_next" and txt.isdigit():
             vals["climate_next_temp_c"] = float(txt)
        
        if rid == "com.byd.bydautolink:id/c_air_item_power_btn":
            for m in node.iter("node"):
                low = (m.attrib.get("text") or "").strip().lower()
                if low in ("switch on", "switch off"):
                    vals["climate_power"] = "on" if "on" in low else "off"
                    break

    return vals

# ============ Actions ============

def go_home(max_retries=3):
    for i in range(max_retries):
        local = dump_xml(remote="/sdcard/_check_home.xml", local="/app/_check_home.xml", wait=0.3)
        if not local: continue
        
        found = False
        try:
            root = ET.parse(local).getroot()
            for n in root.iter("node"):
                if n.attrib.get("resource-id") == "com.byd.bydautolink:id/h_km_tv":
                    found = True; break
                if _text(n) == "My car" and "tab" in (n.attrib.get("resource-id") or ""):
                    found = True; break
        except: pass
        
        if found:
            return True
        
        print(f"🔄 Not on Home screen. Pressing Back (Attempt {i+1})...")
        adb("input keyevent 4", 0.3) 
    
    print("⚠️ Failed to reach Home screen after retries.")
    return False

def tap_by_label(label: str):
    local = "/app/_byd_home.xml"
    if not os.path.exists(local):
        dump_xml(local=local)
    
    try:
        root = ET.parse(local).getroot()
    except Exception:
        return False

    target = None
    def walk(n, path):
        nonlocal target
        if _text(n) == label:
            target = path + [n]; return
        for c in n:
            walk(c, path + [n])
            if target: break

    walk(root, [])
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

    if b:
        x,y = _center(b)
        adb(f"input tap {x} {y}", 0.1)
        return True
    return False

# ... PIN helpers ...
PIN_CODE = os.getenv("BYD_PIN", "").strip()
PIN_TAPS = {
    "1": (170,  680), "2": (360,  680), "3": (550,  680),
    "4": (170,  870), "5": (360,  870), "6": (550,  870),
    "7": (170, 1060), "8": (360, 1060), "9": (550, 1060),
    "0": (360, 1255),
}

def ensure_pin_if_needed():
    if not PIN_CODE: return
    local = "/app/_pin_check.xml"
    if not dump_xml(remote="/sdcard/_byd_pin.xml", local=local, wait=0.3):
        return
    is_pin = False
    try:
        root = ET.parse(local).getroot()
        for n in root.iter("node"):
             if "password" in (n.attrib.get("text") or "").lower():
                 is_pin = True; break
    except: pass

    if is_pin:
        print("🔐 PIN Prompt detected. Entering PIN...")
        adb(f"input tap 360 1245", 0.1) 
        for ch in PIN_CODE:
            if ch in PIN_TAPS:
                x,y = PIN_TAPS[ch]
                adb(f"input tap {x} {y}", 0.12)
        time.sleep(1)

# ... Action Functions ...
def _get_home_node(rid):
    local = "/app/_byd_home.xml"
    if not os.path.exists(local): dump_xml(local=local)
    try:
        root = ET.parse(local).getroot()
        for n in root.iter("node"):
            if n.attrib.get("resource-id") == rid:
                return _bounds_str_to_tuple(n.attrib.get("bounds"))
    except: pass
    return None

def ac_temp_up():
    b = _get_home_node("com.byd.bydautolink:id/btn_temperature_plus")
    if b: adb(f"input tap {_center(b)[0]} {_center(b)[1]}")
def ac_temp_down():
    b = _get_home_node("com.byd.bydautolink:id/btn_temperature_reduce")
    if b: adb(f"input tap {_center(b)[0]} {_center(b)[1]}")

def _quick_control_tap(idx):
    local = "/app/_byd_home.xml"
    if not os.path.exists(local): dump_xml(local=local)
    tiles = []
    try:
        root = ET.parse(local).getroot()
        for n in root.iter("node"):
            if n.attrib.get("resource-id") == "com.byd.bydautolink:id/btn_ble_control_layout":
                b = _bounds_str_to_tuple(n.attrib.get("bounds"))
                if b: tiles.append(_center(b))
    except: pass
    tiles.sort(key=lambda p: p[0])
    if idx < len(tiles):
        adb(f"input tap {tiles[idx][0]} {tiles[idx][1]}")
        ensure_pin_if_needed()

def unlock_car(): _quick_control_tap(0)
def lock_car():   _quick_control_tap(1)

# ... Vehicle Status Parsing ...
def gather_items(local_xml_path):
    items = []
    try:
        root = ET.parse(local_xml_path).getroot()
        for n in root.iter("node"):
            t = (n.attrib.get("text") or "").strip()
            if t: items.append((t, n.attrib.get("bounds")))
    except: pass
    return items

def nearest_value_above(items, label_text, value_regex=None):
    label_bounds = None
    for t,b in items:
        if t == label_text:
            m = BOUNDS_RX.search(b or "")
            if m: label_bounds = tuple(map(int, m.groups())); break
    if not label_bounds: return None
    
    cand = []
    for t,b in items:
        m = BOUNDS_RX.search(b or "")
        if not m: continue
        coords = tuple(map(int, m.groups()))
        if coords[1] >= label_bounds[3]: 
            if value_regex and not value_regex.match(t): continue
            cand.append((coords[1], coords[0], t))
    
    if cand:
        cand.sort()
        return cand[0][2]
    return None

def parse_vehicle_status_two_pages():
    adb("input swipe 333 316 333 948 250", 0.2)
    if is_command_pending(): 
        adb("input keyevent 4"); return {}

    adb("input swipe 333 948 333 316 350", 0.5)
    if is_command_pending(): 
        adb("input keyevent 4"); return {}

    if not tap_by_label("Vehicle status"): return {}
    
    top = dump_xml(remote="/sdcard/st1.xml", local="/app/st1.xml")
    if is_command_pending(): 
        adb("input keyevent 4"); return {}

    adb("input swipe 333 948 333 316 450", 0.6)
    
    bot = dump_xml(remote="/sdcard/st2.xml", local="/app/st2.xml")
    
    vals = {}
    if not top or not bot: 
        adb("input keyevent 4"); return vals

    items = gather_items(top) + gather_items(bot)
    
    # Tires - Extract the NUMBER, not the text
    tyres = []
    for t,b in items:
        m_psi = PSI_RX.match(t)
        if m_psi:
            m_bounds = BOUNDS_RX.search(b or "")
            if m_bounds: 
                # Extract float value of PSI to be clean
                val = m_psi.group(1) 
                tyres.append((int(m_bounds.group(2)), int(m_bounds.group(1)), val))
    tyres.sort() 
    if len(tyres) >= 4:
        vals["tire_pressure_fl"] = tyres[0][2]
        vals["tire_pressure_fr"] = tyres[1][2]
        vals["tire_pressure_rl"] = tyres[2][2]
        vals["tire_pressure_rr"] = tyres[3][2]

    lmap = {"Front hood":"front_bonnet", "Door":"doors", "Window":"windows", "Trunk":"boot",
            "Whole vehicle":"whole_vehicle_status", "Driving status":"driving_status", "Odometer":"odometer",
            "Recent energy": "byd_recent_energy", "Total energy": "byd_total_energy"}
    
    for lab, k in lmap.items():
        regex = KWH100_RX if "energy" in k else None
        v = nearest_value_above(items, lab, value_regex=regex)
        if v and v not in ("--", "---"):
            if k == "odometer":
                m = KM_RX.match(v)
                vals[k] = m.group(1).replace(",", "") if m else v
            else:
                vals[k] = v
    
    adb("input keyevent 4", 0.5)
    return vals

# ... AC functions ...
def open_ac_page():
    local = "/app/_byd_home.xml"
    if not os.path.exists(local): dump_xml(local=local)
    try:
        root = ET.parse(local).getroot()
        for n in root.iter("node"):
            if n.attrib.get("resource-id") == "com.byd.bydautolink:id/c_air_item_rl_2":
                 b = _bounds_str_to_tuple(n.attrib.get("bounds"))
                 if b: adb(f"input tap {_center(b)[0]} {_center(b)[1]}"); return True
    except: pass
    adb("input tap 190 1005", 0.2)
    return True

def ac_action(res_id):
    if not open_ac_page(): return
    path = dump_xml(remote="/sdcard/ac.xml", local="/app/ac.xml")
    if path:
        try:
            root = ET.parse(path).getroot()
            for n in root.iter("node"):
                if n.attrib.get("resource-id") == res_id:
                     b = _bounds_str_to_tuple(n.attrib.get("bounds"))
                     if b: adb(f"input tap {_center(b)[0]} {_center(b)[1]}")
        except: pass
    time.sleep(0.5)
    adb("input keyevent 4")

def ac_rapid_heat(): ac_action("com.byd.bydautolink:id/c_air_item_heat_btn")
def ac_rapid_cool(): ac_action("com.byd.bydautolink:id/c_air_item_cool_btn")
def ac_switch_on(): 
    if not open_ac_page(): return
    path = dump_xml(remote="/sdcard/ac.xml", local="/app/ac.xml")
    if path:
        try:
            root = ET.parse(path).getroot()
            for n in root.iter("node"):
                if n.attrib.get("resource-id") == "com.byd.bydautolink:id/c_air_item_power_btn":
                     b = _bounds_str_to_tuple(n.attrib.get("bounds"))
                     if b: adb(f"input tap {_center(b)[0]} {_center(b)[1]}")
        except: pass
        
    ensure_pin_if_needed()
    time.sleep(1.0)
    path = dump_xml(remote="/sdcard/ac.xml", local="/app/ac.xml")
    if path:
        ac_vals = extract_values_from_xml(path)
        if "climate_power" in ac_vals:
            print(f"✅ AC State Confirmed: {ac_vals['climate_power']}")
            client.publish(f"{MQTT_TOPIC_BASE}/climate_power", ac_vals["climate_power"])
    adb("input keyevent 4")

def publish_values(vals: dict):
    if not vals: 
        print("⚠️ XML Parsed but 0 matching values found. (Layout mismatch?)")
        return
    for k, v in vals.items():
        print(f"➡️  {k}: {v}")
        client.publish(f"{MQTT_TOPIC_BASE}/{k}", v if isinstance(v, str) else json.dumps(v))

# ============ Main Loops ============

def _command_worker():
    while True:
        fn = CMD_Q.get()
        try:
            GLOBAL_STATE["last_command_time"] = time.time()
            with SEQUENCE_LOCK:
                print("[worker] Executing Priority Command...")
                go_home()
                ensure_pin_if_needed()
                fn()
            WAKE_EVENT.set()
        except Exception as e:
            print(f"[worker] Error: {e}")
        finally:
            CMD_Q.task_done()

def main_loop():
    print(f"🚀 Bridge Started. Active Poll: {POLL_ACTIVE_SECONDS}s, Idle: {POLL_IDLE_SECONDS}s")
    
    while True:
        now = time.time()
        time_since_cmd = now - GLOBAL_STATE["last_command_time"]
        
        mode = "ACTIVE"
        sleep_time = POLL_ACTIVE_SECONDS

        if GLOBAL_STATE["is_charging"]:
            mode = "CHARGING"
            sleep_time = POLL_CHARGING_SECONDS
        elif time_since_cmd > (IDLE_TIMEOUT_MINS * 60):
            mode = "IDLE"
            sleep_time = POLL_IDLE_SECONDS
        
        print(f"🔄 Polling ({mode})...")
        cycle_vals = {}

        with SEQUENCE_LOCK:
            home_xml = dump_xml("/sdcard/byd_home.xml", "/app/byd_home.xml")
            if home_xml:
                home_vals = extract_values_from_xml(home_xml)
                publish_values(home_vals)
                cycle_vals.update(home_vals)
        
        if not CMD_Q.empty(): 
            print("⏳ Yielding for command...")
            time.sleep(1); continue

        if PAGES_ENABLED["vehicle_status"] and mode != "IDLE": 
            with SEQUENCE_LOCK:
                vals = parse_vehicle_status_two_pages()
                publish_values(vals)
                cycle_vals.update(vals)

        if PAGES_ENABLED["vehicle_position"] and mode != "IDLE":
             with SEQUENCE_LOCK:
                if tap_by_label("Vehicle position"):
                    gps_xml = dump_xml("/sdcard/gps.xml", "/app/gps.xml")
                    if gps_xml:
                        try:
                            root = ET.parse(gps_xml).getroot()
                            for n in root.iter("node"):
                                txt = _text(n)
                                m = LATLON_RX.search(txt)
                                if m:
                                    pl = {"latitude": float(m.group(1)), "longitude": float(m.group(2)), "gps_accuracy": 10}
                                    client.publish(f"{MQTT_TOPIC_BASE}/location", json.dumps(pl))
                                    break
                        except: pass
                    adb("input keyevent 4")
        
        if PAGES_ENABLED["ac"] and mode != "IDLE":
             with SEQUENCE_LOCK:
                 if open_ac_page():
                     ac_xml = dump_xml("/sdcard/ac_full.xml", "/app/ac_full.xml")
                     if ac_xml:
                         ac_vals = extract_values_from_xml(ac_xml)
                         publish_values(ac_vals)
                     adb("input keyevent 4")

        if WAKE_EVENT.wait(timeout=sleep_time):
            print("⚡ Woke up for command feedback!")
            WAKE_EVENT.clear()

if __name__ == "__main__":
    try:
        threading.Thread(target=_command_worker, daemon=True).start()
        main_loop()
    except KeyboardInterrupt:
        print("🚪 Exiting.")
        client.publish(f"{MQTT_TOPIC_BASE}/availability", "offline", retain=True)
        client.disconnect()