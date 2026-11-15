"""xui-fleet command line."""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, Optional, Set

import time

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from . import xrayconf as xc
from .config import append_node
from .engine import Fleet, FleetError
from .profiles import resolve as resolve_profile
from .panel import PanelError
from .reconcile import Report
from .ssh import SSHError

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Автоматизация 3x-ui: главная панель в RU, зарубежные ноды, каскад и общая подписка.")
node_app = typer.Typer(no_args_is_help=True, help="Управление нодами.")
user_app = typer.Typer(no_args_is_help=True, help="Пользователи и подписки.")
app.add_typer(node_app, name="node")
app.add_typer(user_app, name="user")
console = Console()

ConfigOpt = typer.Option(Path("fleet.yaml"), "--config", "-c", help="Файл конфигурации.")

EXAMPLE = """\
# xui-fleet: желаемое состояние всей сети. После правки: `xui-fleet apply`.
version: 1

panel:
  version: v3.11.0          # одна версия 3x-ui на всех серверах

defaults:
  ssh:
    user: root
    port: 22
    key: ~/.ssh/id_ed25519  # или password_env: ИМЯ_ПЕРЕМЕННОЙ с паролем
  hardening:
    bbr: true
    firewall: false         # true = ufw на нодах (панель ноды открыта только мастеру)

# Главная панель (RU). Клиенты получают подписку отсюда.
master:
  name: ru-msk
  host: 203.0.113.10        # IP или домен
  label: "🇷🇺 RU"
  tls:
    mode: ip                # ip | domain | selfsigned | none (ip/domain: Let's Encrypt, нужен порт 80)
    # domain: panel.example.com
  cascade_ports: [8443, 8543]   # порты на RU под каскадные входы (по одному на ноду)

# Зарубежные серверы. Добавить: `xui-fleet node add de-fra-1 198.51.100.20 --label "🇩🇪 Germany"`
nodes: []

# Необязательно: пользователи, которых нужно создать сразу.
# Дальше ими можно управлять в панели: новые ноды подключаются всем автоматически.
users:
  - name: me
"""


def _fleet(config: Path) -> Fleet:
    config = resolve_profile(config)
    if not config.exists():
        console.print(f"[red]Нет файла {config}. Создайте его: `xui-fleet init`[/red]")
        raise typer.Exit(2)
    return Fleet(config, log=lambda m: console.print(m, markup=False, highlight=False))


def _run(fn):
    try:
        return fn()
    except (FleetError, PanelError, SSHError, KeyError, ValueError, ValidationError, RuntimeError) as exc:
        console.print(f"[red]Ошибка:[/red] {exc}", highlight=False)
        raise typer.Exit(1)


def _print_report(report: Report, dry: bool = False) -> None:
    if not report.changes:
        console.print("[green]Изменений нет: всё уже соответствует конфигу.[/green]")
    else:
        verb = "Будет изменено" if dry else "Применено изменений"
        console.print(f"[green]{verb}: {len(report.changes)}[/green]")
    for name in report.failed:
        console.print(f"[red]✗[/red] {name}: сервер не настроен, причина выше", highlight=False)
    for warning in report.warnings:
        console.print(f"[yellow]![/yellow] {warning}", highlight=False)
    if report.subscriptions:
        table = Table(title="Подписки (одна ссылка — все подключения)")
        table.add_column("Пользователь")
        table.add_column("Ссылка", overflow="fold")
        for name, url in sorted(report.subscriptions.items()):
            table.add_row(name, url)
        console.print(table)


def _only(value: Optional[str]) -> Optional[Set[str]]:
    return {v.strip() for v in value.split(",") if v.strip()} if value else None


@app.command()
def init(config: Path = ConfigOpt, force: bool = typer.Option(False, "--force", help="Перезаписать файл.")):
    """Создать пример fleet.yaml."""
    config = resolve_profile(config)
    if config.exists() and not force:
        console.print(f"[yellow]{config} уже существует (--force чтобы перезаписать).[/yellow]")
        raise typer.Exit(1)
    config.write_text(EXAMPLE, encoding="utf-8")
    gitignore = config.parent / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(".xui-fleet/\n", encoding="utf-8")
    console.print(f"Создан {config}. Укажите мастер, затем: [bold]xui-fleet apply[/bold]")


@app.command()
def apply(
    config: Path = ConfigOpt,
    only: Optional[str] = typer.Option(None, "--only", help="Только эти ноды (через запятую)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Показать план, ничего не менять."),
    skip_provision: bool = typer.Option(False, "--skip-provision", help="Не ходить по SSH на серверы, только панель."),
):
    """Привести все серверы и главную панель к состоянию из конфига (идемпотентно)."""
    fleet = _fleet(config)
    report = _run(lambda: fleet.apply(_only(only), dry_run=dry_run, skip_provision=skip_provision))
    _print_report(report, dry_run)


@app.command()
def plan(config: Path = ConfigOpt, only: Optional[str] = typer.Option(None, "--only")):
    """Показать, что сделает apply (то же, что apply --dry-run)."""
    fleet = _fleet(config)
    _print_report(_run(lambda: fleet.apply(_only(only), dry_run=True)), True)


@app.command()
def status(config: Path = ConfigOpt):
    """Состояние нод и подключений по данным главной панели."""
    fleet = _fleet(config)
    nodes, inbounds = _run(fleet.status)
    by_tag = {i.get("tag"): i for i in inbounds}
    table = Table(title=f"Мастер {fleet.cfg.master.name} ({fleet.cfg.master.host})")
    for col in ("Нода", "Статус", "Пинг", "Версии", "CPU/RAM", "Прямой", "Каскад", "Ошибка"):
        table.add_column(col, overflow="fold")
    seen = set()
    for n in nodes:
        seen.add(n["name"])
        direct, cascade = by_tag.get(xc.direct_tag(n["name"])), by_tag.get(xc.cascade_tag(n["name"]))
        ok = n.get("status") == "online" and not n.get("xrayError")
        table.add_row(
            n["name"], f"[green]{n.get('status')}[/green]" if ok else f"[red]{n.get('status')}[/red]",
            f"{n.get('latencyMs', 0)} мс", f"{n.get('panelVersion', '')} / {n.get('xrayVersion', '')}",
            f"{n.get('cpuPct', 0):.0f}% / {n.get('memPct', 0):.0f}%",
            f":{direct['port']}" if direct else "—", f"RU:{cascade['port']}" if cascade else "—",
            (n.get("lastError") or n.get("xrayError") or "")[:80],
        )
    for node in fleet.cfg.nodes:
        if node.name not in seen:
            table.add_row(node.name, "[yellow]не зарегистрирована[/yellow]", *[""] * 6)
    console.print(table)


@app.command()
def upgrade(config: Path = ConfigOpt, only: Optional[str] = typer.Option(None, "--only")):
    """Обновить 3x-ui до panel.version: сначала ноды, затем мастер."""
    fleet = _fleet(config)
    for name, version in _run(lambda: fleet.upgrade(_only(only))).items():
        console.print(f"{name}: {version}", highlight=False)


@app.command("sub-domain")
def sub_domain(domain: str, config: Path = ConfigOpt):
    """Отдавать подписки на своём домене (сертификат Let's Encrypt). Домен должен указывать на мастер."""
    fleet = _fleet(config)
    console.print(_run(lambda: fleet.set_sub_domain(domain)), highlight=False)


@app.command()
def version():
    """Версия приложения."""
    from . import __version__
    console.print(__version__)


@app.command("app-export")
def app_export(file: Path = typer.Argument(..., help="Куда сохранить, например xui-fleet.xfleet"), config: Path = ConfigOpt):
    """Копия приложения для переноса на другой компьютер (настройки, доступы, бэкапы мастера), под паролем."""
    from . import appbackup as ab
    from .service import backup_dir
    config = resolve_profile(config)
    password = typer.prompt("Пароль для копии (не короче 8 символов)", hide_input=True, confirmation_prompt=True)
    data = _run(lambda: ab.export_bytes(config, backup_dir(config), password))
    file.write_bytes(data)
    console.print(f"Сохранено: {file} ({len(data) // 1024} КБ). Пароль нужен для восстановления — без него копию не открыть.")


@app.command("app-import")
def app_import(file: Path = typer.Argument(..., exists=True, help="Файл .xfleet"),
               replace: bool = typer.Option(False, "--replace", help="Заменить текущие настройки (они сохранятся рядом)"),
               config: Path = ConfigOpt):
    """Восстановить приложение из копии (.xfleet) на этом компьютере."""
    from . import appbackup as ab
    from .service import backup_dir
    config = resolve_profile(config)
    password = typer.prompt("Пароль копии", hide_input=True)
    data = file.read_bytes()
    info = _run(lambda: ab.describe(data, password))
    console.print(f"Копия от {time.strftime('%d.%m.%Y %H:%M', time.localtime(info['created'] or 0))}: главный сервер "
                  f"{info['master']}, ноды {', '.join(info['nodes']) or '—'}, бэкапов мастера {info['masterBackups']}")
    res = _run(lambda: ab.import_bytes(data, password, config, backup_dir(config), replace))
    console.print(f"Готово. Восстановлено бэкапов мастера: {res['backups']}. Запустите интерфейс — всё на месте.")


@app.command("reserve-domain")
def reserve_domain(domain: str = typer.Argument("", help="Пусто — убрать резервный домен"), config: Path = ConfigOpt):
    """Резервный домен подписки (тот же сертификат). Переключиться на него: `sub-domain РЕЗЕРВНЫЙ`."""
    fleet = _fleet(config)
    console.print(_run(lambda: fleet.set_reserve_domain(domain)), highlight=False)


@app.command()
def backup(config: Path = ConfigOpt):
    """Сделать бэкап 3x-ui главного сервера (хранятся последние 10)."""
    fleet = _fleet(config)
    _run(lambda: fleet.backup_master("вручную, командой xui-fleet backup"))
    for b in fleet.backups():
        console.print(f"{b.path.name}  {b.host}  подключений {b.inbounds}, клиентов {b.clients}, нод {b.nodes}", highlight=False)


@app.command("migrate-master")
def migrate_master(
    host: str = typer.Argument(..., help="IP нового сервера."),
    ssh_user: str = typer.Option("root", "--ssh-user"), ssh_port: int = typer.Option(22, "--ssh-port"),
    ssh_key: Optional[str] = typer.Option(None, "--ssh-key"),
    password_env: Optional[str] = typer.Option(None, "--password-env", help="Переменная окружения с паролем SSH."),
    from_backup: Optional[str] = typer.Option(None, "--backup", help="Имя файла бэкапа (по умолчанию — свежий или последний)."),
    config: Path = ConfigOpt,
):
    """Перенести главный сервер на другой сервер из бэкапа (при блокировке старого)."""
    import os
    fleet = _fleet(config)
    pw = os.environ.get(password_env) if password_env else None
    _print_report(_run(lambda: fleet.migrate_master(host, {"user": ssh_user, "port": ssh_port, "key": ssh_key}, pw, from_backup)))


@app.command("check")
def check(user: Optional[str] = typer.Option(None, "--user"), config: Path = ConfigOpt):
    """Проверить трафиком все подключения из подписки пользователя (с главного сервера)."""
    fleet = _fleet(config)
    _print_report(_run(lambda: fleet.check_links(user)))


@app.command("hide-legacy")
def hide_legacy(
    node: str = typer.Option(..., "--node"),
    keep: List[str] = typer.Option([], "--keep", help="Тег, который оставить в подписке; можно «тег=Новое название»."),
    config: Path = ConfigOpt,
):
    """Скрыть из подписки старые подключения ноды (не созданные сервисом). Они продолжают работать."""
    fleet = _fleet(config)
    keep_map = dict((k.split("=", 1) + [""])[:2] for k in keep)
    _print_report(_run(lambda: fleet.hide_legacy(node, keep_map)))


@app.command("import-backup")
def import_backup(
    backup: Path = typer.Argument(..., exists=True, dir_okay=False, help="Файл .db — бэкап 3x-ui."),
    node: str = typer.Option(..., "--node", help="Нода, на которой воссоздать подключения."),
    label: Optional[str] = typer.Option(None, "--label", help='Как назвать страну в подключениях, например "🇬🇧 UK".'),
    dry_run: bool = typer.Option(False, "--dry-run", help="Только показать план."),
    config: Path = ConfigOpt,
):
    """Перенести клиентов и подключения из бэкапа 3x-ui на мастер и ноду."""
    fleet = _fleet(config)
    result = _run(lambda: fleet.import_backup(backup, node, label, dry_run))
    if isinstance(result, Report):
        _print_report(result, dry_run)
    else:
        for w in result.warnings:
            console.print(f"[yellow]![/yellow] {w}", highlight=False)
        console.print(f"[green]В плане действий: {len(result.changes)}[/green]")


@app.command()
def ui(
    config: Path = ConfigOpt,
    port: int = typer.Option(8787, "--port", help="Порт на 127.0.0.1."),
    no_open: bool = typer.Option(False, "--no-open", help="Не открывать браузер."),
    service_mode: bool = typer.Option(False, "--service", hidden=True),
):
    """Открыть веб-интерфейс в браузере (работает только на этом компьютере)."""
    from .ui import serve
    _run(lambda: serve(config, port=port, open_browser=not no_open and not service_mode, service_mode=service_mode))


@app.command()
def service(
    action: str = typer.Argument("install", help="install — запускать интерфейс в фоне при входе в систему; uninstall — убрать; url — показать адрес."),
    config: Path = ConfigOpt,
):
    """Интерфейс как фоновая служба macOS: всегда доступен по одной ссылке, сам подхватывает обновления."""
    from . import service as svc
    if action == "install":
        link = _run(lambda: svc.install(config))
        console.print(f"Служба установлена и запущена. Адрес (сохраните в закладки): {link}", highlight=False, soft_wrap=True)
    elif action == "uninstall":
        svc.uninstall()
        console.print("Служба остановлена и убрана из автозапуска.")
    else:
        console.print(svc.url(), highlight=False, soft_wrap=True)


@app.command("forget-host")
def forget_host(name: str, config: Path = ConfigOpt):
    """Забыть сохранённый SSH-ключ хоста (после переустановки сервера)."""
    fleet = _fleet(config)
    fleet.state.drop(name, "ssh_host_key")
    console.print(f"SSH-ключ хоста {name} забыт.")


# ---- nodes --------------------------------------------------------------
@node_app.command("add")
def node_add(
    name: str = typer.Argument(..., help="Короткое имя, например de-fra-1."),
    host: str = typer.Argument(..., help="IP или домен сервера."),
    label: Optional[str] = typer.Option(None, "--label", help='Название в приложениях, например "🇩🇪 Germany".'),
    ssh_user: Optional[str] = typer.Option(None, "--ssh-user"),
    ssh_port: Optional[int] = typer.Option(None, "--ssh-port"),
    ssh_key: Optional[str] = typer.Option(None, "--ssh-key", help="Путь к приватному ключу."),
    password_env: Optional[str] = typer.Option(None, "--password-env", help="Имя переменной окружения с SSH-паролем."),
    port: Optional[int] = typer.Option(None, "--port", help="Порт прямого входа на ноде (по умолчанию 443)."),
    cascade_port: Optional[int] = typer.Option(None, "--cascade-port", help="Порт каскадного входа на RU."),
    no_cascade: bool = typer.Option(False, "--no-cascade", help="Без каскада через RU."),
    no_direct: bool = typer.Option(False, "--no-direct", help="Не выдавать пользователям прямое подключение."),
    no_apply: bool = typer.Option(False, "--no-apply", help="Только записать в конфиг."),
    config: Path = ConfigOpt,
):
    """Зарегистрировать новый сервер: установка, подключение к мастеру, каскад, подписки."""
    fleet = _fleet(config)
    if any(n.name == name for n in fleet.cfg.nodes):
        console.print(f"[yellow]Нода {name} уже есть в конфиге — просто применяю.[/yellow]")
    else:
        entry: dict = {"name": name, "host": host}
        if label:
            entry["label"] = label
        ssh = {k: v for k, v in (("user", ssh_user), ("port", ssh_port), ("key", ssh_key),
                                 ("password_env", password_env)) if v}
        if ssh:
            entry["ssh"] = ssh
        direct = {k: v for k, v in (("enabled", False if no_direct else None), ("port", port)) if v is not None}
        if direct:
            entry["direct"] = direct
        cascade = {k: v for k, v in (("enabled", False if no_cascade else None), ("port", cascade_port)) if v is not None}
        if cascade:
            entry["cascade"] = cascade
        _run(lambda: append_node(fleet.config_path, entry))
        fleet.reload()
        console.print(f"Нода {name} добавлена в {fleet.config_path}.")
    if no_apply:
        return
    _print_report(_run(lambda: fleet.apply({name})))


@node_app.command("remove")
def node_remove(
    name: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Не спрашивать подтверждение."),
    config: Path = ConfigOpt,
):
    """Убрать ноду с мастера (inbound-ы, каскад, запись ноды) и из конфига. Сам сервер не трогается."""
    fleet = _fleet(config)
    if not yes and not typer.confirm(f"Удалить ноду {name} с мастера? Подключения к ней перестанут работать"):
        raise typer.Exit(1)
    report = _run(lambda: fleet.remove_node(name))
    if _drop_node_from_yaml(fleet.config_path, name):
        console.print(f"Нода {name} удалена из {fleet.config_path}.")
    console.print(f"[green]Готово, изменений: {len(report.changes)}[/green]")


@node_app.command("list")
def node_list(config: Path = ConfigOpt):
    """Ноды из конфига."""
    fleet = _fleet(config)
    table = Table()
    for col in ("Имя", "Хост", "Название", "Прямой вход", "Каскад"):
        table.add_column(col)
    for n in fleet.cfg.nodes:
        d, c = fleet.cfg.direct_for(n), fleet.cfg.cascade_for(n)
        table.add_row(n.name, n.host, n.label or "", f":{d.port}" if d.enabled else "скрыт",
                      (f":{c.port}" if c.port else "авто") if c.enabled else "выкл")
    console.print(table)


def _drop_node_from_yaml(path: Path, name: str) -> bool:
    from .config import remove_node_entry
    return remove_node_entry(path, name)


# ---- users --------------------------------------------------------------
@user_app.command("add")
def user_add(name: str, comment: str = typer.Option("", "--comment"),
             total_gb: float = typer.Option(0, "--total-gb", help="Лимит трафика, 0 = без лимита."),
             config: Path = ConfigOpt):
    """Создать пользователя на всех подключениях и показать ссылку подписки."""
    fleet = _fleet(config)
    url = _run(lambda: fleet.add_user(name, comment, total_gb))
    console.print(f"Подписка {name}: {url}", highlight=False, soft_wrap=True)


@user_app.command("list")
def user_list(config: Path = ConfigOpt):
    """Ссылки подписок всех пользователей."""
    fleet = _fleet(config)
    for name, url in sorted(_run(fleet.subscriptions).items()):
        console.print(f"{name}: {url}", highlight=False, soft_wrap=True)


def main(argv: Optional[List[str]] = None) -> None:
    app(args=argv if argv is not None else sys.argv[1:])


if __name__ == "__main__":
    main()
