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
import http.server
import socketserver
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Callable, Tuple
from datetime import datetime, timezone
from subprocess import Popen, PIPE, TimeoutExpired
import re
import xml.etree.ElementTree as ET

# ---- Global Locks ----
ADB_LOCK = threading.RLock()
SEQUENCE_LOCK = threading.RLock()

# ---- STATE DEFINITIONS (CONSTANTS) ----
# Code will check these lists to determine polling speed.
STRINGS_SHUTDOWN = ["Switched off", "unavailable"]
STRINGS_CHARGING = ["EV Charging", "Plugged in", "Charging"]
# "Running" is the catch-all state, but these are here for reference:
STRINGS_RUNNING  = ["Commenced", "Driving", "Started"]

# ---- BYD selectors ----
SEL = {
    # Home (read)
    "home_range": "com.byd.bydautolink:id/h_km_tv",
    "home_soc": "com.byd.bydautolink:id/tv_batter_percentage",
    "home_cabin_temp": "com.byd.bydautolink:id/car_inner_temperature",
    "home_setpoint": "com.byd.bydautolink:id/tem_tv",
    "home_car_status": "com.byd.bydautolink:id/h_car_name_tv",
    "home_last_update": "com.byd.bydautolink:id/tv_update_time",

    # Home (tap targets)
    "home_ac_row": "com.byd.bydautolink:id/c_air_item_rl_2",
    "quick_control_row": "com.byd.bydautolink:id/btn_ble_control_layout", # Lock/Unlock tiles

    # A/C page (read)
    "ac_setpoint": "com.byd.bydautolink:id/tem_tv",
    "ac_prev": "com.byd.bydautolink:id/tem_tv_last",
    "ac_next": "com.byd.bydautolink:id/tem_tv_next",
    "ac_power_btn": "com.byd.bydautolink:id/c_air_item_power_btn",

    # A/C page (tap)
    "ac_heat_btn": "com.byd.bydautolink:id/c_air_item_heat_btn",
    "ac_cool_btn": "com.byd.bydautolink:id/c_air_item_cool_btn",
    
    # Home buttons
    "temp_up": "com.byd.bydautolink:id/btn_temperature_plus",
    "temp_down": "com.byd.bydautolink:id/btn_temperature_reduce",

    # Nav Text (Used for Smart Recovery)
    "nav_vehicle_status_text": "Vehicle status",
    "nav_vehicle_position_text": "Vehicle position",
    "nav_ac_text": "A/C",
}

# ---- PIN Coordinates ----
PIN_TAPS = {
    "1": (170,  680), "2": (360,  680), "3": (550,  680),
    "4": (170,  870), "5": (360,  870), "6": (550,  870),
    "7": (170, 1060), "8": (360, 1060), "9": (550, 1060),
    "0": (360, 1255),
}

# ---- XML helpers ----
def _parse_xml(path: str) -> ET.Element:
    return ET.parse(path).getroot()

def _all_nodes(root: ET.Element):
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

# ---------- Utility logging ----------

def jslog(level: str, msg: str, **fields):
    record = {"ts": datetime.now(timezone.utc).isoformat(), "level": level.upper()[:3], "msg": msg}
    if fields: record.update(fields)
    print(f'{record["level"]} | ' + json.dumps({k:v for k,v in record.items() if k not in ("level",)}, ensure_ascii=False), flush=True)

# ---------- Env & secrets ----------

def read_env_or_file(name: str, default: Optional[str]=None) -> Optional[str]:
    val = os.getenv(name)
    if val is not None and val != "": return val
    f = os.getenv(name + "_FILE")
    if f:
        try:
            with open(f, "r", encoding="utf-8") as fh: return fh.read().strip()
        except Exception as e:
            jslog("ERR", "failed to read secret file", var=name, path=f, error=str(e))
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
    enable_vehicle_position: bool
    enable_ac_page: bool
    byd_pin: Optional[str]
    http_status_port: int
    discovery_prefix: str

def load_config() -> Config:
    mqtt_broker = read_env_or_file("MQTT_BROKER", None)
    phone_ip    = read_env_or_file("PHONE_IP", None)
    if not mqtt_broker: raise SystemExit("MQTT_BROKER is required")
    if not phone_ip: raise SystemExit("PHONE_IP is required")

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
        enable_vehicle_position=env_bool("POLL_VEHICLE_POSITION", True),
        enable_ac_page=env_bool("POLL_AC", True),
        byd_pin=read_env_or_file("BYD_PIN", None),
        http_status_port=env_int("HTTP_STATUS_PORT", 8080),
        discovery_prefix=os.getenv("DISCOVERY_PREFIX", "homeassistant"),
    )

# ---------- HTTP Status Server ----------

class _StatusHandler(http.server.BaseHTTPRequestHandler):
    METRICS = {
        "poll_cycles_total": 0, "poll_failures_total": 0,
        "adb_calls_total": 0, "dumps_failed_total": 0, "mqtt_publishes_total": 0,
    }
    LAST_HEALTH = {"last_ok": "", "mqtt": "unknown", "last_state": "unknown"}
    def log_message(self, format, *args): return
    def do_GET(self):
        if self.path == "/healthz":
            body = json.dumps(self.LAST_HEALTH).encode("utf-8")
            self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers(); self.wfile.write(body)
        elif self.path == "/metrics":
            lines = [f"# TYPE {k} counter\n{k} {v}" for k, v in self.METRICS.items()]
            body = ("\n".join(lines)+"\n").encode("utf-8")
            self.send_response(200); self.send_header("Content-Type","text/plain"); self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()

def maybe_start_status_server(port: int):
    if not port: return None
    def _serve():
        with socketserver.TCPServer(("0.0.0.0", port), _StatusHandler) as httpd:
            jslog("INF", "status server started", port=port)
            httpd.serve_forever()
    t = threading.Thread(target=_serve, name="status-http", daemon=True); t.start(); return t

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
                p.kill(); raise ADBError(f"timeout executing: {' '.join(args)}")
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
        if mqtt is None: raise SystemExit("paho-mqtt is required")
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
        self.client.connect(self.cfg.mqtt_broker, self.cfg.mqtt_port, keepalive=60)
        self.client.loop_start()
        if not self._connected.wait(timeout=10): raise RuntimeError("MQTT connect timeout")
        self.publish(f"{self.cfg.topic_base}/availability", "online", retain=True, qos=1)

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected.set(); _StatusHandler.LAST_HEALTH["mqtt"] = "connected"
            jslog("INF", "mqtt connected", broker=self.cfg.mqtt_broker)
        else: jslog("ERR", "mqtt connection failed", rc=rc)

    def _on_disconnect(self, client, userdata, rc):
        self._connected.clear(); _StatusHandler.LAST_HEALTH["mqtt"] = "disconnected"
        jslog("WRN", "mqtt disconnected", rc=rc)

    def publish(self, topic: str, payload: str, retain: bool=False, qos: int=0):
        if payload is None: payload = ""
        if retain and self._state_cache.get(topic) == payload: return
        res = self.client.publish(topic, payload, qos=qos, retain=retain)
        if res.rc != 0: jslog("WRN", "mqtt publish failed", topic=topic, rc=res.rc)
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
    return {"identifiers":[cfg.device_id], "manufacturer":"BYD", "name":cfg.device_name, "model":"BYD App Bridge", "sw_version":"2.1.0-adaptive"}

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

    # Vehicle Position (conditional)
    if caps.get("position"):
        dt_oid = "byd_vehicle_tracker"
        dt_payload = {
            "uniq_id": f"{dt_oid}_{cfg.device_id}", "name": "BYD Vehicle", "dev": dev,
            "json_attr_t": f"{cfg.topic_base}/location_json",
            "state_topic": f"{cfg.topic_base}/vehicle_tracker_state",
            "icon": "mdi:car-arrow-right",
        }
        dt_payload.update(AV)
        mq.publish(disc_topic(cfg, "device_tracker", dt_oid), json.dumps(dt_payload), retain=True, qos=1)
        for oid, name, key in [("byd_latitude","Latitude","latitude"),("byd_longitude","Longitude","longitude")]:
            payload = {"uniq_id": f"{oid}_{cfg.device_id}", "name": name, "stat_t": f"{cfg.topic_base}/{key}", "dev": dev, "icon":"mdi:crosshairs-gps"}
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
        jslog("WRN", "UI Dump failed", error=str(e))
        return XML_TMP

# --- Page Parsers ---
def parse_home_xml(path: str) -> Dict[str, Any]:
    try:
        root = _parse_xml(path)
    except Exception: return {}
    
    out: Dict[str, Any] = {}
    if (n := _find_by_rid(root, SEL["home_soc"])) is not None:
        m = re.search(r"(\d+)", _txt(n))
        if m: out["battery_soc"] = int(m.group(1))
    if (n := _find_by_rid(root, SEL["home_range"])) is not None:
        m = re.search(r"(\d+)", _txt(n))
        if m: out["range_km"] = int(m.group(1))
    if (n := _find_by_rid(root, SEL["home_cabin_temp"])) is not None:
        m = re.search(r"(-?\d+(?:\.\d+)?)", _txt(n))
        if m: out["cabin_temp_c"] = float(m.group(1))
    if (n := _find_by_rid(root, SEL["home_setpoint"])) is not None:
        m = re.search(r"(-?\d+(?:\.\d+)?)", _txt(n))
        if m: out["climate_target_temp_c"] = float(m.group(1))
    if (n := _find_by_rid(root, SEL["home_car_status"])) is not None:
        out["car_status"] = _txt(n)
    if (n := _find_by_rid(root, SEL["home_last_update"])) is not None:
        out["last_update"] = _txt(n)
    return out

def parse_status_xmls(top_path: str, bottom_path: str) -> Dict[str, Any]:
    try:
        root = _parse_xml(top_path)
    except Exception: return {}
    
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
    try:
        root = _parse_xml(path)
    except Exception: return {}
    
    out: Dict[str, Any] = {}
    if (n := _find_by_rid(root, SEL["ac_setpoint"])) is not None:
        m = re.search(r"(-?\d+(?:\.\d+)?)", _txt(n))
        if m: out["climate_target_temp_c"] = float(m.group(1))
    if (n := _find_by_rid(root, SEL["ac_prev"])) is not None:
        m = re.search(r"(-?\d+(?:\.\d+)?)", _txt(n))
        if m: out["climate_prev_temp_c"] = float(m.group(1))
    if (n := _find_by_rid(root, SEL["ac_next"])) is not None:
        m = re.search(r"(-?\d+(?:\.\d+)?)", _txt(n))
        if m: out["climate_next_temp_c"] = float(m.group(1))
    btn = _find_by_rid(root, SEL["ac_power_btn"])
    if btn is not None: 
        texts = [ _txt(n) for n in btn.iter() if _txt(n) ]
        if any("Switch on" in t for t in texts): out["climate_power"] = "off"
        elif any("Switch off" in t for t in texts): out["climate_power"] = "on"
    return out

def parse_position_xml(path: str) -> Dict[str, Any]:
    try:
        root = _parse_xml(path)
    except Exception: return {}
    
    latlon = None
    for n in _all_nodes(root):
        t = _txt(n)
        m = re.search(r"(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)", t)
        if m: latlon = (float(m.group(1)), float(m.group(2))); break
    if latlon:
        return {"latitude": latlon[0], "longitude": latlon[1], "gps_accuracy": 10}
    return {}

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
    try: root = _parse_xml(p)
    except: return False
    
    target = _find_text_equals(root, SEL["nav_vehicle_status_text"])
    if target is None:
        cands = _find_all_text_contains(root, SEL["nav_vehicle_status_text"])
        if cands: target = cands[0]
    if (target is not None) and _adb_tap_center_of(adb, target):
        time.sleep(3.5) 
        return True
    return False

def _open_ac_page(adb: "ADB") -> bool:
    p = dump_xml(adb)
    try: root = _parse_xml(p)
    except: return False
    
    target = _find_by_rid(root, SEL["home_ac_row"])
    if target is None: target = _find_text_equals(root, SEL["nav_ac_text"])
    if (target is not None) and _adb_tap_center_of(adb, target):
        time.sleep(3.5) 
        return True
    return False

def _open_vehicle_position(adb: "ADB") -> bool:
    p = dump_xml(adb)
    try: root = _parse_xml(p)
    except: return False
    
    target = _find_text_equals(root, SEL["nav_vehicle_position_text"])
    if target is None:
        c = _find_all_text_contains(root, SEL["nav_vehicle_position_text"])
        target = c[0] if c else None
    if (target is not None) and _adb_tap_center_of(adb, target):
        time.sleep(0.8)
        return True
    return False

# --- Smart State Recovery (New Feature) ---
def parse_xml_safe(path: str) -> Optional[ET.Element]:
    try: return _parse_xml(path)
    except: return None

def ensure_home_state(cfg: Config, adb: ADB) -> bool:
    """
    Intelligently navigates back to the Home screen.
    Returns True if Home is reached, False if failed.
    """
    for attempt in range(3): # Try 3 times to fix the state
        # 1. Take a snapshot
        xml_path = dump_xml(adb)
        root = parse_xml_safe(xml_path) 
        if root is None: 
            continue # Bad dump, try again

        # 2. Check: Are we already Home? (The Ideal State)
        # We look for the "Car Status" text ID which is unique to Home
        if _find_by_rid(root, SEL["home_car_status"]) is not None:
            if attempt > 0: jslog("INF", "Recovered to Home screen")
            return True

        # 3. Check: Are we in a known Sub-Page? (The "Stuck" State)
        # Check for Vehicle Status Title
        if _find_text_equals(root, SEL["nav_vehicle_status_text"]) or \
           _find_all_text_contains(root, SEL["nav_vehicle_status_text"]):
            jslog("WRN", "Stuck on Status page. Pressing BACK.")
            adb.shell("input keyevent 4") # Back
            time.sleep(2.0)
            continue
            
        # Check for A/C Page specific ID
        if _find_by_rid(root, SEL["ac_heat_btn"]):
            jslog("WRN", "Stuck on A/C page. Pressing BACK.")
            adb.shell("input keyevent 4") # Back
            time.sleep(2.0)
            continue

        # 4. Check: Are we completely lost? (The "Crashed" State)
        # If we see none of the above, we are likely not in the app.
        jslog("WRN", "App not detected. Launching BYD App...")
        # Launch command
        adb.shell("monkey -p com.byd.bydautolink -c android.intent.category.LAUNCHER 1")
        time.sleep(6.0) # Launching takes longer
    
    # If we exit the loop, we failed to find Home after 3 corrective actions
    jslog("ERR", "Could not recover to Home screen after 3 attempts")
    return False

# --- Actions (Fixed) ---
def execute_command(cfg: Config, adb: ADB, action: str):
    # RESTORED: Sequence lock is acquired in CommandWorker before calling this
    jslog("INF", "executing command", action=action)
    _go_home(adb)
    
    if action == "unlock":
        p = dump_xml(adb); 
        try: root = _parse_xml(p)
        except: return
        tiles = []
        for n in _all_nodes(root):
            if _rid(n) == SEL["quick_control_row"]: tiles.append(_bounds(n))
        tiles = sorted([t for t in tiles if t], key=lambda x: x[0]) 
        if tiles:
            x,y = tiles[0]
            adb.shell(f"input tap {x} {y}")
            _ensure_pin(adb, cfg.byd_pin)

    elif action == "lock":
        p = dump_xml(adb); 
        try: root = _parse_xml(p)
        except: return
        tiles = []
        for n in _all_nodes(root):
            if _rid(n) == SEL["quick_control_row"]: tiles.append(_bounds(n))
        tiles = sorted([t for t in tiles if t], key=lambda x: x[0])
        if len(tiles) > 1:
            x,y = tiles[1]
            adb.shell(f"input tap {x} {y}")
            _ensure_pin(adb, cfg.byd_pin)

    elif action in ("ac_up", "ac_down"):
        rid = SEL["temp_up"] if action == "ac_up" else SEL["temp_down"]
        p = dump_xml(adb); 
        try: root = _parse_xml(p)
        except: return
        if (n := _find_by_rid(root, rid)) is not None: _adb_tap_center_of(adb, n)

    elif action in ("climate_switch_on", "climate_rapid_heat", "climate_rapid_vent"):
        if _open_ac_page(adb):
            p2 = dump_xml(adb); 
            try: root2 = _parse_xml(p2)
            except: return
            
            if action == "climate_switch_on": tgt = _find_by_rid(root2, SEL["ac_power_btn"])
            elif action == "climate_rapid_heat": tgt = _find_by_rid(root2, SEL["ac_heat_btn"])
            else: tgt = _find_by_rid(root2, SEL["ac_cool_btn"])
            
            if tgt is not None:
                _adb_tap_center_of(adb, tgt)
                _ensure_pin(adb, cfg.byd_pin)
            adb.shell("input keyevent 4")

# ---------- Tasks ----------

def task_home(cfg: Config, adb: ADB, mq: MQTT) -> Optional[str]:
    """Returns the 'car_status' string found (or empty string if none)"""
    # RESTORED: Sequence Lock
    with SEQUENCE_LOCK:
        path = dump_xml(adb)
        vals = parse_home_xml(path)
        if not vals:
            jslog("WRN", "home XML yielded no values; skipping publish")
            return None
        for key, value in vals.items():
            mq.publish(f"{cfg.topic_base}/{key}", json.dumps(value) if isinstance(value, (dict,list)) else str(value), retain=True, qos=1)
        
        return vals.get("car_status", "")

def task_vehicle_status(cfg: Config, adb: ADB, mq: MQTT):
    # RESTORED: Sequence Lock
    with SEQUENCE_LOCK:
        if not _open_vehicle_status(adb):
            jslog("WRN", "could not open Vehicle status page; skipping")
            return

        # Top half
        top = dump_xml(adb)
        vals_top = parse_status_xmls(top, top)

        # Scroll
        _scroll_down_once(adb)
        time.sleep(1.0) 

        # Bottom half
        bottom = dump_xml(adb)
        vals_bottom = parse_status_xmls(bottom, bottom)

        vals = {**vals_top, **vals_bottom}
        if not vals:
            jslog("WRN", "status XML yielded no values; skipping")
        else:
            for key, value in vals.items():
                mq.publish(f"{cfg.topic_base}/{key}", str(value), retain=True, qos=1)

        adb.shell("input keyevent 4", timeout=2.0)

def task_vehicle_position(cfg: Config, adb: ADB, mq: MQTT):
    # RESTORED: Sequence Lock
    with SEQUENCE_LOCK:
        if not _open_vehicle_position(adb):
            jslog("WRN", "could not open Vehicle position page; skipping")
            return
        path = dump_xml(adb)
        vals = parse_position_xml(path)
        if "latitude" in vals:
            mq.publish(f"{cfg.topic_base}/latitude", str(vals["latitude"]), retain=True, qos=1)
            mq.publish(f"{cfg.topic_base}/longitude", str(vals["longitude"]), retain=True, qos=1)
            mq.publish(f"{cfg.topic_base}/location_json", json.dumps(vals), retain=True, qos=1)
        adb.shell("input keyevent 4", timeout=2.0)

def task_ac_page(cfg: Config, adb: ADB, mq: MQTT):
    # RESTORED: Sequence Lock
    with SEQUENCE_LOCK:
        if not _open_ac_page(adb):
            jslog("WRN", "could not open A/C page; skipping")
            return
        path = dump_xml(adb)
        vals = parse_ac_xml(path)
        if vals:
            for key, value in vals.items():
                mq.publish(f"{cfg.topic_base}/{key}", str(value), retain=True, qos=1)
        adb.shell("input keyevent 4", timeout=2.0)

# ---------- Main loop ----------

class CommandWorker:
    def __init__(self, cfg: Config, adb: ADB):
        self.q = queue.Queue()
        self.cfg = cfg
        self.adb = adb
        self.t = threading.Thread(target=self._run, name="cmd-worker", daemon=True)
        self.t.start()

    def submit(self, action):
        self.q.put(action)

    def _run(self):
        while True:
            action = self.q.get()
            try:
                with SEQUENCE_LOCK:
                    execute_command(self.cfg, self.adb, action)
            except Exception as e:
                jslog("ERR", "command failed", action=action, error=str(e))
            self.q.task_done()

def build_caps(cfg: Config) -> Dict[str, bool]:
    return {"home": True, "status": cfg.enable_vehicle_status, "position": cfg.enable_vehicle_position, "ac": cfg.enable_ac_page}

def main():
    cfg = load_config()
    jslog("INF", "boot", 
          intervals={"run":cfg.poll_running, "chg":cfg.poll_charging, "off":cfg.poll_shutdown},
    )

    maybe_start_status_server(cfg.http_status_port)

    mq = MQTT(cfg); mq.connect()

    adb = ADB(cfg.phone_ip, cfg.adb_port)
    try: adb.shell("echo warmup >/dev/null", timeout=2.0)
    except Exception as e: jslog("WRN", "adb warmup failed", error=str(e))

    caps = build_caps(cfg); publish_discovery(cfg, mq, caps)

    global worker; worker = CommandWorker(cfg, adb)

    def on_mqtt_cmd(topic, payload):
        action = topic.split("/cmd/")[-1]
        worker.submit(action)
    mq.subscribe_handler(f"{cfg.topic_base}/cmd/#", on_mqtt_cmd)

    consecutive_failures = 0
    
    # Main Loop with Adaptive Polling, Deep Sleep, and Self-Healing
    while True:
        _StatusHandler.METRICS["poll_cycles_total"] += 1
        t0 = time.time()
        
        detected_status_text = ""
        poll_mode = "RUNNING" # Default safety
        delay = cfg.poll_running

        try:
            # --- PHASE 0: Smart State Recovery ---
            # Ensure we are on the Home screen before we start.
            # This handles "Stuck on Sub-page" and "Crashed to Desktop".
            if not ensure_home_state(cfg, adb):
                raise Exception("Failed to navigate to Home Screen")

            # --- PHASE 1: Always Run Home Task ---
            # This is the "Lightweight" check to see what the car is doing.
            res = task_home(cfg, adb, mq)
            if isinstance(res, str):
                detected_status_text = res

            # --- PHASE 2: State Detection ---
            st_clean = detected_status_text.lower()
            
            # Check Hardcoded Constants for State
            if any(s.lower() in st_clean for s in STRINGS_SHUTDOWN):
                poll_mode = "SHUTDOWN"
                delay = cfg.poll_shutdown
            elif any(s.lower() in st_clean for s in STRINGS_CHARGING):
                poll_mode = "CHARGING"
                delay = cfg.poll_charging
            else:
                poll_mode = "RUNNING"
                delay = cfg.poll_running

            # --- PHASE 3: Conditional "Deep Sleep" Execution ---
            # Suggestion 1: Only run heavy tasks if NOT shutdown
            if poll_mode != "SHUTDOWN":
                if cfg.enable_vehicle_status:
                    task_vehicle_status(cfg, adb, mq)
                if cfg.enable_vehicle_position:
                    task_vehicle_position(cfg, adb, mq)
                if cfg.enable_ac_page:
                    task_ac_page(cfg, adb, mq)
            else:
                jslog("INF", "skipping heavy tasks (status/pos/ac) due to shutdown mode")

            # Log decision
            jslog("INF", "state detect", text=detected_status_text, mode=poll_mode, next_poll_in=delay)
            _StatusHandler.LAST_HEALTH["last_state"] = poll_mode

            # Reset failures on success
            consecutive_failures = 0
            _StatusHandler.LAST_HEALTH["last_ok"] = datetime.now(timezone.utc).isoformat()
        
        except Exception as e:
            _StatusHandler.METRICS["poll_failures_total"] += 1
            consecutive_failures += 1
            jslog("ERR", "poll iteration failed", error=str(e), failures=consecutive_failures)

            # --- PHASE 4: Self-Healing (Suggestion 5) ---
            
            # Soft Healing: Try to reconnect ADB on failure #3
            if consecutive_failures == 3:
                jslog("WRN", "Attempting soft ADB reconnect...")
                try:
                    adb.shell("disconnect") 
                    time.sleep(2)
                    Popen(["adb", "connect", f"{cfg.phone_ip}:{cfg.adb_port}"]).wait()
                except Exception as ex:
                    jslog("ERR", "Soft reconnect failed", error=str(ex))

            # Hard Kill Switch: Exit on failure #5
            if consecutive_failures >= 5:
                jslog("FATAL", "Too many consecutive failures. Exiting to trigger Docker restart.")
                sys.exit(1)

            # Default to shorter delay on failure to retry soon
            delay = cfg.poll_running
        
        elapsed = time.time() - t0
        sleep_time = max(0.0, delay - elapsed)
        time.sleep(sleep_time)

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: jslog("INF","shutdown requested")
