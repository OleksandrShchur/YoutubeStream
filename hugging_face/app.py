import os
import subprocess
import time
import json
import threading
import logging
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from werkzeug.utils import secure_filename

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = 'hf_stream_stable_v1'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 * 1024

BASE_DIR = '/app/data'
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'video_library')
STATE_FILE = os.path.join(BASE_DIR, 'stream_state.json')
FFMPEG_LOG_DIR = os.path.join(BASE_DIR, 'ffmpeg_logs')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(FFMPEG_LOG_DIR, exist_ok=True)

# Restart ffmpeg before YouTube drops the session (~2–3 h observed with copy + high bitrate).
PLANNED_RESTART_SEC = int(os.environ.get('PLANNED_RESTART_SEC', 2 * 3600))
# Exponential backoff when ffmpeg dies quickly or with a non-zero exit code.
BACKOFF_BASE_SEC = int(os.environ.get('BACKOFF_BASE_SEC', 5))
BACKOFF_MAX_SEC = int(os.environ.get('BACKOFF_MAX_SEC', 1800))
# Runs shorter than this after a crash count as a failure for backoff.
MIN_HEALTHY_RUNTIME_SEC = int(os.environ.get('MIN_HEALTHY_RUNTIME_SEC', 60))
# Long runs ended by YouTube still get a short reconnect pause (not full backoff).
YOUTUBE_RECONNECT_SEC = int(os.environ.get('YOUTUBE_RECONNECT_SEC', 10))

VIDEO_BITRATE = os.environ.get('VIDEO_BITRATE', '8000k')
AUDIO_BITRATE = os.environ.get('AUDIO_BITRATE', '128k')
X264_PRESET = os.environ.get('X264_PRESET', 'ultrafast')
# copy = near-zero CPU. cap = ultrafast re-encode capped for YouTube (recommended).
# reencode = full quality re-encode (high CPU).
ENCODE_MODE = os.environ.get('ENCODE_MODE', 'cap').lower()

active_streams = {}
streams_lock = threading.Lock()
monitor_thread = None


# ── State persistence ──────────────────────────────────────────────────────────

def save_all_states():
    with streams_lock:
        data = {k: {'filename': v['filename'], 'loop': v['loop']}
                for k, v in active_streams.items()}
    with open(STATE_FILE, 'w') as f:
        json.dump(data, f)


def load_all_states():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


# ── FFmpeg helpers ─────────────────────────────────────────────────────────────

def _log_path(stream_key):
    return os.path.join(FFMPEG_LOG_DIR, f"ffmpeg_{stream_key[-8:]}.log")


def _tail_file(path, lines=25):
    try:
        with open(path, 'rb') as f:
            text = f.read().decode('utf-8', errors='replace')
        return '\n'.join(text.splitlines()[-lines:])
    except Exception:
        return ''


def _close_log_file(info):
    try:
        info['log_file'].close()
    except Exception:
        pass


def _input_ts_args():
    """Keep timestamps sane when looping MP4 → FLV with stream copy."""
    return ['-fflags', '+genpts', '-avoid_negative_ts', 'make_zero']


def _output_args():
    if ENCODE_MODE in ('reencode', 'cap'):
        preset = X264_PRESET if ENCODE_MODE == 'reencode' else 'ultrafast'
        return [
            '-c:v', 'libx264', '-preset', preset, '-tune', 'zerolatency',
            '-b:v', VIDEO_BITRATE, '-maxrate', VIDEO_BITRATE,
            '-bufsize', '16000k', '-pix_fmt', 'yuv420p',
            '-g', '60', '-keyint_min', '60',
            '-c:a', 'aac', '-b:a', AUDIO_BITRATE, '-ar', '44100',
        ]
    return ['-c', 'copy']


def _classify_stderr(tail):
    lower = tail.lower()
    if 'broken pipe' in lower or 'connection timed out' in lower:
        return 'youtube_disconnect'
    return 'unknown'


def _terminate_ffmpeg(proc):
    """Ask ffmpeg to quit cleanly so YouTube sees a proper stream end."""
    if proc is None or proc.poll() is not None:
        return
    if proc.stdin is not None:
        try:
            proc.stdin.write(b'q')
            proc.stdin.flush()
            proc.wait(timeout=15)
            return
        except Exception:
            pass
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def start_ffmpeg_stream(filename, stream_key, loop=False):
    """
    Loop mode: one long-lived ffmpeg with -stream_loop -1 (single RTMP session).
      Python restarts every PLANNED_RESTART_SEC so timestamps never hit YouTube's
      ~68 h limit — without reconnecting every time a short file ends.

    Default ENCODE_MODE=cap re-encodes at 8 Mbps (low CPU, under YouTube's 13.5 Mbps cap).
    Set ENCODE_MODE=copy for minimum CPU if source file is already compressed.
    """
    video_path = os.path.join(UPLOAD_FOLDER, filename)
    youtube_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"
    log_path = _log_path(stream_key)
    log_file = open(log_path, 'a', encoding='utf-8', errors='replace')

    command = ['ffmpeg', '-hide_banner', '-loglevel', 'warning']
    command += _input_ts_args()
    if loop:
        command += ['-re', '-stream_loop', '-1', '-i', video_path]
    else:
        command += ['-re', '-i', video_path]
    command += _output_args()
    command += ['-flvflags', 'no_duration_filesize', '-f', 'flv', youtube_url]

    proc = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log_file
    )
    return proc, log_file


def _backoff_delay(failures):
    if failures <= 0:
        return 0
    return min(BACKOFF_BASE_SEC * (2 ** (failures - 1)), BACKOFF_MAX_SEC)


def _log_ffmpeg_exit(key, info, exit_code, planned=False, disconnect_kind=None):
    _close_log_file(info)
    tail = _tail_file(_log_path(key))
    runtime = time.time() - info.get('started_at', time.time())
    kind = 'planned restart' if planned else 'exit'
    logger.warning(
        "ffmpeg %s: key=...%s file=%s code=%d runtime=%.0fs failures=%d%s",
        kind, key[-5:], info['filename'], exit_code, runtime,
        info.get('consecutive_failures', 0),
        f" ({disconnect_kind})" if disconnect_kind else '',
    )
    if tail:
        logger.warning("ffmpeg stderr (last lines) for key=...%s:\n%s", key[-5:], tail)
    if disconnect_kind == 'youtube_disconnect' and info.get('consecutive_failures', 0) >= 3:
        logger.error(
            "YouTube likely ended the broadcast (key=...%s). "
            "Open YouTube Studio → Go Live on your scheduled stream, then restart here. "
            "If reconnects keep failing, regenerate the stream key.",
            key[-5:],
        )


def _spawn_stream_locked(key, info):
    """Must be called with streams_lock held. Reuses filename/loop on info."""
    proc, log_file = start_ffmpeg_stream(info['filename'], key, loop=info['loop'])
    info['process'] = proc
    info['log_file'] = log_file
    info['started_at'] = time.time()
    info['planned_restart'] = False
    info['next_restart_at'] = 0


# ── Background monitor ─────────────────────────────────────────────────────────

def monitor_streams():
    consecutive_errors = 0

    while True:
        time.sleep(3)
        try:
            now = time.time()
            with streams_lock:
                for key in list(active_streams.keys()):
                    info = active_streams[key]
                    proc = info.get('process')

                    # Proactive restart before timestamp overflow (loop mode only).
                    if (
                        proc is not None
                        and info['loop']
                        and proc.poll() is None
                        and now - info.get('started_at', now) >= PLANNED_RESTART_SEC
                    ):
                        logger.info(
                            "Planned restart for key=...%s after %ds",
                            key[-5:], PLANNED_RESTART_SEC
                        )
                        info['planned_restart'] = True
                        _terminate_ffmpeg(proc)

                    if proc is None or proc.poll() is None:
                        continue

                    exit_code = proc.returncode
                    planned = info.pop('planned_restart', False)
                    runtime = now - info.get('started_at', now)
                    stderr_tail = _tail_file(_log_path(key))
                    disconnect_kind = None if planned else _classify_stderr(stderr_tail)

                    if planned:
                        info['consecutive_failures'] = 0
                    elif runtime >= PLANNED_RESTART_SEC * 0.75:
                        # Ran long enough; YouTube closed the socket — not an ffmpeg fault.
                        info['consecutive_failures'] = 0
                    elif disconnect_kind == 'youtube_disconnect' and runtime >= MIN_HEALTHY_RUNTIME_SEC:
                        info['consecutive_failures'] = 0
                    elif exit_code != 0 or runtime < MIN_HEALTHY_RUNTIME_SEC:
                        info['consecutive_failures'] = info.get('consecutive_failures', 0) + 1
                    else:
                        info['consecutive_failures'] = 0

                    _log_ffmpeg_exit(
                        key, info, exit_code, planned=planned, disconnect_kind=disconnect_kind
                    )

                    if info['loop']:
                        failures = info['consecutive_failures']
                        if failures == 0 and disconnect_kind == 'youtube_disconnect':
                            delay = YOUTUBE_RECONNECT_SEC
                        else:
                            delay = _backoff_delay(failures)
                        if delay:
                            logger.info(
                                "Backoff %ds before restart (key=...%s, failures=%d)",
                                delay, key[-5:], info['consecutive_failures']
                            )
                        info['next_restart_at'] = now + delay
                        info['process'] = None
                    else:
                        del active_streams[key]
                        threading.Thread(target=save_all_states, daemon=True).start()

                # Start streams whose backoff window has elapsed.
                for key in list(active_streams.keys()):
                    info = active_streams[key]
                    if not info['loop'] or info.get('process') is not None:
                        continue
                    if now < info.get('next_restart_at', 0):
                        continue
                    logger.info("Restarting stream key=...%s file=%s", key[-5:], info['filename'])
                    _spawn_stream_locked(key, info)

            consecutive_errors = 0

        except Exception:
            consecutive_errors += 1
            logger.exception(
                "monitor_streams iteration error #%d — will retry in 3 s",
                consecutive_errors
            )


def start_monitor():
    global monitor_thread
    monitor_thread = threading.Thread(target=monitor_streams, daemon=True, name='monitor')
    monitor_thread.start()
    logger.info("monitor_streams thread started (id=%d)", monitor_thread.ident)


def watchdog():
    while True:
        time.sleep(30)
        if monitor_thread is None or not monitor_thread.is_alive():
            logger.critical("monitor_streams thread is dead — restarting it now")
            start_monitor()


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    with streams_lock:
        streams_snapshot = {k: {'filename': v['filename'], 'loop': v['loop']}
                            for k, v in active_streams.items()}
    files = sorted(os.listdir(UPLOAD_FOLDER))
    return render_template('index.html', streams=streams_snapshot, files=files)


@app.route('/health')
def health():
    with streams_lock:
        count = len(active_streams)
    return jsonify({
        'status': 'ok',
        'active_streams': count,
        'monitor_alive': monitor_thread is not None and monitor_thread.is_alive(),
        'planned_restart_hours': PLANNED_RESTART_SEC / 3600,
        'encode_mode': ENCODE_MODE,
    })


@app.route('/upload', methods=['POST'])
def upload_file():
    if 'video_file' not in request.files:
        flash('No file part in request', 'danger')
        return redirect(url_for('index'))

    file = request.files['video_file']
    if file.filename == '':
        flash('No file selected', 'danger')
        return redirect(url_for('index'))

    filename = secure_filename(file.filename)
    file.save(os.path.join(UPLOAD_FOLDER, filename))
    flash(f'File "{filename}" uploaded successfully', 'success')
    return redirect(url_for('index'))


@app.route('/delete_file/<filename>', methods=['POST'])
def delete_file(filename):
    with streams_lock:
        in_use = any(s['filename'] == filename for s in active_streams.values())

    if in_use:
        flash('Cannot delete a file that is currently streaming!', 'danger')
    else:
        path = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(path):
            os.remove(path)
            flash(f'File "{filename}" deleted', 'info')
        else:
            flash('File not found', 'warning')
    return redirect(url_for('index'))


@app.route('/start', methods=['POST'])
def start_stream():
    stream_key = (request.form.get('stream_key') or '').strip()
    filename   = request.form.get('selected_file', '').strip()
    loop       = 'loop' in request.form

    if not stream_key or not filename:
        flash('Please select a file and enter a stream key', 'danger')
        return redirect(url_for('index'))

    video_path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(video_path):
        flash(f'File "{filename}" not found in library', 'danger')
        return redirect(url_for('index'))

    with streams_lock:
        if stream_key in active_streams:
            _stop_stream_internal(stream_key)

        proc, log_file = start_ffmpeg_stream(filename, stream_key, loop=loop)
        active_streams[stream_key] = {
            'process': proc,
            'filename': filename,
            'loop': loop,
            'log_file': log_file,
            'started_at': time.time(),
            'consecutive_failures': 0,
            'next_restart_at': 0,
            'planned_restart': False,
        }

    save_all_states()
    logger.info("Stream started: key=...%s file=%s loop=%s", stream_key[-5:], filename, loop)
    flash(f'Stream started: {filename} (loop={loop})', 'success')
    return redirect(url_for('index'))


def _stop_stream_internal(key):
    """Must be called with streams_lock held."""
    if key in active_streams:
        _terminate_ffmpeg(active_streams[key].get('process'))
        _close_log_file(active_streams[key])
        del active_streams[key]
        return True
    return False


@app.route('/stop/<key>', methods=['POST'])
def stop_stream(key):
    with streams_lock:
        stopped = _stop_stream_internal(key)
    if stopped:
        save_all_states()
        logger.info("Stream stopped: key=...%s", key[-5:])
        flash('Stream stopped', 'info')
    else:
        flash('Stream not found', 'warning')
    return redirect(url_for('index'))


# ── Startup ────────────────────────────────────────────────────────────────────

saved_data = load_all_states()
for key, info in saved_data.items():
    video_path = os.path.join(UPLOAD_FOLDER, info['filename'])
    if os.path.exists(video_path):
        proc, log_file = start_ffmpeg_stream(info['filename'], key, loop=info['loop'])
        active_streams[key] = {
            'process': proc,
            'filename': info['filename'],
            'loop': info['loop'],
            'log_file': log_file,
            'started_at': time.time(),
            'consecutive_failures': 0,
            'next_restart_at': 0,
            'planned_restart': False,
        }
        logger.info("Auto-restored stream: key=...%s file=%s", key[-5:], info['filename'])
    else:
        logger.warning(
            "Skipping auto-restore for key=...%s: file '%s' not found",
            key[-5:], info['filename']
        )

start_monitor()
threading.Thread(target=watchdog, daemon=True, name='watchdog').start()
logger.info(
    "Encode mode: %s | planned restart every %.1fh | video bitrate %s",
    ENCODE_MODE, PLANNED_RESTART_SEC / 3600, VIDEO_BITRATE,
)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=7860, debug=False)
