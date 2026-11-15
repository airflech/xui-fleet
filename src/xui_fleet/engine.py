"""Orchestration: provision servers over SSH, then reconcile the master panel."""
from __future__ import annotations

import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

from . import provision as pv
from .config import FleetConfig, NodeConfig, load_config
from .panel import Panel, PanelError
from .reconcile import NodeAccess, Reconciler, Report
from .ssh import SSH, SSHError
from .state import State
from .xrayconf import cascade_tag as xc_cascade, direct_tag as xc_direct

Log = Callable[[str], None]


class FleetError(RuntimeError):
    pass


def parallel(items: List[Any], fn: Callable[[Any], Any], limit: float, on_timeout: Callable[[Any], Any]) -> List[Any]:
    """Run fn over items at the same time; one slow or dead server never holds up the others.

    Results keep the input order. An item that does not finish within `limit` seconds gets on_timeout(item).
    """
    if not items:
        return []
    pool = ThreadPoolExecutor(max_workers=min(8, len(items)))
    futures = [pool.submit(fn, it) for it in items]
    deadline = time.time() + limit
    out = []
    for it, fut in zip(items, futures):
        try:
            out.append(fut.result(timeout=max(0.0, deadline - time.time())))
        except FutureTimeout:
            out.append(on_timeout(it))
    pool.shutdown(wait=False, cancel_futures=True)  # stragglers finish in the background
    return out


class Fleet:
    def __init__(self, config_path: Path, log: Log = print):
        self.config_path = config_path
        self.cfg: FleetConfig = load_config(config_path)
        self.state = State(config_path.parent / ".xui-fleet" / "state.json")
        self.log = log
        self._master_ssh: Optional[SSH] = None
        self._warp_bad: Dict[str, bool] = {}
        self.purpose = ""  # what the user started (shown as the reason of the automatic backup)

    def countries(self) -> Dict[str, str]:
        return {n.name: c for n in [self.cfg.master] + list(self.cfg.nodes) for c in [self.state.get(n.name, "country")] if c}

    def _master_facts(self) -> NodeAccess:
        """What the master's own entries need (master.vpn): busy ports, its cert, country, a neighbor site."""
        from .geo import DETECT_SCRIPT
        ssh, m = self._master_ssh, self.cfg.master
        facts = pv.node_facts(ssh)
        if not self.state.get(m.name, "country"):
            country = ssh.run(DETECT_SCRIPT, check=False).out.strip()
            if len(country) == 2:
                self.state.update(m.name, country=country)
                self.log(f"главный сервер находится в стране {country}")
        if self.cfg.tls_for(m).mode != "domain":
            try:
                self._ensure_neighbor(self.cfg.local_node(), ssh)
            except Exception as exc:
                self.log(f"⚠ главный сервер: не удалось подобрать сайт для маскировки: {exc}")
        acc = NodeAccess("https", m.host, 0, "", None)
        acc.busy_tcp, acc.busy_udp = facts["busy_tcp"], facts["busy_udp"]
        acc.cert_pem, acc.key_pem, acc.cert_pin_hex = facts["cert_pem"], facts["key_pem"], facts["cert_pin_hex"]
        return acc

    def ensure_master_warp(self, panel: Panel) -> Optional[str]:
        """WARP exit on the master for its own WARP entry (master.vpn): own registration, best endpoint."""
        from . import migrate as mg
        from . import xrayconf as xc
        tag = xc.warp_tag(self.cfg.master.name)
        if not any(i.get("tag") == tag and not i.get("nodeId") for i in panel.inbounds()):
            return None
        template = panel.xray_template()
        current = next((o for o in template.get("outbounds") or [] if o.get("tag") == "warp"), None)
        if current is not None and current.get("settings", {}).get("secretKey"):
            warp = current
        else:
            reg = panel.post("/xray/warp/reg", form=mg.new_wireguard_keys())
            warp = mg.warp_from_registration(json.loads(json.dumps(self.DEFAULT_WARP)), json.loads(reg) if isinstance(reg, str) else reg)
            self.log("главный сервер: зарегистрировал собственный WARP")
        tags = {tag}
        for rule in (template.get("routing") or {}).get("rules") or []:
            if rule.get("outboundTag") == "warp":
                tags |= set(rule.get("inboundTag") or [])
        endpoint = self._warp_endpoint(self.cfg.local_node(), self._master_ssh, warp) if self._master_ssh else ""
        merged = mg.merge_warp(template, warp, sorted(tags), endpoint)
        if merged != template:
            panel.save_xray_template(merged)
            panel.restart_xray()
            self.log(f"главный сервер: WARP-маршрут для {', '.join(sorted(tags))}")
        return None

    def neighbors(self) -> Dict[str, tuple]:
        """Node name -> (ip:443, server name) of the real site its Reality entries borrow the handshake from."""
        out = {}
        for n in self.cfg.nodes:
            nb = self.state.get(n.name, "reality_neighbor") or {}
            if nb.get("ip") and nb.get("name"):
                out[n.name] = (f"{nb['ip']}:443", nb["name"])
        return out

    def health(self) -> Dict[str, float]:
        """Node name -> share of test connections RU -> node that opened at the last check (1.0 = stable)."""
        out = {}
        for n in self.cfg.nodes:
            link = self.state.get(n.name, "ru_link") or {}
            score = link["ok"] / link["of"] if link.get("of") else 1.0
            hist = self.state.get(n.name, "cascade_hist") or []
            if hist:  # share of the last RU -> node traffic checks that passed
                score *= sum(hist) / len(hist)
            if score < 1.0 or link.get("of"):
                out[n.name] = score
        return out

    def _ranking(self, health: Dict[str, float]) -> List[str]:
        """Order of servers the subscription would use (stable ones first), to see if it changed."""
        return sorted((n.name for n in self.cfg.nodes),
                      key=lambda n: (health.get(n, 1.0) < 1.0, -health.get(n, 1.0) if health.get(n, 1.0) < 1.0 else 0, n))

    NEIGHBOR_RECHECK = 24 * 3600

    def _ensure_neighbor(self, node: NodeConfig, ssh: SSH) -> None:
        """Find (once) and re-check (daily) a same-subnet site for the node's Reality entries."""
        if node.direct.reality.target or node.direct.reality.server_names or \
                self.cfg.defaults.direct.reality.target or self.cfg.defaults.direct.reality.server_names:
            return  # the config names the target itself
        nb = self.state.get(node.name, "reality_neighbor") or {}
        if nb.get("ip") and time.time() - nb.get("checked", 0) < self.NEIGHBOR_RECHECK:
            return
        if not nb.get("ip") and time.time() - nb.get("checked", 0) < self.NEIGHBOR_RECHECK:
            return  # scanned recently, the subnet had nothing suitable
        if nb.get("ip") and pv.neighbor_alive(ssh, nb["ip"], nb["name"]):
            self.state.update(node.name, reality_neighbor=dict(nb, checked=time.time()))
            return
        self.log(f"{node.name}: ищу в подсети сервера настоящий сайт для маскировки Reality (до минуты)…")
        found = pv.find_reality_neighbor(ssh)
        if found:
            if nb.get("ip"):
                self.log(f"{node.name}: прежний сайт {nb['name']} больше не отвечает как нужно")
            self.log(f"{node.name}: подключения будут выглядеть как обращения к {found[1]} ({found[0]}) — сайту в той же подсети")
            self.state.update(node.name, reality_neighbor={"ip": found[0], "name": found[1], "checked": time.time()})
        else:
            if not nb.get("ip"):
                self.log(f"{node.name}: подходящего сайта в подсети нет — оставляю маскировку по умолчанию")
            self.state.update(node.name, reality_neighbor=dict(nb, checked=time.time()))

    def reload(self) -> None:
        self.cfg = load_config(self.config_path)

    # ---- connections ----------------------------------------------------
    def ssh(self, server) -> SSH:
        cfg = self.cfg.ssh_for(server)
        if not (cfg.key or cfg.password or cfg.password_env):
            saved = self.state.get(server.name, "ssh_password")  # entered in the web UI
            if saved:
                cfg = cfg.model_copy(update={"password": saved})
        return SSH(server.name, server.host, cfg, self.state)

    @contextmanager
    def master_panel(self, provision: bool = True) -> Iterator[Panel]:
        """Yield an API client for the master, provisioning it first when asked."""
        master = self.cfg.master
        with ExitStack() as stack:
            if master.api_url:  # direct mode: no SSH (local testbeds)
                token = os.environ.get("XUI_FLEET_MASTER_TOKEN") or self.state.get(master.name, "api_token")
                if not token:
                    raise FleetError("master.api_url задан, но нет токена: XUI_FLEET_MASTER_TOKEN")
                panel = Panel(master.api_url, token)
            else:
                ssh = self.ssh(master).connect()
                stack.callback(ssh.close)
                self._master_ssh = ssh
                stack.callback(lambda: setattr(self, "_master_ssh", None))
                if provision:
                    info = pv.provision(self.cfg, master, "master", ssh, self.state, self.log)
                    scheme, port, base, token = info.scheme, info.port, info.base_path, info.token
                else:
                    st = self.state.server(master.name)
                    if not st.get("api_token"):
                        raise FleetError(f"мастер {master.name} ещё не подготовлен: выполните `xui-fleet apply`")
                    scheme, port, base, token = st["scheme"], st["panel_port"], st["base_path"], st["api_token"]
                local = stack.enter_context(ssh.forward(int(port)))
                panel = Panel(f"{scheme}://127.0.0.1:{local}{base}", token or "")
            stack.callback(panel.close)
            panel.wait_ready(90)
            yield panel

    # ---- node provisioning ----------------------------------------------
    def _access_from_state(self, node: NodeConfig) -> Optional[NodeAccess]:
        st = self.state.server(node.name)
        if not st.get("api_token") or not st.get("panel_port"):
            return None
        acc = self._access(node, st["scheme"], st["panel_port"], st["base_path"], st["api_token"],
                           st.get("cert_pin", ""), fresh=False)
        acc.direct_port = st.get("direct_port")
        return acc

    def _access(self, node: NodeConfig, scheme: str, port: int, base: str, token: Optional[str], pin: str,
                fresh: bool) -> NodeAccess:
        if scheme != "https":
            mode = "verify"
        elif pin:
            mode = "pin"
        else:
            mode = "verify"
        return NodeAccess(scheme=scheme, address=node.panel_address or node.host, port=int(port), base_path=base,
                          api_token=token, token_fresh=fresh, tls_verify_mode=mode, pinned_cert_sha256=pin,
                          allow_private_address=node.allow_private_address)

    def _provision_node(self, node: NodeConfig) -> NodeAccess:
        ssh = self.ssh(node).connect()
        try:
            direct = self.cfg.direct_for(node)
            info = pv.provision(self.cfg, node, "node", ssh, self.state, self.log,
                                master_ip=self.cfg.master.host, open_ports=[])
            wanted = node.direct.port or direct.port or 443
            known = self.state.get(node.name, "direct_port")
            port = node.direct.port or pv.pick_direct_port(ssh, wanted, known)
            if port != wanted and port != known:
                self.log(f"{node.name}: порт {wanted} уже занят на сервере — подключение сервиса будет на порту {port}")
            self.state.update(node.name, direct_port=port)
            if pv.open_port(ssh, port):
                self.log(f"{node.name}: открыл порт {port}/tcp в ufw")
            facts = pv.node_facts(ssh)
            from .geo import DETECT_SCRIPT
            country = ssh.run(DETECT_SCRIPT, check=False).out.strip()
            if len(country) == 2 and country != self.state.get(node.name, "country"):
                self.state.update(node.name, country=country)
                self.log(f"{node.name}: сервер находится в стране {country}")
            if self.cfg.defaults.hardening.cover_site:
                try:
                    busy80 = ssh.run("ss -Hltnp 'sport = :80' | grep -v nginx | head -1", check=False).out.strip()
                    if busy80:
                        self.log(f"{node.name}: порт 80 занят другой программой — сайт-витрину не ставлю")
                    elif pv.ensure_site(ssh, [], ip=node.host):
                        pv.open_port(ssh, 80, "tcp")
                        self.log(f"{node.name}: сайт-витрина на http://{node.host}/")
                except Exception as exc:  # cosmetics must never block provisioning
                    self.log(f"⚠ {node.name}: сайт-витрину поставить не удалось: {exc}")
            try:
                self._ensure_neighbor(node, ssh)
            except Exception as exc:  # cosmetics of the handshake must never block provisioning
                self.log(f"⚠ {node.name}: не удалось подобрать сайт для маскировки: {exc}")
        finally:
            ssh.close()
        acc = self._access(node, info.scheme, info.port, info.base_path, info.token, info.cert_pin,
                           fresh=info.api_token is not None)
        acc.direct_port = port
        acc.busy_tcp, acc.busy_udp = facts["busy_tcp"], facts["busy_udp"]
        acc.cert_pem, acc.key_pem, acc.cert_pin_hex = facts["cert_pem"], facts["key_pem"], facts["cert_pin_hex"]
        return acc

    def check_reachability(self, access: Dict[str, NodeAccess], failed: Dict[str, str]) -> List[str]:
        """Test from the RU master that each node panel answers TCP; filtered IPs fail here, not in 3x-ui."""
        warnings: List[str] = []
        if self._master_ssh is None:
            return warnings
        items = list(access.items())
        # Several SSH channels on the master connection run at once; each node costs ~6 s at worst, not the sum.
        counts = parallel(items, lambda it: pv.reachable_from(self._master_ssh, it[1].address, it[1].port), 40, lambda it: 0)
        for (name, acc), ok in zip(items, counts):
            self.state.update(name, ru_link={"ok": ok, "of": 6, "at": time.time()})
            if ok == 0:
                failed[name] = (
                    f"{name}: с главного сервера {self.cfg.master.host} не открывается ни одно соединение к "
                    f"{acc.address}:{acc.port} (0 из 6), хотя сам сервер в порядке. Скорее всего, IP {acc.address} "
                    f"фильтруется в России: через него не будет работать ни управление, ни каскад. "
                    f"Нужен другой IP или другой зарубежный сервер."
                )
                self.log("[ошибка] " + failed[name])
                access.pop(name)
            elif ok < 6:
                warnings.append(f"{name}: связь RU → {acc.address} нестабильна ({ok} из 6 соединений) — "
                                f"возможна фильтрация IP в России, каскад может работать с перебоями")
                self.log(f"⚠ {warnings[-1]}")
        return warnings

    def provision_nodes(self, nodes: List[NodeConfig]) -> Tuple[Dict[str, NodeAccess], Dict[str, str]]:
        access: Dict[str, NodeAccess] = {}
        failed: Dict[str, str] = {}
        if not nodes:
            return access, failed
        with ThreadPoolExecutor(max_workers=min(8, len(nodes))) as pool:
            futures = {n.name: pool.submit(self._provision_node, n) for n in nodes}
            for name, future in futures.items():
                try:
                    access[name] = future.result()
                except Exception as exc:  # one broken server must not stop the rest
                    failed[name] = str(exc)
                    self.log(f"[ошибка] {exc}" if str(exc).startswith(name + ":") else f"[ошибка] {name}: {exc}")
        return access, failed

    # ---- commands -------------------------------------------------------
    def apply(self, only: Optional[Set[str]] = None, dry_run: bool = False,
              skip_provision: bool = False) -> Report:
        for name in only or ():
            self.cfg.node(name)  # fail early on typos
        nodes = [n for n in self.cfg.nodes if n.enabled and (only is None or n.name in only)]
        if dry_run:
            return self._plan(nodes, only)
        if self.state.get(self.cfg.master.name, "api_token") and not self.cfg.master.api_url:
            self.auto_backup(reason="перед: " + (self.purpose or (
                f"изменением {', '.join(sorted(only))}" if only else "применением настроек")))
        with self.master_panel(provision=not skip_provision) as panel:
            if skip_provision:
                access = {n.name: a for n in nodes for a in [self._access_from_state(n)] if a}
                failed: Dict[str, str] = {}
            else:
                access, failed = self.provision_nodes(nodes)
            tls = self.cfg.tls_for(self.cfg.master)
            if self._master_ssh is not None and tls.mode == "domain" and tls.domain:
                if pv.ensure_decoy_site(self._master_ssh, tls.domain, self.cfg.master.decoy_port, list(tls.reserve_domains)):
                    for p in (80, 443):
                        pv.open_port(self._master_ssh, p, "tcp")
                    names = ", ".join([tls.domain] + list(tls.reserve_domains))
                    self.log(f"мастер: сайт-витрина {names} обновлён (80/443 и маскировка Reality)")
            fresh = {n: a for n, a in access.items() if a.token_fresh or not self._registered(panel, n)}
            net_warnings = self.check_reachability(fresh, failed)
            access = {n: a for n, a in access.items() if n not in failed}
            if self.cfg.master.vpn and self._master_ssh is not None:
                access[self.cfg.master.name] = self._master_facts()
            healthy = None if only is None and not failed else {n.name for n in nodes if n.name not in failed}
            used = self.health()
            report = Reconciler(panel, self.cfg, countries=self.countries(), neighbors=self.neighbors(), health=used, log=self.log).run(access, healthy)
            for name, ok in report.cascade_ok.items():
                hist = list(self.state.get(name, "cascade_hist") or [])[-5:] + [ok]
                self.state.update(name, cascade_hist=hist)
            if self._ranking(self.health()) != self._ranking(used):  # a server turned out (un)stable: reorder the subscription right away
                self.log("мастер: меняю порядок подключений в подписке по результатам проверки")
                again = Reconciler(panel, self.cfg, countries=self.countries(), neighbors=self.neighbors(),
                                   health=self.health(), log=self.log).run({}, healthy)
                report.changes += again.changes
            report.warnings = net_warnings + report.warnings
            self._open_fleet_ports(panel, access)
            if self.cfg.master.vpn and self.cfg.defaults.set.warp:
                try:
                    msg = self.ensure_master_warp(panel)
                    if msg:
                        report.warnings.append(msg)
                except Exception as exc:  # WARP is an extra; the rest of the run stands
                    report.warnings.append(f"мастер: WARP не настроен: {exc}")
            warp_nodes = [n for n in nodes if n.name not in failed and (self.cfg.set_for(n).warp or self.cfg.set_for(n).cascade_warp)]
            for msg in parallel(warp_nodes, lambda n: self.ensure_node_warp(panel, n), 180, lambda n: f"{n.name}: WARP не успел настроиться"):
                if msg:
                    report.warnings.append(msg)
            for name, error in failed.items():
                report.failed.append(name)
            return report

    def _open_fleet_ports(self, panel: Panel, access: Dict[str, NodeAccess]) -> None:
        """Entries created on a node with an active ufw must be reachable."""
        ids = {n["name"]: n["id"] for n in panel.nodes()}
        fleet = [i for i in panel.inbounds() if str(i.get("tag", "")).startswith("fleet-")]
        hosted = [i for i in fleet if i.get("nodeId")]
        if self._master_ssh is not None:
            for i in fleet:
                if not i.get("nodeId") and pv.open_port(self._master_ssh, int(i["port"]), "udp" if i["protocol"] == "hysteria" else "tcp"):
                    self.log(f"мастер: открыл порт {i['port']} в ufw")
        for name in access:
            ports = {(int(i["port"]), "udp" if i["protocol"] == "hysteria" else "tcp") for i in hosted if i["nodeId"] == ids.get(name)}
            if not ports:
                continue
            try:
                ssh = self.ssh(self.cfg.node(name)).connect()
                try:
                    for port, proto in sorted(ports):
                        if pv.open_port(ssh, port, proto):
                            self.log(f"{name}: открыл порт {port}/{proto} в ufw")
                finally:
                    ssh.close()
            except Exception as exc:  # the entries exist; a firewall hiccup must not fail the run
                self.log(f"⚠ {name}: не удалось проверить ufw: {exc}")

    @staticmethod
    def _registered(panel: Panel, name: str) -> bool:
        return any(n.get("name") == name for n in panel.nodes())

    def _plan(self, nodes: List[NodeConfig], only: Optional[Set[str]]) -> Report:
        """Read-only preview: SSH facts for every server, then a dry reconcile."""
        master_ready = bool(self.cfg.master.api_url or self.state.get(self.cfg.master.name, "api_token"))
        servers = ([] if master_ready else [self.cfg.master]) + [n for n in nodes if not self._access_from_state(n)]
        report = Report()
        for server in servers:
            try:
                ssh = self.ssh(server).connect()
                try:
                    facts = pv._facts(ssh)
                finally:
                    ssh.close()
                what = (f"3x-ui {facts['xui']} уже установлен, будет подключён" if facts.get("xui")
                        else f"будет установлен 3x-ui {self.cfg.panel.version}")
                self.log(f"[dry-run] {server.name}: SSH ок ({facts.get('os')}, {facts.get('arch')}), {what}")
                report.changes.append(f"{server.name}: {what}")
            except Exception as exc:
                self.log(f"[dry-run] {server.name}: SSH недоступен — {exc}")
                report.warnings.append(f"{server.name}: SSH недоступен — {exc}")
        if not master_ready:
            report.warnings.append("мастер ещё не подготовлен: план по панели появится после первого apply")
            return report
        pending = {s.name for s in servers}
        with self.master_panel(provision=False) as panel:
            access = {n.name: a for n in nodes for a in [self._access_from_state(n)] if a}
            scope = {n.name for n in nodes if n.name not in pending}
            dry = Reconciler(panel, self.cfg, countries=self.countries(), neighbors=self.neighbors(), health=self.health(), dry_run=True, log=self.log).run(access, scope)
        dry.changes = report.changes + dry.changes
        dry.warnings = report.warnings + dry.warnings
        for name in sorted(pending & {n.name for n in nodes}):
            dry.changes.append(f"{name}: регистрация на мастере, inbound-ы и каскад")
            self.log(f"[dry-run] {name}: после установки — регистрация на мастере, inbound-ы и каскад")
        return dry

    def _servers(self):
        from .geo import display_labels
        labels = display_labels(self.cfg.nodes, self.countries())
        return [("Главная панель (RU)", self.cfg.master)] + [
            (f"Нода {labels[n.name]}", n)
            for n in self.cfg.nodes if n.enabled]

    def panels(self) -> List[Dict[str, Any]]:
        """Link, login and password of every 3x-ui panel, read from each server and checked live."""
        def one(item):
            title, server = item
            st = self.state.server(server.name)
            row: Dict[str, Any] = {"name": server.name, "title": title, "url": None, "username": None,
                                   "password": None, "valid": False, "selfSigned": bool(st.get("cert_pin")), "error": ""}
            if not st.get("panel_port"):
                row["error"] = "сервер ещё не настроен"
                return row
            # The master's panel shares the domain cert once one is set up; nodes keep their IP.
            host = (self.cfg.tls_for(server).domain if server is self.cfg.master else None) or server.host
            row["url"] = f"{st['scheme']}://{host}:{st['panel_port']}{st['base_path']}"
            try:
                ssh = self.ssh(server).connect()
                try:
                    row.update(pv.panel_login(ssh, st["scheme"], int(st["panel_port"]), st["base_path"]))
                finally:
                    ssh.close()
                if not row["username"]:
                    row["error"] = "панель ставили не через xui-fleet — сохранённого пароля нет"
                elif not row["valid"]:
                    row["error"] = "сохранённый пароль не подходит — его меняли в панели"
            except Exception as exc:
                row["error"] = str(exc)[:200]
            return row

        def late(item):
            title, server = item
            st = self.state.server(server.name)
            host = (self.cfg.tls_for(server).domain if server is self.cfg.master else None) or server.host
            url = f"{st['scheme']}://{host}:{st['panel_port']}{st['base_path']}" if st.get("panel_port") else None
            return {"name": server.name, "title": title, "url": url, "username": None, "password": None, "valid": False,
                    "selfSigned": bool(st.get("cert_pin")), "error": "сервер не ответил вовремя — данные не получены"}
        return parallel(self._servers(), one, 25, late)

    def reset_panel_password(self, name: str) -> str:
        server = self.cfg.master if self.cfg.master.name == name else self.cfg.node(name)
        ssh = self.ssh(server).connect()
        try:
            out = ssh.run(pv._ENV + f"{pv.XUI} setting -show true 2>/dev/null; cat /etc/x-ui/install-result.env 2>/dev/null",
                          check=False).out
            import re as _re
            m = _re.search(r"^XUI_USERNAME=(\S+)", out, flags=_re.M)
            username = m.group(1).strip("'\"") if m else "admin" + __import__("secrets").token_hex(3)
            pv.set_panel_password(ssh, username)
        finally:
            ssh.close()
        self.log(f"{name}: задан новый пароль панели (логин {username})")
        return f"Новый пароль для {name} задан — он показан в блоке «Панели 3x-ui»"

    def _check_domain(self, ssh, domain: str) -> str:
        import re as _re
        domain = domain.strip().lower().rstrip(".")
        if not _re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}", domain):
            raise FleetError(f"«{domain}» не похоже на домен")
        ips = pv.resolve_public(ssh, domain)
        if ips != [self.cfg.master.host]:
            raise FleetError(
                f"{domain} сейчас указывает на {', '.join(ips) or 'ничего'}, а должен на {self.cfg.master.host}. "
                f"Добавьте у регистратора A-запись {domain} → {self.cfg.master.host}; обновление расходится до часа."
            )
        return domain

    def _master_cert(self, ssh, main: str, reserves: List[str]) -> Dict[str, str]:
        """One Let's Encrypt certificate for the main and every reserve domain that resolves here."""
        ok = []
        for d in reserves:
            if d != main and pv.resolve_public(ssh, d) == [self.cfg.master.host]:
                ok.append(d)
            elif d != main:
                self.log(f"⚠ {d} не указывает на {self.cfg.master.host} — в сертификат не включаю")
        names = ", ".join([main] + ok)
        self.log(f"сертификат Let's Encrypt для {names}")
        cert = pv.issue_domain_cert(ssh, main, self.cfg.tls_for(self.cfg.master).email, ok)
        if pv.ensure_decoy_site(ssh, main, self.cfg.master.decoy_port, ok):
            self.log(f"мастер: сайт-заглушка для {names} (nginx, 127.0.0.1:{self.cfg.master.decoy_port})")
        return cert

    def _sub_answers(self, domains: List[str]) -> Dict[str, str]:
        """HTTP code of a real subscription fetched on each domain (from the master itself)."""
        with self.master_panel(provision=False) as panel:
            s = panel.settings()
            sample = next((c["subId"] for c in panel.clients() if c.get("subId") and c["email"] != self.cfg.clients.bridge_email), "")
        port, path = s.get("subPort", 2096), s.get("subPath", "/sub/")
        ssh = self.ssh(self.cfg.master).connect()
        try:
            return {d: ssh.run(f"curl -s -o /dev/null -w '%{{http_code}}' -m 15 --resolve {d}:{port}:127.0.0.1 "
                               f"https://{d}:{port}{path}{sample}", check=False).out.strip() for d in domains}
        finally:
            ssh.close()

    def set_sub_domain(self, domain: str) -> str:
        """Serve subscriptions on `domain` with a Let's Encrypt cert once its DNS points at the master.

        Switching to a reserve domain works the same way: the previous main domain becomes a reserve."""
        from .config import set_master_domain
        master = self.cfg.master
        tls = self.cfg.tls_for(master)
        old = tls.domain if tls.mode == "domain" else None
        ssh = self.ssh(master).connect()
        try:
            domain = self._check_domain(ssh, domain)
            reserves = [d for d in list(tls.reserve_domains) + ([old] if old else []) if d and d != domain]
            self.log(f"DNS: {domain} → {master.host}")
            cert = self._master_cert(ssh, domain, reserves)
        finally:
            ssh.close()
        with self.master_panel(provision=False) as panel:
            s = panel.settings()
            want = {"subCertFile": cert["cert"], "subKeyFile": cert["key"],
                    # The panel itself too, so its link can use the domain instead of the IP.
                    "webCertFile": cert["cert"], "webKeyFile": cert["key"]}
            if any(s.get(k) != v for k, v in want.items()):
                s.update(want)
                panel.update_settings(s)
                panel.restart_panel()
                time.sleep(5)
                panel.wait_ready(90)
                self.log("мастер: подписки и панель теперь работают с сертификатом домена")
            path, port = s.get("subPath", "/sub/"), s.get("subPort", 2096)
        set_master_domain(self.config_path, domain, reserves)
        self.reload()
        code = self._sub_answers([domain])[domain]
        if code != "200":
            raise FleetError(f"сертификат выпущен, но подписка по https://{domain}:{port}{path}… ответила {code or 'ошибкой'}")
        # Entries via RU carry the domain too (address and Reality SNI).
        self.apply(skip_provision=True)
        if old and old != domain:
            self.log(f"«{old}» оставлен резервным доменом: старые ссылки продолжают открываться, пока он не заблокирован")
        return f"Готово: подписки работают по https://{domain}:{port}{path}<subId> с проверенным сертификатом"

    def set_reserve_domain(self, domain: str) -> str:
        """Add a reserve domain to the master (same certificate), or remove all reserves with an empty value."""
        from .config import set_master_domain
        master = self.cfg.master
        tls = self.cfg.tls_for(master)
        if tls.mode != "domain" or not tls.domain:
            raise FleetError("сначала задайте основной домен подписки")
        ssh = self.ssh(master).connect()
        try:
            if domain.strip():
                domain = self._check_domain(ssh, domain)
                if domain == tls.domain:
                    raise FleetError("это уже основной домен")
                reserves = [domain]
            else:
                reserves = []
            self._master_cert(ssh, tls.domain, reserves)
        finally:
            ssh.close()
        set_master_domain(self.config_path, tls.domain, reserves)
        self.reload()
        if not reserves:
            return "Резервный домен убран"
        answers = self._sub_answers([tls.domain, domain])
        bad = {d: c for d, c in answers.items() if c != "200"}
        if bad:
            raise FleetError("сертификат выпущен, но подписка не открывается: " +
                             ", ".join(f"{d} → {c or 'ошибка'}" for d, c in bad.items()))
        self.log(f"✅ подписки открываются по обоим доменам: {tls.domain} и {domain}")
        return f"Готово: резервный домен {domain}. Если {tls.domain} заблокируют — «Перейти на {domain}»"

    def import_backup(self, path: Path, node_name: str, label: Optional[str] = None, dry_run: bool = False):
        """Move clients and inbounds of a 3x-ui backup onto the master and `node_name`."""
        from . import migrate as mg
        backup = mg.read_backup(path)
        if not dry_run:
            self.auto_backup(max_age_hours=0, reason=f"перед импортом бэкапа {path.name} на {node_name}")
        node = self.cfg.node(node_name)
        self.log(f"бэкап: {len(backup.clients)} клиент(ов), {len(backup.local_inbounds())} подключений сервера, "
                 f"{len(backup.inbounds) - len(backup.local_inbounds())} — на его старых нодах (заменятся каскадом)")
        with self.master_panel(provision=False) as panel:
            record = next((n for n in panel.nodes() if n.get("name") == node_name), None)
            if record is None:
                raise FleetError(f"нода {node_name} ещё не зарегистрирована на мастере — сначала «Применить»")
            ssh = self.ssh(node).connect()
            try:
                ports = ssh.run("ss -Htuln | awk '{print $1, $5}'", check=False).out
                busy = {"tcp": set(), "udp": set()}
                for line in ports.splitlines():
                    proto, _, addr = line.partition(" ")
                    port = addr.rsplit(":", 1)[-1]
                    if port.isdigit() and proto in busy and not addr.startswith(("127.", "[::1]")):
                        busy[proto].add(int(port))
                cert = ssh.run(pv._ENV + f"{pv.XUI} setting -getCert true | sed -n 's/^cert: //p;s/^key: //p'").out.split()
                pems = [ssh.run(f"cat {p}").out if p else "" for p in (cert + ["", ""])[:2]]
                facts = mg.NodeFacts(node_id=int(record["id"]), host=node.host, busy_tcp=busy["tcp"],
                                     busy_udp=busy["udp"], cert_pem=pems[0], key_pem=pems[1])
                imp = mg.Importer(panel, backup, node_name, facts, label or node.label, dry_run, self.log)
                mapping = imp.ensure_inbounds()
                live = {i["tag"]: i["id"] for i in panel.inbounds()}
                cascade = [live[t] for t in (xc_cascade(node_name), xc_direct(node_name)) if t in live]
                imp.ensure_clients(mapping, cascade)
                self._import_warp(imp, ssh, node_name, backup)
                imp.ensure_sub_path()
            finally:
                ssh.close()
        if dry_run:
            return imp.result
        report = self.apply({node_name}, skip_provision=True)
        report.changes = imp.result.changes + report.changes
        report.warnings = imp.result.warnings + report.warnings
        return report

    DEFAULT_WARP = {"tag": "warp", "protocol": "wireguard",
                    "settings": {"mtu": 1280, "secretKey": "", "address": [], "reserved": [], "workers": 2,
                                 "peers": [{"publicKey": "", "endpoint": "engage.cloudflareclient.com:2408",
                                            "allowedIPs": ["0.0.0.0/0", "::/0"]}]}}

    def ensure_node_warp(self, panel: Panel, node, fresh: bool = False) -> Optional[str]:
        """Own WARP registration on the node, and its WARP entries routed into it. Returns a warning or None."""
        from . import migrate as mg
        from . import xrayconf as xc
        node_id = next((n["id"] for n in panel.nodes() if n.get("name") == node.name), None)
        tags = {i["tag"] for i in panel.inbounds() if i.get("nodeId") == node_id
                and i["tag"] in (xc.warp_tag(node.name), xc.warpup_tag(node.name))}
        if not tags:
            return None
        st = self.state.server(node.name)
        try:
            ssh = self.ssh(node).connect()
        except SSHError as exc:
            return f"{node.name}: WARP не настроен — нет доступа по SSH ({exc})"
        try:
            token = pv._mint_token(ssh, "xui-fleet-admin", "admin")
            with ssh.forward(int(st["panel_port"])) as port:
                node_panel = Panel(f"{st['scheme']}://127.0.0.1:{port}{st['base_path']}", token)
                try:
                    node_panel.wait_ready(60)
                    template = node_panel.xray_template()
                    current = next((o for o in template.get("outbounds") or [] if o.get("tag") == "warp"), None)
                    registered = node_panel.post("/xray/warp/data")
                    if fresh:
                        self.state.drop(node.name, "warp_endpoint")
                    if not fresh and current is not None and registered and current.get("settings", {}).get("secretKey"):
                        warp = current
                    else:
                        reg = node_panel.post("/xray/warp/reg", form=mg.new_wireguard_keys())
                        warp = mg.warp_from_registration(json.loads(json.dumps(self.DEFAULT_WARP)),
                                                         json.loads(reg) if isinstance(reg, str) else reg)
                        self.log(f"{node.name}: зарегистрировал собственный WARP")
                    # Keep whatever else the owner already sends to WARP (e.g. an imported entry).
                    for rule in (template.get("routing") or {}).get("rules") or []:
                        if rule.get("outboundTag") == "warp":
                            tags |= set(rule.get("inboundTag") or [])
                    endpoint = self._warp_endpoint(node, ssh, warp)
                    if not fresh and self._warp_bad.get(node.name):
                        # Every entry point slow or silent: the WARP identity itself has gone bad (seen on a
                        # node whose session degraded to 5 s per connection). A new registration fixes it.
                        self.log(f"⚠ {node.name}: WARP отвечает медленно на всех точках входа — регистрирую заново")
                        reg = node_panel.post("/xray/warp/reg", form=mg.new_wireguard_keys())
                        warp = mg.warp_from_registration(json.loads(json.dumps(self.DEFAULT_WARP)),
                                                         json.loads(reg) if isinstance(reg, str) else reg)
                        self.state.drop(node.name, "warp_endpoint")
                        endpoint = self._warp_endpoint(node, ssh, warp)
                    merged = mg.merge_warp(template, warp, sorted(tags), endpoint)
                    if merged != template:
                        node_panel.save_xray_template(merged)
                        node_panel.restart_xray()
                        self.log(f"{node.name}: WARP-маршрут для {', '.join(sorted(tags))}")
                finally:
                    node_panel.close()
        finally:
            ssh.close()
        return None

    WARP_RECHECK = 24 * 3600

    def _warp_endpoint(self, node, ssh: SSH, warp: Dict[str, Any]) -> str:
        """Best Cloudflare entry point for this server, measured once a day (the routes change)."""
        saved = self.state.get(node.name, "warp_endpoint") or {}
        if saved.get("endpoint") and time.time() - saved.get("at", 0) < self.WARP_RECHECK:
            return saved["endpoint"]
        best, scores = pv.pick_warp_endpoint(ssh, warp)
        self._warp_bad[node.name] = not best or scores[best] > 1.5
        if not best:
            self.log(f"⚠ {node.name}: ни одна точка входа WARP не ответила — оставляю прежнюю")
            return saved.get("endpoint", "")
        shown = ", ".join(f"{ep} {t:.2f} с" if t < 99 else f"{ep} ✗" for ep, t in sorted(scores.items(), key=lambda x: x[1]))
        if best != saved.get("endpoint"):
            self.log(f"{node.name}: WARP через {best} (замер: {shown})")
        self.state.update(node.name, warp_endpoint={"endpoint": best, "at": time.time(), "scores": scores})
        return best

    def _import_warp(self, imp, ssh: SSH, node_name: str, backup) -> None:
        from . import migrate as mg
        tags, warp = backup.warp_tags(), backup.warp_outbound()
        if not tags or not warp:
            return
        st = self.state.server(node_name)
        token = pv._mint_token(ssh, "xui-fleet-admin", "admin")
        with ssh.forward(int(st["panel_port"])) as port:
            node_panel = Panel(f"{st['scheme']}://127.0.0.1:{port}{st['base_path']}", token)
            try:
                node_panel.wait_ready(60)
                template = node_panel.xray_template()
                current = next((o for o in template.get("outbounds") or [] if o.get("tag") == "warp"), None)
                own = node_panel.post("/xray/warp/data")
                if current is not None and own and current.get("settings", {}).get("secretKey") != warp["settings"].get("secretKey"):
                    warp = current  # already has its own WARP identity from an earlier run
                else:
                    reg = node_panel.post("/xray/warp/reg", form=mg.new_wireguard_keys())
                    warp = mg.warp_from_registration(warp, json.loads(reg) if isinstance(reg, str) else reg)
                    self.log(f"{node_name}: зарегистрировал для сервера собственный WARP (старый занят прежним сервером)")
                merged = mg.merge_warp(template, warp, tags)
                if merged != template:
                    def apply() -> None:
                        node_panel.save_xray_template(merged)
                        node_panel.restart_xray()
                    imp._do(f"{node_name}: WARP как на старом сервере для {', '.join(tags)}", apply)
            finally:
                node_panel.close()

    def check_links(self, email: Optional[str] = None) -> Report:
        """Real traffic through every link of one user's subscription, run from the master."""
        report = Report()
        with self.master_panel(provision=False) as panel:
            clients = [c for c in panel.clients() if c.get("subId") and c["email"] != self.cfg.clients.bridge_email]
            user = next((c for c in clients if c["email"] == email), clients[0] if clients else None)
            if user is None:
                raise FleetError("нет ни одного пользователя с подпиской")
            s = panel.settings()
            hosts = [h for h in (self.cfg.tls_for(self.cfg.master).domain, self.cfg.master.host) if h]
            outs, last_err = None, None
            for host in hosts:  # the domain may still point at the old server right after a move
                url = f"https://{host}:{s.get('subPort', 2096)}{s.get('subPath', '/sub/')}{user['subId']}"
                try:
                    parsed = panel.post("/xray/outbound-subs/parse", form={"url": url})
                    outs = parsed.get("outbounds") if isinstance(parsed, dict) else parsed
                    break
                except PanelError as exc:
                    last_err = exc
            if outs is None:
                raise FleetError(f"главный сервер не смог прочитать подписку: {last_err}")
            node_host = {n["id"]: n.get("address") for n in panel.nodes()}
            cfg_host = {n.name: n.host for n in self.cfg.nodes}
            node_host.update({n["id"]: cfg_host.get(n["name"], n.get("address")) for n in panel.nodes()})
            master_hosts = {self.cfg.master.host, self.cfg.tls_for(self.cfg.master).domain}
            names: Dict[Any, str] = {}
            for i in panel.inbounds():  # ports repeat across servers, so match on (address, port, protocol)
                proto = "hysteria" if i["protocol"] == "hysteria" else "vless"
                hosts = {node_host.get(i["nodeId"])} if i.get("nodeId") else master_hosts | {i.get("shareAddr")}
                for h in hosts:
                    names[(h, int(i["port"]), proto)] = i["remark"]
            self.log(f"проверяю {len(outs or [])} подключений пользователя {user['email']} с главного сервера "
                     f"(из российского дата-центра; домашние сети и мобильные операторы могут вести себя иначе)")
            for n, o in enumerate(outs or []):
                st = o.get("settings") or {}
                h = (st.get("vnext") or st.get("servers") or [st])[0]
                o["tag"] = f"check-{n}"
                result = {}
                for _ in range(2):
                    try:
                        result = panel.test_outbound(o)
                    except PanelError as exc:
                        result = {"error": str(exc)}
                    if result.get("success"):
                        break
                key = (h.get("address"), int(h.get("port") or 0), "hysteria" if o.get("protocol") == "hysteria" else "vless")
                label = names.get(key) or f"{h.get('address')}:{h.get('port')}"
                if result.get("success"):
                    eg = result.get("egress") or {}
                    self.log(f"  ✅ {label} — {result.get('delay')} мс, выход {eg.get('ipv4') or '?'} ({eg.get('country') or '?'})")
                else:
                    msg = f"{label} — не работает: {(result.get('error') or 'нет ответа')[:100]}"
                    self.log("  ❌ " + msg)
                    report.warnings.append(msg)
        return report

    # ---- backups and moving the master ----------------------------------
    @property
    def backup_dir(self) -> Path:
        """Permanent place on this computer, independent of where the project folder lives
        (see service.data_dir). XUI_FLEET_BACKUP_DIR overrides it. Backups made earlier inside the
        project are moved here.
        """
        from .service import backup_dir
        target = backup_dir(self.config_path)
        legacy = self.config_path.parent / ".xui-fleet" / "backups"
        if legacy.is_dir() and legacy.resolve() != target.resolve():
            target.mkdir(parents=True, exist_ok=True)
            os.chmod(target, 0o700)
            for old in legacy.glob("master-*.tar.gz"):
                if not (target / old.name).exists():
                    shutil.copy2(old, target / old.name)
                    os.chmod(target / old.name, 0o600)
                old.unlink()
            try:
                legacy.rmdir()
            except OSError:
                pass
        return target

    def backups(self):
        from . import backup as bk
        return bk.list_backups(self.backup_dir)

    def backup_master(self, reason: str = "вручную"):
        """Database exported by the panel + the files it depends on, kept on this computer (newest 10)."""
        from . import backup as bk
        with self.master_panel(provision=False) as panel:
            db = panel.download("/server/getDb")
            settings = panel.settings()
            files: Dict[str, bytes] = {}
            if self._master_ssh is not None:
                wanted = {settings.get(k) for k in ("webCertFile", "webKeyFile", "subCertFile", "subKeyFile")}
                wanted |= {"/etc/x-ui/node_token_key.json", "/etc/default/x-ui"}
                for remote in sorted(p for p in wanted if p):
                    out = self._master_ssh.run(f"[ -f {remote} ] && base64 -w0 {remote} || true", check=False).out.strip()
                    if out:
                        files[remote] = __import__("base64").b64decode(out)
        counts = bk.db_summary(db)
        st = self.state.server(self.cfg.master.name)
        tls = self.cfg.tls_for(self.cfg.master)
        meta = {"created": time.time(), "reason": reason, "name": self.cfg.master.name, "host": self.cfg.master.host,
                "domain": tls.domain or "", "panel_port": st.get("panel_port"), "base_path": st.get("base_path"),
                "xui_version": self.cfg.panel.version, "counts": counts, "files": sorted(files)}
        path = bk.write_bundle(self.backup_dir, db, files, meta)
        self.log(f"бэкап главного сервера: {path.name} — подключений {counts['inbounds']}, клиентов {counts['clients']}, "
                 f"нод {counts['nodes']}, файлов {len(files)}")
        return bk.list_backups(self.backup_dir)[0]

    def auto_backup(self, max_age_hours: float = 12, reason: str = "автоматически") -> None:
        """Before changes and on start: a fresh backup unless a recent one exists. Never blocks the work."""
        latest = self.backups()
        if latest and time.time() - latest[0].created < max_age_hours * 3600:
            return
        try:
            self.backup_master(reason)
        except Exception as exc:
            self.log(f"⚠ автоматический бэкап не удался: {exc}")

    def migrate_master(self, new_host: str, ssh: Dict[str, Any], password: Optional[str] = None,
                       backup_file: Optional[str] = None) -> Report:
        """Move the master's 3x-ui to another server from a backup (when the old one is blocked or dead)."""
        from . import backup as bk
        from .config import SSHConfig, set_master_host
        master = self.cfg.master
        if new_host == master.host:
            raise FleetError("новый адрес совпадает с текущим")
        if backup_file is None:
            try:
                self.log("пробую сделать свежий бэкап со старого сервера…")
                path = self.backup_master("перед переносом").path
            except Exception as exc:
                existing = self.backups()
                if not existing:
                    raise FleetError(f"старый сервер недоступен ({exc}), а сохранённых бэкапов нет") from exc
                path = existing[0].path
                self.log(f"старый сервер недоступен — беру последний бэкап {path.name}")
        else:
            path = self.backup_dir / backup_file
        bundle = bk.read_bundle(path)
        counts = bk.db_summary(bundle["db"])
        env = bundle["files"].get("/etc/default/x-ui", b"").decode(errors="replace")
        if "XUI_DB_TYPE=postgres" in env:
            raise FleetError("перенос поддерживается только для SQLite-панели")
        self.log(f"бэкап {path.name}: подключений {counts['inbounds']}, клиентов {counts['clients']}, нод {counts['nodes']}")

        temp_name = f"{master.name}-new"
        self.state.forget(temp_name)
        cfg = SSHConfig(user=ssh.get("user") or "root", port=int(ssh.get("port") or 22), key=ssh.get("key") or None,
                        password=password or None)
        new = SSH(temp_name, new_host, cfg, self.state).connect()
        try:
            facts = pv._facts(new)
            if facts.get("init") != "systemd":
                raise FleetError("на новом сервере нужен Linux с systemd")
            if not facts.get("xui"):
                self.log(f"новый сервер: устанавливаю 3x-ui {self.cfg.panel.version}")
                stub = master.model_copy(update={"host": new_host, "panel_port": None})
                pv._install(new, self.cfg.panel.version, stub, self.cfg.tls_for(master).model_copy(update={"mode": "none"}))
            self.log("новый сервер: восстанавливаю базу 3x-ui и файлы из бэкапа")
            new.run("systemctl stop x-ui; rm -f /etc/x-ui/x-ui.db-wal /etc/x-ui/x-ui.db-shm")
            new.run(bk.upload_script({"/etc/x-ui/x-ui.db": bundle["db"], **bundle["files"]}))
            new.run("systemctl start x-ui")
            settings = pv._settings(new)
            cert = pv._cert_path(new)
            scheme = "https" if cert else "http"
            token = pv._mint_token(new, pv.MASTER_TOKEN, "admin")
            if not pv._wait_token(new, scheme, settings["port"], settings["base_path"], token):
                raise FleetError("панель на новом сервере не запустилась — смотрите `journalctl -u x-ui` на нём")
            sub_port = new.run(pv._ENV + f"{pv.XUI} setting -show true 2>/dev/null | sed -n 's/^subPort: //p'", check=False).out.strip()
            for port in (settings["port"], int(sub_port) if sub_port.isdigit() else 2096, 80):
                pv.open_port(new, int(port))
            host_key = self.state.get(temp_name, "ssh_host_key")
        finally:
            new.close()

        old = self.state.server(master.name)
        self.state.update(f"{master.name}-previous", **{k: v for k, v in old.items() if k != "ssh_password"}, host=master.host)
        self.state.forget(master.name)
        self.state.update(master.name, api_token=token, scheme=scheme, panel_port=settings["port"],
                          base_path=settings["base_path"], ssh_host_key=host_key, cert_pin="",
                          **({"ssh_password": password} if password else {}))
        self.state.forget(temp_name)
        set_master_host(self.config_path, new_host, {k: v for k, v in (("user", cfg.user), ("port", cfg.port),
                                                                        ("key", cfg.key)) if v})
        self.reload()
        self.log(f"главный сервер теперь {new_host}; привожу ноды и каскады в порядок")
        report = self.apply(skip_provision=True)
        domain = self.cfg.tls_for(self.cfg.master).domain
        if domain:
            ssh_ = self.ssh(self.cfg.master).connect()
            try:
                ips = pv.resolve_public(ssh_, domain)
            finally:
                ssh_.close()
            if ips != [new_host]:
                report.warnings.insert(0, f"Перенаправьте домен {domain} на {new_host} (A-запись у регистратора, сейчас "
                                          f"{', '.join(ips) or 'нет записи'}). Пока DNS не обновится, пользователи ходят на старый сервер. "
                                          f"После обновления нажмите «Домен подписки», чтобы новый сервер сам продлевал сертификат.")
        return report

    def hide_legacy(self, node_name: str, keep: Optional[Dict[str, str]] = None) -> Report:
        """Hide a node's non-service inbounds from subscriptions; they keep working for cached configs.

        `keep` maps tags that must stay visible to their new remark ("" keeps the current one).
        """
        keep = keep or {}
        report = Report()
        self.auto_backup(max_age_hours=0, reason=f"перед скрытием старых подключений {node_name}")
        with self.master_panel(provision=False) as panel:
            node_id = next((n["id"] for n in panel.nodes() if n.get("name") == node_name), None)
            if node_id is None:
                raise FleetError(f"нода {node_name} не зарегистрирована на мастере")
            for inbound in panel.inbounds():
                if inbound.get("nodeId") != node_id or str(inbound.get("tag", "")).startswith("fleet-"):
                    continue
                tag = inbound["tag"]
                visible = tag in keep
                remark = keep.get(tag) or inbound["remark"]
                if bool(inbound.get("excludeFromSub")) == (not visible) and inbound["remark"] == remark:
                    continue
                updated = {k: v for k, v in inbound.items() if k not in ("clientStats", "up", "down", "fallbackParent", "originNodeGuid")}
                updated.update(excludeFromSub=not visible, remark=remark)
                if visible and tag in keep and keep[tag]:
                    updated["subSortIndex"] = 4001  # after the unified set
                msg = (f"{node_name}: «{remark}» остаётся в подписке" if visible
                       else f"{node_name}: «{inbound['remark']}» скрыт из подписки (работает у тех, кто не обновился)")
                report.changes.append(msg)
                self.log(msg)
                panel.update_inbound(inbound["id"], updated)
        return report

    def remove_node(self, name: str) -> Report:
        self.auto_backup(max_age_hours=0, reason=f"перед удалением сервера {name}")
        with self.master_panel(provision=False) as panel:
            rec = Reconciler(panel, self.cfg, countries=self.countries(), neighbors=self.neighbors(), health=self.health(), log=self.log)
            rec.remove_node(name)
            self.state.forget(name)
            return rec.report

    def add_user(self, name: str, comment: str = "", total_gb: float = 0) -> str:
        from .config import UserConfig
        self.cfg.users.append(UserConfig(name=name, comment=comment, total_gb=total_gb))
        with self.master_panel(provision=False) as panel:
            rec = Reconciler(panel, self.cfg, countries=self.countries(), neighbors=self.neighbors(), health=self.health(), log=self.log)
            rec._ensure_users({}, {})
            rec._collect_subscriptions()
            if name not in rec.report.subscriptions:
                raise FleetError("пользователь не создан: на мастере ещё нет ни одного inbound (сначала `apply`)")
            return rec.report.subscriptions[name]

    def subscriptions(self) -> Dict[str, str]:
        with self.master_panel(provision=False) as panel:
            rec = Reconciler(panel, self.cfg, countries=self.countries(), neighbors=self.neighbors(), health=self.health(), log=self.log)
            rec._collect_subscriptions()
            return rec.report.subscriptions

    # ---- user statistics --------------------------------------------------
    def user_stats(self) -> Dict[str, Dict[str, Any]]:
        """Traffic, servers in use right now, devices and IPs of every user (see stats.py)."""
        from . import stats
        with self.master_panel(provision=False) as panel:
            clients = panel.clients_with_traffic()
            names = stats.server_names(panel.nodes(), self.cfg.master.name)
            online, ips = panel.onlines_by_server(), panel.ips_by_server()
            bridge = self.cfg.clients.bridge_email
            emails = [c["email"] for c in clients if c.get("email") and c["email"] != bridge]

            def count(email: str) -> Optional[int]:
                try:
                    return len([d for d in panel.client_devices(email) if not stats.is_service_device(d)])
                except PanelError:
                    return None  # a panel without device tracking: shown as "—", never as 0
            devices = dict(zip(emails, parallel(emails, count, 30, lambda _e: None)))
            return stats.summarize(clients, names, online, ips, devices, hidden=[bridge])

    def user_detail(self, name: str) -> Dict[str, Any]:
        """One user: the list row plus every device and recent IP with its server."""
        from . import stats
        with self.master_panel(provision=False) as panel:
            client = next((c for c in panel.clients_with_traffic() if c.get("email") == name), None)
            if not client:
                raise FleetError(f"пользователя {name} нет на главном сервере")
            names = stats.server_names(panel.nodes(), self.cfg.master.name)
            devices = panel.client_devices(name)
            row = stats.summarize([client], names, panel.onlines_by_server(), panel.ips_by_server(),
                                  {name: len([d for d in devices if not stats.is_service_device(d)])})[name]
            ips = sorted(panel.client_ips(name), key=lambda x: str(x.get("time") or ""), reverse=True)
            row["deviceList"] = sorted((stats.device_view(d) for d in devices), key=lambda d: -d["lastSeen"])
            row["ipList"] = [{"ip": i.get("ip", ""), "time": i.get("time", ""), "server": i.get("node") or self.cfg.master.name}
                             for i in ips]
            return row

    def set_user_limits(self, name: str, devices: Any, ips: Any) -> None:
        """Limits live in the panel (fleet.yaml only seeds new users), so `apply` never undoes them."""
        from . import stats
        fields = {"limitHwid": stats.validate_limit(devices, "устройств"), "limitIp": stats.validate_limit(ips, "IP-адресов")}
        with self.master_panel(provision=False) as panel:
            panel.update_client(name, fields)
        self.log(f"{name}: устройств — {fields['limitHwid'] or 'без ограничения'}, IP — {fields['limitIp'] or 'без ограничения'}")

    def forget_device(self, name: str, device_id: int) -> None:
        """Free a device slot: the device has to fetch the subscription again to come back."""
        with self.master_panel(provision=False) as panel:
            panel.forget_device(name, device_id)

    def status(self) -> Tuple[List[dict], List[dict]]:
        with self.master_panel(provision=False) as panel:
            def probe(node):  # refresh, do not show a stale heartbeat; an offline node must not hold the rest
                try:
                    panel.probe_node(node["id"])
                except PanelError:
                    pass
            parallel([n for n in panel.nodes() if n.get("enable", True)], probe, 10, lambda n: None)
            nodes, inbounds = panel.nodes(), panel.inbounds()
            try:
                self.master_status = panel.status() or {}
            except PanelError:
                self.master_status = {}
            try:  # who is connected right now and when everyone was last seen (deduped across nodes)
                self.last_activity = {"online": sorted(panel.post("/clients/onlines") or []),
                                      "lastOnline": panel.post("/clients/lastOnline") or {}}
            except PanelError:
                self.last_activity = {"online": [], "lastOnline": {}}
            return nodes, inbounds

    def upgrade(self, only: Optional[Set[str]] = None) -> Dict[str, str]:
        """Nodes first, the master last: a node must never be older than its master."""
        def one(server):
            try:
                ssh = self.ssh(server).connect()
                try:
                    self.log(f"{server.name}: обновляю до {self.cfg.panel.version}")
                    return pv.upgrade(ssh, self.cfg.panel.version)
                finally:
                    ssh.close()
            except (SSHError, PanelError, RuntimeError) as exc:
                return f"ошибка: {exc}"
        pick = lambda s: only is None or s.name in only  # noqa: E731
        nodes = [n for n in self.cfg.nodes if n.enabled and pick(n)]
        result = dict(zip((n.name for n in nodes), parallel(nodes, one, 1800, lambda n: "ошибка: не уложился в 30 минут")))
        if pick(self.cfg.master):
            result[self.cfg.master.name] = one(self.cfg.master)
        return result
