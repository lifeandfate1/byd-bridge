#!/usr/bin/env python3
import os
import json
import subprocess
import time
import xml.etree.ElementTree as ET
import paho.mqtt.client as mqtt

# ==============================
# MQTT CONFIG (env-overridable)
# ==============================
MQTT_BROKER = os.getenv("MQTT_BROKER", "192.168.1.40")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "user")
MQTT_PASS = os.getenv("MQTT_PASS", "password")
MQTT_TOPIC_BASE = os.getenv("MQTT_TOPIC_BASE", "byd/app")

# Home Assistant MQTT Discovery (env-overridable)
DISCOVERY_PREFIX = os.getenv("DISCOVERY_PREFIX", "homeassistant")
DEVICE_ID = os.getenv("DEVICE_ID", "byd_app_bridge")
DEVICE_NAME = os.getenv("DEVICE_NAME", "BYD")

# Availability topic (LWT)
AVAIL_TOPIC = f"{MQTT_TOPIC_BASE}/availability"

# Poll interval seconds
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))

# ==============================
# XML → Topic mapping
# (update if BYD app changes)
# ==============================
XML_MAP = {
    "com.byd.bydautolink:id/h_km_tv": "range_km",
    "com.byd.bydautolink:id/tv_batter_percentage": "battery_soc",
    "com.byd.bydautolink:id/tv_charging_tip": "charging_tip",
    "com.byd.bydautolink:id/tv_banner": "warning",
    "com.byd.bydautolink:id/car_inner_temperature": "cabin_temp",
    "com.byd.bydautolink:id/tem_tv": "ac_set_temp",
    "com.byd.bydautolink:id/h_car_name_tv": "car_status",
    "com.byd.bydautolink:id/tv_update_time": "last_update",
    "com.byd.bydautolink:id/tire_tv_01": "tire_pressure_fl",
    "com.byd.bydautolink:id/tire_tv_02": "tire_pressure_rl",
    "com.byd.bydautolink:id/tire_tv_03": "tire_pressure_fr",
    "com.byd.bydautolink:id/tire_tv_04": "tire_pressure_rr",
    "com.byd.bydautolink:id/iv_value_front_hood": "front_hood",
    "com.byd.bydautolink:id/iv_value_door": "door",
    "com.byd.bydautolink:id/iv_value_window": "window",
    "com.byd.bydautolink:id/iv_value_sunroof": "sunroof",
    "com.byd.bydautolink:id/iv_value_trunk": "trunk",
    "com.byd.bydautolink:id/tv_value_mileage": "odometer",
    "com.byd.bydautolink:id/tv_recent_energy_value": "recent_energy_value",
    "com.byd.bydautolink:id/tv_total_energy_value": "total_energy_value",
    "com.byd.bydautolink:id/tv_tv_car_state_value": "car_state_value",
    "com.byd.bydautolink:id/tv_car_travel_state_title": "car_travel_state_title",
}

# Optional metadata to make HA entities pretty
SENSOR_META = {
    "battery_soc":       {"name": "Battery SoC", "unit": "%",  "device_class": "battery",     "icon": "mdi:battery"},
    "range_km":          {"name": "Range",       "unit": "km", "device_class": "distance",    "icon": "mdi:map-marker-distance"},
    "odometer":          {"name": "Odometer",    "unit": "km", "device_class": "distance",    "icon": "mdi:counter"},
    "cabin_temp":        {"name": "Cabin Temp",  "unit": "°C", "device_class": "temperature", "icon": "mdi:thermometer"},
    "ac_set_temp":       {"name": "AC Setpoint", "unit": "°C", "device_class": "temperature", "icon": "mdi:thermostat"},
    "tire_pressure_fl":  {"name": "Tire FL",     "icon": "mdi:car-tire-alert"},
    "tire_pressure_fr":  {"name": "Tire FR",     "icon": "mdi:car-tire-alert"},
    "tire_pressure_rl":  {"name": "Tire RL",     "icon": "mdi:car-tire-alert"},
    "tire_pressure_rr":  {"name": "Tire RR",     "icon": "mdi:car-tire-alert"},
    "warning":           {"name": "Warning",     "icon": "mdi:alert"},
    "charging_tip":      {"name": "Charging Tip","icon": "mdi:ev-station"},
    "car_status":        {"name": "Car Status",  "icon": "mdi:car-info"},
    "last_update":       {"name": "Last Update", "icon": "mdi:clock-outline"},
    "recent_energy_value":{"name":"Recent Energy","icon":"mdi:lightning-bolt"},
    "total_energy_value": {"name":"Total Energy","icon":"mdi:lightning-bolt-outline"},
    "front_hood":        {"name": "Front Hood",  "icon":"mdi:car"},
    "door":              {"name": "Door",        "icon":"mdi:door"},
    "window":            {"name": "Window",      "icon":"mdi:window-closed-variant"},
    "sunroof":           {"name": "Sunroof",     "icon":"mdi:car"},
    "trunk":             {"name": "Trunk",       "icon":"mdi:car"},
    "car_state_value":   {"name": "Car State",   "icon":"mdi:car"},
    "car_travel_state_title":{"name":"Travel State","icon":"mdi:car"},
}

# ==============================
# ADB helpers (container runs adb)
# ==============================
def adb_shell(cmd: str, wait: float = 1.0):
    """Run an adb shell command against the connected phone."""
    subprocess.run(["adb", "shell"] + cmd.split(), check=False)
    if wait:
        time.sleep(wait)

# Tap/Swipe actions (coordinates may need tuning for your device)
ACTIONS = {
    "refresh": ("input tap 546 370", 2),
    "car_status": ("input tap 284 1131", 1),
    "car_position": ("input tap 854 1822", 1),
    "navigation": ("input tap 726 1983", 1),
    "map_back": ("input tap 92 242", 1),
    "finish": ("input tap 84 170", 1),
    "swipe_up": ("input swipe 500 1500 500 500 500", 2),
    "swipe_down": ("input swipe 500 500 500 1500 500", 2),
    "stop_google_maps": ("am force-stop com.google.android.apps.maps", 2),
}

def perform(action: str):
    if action not in ACTIONS:
        print(f"❌ Unknown action: {action}")
        return
    cmd, wait = ACTIONS[action]
    adb_shell(cmd, wait)

def get_ui_dump(filename="window_dump.xml"):
    """Dump the current UI hierarchy to /sdcard and pull it into the container."""
    remote_file = f"/sdcard/{filename}"
    adb_shell(f"uiautomator dump {remote_file}", 1)
    subprocess.run(
        ["adb", "pull", remote_file, filename],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return filename

# ==============================
# XML parsing
# ==============================
def extract_values(xml_file: str):
    tree = ET.parse(xml_file)
    root = tree.getroot()
    values = {}

    for node in root.iter("node"):
        rid = node.attrib.get("resource-id", "")
        text = node.attrib.get("text", "")
        bounds = node.attrib.get("bounds", "")
        cls = node.attrib.get("class", "")

        if not text:
            continue

        if rid in XML_MAP:
            values[XML_MAP[rid]] = text
        elif cls == "android.widget.TextView" and "[83,153]" in bounds:
            # Car position (lat,lon) anchored by screen coords; may need adjustment per device
            values["car_position"] = text

    return values

# ==============================
# MQTT client + Discovery
# ==============================
client: mqtt.Client

def connect_mqtt():
    global client
    client = mqtt.Client()
    if MQTT_USER or MQTT_PASS:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    # Last Will & Testament: mark offline if we die unexpectedly
    client.will_set(AVAIL_TOPIC, payload="offline", retain=True)
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()
    # Mark online immediately after connect
    client.publish(AVAIL_TOPIC, "online", retain=True)
    return client

def publish_discovery_configs():
    """
    Publish Home Assistant MQTT Discovery configs so all entities appear
    under a single 'BYD' device.
    """
    device_block = {
        "identifiers": [DEVICE_ID],
        "manufacturer": "BYD",
        "model": "BYD App Scraper",
        "name": DEVICE_NAME,
        "sw_version": "1.0",
    }

    # Sensors derived from XML_MAP
    unique_keys = sorted(set(XML_MAP.values()))
    for key in unique_keys:
        meta = SENSOR_META.get(key, {"name": key.replace("_", " ").title()})
        obj_id = f"{DEVICE_ID}_{key}"
        cfg_topic = f"{DISCOVERY_PREFIX}/sensor/{DEVICE_ID}/{key}/config"
        cfg = {
            "name": f"{DEVICE_NAME} {meta.get('name', key)}",
            "uniq_id": obj_id,
            "state_topic": f"{MQTT_TOPIC_BASE}/{key}",
            "availability_topic": AVAIL_TOPIC,
            "device": device_block,
        }
        if meta.get("unit"):
            cfg["unit_of_measurement"] = meta["unit"]
        if meta.get("device_class"):
            cfg["device_class"] = meta["device_class"]
        if meta.get("icon"):
            cfg["icon"] = meta["icon"]

        # Retain discovery so HA remembers across restarts
        client.publish(cfg_topic, json.dumps(cfg), retain=True)

    # Device Tracker for GPS (uses the JSON we publish to /location)
    # Discovery for device_tracker supports JSON attributes; many setups also
    # use the same topic as state. HA will treat it as gps-based tracker.
    dt_cfg_topic = f"{DISCOVERY_PREFIX}/device_tracker/{DEVICE_ID}/location/config"
    dt_cfg = {
        "name": f"{DEVICE_NAME} Location",
        "uniq_id": f"{DEVICE_ID}_location",
        "state_topic": f"{MQTT_TOPIC_BASE}/location",
        "json_attributes_topic": f"{MQTT_TOPIC_BASE}/location",
        "availability_topic": AVAIL_TOPIC,
        "source_type": "gps",
        "icon": "mdi:car",
        "device": device_block,
    }
    client.publish(dt_cfg_topic, json.dumps(dt_cfg), retain=True)

def publish_car_position(raw_coords: str):
    try:
        lat_str, lon_str = map(str.strip, raw_coords.split(","))
        payload = {
            "latitude": float(lat_str),
            "longitude": float(lon_str),
            "gps_accuracy": 10,
            "status": "driving",
        }
        client.publish(f"{MQTT_TOPIC_BASE}/location", json.dumps(payload))
        print(f"📍 MQTT publish location -> {payload}")
    except Exception as e:
        print(f"Coordinate parse error: {e}")

def publish_values(values: dict):
    if not values:
        print("⚠️ No values found.")
        return
    for key, val in values.items():
        topic = f"{MQTT_TOPIC_BASE}/{key}"
        client.publish(topic, val)
        print(f"➡️ Publish {topic} -> {val}")

# ==============================
# Main loop
# ==============================
def main_loop():
    # One-time discovery advertisement
    publish_discovery_configs()

    while True:
        all_values = {}

        # Sequence of screens/dumps (adjust actions for your device)
        dumps_sequence = [
            ("window_dump_1.xml", []),
            ("window_dump_2.xml", ["swipe_up", "car_status"]),
            ("window_dump_3.xml", ["swipe_up"]),
            ("window_dump_4.xml", ["finish", "swipe_down", "car_position"]),
            ("window_dump_5.xml", ["navigation"]),
        ]

        for filename, actions in dumps_sequence:
            for action in actions:
                perform(action)
            get_ui_dump(filename)
            all_values.update(extract_values(filename))

        # Publish position JSON
        if "car_position" in all_values:
            publish_car_position(all_values["car_position"])

        # Publish each sensor as its own topic
        publish_values(all_values)

        # Clean up any launched maps UI and go back
        perform("stop_google_maps")
        perform("map_back")

        time.sleep(POLL_INTERVAL)

# ==============================
# Entrypoint
# ==============================
if __name__ == "__main__":
    client = connect_mqtt()
    try:
        main_loop()
    except KeyboardInterrupt:
        print("🚪 Exiting...")
    finally:
        # Mark offline so HA shows the device unavailable if we stop
        try:
            client.publish(AVAIL_TOPIC, "offline", retain=True)
            time.sleep(0.2)
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass
