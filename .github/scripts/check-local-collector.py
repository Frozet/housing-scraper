"""Smoke-test the platform launcher against a local API stub."""

import json
import os
from pathlib import Path
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tempfile import TemporaryDirectory, TemporaryFile
from threading import Event, Thread
from time import monotonic


ROOT = Path(__file__).resolve().parents[2]
CLAIMED = Event()
STOP = Event()


class APIStub(BaseHTTPRequestHandler):
    def reply(self, status, data):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/api/health":
            self.reply(200, {"ok": True})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/browser/claim" or self.headers.get("X-Local-Collector") != "1":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not body.get("token") or not body.get("protocol_version"):
            self.send_error(400)
            return
        CLAIMED.set()
        if STOP.is_set():
            self.reply(403, {"detail": "Smoke test finished."})
        else:
            self.reply(200, {"run_id": None})


def main():
    windows = os.name == "nt"
    launcher = ["cmd.exe", "/d", "/c", "start-collector.cmd"] if windows else ["./start-collector.sh"]
    python = ROOT / ".runtime" / "venv" / ("Scripts/python.exe" if windows else "bin/python")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "backend"), "PYTHONUTF8": "1"}

    with TemporaryDirectory(prefix="housing smoke ") as workdir, TemporaryFile() as output:
        with ThreadingHTTPServer(("127.0.0.1", 0), APIStub) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            args = ["--api-url", f"http://127.0.0.1:{server.server_port}/api",
                    "--profile-dir", str(Path(workdir) / "browser profile")]
            try:
                with subprocess.Popen(launcher + args, cwd=ROOT, env=env, stdout=output,
                                      stderr=subprocess.STDOUT) as collector:
                    try:
                        deadline = monotonic() + 180
                        while not CLAIMED.wait(0.5):
                            if collector.poll() is not None or monotonic() >= deadline:
                                raise RuntimeError("The collector did not start polling the local API.")
                        duplicate = subprocess.run([python, "-m", "app.local_browser", *args], cwd=ROOT,
                                                   env=env, capture_output=True, text=True,
                                                   encoding="utf-8", timeout=10, check=True)
                        assert "Локальный сборщик уже запущен." in duplicate.stdout
                    finally:
                        STOP.set()
                        try:
                            collector.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            collector.kill()
                    assert collector.returncode == 0
            finally:
                server.shutdown()
                thread.join()
                output.seek(0)
                print(output.read().decode("utf-8", errors="replace"))


if __name__ == "__main__":
    main()
