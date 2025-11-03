import os, json, subprocess, time, xml.etree.ElementTree as ET
import paho.mqtt.client as mqtt

# ===== MQTT from env (with sensible defaults) =====
MQTT_BROKER = os.getenv("MQTT_BROKER", "192.168.1.40")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "user")
MQTT_PASS = os.getenv("MQTT_PASS", "password")
MQTT_TOPIC_BASE = os.getenv("MQTT_TOPIC_BASE", "byd/app")

# ===== XML Mapping (same as your script) =====
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

# ===== ADB-like local shell (container has adb client; we still use shell cmds on phone) =====
def sh(cmd: str, wait: float = 1.0):
    subprocess.run(["adb", "shell"] + cmd.split(), check=False)
    if wait:
        time.sleep(wait)

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
    sh(cmd, wait)

def get_ui_dump(filename="window_dump.xml"):
    remote_file = f"/sdcard/{filename}"
    sh(f"uiautomator dump {remote_file}", 1)
    # Pull into container to parse
    subprocess.run(["adb", "pull", remote_file, filename],
                   check=False,
                   stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    return filename

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
            values["car_position"] = text
    return values

# ===== MQTT =====
def connect_mqtt():
    client = mqtt.Client()
    if MQTT_USER or MQTT_PASS:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_disconnect = lambda c, u, rc: print("⚠️ MQTT disconnected; will keep trying...")
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_start()
    return client

client = connect_mqtt()

def publish_car_position(raw_coords):
    try:
        lat, lon = map(str.strip, raw_coords.split(","))
        payload = {
            "latitude": float(lat),
            "longitude": float(lon),
            "gps_accuracy": 10,
            "status": "driving"
        }
        client.publish(f"{MQTT_TOPIC_BASE}/location", json.dumps(payload))
        print(f"MQTT publish -> {payload}")
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

def main_loop():
    while True:
        all_values = {}
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

        if "car_position" in all_values:
            publish_car_position(all_values["car_position"])
        publish_values(all_values)

        perform("stop_google_maps")
        perform("map_back")
        time.sleep(60)

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        print("🚪 Exiting.")
        client.loop_stop()
        client.disconnect()
