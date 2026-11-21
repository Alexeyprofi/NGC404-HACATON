#!/usr/bin/env python3
"""Веб-интерфейс для ИИ-помощника покупателя.

Отдаёт страницу index.html и пересылает историю диалога в исполняемый файл run
(тот же контракт, что и у проверяющего: JSON в stdin, JSON в stdout).

Пример запуска из корня проекта (после сборки):
  python3 web/server.py --binary build/run --catalog /abs/path/data/catalog.json

Затем откройте http://127.0.0.1:8080

Переменные LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_USER_AGENT читаются самим run
(из окружения или из .env рядом с ним / в текущей папке).
Сервер слушает только 127.0.0.1.
"""

import argparse
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_BODY_BYTES = 256 * 1024
RUN_TIMEOUT_SEC = 125  # контракт: до 120 секунд на запрос, с запасом на запуск


class ChatHandler(BaseHTTPRequestHandler):
    server_version = "AvitoAssistantUI/1.0"
    binary = None   # заполняется в main()
    catalog = None  # заполняется в main()

    def log_message(self, fmt, *args):
        sys.stderr.write("[web] " + (fmt % args) + "\n")

    def _send(self, code, data, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        else:
            self._json(404, {"error": "страница не найдена"})

    def do_POST(self):
        if self.path != "/api/chat":
            self._json(404, {"error": "страница не найдена"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(413, {"error": "некорректный размер запроса"})
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "тело запроса должно быть JSON"})
            return

        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            self._json(400, {"error": "ожидается объект с полем messages"})
            return

        stdin_bytes = json.dumps({"messages": payload["messages"]}, ensure_ascii=False).encode("utf-8")
        try:
            proc = subprocess.run(
                [self.binary, "--catalog", self.catalog],
                input=stdin_bytes,
                capture_output=True,
                timeout=RUN_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            self._json(504, {"error": "помощник не ответил за отведённое время"})
            return
        except OSError as e:
            self._json(500, {"error": f"не удалось запустить run: {e}"})
            return

        stderr_tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        if stderr_tail:
            sys.stderr.write("[run] " + " | ".join(stderr_tail) + "\n")

        if proc.returncode != 0:
            self._json(502, {"error": "сервис подбора вернул ошибку. Подробности в журнале сервера."})
            return

        try:
            result = json.loads(proc.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(502, {"error": "run вернул некорректный JSON"})
            return

        self._json(200, result)


def main():
    parser = argparse.ArgumentParser(description="Веб-интерфейс чата для run")
    parser.add_argument("--binary", default=str(HERE.parent / "build" / "run"),
                        help="путь к исполняемому файлу run")
    parser.add_argument("--catalog", required=True, help="путь к catalog.json")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    ChatHandler.binary = str(Path(args.binary).resolve())
    ChatHandler.catalog = str(Path(args.catalog).resolve())

    if not Path(ChatHandler.binary).is_file():
        sys.exit(f"[error] не найден исполняемый файл: {ChatHandler.binary}")
    if not Path(ChatHandler.catalog).is_file():
        sys.exit(f"[error] не найден каталог: {ChatHandler.catalog}")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), ChatHandler)
    print(f"Открой в браузере: http://127.0.0.1:{args.port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
