"""The interface's API as a table: one line per endpoint says its path, what it needs and when it may run.

Every handler gets a `Call`: the app, the checked request body (or query) and the helpers that start a
background operation. Checks that used to be repeated by hand are declared once:

    needs={"name": "str", "id": "int", "mode": "enum:main|both|reserve", "devices": "int?"}
    idle=True     — refuse while another operation runs (it would race with it)
    busy=True     — GET only: answer {"busy": True} while an operation runs (the data would be half-written)

A field ending in "?" is optional; a value that does not fit is refused with a message for the person,
before anything is touched.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

Log = Callable[[str], None]

# Field names as the person sees them in the forms.
LABELS = {"name": "имя", "host": "адрес сервера", "domain": "домен", "mode": "режим", "id": "номер", "version": "версия",
          "server": "сервер", "devices": "устройств", "ips": "IP одновременно", "confirm": "подтверждение"}
BUSY_MSG = "уже выполняется другая операция — дождитесь её окончания"


class NotFound(Exception):
    pass


def _label(key: str) -> str:
    return LABELS.get(key, key)


def check(body: Dict[str, Any], needs: Dict[str, str]) -> Dict[str, Any]:
    """Validate and normalise the declared fields in place; other fields pass through untouched."""
    for key, kind in needs.items():
        optional = kind.endswith("?")
        kind = kind.rstrip("?")
        value = body.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            if optional:
                continue
            raise ValueError(f"не заполнено поле «{_label(key)}»")
        if kind == "str":
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                raise ValueError(f"поле «{_label(key)}» должно быть текстом")
            body[key] = str(value).strip()
        elif kind == "int":
            try:
                number = int(str(value).strip())
            except ValueError:
                raise ValueError(f"поле «{_label(key)}» должно быть целым числом") from None
            if number < 0:
                raise ValueError(f"поле «{_label(key)}» не может быть отрицательным")
            body[key] = number
        elif kind == "bool":
            body[key] = bool(value)
        elif kind.startswith("enum:"):
            options = kind[5:].split("|")
            if value not in options:
                raise ValueError(f"поле «{_label(key)}»: допустимо {', '.join(options)}")
        else:  # a typo in the table must fail loudly in tests, not quietly accept anything
            raise AssertionError(f"неизвестный тип поля {kind!r}")
    return body


def req(body: Dict[str, Any], key: str) -> str:
    """One required text field (for handlers that read a field conditionally)."""
    return check(body, {key: "str"})[key]


def ssh_fields(body: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    ssh: Dict[str, Any] = {}
    if body.get("sshUser"):
        ssh["user"] = body["sshUser"]
    if body.get("sshPort"):
        ssh["port"] = check({"sshPort": body["sshPort"]}, {"sshPort": "int"})["sshPort"]
    if body.get("sshKey"):
        ssh["key"] = body["sshKey"]
    return ssh, (body.get("sshPassword") or None)


@dataclass
class Call:
    app: Any
    body: Dict[str, Any]
    path: str

    def run(self, title: str, fn: Callable[[Log], Any]) -> Dict[str, Any]:
        """Start a background operation; the page follows its log."""
        if not self.app.job.start(title, fn, scope=str(self.app.path)):
            raise RuntimeError(BUSY_MSG)
        return {"ok": True}

    def run_change(self, title: str, change: Callable[[], Any], work: Callable[[Log], Any]) -> Dict[str, Any]:
        """A setting is written to fleet.yaml and then applied. If applying fails, the old file comes
        back: the app never shows a setting the servers do not have."""
        app = self.app
        if app.job.running:
            raise RuntimeError(BUSY_MSG)
        before = app.path.read_text(encoding="utf-8")
        change()
        after = app.path.read_text(encoding="utf-8")

        def guarded(log: Log) -> Any:
            try:
                return work(log)
            except Exception:
                if app.path.read_text(encoding="utf-8") == after:  # untouched since (e.g. by sync)
                    app.path.write_text(before, encoding="utf-8")
                    log("настройка не сохранена: применить её не удалось, вернул прежнюю. "
                        "Если часть изменений успела дойти до серверов, «Применить» вернёт их к прежним настройкам.")
                raise
        return self.run(title, guarded)


@dataclass
class Route:
    fn: Callable[[Call], Any]
    needs: Dict[str, str] = field(default_factory=dict)
    idle: bool = False
    busy: bool = False


GET: Dict[str, Route] = {}
POST: Dict[str, Route] = {}
PREFIX_POST: List[Tuple[str, Route]] = []


def get(path: str, busy: bool = False, needs: Optional[Dict[str, str]] = None):
    def deco(fn):
        GET[path] = Route(fn, needs or {}, busy=busy)
        return fn
    return deco


def post(*paths: str, needs: Optional[Dict[str, str]] = None, idle: bool = False, prefix: bool = False):
    def deco(fn):
        for p in paths:
            route = Route(fn, needs or {}, idle=idle)
            if prefix:
                PREFIX_POST.append((p, route))
            else:
                POST[p] = route
        return fn
    return deco


def dispatch(app: Any, method: str, path: str, body: Dict[str, Any]) -> Any:
    """Find the route, check it may run now and that its fields are right, then call it."""
    if method == "GET":
        route = GET.get(path)
    else:
        route = POST.get(path) or next((r for p, r in PREFIX_POST if path.startswith(p)), None)
    if route is None:
        raise NotFound(path)
    if route.busy and app.job.running:
        return {"busy": True}
    if route.idle and app.job.running:
        raise RuntimeError(BUSY_MSG)
    return route.fn(Call(app, check(dict(body), route.needs), path))


# ---------------------------------------------------------------- reading
@get("/api/overview")
def _overview(c: Call):
    return c.app.overview()


@get("/api/live")
def _live(c: Call):
    return c.app.live()


@get("/api/job")
def _job(c: Call):
    return c.app.job.view()


@get("/api/jobs")
def _jobs(c: Call):
    """Earlier operations, newest first, without their logs (those come one at a time)."""
    return {"jobs": c.app.job.history(str(c.app.path))}


@get("/api/jobs/one", needs={"id": "int"})
def _job_one(c: Call):
    found = c.app.job.past(c.body["id"], str(c.app.path))
    if found is None:
        raise ValueError("эта операция уже не хранится")
    return found


@get("/api/versions")
def _versions(c: Call):
    from . import __version__
    from . import update as upd
    st = upd.settings()
    out = {"current": __version__, "channel": st["channel"], "pinned": st["pinned"], "repo": upd.REPO, "auto": st["auto"],
           "releases": [], "error": ""}
    try:
        rels = upd.releases(fresh=bool(c.body.get("fresh")))
        out["releases"] = [{k: r[k] for k in ("version", "prerelease", "date", "notes", "page")} for r in rels]
        stable, pre = upd.latest(rels, False), upd.latest(rels, True)
        out["latest"] = stable["version"] if stable else ""
        out["latestPre"] = pre["version"] if pre and pre["prerelease"] else ""
    except Exception as exc:
        out["error"] = f"не удалось получить список версий с GitHub: {exc}"
    return out


@get("/api/sync")
def _sync_status(c: Call):
    from .sync import Sync
    app, servers = c.app, []
    if app.path.exists():
        cfg = app.fleet().cfg
        servers = [{"name": cfg.master.name, "host": cfg.master.host, "title": "Главный сервер"}] + [
            {"name": n.name, "host": n.host, "title": n.label or n.name} for n in cfg.nodes]
    return dict(Sync(app.path).status(), servers=servers)


@get("/api/version")
def _version(c: Call):
    from . import __version__
    from .service import code_version
    from .ui import WHATS_NEW
    return {"version": code_version(), "app": __version__, "whatsNew": WHATS_NEW, "data": c.app.data_mark(),
            **c.app.update_note()}


@get("/api/master/probe", busy=True)
def _master_probe(c: Call):
    return c.app.fleet().master_probe()


@get("/api/inbounds", busy=True)
def _inbounds(c: Call):
    return {"inbounds": c.app.fleet().inbounds_view()}


@get("/api/users/stats", busy=True)
def _user_stats(c: Call):
    return {"stats": c.app.fleet().user_stats()}


@get("/api/user", needs={"name": "str"})
def _user(c: Call):
    return c.app.fleet().user_detail(c.body["name"])


@get("/api/panels", busy=True)
def _panels(c: Call):
    return {"panels": c.app.fleet().panels()}


@get("/api/backups")
def _backups(c: Call):
    fleet = c.app.fleet()
    return {"backups": [b.view() for b in fleet.backups()], "dir": str(fleet.backup_dir)}


# ---------------------------------------------------------------- servers and settings
@post("/api/profile/", prefix=True)
def _profile(c: Call):
    return c.app.profile_change(c.path[len("/api/profile/"):], c.body)


@post("/api/setup")
def _setup(c: Call):
    c.app.setup(c.body)
    return c.run("Настройка главного сервера", lambda log: c.app.fleet(log).apply())


@post("/api/access", idle=True)
def _access(c: Call):
    node = c.app.set_access(c.body)
    only = {node} if node else None
    return c.run(f"Подключение к серверу {c.body.get('name')}", lambda log: c.app.fleet(log).apply(only))


@post("/api/panel/password", needs={"name": "str"})
def _panel_password(c: Call):
    name = c.body["name"]
    return c.run(f"Новый пароль панели {name}", lambda log: c.app.fleet(log).reset_panel_password(name))


@post("/api/apply")
def _apply(c: Call):
    return c.run("Применение настроек", lambda log: c.app.fleet(log).apply())


@post("/api/plan")
def _plan(c: Call):
    def plan(log: Log) -> Any:
        report = c.app.fleet(log).apply(dry_run=True)
        c.app.job.preview = list(report.changes)  # the page offers «Применить» for exactly this
        return report
    return c.run("Что изменит «Применить»", plan)


@post("/api/upgrade")
def _upgrade(c: Call):
    return c.run("Обновление 3x-ui", lambda log: c.app.fleet(log).upgrade())


@post("/api/check")
def _check(c: Call):
    return c.run("Проверка подключений", lambda log: c.app.fleet(log).check_links(c.body.get("user") or None))


@post("/api/node", idle=True)
def _node_add(c: Call):
    name = c.app.add_node(c.body)
    return c.run(f"Добавление сервера {name}", lambda log: c.app.fleet(log).apply({name}))


@post("/api/node/label", needs={"name": "str"})
def _node_label(c: Call):
    from .config import set_node_label
    b, name = c.body, c.body["name"]
    hide = [k for k in ("direct", "xhttp", "hy2", "warp", "cascade", "cascade_xhttp", "cascade_hy2", "cascade_warp")
            if b.get("show_" + k) is False]
    return c.run_change(f"Название сервера {name}", lambda: set_node_label(c.app.path, name, b.get("label"), b.get("flag"), hide),
                        lambda log: c.app.fleet(log).apply({name}, skip_provision=True))


@post("/api/node/remove", needs={"name": "str", "purge": "bool?"})
def _node_remove(c: Call):
    name, purge = c.body["name"], bool(c.body.get("purge"))
    return c.run(f"Удаление сервера {name}" + (" вместе с 3x-ui" if purge else ""), lambda log: c.app.remove_node(name, log, purge))


@post("/api/master/remove")
def _master_remove(c: Call):
    master = c.app.fleet().cfg.master
    if " ".join(str(c.body.get("confirm") or "").split()).lower() != master.name.lower():
        raise ValueError(f"для подтверждения введите имя главного сервера: {master.name}")
    return c.run("Удаление главного сервера" + (" вместе с 3x-ui" if c.body.get("purge") else ""),
                 lambda log: c.app.remove_master(c.body, log))


@post("/api/master/role")
def _master_role(c: Call):
    from .config import set_master_role
    b = c.body
    if not b.get("vpn") and not b.get("cascades"):
        raise ValueError("главный сервер должен выполнять хотя бы одну роль")
    hide = [k for k in ("direct", "xhttp", "hy2", "warp") if b.get("show_" + k) is False]
    return c.run_change("Роль главного сервера",
                        lambda: set_master_role(c.app.path, bool(b.get("vpn")), bool(b.get("cascades")), b.get("label"), b.get("flag"), hide),
                        lambda log: c.app.fleet(log).apply())


@post("/api/master/addresses", needs={"mode": "enum:main|both|reserve"})
def _master_addresses(c: Call):
    from .config import set_address_mode
    mode = c.body["mode"]
    title = {"main": "Основной домен", "both": "Основной и резервный домены", "reserve": "Только резервный домен"}[mode]
    return c.run_change(f"Адреса подключений: {title.lower()}", lambda: set_address_mode(c.app.path, mode),
                        lambda log: c.app.fleet(log).apply(skip_provision=True))


@post("/api/domain", needs={"domain": "str"})
def _domain(c: Call):
    domain = c.body["domain"]
    return c.run(f"Домен подписки {domain}", lambda log: c.app.fleet(log).set_sub_domain(domain))


@post("/api/domain/reserve", needs={"domain": "str?"})
def _domain_reserve(c: Call):
    domain = c.body.get("domain") or ""
    return c.run(f"Резервный домен {domain}" if domain else "Удаление резервного домена",
                 lambda log: c.app.fleet(log).set_reserve_domain(domain))


@post("/api/inbound/edit", "/api/inbound/delete", needs={"id": "int"})
def _inbound(c: Call):
    inbound_id, delete = c.body["id"], c.path.endswith("delete")
    label = str(c.body.get("label") or inbound_id)

    def change(log: Log) -> Any:
        fleet = c.app.fleet(log)
        server = fleet.delete_inbound(inbound_id) if delete else fleet.edit_inbound(inbound_id, c.body)
        if server is None:
            return "Готово"
        fleet = c.app.fleet(log)  # fleet.yaml may have changed
        log("применяю настройки сервера " + server)
        if server == fleet.cfg.master.name:
            return fleet.apply(skip_provision=True)
        return fleet.apply({server}, skip_provision=True)
    return c.run(("Удаление" if delete else "Изменение") + f" входящего «{label}»", change)


# ---------------------------------------------------------------- users
@post("/api/user", needs={"name": "str"})
def _user_add(c: Call):
    name = c.body["name"]
    return c.run(f"Создание пользователя {name}", lambda log: "Подписка создана: " + c.app.fleet(log).add_user(name))


@post("/api/user/delete", needs={"name": "str"})
def _user_delete(c: Call):
    name = c.body["name"]
    return c.run(f"Удаление пользователя {name}", lambda log: c.app.fleet(log).delete_user(name))


@post("/api/user/edit", needs={"name": "str", "devices": "int?", "ips": "int?"}, idle=True)
def _user_edit(c: Call):
    fleet, name = c.app.fleet(), c.body["name"]
    fleet.edit_user(name, c.body)
    if "devices" in c.body or "ips" in c.body:  # the dialog saves settings and limits with one button
        fleet.set_user_limits(name, c.body.get("devices"), c.body.get("ips"))
    return {"ok": True, "user": fleet.user_detail(name)}


@post("/api/user/limits", needs={"name": "str", "devices": "int?", "ips": "int?"}, idle=True)
def _user_limits(c: Call):
    fleet = c.app.fleet()
    fleet.set_user_limits(c.body["name"], c.body.get("devices"), c.body.get("ips"))
    return {"ok": True, "user": fleet.user_detail(c.body["name"])}


@post("/api/user/reset", needs={"name": "str"}, idle=True)
def _user_reset(c: Call):
    fleet = c.app.fleet()
    fleet.reset_user_traffic(c.body["name"])
    return {"ok": True, "user": fleet.user_detail(c.body["name"])}


@post("/api/user/device/forget", needs={"name": "str", "id": "int"}, idle=True)
def _user_forget(c: Call):
    fleet = c.app.fleet()
    fleet.forget_device(c.body["name"], c.body["id"])
    return {"ok": True, "user": fleet.user_detail(c.body["name"])}


# ---------------------------------------------------------------- backups, moving, the app itself
@post("/api/backup")
def _backup(c: Call):
    return c.run("Бэкап главного сервера", lambda log: c.app.fleet(log).backup_master("вручную, кнопкой в интерфейсе"))


@post("/api/backups/open")
def _backups_open(c: Call):
    from .service import open_folder
    folder = c.app.fleet().backup_dir
    folder.mkdir(parents=True, exist_ok=True)
    open_folder(folder)
    return {"ok": True}


@post("/api/migrate", needs={"host": "str"})
def _migrate(c: Call):
    host = c.body["host"]
    ssh, password = ssh_fields(c.body)
    if not ssh.get("key") and not password:
        raise ValueError("введите пароль SSH нового сервера")
    return c.run(f"Перенос главного сервера на {host}",
                 lambda log: c.app.fleet(log).migrate_master(host, ssh, password, c.body.get("backup") or None))


@post("/api/app/inspect")
def _app_inspect(c: Call):
    import base64
    from . import appbackup as ab
    data = base64.b64decode(c.body.get("data") or "")
    return {"info": ab.describe(data, c.body.get("password") or ""), "configured": c.app.path.exists()}


@post("/api/app/import", idle=True)
def _app_import(c: Call):
    import base64
    from . import appbackup as ab
    from .service import backup_dir
    data = base64.b64decode(c.body.get("data") or "")
    res = ab.import_bytes(data, c.body.get("password") or "", c.app.path, backup_dir(c.app.path), bool(c.body.get("replace")))
    return {"ok": True, **res}


@post("/api/versions/settings", needs={"channel": "enum:stable|prerelease?"})
def _versions_settings(c: Call):
    from . import update as upd
    changes: Dict[str, Any] = {}
    if c.body.get("channel"):
        changes["channel"] = c.body["channel"]
    if c.body.get("resume"):
        changes["pinned"] = ""
    if "auto" in c.body:
        changes["auto"] = bool(c.body["auto"])
    return {"ok": True, **upd.save_settings(**changes)}


@post("/api/versions/install", needs={"version": "str"})
def _versions_install(c: Call):
    from . import update as upd
    version = c.body["version"]

    def install(log: Log) -> str:
        log(f"скачиваю версию {version} с GitHub и проверяю контрольную сумму…")
        v = upd.install_version(version)
        pinned = upd.settings()["pinned"]
        log(f"установлена версия {v}" + (" — автообновление на этом компьютере на паузе" if pinned else ""))
        log("приложение перезапустится само через несколько секунд")
        return f"Версия {v} установлена"
    return c.run(f"Установка версии {version}", install)


# ---------------------------------------------------------------- sync between computers
@post("/api/sync/disable")
def _sync_disable(c: Call):
    from .sync import Sync
    Sync(c.app.path).disable()
    return {"ok": True}


@post("/api/sync/enable", needs={"server": "str"})
def _sync_enable(c: Call):
    from .sync import Sync
    app = c.app
    cfg = app.fleet().cfg
    name = c.body["server"]
    srv = cfg.master if name == cfg.master.name else cfg.node(name)
    ssh = cfg.ssh_for(srv)
    server = {"name": srv.name, "host": srv.host, "port": ssh.port or 22, "user": ssh.user or "root"}
    password = c.body.get("password") or ""

    def enable(log: Log) -> str:
        s = Sync(app.path, log)
        s.configure(server, password)
        try:
            s.pull()  # an existing sync on that server: join it instead of overwriting
            msg = s.push(force=True) if not s.meta.get("rev") else "данные с сервера взяты"
        except Exception:
            s.disable()
            raise
        log(msg)
        return f"Синхронизация включена: данные хранятся на {srv.host}, версия {s.meta.get('rev')}"
    return c.run("Включение синхронизации", enable)


@post("/api/sync/now")
def _sync_now(c: Call):
    from .sync import Sync

    def now(log: Log) -> str:
        s = Sync(c.app.path, log)
        msgs = [m for m in (s.pull(), s.push()) if m]
        for m in msgs:
            log(m)
        return msgs[-1] if msgs else "всё уже совпадает"
    return c.run("Синхронизация", now)


@post("/api/sync/join", needs={"host": "str"}, idle=True)
def _sync_join(c: Call):
    from .sync import Sync
    sync = Sync(c.app.path)
    ssh, password = ssh_fields(c.body)
    server = {"name": "sync", "host": c.body["host"], "port": ssh.get("port", 22), "user": ssh.get("user", "root"),
              "ssh_password": password or ""}
    if ssh.get("key"):
        server["key"] = ssh["key"]
    msg = sync.join(server, c.body.get("password") or "")
    try:  # the server is one of ours: remember its name so its saved access is used later
        cfg = c.app.fleet().cfg
        hit = next((x for x in [cfg.master] + list(cfg.nodes) if x.host == server["host"]), None)
        if hit:
            sync.meta["server"]["name"] = hit.name
            sync._save()
    except Exception:
        pass
    return {"ok": True, "message": msg}
