# Contributing to BYD Bridge

Thank you for your interest in improving the BYD Bridge! Since this project relies on **ADB screen scraping**, it is highly dependent on the specific Android device resolution, font size, and app version being used.

## 🐛 Reporting Bugs

If the bridge is not detecting values correctly (or clicking the wrong coordinates), please provide the following in your Issue report:

1.  **Device Info:** What phone model and Android version are you using?
2.  **App Version:** Which version of the BYD App is installed?
3.  **UI Dump:**
    The script generates XML dumps of the screen structure. To help us debug, please run the following command while the container is running and attach the file to your issue:
    ```bash
    # Option A: Dump the current screen manually
    docker exec -it byd-bridge adb shell uiautomator dump /sdcard/debug_dump.xml
    docker exec -it byd-bridge adb pull /sdcard/debug_dump.xml /tmp/debug_dump.xml
    docker cp byd-bridge:/tmp/debug_dump.xml ./debug_dump.xml

    # Option B: Retrieve the last dump the script attempted to parse
    # (Useful if the bridge is crashing or failing to read a specific page)
    docker cp byd-bridge:/tmp/byd_last_dump.xml ./byd_last_dump.xml
    ```
    *Note: Please redact any personal info (VIN, License Plate) from the XML file before uploading.*

## 💡 Feature Requests

If you want to add support for a new page or sensor:
1.  Verify that the data is visible on the phone screen.
2.  Provide a screenshot (if possible) and an XML dump of that specific page.

## 🛠️ Pull Requests

1.  Ensure your code adheres to the existing style (Python 3.11+).
2.  If you change the polling logic, please ensure the **Adaptive Polling** (Active/Charging/Idle) behavior remains intact.
3.  Test your changes against the `byd_bridge.py` script.

## 💻 Local Development

1.  Clone the repository.
2.  Create a `docker-compose.yml` (see README).
3.  Run `docker-compose up --build` to test your changes.
