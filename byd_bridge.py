#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import subprocess
import xml.etree.ElementTree as ET
import paho.mqtt.client as mqtt

# ============ Settings (env overrides supported) ============
MQTT_BROKER      = os.getenv("MQTT_BROKER", "192.168.1.40")
MQTT_PORT        = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER        = os.getenv("MQTT_USER", "user")
MQTT_PASS        = os.getenv("MQTT_PASS", "password")
MQTT_TOPIC_BASE  = os.getenv("MQTT_TOPIC_BASE", "byd/app")

POLL_SECONDS     = int(os.getenv("POLL_SECONDS", "60"))
DEVICE_ID        = os.getenv("BYD_DEVICE_ID", "byd-app-phone")
DEVICE_NAME      = os.getenv("BYD_DEVICE_NAME", "BYD App Bridge")

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

# ============ MQTT ============

def connect_mqtt():
    client = mqtt.Client()
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    # Last Will: set offline if we die
    client.will_set(f"{MQTT_TOPIC_BASE}/availability", "offline", retain=True)
    client.on_disconnect = lambda c, u, rc: print("⚠️ MQTT disconnected (rc=%s)" % rc)
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()
    # Mark online
    client.publish(f"{MQTT_TOPIC_BASE}/availability", "online", retain=True)
    return client

client = connect_mqtt()

def publish_discovery_once():
    """Minimal HA discovery for the most important sensors."""
    device = {
        "identifiers": [DEVICE_ID],
        "name": DEVICE_NAME,
        "manufacturer": "BYD (via UI scrape)",
        "model": "Autolink",
    }

    def disc_sensor(name, uniq, stat_t, unit=None, dev_cla=None, value_tpl=None):
        cfg = {
            "name": name, "uniq_id": uniq, "stat_t": stat_t,
            "dev": device, "avty_t": f"{MQTT_TOPIC_BASE}/availability",
            "pl_avail": "online", "pl_not_avail": "offline",
        }
        if unit: cfg["unit_of_meas"] = unit
        if dev_cla: cfg["dev_cla"] = dev_cla
        if value_tpl: cfg["val_tpl"] = value_tpl
        topic = f"homeassistant/sensor/{uniq}/config"
        client.publish(topic, json.dumps(cfg), retain=True)

    # A few key sensors:
    disc_sensor("BYD Range",           "byd_range_km",          f"{MQTT_TOPIC_BASE}/range_km", unit="km")
    disc_sensor("BYD Battery SoC",     "byd_battery_soc",       f"{MQTT_TOPIC_BASE}/battery_soc", unit="%", dev_cla="battery")
    disc_sensor("BYD Car Status",      "byd_car_status",        f"{MQTT_TOPIC_BASE}/car_status")
    disc_sensor("BYD Last Update",     "byd_last_update",       f"{MQTT_TOPIC_BASE}/last_update")
    disc_sensor("BYD A/C Target Temp", "byd_ac_target_temp",    f"{MQTT_TOPIC_BASE}/climate_target_temp_c", unit="°C", dev_cla="temperature")
    disc_sensor("BYD Odometer",        "byd_odometer",          f"{MQTT_TOPIC_BASE}/odometer", unit="km", dev_cla="distance")
    # Tyres (PSI as string; HA will still show them)
    for axle in ("fl","fr","rl","rr"):
        disc_sensor(f"BYD Tyre {axle.upper()} (psi)", f"byd_tire_{axle}", f"{MQTT_TOPIC_BASE}/tire_pressure_{axle}", unit="psi")
    # A/C power as sensor (string). Could be made a switch if we add write actions.
    disc_sensor("BYD A/C Power", "byd_ac_power", f"{MQTT_TOPIC_BASE}/climate_power")

publish_discovery_once()

# ============ ADB helpers ============

def adb(cmd: str, wait: float = 0.5):
    """Run an adb shell command."""
    subprocess.run(["adb", "shell"] + cmd.split(), check=False)
    if wait: time.sleep(wait)

def dump_xml(remote="/sdcard/_byd_ui.xml", local="/app/_byd_ui.xml", wait=0.7):
    adb(f"uiautomator dump {remote}", wait)
    subprocess.run(["adb", "pull", remote, local],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return local

def _bounds_str_to_tuple(s: str):
    m = BOUNDS_RX.search(s or "")
    return tuple(map(int, m.groups())) if m else None

def _center(bounds):
    x1,y1,x2,y2 = bounds
    return (x1+x2)//2, (y1+y2)//2

def tap_by_label(label: str) -> bool:
    """Find text==label on the current page and tap its clickable/container ancestor."""
    xml = dump_xml("/sdcard/_byd_home.xml", "/app/_byd_home.xml", wait=0.6)
    root = ET.parse(xml).getroot()

    target = None
    path = []

    def walk(n):
        nonlocal target
        if target: return
        path.append(n)
        if (n.attrib.get("text") or "").strip().lower() == label.strip().lower():
            target = list(path)
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
    adb(f"input tap {x} {y}", 0.7)
    return True

# ============ Generic parsers ============

def extract_values_from_xml(xml_file: str):
    """Parse a dumped UI xml using XML_MAP + simple heuristics."""
    values = {}
    root = ET.parse(xml_file).getroot()

    for node in root.iter("node"):
        rid = node.attrib.get("resource-id", "")
        text = (node.attrib.get("text") or "").strip()
        bounds = node.attrib.get("bounds", "")
        cls = node.attrib.get("class", "")

        if not text:
            continue

        if rid in XML_MAP:
            values[XML_MAP[rid]] = text
        else:
            # Optional: other heuristics can go here
            pass

        # GPS label fallback (if a lat,lon ever appears as a TextView)
        if cls == "android.widget.TextView":
            m = LATLON_RX.search(text)
            if m:
                values["car_position"] = f"{m.group(1)},{m.group(2)}"

    return values

# ============ Vehicle status: two-page capture & parse ============

def gather_items(xml_path):
    items = []
    root = ET.parse(xml_path).getroot()
    for n in root.iter("node"):
        txt = (n.attrib.get("text") or "").strip()
        if not txt:
            continue
        b = _bounds_str_to_tuple(n.attrib.get("bounds"))
        if not b:
            continue
        items.append((txt, b))
    return items

def row_value(items, label_txt, y_tol=40, x_pad=20):
    """Find the value text on the same row as label_txt, to the right."""
    lab = [(t,b) for t,b in items if t.lower() == label_txt.lower()]
    for _, b in lab:
        ymid = (b[1]+b[3])//2
        cand = []
        for t2, b2 in items:
            y2 = (b2[1]+b2[3])//2; x2 = (b2[0]+b2[2])//2
            if abs(y2 - ymid) <= y_tol and x2 > (b[2] + x_pad):
                cand.append((abs(y2-ymid), x2, t2))
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
            x1,y1,x2,y2 = b
            cx = (x1+x2)//2; cy = (y1+y2)//2
            tyres.append((cy, cx, v))
    if tyres:
        tyres.sort()  # top→bottom then left→right
        if len(tyres) >= 1: vals["tire_pressure_fl"] = tyres[0][2]
        if len(tyres) >= 2: vals["tire_pressure_fr"] = tyres[1][2]
        if len(tyres) >= 3: vals["tire_pressure_rl"] = tyres[2][2]
        if len(tyres) >= 4: vals["tire_pressure_rr"] = tyres[3][2]

    # Row labels
    for label, key in [
        ("Front bonnet", "front_bonnet"),
        ("Doors",        "doors"),
        ("Windows",      "windows"),
        ("Boot",         "boot"),
        ("Whole Vehicle Status", "whole_vehicle_status"),
        ("Driving status",       "driving_status"),
        ("Past 50 km Electricity","past_50km_kwh_per_100km"),
        ("Cumulative AEC",       "cumulative_aec_kwh_per_100km"),
    ]:
        v = row_value(items, label)
        if v:
            vals[key] = v

    # Normalise kWh/100km numerics if present
    if "past_50km_kwh_per_100km" in vals:
        m = KWH100_RX.search(vals["past_50km_kwh_per_100km"])
        if m: vals["past_50km_kwh_per_100km"] = m.group(1)
    if "cumulative_aec_kwh_per_100km" in vals:
        m = KWH100_RX.search(vals["cumulative_aec_kwh_per_100km"])
        if m: vals["cumulative_aec_kwh_per_100km"] = m.group(1)

    # Odometer: take biggest "### km" text
    km_cand = [(t,b) for t,b in items if KM_RX.match(t)]
    if km_cand:
        km_cand.sort(key=lambda tb: (tb[1][2]-tb[1][0])*(tb[1][3]-tb[1][1]), reverse=True)
        km_txt = km_cand[0][0]
        vals["odometer"] = KM_RX.match(km_txt).group(1).replace(",", "")

    return vals

# ============ Vehicle position (GPS) ============

def parse_and_publish_location(values: dict):
    """If 'car_position' exists (as 'lat,lon'), publish JSON location."""
    if "car_position" not in values:
        return
    try:
        lat, lon = map(lambda s: float(s.strip()), values["car_position"].split(","))
        payload = {
            "latitude": lat,
            "longitude": lon,
            "gps_accuracy": 10,
            "status": "driving"
        }
        client.publish(f"{MQTT_TOPIC_BASE}/location", json.dumps(payload))
        print(f"➡️ Publish {MQTT_TOPIC_BASE}/location -> {payload}")
    except Exception as e:
        print(f"Location parse error: {e}")

# ============ A/C page parsing ============

def parse_ac_page(xml_path: str):
    """
    Returns:
      - climate_target_temp_c (float)
      - climate_power ('on'/'off') inferred from button label 'Switch off/on'
    Optionally:
      - climate_prev_temp_c, climate_next_temp_c (floats) if present
    """
    vals = {}
    root = ET.parse(xml_path).getroot()

    # Target temp
    for n in root.iter("node"):
        if n.attrib.get("resource-id") == "com.byd.bydautolink:id/tem_tv":
            txt = (n.attrib.get("text") or "").replace("°","").replace("℃","").strip()
            try:
                vals["climate_target_temp_c"] = float(txt)
            except ValueError:
                pass
            break

    # Previous/next temps (optional)
    for n in root.iter("node"):
        rid = n.attrib.get("resource-id") or ""
        if rid == "com.byd.bydautolink:id/tem_tv_last":
            t = (n.attrib.get("text") or "").strip()
            if t.isdigit():
                vals["climate_prev_temp_c"] = float(t)
        elif rid == "com.byd.bydautolink:id/tem_tv_next":
            t = (n.attrib.get("text") or "").strip()
            if t.isdigit():
                vals["climate_next_temp_c"] = float(t)

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
        # If it says "Switch on", AC is currently off (pressing would turn it on)
        vals["climate_power"] = "off" if power_label == "switch on" else "on"

    return vals

def open_ac_page():
    """
    From home, tap the climate tile to open A/C. We aim at the setpoint TextView,
    then bias the tap slightly into its container to be safe.
    """
    xml = dump_xml("/sdcard/_home.xml", "/app/_home.xml", wait=0.5)
    root = ET.parse(xml).getroot()
    target = None
    for n in root.iter("node"):
        if n.attrib.get("resource-id") == "com.byd.bydautolink:id/tem_tv":
            target = n; break
    if target:
        b = _bounds_str_to_tuple(target.attrib.get("bounds"))
        if b:
            x1,y1,x2,y2 = b
            cx, cy = (x1+x2)//2 + 30, (y1+y2)//2 + 60
            adb(f"input tap {cx} {cy}", 0.8)
            return True
    # Fallback approximate tap (works on your layout)
    adb("input tap 160 1030", 0.8)
    return True

# ============ Publishing helpers ============

def _as_float(s):
    try:
        return float(str(s).replace("°","").strip())
    except Exception:
        return None

def publish_values(values: dict):
    if not values:
        return
    for key, val in values.items():
        topic = f"{MQTT_TOPIC_BASE}/{key}"
        # Normalise a few
        if key == "climate_target_temp_c":
            f = _as_float(val)
            if f is not None:
                client.publish(topic, f)
                print(f"➡️ Publish {topic} -> {f}")
                continue
        client.publish(topic, val)
        print(f"➡️ Publish {topic} -> {val}")

# ============ Main loop ============

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
            adb("input keyevent 4", 0.5)  # BACK to home
        else:
            print("⚠️ Could not open Vehicle status")

        # --- VEHICLE POSITION: open, dump, parse lat,lon, publish JSON, back
        if tap_by_label("Vehicle position"):
            gps_xml = dump_xml("/sdcard/byd_gps.xml", "/app/byd_gps.xml")
            gps_vals = extract_values_from_xml(gps_xml)
            parse_and_publish_location(gps_vals)
            adb("input keyevent 4", 0.5)
        else:
            print("⚠️ Vehicle position label not found")

        # --- A/C PAGE HOP: open, dump, parse power + setpoint, publish, back
        if open_ac_page():
            ac_xml = dump_xml("/sdcard/byd_ac.xml", "/app/byd_ac.xml")
            ac_vals = parse_ac_page(ac_xml)
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
