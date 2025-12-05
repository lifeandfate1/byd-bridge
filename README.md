App# 🚗 BYD App Bridge (Wireless ADB + MQTT)

![Banner](images/banner.png)

A self-healing bridge that connects the official BYD Android App to Home Assistant.

This project runs a Python script in a Docker container that communicates with a dedicated Android phone over **Wireless ADB**. It "scrapes" the UI of the BYD app to read sensors (Range, SoC, Tire Pressure, Location) and interacts with onscreen buttons to send commands (Lock, Unlock, AC).

## 🏗️ Architecture

The Python script does not handle the connection; it assumes the environment has a valid ADB session. The Docker container handles the Wireless ADB connection at startup.

## ✨ Key Features

* **⚡ Adaptive Polling Engine:**
    * **Driving:** Polls every **60s** (configurable).
    * **Charging:** Detects charging state (via UI text) and speeds up to **30s**.
    * **Idle:** Slows down to **10 mins** if parked for >15 mins to save battery.
    * **Instant Wake:** Command queue interrupts polling immediately to execute actions (e.g., Unlock).

* **🛡️ Robustness:**
    * **Self-Healing:** If the UI dump fails 3 times (ADB instability), it performs a `force-stop` and restarts the BYD app automatically.
    * **Ghosting Protection:** Filters out placeholder values (`--`, `---`) so Home Assistant history remains clean.
    * **Race Condition Locking:** A global sequence lock prevents commands from firing while the screen is being swiped/read.

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
| `MQTT_BROKER` | `192.168.1.40` | IP of MQTT Broker. |
| `MQTT_USER` / `_PASS` | *None* | MQTT Credentials. |

#### Polling Logic
| Variable | Default | Description |
| :--- | :--- | :--- |
| `POLL_SECONDS` | `60` | Loop interval when active. |
| `POLL_CHARGING_SECONDS` | `30` | Loop interval when "Charging" text is detected. |
| `POLL_IDLE_SECONDS` | `600` | Loop interval when inactive for >15 mins. |

#### Features (1=On, 0=Off)
| Variable | Default | Description |
| :--- | :--- | :--- |
| `POLL_VEHICLE_STATUS` | `1` | Scrapes Tire Pressure/Doors page. |
| `POLL_AC` | `1` | Scrapes AC page. |
| `POLL_VEHICLE_POSITION`| `1` | Scrapes GPS page. |
| `BYD_PIN` | *None* | **Required for Actions.** Your car's PIN code. |

## 🛠️ Troubleshooting

**Q: The container loops with "Host is down" or "Connection refused".**
* **Cause:** The phone is not in TCP/IP mode.
* **Fix:** You must plug the phone into USB and run `adb tcpip 5555` again (Phase 1). This resets if the phone reboots.

**Q: "Unauthorized" in logs.**
* **Cause:** The phone received the connection request but you didn't click "Allow" on the screen.
* **Fix:** Unlock the phone screen, restart the container, and watch for the "Allow USB Debugging?" popup. Check "Always allow".

**Q: Sensors are unavailable.**
* Check the logs (`docker logs byd-bridge`). If the script cannot dump the XML, it won't publish data.