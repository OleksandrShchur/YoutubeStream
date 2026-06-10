import http.server
import socketserver
import os
import subprocess
import threading
import time
import datetime

PORT = 7860

def start_stream():
    stream_key = os.getenv("YOUTUBE_STREAM_KEY")
    if not stream_key:
        print("ERROR: YOUTUBE_STREAM_KEY environment variable is missing!", flush=True)
        return

    cmd = [
        "ffmpeg",
        "-fflags", "+genpts",  # regenerates timestamps on every loop — fixes the discontinuity
        "-re",
        "-stream_loop", "-1",
        "-i", "video.mp4",
        "-c", "copy",          # no re-encoding = near-zero CPU, full original 4K bitrate
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-f", "flv", f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"
    ]

    session = 0
    while True:
        session += 1
        now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"=== Stream session #{session} starting at {now} ===", flush=True)
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
            for line in process.stdout:
                print(line, end="", flush=True)
            process.wait()
            print(f"[{datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}] ffmpeg exited with code: {process.returncode}", flush=True)
        except Exception as e:
            print(f"Exception encountered during execution: {e}", flush=True)

        print("Connection lost. Restarting stream pipeline in 3 seconds...", flush=True)
        time.sleep(3)

threading.Thread(target=start_stream, daemon=True).start()

Handler = http.server.SimpleHTTPRequestHandler
with socketserver.TCPServer(("", PORT), Handler) as httpd:
    print(f"Serving health-check traffic on port {PORT}", flush=True)
    httpd.serve_forever()
