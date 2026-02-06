App# 🚗 BYD App Bridge (Wireless ADB + MQTT)

![Banner](images/banner.png)

A self-healing bridge that connects the official BYD Android App to Home Assistant.

This project runs a Python script in a Docker container that communicates with a dedicated Android phone over **Wireless ADB**. It "scrapes" the UI of the BYD app to read sensors (Range, SoC, Tire Pressure, Location) and interacts with onscreen buttons to send commands (Lock, Unlock, AC).

## 🏗️ Architecture

The Python script does not handle the connection; it assumes the environment has a valid ADB session. The Docker container handles the Wireless ADB connection at startup.

## ✨ Key Features

* **⚡ Adaptive Polling Engine:**
    * **Driving:** Polls every **60s** (configurable).
    * **Instant Wake:** Command queue interrupts polling immediately to execute actions (e.g., Unlock).

* **🛡️ Robustness:**
    * **Ghosting Protection:** Filters out placeholder values (`--`, `---`) so Home Assistant history remains clean.
    * **Race Condition Locking:** A global sequence lock prevents commands from firing while the screen is being swiped/read.
    * **Smart Recovery:** Detects if the app is crashed or stuck on the wrong screen and automatically relaunches/navigates back to home.

![Architecture Diagram](images/diagram.png)

## 📋 Prerequisites

1.  **Dedicated Android Phone:**
    * **Developer Options** enabled.
    * **USB Debugging** enabled.
    * **Screen Timeout** set to "Never" (or "Stay Awake" in Dev Options).
    * BYD App installed and logged in (tick "Remember Me").
2.  **Network:**
    * The phone must be on the same network as the Docker host.
    * You need the phone's **Static IP Address**.

## ⚙️ Setup Instructions

### Phase 1: Enable Wireless ADB (The "Tethered" Step)
*You only need to do this once per phone reboot.*

1.  Connect the phone to your computer (or Docker host) via **USB cable**.
2.  Open a terminal/command prompt.
3.  Run the command to switch ADB to TCP/IP mode:
    ```bash
    adb tcpip 5555
    ```
4.  **Unplug the USB cable.** The phone is now listening for wireless connections.

### Phase 2: Deploy Container
The container is configured to automatically connect to the phone IP on startup.

1.  Place `byd_bridge.py` and `docker-compose.yml` in a folder.
2.  Edit `docker-compose.yml` (see Configuration below) and set your `PHONE_IP`.
3.  Start the bridge:
    ```bash
    docker-compose up -d
    ```

### Phase 3: Home Assistant
Go to **Settings > Devices & Services > MQTT**. A new device **BYD App Bridge** will appear automatically.

## 🔧 Configuration

### Docker Environment Variables

#### Connection
| Variable | Default | Description |
| :--- | :--- | :--- |
| `PHONE_IP` | *None* | **Required.** The LAN IP of your Android phone (e.g., `192.168.1.55`). |
| `MQTT_BROKER` | *None* | **Required.** IP of Home Assistant/Mosquitto. |
| `MQTT_PORT` | `1883` | Port for MQTT. |
| `MQTT_USER` | *None* | Username for MQTT. |
| `MQTT_PASS` | *None* | Password for MQTT. |
| `ADB_PORT` | `5555` | Port for ADB. |

#### Polling Logic & Battery Saving
| Variable | Default | Description |
| :--- | :--- | :--- |
| `POLL_SHUTDOWN_SECONDS` | `300` | Interval when car is "Switched off" (Deep Sleep). |
| `POLL_CHARGING_SECONDS` | `60` | Interval when car is charging. |
| `POLL_RUNNING_SECONDS` | `120` | Interval when car is driving/running. |

#### Features (1=On, 0=Off)
| Variable | Default | Description |
| :--- | :--- | :--- |
| `POLL_VEHICLE_POSITION`| `True` | **Critical:** Scrapes GPS page. **Set to `0` or `false` to stop battery drain.** |
| `POLL_VEHICLE_STATUS` | `True` | Scrapes Tire Pressure/Doors page. |
| `POLL_AC` | `True` | Scrapes AC page. |
| `BYD_PIN` | *None* | **Required for Actions.** Your car's PIN code. |

#### Advanced Configuration
| Variable | Default | Description |
| :--- | :--- | :--- |
| `MQTT_TOPIC_BASE` | `byd/app` | Topic prefix. |
| `BYD_DEVICE_ID` | `byd-vehicle` | Unique ID for HA discovery. |
| `BYD_DEVICE_NAME` | `BYD Vehicle` | Friendly name in HA. |
| `HTTP_STATUS_PORT` | `8080` | Port for health check/metrics. |
| `DISCOVERY_PREFIX` | `homeassistant` | MQTT discovery prefix. |

## 🛠️ Troubleshooting

**Q: The container loops with "Host is down" or "Connection refused".**
* **Cause:** The phone is not in TCP/IP mode.
* **Fix:** You must plug the phone into USB and run `adb tcpip 5555` again (Phase 1). This resets if the phone reboots.

**Q: "Unauthorized" in logs.**
* **Cause:** The phone received the connection request but you didn't click "Allow" on the screen.
* **Fix:** Unlock the phone screen, restart the container, and watch for the "Allow USB Debugging?" popup. Check "Always allow".

**Q: Sensors are unavailable.**
* Check the logs (`docker logs byd-bridge`). If the script cannot dump the XML, it won't publish data.
