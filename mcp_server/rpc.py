"""Транспорт до Django-шару: `docker compose exec -T web manage.py mcp_rpc`.

Чому не прямий доступ до БД з хоста: воркери, Telethon-сесії, проксі й
налаштування живуть У КОНТЕЙНЕРІ. Інструмент, що ходить у Telegram звідти,
бачить рівно те саме, що й стадії конвеєра, — інакше діагноз «проксі жива»
означав би лише «жива з ноутбука».

Ціль керування (локальний docker / прод по ssh) налаштовується змінними
середовища — див. README.md.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARK_BEGIN = "<<<MCP-RESULT-BEGIN>>>"
MARK_END = "<<<MCP-RESULT-END>>>"


class RpcError(RuntimeError):
    """Транспорт не зміг виконати виклик (контейнер лежить, ssh, таймаут)."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class Target:
    """Куди керуємо: каталог проєкту, набір compose-файлів, опційний ssh-хост."""

    def __init__(self):
        self.ssh = _env("TGA_SSH")
        self.dir = _env("TGA_DIR") or ("/opt/tg-event-analytics" if self.ssh else str(ROOT))
        self.files = [f for f in _env("TGA_COMPOSE_FILES", "docker-compose.yml").split(":") if f]
        self.web = _env("TGA_WEB_SERVICE", "web")
        self.timeout = int(_env("TGA_TIMEOUT", "240"))
        self.readonly = _env("TGA_READONLY").lower() in ("1", "true", "yes")

    @property
    def label(self) -> str:
        where = f"ssh {self.ssh}:{self.dir}" if self.ssh else self.dir
        return f"{where} [{'+'.join(self.files)}]" + (" (лише читання)" if self.readonly else "")

    def compose(self, *args: str) -> list[str]:
        cmd = ["docker", "compose"]
        for f in self.files:
            cmd += ["-f", f]
        return cmd + list(args)

    def run(self, argv: list[str], stdin: str = "", timeout: int | None = None):
        """Виконати команду в каталозі цілі (локально або через ssh)."""
        timeout = timeout or self.timeout
        if self.ssh:
            remote = f"cd {shlex.quote(self.dir)} && " + " ".join(shlex.quote(a) for a in argv)
            argv = ["ssh", self.ssh, remote]
            cwd = None
        else:
            cwd = self.dir
        try:
            return subprocess.run(argv, cwd=cwd, input=stdin, capture_output=True,
                                  text=True, timeout=timeout)
        except FileNotFoundError as e:
            raise RpcError(f"немає бінарника: {e}")
        except subprocess.TimeoutExpired:
            raise RpcError(f"таймаут {timeout}с: {' '.join(argv[:6])}… "
                           "(довга мережева перевірка? звузь вибірку або підніми TGA_TIMEOUT)")


TARGET = Target()


def call(tool: str, payload: dict | None = None, timeout: int | None = None) -> str:
    """Викликати інструмент у контейнері й повернути готовий текст."""
    argv = TARGET.compose("exec", "-T")
    if TARGET.readonly:
        argv += ["-e", "MCP_READONLY=1"]
    argv += [TARGET.web, "python", "manage.py", "mcp_rpc", tool]
    proc = TARGET.run(argv, stdin=json.dumps(payload or {}, ensure_ascii=False),
                      timeout=timeout)
    out = proc.stdout or ""
    if MARK_BEGIN not in out:
        tail = (proc.stderr or out or "").strip()[-1200:]
        raise RpcError(f"виклик `{tool}` не дав результату (код {proc.returncode}). "
                       f"Ціль: {TARGET.label}\n{tail or 'порожній вивід'}")
    body = out.split(MARK_BEGIN, 1)[1].split(MARK_END, 1)[0].strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise RpcError(f"зіпсована відповідь `{tool}`: {e}\n{body[:500]}")
    if not data.get("ok"):
        err = data.get("error", "невідома помилка")
        if data.get("traceback"):
            err += "\n\n" + data["traceback"]
        return f"⚠ {err}"
    return data.get("text", "")


# --- маніфест інструментів (з кешем на випадок, коли стек лежить) -----------

SNAPSHOT = Path(__file__).resolve().parent / "tools.json"


def manifest(refresh: bool = True) -> list[dict]:
    """Опис інструментів Django-шару; при недоступності стека — знімок із репо."""
    if refresh:
        try:
            argv = TARGET.compose("exec", "-T", TARGET.web, "python", "manage.py",
                                  "mcp_rpc", "--list")
            proc = TARGET.run(argv, stdin="", timeout=60)
            body = (proc.stdout or "").split(MARK_BEGIN, 1)[1].split(MARK_END, 1)[0]
            data = json.loads(body)["manifest"]
            SNAPSHOT.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                encoding="utf-8")
            return data
        except Exception:  # noqa: BLE001 — падати без стека не можна: віддамо знімок
            pass
    if SNAPSHOT.exists():
        return json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    raise RpcError("не вдалось отримати список інструментів і немає знімка tools.json")
