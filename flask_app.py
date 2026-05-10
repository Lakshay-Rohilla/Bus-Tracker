from flask import Flask, render_template, jsonify, request
import datetime, os, math, requests, time, threading, json, sqlite3, hashlib, hmac, queue
import logging
import sys
from threading import Lock
from collections import defaultdict
import gzip
from contextlib import contextmanager
from markupsafe import escape
import pytz
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ========== CONFIGURATION (Environment Variables) ==========
BASE_DIR = os.path.dirname(os.path.realpath(__file__))
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, 'templates'))

# Environment Variables for configuration
ADMIN_SECRET = os.environ.get('ADMIN_KEY', 'Admin@Laksh_Rohilla_7')
MAX_VIEWERS = int(os.environ.get('MAX_VIEWERS', '1000'))
MAX_LOGGERS = int(os.environ.get('MAX_LOGGERS', '100'))
SESSION_TIMEOUT = int(os.environ.get('SESSION_TIMEOUT', '90'))
RATE_LIMIT = int(os.environ.get('RATE_LIMIT', '15'))
RATE_WINDOW = int(os.environ.get('RATE_WINDOW', '1'))
REQUEST_TIMEOUT = int(os.environ.get('REQUEST_TIMEOUT', '5'))
MIN_UPDATE_INTERVAL = 0.5  # Minimum seconds between GPS updates
NOMINATIM_DELAY = 2.0  # Seconds between Nominatim requests

ACCESS_LOG = os.path.join(BASE_DIR, 'access_log.txt')
DB_FILE = os.path.join(BASE_DIR, 'bus_tracker.db')
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
data_lock = Lock()
db_queue = queue.Queue(maxsize=5000)  # Increased from 1000

# Server start time for uptime tracking
START_TIME = time.time()

# Nominatim rate limiting
last_nominatim_request = 0
nominatim_queue = queue.Queue(maxsize=100)

# Create session with retry strategy for Nominatim
session = requests.Session()
retry_strategy = Retry(
    total=2,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
)
adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=20)
session.mount("https://", adapter)
session.mount("http://", adapter)

# ========== DATABASE CONNECTION POOL ==========
class DatabasePool:
    def __init__(self, db_file, max_connections=5):
        self.db_file = db_file
        self.connections = []
        self.lock = Lock()
        self.max_connections = max_connections
    
    @contextmanager
    def get_connection(self):
        with self.lock:
            if self.connections:
                conn = self.connections.pop()
            else:
                conn = sqlite3.connect(self.db_file, check_same_thread=False)
        try:
            yield conn
        except Exception:
            if conn:
                conn.close()
            raise
        finally:
            with self.lock:
                if len(self.connections) < self.max_connections:
                    self.connections.append(conn)
                else:
                    conn.close()

db_pool = DatabasePool(DB_FILE)

# ========== IST TIMEZONE LOGGER ==========
class ISTFormatter(logging.Formatter):
    def converter(self, timestamp):
        dt = datetime.datetime.fromtimestamp(timestamp)
        ist = pytz.timezone('Asia/Kolkata')
        return dt.replace(tzinfo=pytz.UTC).astimezone(ist)
    
    def formatTime(self, record, datefmt=None):
        dt = self.converter(record.created)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime('%Y-%m-%d %H:%M:%S')

log_formatter = ISTFormatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
log_handler = logging.StreamHandler(sys.stderr)
log_handler.setFormatter(log_formatter)
app_logger = logging.getLogger('sgt_bus_tracker')
app_logger.setLevel(logging.INFO)
app_logger.addHandler(log_handler)

active_loggers = {}
active_viewers = {} 
geo_cache = {}

# FIX 3: Changed default driver name and phone to actual values
bus_data = {
    "lat": None, "lon": None, "status": "STOPPED", "speed_kmh": 0,
    "time": None, "driver_name": "Ankit", "driver_phone": "9813483418",
    "last_ping_time": None, "last_update_time": None
}

# Rate limiting
rate_limits = defaultdict(list)

def is_rate_limited(ip):
    now = time.time()
    with data_lock:
        if len(rate_limits) > 1000:
            empty_keys = [k for k, v in rate_limits.items() if not [t for t in v if now - t < RATE_WINDOW]]
            for k in empty_keys:
                rate_limits.pop(k, None)
        
        if len(rate_limits) > 2000:
            rate_limits.clear()
            
        rate_limits[ip] = [t for t in rate_limits[ip] if now - t < RATE_WINDOW]
        rate_limits[ip].append(now)
        return len(rate_limits[ip]) > RATE_LIMIT

def compact_db_queue():
    """Remove old pending updates if queue gets too full"""
    try:
        while db_queue.qsize() > 4500:
            db_queue.get_nowait()
        if db_queue.qsize() > 4000:
            app_logger.warning(f'DB Queue size {db_queue.qsize()}, compacting...')
    except queue.Empty:
        pass

def db_worker():
    last_compact = time.time()
    while True:
        try:
            with db_pool.get_connection() as conn:
                conn.execute("PRAGMA journal_mode=WAL")  # Better concurrency
                while True:
                    try:
                        task = db_queue.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    
                    if task is None:
                        return
                    try:
                        c = conn.cursor()
                        query, params = task
                        c.execute(query, params)
                        conn.commit()
                    except Exception as e:
                        app_logger.error(f'DB Batch Write Error: {e}')
                    finally:
                        db_queue.task_done()
                    
                    if time.time() - last_compact > 60:
                        compact_db_queue()
                        last_compact = time.time()
        except Exception as e:
            app_logger.error(f'DB Connection Lost: {e}. Reconnecting in 5s...')
            time.sleep(5)

_writer = threading.Thread(target=db_worker, daemon=True)
_writer.start()

def init_db():
    with db_pool.get_connection() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS bus_state 
                     (id INTEGER PRIMARY KEY, state_json TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS geo_cache
                     (cache_key TEXT PRIMARY KEY, address TEXT)''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_geo_cache ON geo_cache(cache_key)')
        
        c.execute('SELECT count(*) FROM bus_state')
        if c.fetchone()[0] == 0:
            c.execute('INSERT INTO bus_state (id, state_json) VALUES (1, ?)', (json.dumps(bus_data),))
        
        conn.commit()

def save_bus_state():
    state_json = json.dumps(bus_data)
    try:
        db_queue.put(('UPDATE bus_state SET state_json = ? WHERE id = 1', (state_json,)), block=True, timeout=2)
    except queue.Full:
        app_logger.warning('DB Queue Full - trying to compact')
        compact_db_queue()
        try:
            db_queue.put(('UPDATE bus_state SET state_json = ? WHERE id = 1', (state_json,)), block=True, timeout=2)
        except queue.Full:
            app_logger.error('DB Queue Full - dropping update')

def load_bus_state():
    if os.path.exists(DB_FILE):
        try:
            with db_pool.get_connection() as conn:
                c = conn.cursor()
                c.execute('SELECT state_json FROM bus_state WHERE id = 1')
                row = c.fetchone()
                if row:
                    loaded = json.loads(row[0])
                    # Ensure driver fields are not overwritten by old/incomplete data
                    if 'driver_name' in loaded and loaded['driver_name']:
                        bus_data['driver_name'] = loaded['driver_name']
                    if 'driver_phone' in loaded and loaded['driver_phone']:
                        bus_data['driver_phone'] = loaded['driver_phone']
                    # Update other fields
                    for k, v in loaded.items():
                        if k not in ['driver_name', 'driver_phone']:
                            bus_data[k] = v
        except:
            pass

def load_geo_cache():
    if os.path.exists(DB_FILE):
        try:
            with db_pool.get_connection() as conn:
                c = conn.cursor()
                c.execute('SELECT cache_key, address FROM geo_cache ORDER BY rowid DESC LIMIT 250')
                rows = c.fetchall()
                for k, v in reversed(rows):
                    geo_cache[k] = v
        except:
            pass

init_db()
load_bus_state()
load_geo_cache()

_last_area_lat = None
_last_area_lon = None
_last_area_name = "Unknown Area"

def get_real_ip():
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr or 'Unknown'

def get_ip():
    ip = get_real_ip()
    return hashlib.sha256(ip.encode()).hexdigest()[:16]

def get_area_worker():
    global _last_area_lat, _last_area_lon, _last_area_name, last_nominatim_request
    while True:
        try:
            try:
                req = nominatim_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            
            if req is None:
                return
            lat, lon, cache_key = req
            
            # FIX 1: Move sleep outside the lock to prevent freezing
            now = time.time()
            with data_lock:
                elapsed = now - last_nominatim_request
            if elapsed < NOMINATIM_DELAY:
                time.sleep(NOMINATIM_DELAY - elapsed)
            
            with data_lock:
                last_nominatim_request = time.time()
            
            url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lon}&zoom=18"
            resp = session.get(url, headers={'User-Agent': 'SGT_Bus_Tracker'}, timeout=REQUEST_TIMEOUT)
            
            if resp.status_code == 200:
                data = resp.json()
                if 'error' in data:
                    continue
                addr = data.get('display_name', 'Unknown')
                parts = addr.split(',')
                new_area_name = f"{parts[0].strip()}, {parts[1].strip()}" if len(parts) > 1 else parts[0]
                
                with data_lock:
                    _last_area_lat, _last_area_lon = lat, lon
                    _last_area_name = new_area_name
                    geo_cache[cache_key] = _last_area_name
                    if len(geo_cache) > 250:
                        keys_to_del = list(geo_cache.keys())[:125]
                        for k in keys_to_del:
                            del geo_cache[k]
                            
                try:
                    db_queue.put(('INSERT OR REPLACE INTO geo_cache (cache_key, address) VALUES (?, ?)', (cache_key, new_area_name)), block=False)
                except queue.Full:
                    pass
        except Exception as e:
            app_logger.error(f"Area fetch error: {e}")
        finally:
            nominatim_queue.task_done()

_area_thread = threading.Thread(target=get_area_worker, daemon=True)
_area_thread.start()

def get_area_name(lat, lon):
    global _last_area_lat, _last_area_lon, _last_area_name
    
    with data_lock:
        cache_key = f"{round(lat, 4)},{round(lon, 4)}"
        if cache_key in geo_cache:
            return geo_cache[cache_key]

        if _last_area_lat is not None:
            moved = calculate_distance(_last_area_lat, _last_area_lon, lat, lon)
            if moved < 50:
                return _last_area_name
                
    try:
        nominatim_queue.put_nowait((lat, lon, cache_key))
    except queue.Full:
        pass
    
    with data_lock:
        return _last_area_name

def write_log(msg):
    app_logger.info(msg)

def calculate_distance(lat1, lon1, lat2, lon2):
    if lat1 == lat2 and lon1 == lon2:
        return 0
    
    R = 6371000 
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1-a))

# ========== GZIP COMPRESSION ==========
@app.after_request
def gzip_compress(response):
    compress_mimetypes = ['text/html', 'text/css', 'text/javascript', 'application/json', 'text/plain']
    if response.mimetype in compress_mimetypes and len(response.get_data()) > 1024:
        try:
            response.direct_passthrough = False
            compressed = gzip.compress(response.get_data(), compresslevel=6)
            response.set_data(compressed)
            response.headers['Content-Encoding'] = 'gzip'
            response.headers['Content-Length'] = len(compressed)
            response.headers['Vary'] = 'Accept-Encoding'
        except Exception as e:
            app_logger.error(f"Gzip compression error: {e}")
    return response

# ========== CACHE CONTROL HEADERS ==========
@app.after_request
def add_cache_headers(response):
    if request.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'public, max-age=31536000'
    elif request.path == '/':
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    elif request.path == '/get_bus':
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response

@app.route('/')
def index():
    user_agent = request.headers.get('User-Agent', '')
    mobile_agents = ['android', 'webos', 'iphone', 'ipad', 'ipod', 'blackberry', 'windows phone']
    is_mobile = any(agent in user_agent.lower() for agent in mobile_agents)
    
    if request.args.get('mobile') == 'true':
        is_mobile = True
    
    admin_key_from_url = request.args.get('admin', '')
    admin_key_escaped = escape(admin_key_from_url)
        
    return render_template('index.html', is_mobile=is_mobile, admin_key_from_url=admin_key_escaped)

@app.route('/log', methods=['GET', 'POST'])
def log_gps():
    key = request.args.get('key', '')
    ip = get_ip()
    
    if is_rate_limited(ip):
        return "Rate Limited", 429
    
    net_type = request.args.get('net', 'Unknown')
    net_str = request.args.get('str', 'Unknown')
    
    VALID_KEYS = {"Driver@SGT_43", ADMIN_SECRET}
    if key not in VALID_KEYS: 
        return "Unauthorized", 401
        
    lat, lon, speed, acc = request.args.get('lat'), request.args.get('lon'), request.args.get('speed', '0'), request.args.get('acc', '100')
    
    if not lat or lat == '%LAT' or lat.strip() == '':
        return "Invalid GPS", 400
    if not lon or lon == '%LON' or lon.strip() == '':
        return "Invalid GPS", 400
        
    try:
        curr_lat, curr_lon = float(lat), float(lon)
        if not (-90 <= curr_lat <= 90) or not (-180 <= curr_lon <= 180):
            return "Invalid GPS", 400
        accuracy = float(acc) if acc != '%ACC' else 50
        raw_speed_kmh = float(speed) * 3.6 if speed != '%SPD' else 0.0
        now = datetime.datetime.now(IST)
        # FIX: Accuracy threshold increased to 100 metres
        if accuracy > 100: 
            return "Low Accuracy", 200
        
        with data_lock:
            # Rate limit updates to prevent flooding
            last_update = bus_data.get("last_update_time")
            if last_update:
                last_update_dt = datetime.datetime.fromisoformat(last_update)
                if (now - last_update_dt).total_seconds() < MIN_UPDATE_INTERVAL:
                    return "Throttled", 200
            
            if bus_data["time"]:
                try:
                    last_time = datetime.datetime.fromisoformat(bus_data["time"].replace('Z', '+00:00'))
                    if (now - last_time).total_seconds() < 0.5:
                        return "Throttled", 200
                except:
                    pass

            # FIX 2: First time initialization - mark logger as active immediately
            if bus_data["lat"] is None:
                bus_data.update({
                    "lat": curr_lat, "lon": curr_lon, "time": now.isoformat(), 
                    "last_ping_time": now.isoformat(), "last_update_time": now.isoformat()
                })
                save_bus_state()
                # Force add to active_loggers so bus appears online immediately
                active_loggers[key] = {"ip": ip, "last": now, "session_start": now}
                return "Initialized", 200
            
            lpt_str = bus_data.get("last_ping_time")
            last_ping_datetime = datetime.datetime.fromisoformat(lpt_str) if lpt_str else now
            
            dist = calculate_distance(bus_data["lat"], bus_data["lon"], curr_lat, curr_lon)
            dt = (now - last_ping_datetime).total_seconds() if last_ping_datetime else 1
            calc_speed = (dist / dt) * 3.6 if dt > 0 else 0

            MAX_SPEED_MS = 25.0
            if dist > 10 and dt > 0 and (dist / dt) > MAX_SPEED_MS:
                return "OK", 200

            prev = active_loggers.get(key)
            gap = (now - prev['last']).total_seconds() if prev else None
            is_new_session = prev is None or gap > SESSION_TIMEOUT

            speed_param = request.args.get('speed')
            gps_speed_available = speed_param is not None and speed_param != '%SPD'

            if gps_speed_available:
                final_speed = 0.0 if raw_speed_kmh < 2.0 else raw_speed_kmh
            else:
                final_speed = calc_speed if dist >= 10.0 else 0.0

            if is_new_session:
                current_status = "STOPPED"
            else:
                current_status = bus_data.get("status", "STOPPED")

            actually_moving = final_speed >= 5.0 and dist >= 8.0
            clearly_stopped = final_speed < 3.0 or dist < 3.0

            if current_status == "STOPPED":
                new_status = "RUNNING" if actually_moving else "STOPPED"
            else:
                new_status = "STOPPED" if clearly_stopped else "RUNNING"

            data_changed = (bus_data["lat"] != curr_lat or 
                          bus_data["lon"] != curr_lon or 
                          abs(bus_data.get("speed_kmh", 0) - final_speed) > 0.1 or
                          bus_data.get("status") != new_status)

            bus_data.update({
                "lat": curr_lat, 
                "lon": curr_lon, 
                "speed_kmh": round(final_speed, 1), 
                "status": new_status,
                "time": now.isoformat(),
                "last_ping_time": now.isoformat(),
                "last_update_time": now.isoformat()
            })
            
            if data_changed:
                save_bus_state()

            if is_new_session:
                session_start = now
                net_info_log = f" | Net: {net_type}({net_str})" if net_type != 'Unknown' and net_str != 'Unknown' else ""
                write_log(f"{'─'*55}")
                write_log(f"SESSION_START | Key: {key} | IP: {ip}{net_info_log}")
            else:
                session_start = prev['session_start']

            if len(active_loggers) > MAX_LOGGERS:
                oldest = min(active_loggers.keys(), key=lambda k: active_loggers[k]['last'])
                del active_loggers[oldest]
                
            active_loggers[key] = {"ip": ip, "last": now, "session_start": session_start}
            log_lat, log_lon, log_spd = curr_lat, curr_lon, round(final_speed, 1)
            
        area = get_area_name(log_lat, log_lon)
        net_info = f" | Net: {net_type}({net_str})" if net_type != 'Unknown' and net_str != 'Unknown' else ""
        write_log(f"DRIVER_PING | Area: {area} | Spd: {log_spd}{net_info}")
    except Exception as e: 
        write_log(f"Error in /log: {str(e)}")
        return "Internal Server Error", 500
    return "OK", 200

@app.route('/get_bus')
def get_bus():
    now = datetime.datetime.now(IST)
    admin_key = request.headers.get('X-Admin-Key')
    
    ip = get_ip()
    real_ip = get_real_ip()
    ua = request.headers.get('User-Agent', '')
    ua_lower = ua.lower()
    if 'android' in ua_lower:
        device = 'Android'
    elif 'iphone' in ua_lower or 'ipad' in ua_lower or 'ipod' in ua_lower:
        device = 'iPhone'
    elif 'windows phone' in ua_lower:
        device = 'Windows Phone'
    else:
        device = 'PC'
    
    with data_lock:
        existing = active_viewers.get(ip, {})
        connected_at = existing.get('connected_at', now.isoformat())
        
        if len(active_viewers) > MAX_VIEWERS:
            oldest_ips = sorted(active_viewers.keys(), key=lambda k: active_viewers[k]['last'])[:200]
            for old_ip in oldest_ips:
                del active_viewers[old_ip]
        
        active_viewers[ip] = {
            'lat': existing.get('lat'),
            'lon': existing.get('lon'),
            'device': device,
            'real_ip': real_ip,
            'connected_at': connected_at,
            'last': now
        }
        
        res = dict(bus_data)
        res['server_now'] = now.isoformat()
        res['is_online'] = any((now - v['last']).total_seconds() < 15 for v in active_loggers.values())
        
        is_admin_valid = admin_key and hmac.compare_digest(admin_key, ADMIN_SECRET)
        res['admin_active'] = bool(is_admin_valid)
        
        if is_admin_valid:
            recent_viewers = {}
            for k, v in active_viewers.items():
                age = (now - v['last']).total_seconds()
                if age < 120:
                    recent_viewers[k] = {
                        'lat': v.get('lat'),
                        'lon': v.get('lon'),
                        'device': v.get('device', 'Unknown'),
                        'real_ip': v.get('real_ip', 'Unknown'),
                        'connected_at': v.get('connected_at', ''),
                        'last_seen_secs': int(age),
                        'has_location': v.get('lat') is not None
                    }
            res['all_viewers'] = recent_viewers
        return jsonify(res)

@app.route('/update_viewer')
def update_viewer():
    ip = get_ip()
    real_ip = get_real_ip()
    lat, lon = request.args.get('lat'), request.args.get('lon')
    ua = request.headers.get('User-Agent', '')
    ua_lower = ua.lower()
    if 'android' in ua_lower:
        device = 'Android'
    elif 'iphone' in ua_lower or 'ipad' in ua_lower or 'ipod' in ua_lower:
        device = 'iPhone'
    elif 'windows phone' in ua_lower:
        device = 'Windows Phone'
    else:
        device = 'PC'
    now = datetime.datetime.now(IST)
    with data_lock:
        existing = active_viewers.get(ip, {})
        connected_at = existing.get('connected_at', now.isoformat())
        entry = {
            'lat': float(lat) if lat else existing.get('lat'),
            'lon': float(lon) if lon else existing.get('lon'),
            'device': device,
            'real_ip': real_ip,
            'connected_at': connected_at,
            'last': now
        }
        active_viewers[ip] = entry
    return "OK", 200

def session_watchdog():
    last_cleanup = time.time()
    
    while True:
        try:
            time.sleep(15)
            now = datetime.datetime.now(IST)
            ended = []
            
            with data_lock:
                for key, v in list(active_loggers.items()):
                    if (now - v['last']).total_seconds() > SESSION_TIMEOUT:
                        ended.append((key, v))
                        del active_loggers[key]
                
                if time.time() - last_cleanup > 300:
                    stale_ips = [ip for ip, data in active_viewers.items() if (now - data['last']).total_seconds() > 300]
                    for ip in stale_ips:
                        del active_viewers[ip]
                    last_cleanup = time.time()

            for key, v in ended:
                duration = (now - v['session_start']).total_seconds()
                mins, secs = int(duration // 60), int(duration % 60)
                write_log(f"SESSION_END   | Key: {key} | IP: {v['ip']} | Duration: {mins}m {secs}s")
                write_log(f"{'─'*55}")
        except Exception as e:
            app_logger.error(f"Watchdog error: {e}")

_watchdog = threading.Thread(target=session_watchdog, daemon=True)
_watchdog.start()

@app.route('/health')
def health():
    uptime = time.time() - START_TIME
    uptime_str = f"{int(uptime // 3600)}h {int((uptime % 3600) // 60)}m {int(uptime % 60)}s"
    
    with data_lock:
        return jsonify({
            'status': 'ok',
            'active_loggers': len(active_loggers),
            'active_viewers': len(active_viewers),
            'db_queue_size': db_queue.qsize(),
            'geo_cache_size': len(geo_cache),
            'rate_limits_size': len(rate_limits),
            'nominatim_queue_size': nominatim_queue.qsize(),
            'server_time': datetime.datetime.now(IST).isoformat(),
            'uptime': uptime_str
        })

if __name__ == '__main__':
    print("=" * 50)
    print("[-] SGT Bus Tracker Server Starting...")
    print(f"📅 Server Time: {datetime.datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"⚙️  Configuration: MAX_VIEWERS={MAX_VIEWERS}, SESSION_TIMEOUT={SESSION_TIMEOUT}")
    print("⚠️  WARNING: Running with Flask development server")
    print("For production, use: gunicorn -w 4 -b 0.0.0.0:5000 app:app")
    print("=" * 50)
    app.run(debug=False, threaded=True, host='0.0.0.0', port=5000)