import os
import subprocess
import time
import json
import threading
import shutil
from flask import Flask, render_template, request, redirect, url_for, flash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'hetzner_stream_final_v3'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 * 1024

# /app/data is writable on Hugging Face Spaces (free tier has no persistent disk,
# so files are lost on restart — upgrade to a paid persistent-storage Space to keep them)
BASE_DIR = '/app/data'
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'video_library')
STATE_FILE = os.path.join(BASE_DIR, 'stream_state.json')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# active_streams: { 'stream_key': { 'process': proc, 'filename': 'video.mp4', 'loop': bool } }
active_streams = {}

def save_all_states():
    data = {k: {'filename': v['filename'], 'loop': v['loop']} for k, v in active_streams.items()}
    with open(STATE_FILE, 'w') as f:
        json.dump(data, f)

def load_all_states():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except: return {}
    return {}

def start_ffmpeg_stream(filename, stream_key, loop):
    video_path = os.path.join(UPLOAD_FOLDER, filename)
    youtube_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"
    
    command = ['ffmpeg', '-re']
    if loop: command.extend(['-stream_loop', '-1'])
    
    command.extend([
        '-fflags', '+genpts',       # fixes timestamp resets / 12-hour dropout
        '-i', video_path,
        '-c', 'copy',               # zero re-encoding — near-zero CPU usage
        '-f', 'flv', youtube_url
    ])

    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def monitor_streams():
    while True:
        time.sleep(10)
        for key in list(active_streams.keys()):
            info = active_streams[key]
            if info['process'].poll() is not None:
                print(f"Перезапуск {key}...")
                active_streams[key]['process'] = start_ffmpeg_stream(info['filename'], key, info['loop'])

@app.route('/')
def index():
    files = os.listdir(UPLOAD_FOLDER)
    return render_template('index.html', streams=active_streams, files=files)

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'video_file' in request.files:
        file = request.files['video_file']
        if file.filename != '':
            filename = secure_filename(file.filename)
            file.save(os.path.join(UPLOAD_FOLDER, filename))
            flash(f'Файл {filename} загружен в библиотеку', 'success')
    return redirect(url_for('index'))

@app.route('/delete_file/<filename>', methods=['POST'])
def delete_file(filename):
    in_use = any(s['filename'] == filename for s in active_streams.values())
    if in_use:
        flash('Нельзя удалить файл, который сейчас транслируется!', 'danger')
    else:
        path = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(path):
            os.remove(path)
            flash('Файл удален', 'info')
    return redirect(url_for('index'))

@app.route('/start', methods=['POST'])
def start_stream():
    stream_key = request.form.get('stream_key').strip()
    loop = 'loop' in request.form
    filename = request.form.get('selected_file')

    if not stream_key or not filename:
        flash('Выберите файл и введите ключ!', 'danger')
        return redirect(url_for('index'))

    if stream_key in active_streams:
        stop_stream_internal(stream_key)

    proc = start_ffmpeg_stream(filename, stream_key, loop)
    active_streams[stream_key] = {'process': proc, 'filename': filename, 'loop': loop}
    save_all_states()
    return redirect(url_for('index'))

def stop_stream_internal(key):
    if key in active_streams:
        active_streams[key]['process'].terminate()
        del active_streams[key]
        save_all_states()

@app.route('/stop/<key>', methods=['POST'])
def stop_stream(key):
    stop_stream_internal(key)
    return redirect(url_for('index'))

# Auto-restore on startup
saved_data = load_all_states()
for key, info in saved_data.items():
    if os.path.exists(os.path.join(UPLOAD_FOLDER, info['filename'])):
        p = start_ffmpeg_stream(info['filename'], key, info['loop'])
        active_streams[key] = {'process': p, 'filename': info['filename'], 'loop': info['loop']}

threading.Thread(target=monitor_streams, daemon=True).start()

if __name__ == '__main__':
    # Port 7860 is required by Hugging Face Spaces
    app.run(host='0.0.0.0', port=7860)
