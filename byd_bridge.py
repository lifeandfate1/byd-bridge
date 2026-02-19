#!/usr/bin/env python3
"""
BYD Bridge — ADB-driven scraper + MQTT publisher for Home Assistant.
(Refactored Architecture + Adaptive Polling + Deep Sleep + Self-Healing + Smart Recovery)
"""

import os
import sys
import json
import time
import queue
import threading
import signal
import logging
import http.server
import socketserver
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Callable, Tuple
from datetime import datetime, timezone
from subprocess import Popen, PIPE, TimeoutExpired
import re
import xml.etree.ElementTree as ET
try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Fallback for older Python (though container usually has 3.9+)
    from datetime import timezone as _basetz
    ZoneInfo = lambda x: _basetz.utc

# ---- Logging Configuration ----
class JsonFormatter(logging.Formatter):
    def format(self, record):
        tz_name = os.getenv("TZ", "UTC")
        try:
            tz = ZoneInfo(tz_name)
        except:
            tz = timezone.utc
            
        log_record = {
            "ts": datetime.now(tz).isoformat(),
            "level": record.levelname,
            "msg": record.getMessage(),
            "logger": record.name
        }
        if hasattr(record, "extra_info"):
            log_record.update(record.extra_info)
        if record.exc_info:
            log_record["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(log_record, ensure_ascii=False)

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
logger = logging.getLogger("byd_bridge")
logger.setLevel(logging.INFO)
logger.addHandler(handler)

def log_extra(logger, level, msg, **kwargs):
    if logger.isEnabledFor(level):
        logger._log(level, msg, args=(), extra={"extra_info": kwargs})

# ---- Global Locks ----
ADB_LOCK = threading.RLock()
SEQUENCE_LOCK = threading.RLock()

# ---- Constants & Selectors ----

@dataclass
class Selectors:
    # Home (read)
    home_range: str = "com.byd.bydautolink:id/h_km_tv"
    home_soc: str = "com.byd.bydautolink:id/tv_batter_percentage"
    home_cabin_temp: str = "com.byd.bydautolink:id/car_inner_temperature"
    home_setpoint: str = "com.byd.bydautolink:id/tem_tv"
    home_car_status: str = "com.byd.bydautolink:id/h_car_name_tv"
    home_last_update: str = "com.byd.bydautolink:id/tv_update_time"
    home_refresh_btn: str = "com.byd.bydautolink:id/btn_touch_refresh"

    # Home (tap targets)
    home_ac_row: str = "com.byd.bydautolink:id/c_air_item_rl_2"
    quick_control_row: str = "com.byd.bydautolink:id/btn_ble_control_layout" # Lock/Unlock tiles

    # A/C page (read)
    ac_setpoint: str = "com.byd.bydautolink:id/tem_tv"
    ac_prev: str = "com.byd.bydautolink:id/tem_tv_last"
    ac_next: str = "com.byd.bydautolink:id/tem_tv_next"
    ac_power_btn: str = "com.byd.bydautolink:id/c_air_item_power_btn"

    # A/C page (tap)
    ac_heat_btn: str = "com.byd.bydautolink:id/c_air_item_heat_btn"
    ac_cool_btn: str = "com.byd.bydautolink:id/c_air_item_cool_btn"
    
    # Home buttons
    temp_up: str = "com.byd.bydautolink:id/btn_temperature_plus"
    temp_down: str = "com.byd.bydautolink:id/btn_temperature_reduce"

    # Nav Text (Used for Smart Recovery)
    nav_vehicle_status_text: str = "Vehicle status"
    nav_vehicle_status_text: str = "Vehicle status"
    nav_ac_text: str = "A/C"

    # My Account Recovery
    # "Account and Security" is a unique text on the My Account page
    my_account_unique_text: str = "Account and Security" 
    nav_my_car_text: str = "My car"

SEL = Selectors()

PIN_TAPS = {
    "1": (170,  680), "2": (360,  680), "3": (550,  680),
    "4": (170,  870), "5": (360,  870), "6": (550,  870),
    "7": (170, 1060), "8": (360, 1060), "9": (550, 1060),
    "0": (360, 1255),
}

# ---- XML helpers ----
def _parse_xml(path: str) -> Optional[ET.Element]:
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return None
        return ET.parse(path).getroot()
    except ET.ParseError as e:
        log_extra(logger, logging.WARNING, "XML Parse Error", path=path, error=str(e))
        return None
    except Exception as e:
        log_extra(logger, logging.ERROR, "Unexpected XML Error", path=path, error=str(e))
        return None

def _all_nodes(root: Optional[ET.Element]):
    if root is None: return
    for el in root.iter():
        yield el

def _rid(node: ET.Element) -> str:
    return node.attrib.get("resource-id", "")

def _txt(node: ET.Element) -> str:
    return node.attrib.get("text", "").strip()

def _bounds(node: ET.Element):
    m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.attrib.get("bounds", ""))
    if not m: return None
    x1, y1, x2, y2 = map(int, m.groups())
    return ((x1 + x2) // 2, (y1 + y2) // 2)

def _find_by_rid(root: ET.Element, rid: str):
    for n in _all_nodes(root):
        if _rid(n) == rid: return n
    return None

def _find_all_text_contains(root: ET.Element, sub: str):
    sub_low = sub.lower()
    return [n for n in _all_nodes(root) if _txt(n).lower().find(sub_low) != -1]

def _find_text_equals(root: ET.Element, s: str):
    s_low = s.lower()
    for n in _all_nodes(root):
        if _txt(n).lower() == s_low: return n
    return None

def _adb_tap_center_of(adb: "ADB", node: ET.Element) -> bool:
    c = _bounds(node)
    if not c: return False
    x, y = c
    adb.shell(f"input tap {x} {y}", timeout=2.0)
    return True

# External dep
try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None

# ---------- Env & secrets ----------

def read_env_or_file(name: str, default: Optional[str]=None) -> Optional[str]:
    val = os.getenv(name)
    if val is not None and val != "": return val
    f = os.getenv(name + "_FILE")
    if f:
        try:
            with open(f, "r", encoding="utf-8") as fh: return fh.read().strip()
        except Exception as e:
            log_extra(logger, logging.ERROR, "failed to read secret file", var=name, path=f, error=str(e))
    return default

def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "": return default
    return v.strip().lower() not in ("0","false","no")

def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v == "": return default
    try: return int(v)
    except ValueError: return default

def env_list(name: str, default: str) -> List[str]:
    v = os.getenv(name, default)
    return [s.strip().lower() for s in v.split(",") if s.strip()]

# ---------- Configuration ----------

@dataclass
class Config:
    mqtt_broker: str
    mqtt_port: int
    mqtt_user: Optional[str]
    mqtt_pass: Optional[str]
    topic_base: str
    device_id: str
    device_name: str
    
    # Adaptive Polling Intervals
    poll_shutdown: int
    poll_charging: int
    poll_running: int
    
    phone_ip: str
    adb_port: int
    enable_vehicle_status: bool
    enable_ac_page: bool
    byd_pin: Optional[str]
    http_status_port: int
    discovery_prefix: str

def load_config() -> Config:
    mqtt_broker = read_env_or_file("MQTT_BROKER", None)
    phone_ip    = read_env_or_file("PHONE_IP", None)
    
    if not mqtt_broker:
        logger.critical("MQTT_BROKER environment variable is required")
        sys.exit(1)
    if not phone_ip:
        logger.critical("PHONE_IP environment variable is required")
        sys.exit(1)

    return Config(
        mqtt_broker=mqtt_broker,
        mqtt_port=env_int("MQTT_PORT", 1883),
        mqtt_user=read_env_or_file("MQTT_USER", None),
        mqtt_pass=read_env_or_file("MQTT_PASS", None),
        topic_base=os.getenv("MQTT_TOPIC_BASE", "byd/app").rstrip("/"),
        device_id=os.getenv("BYD_DEVICE_ID", "byd-vehicle"),
        device_name=os.getenv("BYD_DEVICE_NAME", "BYD Vehicle"),
        
        # New Adaptive Polling Config
        poll_shutdown=env_int("POLL_SHUTDOWN_SECONDS", 300), # Default 5 mins
        poll_charging=env_int("POLL_CHARGING_SECONDS", 60),  # Default 1 min
        poll_running=env_int("POLL_RUNNING_SECONDS", 120),   # Default 2 mins
        
        phone_ip=phone_ip,
        adb_port=env_int("ADB_PORT", 5555),
        enable_vehicle_status=env_bool("POLL_VEHICLE_STATUS", True),
        enable_ac_page=env_bool("POLL_AC", True),
        byd_pin=read_env_or_file("BYD_PIN", None),
        http_status_port=env_int("HTTP_STATUS_PORT", 8080),
        discovery_prefix=os.getenv("DISCOVERY_PREFIX", "homeassistant"),
    )

# ---------- Signal Handling ----------
class ServiceExit(Exception):
    pass

class SignalHandler:
    def __init__(self):
        self.shutdown_requested = False
        signal.signal(signal.SIGINT, self.handle_signal)
        signal.signal(signal.SIGTERM, self.handle_signal)

    def handle_signal(self, signum, frame):
        log_extra(logger, logging.INFO, "Shutdown signal received", signal=signum)
        self.shutdown_requested = True

# ---------- Watchdog ----------
class Watchdog:
    def __init__(self, timeout=600):
        self.last_tick = time.time()
        self.timeout = timeout
        self.lock = threading.Lock()

    def tick(self):
        with self.lock:
            self.last_tick = time.time()

    def is_healthy(self):
        with self.lock:
            return (time.time() - self.last_tick) < self.timeout

# ---------- HTTP Status Server ----------

class _StatusHandler(http.server.BaseHTTPRequestHandler):
    METRICS = {
        "poll_cycles_total": 0, "poll_failures_total": 0,
        "adb_calls_total": 0, "dumps_failed_total": 0, "mqtt_publishes_total": 0,
    }
    LAST_HEALTH = {"last_ok": "", "mqtt": "unknown", "last_state": "unknown"}
    watchdog = None

    def log_message(self, format, *args): return
    def do_GET(self):
        if self.path == "/healthz":
            healthy = True
            if self.watchdog and not self.watchdog.is_healthy():
                healthy = False
            
            status = 200 if healthy else 503
            self.LAST_HEALTH["watchdog"] = "ok" if healthy else "stalled"
            
            body = json.dumps(self.LAST_HEALTH).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type","application/json")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/metrics":
            lines = [f"# TYPE {k} counter\n{k} {v}" for k, v in self.METRICS.items()]
            body = ("\n".join(lines)+"\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type","text/plain")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

def maybe_start_status_server(port: int, watchdog: Watchdog):
    if not port: return None
    _StatusHandler.watchdog = watchdog
    def _serve():
        try:
            with socketserver.TCPServer(("0.0.0.0", port), _StatusHandler) as httpd:
                log_extra(logger, logging.INFO, "status server started", port=port)
                httpd.serve_forever()
        except Exception as e:
            log_extra(logger, logging.ERROR, "Status server failed", error=str(e))

    t = threading.Thread(target=_serve, name="status-http", daemon=True)
    t.start()
    return t

# ---------- ADB wrapper ----------

class ADBError(Exception): pass

class ADB:
    def __init__(self, host: str, port: int, base_timeout: float = 8.0, max_retries: int = 3):
        self.host = host; self.port = port
        self.base_timeout = base_timeout; self.max_retries = max_retries
        self.lock = ADB_LOCK

    def _run(self, args: List[str], timeout: Optional[float]) -> Tuple[int, bytes, bytes]:
        cmd = ["adb", "-s", f"{self.host}:{self.port}"] + args
        _StatusHandler.METRICS["adb_calls_total"] += 1
        with Popen(cmd, stdout=PIPE, stderr=PIPE) as p:
            try:
                out, err = p.communicate(timeout=timeout or self.base_timeout)
            except TimeoutExpired:
                p.kill()
                raise ADBError(f"timeout executing: {' '.join(args)}")
        return p.returncode, out, err

    def exec_out(self, args: List[str], timeout: Optional[float]=None) -> bytes:
        with self.lock:
            delay = 0.25; last_err = None
            for attempt in range(1, self.max_retries+1):
                try:
                    rc, out, err = self._run(["exec-out"] + args, timeout)
                    if rc == 0: return out
                    last_err = err.decode("utf-8","ignore")
                except ADBError as e: last_err = str(e)
                time.sleep(delay); delay = min(delay*2, 2.0)
            raise ADBError(last_err or "unknown adb exec-out error")

    def shell(self, cmd: str, timeout: Optional[float]=None) -> str:
        with self.lock:
            delay = 0.25; last_err = None
            for attempt in range(1, self.max_retries+1):
                try:
                    rc, out, err = self._run(["shell", cmd], timeout)
                    if rc == 0: return out.decode("utf-8","ignore")
                    last_err = err.decode("utf-8","ignore")
                except ADBError as e: last_err = str(e)
                time.sleep(delay); delay = min(delay*2, 2.0)
            raise ADBError(last_err or "unknown adb error")

# ---------- MQTT helper ----------

class MQTT:
    def __init__(self, cfg: Config):
        if mqtt is None:
            logger.critical("paho-mqtt library is not installed")
            sys.exit(1)
        self.cfg = cfg
        self.client = mqtt.Client(client_id=f"byd-bridge-{cfg.device_id}", clean_session=True)
        if cfg.mqtt_user: self.client.username_pw_set(cfg.mqtt_user, cfg.mqtt_pass or "")
        self.client.will_set(f"{self.cfg.topic_base}/availability", payload="offline", retain=True, qos=1)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self._connected = threading.Event()
        self._handlers: Dict[str, Callable[[str, str], None]] = {}
        self._state_cache: Dict[str, str] = {}

    def connect(self):
        try:
            self.client.connect(self.cfg.mqtt_broker, self.cfg.mqtt_port, keepalive=60)
            self.client.loop_start()
            if not self._connected.wait(timeout=10):
                raise RuntimeError("MQTT connect timeout")
            self.publish(f"{self.cfg.topic_base}/availability", "online", retain=True, qos=1)
        except Exception as e:
            log_extra(logger, logging.ERROR, "MQTT Connection Error", error=str(e))
            raise

    def disconnect(self):
        self.publish(f"{self.cfg.topic_base}/availability", "offline", retain=True, qos=1)
        self.client.loop_stop()
        self.client.disconnect()
        log_extra(logger, logging.INFO, "MQTT Disconnected gracefully")

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected.set(); _StatusHandler.LAST_HEALTH["mqtt"] = "connected"
            log_extra(logger, logging.INFO, "mqtt connected", broker=self.cfg.mqtt_broker)
        else:
            log_extra(logger, logging.ERROR, "mqtt connection failed", rc=rc)

    def _on_disconnect(self, client, userdata, rc):
        self._connected.clear(); _StatusHandler.LAST_HEALTH["mqtt"] = "disconnected"
        log_extra(logger, logging.WARNING, "mqtt disconnected", rc=rc)

    def publish(self, topic: str, payload: str, retain: bool=False, qos: int=0):
        if payload is None: payload = ""
        if retain and self._state_cache.get(topic) == payload: return
        res = self.client.publish(topic, payload, qos=qos, retain=retain)
        if res.rc != 0:
            log_extra(logger, logging.WARNING, "mqtt publish failed", topic=topic, rc=res.rc)
        else:
            if retain: self._state_cache[topic] = payload
            _StatusHandler.METRICS["mqtt_publishes_total"] += 1

    def subscribe_handler(self, topic_filter: str, handler: Callable[[str, str], None]):
        self._handlers[topic_filter] = handler
        self.client.subscribe(topic_filter, qos=0)

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8","ignore")
        for filt, handler in self._handlers.items():
            if (filt.endswith("#") and msg.topic.startswith(filt[:-1])) or msg.topic == filt:
                handler(msg.topic, payload); return

# ---------- Discovery ----------

def device_info(cfg: Config) -> Dict[str, Any]:
    return {"identifiers":[cfg.device_id], "manufacturer":"BYD", "name":cfg.device_name, "model":"BYD App Bridge", "sw_version":"2.2.0-enhanced"}

def disc_topic(cfg: Config, domain: str, object_id: str) -> str:
    return f"{cfg.discovery_prefix}/{domain}/{object_id}/config"

def publish_discovery(cfg: Config, mq: MQTT, caps: Dict[str, bool]):
    dev = device_info(cfg)
    AV = {"avty_t": f"{cfg.topic_base}/availability", "pl_avail": "online", "pl_not_avail": "offline"}

    # Core Sensors
    sensors = [
        ("byd_battery_soc", "Battery State of Charge", f"{cfg.topic_base}/battery_soc", "%", "battery", "mdi:battery"),
        ("byd_range_km", "Range", f"{cfg.topic_base}/range_km", "km", "distance", "mdi:map-marker-distance"),
        ("byd_cabin_temp_c", "Cabin Temperature", f"{cfg.topic_base}/cabin_temp_c", "°C", "temperature", None),
        ("byd_climate_setpoint_c", "Climate Setpoint", f"{cfg.topic_base}/climate_target_temp_c", "°C", None, "mdi:thermostat"),
        ("byd_status", "Vehicle Status", f"{cfg.topic_base}/car_status", None, None, "mdi:car-info"),
        ("byd_last_update", "BYD App Last Update", f"{cfg.topic_base}/last_update", None, None, "mdi:update"),
    ]
    for oid, name, stat_t, unit, dclass, icon in sensors:
        p = {"uniq_id": f"{oid}_{cfg.device_id}", "name": name, "stat_t": stat_t, "dev": dev}
        if unit: p["unit_of_measurement"] = unit
        if dclass: p["device_class"] = dclass
        if icon: p["icon"] = icon
        p.update(AV)
        mq.publish(disc_topic(cfg, "sensor", oid), json.dumps(p), retain=True, qos=1)

    # Vehicle Status (conditional)
    if caps.get("status"):
        for oid, name, unit, icon, dclass in [
            ("byd_odometer_km","Odometer","km","mdi:counter","distance"),
            ("byd_driving_status","Driving Status",None,"mdi:car",None),
            ("byd_whole_vehicle","Whole Vehicle Status",None,"mdi:car-connected",None),
            ("byd_doors","Doors",None,"mdi:car-door",None),
            ("byd_windows","Windows",None,"mdi:window-closed-variant",None),
            ("byd_bonnet","Bonnet",None,"mdi:car",None),
            ("byd_boot","Boot",None,"mdi:car-back",None),
            ("byd_tyre_fl_psi","Tyre Front Left","psi","mdi:tire","pressure"),
            ("byd_tyre_fr_psi","Tyre Front Right","psi","mdi:tire","pressure"),
            ("byd_tyre_rl_psi","Tyre Rear Left","psi","mdi:tire","pressure"),
            ("byd_tyre_rr_psi","Tyre Rear Right","psi","mdi:tire","pressure"),
            ("byd_energy_recent_kwh_per_100km","Energy Recent","kWh/100km","mdi:lightning-bolt",None),
            ("byd_energy_total_kwh_per_100km","Energy Total","kWh/100km","mdi:lightning-bolt-outline",None),
        ]:
            st = f"{cfg.topic_base}/{oid.replace('byd_','')}"
            payload = {"uniq_id": f"{oid}_{cfg.device_id}", "name": name, "stat_t": st, "dev": dev}
            if unit: payload["unit_of_measurement"] = unit
            if icon: payload["icon"] = icon
            if dclass: payload["device_class"] = dclass
            payload.update(AV)
            mq.publish(disc_topic(cfg, "sensor", oid), json.dumps(payload), retain=True, qos=1)



    # A/C Page (conditional)
    if caps.get("ac"):
        for oid, name, unit in [
            ("byd_climate_target_temp_c","Climate Target (°C)","°C"),
            ("byd_climate_prev_temp_c","Climate Prev (°C)","°C"),
            ("byd_climate_next_temp_c","Climate Next (°C)","°C"),
        ]:
            st = f"{cfg.topic_base}/{oid.replace('byd_','')}"
            payload = {"uniq_id": f"{oid}_{cfg.device_id}", "name": name, "stat_t": st, "dev": dev, "unit_of_measurement": unit, "icon":"mdi:thermometer"}
            payload.update(AV)
            mq.publish(disc_topic(cfg, "sensor", oid), json.dumps(payload), retain=True, qos=1)
        
        payload = {"uniq_id":f"byd_climate_power_{cfg.device_id}","name":"Climate Power","stat_t":f"{cfg.topic_base}/climate_power","dev":dev,"icon":"mdi:power"}
        payload.update(AV)
        mq.publish(disc_topic(cfg, "sensor","byd_climate_power"), json.dumps(payload), retain=True, qos=1)

        for oid, name, cmd in [
            ("byd_ac_up_btn","A/C Temp Up","ac_up"),
            ("byd_ac_down_btn","A/C Temp Down","ac_down"),
            ("byd_climate_rapid_heat","Climate Rapid Heat","climate_rapid_heat"),
            ("byd_climate_rapid_vent","Climate Rapid Vent","climate_rapid_vent"),
            ("byd_climate_switch_on","Climate Power Toggle","climate_switch_on"),
        ]:
            payload = {"uniq_id": f"{oid}_{cfg.device_id}", "name": name, "dev": dev, "cmd_t": f"{cfg.topic_base}/cmd/{cmd}"}
            payload.update(AV)
            mq.publish(disc_topic(cfg, "button", oid), json.dumps(payload), retain=True, qos=1)

    # Buttons
    cmds = [
        ("byd_cmd_unlock", "Unlock", "unlock", "mdi:lock-open-variant"),
        ("byd_cmd_lock", "Lock", "lock", "mdi:lock"),
    ]
    for oid, name, cmd_slug, icon in cmds:
        p = {"uniq_id": f"{oid}_{cfg.device_id}", "name": name, "dev": dev, "cmd_t": f"{cfg.topic_base}/cmd/{cmd_slug}", "icon": icon}
        p.update(AV)
        mq.publish(disc_topic(cfg, "button", oid), json.dumps(p), retain=True, qos=1)

# ---------- Scraper & Actions ----------

XML_TMP = os.getenv("XML_TMP", "/tmp/byd_last_dump.xml")

def _looks_like_xml(buf: bytes) -> bool:
    if not buf: return False
    b = buf.lstrip()
    return b.startswith(b"<?xml")

def dump_xml(adb: ADB) -> str:
    # 1. Attempt Dump
    try:
        dump_out = adb.shell("uiautomator dump", timeout=12.0)
        m = re.search(r"(/sdcard/\S+\.xml)", dump_out or "")
        dump_path = m.group(1) if m else "/sdcard/window_dump.xml"
        
        # 2. Retrieve Content
        out = adb.exec_out(["cat", dump_path], timeout=8.0)
        if not _looks_like_xml(out):
            out = adb.shell(f"cat {dump_path}", timeout=8.0).encode("utf-8","ignore")
        
        # 3. Write & Validate
        with open(XML_TMP, "wb") as fh: fh.write(out)
        return XML_TMP

    except Exception as e:
        _StatusHandler.METRICS["dumps_failed_total"] += 1
        log_extra(logger, logging.WARNING, "UI Dump failed", error=str(e))
        return XML_TMP

# --- Page Parsers ---
def parse_home_xml(path: str) -> Dict[str, Any]:
    root = _parse_xml(path)
    if root is None: return {}
    
    out: Dict[str, Any] = {}
    if (n := _find_by_rid(root, SEL.home_soc)) is not None:
        m = re.search(r"(\d+)", _txt(n))
        if m: out["battery_soc"] = int(m.group(1))
    if (n := _find_by_rid(root, SEL.home_range)) is not None:
        m = re.search(r"(\d+)", _txt(n))
        if m: out["range_km"] = int(m.group(1))
    if (n := _find_by_rid(root, SEL.home_cabin_temp)) is not None:
        m = re.search(r"(-?\d+(?:\.\d+)?)", _txt(n))
        if m: out["cabin_temp_c"] = float(m.group(1))
    if (n := _find_by_rid(root, SEL.home_setpoint)) is not None:
        m = re.search(r"(-?\d+(?:\.\d+)?)", _txt(n))
        if m: out["climate_target_temp_c"] = float(m.group(1))
    if (n := _find_by_rid(root, SEL.home_car_status)) is not None:
        out["car_status"] = _txt(n)
    if (n := _find_by_rid(root, SEL.home_last_update)) is not None:
        out["last_update"] = _txt(n)
    return out

def parse_status_xmls(top_path: str, bottom_path: str) -> Dict[str, Any]:
    root = _parse_xml(top_path)
    if root is None: return {}
    
    text_nodes = [(_txt(n), n) for n in _all_nodes(root) if _txt(n)]
    out: Dict[str, Any] = {}

    tyre_vals = []
    for t, n in text_nodes:
        m = re.search(r"(\d+\.\d)\s*psi", t, re.I)
        if m: tyre_vals.append(float(m.group(1)))
    if len(tyre_vals) >= 4:
        out["tyre_fl_psi"], out["tyre_fr_psi"], out["tyre_rl_psi"], out["tyre_rr_psi"] = tyre_vals[:4]

    for t, _ in text_nodes:
        m = re.search(r"(\d{1,7})\s*km\b", t, re.I)
        if m: out["odometer_km"] = int(m.group(1)); break

    for t, _ in text_nodes:
        m = re.search(r"(\d+(?:\.\d+)?)\s*kW.?h/100km", t)
        if m and "energy_recent_kwh_per_100km" not in out:
            out["energy_recent_kwh_per_100km"] = float(m.group(1))
        elif m:
            out["energy_total_kwh_per_100km"] = float(m.group(1))

    def _label_value(label: str, key: str):
        n = _find_text_equals(root, label)
        if n is None: return
        after = []
        found = False
        for el in _all_nodes(root):
            if el is n: found = True; continue
            if found and _txt(el):
                after.append(_txt(el))
                if len(after) >= 1: break
        if after: out[key] = after[0]

    _label_value("Front bonnet", "bonnet")
    _label_value("Doors", "doors")
    _label_value("Windows", "windows")
    _label_value("Boot", "boot")
    _label_value("Whole Vehicle Status", "whole_vehicle")
    _label_value("Driving status", "driving_status")
    return out

def parse_ac_xml(path: str) -> Dict[str, Any]:
    root = _parse_xml(path)
    if root is None: return {}
    
    out: Dict[str, Any] = {}
    if (n := _find_by_rid(root, SEL.ac_setpoint)) is not None:
        m = re.search(r"(-?\d+(?:\.\d+)?)", _txt(n))
        if m: out["climate_target_temp_c"] = float(m.group(1))
    if (n := _find_by_rid(root, SEL.ac_prev)) is not None:
        m = re.search(r"(-?\d+(?:\.\d+)?)", _txt(n))
        if m: out["climate_prev_temp_c"] = float(m.group(1))
    if (n := _find_by_rid(root, SEL.ac_next)) is not None:
        m = re.search(r"(-?\d+(?:\.\d+)?)", _txt(n))
        if m: out["climate_next_temp_c"] = float(m.group(1))
    btn = _find_by_rid(root, SEL.ac_power_btn)
    if btn is not None: 
        texts = [ _txt(n) for n in btn.iter() if _txt(n) ]
        if any("Switch on" in t for t in texts): out["climate_power"] = "off"
        elif any("Switch off" in t for t in texts): out["climate_power"] = "on"
    return out



# --- Nav Helpers ---
def _go_home(adb: ADB):
    adb.shell("input keyevent 4", timeout=1)
    time.sleep(0.5)
    adb.shell("input keyevent 4", timeout=1)
    time.sleep(0.5)

def _scroll_down_once(adb: ADB):
    adb.shell("input swipe 333 948 333 316 450", timeout=2.0)

def _ensure_pin(adb: ADB, pin: str):
    if not pin: return
    adb.shell("input tap 360 1245", timeout=1.0)
    for digit in pin:
        x, y = PIN_TAPS.get(digit, (0,0))
        if x: adb.shell(f"input tap {x} {y}", timeout=0.5)
    time.sleep(1.0)

def _open_vehicle_status(adb: "ADB") -> bool:
    p = dump_xml(adb)
    root = _parse_xml(p)
    if root is None: return False
    
    target = _find_text_equals(root, SEL.nav_vehicle_status_text)
    if target is None:
        cands = _find_all_text_contains(root, SEL.nav_vehicle_status_text)
        if cands: target = cands[0]
    if (target is not None) and _adb_tap_center_of(adb, target):
        time.sleep(3.5) 
        return True
    return False

def _open_ac_page(adb: "ADB") -> bool:
    p = dump_xml(adb)
    root = _parse_xml(p)
    if root is None: return False
    
    target = _find_by_rid(root, SEL.home_ac_row)
    if target is None: target = _find_text_equals(root, SEL.nav_ac_text)
    if (target is not None) and _adb_tap_center_of(adb, target):
        time.sleep(3.5) 
        return True
    return False



# --- Smart State Recovery (New Feature) ---

def _is_screen_on(adb: "ADB") -> bool:
    """Checks if the screen is currently on (Awake)."""
    # 'mWakefulness=Awake' is the standard indicator in dumpsys power
    out = adb.shell("dumpsys power")
    return "mWakefulness=Awake" in out

def _wake_and_unlock(adb: "ADB"):
    """Wakes the device and swipes up to dismiss keyguard."""
    log_extra(logger, logging.INFO, "Screen is OFF. Waking device...")
    adb.shell("input keyevent 224") # KEYCODE_WAKEUP
    time.sleep(1.0)
    # Swipe up to dismiss any non-PIN lockscreen
    adb.shell("input swipe 500 2000 500 500 300")
    time.sleep(1.5)

def parse_xml_safe(path: str) -> Optional[ET.Element]:
    try: return _parse_xml(path)
    except: return None

def ensure_home_state(cfg: Config, adb: ADB) -> bool:
    """
    Intelligently navigates back to the Home screen.
    Returns True if Home is reached, False if failed.
    """
    for attempt in range(3): # Try 3 times to fix the state
        # 0. Pre-Check: Is Screen On?
        if not _is_screen_on(adb):
            _wake_and_unlock(adb)
            # Loop back to take a fresh dump
            continue

        # 1. Take a snapshot
        xml_path = dump_xml(adb)
        root = parse_xml_safe(xml_path) 
        if root is None: 
            continue # Bad dump, try again

        # 2. Check: Are we already Home? (The Ideal State)
        # We look for the "Car Status" text ID which is unique to Home
        if _find_by_rid(root, SEL.home_car_status) is not None:
            if attempt > 0: log_extra(logger, logging.INFO, "Recovered to Home screen")
            return True

        # 3. Check: Are we in a known Sub-Page? (The "Stuck" State)
        # Check for Vehicle Status Title
        if _find_text_equals(root, SEL.nav_vehicle_status_text) or \
           _find_all_text_contains(root, SEL.nav_vehicle_status_text):
            log_extra(logger, logging.WARNING, "Stuck on Status page. Pressing BACK.")
            adb.shell("input keyevent 4") # Back
            time.sleep(2.0)
            continue
            
            continue

        # Check for A/C Page specific ID
        if _find_by_rid(root, SEL.ac_heat_btn):
            log_extra(logger, logging.WARNING, "Stuck on A/C page. Pressing BACK.")
            adb.shell("input keyevent 4") # Back
            time.sleep(2.0)
            continue

        # Check for My Account Page ("Account and Security" text)
        if _find_text_equals(root, SEL.my_account_unique_text):
            log_extra(logger, logging.WARNING, "Stuck on 'My Account' screen. Tapping 'My Car' tab.")
            
            # Find the "My car" tab text and tap it
            my_car_btn = _find_text_equals(root, SEL.nav_my_car_text)
            if my_car_btn is not None:
                _adb_tap_center_of(adb, my_car_btn)
            else:
                log_extra(logger, logging.ERROR, "Recovery failed: 'My car' button not found on screen")
                
            time.sleep(3.0)
            continue

        # 4. Check: Are we completely lost? (The "Crashed" State)
        # If we see none of the above, we are likely not in the app.
        log_extra(logger, logging.WARNING, "App not detected. Launching BYD App...")
        # Launch command
        adb.shell("monkey -p com.byd.bydautolink -c android.intent.category.LAUNCHER 1")
        time.sleep(6.0) # Launching takes longer
    
    # If we exit the loop, we failed to find Home after 3 corrective actions
    log_extra(logger, logging.ERROR, "Could not recover to Home screen after 3 attempts")
    return False

# --- Actions (Fixed) ---
def execute_command(cfg: Config, adb: ADB, action: str):
    # RESTORED: Sequence lock is acquired in CommandWorker before calling this
    log_extra(logger, logging.INFO, "executing command", action=action)
    _go_home(adb)
    
    if action == "unlock":
        p = dump_xml(adb) 
        root = _parse_xml(p)
        if root is None: return
        tiles = []
        for n in _all_nodes(root):
            if _rid(n) == SEL.quick_control_row: tiles.append(_bounds(n))
        tiles = sorted([t for t in tiles if t], key=lambda x: x[0]) 
        if tiles:
            x,y = tiles[0]
            adb.shell(f"input tap {x} {y}")
            _ensure_pin(adb, cfg.byd_pin)

    elif action == "lock":
        p = dump_xml(adb)
        root = _parse_xml(p)
        if root is None: return
        tiles = []
        for n in _all_nodes(root):
            if _rid(n) == SEL.quick_control_row: tiles.append(_bounds(n))
        tiles = sorted([t for t in tiles if t], key=lambda x: x[0])
        if len(tiles) > 1:
            x,y = tiles[1]
            adb.shell(f"input tap {x} {y}")
            _ensure_pin(adb, cfg.byd_pin)

    elif action in ("ac_up", "ac_down"):
        rid = SEL.temp_up if action == "ac_up" else SEL.temp_down
        p = dump_xml(adb)
        root = _parse_xml(p)
        if root is None: return
        if (n := _find_by_rid(root, rid)) is not None: _adb_tap_center_of(adb, n)

    elif action in ("climate_switch_on", "climate_rapid_heat", "climate_rapid_vent"):
        if _open_ac_page(adb):
            p2 = dump_xml(adb) 
            root2 = _parse_xml(p2)
            if root2 is None: return
            
            if action == "climate_switch_on": tgt = _find_by_rid(root2, SEL.ac_power_btn)
            elif action == "climate_rapid_heat": tgt = _find_by_rid(root2, SEL.ac_heat_btn)
            else: tgt = _find_by_rid(root2, SEL.ac_cool_btn)
            
            if tgt is not None:
                _adb_tap_center_of(adb, tgt)
                _ensure_pin(adb, cfg.byd_pin)
            adb.shell("input keyevent 4")

# ---------- Tasks ----------

def task_home(cfg: Config, adb: ADB, mq: MQTT) -> Optional[str]:
    """Returns the 'car_status' string found (or empty string if none)"""
    with SEQUENCE_LOCK:
        log_extra(logger, logging.INFO, "Refreshing Home Page...")
        
        # 1. Find and Tap Refresh Button
        # We take a preliminary snapshot just to find the button
        p_refresh = dump_xml(adb)
        root_refresh = parse_xml_safe(p_refresh)
        
        if root_refresh is not None:
            btn = _find_by_rid(root_refresh, SEL.home_refresh_btn)
            if btn is not None:
                log_extra(logger, logging.INFO, "Found Refresh Button - Tapping")
                _adb_tap_center_of(adb, btn)
                time.sleep(5.0) # Wait for network refresh
            else:
                log_extra(logger, logging.WARNING, "Refresh button not found (possibly not on top/home?)")

        # 2. Capture Final Data
        path = dump_xml(adb)
        vals = parse_home_xml(path)
        if not vals:
            log_extra(logger, logging.WARNING, "home XML yielded no values; skipping publish")
            return None
        for key, value in vals.items():
            mq.publish(f"{cfg.topic_base}/{key}", json.dumps(value) if isinstance(value, (dict,list)) else str(value), retain=True, qos=1)
        
        return vals.get("car_status", "")

def task_vehicle_status(cfg: Config, adb: ADB, mq: MQTT):
    with SEQUENCE_LOCK:
        if not _open_vehicle_status(adb):
            log_extra(logger, logging.WARNING, "could not open Vehicle status page; skipping")
            return

        # Top half
        top = dump_xml(adb)
        # Scroll
        _scroll_down_once(adb)
        time.sleep(1.0) 
        # Bottom half
        bottom = dump_xml(adb)
        
        vals_top = parse_status_xmls(top, top)
        vals_bottom = parse_status_xmls(bottom, bottom)

        vals = {**vals_top, **vals_bottom}
        if not vals:
            log_extra(logger, logging.WARNING, "status XML yielded no values; skipping")
        else:
            for key, value in vals.items():
                mq.publish(f"{cfg.topic_base}/{key}", str(value), retain=True, qos=1)

        adb.shell("input keyevent 4", timeout=2.0)



def task_ac_page(cfg: Config, adb: ADB, mq: MQTT):
    with SEQUENCE_LOCK:
        if not _open_ac_page(adb):
            log_extra(logger, logging.WARNING, "could not open A/C page; skipping")
            return
            
        path = dump_xml(adb)
        vals = parse_ac_xml(path)
        for key, value in vals.items():
            mq.publish(f"{cfg.topic_base}/{key}", str(value), retain=True, qos=1)
        
        adb.shell("input keyevent 4", timeout=2.0)

# ---------- Main Loop ----------

def main():
    cfg = load_config()
    log_extra(logger, logging.INFO, "starting bridge", device=cfg.device_id)
    
    # Init Signal Handler
    sig_handler = SignalHandler()
    watchdog = Watchdog()

    t_http = maybe_start_status_server(cfg.http_status_port, watchdog)
    
    adb = ADB(cfg.phone_ip, cfg.adb_port)
    # Check ADB connection
    try:
        adb.shell("id")
    except ADBError:
        log_extra(logger, logging.INFO, "Connecting to ADB...", ip=cfg.phone_ip)
        try:
            Popen(["adb", "connect", f"{cfg.phone_ip}:{cfg.adb_port}"], stdout=PIPE, stderr=PIPE).communicate(timeout=5)
        except Exception as e:
            log_extra(logger, logging.WARNING, "Initial ADB connect failed (ignoring)", error=str(e))

    mq = MQTT(cfg)
    try:
        mq.connect()
    except Exception:
        sys.exit(1)

    # Publish discovery
    publish_discovery(cfg, mq, {
        "status": cfg.enable_vehicle_status,
        "status": cfg.enable_vehicle_status,
        "ac": cfg.enable_ac_page
    })

    # Command Queue
    q_cmds = queue.Queue()
    mq.subscribe_handler(f"{cfg.topic_base}/cmd/+", lambda t, p: q_cmds.put((t, p)))

    def cmd_worker():
        while not sig_handler.shutdown_requested:
            try:
                topic, payload = q_cmds.get(timeout=1.0)
                cmd = topic.split("/")[-1]
                with SEQUENCE_LOCK:
                    execute_command(cfg, adb, cmd)
                q_cmds.task_done()
            except queue.Empty:
                pass
            except Exception as e:
                log_extra(logger, logging.ERROR, "Command execution error", error=str(e))
    
    t_cmd = threading.Thread(target=cmd_worker, name="cmd-worker", daemon=True)
    t_cmd.start()

    # Definitions for Polling States
    STRINGS_SHUTDOWN = ["Switched off", "unavailable", "Shutdown"]
    STRINGS_CHARGING = ["EV Charging", "Plugged in", "Charging"]
    
    last_state = "unknown"
    cycles = 0

    log_extra(logger, logging.INFO, "Main loop started")
    
    while not sig_handler.shutdown_requested:
        # 1. Update Watchdog
        watchdog.tick()
        
        loop_start = time.time()
        cycles += 1
        _StatusHandler.METRICS["poll_cycles_total"] = cycles

        # 2. Ensure we are Home
        #    If we can't get to Home, we can't trust the scrape, so skip this cycle.
        if not ensure_home_state(cfg, adb):
            log_extra(logger, logging.WARNING, "Skipping cycle: Not at Home screen")
            time.sleep(10)
            continue
            
        # 3. Read Home Data
        try:
            state_str = task_home(cfg, adb, mq) # Returns "Driving", "Switched off", etc.
        except Exception as e:
            log_extra(logger, logging.ERROR, "Task Home failed", error=str(e))
            state_str = None

        if state_str:
            last_state = state_str
            _StatusHandler.LAST_HEALTH["last_state"] = state_str
            mq.publish(f"{cfg.topic_base}/state", state_str, retain=True, qos=1)

        # 4. Decide "Sleep" Time based on State
        #    Default is RUNNING speed
        sleep_time = cfg.poll_running
        
        if last_state in STRINGS_SHUTDOWN:
            sleep_time = cfg.poll_shutdown
        elif any(s in last_state for s in STRINGS_CHARGING):
            sleep_time = cfg.poll_charging
        
        # 5. Run Secondary Tasks (only if active/charging or forcibly enabled)
        #    If "Shutdown", we usually skip deep scraping to save 12V battery,
        #    UNLESS user wants aggressive polling.
        should_deep_poll = True
        if last_state in STRINGS_SHUTDOWN and sleep_time >= 300:
             # If we are in long-sleep shutdown mode, skip deep polling
             should_deep_poll = False
        
        if should_deep_poll:
            if cfg.enable_vehicle_status:
                try: task_vehicle_status(cfg, adb, mq)
                except Exception as e: log_extra(logger, logging.ERROR, "Task Status failed", error=str(e))



            if cfg.enable_ac_page:
                try: task_ac_page(cfg, adb, mq)
                except Exception as e: log_extra(logger, logging.ERROR, "Task AC failed", error=str(e))

                except Exception as e: log_extra(logger, logging.ERROR, "Task AC failed", error=str(e))

        try:
             tz = ZoneInfo(os.getenv("TZ", "UTC"))
        except:
             tz = timezone.utc
        _StatusHandler.LAST_HEALTH["last_ok"] = datetime.now(tz).isoformat()
        
        # 6. Sleep Calculation
        elapsed = time.time() - loop_start
        to_sleep = max(0.0, sleep_time - elapsed)
        log_extra(logger, logging.INFO, "Cycle done", state=last_state, elapsed=f"{elapsed:.1f}s", next_poll=f"{to_sleep:.1f}s")
        
        # Sleep in chunks to allow fast shutdown
        remaining = to_sleep
        while remaining > 0 and not sig_handler.shutdown_requested:
            step = min(1.0, remaining)
            time.sleep(step)
            remaining -= step

    # Shutdown sequence
    mq.disconnect()
    log_extra(logger, logging.INFO, "Exiting...")

if __name__ == "__main__":
    main()
