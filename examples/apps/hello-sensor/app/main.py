"""An example EdgeShell app (docs/APPS.md): reads a value every INTERVAL_S
seconds, keeps the last one in $APP_DATA, and serves it on 127.0.0.1:8095.
Standard library only, so it needs no requirements.txt."""
import http.server
import json
import os
import threading
import time

DATA = os.environ.get('APP_DATA', '.')
INTERVAL = float(os.environ.get('INTERVAL_S', '5'))
LAST = os.path.join(DATA, 'last.json')


def read_sensor():
    """Replace with the real sensor. Here: the CPU temperature when there is one."""
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as fh:
            return round(int(fh.read()) / 1000.0, 1)
    except OSError:
        return None


def loop():
    while True:
        value = {'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'temp_c': read_sensor()}
        tmp = LAST + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump(value, fh)
        os.replace(tmp, LAST)
        time.sleep(INTERVAL)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            body = b'ok'
        else:
            try:
                with open(LAST, 'rb') as fh:
                    body = fh.read()
            except OSError:
                body = b'{}'
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    threading.Thread(target=loop, daemon=True).start()
    print('hello-sensor %s serving on 127.0.0.1:8095' % os.environ.get('APP_VERSION'), flush=True)
    http.server.HTTPServer(('127.0.0.1', 8095), Handler).serve_forever()
