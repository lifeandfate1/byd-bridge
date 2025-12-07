#!/usr/bin/env python3
"""
BYD Bridge — ADB-driven scraper + MQTT publisher for Home Assistant.

Key improvements:
- Robust ADB wrapper with retries/backoff and new exec_out() support.
- UI dump no longer uses `adb pull`; streams XML via `exec-out` or fallback `cat`.
- Structured JSON logs; optional status server (/healthz, /metrics).
- HA Discovery with module flags for Vehicle Status, Position, A/C.
- Change-only retained publishes; adaptive backoff on failures.
- Strict env & *_FILE secret support.
"""

import os
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

# ---- BYD selectors (from older working code) ----
SEL = {
    # Home (read)
    "home_range": "com.byd.bydautolink:id/h_km_tv",
    "home_soc": "com.byd.bydautolink:id/tv_batter_percentage",
    "home_cabin_temp": "com.byd.bydautolink:id/car_inner_temperature",
    "home_setpoint": "com.byd.bydautolink:id/tem_tv",
    "home_car_status": "com.byd.bydautolink:id/h_car_name_tv",
    "home_last_update": "com.byd.bydautolink:id/tv_update_time",

    # Home (tap targets)
    "home_ac_row": "com.byd.bydautolink:id/c_air_item_rl_2",  # opens A/C page

    # A/C page (read)
    "ac_setpoint": "com.byd.bydautolink:id/tem_tv",
    "ac_prev": "com.byd.bydautolink:id/tem_tv_last",
    "ac_next": "com.byd.bydautolink:id/tem_tv_next",
    "ac_power_btn": "com.byd.bydautolink:id/c_air_item_power_btn",  # child text “Switch on/off”

    # A/C page (tap)
    "ac_heat_btn": "com.byd.bydautolink:id/c_air_item_heat_btn",
    "ac_cool_btn": "com.byd.bydautolink:id/c_air_item_cool_btn",

    # Page nav by visible text (fallbacks)
    "nav_vehicle_status_text": "Vehicle status",
    "nav_vehicle_position_text": "Vehicle position",
    "nav_ac_text": "A/C",
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
    # bounds like: [x1,y1][x2,y2]
    m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.attrib.get("bounds", ""))
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return ((x1 + x2) // 2, (y1 + y2) // 2)

def _find_by_rid(root: ET.Element, rid: str):
    for n in _all_nodes(root):
        if _rid(n) == rid:
            return n
    return None

def _find_all_text_contains(root: ET.Element, sub: str):
    sub_low = sub.lower()
    return [n for n in _all_nodes(root) if _txt(n).lower().find(sub_low) != -1]

def _find_text_equals(root: ET.Element, s: str):
    s_low = s.lower()
    for n in _all_nodes(root):
        if _txt(n).lower() == s_low:
            return n
    return None

def _adb_tap_center_of(adb: "ADB", node: ET.Element) -> bool:
    c = _bounds(node)
    if not c:
        return False
    x, y = c
    adb.shell(f"input tap {x} {y}", timeout=2.0)
    return True

# External dep
try:
    import paho.mqtt.client as mqtt  # type: ignore
except Exception:
    mqtt = None  # type: ignore

# ---------- Utility logging ----------

def jslog(level: str, msg: str, **fields):
    record = {"ts": datetime.now(timezone.utc).isoformat(), "level": level.upper()[:3], "msg": msg}
    if fields:
        record.update(fields)
    print(f'{record["level"]} | ' + json.dumps({k:v for k,v in record.items() if k not in ("level",)}, ensure_ascii=False), flush=True)

# ---------- Env & secrets ----------

def read_env_or_file(name: str, default: Optional[str]=None) -> Optional[str]:
    val = os.getenv(name)
    if val is not None and val != "":
        return val
    f = os.getenv(name + "_FILE")
    if f:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except Exception as e:
            jslog("ERR", "failed to read secret file", var=name, path=f, error=str(e))
    return default

def env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip() not in ("0","false","False","no","NO")

def env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError:
        return default

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
    poll_seconds: int
    phone_ip: str
    adb_port: int
    enable_vehicle_status: bool
    enable_vehicle_position: bool
    enable_ac_page: bool
    byd_pin: Optional[str]
    http_status_port: int
    discovery_prefix: str  # (1) NEW

def load_config() -> Config:
    mqtt_broker = read_env_or_file("MQTT_BROKER", None)
    phone_ip    = read_env_or_file("PHONE_IP", None)
    if not mqtt_broker:
        raise SystemExit("MQTT_BROKER is required")
    if not phone_ip:
        raise SystemExit("PHONE_IP is required")

    return Config(
        mqtt_broker=mqtt_broker,
        mqtt_port=env_int("MQTT_PORT", 1883),
        mqtt_user=read_env_or_file("MQTT_USER", None),
        mqtt_pass=read_env_or_file("MQTT_PASS", None),
        topic_base=os.getenv("MQTT_TOPIC_BASE", "byd/app").rstrip("/"),
        device_id=os.getenv("BYD_DEVICE_ID", "byd-vehicle"),
        device_name=os.getenv("BYD_DEVICE_NAME", "BYD Vehicle"),
        poll_seconds=env_int("POLL_SECONDS", 60),
        phone_ip=phone_ip,
        adb_port=env_int("ADB_PORT", 5555),
        enable_vehicle_status=env_bool("ENABLE_VEHICLE_STATUS", True),
        enable_vehicle_position=env_bool("ENABLE_VEHICLE_POSITION", True),
        enable_ac_page=env_bool("ENABLE_AC_PAGE", True),
        byd_pin=read_env_or_file("BYD_PIN", None),
        http_status_port=env_int("HTTP_STATUS_PORT", 8080),
        discovery_prefix=os.getenv("DISCOVERY_PREFIX", "homeassistant"),  # (1) NEW
    )

# ---------- HTTP Status Server ----------

class _StatusHandler(http.server.BaseHTTPRequestHandler):
    METRICS = {
        "poll_cycles_total": 0,
        "poll_failures_total": 0,
        "adb_calls_total": 0,
        "dumps_failed_total": 0,
        "mqtt_publishes_total": 0,
    }
    LAST_HEALTH = {"last_ok": "", "mqtt": "unknown"}

    def log_message(self, format, *args):
        return

    def do_GET(self):
        if self.path == "/healthz":
            body = json.dumps(self.LAST_HEALTH).encode("utf-8")
            self.send_response(200); self.send_header("Content-Type","application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        if self.path == "/metrics":
            lines = []
            for k, v in self.METRICS.items():
                lines.append(f"# TYPE {k} counter"); lines.append(f"{k} {v}")
            body = ("\n".join(lines)+"\n").encode("utf-8")
            self.send_response(200); self.send_header("Content-Type","text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_response(404); self.end_headers()

def maybe_start_status_server(port: int):
    if not port:
        return None
    def _serve():
        with socketserver.TCPServer(("0.0.0.0", port), _StatusHandler) as httpd:
            jslog("INF", "status server started", port=port)
            httpd.serve_forever()
    t = threading.Thread(target=_serve, name="status-http", daemon=True); t.start(); return t

# ---------- ADB wrapper ----------

class ADBError(Exception):
    pass

class ADB:
    def __init__(self, host: str, port: int, base_timeout: float = 8.0, max_retries: int = 3):
        self.host = host; self.port = port
        self.base_timeout = base_timeout; self.max_retries = max_retries
        self.lock = threading.RLock()

    def _run(self, args: List[str], timeout: Optional[float]) -> Tuple[int, bytes, bytes]:
        cmd = ["adb", "-s", f"{self.host}:{self.port}"] + args
        _StatusHandler.METRICS["adb_calls_total"] += 1
        jslog("DBG", "adb exec", cmd=" ".join(cmd))
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
                    if rc == 0:
                        return out
                    last_err = err.decode("utf-8","ignore")
                    jslog("WRN", "adb exec-out nonzero", attempt=attempt, rc=rc, stderr=last_err)
                except ADBError as e:
                    last_err = str(e); jslog("WRN", "adb exec-out error", attempt=attempt, error=last_err)
                time.sleep(delay); delay = min(delay*2, 2.0)
            raise ADBError(last_err or "unknown adb exec-out error")

    def shell(self, cmd: str, timeout: Optional[float]=None) -> str:
        with self.lock:
            delay = 0.25; last_err = None
            for attempt in range(1, self.max_retries+1):
                try:
                    rc, out, err = self._run(["shell", cmd], timeout)
                    if rc == 0:
                        return out.decode("utf-8","ignore")
                    last_err = err.decode("utf-8","ignore")
                    jslog("WRN", "adb shell nonzero", attempt=attempt, rc=rc, stderr=last_err)
                except ADBError as e:
                    last_err = str(e); jslog("WRN", "adb shell error", attempt=attempt, error=last_err)
                time.sleep(delay); delay = min(delay*2, 2.0)
            raise ADBError(last_err or "unknown adb error")

# ---------- MQTT helper ----------

class MQTT:
    def __init__(self, cfg: Config):
        if mqtt is None:
            raise SystemExit("paho-mqtt is required at runtime")
        self.cfg = cfg
        self.client = mqtt.Client(client_id=f"byd-bridge-{cfg.device_id}", clean_session=True)
        if cfg.mqtt_user:
            self.client.username_pw_set(cfg.mqtt_user, cfg.mqtt_pass or "")
        self.client.will_set(f"{self.cfg.topic_base}/availability", payload="offline", retain=True, qos=1)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        self._connected = threading.Event()
        self._handlers: Dict[str, Callable[[str, str], None]] = {}
        self._state_cache: Dict[str, str] = {}

    def connect(self):
        self.client.connect(self.cfg.mqtt_broker, self.cfg.mqtt_port, keepalive=60)
        self.client.loop_start()
        if not self._connected.wait(timeout=10):
            raise RuntimeError("MQTT connect timeout")
        # (Optional) qos=1 on retained state publishes
        self.publish(f"{self.cfg.topic_base}/availability", "online", retain=True, qos=1)

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected.set()
            _StatusHandler.LAST_HEALTH["mqtt"] = "connected"
            jslog("INF", "mqtt connected", broker=self.cfg.mqtt_broker)
        else:
            jslog("ERR", "mqtt connection failed", rc=rc)

    def _on_disconnect(self, client, userdata, rc):
        self._connected.clear()
        _StatusHandler.LAST_HEALTH["mqtt"] = "disconnected"
        jslog("WRN", "mqtt disconnected", rc=rc)

    def publish(self, topic: str, payload: str, retain: bool=False, qos: int=0):
        if payload is None:
            payload = ""
        key = topic
        if retain and self._state_cache.get(key) == payload:
            return
        res = self.client.publish(topic, payload, qos=qos, retain=retain)
        res.wait_for_publish()
        if res.rc != 0:
            jslog("WRN", "mqtt publish failed", topic=topic, rc=res.rc)
        else:
            if retain:
                self._state_cache[key] = payload
            _StatusHandler.METRICS["mqtt_publishes_total"] += 1

    def subscribe_handler(self, topic_filter: str, handler: Callable[[str, str], None]):
        self._handlers[topic_filter] = handler
        self.client.subscribe(topic_filter, qos=0)

    def _on_message(self, client, userdata, msg):
        for filt, handler in self._handlers.items():
            if filt.endswith("#"):
                prefix = filt[:-1]
                if msg.topic.startswith(prefix):
                    handler(msg.topic, msg.payload.decode("utf-8","ignore")); return
            if msg.topic == filt:
                handler(msg.topic, msg.payload.decode("utf-8","ignore")); return

# ---------- Discovery ----------

def device_info(cfg: Config) -> Dict[str, Any]:
    return {"identifiers":[cfg.device_id], "manufacturer":"BYD", "name":cfg.device_name, "model":"BYD App Bridge", "sw_version":"bridge-1.0.1"}

def disc_topic(cfg: Config, domain: str, object_id: str) -> str:  # (1) UPDATED SIGNATURE
    # Discovery prefix now configurable
    return f"{cfg.discovery_prefix}/{domain}/{object_id}/config"

def publish_discovery(cfg: Config, mq: MQTT, caps: Dict[str, bool]):
    dev = device_info(cfg)
    # (2) Availability on entities
    AV = {"avty_t": f"{cfg.topic_base}/availability", "pl_avail": "online", "pl_not_avail": "offline"}

    # Home (always)
    home_entities = [
        ("sensor","byd_battery_soc",{"name":"Battery State of Charge","state_topic":f"{cfg.topic_base}/battery_soc","unit_of_measurement":"%","device_class":"battery","icon":"mdi:battery"}),
        ("sensor","byd_range_km",{"name":"Range","state_topic":f"{cfg.topic_base}/range_km","unit_of_measurement":"km","device_class":"distance","icon":"mdi:map-marker-distance"}),
        ("sensor","byd_cabin_temp_c",{"name":"Cabin Temperature","state_topic":f"{cfg.topic_base}/cabin_temp_c","unit_of_measurement":"°C","device_class":"temperature"}),
        ("sensor","byd_climate_setpoint_c",{"name":"Climate Setpoint","state_topic":f"{cfg.topic_base}/climate_target_temp_c","unit_of_measurement":"°C","icon":"mdi:thermostat"}),
        ("sensor","byd_status",{"name":"Vehicle Status","state_topic":f"{cfg.topic_base}/car_status","icon":"mdi:car-info"}),
        ("sensor","byd_last_update",{"name":"BYD App Last Update","state_topic":f"{cfg.topic_base}/last_update","icon":"mdi:update"}),
    ]
    for domain, oid, extra in home_entities:
        payload = {"uniq_id": f"{oid}_{cfg.device_id}", "name": extra["name"], "stat_t": extra["state_topic"], "dev": dev}
        payload.update({k:v for k,v in extra.items() if k not in ("name","state_topic")})
        payload.update(AV)
        mq.publish(disc_topic(cfg, domain, oid), json.dumps(payload), retain=True, qos=1)

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
            # (3) Units/names changed to efficiency:
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
    else:
        for oid in [
            "byd_odometer_km","byd_driving_status","byd_whole_vehicle","byd_doors","byd_windows","byd_bonnet","byd_boot",
            "byd_tyre_fl_psi","byd_tyre_fr_psi","byd_tyre_rl_psi","byd_tyre_rr_psi",
            "byd_energy_recent_kwh_per_100km","byd_energy_total_kwh_per_100km"  # (3) UPDATED NAMES
        ]:
            mq.publish(disc_topic(cfg, "sensor", oid), "", retain=True, qos=1)

    # Vehicle Position (conditional)
    if caps.get("position"):
        dt_oid = "byd_vehicle_tracker"
        dt_payload = {
            "uniq_id": f"{dt_oid}_{cfg.device_id}", "name": "BYD Vehicle", "dev": dev,
            "json_attr_t": f"{cfg.topic_base}/location_json",
            "state_topic": f"{cfg.topic_base}/vehicle_tracker_state",
            "icon": "mdi:car-arrow-right",
        }
        dt_payload.update(AV)  # (Optional) availability on device_tracker
        mq.publish(disc_topic(cfg, "device_tracker", dt_oid), json.dumps(dt_payload), retain=True, qos=1)
        for oid, name, key in [("byd_latitude","Latitude","latitude"),("byd_longitude","Longitude","longitude")]:
            payload = {"uniq_id": f"{oid}_{cfg.device_id}", "name": name, "stat_t": f"{cfg.topic_base}/{key}", "dev": dev, "icon":"mdi:crosshairs-gps"}
            payload.update(AV)
            mq.publish(disc_topic(cfg, "sensor", oid), json.dumps(payload), retain=True, qos=1)
    else:
        mq.publish(disc_topic(cfg, "device_tracker","byd_vehicle_tracker"), "", retain=True, qos=1)
        mq.publish(disc_topic(cfg, "sensor","byd_latitude"), "", retain=True, qos=1)
        mq.publish(disc_topic(cfg, "sensor","byd_longitude"), "", retain=True, qos=1)

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
    else:
        for oid in ["byd_climate_target_temp_c","byd_climate_prev_temp_c","byd_climate_next_temp_c","byd_climate_power"]:
            mq.publish(disc_topic(cfg, "sensor", oid), "", retain=True, qos=1)
        for oid in ["byd_ac_up_btn","byd_ac_down_btn","byd_climate_rapid_heat","byd_climate_rapid_vent","byd_climate_switch_on"]:
            mq.publish(disc_topic(cfg, "button", oid), "", retain=True, qos=1)

# ---------- Command worker ----------

class CommandWorker:
    def __init__(self):
        self.q: "queue.Queue[Tuple[str, Callable[[], None]]]" = queue.Queue()
        self.t = threading.Thread(target=self._run, name="cmd-worker", daemon=True)
        self._stop = threading.Event()

    def start(self): self.t.start()
    def stop(self):
        self._stop.set()
        try: self.q.put_nowait(("__stop__", lambda: None))
        except Exception: pass
        self.t.join(timeout=2)

    def submit(self, key: str, fn: Callable[[], None]):
        try:
            if key in ("ac_up","ac_down"):
                items = []
                while not self.q.empty():
                    k, f = self.q.get_nowait()
                    if k not in ("ac_up","ac_down"):
                        items.append((k,f))
                for it in items: self.q.put_nowait(it)
            self.q.put_nowait((key, fn))
        except Exception as e:
            jslog("WRN", "command enqueue failed", error=str(e))

    def _run(self):
        while not self._stop.is_set():
            k, fn = self.q.get()
            if k == "__stop__": break
            try:
                jslog("INF", "command start", key=k); fn(); jslog("INF", "command ok", key=k)
            except Exception as e:
                jslog("ERR", "command failed", key=k, error=str(e))

# ---------- Scraper placeholders ----------

# Write UI dumps to /tmp by default (override with XML_TMP env)
XML_TMP = os.getenv("XML_TMP", "/tmp/byd_last_dump.xml")

def _looks_like_xml(buf: bytes) -> bool:
    if not buf:
        return False
    b = buf.lstrip()
    return b.startswith(b"<?xml") or b[:200].find(b"<?xml") != -1

def dump_xml(adb: ADB) -> str:
    """Dump current UI and write to local file, avoiding scoped-storage issues.

    Strategy (robust path):
      1) adb shell uiautomator dump [--compressed]
         - parse "dumped to: /sdcard/XXX.xml"
      2) adb exec-out cat <that path>   (fallback to adb shell cat)
      3) validate XML header, write to XML_TMP, return
    """
    target = XML_TMP
    # Ensure the target directory exists and is writable
    os.makedirs(os.path.dirname(target), exist_ok=True)

    # 1) Try with --compressed first, then without
    dump_out = None
    try:
        dump_out = adb.shell("uiautomator dump --compressed", timeout=10.0)
        jslog("DBG", "uiautomator dump (compressed) stdout", stdout=dump_out.strip())
    except Exception as e:
        jslog("WRN", "uiautomator dump --compressed failed; will try without", error=str(e))
        try:
            dump_out = adb.shell("uiautomator dump", timeout=10.0)
            jslog("DBG", "uiautomator dump (plain) stdout", stdout=dump_out.strip())
        except Exception as e2:
            _StatusHandler.METRICS["dumps_failed_total"] += 1
            jslog("ERR", "uiautomator dump failed (plain)", error=str(e2))
            raise

    # Parse the dumped path
    import re as _re
    m = _re.search(r"dump(?:ed)? to:\s*(/sdcard/\S+\.xml)", dump_out or "") or _re.search(r"(/sdcard/\S+\.xml)", dump_out or "")
    dump_path = m.group(1) if m else "/sdcard/window_dump.xml"
    jslog("DBG", "parsed dump path", path=dump_path)

    # 2) Fetch file content (prefer exec-out; fallback to shell)
    try:
        out = adb.exec_out(["cat", dump_path], timeout=6.0)
    except Exception as e:
        jslog("WRN", "exec-out cat failed, trying shell cat", error=str(e))
        try:
            out = adb.shell(f"cat {dump_path}", timeout=6.0).encode("utf-8", "ignore")
        except Exception as e2:
            _StatusHandler.METRICS["dumps_failed_total"] += 1
            jslog("ERR", "failed to retrieve dump file via shell cat", error=str(e2))
            raise

    # 3) Validate content and write to target
    if isinstance(out, (bytes, bytearray)):
        ok = _looks_like_xml(out)
        data = out
    else:
        ok = (out or b"").lstrip().startswith(b"<?xml")
        data = out if isinstance(out, (bytes, bytearray)) else (out or b"")
    if not ok:
        _StatusHandler.METRICS["dumps_failed_total"] += 1
        jslog("ERR", "dump file content did not look like XML", first_bytes=(data[:64] if data else b"").decode("utf-8","ignore"))
        raise ADBError("dump file content did not look like XML")

    with open(target, "wb") as fh:
        fh.write(data)
    return target

def parse_home_xml(path: str) -> Dict[str, Any]:
    root = _parse_xml(path)
    out: Dict[str, Any] = {}

    # SOC (e.g., "100%")
    if (n := _find_by_rid(root, SEL["home_soc"])) is not None:
        m = re.search(r"(\d+)", _txt(n))
        if m: out["battery_soc"] = int(m.group(1))

    # Range (e.g., "567km")
    if (n := _find_by_rid(root, SEL["home_range"])) is not None:
        m = re.search(r"(\d+)", _txt(n))
        if m: out["range_km"] = int(m.group(1))

    # Cabin temperature (e.g., "23°C")
    if (n := _find_by_rid(root, SEL["home_cabin_temp"])) is not None:
        m = re.search(r"(-?\d+(?:\.\d+)?)", _txt(n))
        if m: out["cabin_temp_c"] = float(m.group(1))

    # Climate setpoint (e.g., "22°")
    if (n := _find_by_rid(root, SEL["home_setpoint"])) is not None:
        m = re.search(r"(-?\d+(?:\.\d+)?)", _txt(n))
        if m: out["climate_target_temp_c"] = float(m.group(1))

    # Car status (free text like “Switched off”)
    if (n := _find_by_rid(root, SEL["home_car_status"])) is not None:
        t = _txt(n)
        if t: out["car_status"] = t

    # Last update (free text)
    if (n := _find_by_rid(root, SEL["home_last_update"])) is not None:
        t = _txt(n)
        if t: out["last_update"] = t

    return out

def parse_status_xmls(top_path: str, bottom_path: str) -> Dict[str, Any]:
    # Many values live across a scroll; we accept one dump (top_path) for now.
    # Strategy: use label->value by proximity, plus direct regex for tyres/odo/energy.
    root = _parse_xml(top_path)
    text_nodes = [(_txt(n), n) for n in _all_nodes(root) if _txt(n)]

    out: Dict[str, Any] = {}

    # Tyres (any “NN.Npsi”)
    tyre_vals = []
    for t, n in text_nodes:
        m = re.search(r"(\d+\.\d)\s*psi", t, re.I)
        if m: tyre_vals.append(float(m.group(1)))
    if len(tyre_vals) >= 4:
        # Heuristic order: FL, FR, RL, RR (as the app visually lays)
        out["tyre_fl_psi"], out["tyre_fr_psi"], out["tyre_rl_psi"], out["tyre_rr_psi"] = tyre_vals[:4]

    # Odometer (e.g., "9311 km")
    for t, _ in text_nodes:
        m = re.search(r"(\d{1,7})\s*km\b", t, re.I)
        if m:
            out["odometer_km"] = int(m.group(1))
            break

    # Energy (Past 50 km Electricity, Cumulative AEC) – capture efficiency values “kWh/100km”
    for t, _ in text_nodes:
        m = re.search(r"(\d+(?:\.\d+)?)\s*kW.?h/100km", t)
        if m and "energy_recent_kwh_per_100km" not in out:
            out["energy_recent_kwh_per_100km"] = float(m.group(1))  # (3) RENAMED KEY
        elif m:
            out["energy_total_kwh_per_100km"] = float(m.group(1))   # (3) RENAMED KEY

    # Simple label → value mappings by nearest following text node
    def _label_value(label: str, key: str):
        n = _find_text_equals(root, label)
        if n is None:
            return
        # Find a sibling-ish value after this label (same subtree, next text element)
        after = []
        found = False
        for el in _all_nodes(root):
            if el is n:
                found = True
                continue
            if found and _txt(el):
                after.append(_txt(el))
                if len(after) >= 1: break
        if after:
            out[key] = after[0]

    _label_value("Front bonnet", "bonnet")
    _label_value("Doors", "doors")
    _label_value("Windows", "windows")
    _label_value("Boot", "boot")
    _label_value("Whole Vehicle Status", "whole_vehicle")
    _label_value("Driving status", "driving_status")

    return out

def parse_ac_xml(path: str) -> Dict[str, Any]:
    root = _parse_xml(path)
    out: Dict[str, Any] = {}

    # Current setpoint
    if (n := _find_by_rid(root, SEL["ac_setpoint"])) is not None:
        m = re.search(r"(-?\d+(?:\.\d+)?)", _txt(n))
        if m: out["climate_target_temp_c"] = float(m.group(1))

    # Prev / Next setpoints
    if (n := _find_by_rid(root, SEL["ac_prev"])) is not None:
        m = re.search(r"(-?\d+(?:\.\d+)?)", _txt(n))
        if m: out["climate_prev_temp_c"] = float(m.group(1))
    if (n := _find_by_rid(root, SEL["ac_next"])) is not None:
        m = re.search(r"(-?\d+(?:\.\d+)?)", _txt(n))
        if m: out["climate_next_temp_c"] = float(m.group(1))

    # Power state: read the button container, check child/descendant text
    btn = _find_by_rid(root, SEL["ac_power_btn"])
    if btn:
        # Search descendants for “Switch on/off” or icon state
        texts = [ _txt(n) for n in btn.iter() if _txt(n) ]
        # Very loose heuristic: if “Switch on” appears, we consider current state “off” (press to switch on)
        if any("Switch on" in t for t in texts):
            out["climate_power"] = "off"
        elif any("Switch off" in t for t in texts):
            out["climate_power"] = "on"

    return out

def parse_position_xml(path: str) -> Dict[str, Any]:
    root = _parse_xml(path)
    # Scan any text for lat/lon (allow any number of decimals)
    latlon = None
    for n in _all_nodes(root):
        t = _txt(n)
        m = re.search(r"(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)", t)  # (5) RELAXED
        if m:
            latlon = (float(m.group(1)), float(m.group(2)))
            break
    if latlon:
        return {"latitude": latlon[0], "longitude": latlon[1], "gps_accuracy": 10}
    return {}

def _open_vehicle_status(adb: "ADB") -> bool:
    # Start from wherever we are: dump once, try to tap the “Vehicle status” card/label
    p = dump_xml(adb)
    root = _parse_xml(p)
    target = _find_text_equals(root, SEL["nav_vehicle_status_text"])
    if target is None:
        # Some builds show the label inside a card; try any node containing it
        cands = _find_all_text_contains(root, SEL["nav_vehicle_status_text"])
        if cands:
            target = cands[0]
    if (target is not None) and _adb_tap_center_of(adb, target):
        time.sleep(0.8)
        return True
    return False

def _open_ac_page(adb: "ADB") -> bool:
    # Try by resource-id on home; fallback to text “A/C”
    p = dump_xml(adb)
    root = _parse_xml(p)
    target = _find_by_rid(root, SEL["home_ac_row"])
    if target is None:
        target = _find_text_equals(root, SEL["nav_ac_text"])
    if (target is not None) and _adb_tap_center_of(adb, target):
        time.sleep(0.6)
        return True
    return False

def _open_vehicle_position(adb: "ADB") -> bool:
    p = dump_xml(adb)
    root = _parse_xml(p)
    target = _find_text_equals(root, SEL["nav_vehicle_position_text"])
    if target is None:
        c = _find_all_text_contains(root, SEL["nav_vehicle_position_text"])
        target = c[0] if c else None
    if (target is not None) and _adb_tap_center_of(adb, target):
        time.sleep(0.8)
        return True
    return False

# ---------- Tasks ----------

def task_home(cfg: Config, adb: ADB, mq: MQTT):
    path = dump_xml(adb)
    vals = parse_home_xml(path)
    if not vals:
        jslog("WRN", "home XML yielded no values; skipping publish")
        return
    # sanity: only publish when at least SOC or range is present
    if "battery_soc" not in vals and "range_km" not in vals:
        jslog("WRN", "home XML missing key markers; skipping publish")
        return
    for key, value in vals.items():
        mq.publish(f"{cfg.topic_base}/{key}", json.dumps(value) if isinstance(value, (dict,list)) else str(value), retain=True, qos=1)  # (Optional) qos=1

def task_vehicle_status(cfg: Config, adb: ADB, mq: MQTT):
    if not _open_vehicle_status(adb):
        jslog("WRN", "could not open Vehicle status page (tap failed); skipping")
        return

    time.sleep(0.5)

    # Top half scrape
    top = dump_xml(adb)
    vals_top = parse_status_xmls(top, top)

    # ↓↓↓ THESE ARE THE TWO LINES I MEANT ↓↓↓
    _scroll_down_once(adb)
    time.sleep(0.6)
    # ↑↑↑ KEEP THEM IN THIS ORDER ↑↑↑

    # Bottom half scrape
    bottom = dump_xml(adb)
    vals_bottom = parse_status_xmls(bottom, bottom)

    # Merge (bottom overrides top if keys overlap)
    vals = {**vals_top, **vals_bottom}

    # Require at least one strong marker (odometer or any tyre psi)
    has_marker = ("odometer_km" in vals) or any(k.startswith("tyre_") for k in vals)
    if not has_marker:
        jslog("WRN", "status XML had no strong markers; skipping publish")
        try:
            adb.shell("input keyevent 4", timeout=2.0)  # go back anyway
        except Exception:
            pass
        return

    for key, value in vals.items():
        mq.publish(f"{cfg.topic_base}/{key}", str(value), retain=True, qos=1)

    # Navigate back to Home
    try:
        adb.shell("input keyevent 4", timeout=2.0)
    except Exception as e:
        jslog("WRN", "failed to navigate back from vehicle status", error=str(e))


def task_vehicle_position(cfg: Config, adb: ADB, mq: MQTT):
    if not _open_vehicle_position(adb):
        jslog("WRN", "could not open Vehicle position page; skipping")
        return
    time.sleep(0.5)
    path = dump_xml(adb)
    vals = parse_position_xml(path)
    if "latitude" in vals and "longitude" in vals:
        mq.publish(f"{cfg.topic_base}/latitude", str(vals["latitude"]), retain=True, qos=1)   # (Optional) qos=1
        mq.publish(f"{cfg.topic_base}/longitude", str(vals["longitude"]), retain=True, qos=1) # (Optional) qos=1
        mq.publish(f"{cfg.topic_base}/location_json", json.dumps(vals), retain=True, qos=1)   # (Optional) qos=1
        mq.publish(f"{cfg.topic_base}/vehicle_tracker_state", "unknown", retain=True, qos=1)  # (6) publish 'unknown' instead of 'home'
    else:
        jslog("WRN", "position XML had no lat/lon; skipping publish")

def task_ac_page(cfg: Config, adb: ADB, mq: MQTT):
    if not _open_ac_page(adb):
        jslog("WRN", "could not open A/C page; skipping")
        return
    time.sleep(0.4)
    path = dump_xml(adb)
    vals = parse_ac_xml(path)
    if not vals:
        jslog("WRN", "A/C XML yielded no values; skipping publish")
        return
    # Require at least the setpoint, otherwise skip
    if "climate_target_temp_c" not in vals:
        jslog("WRN", "A/C XML missing setpoint; skipping publish")
        return
    for key, value in vals.items():
        mq.publish(f"{cfg.topic_base}/{key}", str(value), retain=True, qos=1)  # (Optional) qos=1

# ---------- Main loop ----------

def build_caps(cfg: Config) -> Dict[str, bool]:
    return {"home": True, "status": cfg.enable_vehicle_status, "position": cfg.enable_vehicle_position, "ac": cfg.enable_ac_page}

def build_tasks(cfg: Config) -> List[Callable[[Config, ADB, MQTT], None]]:
    tasks = [task_home]
    if cfg.enable_vehicle_status: tasks.append(task_vehicle_status)
    if cfg.enable_vehicle_position: tasks.append(task_vehicle_position)
    if cfg.enable_ac_page: tasks.append(task_ac_page)
    return tasks

def main():
    cfg = load_config()
    jslog("INF", "boot", modules={"vehicle_status":cfg.enable_vehicle_status,"vehicle_position":cfg.enable_vehicle_position,"ac_page":cfg.enable_ac_page})

    maybe_start_status_server(cfg.http_status_port)

    mq = MQTT(cfg); mq.connect()

    adb = ADB(cfg.phone_ip, cfg.adb_port)
    try: adb.shell("echo warmup >/dev/null", timeout=2.0)
    except Exception as e: jslog("WRN", "adb warmup failed", error=str(e))

    caps = build_caps(cfg); publish_discovery(cfg, mq, caps)

    if cfg.enable_ac_page:
        def handle_cmd(topic: str, payload: str):
            action = topic.split("/cmd/")[-1]
            def do(): time.sleep(0.2)  # TODO: tap actions + ensure PIN
            worker.submit(action, do)
        mq.subscribe_handler(f"{cfg.topic_base}/cmd/#", handle_cmd)

    global worker; worker = CommandWorker(); worker.start()

    tasks = build_tasks(cfg)
    consecutive_failures = 0
    while True:
        _StatusHandler.METRICS["poll_cycles_total"] += 1
        t0 = time.time()
        try:
            for t in tasks: t(cfg, adb, mq)
            consecutive_failures = 0
            _StatusHandler.LAST_HEALTH["last_ok"] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            _StatusHandler.METRICS["poll_failures_total"] += 1
            consecutive_failures += 1
            jslog("ERR", "poll iteration failed", error=str(e), failures=consecutive_failures)
        delay = cfg.poll_seconds * min(1 + 0.5*consecutive_failures, 4)
        elapsed = time.time()-t0
        time.sleep(max(0.0, delay - elapsed))

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: jslog("INF","shutdown requested")
