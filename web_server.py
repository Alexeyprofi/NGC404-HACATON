#!/usr/bin/env python3
"""Веб-сервер для фронтенда из static/ и C++-движка run.

- GET  /...          раздаёт файлы из static/ (index.html по умолчанию)
- POST /api/chat     принимает {"messages": [{"role": "user"|"assistant", "text": "..."}]},
                     запускает бинарник run в пакетном режиме и возвращает его JSON-ответ

Бинарник запускается с рабочей директорией, где лежат prompt.txt, system_l2.txt,
.env и data/ (по умолчанию build/).

Пример:
    python3 web_server.py --port 8000
"""

import argparse
import json
import subprocess
import sys
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class Handler(SimpleHTTPRequestHandler):
    # Настраивается через функцию make_handler ниже.
    engine_cmd: list[str] = []
    engine_cwd: Path = ROOT / "build"
    engine_timeout: float = 120.0

    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=directory, **kwargs)

    def do_POST(self):
        if self.path != "/api/chat":
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"status": "error", "message": "Некорректный JSON."})
            return

        if not isinstance(request, dict) or not isinstance(request.get("messages"), list):
            self._send_json(HTTPStatus.BAD_REQUEST, {"status": "error", "message": "Ожидается поле messages (список)."})
            return

        try:
            proc = subprocess.run(
                self.engine_cmd,
                input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
                capture_output=True,
                cwd=self.engine_cwd,
                timeout=self.engine_timeout,
            )
        except subprocess.TimeoutExpired:
            self._send_json(HTTPStatus.GATEWAY_TIMEOUT, {"status": "error", "message": "Движок не ответил вовремя."})
            return
        except OSError as exc:
            sys.stderr.write(f"[web] не удалось запустить движок: {exc}\n")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"status": "error", "message": "Движок не запущен."})
            return

        if proc.stderr:
            sys.stderr.write(proc.stderr.decode("utf-8", errors="replace"))

        if proc.returncode != 0:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"status": "error", "message": "Движок завершился с ошибкой."})
            return

        try:
            payload = json.loads(proc.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_GATEWAY, {"status": "error", "message": "Движок вернул некорректный ответ."})
            return

        self._send_json(HTTPStatus.OK, payload)

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("[web] " + (fmt % args) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Веб-сервер для static/ и движка run")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--static-dir", type=Path, default=ROOT / "static")
    parser.add_argument("--binary", type=Path, default=ROOT / "build" / "run",
                        help="путь к исполняемому run")
    parser.add_argument("--workdir", type=Path, default=ROOT / "build",
                        help="рабочая директория движка (prompt.txt, .env, data/)")
    parser.add_argument("--catalog", default="data/catalog.json",
                        help="путь к каталогу относительно --workdir")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    binary = args.binary.resolve()
    workdir = args.workdir.resolve()
    static_dir = args.static_dir.resolve()

    if not binary.is_file():
        sys.exit(f"[fatal] не найден исполняемый файл: {binary}")
    if not (workdir / args.catalog).is_file():
        sys.exit(f"[fatal] не найден каталог: {workdir / args.catalog}")
    if not static_dir.is_dir():
        sys.exit(f"[fatal] не найдена папка фронтенда: {static_dir}")

    Handler.engine_cmd = [str(binary), "--catalog", args.catalog]
    Handler.engine_cwd = workdir
    Handler.engine_timeout = args.timeout

    class BoundHandler(Handler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(static_dir), **kw)

    server = ThreadingHTTPServer((args.host, args.port), BoundHandler)
    print(f"[web] фронтенд:  http://{args.host}:{args.port}/", file=sys.stderr)
    print(f"[web] движок:    {binary} (cwd={workdir})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
