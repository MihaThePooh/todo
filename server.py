#!/usr/bin/env python3
"""Tiny store behind the to-do page. Two operations, nothing else.

    GET  /api/todo   -> current JSON
    PUT  /api/todo   -> replace it

Auth: Bearer token, read from token-file. Listens on localhost only;
nginx terminates TLS in front of it.

Writes are atomic (temp file + rename) and every previous version is
kept, so a bad save can be undone.
"""
import base64
import hashlib
import json
import os
import pathlib
import re
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = pathlib.Path(__file__).resolve().parent
# Папка данных и порт берутся из окружения: иначе тесты пришлось бы
# гонять на боевом списке дел, а это ровно один неверный запрос до
# потери настоящих задач.
DATA = pathlib.Path(os.environ.get("TODO_DATA") or (BASE / "data"))
STORE = DATA / "todo.json"
LISTS = DATA / "lists"
BACKUPS = DATA / "backups"
TOKEN_FILE = DATA / "token" if os.environ.get("TODO_DATA") else BASE / "token"
HOST = "127.0.0.1"
PORT = int(os.environ.get("TODO_PORT") or 8787)
KEEP_BACKUPS = 20
MAX_BODY = 2 * 1024 * 1024

# Ключ — 64 шестнадцатеричных символа, ровно как их выдаёт клиент.
KEY_RE = re.compile(r"^[0-9a-f]{64}$", re.I)
# Потолок на число чужих списков: страница открыта всему интернету,
# и без него любой желающий забьёт диск пустыми списками.
MAX_LISTS = 300

# Создание нового списка — единственная операция, которую может позвать
# кто угодно. Её и ограничиваем: три штуки в сутки с адреса. Работа
# с уже существующим списком не ограничена ничем, иначе сломались бы
# обычные сохранения.
RATE_FILE = DATA / "rate.json"
RATE_LIMIT = 3
RATE_WINDOW = 24 * 60 * 60
# Запас на случай, если реальный адрес до нас не доходит (nginx без
# X-Forwarded-For): тогда все запросы сливаются в одну корзину, и без
# общего потолка лимит не значил бы ничего.
RATE_GLOBAL = 60
RATE_LOCK = threading.Lock()

KEYS = ("deadline", "free", "ideas", "done", "archive", "bin")
NAMED = ("deadline", "free", "ideas")   # переименовать можно только эти

# Разделы, заведённые самим человеком. Их имена приходят от клиента, и
# принимать любую строку нельзя: имя раздела становится ключом в
# документе, а рядом лежат служебные поля (version, updated, names).
# Поэтому форма ключа жёсткая — «x» и восемь шестнадцатеричных цифр,
# ни с чем нашим не пересекается. Порядок разделов задаёт список extra.
EXTRA_RE = re.compile(r"^x[0-9a-f]{8}$")
MAX_EXTRA = 20          # столько своих разделов хватит любому списку

# Договор о форме данных. Сервер принимает ровно эти поля и ровно этих
# типов, остальное выбрасывает. Не потому, что чужое поле опасно само по
# себе, а потому, что своё поле НЕ ТОГО ТИПА ломает страницу: приложение
# ждёт от раздела список и перебирает его. Придёт строка — перебор
# рухнет, и список не нарисуется вообще.
MAX_TEXT = 200000       # символов в одном деле, это около ста страниц
MAX_TASKS = 2000        # дел в одном разделе
MAX_NAME = 40           # символов в названии раздела
MAX_ID = 40

# Единственные файлы, кроме страницы, которые сервер отдаёт наружу.
ICON = "image/png"
STATIC = {
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/icons/icon-192.png": ("icons/icon-192.png", ICON),
    "/icons/icon-512.png": ("icons/icon-512.png", ICON),
    "/icons/icon-mask.png": ("icons/icon-mask.png", ICON),
    "/favicon.ico": ("icons/icon-192.png", ICON),
    # Игра Алисы. Лежит рядом ссылкой на ~/tools/alisa-game/game.html,
    # чтобы правка игры сразу оказывалась на адресе, без копий.
    "/game": ("game.html", "text/html; charset=utf-8"),
}
EMPTY = dict({"version": 1, "updated": ""}, **{k: [] for k in KEYS})


def extras(doc):
    """Ключи своих разделов из документа: по порядку, без повторов."""
    out = []
    arr = doc.get("extra")
    if isinstance(arr, list):
        for name in arr:
            if (isinstance(name, str) and EXTRA_RE.match(name)
                    and name not in out):
                out.append(name)
    return out[:MAX_EXTRA]


def offs(doc):
    """Встроенные разделы, которые человек убрал. Сами данные при этом
    остаются полями документа — просто пустыми: удалять ключ у встроенного
    раздела нельзя, на него завязана форма документа."""
    out = []
    arr = doc.get("off")
    if isinstance(arr, list):
        for name in arr:
            if name in NAMED and name not in out:
                out.append(name)
    return out


def ordering(doc):
    """Порядок разделов на экране. Встроенные и свои лежат вперемешку:
    человек волен поставить свой раздел первым. Разделы, которых нет,
    из порядка выпадают — он не хранилище, а расстановка."""
    out = []
    arr = doc.get("order")
    if isinstance(arr, list):
        for name in arr:
            if (isinstance(name, str) and name not in out
                    and (name in NAMED or EXTRA_RE.match(name))):
                out.append(name)
    return out[:len(NAMED) + MAX_EXTRA]


def clean_task(task, known=KEYS):
    """Одно дело в том виде, в каком его понимает приложение."""
    if not isinstance(task, dict):
        return None
    tid = task.get("id")
    if not isinstance(tid, str) or not tid or len(tid) > MAX_ID:
        return None
    text = task.get("text")
    out = {"id": tid, "text": text[:MAX_TEXT] if isinstance(text, str) else ""}
    if task.get("star") is True:
        out["star"] = True
    for key in ("doneAt", "delAt"):
        val = task.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            out[key] = int(val)
    # Откуда дело попало в корзину — чтобы стрелка «вернуть» положила его
    # обратно в свой раздел. binFrom пишет нынешнее приложение, from —
    # его ранняя версия; вторая форма осталась в старых записях.
    for key in ("binFrom", "from"):
        if task.get(key) in known:
            out[key] = task[key]

    # Место, с которого дело унесли в «сделано» или в корзину: id соседа
    # сверху (пустая строка — стояло первым) и номер по списку про запас.
    # Приложение кладёт дело обратно ровно туда, а не в начало.
    aft = task.get("aft")
    if isinstance(aft, str) and len(aft) <= MAX_ID:
        out["aft"] = aft
    at = task.get("at")
    if isinstance(at, int) and not isinstance(at, bool) and 0 <= at <= MAX_TASKS:
        out["at"] = at

    # Целый раздел, уехавший в корзину. Лежит там наравне с делами, но
    # несёт их внутри: sec — ключ раздела, text — его название, rows —
    # дела, которые в нём были. Вложенность ровно на один уровень:
    # раздела внутри раздела не бывает, и рекурсии здесь нет.
    sec = task.get("sec")
    if isinstance(sec, str) and (sec in NAMED or EXTRA_RE.match(sec)):
        out["sec"] = sec
        out["text"] = out["text"][:MAX_NAME]
        rows = []
        arr = task.get("rows")
        if isinstance(arr, list):
            for item in arr[:MAX_TASKS]:
                row = clean_task(item, known)
                if row is not None:
                    row.pop("sec", None)
                    row.pop("rows", None)
                    rows.append(row)
        out["rows"] = rows
    return out


def clean_doc(doc):
    """Документ целиком по тому же договору. Заодно следит, чтобы одно
    дело не оказалось сразу в двух разделах."""
    out = {"version": 1, "updated": str(doc.get("updated", ""))[:40]}
    own = extras(doc)
    known = tuple(KEYS) + tuple(own)
    seen = set()
    for name in known:
        rows = []
        arr = doc.get(name)
        if isinstance(arr, list):
            for task in arr[:MAX_TASKS]:
                item = clean_task(task, known)
                if item and item["id"] not in seen:
                    seen.add(item["id"])
                    # Дела внутри раздела, уехавшего в корзину, считаются
                    # наравне с остальными: то, что уже лежит в живом
                    # разделе, внутрь копией не попадёт. Корзина идёт
                    # последней, поэтому к этому месту живые уже учтены.
                    if "rows" in item:
                        kept = []
                        for row in item["rows"]:
                            if row["id"] not in seen:
                                seen.add(row["id"])
                                kept.append(row)
                        item["rows"] = kept
                    rows.append(item)
        out[name] = rows
    # Раздел, которого нет в extra, не хранится: список ключей — источник
    # правды, а не набор оставшихся полей.
    if own:
        out["extra"] = own
    gone = offs(doc)
    if gone:
        out["off"] = gone
    seq = [n for n in ordering(doc) if n in NAMED or n in own]
    if seq:
        out["order"] = seq
    names = doc.get("names")
    if isinstance(names, dict):
        keep = {}
        for key, val in names.items():
            if key in NAMED + tuple(own) and isinstance(val, str) and val.strip():
                keep[key] = val.strip()[:MAX_NAME]
        if keep:
            out["names"] = keep
    return out


def apply_patch(current, patch):
    """Новый документ из старого и присланных изменений.

    Клиент шлёт для каждого затронутого раздела порядок id и только те
    дела, чьё содержимое изменилось. Остальные сервер берёт у себя по
    id — поэтому вклеенная в дело статья не улетает по сети заново на
    каждую поставленную галочку.
    """
    # Набор разделов берём из присланного, если он там есть: раздел
    # могли завести этим же запросом, и его список дел должен приехать
    # вместе с ним, а не следующим.
    own = extras(patch) if "extra" in patch else extras(current)
    known = tuple(KEYS) + tuple(own)

    index = {}
    for name in known:
        for task in current.get(name, []):
            if isinstance(task, dict) and isinstance(task.get("id"), str):
                index[task["id"]] = task

    out = dict(current)
    if "extra" in patch:
        out["extra"] = own
    if "off" in patch:
        out["off"] = offs(patch)
    if "order" in patch:
        out["order"] = ordering(patch)
    lists = patch.get("lists")
    for name, part in (lists.items() if isinstance(lists, dict) else []):
        if name not in known or not isinstance(part, dict):
            continue
        ids = part.get("ids")
        if not isinstance(ids, list):
            continue
        tasks = part.get("tasks")
        tasks = tasks if isinstance(tasks, dict) else {}
        rows = []
        for tid in ids[:MAX_TASKS]:
            if not isinstance(tid, str):
                continue
            task = tasks.get(tid, index.get(tid))
            if task is not None:
                rows.append(task)
        out[name] = rows
    if "names" in patch:
        out["names"] = patch["names"]
    return out


def csp(body):
    """Политика безопасности для страницы.

    Скрипт лежит внутри самого документа, поэтому разрешаем его по
    отпечатку: браузер сам посчитает sha256 от содержимого <script> и
    сверит. Любой чужой скрипт — хоть внешний, хоть подставленный
    в разметку — в список не попадёт и выполнен не будет.
    """
    marks = []
    for m in re.finditer(rb"<script>(.*?)</script>", body, re.S):
        marks.append("'sha256-%s'"
                     % base64.b64encode(hashlib.sha256(m.group(1)).digest()).decode())
    return ("default-src 'self'; "
            "script-src " + (" ".join(marks) or "'none'") + "; "
            # воркер и манифест берутся файлами со своего же адреса
            "worker-src 'self'; manifest-src 'self'; "
            # стили правятся из кода (это CSP не касается), но в разметке
            # есть style="" у заглушек — поэтому здесь послабление
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def token():
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


def store_for(key):
    """Файл списка. Свой ключ — прежний todo.json, чужой — отдельный
    файл с именем из хеша: сам ключ в именах файлов не светится."""
    key = key.lower()
    if key == token().lower():
        return STORE
    return LISTS / (hashlib.sha256(key.encode()).hexdigest() + ".json")


def load(path=STORE):
    if not path.exists():
        return dict(EMPTY)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        # Файл мог быть записан старой версией — дозаполняем новые списки.
        for key in KEYS:
            doc.setdefault(key, [])
        return doc
    except Exception:
        # Never hand back garbage: fall back to empty, keep the broken file.
        broken = DATA / f"{path.stem}.broken.{int(time.time())}.json"
        shutil.copy2(path, broken)
        return dict(EMPTY)


def save(doc, path=STORE):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Историю версий держим только для своего списка: чужих может быть
    # много, и каждая правка съедала бы диск копиями.
    if path == STORE and STORE.exists():
        BACKUPS.mkdir(parents=True, exist_ok=True)
        shutil.copy2(STORE, BACKUPS / f"todo.{int(time.time())}.json")
        old = sorted(BACKUPS.glob("todo.*.json"))
        for f in old[:-KEEP_BACKUPS]:
            f.unlink(missing_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)  # atomic: readers never see a half-written file


def who(ip):
    """Адрес не храним: только его отпечаток, посоленный ключом. Файл
    лимитов сам по себе не сообщает, кто именно заходил."""
    return hashlib.sha256((token() + "|" + ip).encode()).hexdigest()[:16]


def rate_ok(ip):
    """Можно ли этому адресу завести ещё один список.

    Возвращает (можно, через сколько секунд можно). Всё хранится в одном
    маленьком файле: заводить ради счётчика базу — перебор."""
    now = int(time.time())
    with RATE_LOCK:
        try:
            log = json.loads(RATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            log = {}

        # Чистим прошлые сутки целиком, заодно не давая файлу расти.
        for k in list(log):
            log[k] = [t for t in log[k] if now - t < RATE_WINDOW]
            if not log[k]:
                del log[k]

        mine = log.get(who(ip), [])
        total = sum(len(v) for v in log.values())
        if len(mine) >= RATE_LIMIT:
            return False, RATE_WINDOW - (now - min(mine))
        if total >= RATE_GLOBAL:
            return False, RATE_WINDOW - (now - min(t for v in log.values() for t in v))

        log.setdefault(who(ip), []).append(now)
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = RATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(log), encoding="utf-8")
        os.replace(tmp, RATE_FILE)
        return True, 0


class Handler(BaseHTTPRequestHandler):
    server_version = "todo/1.0"

    def log_message(self, fmt, *args):
        pass  # the token travels in headers; keep it out of any log

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def ip(self):
        """Настоящий адрес клиента. Сам сокет всегда показывает 127.0.0.1:
        перед нами nginx, поэтому берём первый адрес из X-Forwarded-For —
        его подставляет наш же nginx, всё правее могли подделать."""
        fwd = self.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0]

    def _key(self):
        """Ключ из заголовка или None. Проверять его «правильность»
        не с чем: любой ключ нужного вида открывает свой собственный
        список. Угадать чужой — это подобрать 256 бит."""
        got = self.headers.get("Authorization", "")
        if not got.startswith("Bearer "):
            return None
        k = got[7:].strip()
        return k if KEY_RE.match(k) else None

    def _file(self, rel, ctype, cache):
        # Отдаём сами: домашний каталог закрыт для nginx, а так файлы
        # можно обновлять без root.
        try:
            body = (BASE / rel).read_bytes()
        except OSError:
            return self._send(404, {"error": "not found"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # Встраивать приложение в чужую страницу нельзя: иначе поверх
        # него можно положить свои кнопки и ловить чужие нажатия.
        self.send_header("X-Frame-Options", "DENY")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", csp(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        # Белый список путей: имя файла никогда не берётся из запроса,
        # поэтому выйти из каталога подсунутым «../» невозможно.
        if path in ("/", "/index.html"):
            return self._file("index.html", "text/html; charset=utf-8", "no-cache")
        if path in STATIC:
            rel, ctype = STATIC[path]
            return self._file(rel, ctype, "no-cache")
        if self.path != "/api/todo":
            return self._send(404, {"error": "not found"})
        key = self._key()
        if not key:
            return self._send(401, {"error": "unauthorized"})
        # Файла ещё нет — это новый список, а не ошибка.
        doc = load(store_for(key))
        # Метка версии протокола. По ней страница понимает, что сервер
        # умеет принимать изменения, а не только документ целиком.
        # Со старым сервером метки нет — и страница шлёт по-старому.
        #   2 — принимает изменения диффом
        #   3 — знает свои разделы (extra)
        #   4 — хранит порядок разделов (order)
        #   5 — помнит место дела в списке (aft, at)
        # Метка нужна и для порядка выкладки: страница обновляется на
        # диске сразу, а сервис перезапускается руками. Пока метки нет,
        # новая страница просто не показывает то, чего сервер не поймёт.
        doc["proto"] = 5
        self._send(200, doc)

    def do_PUT(self):
        if self.path != "/api/todo":
            return self._send(404, {"error": "not found"})
        key = self._key()
        if not key:
            return self._send(401, {"error": "unauthorized"})
        path = store_for(key)
        if not path.exists() and path != STORE:
            LISTS.mkdir(parents=True, exist_ok=True)
            if len(list(LISTS.glob("*.json"))) >= MAX_LISTS:
                return self._send(507, {"error": "no room for new lists"})
            allowed, wait = rate_ok(self.ip())
            if not allowed:
                return self._send(429, {"error": "too many new lists",
                                        "retry_after": wait})

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self._send(400, {"error": "bad length"})
        try:
            doc = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return self._send(400, {"error": "bad json"})
        if not isinstance(doc, dict):
            return self._send(400, {"error": "bad shape"})

        current = load(path)
        # Optimistic locking: refuse to overwrite a newer version than the
        # one the client started from. Prevents phone edits being silently
        # wiped by a stale laptop tab.
        base = doc.pop("base", None)
        if current.get("updated") and base != current.get("updated"):
            return self._send(409, {"error": "conflict", "current": current})

        # Присланы изменения — собираем документ из них и того, что
        # уже лежит. Без "lists" считаем, что прислали документ целиком:
        # так умеют старые вкладки, и так заводится новый список.
        if isinstance(doc.get("lists"), dict):
            doc = apply_patch(current, doc)
        doc = clean_doc(doc)
        doc["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        save(doc, path)
        self._send(200, {"ok": True, "updated": doc["updated"]})


if __name__ == "__main__":
    DATA.mkdir(parents=True, exist_ok=True)
    if not TOKEN_FILE.exists():
        TOKEN_FILE.write_text(os.urandom(32).hex(), encoding="utf-8")
        TOKEN_FILE.chmod(0o600)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
