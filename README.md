# BYD App → MQTT Bridge (Docker + ADB over TCP)

Scrape live data from the **BYD Auto** Android app using **ADB** + **uiautomator**, then publish values to **MQTT** for consumption by **Home Assistant** (or any MQTT client).

This container:
- Connects to your Android phone via **ADB over TCP**
- Drives/taps the BYD app UI (screen scraping)
- Dumps the current UI to XML and parses known fields
- Publishes readings to MQTT topics like `byd/app/battery_soc` and a JSON location to `byd/app/location`

---

## ✨ Features
- Containerized: no host Python/ADB setup needed
- Works over **Wi-Fi ADB** (no USB passthrough)
- Per-sensor MQTT topics + GPS JSON
- GitHub Actions workflow included to build/publish to **GHCR**
- Portainer-friendly via external `.env`

---

## 🧠 How It Works (High Level)
1. Container starts **ADB**, optionally pairs (Android 11+), then `adb connect PHONE_IP:ADB_PORT`.
2. Python script performs taps/swipes via `adb shell input ...` to reveal data in the BYD app.
3. UI is dumped with `uiautomator dump` and pulled into the container.
4. Known Android `resource-id`s are parsed and published to MQTT as individual topics plus a device-tracker style JSON.

> ⚠️ **Screen scraping is fragile**: app updates or different screen sizes/DPI may require adjusting the tap/swipe coordinates or the `XML_MAP`.

---

## 📁 Repository Layout
.
├─ Dockerfile
├─ requirements.txt
├─ entrypoint.sh
├─ byd_mqtt_bridge.py
└─ .github/
└─ workflows/
└─ build.yml

yaml
Copy code

- **Dockerfile**: Python 3.12-slim + Android platform-tools (ADB)
- **entrypoint.sh**: starts ADB, (optional) pairs, connects, launches Python
- **byd_mqtt_bridge.py**: scraper + MQTT publisher (reads config from env)
- **build.yml**: CI to build & push `ghcr.io/<USER>/byd-bridge`

---

## ✅ Prerequisites
- Android phone with **BYD Auto** app, logged in and able to show vehicle data
- **Developer options** enabled on the phone
- One-time **ADB over Wi-Fi** setup (below)
- MQTT broker reachable from the container (e.g., Mosquitto)
- (Optional) Home Assistant to consume MQTT topics

---

## 📶 One-Time: Enable ADB over Wi-Fi

Choose **one** of the following:

### Option A — Classic TCP/IP (simplest)
```bash
# On a machine with adb installed; connect phone via USB first
adb devices                   # accept RSA prompt on the phone
adb tcpip 5555
adb connect PHONE_IP:5555
adb devices                   # should show PHONE_IP:5555 device
Unplug USB. The container will connect over TCP on each start.

Option B — Wireless Debugging Pairing (Android 11+)
On the phone: Developer options → Wireless debugging → Pair device with pairing code.
You’ll use the shown IP:port and pairing code as env vars ADB_PAIR_IP_PORT and ADB_PAIR_CODE.

🚀 Deploy with Docker Compose (local or Portainer)
Create an env file (don’t commit secrets):

byd-bridge.env

env
Copy code
# --- Phone ADB over TCP ---
PHONE_IP=192.168.1.123
ADB_PORT=5555

# Optional: Wireless Debugging pairing (Android 11+)
# ADB_PAIR_IP_PORT=192.168.1.123:37099
# ADB_PAIR_CODE=123456

# --- MQTT ---
MQTT_BROKER=192.168.1.40
MQTT_PORT=1883
MQTT_USER=user
MQTT_PASS=password
MQTT_TOPIC_BASE=byd/app
docker-compose.yml

yaml
Copy code
version: "3.8"
services:
  byd-bridge:
    image: ghcr.io/lifeandfate1/byd-bridge:latest
    container_name: byd-bridge
    env_file:
      - byd-bridge.env
    restart: unless-stopped
    # If container → phone routing is blocked by your network, try:
    # network_mode: host
Start / view logs:

bash
Copy code
docker compose up -d
docker logs -f byd-bridge
Expected logs:

adb start-server

adb connect <PHONE_IP>:5555

ADB device list

Periodic “Publish … -> …” lines for MQTT topics

🧪 Home Assistant Examples
Individual MQTT Sensors
sensor:
  - platform: mqtt
    name: "BYD Battery SoC"
    state_topic: "byd/app/battery_soc"
    value_template: "{{ value | replace('%','') | int }}"
    unit_of_measurement: "%"
    device_class: battery
    unique_id: byd_battery_soc

  - platform: mqtt
    name: "BYD Range"
    state_topic: "byd/app/range_km"
    value_template: "{{ value | regex_replace('km','') | trim | int }}"
    unit_of_measurement: "km"
    unique_id: byd_range_km

  - platform: mqtt
    name: "BYD Odometer"
    state_topic: "byd/app/odometer"
    value_template: >
      {{ value | regex_replace('km','') | replace(',','') | trim | int }}
    unit_of_measurement: "km"
    icon: mdi:counter
    unique_id: byd_odometer

Device Tracker from JSON Location
mqtt:
  device_tracker:
    - state_topic: "byd/app/location"
      json_attributes_topic: "byd/app/location"
      source_type: gps
      unique_id: byd_location_tracker


The container publishes:

{"latitude": -37.8136, "longitude": 144.9631, "gps_accuracy": 10, "status": "driving"}

🔧 Configuration & Tuning

Polling interval: edit time.sleep(60) near the end of main_loop() in byd_mqtt_bridge.py.

UI mapping: update XML_MAP if the BYD app’s resource-ids change.

Tap/swipe coordinates: adjust ACTIONS to match your phone’s screen size/DPI.

🐞 Troubleshooting

unauthorized in logs: unlock the phone and accept the ADB trust prompt; restart the container.

adb connect fails: confirm PHONE_IP, same LAN, ADB enabled; make sure you ran adb tcpip 5555 once (classic TCP). Try network_mode: host if needed.

No values published: BYD app must be foreground and screen awake.
Manual test inside the container:

docker exec -it byd-bridge sh
adb shell uiautomator dump /sdcard/x.xml
adb shell ls -l /sdcard/x.xml
adb pull /sdcard/x.xml .


Open x.xml and verify expected resource-ids exist.

Only some fields appear: add another swipe_up or tweak tap coordinates.

HA sensors stay unknown: verify MQTT broker/creds and topic names in your HA config.
