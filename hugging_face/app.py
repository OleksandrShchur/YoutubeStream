"""
24/7 YouTube streamer (ffmpeg `-c copy`) for a Hugging Face Docker Space or any Docker host.

Configuration (Space secrets / environment variables)
  YOUTUBE_STREAM_KEY  stream key. Auto-starts on every boot. Never written to disk or rendered in HTML.
  ADMIN_PASSWORD      REQUIRED for the web UI (HTTP basic auth; user = ADMIN_USER, default "admin").
  STREAM_FILE         file in the video library to auto-start (default: video.mp4)
  ROTATE_SECONDS      lifetime of one ffmpeg process before a planned restart (default: 8 h)
  YOUTUBE_INGEST_URL  default rtmps://a.rtmps.youtube.com:443/live2
                      (HF Spaces only allows outbound ports 80/443/8080, so plain rtmp:// on 1935 is out)
"""
import collections
import hmac
import logging
import os
import re
import secrets
import subprocess
import threading
import time

from flask import (Flask, Response, flash, jsonify, redirect, render_template,
                   request, url_for)
from werkzeug.utils import secure_filename

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('streamer')

# --------------------------------------------------------------------------- config
BASE_DIR = os.environ.get('DATA_DIR', '/app/data')          # not persistent on free HF hardware
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'video_library')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

PORT = int(os.environ.get('PORT', '7860'))
ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
AUTOSTART_KEY = os.environ.get('YOUTUBE_STREAM_KEY', '').strip()
AUTOSTART_FILE = os.environ.get('STREAM_FILE', 'video.mp4')
INGEST_URL = os.environ.get('YOUTUBE_INGEST_URL',
                            'rtmps://a.rtmps.youtube.com:443/live2').rstrip('/')

ROTATE_SECONDS = float(os.environ.get('ROTATE_SECONDS', 8 * 3600))
STALL_SECONDS = float(os.environ.get('STALL_SECONDS', 30))      # no ffmpeg progress for this long => hung
STARTUP_GRACE = float(os.environ.get('STARTUP_GRACE', 45))      # time allowed to connect + first packet
MIN_HEALTHY_RUN = float(os.environ.get('MIN_HEALTHY_RUN', 60))  # a clean exit after this long = planned rotation
FAIL_WINDOW = 600                                               # rolling window for failure counting (s)
MAX_BACKOFF = 300

KEY_RE = re.compile(r'[A-Za-z0-9_.\-]{8,64}')   # no "/" => the key can't redirect ffmpeg's output target

app = Flask(__name__)
# HF exposes a stable per-Space secret; fall back to a random one elsewhere.
app.secret_key = os.environ.get('SPACE_SIGNING_SECRET') or secrets.token_hex(32)
app.config['MAX_CONTENT_LENGTH'] = int(float(os.environ.get('MAX_UPLOAD_GB', 8)) * 1024 ** 3)

streams = {}                    # id -> Stream
lock = threading.RLock()


# --------------------------------------------------------------------------- helpers
def safe_path(filename):
    """Return the absolute path of a file in the library, or None (blocks ../ traversal)."""
    name = secure_filename(filename or '')
    if not name or name != filename:
        return None
    path = os.path.join(UPLOAD_FOLDER, name)
    return path if os.path.isfile(path) else None


def probe_duration(path):
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=nw=1:nk=1', path],
            capture_output=True, text=True, timeout=15)
        d = float(out.stdout.strip())
        return d if d > 0 else None
    except Exception:
        return None


def build_command(path, key, loop):
    cmd = ['ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'warning', '-nostats',
           '-progress', 'pipe:1',            # machine-readable progress => stall detection
           '-re']
    if loop:
        # One process plays the file many times (one RTMP session for hours instead of one per
        # pass), then exits cleanly and the supervisor starts a fresh one => timestamps reset to 0.
        d = probe_duration(path)
        plays = int(ROTATE_SECONDS // d) if d else 500
        cmd += ['-stream_loop', str(max(plays - 1, 0))]   # N = number of *extra* plays
    cmd += ['-fflags', '+genpts', '-avoid_negative_ts', 'make_zero', '-i', path,
            '-c', 'copy', '-flvflags', 'no_duration_filesize',
            '-f', 'flv', f'{INGEST_URL}/{key}']
    return cmd


class Stream:
    def __init__(self, key, filename, loop):
        self.id = secrets.token_hex(4)            # used in URLs/HTML instead of the key
        self.key = key
        self.filename = filename
        self.loop = loop
        self.proc = None
        self.state = 'starting'                   # starting | live | waiting
        self.run_started = 0.0
        self.last_progress = 0.0
        self.out_time = 0.0
        self.next_start = 0.0
        self.restarts = 0
        self.exit_reason = None
        self.last_exit = None
        self.failures = collections.deque(maxlen=50)    # monotonic timestamps
        self.log = collections.deque(maxlen=30)         # redacted ffmpeg output


PROGRESS_RE = re.compile(
    r'^(frame|fps|stream_\d+_\d+_q|bitrate|total_size|out_time_us|out_time_ms|out_time|'
    r'dup_frames|drop_frames|speed|progress)=(.*)$')


def _reader(s, proc):
    """Drain ffmpeg's merged stdout/stderr: progress keys update the heartbeat, the rest is logged."""
    for line in proc.stdout:
        line = line.rstrip('\n')
        m = PROGRESS_RE.match(line)
        if m:
            if m.group(1) == 'out_time_us' and proc is s.proc:
                try:
                    us = int(m.group(2))
                except ValueError:
                    continue                       # "N/A" before the first packet
                if us > 0:
                    s.out_time = us / 1e6
                    s.last_progress = time.monotonic()
            continue
        if line:
            line = line.replace(s.key, '***')      # ffmpeg echoes the URL in errors
            s.log.append(line)
            log.warning('[%s] %s', s.id, line)


def _terminate(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _launch(s):
    path = safe_path(s.filename)
    if not path:
        raise FileNotFoundError(f'{s.filename} is missing from the library')
    proc = subprocess.Popen(build_command(path, s.key, s.loop),
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1, errors='replace')
    now = time.monotonic()
    s.proc, s.state = proc, 'starting'
    s.run_started = s.last_progress = now
    s.out_time = 0.0
    threading.Thread(target=_reader, args=(s, proc), daemon=True).start()
    log.info('[%s] ffmpeg started (pid %s, file %s, loop=%s)', s.id, proc.pid, s.filename, s.loop)


def _schedule_retry(s, now, why):
    """Rolling-window exponential backoff: 5s, 10s, 20s ... capped at 5 min."""
    s.failures.append(now)
    n = sum(1 for t in s.failures if now - t < FAIL_WINDOW)
    delay = min(MAX_BACKOFF, 5 * 2 ** (n - 1))
    s.proc, s.state, s.next_start = None, 'waiting', now + delay
    s.restarts += 1
    s.last_exit = why
    s.log.append(f'[supervisor] {why}; retry in {int(delay)}s')
    log.error('[%s] %s; retry in %ss (%d failure(s) in last %ds)',
              s.id, why, int(delay), n, FAIL_WINDOW)


def _try_launch(s, now):
    try:
        _launch(s)
    except Exception as exc:                        # missing file, OSError, ...
        _schedule_retry(s, now, f'launch failed: {exc}')


def _handle_exit(s, rc, now):
    ran = now - s.run_started
    reason = s.exit_reason or f'ffmpeg exited with code {rc}'
    s.exit_reason = None
    if not s.loop:                                   # play-once mode: done
        log.info('[%s] play-once stream finished (%s)', s.id, reason)
        streams.pop(s.id, None)
        return
    if rc == 0 and ran >= MIN_HEALTHY_RUN:           # planned rotation, not a failure
        s.proc, s.state, s.next_start = None, 'waiting', now + 1
        s.restarts += 1
        s.last_exit = f'rotated after {int(ran)}s'
        log.info('[%s] planned rotation after %ds', s.id, int(ran))
        return
    _schedule_retry(s, now, f'{reason} after {int(ran)}s')


def _tick():
    now = time.monotonic()
    to_kill = []
    with lock:
        for s in list(streams.values()):
            if s.proc is None:
                if now >= s.next_start:
                    _try_launch(s, now)
                continue
            rc = s.proc.poll()
            if rc is not None:
                _handle_exit(s, rc, now)
                continue
            silent = now - s.last_progress
            if s.out_time > 0 and silent < STALL_SECONDS:
                s.state = 'live'
            else:
                s.state = 'starting'
                if silent > STALL_SECONDS and now - s.run_started > STARTUP_GRACE:
                    s.exit_reason = f'stalled (no progress for {int(silent)}s)'
                    to_kill.append(s.proc)
    for p in to_kill:                                # SIGKILL: a write blocked on a dead socket ignores SIGTERM
        try:
            p.kill()
            p.wait(timeout=5)
        except Exception:
            pass


def supervise():
    while True:
        time.sleep(2)
        try:
            _tick()
        except Exception:                            # the thread must never die silently
            log.exception('supervisor tick failed')


def add_stream(key, filename, loop):
    with lock:
        for sid, old in list(streams.items()):       # one stream per key
            if old.key == key:
                del streams[sid]
                _terminate(old.proc)
        s = Stream(key, filename, loop)
        streams[s.id] = s
        _try_launch(s, time.monotonic())
        return s


def snapshot():
    now = time.monotonic()
    with lock:
        return [{'id': s.id, 'key_hint': '…' + s.key[-4:], 'filename': s.filename,
                 'loop': s.loop, 'state': s.state, 'restarts': s.restarts,
                 'uptime': int(now - s.run_started) if s.proc else 0,
                 'last_exit': s.last_exit, 'log': list(s.log)[-8:]}
                for s in streams.values()]


# --------------------------------------------------------------------------- auth
@app.before_request
def require_auth():
    if request.path == '/health':
        return None
    if not ADMIN_PASSWORD:
        return Response('Set the ADMIN_PASSWORD secret to enable the UI.', 503)
    a = request.authorization
    ok = (a is not None
          and hmac.compare_digest((a.username or '').encode(), ADMIN_USER.encode())
          and hmac.compare_digest((a.password or '').encode(), ADMIN_PASSWORD.encode()))
    if not ok:
        return Response('Authentication required', 401,
                        {'WWW-Authenticate': 'Basic realm="Stream control"'})
    return None


# --------------------------------------------------------------------------- routes
@app.route('/')
def index():
    return render_template('index.html', streams=snapshot(),
                           files=sorted(os.listdir(UPLOAD_FOLDER)))


@app.route('/health')
def health():
    """200 only while at least one stream is really sending data; 503 otherwise.
    Point UptimeRobot here: it keeps the Space awake AND alerts you when the stream is down."""
    snap = snapshot()
    live = sum(1 for s in snap if s['state'] == 'live')
    return jsonify({'status': 'ok' if live else 'down', 'live_streams': live,
                    'total_streams': len(snap)}), (200 if live else 503)


@app.route('/upload', methods=['POST'])
def upload_file():
    f = request.files.get('video_file')
    if f is None or f.filename == '':
        flash('No file selected', 'danger')
        return redirect(url_for('index'))
    filename = secure_filename(f.filename)
    if not filename:
        flash('Invalid file name', 'danger')
        return redirect(url_for('index'))
    with lock:
        if any(s.filename == filename for s in streams.values()):
            flash('Cannot overwrite a file that is currently streaming!', 'danger')
            return redirect(url_for('index'))
    f.save(os.path.join(UPLOAD_FOLDER, filename))
    flash(f'File "{filename}" uploaded successfully', 'success')
    return redirect(url_for('index'))


@app.route('/delete_file/<filename>', methods=['POST'])
def delete_file(filename):
    path = safe_path(filename)
    with lock:
        in_use = any(s.filename == filename for s in streams.values())
    if in_use:
        flash('Cannot delete a file that is currently streaming!', 'danger')
    elif not path:
        flash('File not found', 'warning')
    else:
        os.remove(path)
        flash(f'File "{filename}" deleted', 'info')
    return redirect(url_for('index'))


@app.route('/start', methods=['POST'])
def start_stream():
    key = (request.form.get('stream_key') or '').strip()
    filename = (request.form.get('selected_file') or '').strip()
    loop = 'loop' in request.form
    if not key or not filename:
        flash('Please select a file and enter a stream key', 'danger')
    elif not KEY_RE.fullmatch(key):
        flash('That does not look like a YouTube stream key', 'danger')
    elif not safe_path(filename):
        flash(f'File "{filename}" not found in library', 'danger')
    else:
        add_stream(key, filename, loop)
        flash(f'Stream started: {filename} (loop={loop})', 'success')
    return redirect(url_for('index'))


@app.route('/stop/<sid>', methods=['POST'])
def stop_stream(sid):
    with lock:
        s = streams.pop(sid, None)
    if s:
        _terminate(s.proc)
        flash('Stream stopped', 'info')
    else:
        flash('Stream not found', 'warning')
    return redirect(url_for('index'))


# --------------------------------------------------------------------------- boot
def bootstrap():
    if AUTOSTART_KEY:
        if not KEY_RE.fullmatch(AUTOSTART_KEY):
            log.error('YOUTUBE_STREAM_KEY has an unexpected format - not auto-starting')
        elif not safe_path(AUTOSTART_FILE):
            log.error('STREAM_FILE %s not found in %s - not auto-starting', AUTOSTART_FILE, UPLOAD_FOLDER)
        else:
            add_stream(AUTOSTART_KEY, AUTOSTART_FILE, True)
    else:
        log.warning('YOUTUBE_STREAM_KEY not set - nothing will auto-start after a restart')
    if not ADMIN_PASSWORD:
        log.warning('ADMIN_PASSWORD not set - the web UI is disabled')
    threading.Thread(target=supervise, daemon=True, name='supervisor').start()


if __name__ == '__main__':
    from waitress import serve
    bootstrap()
    log.info('listening on :%d', PORT)
    serve(app, host='0.0.0.0', port=PORT, threads=4)
