import os
import subprocess
import time
import json
import threading
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'hf_stream_stable_v1'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 * 1024

# /app/data is writable on Hugging Face Spaces (free tier has no persistent disk —
# files are lost on container restart; upgrade to paid persistent-storage to keep them)
BASE_DIR = '/app/data'
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'video_library')
STATE_FILE = os.path.join(BASE_DIR, 'stream_state.json')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# active_streams: { 'stream_key': { 'process': proc, 'filename': str, 'loop': bool } }
active_streams = {}
streams_lock = threading.Lock()


# State persistence

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


# FFmpeg launcher

def start_ffmpeg_stream(filename, stream_key):
    """
    Start a single ffmpeg pass (no -stream_loop).

    WHY NO -stream_loop:
      Using -stream_loop -1 runs one ffmpeg process forever, causing timestamps
      to accumulate indefinitely. After ~68 hours YouTube's RTMP ingest rejects
      the stream and invalidates the key entirely (requires manual key regeneration).

    SOLUTION — Python-level restart loop (see monitor_streams):
      Each restart launches a fresh ffmpeg process with timestamps starting from 0.
      This gives the same timestamp stability as a re-encoding approach (-c:v libx264)
      but keeps CPU at near-zero because we still use -c copy.

    FLAGS:
      -re               : Read input at native frame rate (required for RTMP push).
      -fflags +genpts   : Regenerate PTS from DTS if PTS is missing/invalid in source.
      -avoid_negative_ts make_zero : Shift timestamps so the stream always starts at 0;
                          guards against source files with a non-zero start offset.
      -c copy           : No re-encoding — near-zero CPU on HuggingFace free tier.
      -f flv            : FLV container required by YouTube RTMP ingest.
    """
    video_path = os.path.join(UPLOAD_FOLDER, filename)
    youtube_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"

    command = [
        'ffmpeg', '-re',
        '-fflags', '+genpts',
        '-avoid_negative_ts', 'make_zero',
        '-i', video_path,
        '-c', 'copy',
        '-f', 'flv', youtube_url
    ]

    # stderr=DEVNULL keeps stream keys out of logs
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# Background monitor

def monitor_streams():
    """
    Polls every 3 seconds. When ffmpeg exits (file ended or crashed):
      - loop=True  → restart immediately (Python-level infinite loop, fresh timestamps)
      - loop=False → clean up the entry (play-once mode)
    """
    while True:
        time.sleep(3)
        with streams_lock:
            for key in list(active_streams.keys()):
                info = active_streams[key]
                if info['process'].poll() is not None:          # ffmpeg has exited
                    if info['loop']:
                        # Restart with a brand-new process → timestamps reset to 0
                        active_streams[key]['process'] = start_ffmpeg_stream(
                            info['filename'], key
                        )
                    else:
                        # Play-once mode: stream finished, remove entry
                        del active_streams[key]
                        # Save outside lock to avoid potential deadlock
                        threading.Thread(target=save_all_states, daemon=True).start()


# Routes

@app.route('/')
def index():
    with streams_lock:
        streams_snapshot = {k: {'filename': v['filename'], 'loop': v['loop']}
                            for k, v in active_streams.items()}
    files = sorted(os.listdir(UPLOAD_FOLDER))
    return render_template('index.html', streams=streams_snapshot, files=files)


@app.route('/health')
def health():
    """
    Uptime endpoint. Point UptimeRobot (or any free monitor) here every 5 minutes
    to prevent the HuggingFace Space from going to sleep and killing your stream.
    """
    with streams_lock:
        count = len(active_streams)
    return jsonify({'status': 'ok', 'active_streams': count})


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
        # Stop any existing stream for this key before replacing
        if stream_key in active_streams:
            active_streams[stream_key]['process'].terminate()
            del active_streams[stream_key]

        proc = start_ffmpeg_stream(filename, stream_key)
        active_streams[stream_key] = {'process': proc, 'filename': filename, 'loop': loop}

    save_all_states()
    flash(f'Stream started: {filename} (loop={loop})', 'success')
    return redirect(url_for('index'))


def _stop_stream_internal(key):
    """Must be called with streams_lock held."""
    if key in active_streams:
        active_streams[key]['process'].terminate()
        del active_streams[key]
        return True
    return False


@app.route('/stop/<key>', methods=['POST'])
def stop_stream(key):
    with streams_lock:
        stopped = _stop_stream_internal(key)
    if stopped:
        save_all_states()
        flash('Stream stopped', 'info')
    else:
        flash('Stream not found', 'warning')
    return redirect(url_for('index'))


# Startup: auto-restore previously running streams

saved_data = load_all_states()
for key, info in saved_data.items():
    video_path = os.path.join(UPLOAD_FOLDER, info['filename'])
    if os.path.exists(video_path):
        proc = start_ffmpeg_stream(info['filename'], key)
        active_streams[key] = {
            'process': proc,
            'filename': info['filename'],
            'loop': info['loop']
        }

threading.Thread(target=monitor_streams, daemon=True).start()

if __name__ == '__main__':
    # Port 7860 is required by Hugging Face Spaces
    app.run(host='0.0.0.0', port=7860, debug=False)
