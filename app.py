import os
import re
import socket
import threading
import time
import logging
import xml.etree.ElementTree as ET
import requests
import base64
import queue
import json
import websocket
import ipaddress
from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    Response,
    session,
    redirect,
    url_for,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("UeberBoseUI")

app = Flask(__name__)

# --- SESSION CONFIGURATION ---
app.secret_key = os.getenv("FLASK_SECRET_KEY", "super-secret-ueberbose-key-change-this")

# --- CONFIGURATION ---
UEBERBOESE_API_URL = os.getenv("UEBERBOESE_API_URL", "http://10.0.0.5:8000")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "YOUR_CLIENT_ID_HERE")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "YOUR_CLIENT_SECRET_HERE")

DISCOVERED_SPEAKERS = {}
DATABASE_DEVICES_CACHE = {}  # Local memory cache to screen network scan duplicates
SOURCE_PROVIDERS_CACHE = (
    {}
)  # Dynamic mapping for sourceproviderid -> name (e.g. {"15": "SPOTIFY"})

# --- REALTIME WEBSOCKET & SSE CACHES ---
LIVE_STATES = {}  # Tracks now-playing info and volume per device ID
SSE_CLIENTS = []  # Active open browser connections
state_lock = threading.Lock()
ACTIVE_WEB_SOCKETS = (
    set()
)  # Keeps track of which device IDs already have a thread running


# --- SECURITY MIDDLEWARE ---
@app.before_request
def check_login():
    if request.endpoint in ["login", "static"]:
        return
    if "api_username" not in session:
        return redirect(url_for("login"))


# --- PARSE BOSE WEBSOCKET XML ---
def parse_speaker_websocket_xml(xml_string):
    """Parses incoming background telemetry from SoundTouch WebSocket endpoint."""
    try:
        xml_clean = xml_string.strip()
        if not xml_clean:
            return None

        root = ET.fromstring(xml_clean)

        if root.tag == "updates":
            wrapper_dev_id = root.attrib.get("deviceID")
            if len(root) > 0:
                child = root[0]
                root = child
                if wrapper_dev_id and not root.attrib.get("deviceID"):
                    root.set("deviceID", wrapper_dev_id)

        # --- 1. Volume Change (<volumeUpdated>) ---
        if root.tag == "volumeUpdated":
            volume_node = root.find(".//actualvolume")
            mute_node = root.find(".//muteenabled")
            if volume_node is not None:
                return {
                    "type": "volume",
                    "volume": int(volume_node.text),
                    "muted": (
                        mute_node.text == "true" if mute_node is not None else False
                    ),
                }

        # --- 2. Now Playing updated (<nowPlayingUpdated>) ---
        elif root.tag == "nowPlayingUpdated":
            dev_id = root.attrib.get("deviceID")
            now_playing = root.find("nowPlaying")
            if now_playing is not None:
                track = now_playing.find("track")
                artist = now_playing.find("artist")
                album = now_playing.find("album")
                play_status = now_playing.find("playStatus")

                art_url = ""
                art_node = now_playing.find("art")
                container_art_node = now_playing.find(".//containerArt")

                if art_node is not None:
                    if art_node.text and art_node.text.strip().startswith("http"):
                        art_url = art_node.text.strip()
                    elif art_node.attrib.get("url"):
                        art_url = art_node.attrib.get("url")

                if not art_url and container_art_node is not None:
                    art_url = (
                        container_art_node.text.strip()
                        if container_art_node.text
                        else ""
                    )

                return {
                    "type": "now_playing",
                    "device_id": dev_id,
                    "source": now_playing.attrib.get("source", "STANDBY"),
                    "track": (
                        track.text if track is not None else "Standby / No media"
                    ),
                    "artist": artist.text if artist is not None else "",
                    "album": album.text if album is not None else "",
                    "art_url": art_url,
                    "status": (
                        play_status.text if play_status is not None else "STANDBY"
                    ),
                }

        # --- 3. Preset / Source Selection Change (<nowSelectionUpdated>) ---
        elif root.tag == "nowSelectionUpdated":
            dev_id = root.attrib.get("deviceID")
            preset_node = root.find(".//preset")
            content_item = root.find(".//ContentItem")

            track_name = "Standby"
            source_name = "STANDBY"
            art_url = ""

            if content_item is not None:
                source_name = content_item.attrib.get("source", "STANDBY")
                item_name_node = content_item.find("itemName")
                art_node = content_item.find("containerArt")

                if item_name_node is not None:
                    track_name = item_name_node.text
                if art_node is not None:
                    art_url = art_node.text

            return {
                "type": "now_playing",
                "device_id": dev_id,
                "source": source_name,
                "track": track_name,
                "artist": (
                    f"Preset {preset_node.attrib.get('id')}"
                    if preset_node is not None
                    else ""
                ),
                "album": "",
                "art_url": art_url,
                "status": "PLAY_STATE",
            }

    except Exception as e:
        logger.error(f"Error processing the WebSocket XML: {e}")
    return None


# --- WORKER TO HOLD CONTINUOUS CONNECTION ---
def speaker_websocket_worker(ip_address, device_id):
    """Maintains a persistent long-lived connection to a specific speaker's ws."""
    ws_url = f"ws://{ip_address}:8080"
    logger.info(f"[WebSocket] Thread started for {device_id} op {ws_url}")

    while True:
        try:
            ws = websocket.create_connection(
                ws_url,
                timeout=None,
                ping_interval=20,
                ping_timeout=10,
                subprotocols=["gabbo"],
            )
            logger.info(
                f"[WebSocket] 🟢 Live connection active with speaker {device_id} ({ip_address})"
            )

            # --- SEEDING INITIAL STATE VIA HTTP API ---
            init_track = ""
            init_artist = ""
            init_status = "STANDBY"
            init_art = ""
            init_source = "STANDBY"
            init_volume = 30
            init_muted = False

            try:
                np_res = requests.get(
                    f"http://{ip_address}:8090/now_playing", timeout=3
                )
                if np_res.status_code == 200:
                    np_root = ET.fromstring(np_res.content)
                    init_source = np_root.attrib.get("source", "STANDBY")

                    track_node = np_root.find("track")
                    artist_node = np_root.find("artist")
                    play_status_node = np_root.find("playStatus")

                    art_node = np_root.find("art")
                    container_art_node = np_root.find(".//containerArt")
                    if art_node is not None:
                        if art_node.text and art_node.text.strip().startswith("http"):
                            init_art = art_node.text.strip()
                        elif art_node.attrib.get("url"):
                            init_art = art_node.attrib.get("url")
                    if not init_art and container_art_node is not None:
                        init_art = (
                            container_art_node.text.strip()
                            if container_art_node.text
                            else ""
                        )

                    if track_node is not None and track_node.text:
                        init_track = track_node.text
                    if artist_node is not None and artist_node.text:
                        init_artist = artist_node.text
                    if play_status_node is not None and play_status_node.text:
                        init_status = play_status_node.text
            except Exception as e:
                logger.warning(
                    f"[WebSocket] ⚠️ Could not retrieve initial /now_playing for {device_id}: {e}"
                )

            try:
                vol_res = requests.get(f"http://{ip_address}:8090/volume", timeout=3)
                if vol_res.status_code == 200:
                    vol_root = ET.fromstring(vol_res.content)

                    target_vol_node = vol_root.find("targetvolume")
                    actual_vol_node = vol_root.find("actualvolume")
                    mute_node = vol_root.find("muteenabled")

                    vol_node = (
                        actual_vol_node
                        if actual_vol_node is not None
                        else target_vol_node
                    )
                    if vol_node is not None and vol_node.text:
                        init_volume = int(vol_node.text)
                    if mute_node is not None and mute_node.text:
                        init_muted = mute_node.text == "true"
            except Exception as e:
                logger.warning(
                    f"[WebSocket] ⚠️ Could not retrieve initial /volume for {device_id}: {e}"
                )

            with state_lock:
                LIVE_STATES[device_id] = {
                    "ip": ip_address,
                    "track": init_track,
                    "artist": init_artist,
                    "status": init_status,
                    "source": init_source,
                    "volume": init_volume,
                    "muted": init_muted,
                    "art_url": init_art,
                }
                frozen_payload = json.dumps(LIVE_STATES)

            notify_sse_browsers(frozen_payload)

            while True:
                msg = ws.recv()
                if not msg:
                    raise websocket.WebSocketConnectionClosedException(
                        "Empty frame received from speaker."
                    )

                update = parse_speaker_websocket_xml(msg)

                if update:
                    with state_lock:
                        target_id = update.get("device_id", device_id)
                        if target_id not in LIVE_STATES:
                            LIVE_STATES[target_id] = {"ip": ip_address}

                        if update["type"] == "volume":
                            LIVE_STATES[target_id]["volume"] = update["volume"]
                            LIVE_STATES[target_id]["muted"] = update["muted"]
                        elif update["type"] == "now_playing":
                            LIVE_STATES[target_id]["track"] = update["track"]
                            LIVE_STATES[target_id]["artist"] = update["artist"]
                            LIVE_STATES[target_id]["status"] = update["status"]
                            LIVE_STATES[target_id]["art_url"] = update["art_url"]
                            LIVE_STATES[target_id]["source"] = update.get(
                                "source", "STANDBY"
                            )

                        frozen_payload = json.dumps(LIVE_STATES)

                    notify_sse_browsers(frozen_payload)

        except (websocket.WebSocketException, socket.error, Exception) as e:
            logger.warning(
                f"[WebSocket] 🔴 Connection lost to {ip_address} ({e}). Reconnect in 5s..."
            )
            time.sleep(5)


def notify_sse_browsers(frozen_payload):
    """Broadcasts a hard-frozen snapshot string across all attached SSE streams."""
    for client_queue in list(SSE_CLIENTS):
        try:
            client_queue.put_nowait(frozen_payload)
        except queue.Full:
            try:
                client_queue.get_nowait()
                client_queue.put_nowait(frozen_payload)
            except Exception:
                pass


def sync_websocket_listeners(devices_map):
    """Spawns asynchronous listener loops for any new database device."""
    for dev_id, dev in devices_map.items():
        ip = dev.get("ip")
        if ip and dev_id not in ACTIVE_WEB_SOCKETS:
            ACTIVE_WEB_SOCKETS.add(dev_id)
            t = threading.Thread(
                target=speaker_websocket_worker,
                args=(ip, dev_id),
                name=f"ws-bose-{dev_id}",
                daemon=True,
            )
            t.start()


# --- NEW: DYNAMIC AND ONE-TIME SOURCEPROVIDER LOADING ---
def fetch_source_providers():
    """Fetches the source providers from the Java API and maps id to upper case name."""
    global SOURCE_PROVIDERS_CACHE
    url = f"{UEBERBOESE_API_URL}/streaming/sourceproviders"

    # Temporarily use admin credentials if the session does not yet exist during cold startup of the main thread
    # If the endpoint is publicly accessible, auth=None is sufficient.
    auth = (
        (session.get("api_username"), session.get("api_password"))
        if "api_username" in session
        else None
    )

    try:
        response = requests.get(url, auth=auth, timeout=5)
        if response.status_code == 200:
            root = ET.fromstring(response.content)
            temp_cache = {}
            for provider in root.findall("sourceprovider"):
                p_id = provider.get("id")
                p_name = provider.find("name")
                if p_id and p_name is not None and p_name.text:
                    temp_cache[p_id] = p_name.text.upper()

            if temp_cache:
                SOURCE_PROVIDERS_CACHE = temp_cache
                logger.info(
                    f"[Inventory Engine] 🛰️ Dynamic source providers loaded successfully: {SOURCE_PROVIDERS_CACHE}"
                )
    except Exception as e:
        logger.error(
            f"[Inventory Engine] 🔴 Failed fetching source providers mapping: {e}"
        )


def fetch_global_inventory():
    """Fetches the unified XML from Java API and parses registered devices, presets and Spotify accounts"""
    global DATABASE_DEVICES_CACHE
    devices_in_db = {}
    spotify_accounts = []

    # Make sure the providers are ALWAYS loaded before parsing the devices XML!
    if not SOURCE_PROVIDERS_CACHE:
        fetch_source_providers()

    url = f"{UEBERBOESE_API_URL}/mgmt/devices"
    auth = (
        (session.get("api_username"), session.get("api_password"))
        if "api_username" in session
        else None
    )

    if not auth:
        return devices_in_db, spotify_accounts

    try:
        response = requests.get(url, auth=auth, timeout=5)
        if response.status_code == 200:
            root = ET.fromstring(response.content)

            devices_node = root.find("devices")
            if devices_node is not None:
                for device in devices_node.findall("device"):
                    dev_id = device.get("deviceid")
                    if dev_id:
                        attached_product = device.find("attachedProduct")
                        product_code = (
                            attached_product.get("product_code")
                            if attached_product is not None
                            else "SoundTouch"
                        )

                        device_presets = []
                        presets_node = device.find("presets")
                        if presets_node is not None:
                            for preset in presets_node.findall("preset"):
                                p_num = preset.get("buttonNumber")
                                p_name = (
                                    preset.find("name").text
                                    if preset.find("name") is not None
                                    else f"Preset {p_num}"
                                )
                                p_art = (
                                    preset.find("containerArt").text
                                    if preset.find("containerArt") is not None
                                    else ""
                                )

                                # DYNAMIC SOURCE RESOLVING FOR PRESETS
                                content_item = preset.find(".//ContentItem")
                                raw_source = preset.get("source") or (
                                    content_item.attrib.get("source")
                                    if content_item is not None
                                    else "TUNEIN"
                                )

                                # Check whether a sourceproviderid is included in the preset XML logic
                                provider_id_node = preset.find(".//sourceproviderid")
                                p_src = raw_source
                                if (
                                    provider_id_node is not None
                                    and provider_id_node.text in SOURCE_PROVIDERS_CACHE
                                ):
                                    p_src = SOURCE_PROVIDERS_CACHE[
                                        provider_id_node.text
                                    ]

                                if p_num:
                                    device_presets.append(
                                        {
                                            "number": p_num,
                                            "name": p_name,
                                            "containerArt": p_art,
                                            "source": p_src,
                                        }
                                    )

                        device_presets.sort(key=lambda x: int(x["number"]))

                        devices_in_db[dev_id] = {
                            "id": dev_id,
                            "name": (
                                device.find("name").text
                                if device.find("name") is not None
                                else "Unknown Bose"
                            ),
                            "ip": (
                                device.find("ipaddress").text
                                if device.find("ipaddress") is not None
                                else ""
                            ),
                            "model": product_code,
                            "serial": (
                                device.find("serialNumber").text
                                if device.find("serialNumber") is not None
                                else ""
                            ),
                            "presets": device_presets,
                        }

            DATABASE_DEVICES_CACHE = devices_in_db
            sync_websocket_listeners(devices_in_db)

            # DYNAMIC LOOKUP FOR SPOTIFY / OPERATIONAL SOURCES
            # We search for the provider_id that matches "SPOTIFY" in our dynamic map
            spotify_provider_id = next(
                (k for k, v in SOURCE_PROVIDERS_CACHE.items() if v == "SPOTIFY"), "15"
            )

            sources_node = root.find("sources")
            if sources_node is not None:
                for source in sources_node.findall("source"):
                    provider_id = source.find("sourceproviderid")

                    # MATCH BASED ON DYNAMIC CACHE INSTEAD OF HARDCODED "15"
                    if (
                        provider_id is not None
                        and provider_id.text == spotify_provider_id
                    ):
                        username = (
                            source.find("username").text
                            if source.find("username") is not None
                            else None
                        )
                        sourcename = (
                            source.find("sourcename").text
                            if source.find("sourcename") is not None
                            else username
                        )
                        if username:
                            spotify_accounts.append(
                                {"spotifyUserId": username, "displayName": sourcename}
                            )

    except Exception as e:
        logger.error(f"Error reading global inventory XML from backend: {e}")

    return devices_in_db, spotify_accounts


def fetch_speaker_details(xml_url):
    try:
        response = requests.get(xml_url, timeout=3)
        if response.status_code == 200:
            root = ET.fromstring(response.content)
            namespaces = {"ns": "urn:schemas-upnp-org:device-1-0"}
            device = root.find(".//ns:device", namespaces)
            if device is not None:
                friendly_name = device.find("ns:friendlyName", namespaces)
                model_name = device.find("ns:modelName", namespaces)

                dev_id = None
                serial = device.find("ns:serialNumber", namespaces)
                if serial is not None:
                    dev_id = serial.text

                return {
                    "id": dev_id,
                    "name": (
                        friendly_name.text
                        if friendly_name is not None
                        else "Unknown Bose"
                    ),
                    "model": (
                        model_name.text if model_name is not None else "SoundTouch"
                    ),
                }
    except Exception as e:
        logger.error(f"Failed to fetch XML descriptor at {xml_url}: {e}")
    return {"id": None, "name": "Unknown Bose Speaker", "model": "SoundTouch"}


def trigger_upnp_scan():
    DISCOVER_MESSAGE_ROOTDEVICE = (
        "M-SEARCH * HTTP/1.1\r\n"
        "ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
        "MX: 3\r\n"
        'MAN: "ssdp:discover"\r\n'
        "HOST: 239.255.255.250:1900\r\n\r\n"
    )
    multicast_group = "239.255.255.250"
    port = 1900

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.settimeout(4.0)
        sock.sendto(
            DISCOVER_MESSAGE_ROOTDEVICE.encode("utf-8"), (multicast_group, port)
        )
        while True:
            try:
                data, addr = sock.recvfrom(2048)
                response_str = data.decode("utf-8", errors="ignore")
                lines = response_str.split("\r\n")
                if lines and lines[0].startswith("HTTP/1.1 200 OK"):
                    location = None
                    for line in lines:
                        if line.lower().startswith("location:"):
                            location = line[9:].strip()
                            break
                    if location:
                        speaker_ip = addr[0]
                        if speaker_ip not in DISCOVERED_SPEAKERS:
                            details = fetch_speaker_details(location)
                            if "soundtouch" in details["model"].lower():
                                logger.info(
                                    f"✨ Found SoundTouch Speaker: '{details['name']}'"
                                )
                                DISCOVERED_SPEAKERS[speaker_ip] = {
                                    "id": details.get("id"),
                                    "ip": speaker_ip,
                                    "name": details["name"],
                                    "model": details["model"],
                                    "location": location,
                                    "last_seen": time.time(),
                                }
                        else:
                            DISCOVERED_SPEAKERS[speaker_ip]["last_seen"] = time.time()
            except socket.timeout:
                break
        sock.close()
    except Exception as e:
        logger.error(f"On-demand UPnP scan error: {e}")


def background_discovery_loop():
    while True:
        trigger_upnp_scan()
        time.sleep(60)


# --- LOGIN & LOGOUT ROUTES ---
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        url = f"{UEBERBOESE_API_URL}/mgmt/spotify/accounts"
        try:
            response = requests.get(url, auth=(username, password), timeout=5)
            if response.status_code == 200:
                session["api_username"] = username
                session["api_password"] = password
                logger.info(f"User '{username}' logged in successfully.")

                # As soon as the user logs in and we have API credentials, refresh the dynamic provider cache immediately
                fetch_source_providers()

                return redirect(url_for("index"))
            else:
                error = "Invalid API credentials (Ueberboese-API rejected access)."
        except Exception as e:
            error = f"Could not connect to Ueberboese-API: {e}"

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- WEB UI ROUTE ---
@app.route("/")
def index():
    return render_template("index.html", username=session.get("api_username"))


# --- INVENTORY DATA PROXY (DB Devices & Spotify Profiles) ---
@app.route("/api/inventory", methods=["GET"])
def get_inventory():
    db_devices, spotify_profiles = fetch_global_inventory()
    return jsonify(
        {
            "databaseDevices": list(db_devices.values()),
            "spotifyAccounts": spotify_profiles,
        }
    )


# --- SNAPPY LIVE SPEAKERS ENDPOINT ---
@app.route("/api/speakers", methods=["GET"])
def get_speakers():
    unregistered_scanned_speakers = {}
    for ip, data in DISCOVERED_SPEAKERS.items():
        is_already_in_db = any(
            dev["ip"] == ip
            or (data.get("id") and dev["id"].lower() == data["id"].lower())
            for dev in DATABASE_DEVICES_CACHE.values()
        )
        if not is_already_in_db:
            unregistered_scanned_speakers[ip] = data

    return jsonify(list(unregistered_scanned_speakers.values()))


@app.route("/api/speakers/scan", methods=["POST"])
def run_manual_scan():
    threading.Thread(target=trigger_upnp_scan).start()
    return jsonify({"status": "scan_triggered"}), 200


@app.route("/api/speaker/volume", methods=["POST"])
def set_speaker_volume_level():
    """Sets the absolute volume level of a speaker using raw XML over HTTP."""
    data = request.json
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid JSON body"}), 400

    ip = data.get("ip")
    level = data.get("level")

    if not ip or level is None:
        return jsonify({"error": "Missing 'ip' or 'level'"}), 400

    try:
        validated_ip = str(ipaddress.ip_address(str(ip).strip()))

        allowed_ips = set(DISCOVERED_SPEAKERS.keys())
        allowed_ips.update(
            str(dev.get("ip")).strip()
            for dev in DATABASE_DEVICES_CACHE.values()
            if dev.get("ip")
        )
        if validated_ip not in allowed_ips:
            return jsonify({"error": "Unknown or unauthorized speaker IP"}), 403

        level = max(0, min(100, int(level)))
        xml_payload = f"<volume>{level}</volume>"

        url = f"http://{validated_ip}:8090/volume"
        response = requests.post(
            url,
            data=xml_payload,
            headers={"Content-Type": "application/xml"},
            timeout=3,
        )

        if response.status_code == 200:
            return jsonify({"status": "success", "level": level})
        else:
            return (
                jsonify(
                    {"error": f"Speaker responded with status {response.status_code}"}
                ),
                500,
            )

    except Exception as e:
        logger.error(f"[Volume API] Error setting volume for {ip}: {e}")
        return jsonify({"error": str(e)}), 500


# --- LIVE SPEAKER SOUNDTOUCH KEYPRESS TRIGGER ENDPOINT ---
@app.route("/api/key", methods=["POST"])
def trigger_speaker_key():
    req_data = request.get_json() or {}
    speaker_ip = req_data.get("ip")
    key_value = req_data.get("key")
    state = req_data.get("state", "cycle")

    if not speaker_ip or not key_value:
        return jsonify({"error": "Missing 'ip' or 'key' parameter"}), 400

    target_url = f"http://{speaker_ip}:8090/key"

    def send_xml(current_state):
        xml_payload = f'<key state="{current_state}" sender="Gabbo">{key_value}</key>'
        return requests.post(
            target_url,
            data=xml_payload,
            headers={"Content-Type": "application/xml"},
            timeout=3,
        )

    try:
        if state in ["press", "release"]:
            res = send_xml(state)
            status_code = res.status_code
        else:
            res_press = send_xml("press")
            time.sleep(0.05)
            res_release = send_xml("release")
            status_code = res_release.status_code

        if status_code == 200:
            return jsonify({"status": "success"}), 200
        return (
            jsonify({"error": f"Speaker hardware returned status {status_code}"}),
            502,
        )
    except Exception as e:
        logger.error(
            f"Failed communicating remote command to hardware endpoint {speaker_ip}: {e}"
        )
        return jsonify({"error": f"Hardware unreachable: {e}"}), 504


# --- LIVE STATE REALTIME SERVER-SENT EVENTS CHANNEL ---
@app.route("/api/stream")
def api_live_stream_channel():
    """Persistent server-to-client loop pipe pushing dictionary frames dynamically."""

    def event_stream():
        q = queue.Queue(maxsize=15)
        SSE_CLIENTS.append(q)

        with state_lock:
            initial_state = json.dumps(LIVE_STATES)

        logger.info("[SSE Pipeline] 🔌 New browser window attached to stream channel.")
        yield f"data: {initial_state}\n\n"

        try:
            while True:
                data = q.get(block=True)
                yield f"data: {data}\n\n"
        except GeneratorExit:
            logger.info("[SSE Pipeline] ❌ Browser window closed or reloaded.")
            if q in SSE_CLIENTS:
                SSE_CLIENTS.remove(q)

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# --- PROXY SPOTIFY MANAGEMENT ENDPOINTS ---
@app.route("/api/spotify/init", methods=["POST"])
def proxy_spotify_init():
    url = f"{UEBERBOESE_API_URL}/mgmt/spotify/init"
    auth = (session.get("api_username"), session.get("api_password"))
    try:
        response = requests.post(url, json={}, auth=auth, timeout=10)
        return (
            response.text,
            response.status_code,
            {"Content-Type": "application/json"},
        )
    except Exception as e:
        return jsonify({"error": f"Failed initializing auth: {e}"}), 500


@app.route("/api/spotify/confirm", methods=["POST"])
def proxy_spotify_confirm():
    code = request.args.get("code")
    if not code:
        return jsonify({"error": "Missing query parameter 'code'"}), 400
    url = f"{UEBERBOESE_API_URL}/mgmt/spotify/confirm?code={code}"
    auth = (session.get("api_username"), session.get("api_password"))
    try:
        response = requests.post(url, auth=auth, timeout=10)
        return (
            response.text,
            response.status_code,
            {"Content-Type": "application/json"},
        )
    except Exception as e:
        return jsonify({"error": f"Failed confirming auth: {e}"}), 500


# --- SPOTIFY CLIENT CREDENTIALS TOKEN GENERATOR ---
def get_spotify_client_token():
    url = "https://accounts.spotify.com/api/token"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "client_credentials",
        "client_id": SPOTIFY_CLIENT_ID,
        "client_secret": SPOTIFY_CLIENT_SECRET,
    }
    try:
        response = requests.post(url, headers=headers, data=data, timeout=5)
        if response.status_code == 200:
            return response.json().get("access_token")
    except Exception as e:
        logger.error(f"Failed to get Spotify client token: {e}")
    return None


@app.route("/api/spotify/avatar/<user_id>", methods=["GET"])
def get_spotify_avatar(user_id):
    token = get_spotify_client_token()
    fallback_url = (
        "https://www.gravatar.com/avatar/00000000000000000000000000000000?d=mp&f=y"
    )

    if not token:
        logger.warning(
            f"[Spotify Avatar] Could not retrieve client token. Fallback applied for {user_id}"
        )
        return redirect(fallback_url)

    url = f"https://api.spotify.com/v1/users/{user_id}"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            profile_data = response.json()
            images = profile_data.get("images", [])

            if images and len(images) > 0:
                avatar_url = images[0].get("url")
                if avatar_url:
                    logger.info(
                        f"[Spotify Avatar] 🟢 Successful match for {user_id} -> Redirecting to image resource."
                    )
                    return redirect(avatar_url)

        logger.info(
            f"[Spotify Avatar] No profile picture found for {user_id}. Fallback applied."
        )
        return redirect(fallback_url)

    except Exception as e:
        logger.error(
            f"[Spotify Avatar] 🔴 Error while retrieving profile for {user_id}: {e}"
        )
        return redirect(fallback_url)


# --- SPEAKER DOCTOR POORT 17000 SOCKET COMMAND HANDLER ---
def send_bose_socket_command(ip, command, wait_for_response=True):
    """Opens a raw TCP stream to port 17000, sends a line command, and reads the response."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(4.0)
        s.connect((ip, 17000))

        # Send the command with a newline
        s.sendall(f"{command}\n".encode("utf-8"))

        if not wait_for_response:
            s.close()
            return ""

        # Read the buffer (briefly wait for chunks until a data pause occurs)
        response_buffer = ""
        time.sleep(0.3)  # Short startup delay similar to envswitchDelay

        while True:
            try:
                data = s.recv(4096)
                if not data:
                    break
                response_buffer += data.decode("utf-8", errors="ignore")
                # If fewer than 4096 bytes are received, the stream is usually complete
                if len(data) < 4096:
                    break
            except socket.timeout:
                break

        s.close()
        return response_buffer
    except Exception as e:
        logger.error(f"[Doctor Socket] Error communicating with {ip}:17000 -> {e}")
        raise e


@app.route("/api/doctor/configuration", methods=["POST"])
def api_doctor_get_config():
    """Fetches and parses the raw getpdo CurrentSystemConfiguration console tree."""
    data = request.json or {}
    ip = data.get("ip")
    if not ip:
        return jsonify({"error": "Missing 'ip' parameter"}), 400

    try:
        raw_response = send_bose_socket_command(ip, "getpdo CurrentSystemConfiguration")

        # PYTHON REGEX PARSER (Corrected to native Python string functions)
        parsed_config = {}
        block_pattern = re.compile(
            r"(\w+)\s*\{\s*\n\s*text:\s*(.*?)\s*\n\s*\}", re.MULTILINE
        )

        for match in block_pattern.finditer(raw_response):
            key = match.group(1)
            value = match.group(2).strip()

            # FIX: endswith uses a lowercase 'w' in Python!
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            parsed_config[key] = value

        return jsonify({"raw": raw_response, "parsed": parsed_config}), 200
    except Exception as e:
        return (
            jsonify({"error": f"Could not retrieve configuration via port 17000: {e}"}),
            500,
        )


@app.route("/api/doctor/reboot", methods=["POST"])
def api_doctor_reboot():
    """Forces the speaker hardware to run a system hard reboot."""
    data = request.json or {}
    ip = data.get("ip")
    if not ip:
        return jsonify({"error": "Missing 'ip' parameter"}), 400

    try:
        # 'sys reboot' does not require a response, the device shuts down immediately
        send_bose_socket_command(ip, "sys reboot", wait_for_response=False)
        return jsonify({"status": "reboot_triggered"}), 200
    except Exception as e:
        return jsonify({"error": f"Reboot command rejected: {e}"}), 500


@app.route("/api/doctor/set-account", methods=["POST"])
def api_doctor_set_account():
    """Pairs the device with a production marge account using raw XML."""
    data = request.json or {}
    ip = data.get("ip")
    account_id = data.get("accountId")

    if not ip or not account_id:
        return (
            jsonify({"error": "Missing parameters. Required: ip, accountId"}),
            400,
        )

    xml_body = (
        f"<PairDeviceWithAccount>"
        f"<accountId>{account_id}</accountId>"
        f"<userAuthToken>not_used</userAuthToken>"
        f"</PairDeviceWithAccount>"
    )

    try:
        url = f"http://{ip}:8090/setMargeAccount"
        res = requests.post(
            url, data=xml_body, headers={"Content-Type": "application/xml"}, timeout=15
        )

        if res.statusCode == 200:
            return jsonify({"status": "success"}), 200
        return jsonify({"error": f"Speaker API returned HTTP {res.status_code}"}), 502
    except Exception as e:
        return jsonify({"error": f"HTTP post to setMargeAccount failed: {e}"}), 500


# --- TUNEIN/RADIOTIME SEARCH & DETAILS PROXY ENDPOINTS ---
@app.route("/api/presets/tunein/search", methods=["GET"])
def api_presets_tunein_search():
    """Proxies and parses the RadioTime/TuneIn OPML search catalog into clean JSON."""
    query = request.args.get("query", "").strip()
    if not query:
        return jsonify([])

    url = f"https://opml.radiotime.com/search.ashx?query={requests.utils.quote(query)}"
    try:
        res = requests.get(url, headers={"Accept": "text/xml"}, timeout=8)
        if res.status_code != 200:
            return (
                jsonify({"error": f"RadioTime API returned HTTP {res.status_code}"}),
                502,
            )

        root = ET.fromstring(res.content)
        stations = []

        # Search all <outline> elements similar to the Dart logic
        for outline in root.findall(".//outline"):
            o_type = outline.attrib.get("type")
            o_item = outline.attrib.get("item")

            # Filter audio stations
            if o_type == "audio" and o_item == "station":
                stations.append(
                    {
                        "stationId": outline.attrib.get("guide_id", ""),
                        "name": outline.attrib.get("text", "Unknown Station"),
                        "logo": outline.attrib.get("image", ""),
                        "currentTrack": outline.attrib.get("current_track", ""),
                        "tuneUrl": outline.attrib.get("URL", ""),
                    }
                )

        return jsonify(stations), 200
    except Exception as e:
        logger.error(f"[TuneIn API] Search failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/presets/assign", methods=["POST"])
def api_presets_assign_slot():
    """Builds the source-specific XML payload and stores the preset directly on the Bose hardware."""
    data = request.json or {}

    # We now need the physical IP of the speaker instead of a database ID
    speaker_ip = data.get("ip")
    preset_number = data.get("presetNumber")  # 1-6
    source = data.get("source", "").upper()  # TUNEIN of SPOTIFY
    content_id = data.get("contentId")  # s2398 (TuneIn) of Spotify URI
    name = data.get("name")  # Naam van de zender / playlist
    logo = data.get("logo", "")
    spotify_user_id = data.get("spotifyUserId", "")  # Alleen nodig bij Spotify

    if not speaker_ip or not preset_number or not source or not content_id or not name:
        return jsonify({"error": "Missing required hardware parameters"}), 400

    # Generate current timestamp in seconds
    timestamp = int(time.time())

    # --- ENTIRE LOGIC PARSING PER SOURCE TYPE ---
    if source == "SPOTIFY":
        # Perform base64 encoding of the Spotify URI for the playback container location
        encoded_uri = base64.b64encode(content_id.encode("utf-8")).decode("utf-8")
        location = f"/playback/container/{encoded_uri}"

        # Add sourceAccount for Spotify authentication mapping
        content_item_attrs = f'source="SPOTIFY" type="tracklisturl" location="{location}" sourceAccount="{spotify_user_id}" isPresetable="true"'

    elif source == "TUNEIN":
        location = f"/v1/playback/station/{content_id}"
        content_item_attrs = f'source="TUNEIN" type="stationurl" location="{location}" isPresetable="true"'

    else:
        return jsonify({"error": f"Unsupported dynamic source provider: {source}"}), 400

    # Build the optional containerArt element
    art_element = f"<containerArt>{logo}</containerArt>" if logo else ""

    # Generate the exact XML payload required by the SoundTouch hardware
    xml_body = (
        f'<preset id="{preset_number}" createdOn="{timestamp}" updatedOn="{timestamp}">'
        f"<ContentItem {content_item_attrs}>"
        f"<itemName>{name}</itemName>{art_element}"
        f"</ContentItem>"
        f"</preset>"
    )

    try:
        url = f"http://{speaker_ip}:8090/storePreset"
        headers = {"Content-Type": "text/xml"}

        # Write the preset directly to the physical SoundTouch node
        res = requests.post(url, data=xml_body, headers=headers, timeout=10)

        if res.status_code == 200:
            logger.info(
                f"[Preset Hardware Engine] 🟢 Preset {preset_number} successfully stored on {speaker_ip} for '{name}'"
            )
            return jsonify({"status": "success", "presetNumber": preset_number}), 200

        return (
            jsonify(
                {
                    "error": f"Speaker hardware rejected preset configuration: HTTP {res.status_code}"
                }
            ),
            502,
        )

    except Exception as e:
        logger.error(
            f"[Preset Hardware Engine] 🔴 Error while writing to speaker {speaker_ip}: {e}"
        )
        return jsonify({"error": str(e)}), 500


@app.route("/api/spotify/metadata", methods=["GET"])
def api_spotify_get_metadata():
    """Fetches real-time name, owner, and cover art from Spotify catalog using client token."""
    m_type = request.args.get("type")  # playlist, track, album
    m_id = request.args.get("id")

    if not m_type or not m_id:
        return jsonify({"error": "Missing type or id"}), 400

    token = get_spotify_client_token()
    if not token:
        return jsonify({"error": "Could not fetch Spotify application token"}), 500

    # Build the official Spotify catalog URL
    url = f"https://api.spotify.com/v1/{m_type}s/{m_id}"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()

            # Format the response into a uniform structure for the frontend preview card
            name = data.get("name", "Unknown")

            # Retrieve the owner or artist
            owner_name = ""
            if "owner" in data:
                owner_name = data["owner"].get("display_name", "")
            elif "artists" in data and len(data["artists"]) > 0:
                owner_name = data["artists"][0].get("name", "")

            # Retrieve the album or playlist artwork image
            img_url = ""
            images = data.get("images", [])
            if "album" in data and "images" in data["album"]:
                images = data["album"]["images"]

            if images and len(images) > 0:
                img_url = images[0].get("url", "")

            return jsonify({"name": name, "owner": owner_name, "image": img_url}), 200

        return (
            jsonify({"error": f"Spotify responded with status {res.status_code}"}),
            502,
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    discovery_thread = threading.Thread(target=background_discovery_loop, daemon=True)
    discovery_thread.start()

    web_port = int(os.getenv("UEBERBOESE_WEB_PORT", 7082))

    logger.info(f"🚀 Starting Überböse Web App on port {web_port}")
    app.run(host="0.0.0.0", port=web_port, debug=False)
