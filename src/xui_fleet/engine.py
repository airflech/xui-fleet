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
from . import xrayconf as xc
from .xrayconf import cascade_tag as xc_cascade, direct_tag as xc_direct

Log = Callable[[str], None]



def parse_master_rtts(out: str) -> Dict[str, int]:
    """Lines "<node> ping <avg ms>" or "<node> tcp <seconds> <seconds> …" from _master_rtts → {node: ms}."""
    import statistics
    got: Dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        name, kind, vals = parts[0], parts[1], parts[2:]
        nums = [float(v) for v in vals if v.replace(".", "", 1).isdigit() and float(v) > 0]
        if not nums:
            continue
        got[name] = round(nums[0]) if kind == "ping" else round(statistics.median(nums) * 1000)
    return got


def public_sub_port(s: Dict[str, Any]) -> int:
    """The port users fetch subscriptions on. Behind the app gate (nginx) 3x-ui itself listens on 127.0.0.1 and
    the public address is its subURI."""
    from urllib.parse import urlparse
    if str(s.get("subListen") or "") == "127.0.0.1" and s.get("subURI"):
        port = urlparse(str(s["subURI"])).port
        if port:
            return port
    return int(s.get("subPort") or 2096)


BEFORE_CHANGE_HOURS = 0.25  # a restore point before every change; a series of quick edits shares one

class FleetError(RuntimeError):
    pass


def _short(exc: Exception) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return text if len(text) <= 160 else text[:157] + "…"


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
        """WARP exit on the master for its own WARP entry (master.vpn or master.warp): own registration, best endpoint."""
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
    def master_panel(self, provision: bool = True, wait: int = 90) -> Iterator[Panel]:
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
            panel.wait_ready(wait)
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
                    f"фильтруется по пути от главного сервера: через него не будет работать ни управление, ни каскад. "
                    f"Нужен другой IP или другой зарубежный сервер."
                )
                self.log("[ошибка] " + failed[name])
                access.pop(name)
            elif ok < 6:
                warnings.append(f"{name}: связь {(self.cfg.master.label or 'главный').strip()} → {acc.address} нестабильна ({ok} из 6 соединений) — "
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
        self.backup_before("перед: " + (self.purpose or (
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
            if (self.cfg.master.vpn or self.cfg.master.warp) and self._master_ssh is not None:
                access[self.cfg.master.name] = self._master_facts()
            healthy = None if only is None and not failed else {n.name for n in nodes if n.name not in failed}
            used = self.health()
            report = Reconciler(panel, self.cfg, countries=self.countries(), neighbors=self.neighbors(), health=used, log=self.log).run(access, healthy)
            if self._master_ssh is not None:
                try:
                    gate = self.ensure_sub_gate(panel, self._master_ssh)
                    if gate:
                        report.changes.append(gate)
                except Exception as exc:  # subscriptions keep working without the gate (it rolls itself back)
                    report.warnings.append(f"мастер: фильтр приложений для подписки не применён: {exc}")
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
            if (self.cfg.master.vpn and self.cfg.defaults.set.warp) or self.cfg.master.warp:
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

    def rotate_masking(self, name: str, new_site: bool = True) -> str:
        """New disguise for one server's entries, when they started to get blocked: new Reality keys and short id
        on its VLESS entries, a new salamander password on its Hysteria2 ones, and — if the subnet has one — a
        different real site to look like. Users only need to refresh the subscription; nothing else changes."""
        import copy
        import secrets as _s
        from .reconcile import _flatten_nodes
        is_master = name == self.cfg.master.name
        node = None if is_master else self.cfg.node(name)
        self.backup_before(f"перед сменой маскировки {name}", max_age_hours=0)
        done: List[str] = []
        if node is not None and new_site:
            explicit = (node.direct.reality.target or node.direct.reality.server_names or
                        self.cfg.defaults.direct.reality.target or self.cfg.defaults.direct.reality.server_names)
            if explicit:
                self.log(f"{name}: сайт для маскировки задан в настройках — его не меняю")
            else:
                old = self.state.get(name, "reality_neighbor") or {}
                self.log(f"{name}: ищу в подсети другой сайт для маскировки (до минуты)…")
                ssh = self.ssh(node).connect()
                try:
                    found = pv.find_reality_neighbor(ssh, exclude=tuple(x for x in [old.get("ip")] if x))
                finally:
                    ssh.close()
                if found:
                    self.state.update(name, reality_neighbor={"ip": found[0], "name": found[1], "checked": time.time()})
                    done.append(f"сайт для маскировки: {old.get('name') or 'по умолчанию'} → {found[1]}")
                else:
                    self.log(f"{name}: другого подходящего сайта в подсети нет — сайт оставляю, меняю ключи")
        with self.master_panel(provision=False) as panel:
            node_id = None
            if not is_master:
                node_id = next((n.get("id") for n in _flatten_nodes(panel.nodes()) if n.get("name") == name), None)
                if node_id is None:
                    raise FleetError(f"сервер {name} не подключён к главной панели — сначала «Исправить»")
            keys_n = obfs_n = 0
            for inb in panel.inbounds():
                kind = xc.inbound_kind(inb.get("tag", ""))
                if not kind or kind[0] == "warpup" and is_master:
                    continue
                mine = (inb.get("nodeId") == node_id) if not is_master else (not inb.get("nodeId"))
                if not mine:
                    continue
                cur = copy.deepcopy(inb)
                stream = cur.get("streamSettings") or {}
                if stream.get("security") == "reality":
                    keys = panel.new_x25519()
                    rs = stream["realitySettings"]
                    rs["privateKey"] = keys["privateKey"]
                    rs.setdefault("settings", {})["publicKey"] = keys["publicKey"]
                    rs["shortIds"] = [_s.token_hex(8)]
                    keys_n += 1
                elif cur.get("protocol") == "hysteria":
                    for m in ((stream.get("finalmask") or {}).get("udp") or []):
                        if m.get("type") == "salamander":
                            m.setdefault("settings", {})["password"] = _s.token_hex(8)
                            obfs_n += 1
                else:
                    continue
                panel.update_inbound(inb["id"], cur)
            if keys_n:
                done.append(f"новые ключи Reality у {keys_n} подключений")
            if obfs_n:
                done.append(f"новый пароль обфускации у {obfs_n} подключений Hysteria2")
        for line in done:
            self.log(f"{name}: {line}")
        # The node's site goes into its entries, the master's cascades pick up the new keys.
        self.reload()
        self.apply(None if is_master else {name}, skip_provision=True)
        return (f"Маскировка {name} сменена: " + "; ".join(done) + ". Пользователям нужно обновить подписку "
                "(в Happ — потянуть список вниз); подключения других серверов не изменились.") if done else "Менять было нечего"

    def drift(self) -> Dict[str, Any]:
        """What differs between fleet.yaml and the servers right now — read-only and quiet, for the page's
        status ("all set" / "N to fix"). Only the master panel is asked; servers never prepared are listed
        as to do, without SSH (that is what fixing them does)."""
        nodes = [n for n in self.cfg.nodes if n.enabled]
        if not (self.cfg.master.api_url or self.state.get(self.cfg.master.name, "api_token")):
            return {"changes": ["главный сервер ещё не настроен"], "warnings": []}
        pending = [n.name for n in nodes if not self._access_from_state(n)]
        with self.master_panel(provision=False) as panel:
            access = {n.name: a for n in nodes for a in [self._access_from_state(n)] if a}
            dry = Reconciler(panel, self.cfg, countries=self.countries(), neighbors=self.neighbors(), health=self.health(),
                             dry_run=True, log=lambda _m: None).run(access, {n.name for n in nodes if n.name not in pending})
            gate = self.sub_gate_drift(panel.settings())
        return {"changes": [f"{p}: сервер ещё не подключён — установить и подключить" for p in pending] + list(dry.changes)
                + ([gate] if gate else []),
                "warnings": list(dry.warnings)}

    # ---- subscriptions only for chosen apps -------------------------------
    def sub_gate_drift(self, s: Dict[str, Any]) -> Optional[str]:
        """Without SSH: does 3x-ui sit where the app gate wants it (behind nginx, or on the public port)?"""
        apps = self.cfg.clients.allowed_apps
        behind = str(s.get("subListen") or "") == "127.0.0.1"
        if apps and not behind:
            return "мастер: подписку будут получать только " + pv._app_names(apps) + " (остальным — сообщение, что приложение не поддерживается)"
        if not apps and behind and self.state.get(self.cfg.master.name, "sub_gate"):
            return "мастер: подписку снова будут получать любые приложения"
        return None

    def ensure_sub_gate(self, panel: Panel, ssh: SSH) -> Optional[str]:
        """Put nginx in front of the subscription server when clients.allowed_apps is set, or take it away.
        The public port and the links stay the same; 3x-ui moves to 127.0.0.1. Returns what changed, or None."""
        master = self.cfg.master
        apps = self.cfg.clients.allowed_apps
        s = panel.settings()
        gate = self.state.get(master.name, "sub_gate") or {}
        behind = str(s.get("subListen") or "") == "127.0.0.1"

        def restart(settings: Dict[str, Any]) -> None:
            panel.update_settings(settings)
            panel.restart_panel()
            time.sleep(4)
            panel.wait_ready(90)

        if not apps:
            if not gate and not behind:
                return None
            public = int(gate.get("port") or public_sub_port(s))
            pv.remove_sub_gate(ssh)  # frees the public port first
            if behind:
                s["subListen"], s["subPort"] = "", public
                if not gate or s.get("subURI") == gate.get("uri"):
                    s["subURI"] = ""
                restart(s)
            self.state.drop(master.name, "sub_gate")
            msg = f"мастер: подписку снова получают любые приложения (3x-ui на порту {public})"
            self.log(msg)
            return msg

        public = int(gate.get("port") or (public_sub_port(s) if behind else s.get("subPort") or 2096))
        facts = pv.sub_gate_facts(ssh)
        # Already behind nginx (e.g. set up from another computer whose state is not here): keep 3x-ui where it is.
        internal = int(gate.get("internal") or (s.get("subPort") if behind else 0) or facts["free"])
        tls = self.cfg.tls_for(master)
        cert, key = str(s.get("subCertFile") or ""), str(s.get("subKeyFile") or "")
        host = s.get("subDomain") or (tls.domain if tls.mode == "domain" and tls.domain else master.host)
        uri = f"{'https' if cert and key else 'http'}://{host}:{public}{s.get('subPath', '/sub/')}"
        ips = list(facts["ips"]) + [master.host]
        domains = [tls.domain, *tls.reserve_domains] if tls.mode == "domain" and tls.domain else []
        moved_from = str(self.state.get(master.name, "domain_moved_from") or "")
        conf = pv.sub_gate_conf(public, internal, cert, key, apps, ips, ipv6=facts["ipv6"], domains=domains,
                                moved_from=moved_from)
        before = dict(s)
        move = not behind or int(s.get("subPort") or 0) != internal or s.get("subURI") != uri
        try:
            changed = pv.write_sub_gate(ssh, conf)
            if move:
                s["subListen"], s["subPort"], s["subURI"] = "127.0.0.1", internal, uri
                restart(s)  # 3x-ui leaves the public port to nginx
            if (changed or move) and not pv.reload_nginx_on(ssh, public):
                raise FleetError(f"nginx не занял порт {public}")
        except Exception:
            # Never leave subscriptions without a listener: back to 3x-ui on the public port.
            pv.remove_sub_gate(ssh)
            if move:
                restart(before)
            raise
        self.state.update(master.name, sub_gate={"port": public, "internal": internal, "uri": uri, "apps": list(apps)})
        if not (changed or move):
            return None
        msg = (f"мастер: подписку получают только {pv._app_names(apps)}; остальным приложениям — сообщение, что они не "
               f"поддерживаются (nginx на порту {public}, 3x-ui на 127.0.0.1:{internal})")
        self.log(msg)
        return msg

    def set_app_gate(self) -> str:
        """Apply clients.allowed_apps right away (the page's switch)."""
        self.backup_before("перед изменением списка приложений для подписки")
        with self.master_panel(provision=False) as panel:
            if self._master_ssh is None:
                raise FleetError("фильтр приложений ставится на главный сервер по SSH, а он подключён напрямую (api_url)")
            self.ensure_sub_gate(panel, self._master_ssh)  # logs what it changed
            Reconciler(panel, self.cfg, countries=self.countries(), neighbors=self.neighbors(), health=self.health(),
                       log=self.log)._ensure_subscription_settings()
        apps = self.cfg.clients.allowed_apps
        return ("Готово: подписку получают только " + pv._app_names(apps) + ", остальные приложения видят сообщение, что они не поддерживаются"
                if apps else "Готово: подписку получают любые приложения")

    def set_vpn_name(self) -> str:
        """Put clients.vpn_name into the panel (3x-ui subTitle → the title apps show). An empty name clears it."""
        name = self.cfg.clients.vpn_name
        self.backup_before("перед изменением названия VPN")
        with self.master_panel(provision=False) as panel:
            s = panel.settings()
            if str(s.get("subTitle") or "") == name:
                return "Название уже такое"
            s["subTitle"] = name
            panel.update_settings(s)
            panel.restart_panel()
            time.sleep(4)
            panel.wait_ready(90)
        self.log(f"мастер: название VPN в приложениях — «{name}»" if name else "мастер: название VPN убрано")
        return (f"Готово: приложения покажут «{name}» после обновления подписки" if name
                else "Готово: название убрано, приложения покажут своё")

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
        port, path = public_sub_port(s), s.get("subPath", "/sub/")
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
        self.backup_before(f"перед сменой домена на {domain}")
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
            path, port = s.get("subPath", "/sub/"), public_sub_port(s)
        mode_before = tls.address_mode
        set_master_domain(self.config_path, domain, reserves)
        self.reload()
        if old and old != domain:
            # Apps that still reach the old domain are told the new address (new-url, see sub_gate_conf).
            self.state.update(master.name, domain_moved_from=old)
        if self.cfg.clients.allowed_apps:  # nginx in front of the subscriptions takes the new certificate and address
            with self.master_panel(provision=False) as panel:
                if self._master_ssh is not None:
                    self.ensure_sub_gate(panel, self._master_ssh)
        if mode_before != self.cfg.tls_for(master).address_mode:
            self.log(f"адреса подключений через главный сервер: теперь основной домен {domain} — режим «только резервный» снят, "
                     f"иначе ссылки вели бы на прежний домен")
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
        self.backup_before("перед изменением резервного домена")
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
        mode_before = tls.address_mode
        set_master_domain(self.config_path, tls.domain, reserves)
        self.reload()
        if self.cfg.clients.allowed_apps:  # the personal fallback addresses follow the new set of domains
            with self.master_panel(provision=False) as panel:
                if self._master_ssh is not None:
                    self.ensure_sub_gate(panel, self._master_ssh)
        if not reserves:
            if mode_before != "main":
                # the hosts of the removed domain still carry the links: back to the main domain right away
                self.log(f"адреса подключений через главный сервер: снова основной домен {tls.domain}")
                self.apply(skip_provision=True)
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
            if "added_ms" not in saved:  # measured by an older version: add the delay figure once
                self.state.update(node.name, warp_endpoint=dict(saved, added_ms=self._warp_added(node, ssh, warp, saved["endpoint"])))
            return saved["endpoint"]
        best, scores = pv.pick_warp_endpoint(ssh, warp)
        self._warp_bad[node.name] = not best or scores[best] > 1.5
        if not best:
            self.log(f"⚠ {node.name}: ни одна точка входа WARP не ответила — оставляю прежнюю")
            return saved.get("endpoint", "")
        shown = ", ".join(f"{ep} {t:.2f} с" if t < 99 else f"{ep} ✗" for ep, t in sorted(scores.items(), key=lambda x: x[1]))
        if best != saved.get("endpoint"):
            self.log(f"{node.name}: WARP через {best} (замер: {shown})")
        self.state.update(node.name, warp_endpoint={"endpoint": best, "at": time.time(), "scores": scores,
                                                    "added_ms": self._warp_added(node, ssh, warp, best)})
        return best

    def _warp_added(self, node, ssh: SSH, warp: Dict[str, Any], endpoint: str) -> Optional[int]:
        """The delay WARP adds on this server (shown on the route map); None if it could not be measured."""
        try:
            ms = pv.warp_added_ms(ssh, warp, endpoint)
        except Exception:  # a figure for the map: never fail the WARP setup over it
            return None
        if ms is not None:
            self.log(f"{node.name}: WARP добавляет к задержке около {ms} мс")
        return ms

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
            check = self.cfg.clients.check_email
            if email is None and not any(c.get("email") == check for c in panel.clients()):
                # made by `apply`; created here too, so the check never borrows a real user's subscription
                Reconciler(panel, self.cfg, countries=self.countries(), neighbors=self.neighbors(), health=self.health(),
                           log=self.log)._ensure_users({}, {})
            clients = [c for c in panel.clients() if c.get("subId") and c["email"] != self.cfg.clients.bridge_email]
            user = next((c for c in clients if c["email"] == (email or check)), None)
            if user is None and email is None:
                user = next((c for c in clients if c["email"] != check), None)
                if user:
                    self.log(f"⚠ служебного клиента {check} нет — проверяю по подписке {user['email']}")
            if user is None:
                raise FleetError("нет ни одного пользователя с подпиской")
            s = panel.settings()
            tls = self.cfg.tls_for(self.cfg.master)
            hosts = [h for h in (tls.domain, *tls.reserve_domains, self.cfg.master.host) if h]
            outs, last_err = None, None
            for host in hosts:  # the domain may still point at the old server right after a move
                url = f"https://{host}:{public_sub_port(s)}{s.get('subPath', '/sub/')}{user['subId']}"
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
            master_hosts = {self.cfg.master.host, tls.domain, *tls.reserve_domains}  # reserve links come from 3x-ui hosts
            names: Dict[Any, str] = {}
            for i in panel.inbounds():  # ports repeat across servers, so match on (address, port, protocol)
                proto = "hysteria" if i["protocol"] == "hysteria" else "vless"
                hosts = {node_host.get(i["nodeId"])} if i.get("nodeId") else master_hosts | {i.get("shareAddr")}
                for h in hosts:
                    names[(h, int(i["port"]), proto)] = i["remark"]
            whose = "служебной подписки" if user["email"] == check else f"пользователя {user['email']}"
            self.log(f"проверяю {len(outs or [])} подключений {whose} с главного сервера "
                     f"(из сети главного сервера; домашние сети и мобильные операторы могут вести себя иначе)")
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
                if names.get(key) and h.get("address") in tls.reserve_domains:
                    label += " · резерв"
                if result.get("success"):
                    eg = result.get("egress") or {}
                    self.log(f"  ✅ {label} — {result.get('delay')} мс, выход {eg.get('ipv4') or '?'} ({eg.get('country') or '?'})")
                else:
                    msg = f"{label} — не работает: {(result.get('error') or 'нет ответа')[:100]}"
                    self.log("  ❌ " + msg)
                    report.warnings.append(msg)
        return report

    def probe_warp(self) -> Dict[str, Optional[int]]:
        """Does WARP work on each server right now? Real traffic from the master through each server's WARP entry
        (the service user's subscription, like the link check): {server: delay ms, or None if it failed}.
        Servers without a WARP entry are left out.

        Alongside, the same request through an entry of the same server without WARP (same protocol, same way in:
        direct for the server's WARP entry, via the master for the one via the master): the difference is what WARP
        adds, kept in self.warp_added {server: ms} — measured every time, no SSH, no settings needed."""
        from .xrayconf import inbound_kind
        got: Dict[str, Optional[int]] = {}
        self.warp_added: Dict[str, int] = {}
        with self.master_panel(provision=False) as panel:
            check = self.cfg.clients.check_email
            user = next((c for c in panel.clients() if c.get("email") == check and c.get("subId")), None)
            if user is None:
                return got
            s = panel.settings()
            tls = self.cfg.tls_for(self.cfg.master)
            host = tls.domain if tls.mode == "domain" and tls.domain else self.cfg.master.host
            try:
                parsed = panel.post("/xray/outbound-subs/parse",
                                    form={"url": f"https://{host}:{public_sub_port(s)}{s.get('subPath', '/sub/')}{user['subId']}"})
            except PanelError:
                return got
            outs = parsed.get("outbounds") if isinstance(parsed, dict) else parsed
            node_host = {n["id"]: n.get("address") for n in panel.nodes()}
            cfg_host = {n.name: n.host for n in self.cfg.nodes}
            node_host.update({n["id"]: cfg_host.get(n["name"], n.get("address")) for n in panel.nodes()})
            # The server's own WARP entry (master → server → Cloudflare); if that one is hidden from subscriptions,
            # the entry via the master → WARP tells the same about the server's WARP.
            master_hosts = {self.cfg.master.host, tls.domain, *tls.reserve_domains}
            entry_at: Dict[tuple, Tuple[str, str]] = {}  # (address, port, protocol) → (kind, server)
            for i in panel.inbounds():
                kind = inbound_kind(str(i.get("tag", "")))
                if not kind:
                    continue
                proto = "hysteria" if i["protocol"] == "hysteria" else "vless"
                hosts = {node_host.get(i["nodeId"])} if i.get("nodeId") else master_hosts | {i.get("shareAddr")}
                for hh in hosts:
                    entry_at[(hh, int(i["port"]), proto)] = (kind[0], kind[1])
            # baseline for each WARP way in: the plain entries of the same server and the same way in
            base_kinds = {"warp": ("direct", "xhttp", "hy2"), "rwarp": ("cascade", "rxh", "rhy2")}
            found: Dict[str, Dict[str, list]] = {}
            for n, o in enumerate(outs or []):
                st = o.get("settings") or {}
                h = (st.get("vnext") or st.get("servers") or [st])[0]
                proto = "hysteria" if o.get("protocol") == "hysteria" else "vless"
                hit = entry_at.get((h.get("address"), int(h.get("port") or 0), proto))
                if hit:
                    found.setdefault(hit[1], {}).setdefault(hit[0], []).append((proto, n, o))

            def best(o: Dict[str, Any], tag: str) -> Optional[int]:
                """The faster of two tries (the first one through a fresh path pays for the handshakes)."""
                times = []
                for _ in range(2):
                    o["tag"] = tag
                    try:
                        r = panel.test_outbound(o)
                    except PanelError:
                        r = {}
                    if r.get("success"):
                        times.append(int(r.get("delay") or 0))
                return min(times) if times else None
            for name, kinds in found.items():
                for wkind in ("warp", "rwarp"):
                    if wkind not in kinds or got.get(name) is not None:
                        continue
                    proto, n, o = kinds[wkind][0]
                    got[name] = best(o, f"warp-probe-{n}")
                    if got[name] is None:
                        continue
                    plain = [e for k in base_kinds[wkind] for e in kinds.get(k, [])]
                    plain.sort(key=lambda e: e[0] != proto)  # the same protocol first
                    if plain:
                        b = best(plain[0][2], f"warp-base-{plain[0][1]}")
                        if b is not None:
                            self.warp_added[name] = max(0, got[name] - b)
        # The figure shown is the ping from each server to its Cloudflare entry point — the WARP leg itself, measured
        # like every other leg of the map and steady; the traffic difference above (noisier) only fills in where
        # that ping does not answer.
        servers = [n for n in self.cfg.nodes if n.name in got]
        if self.cfg.master.name in got:  # the master's own WARP (master.warp / master.vpn)
            servers.append(self.cfg.local_node())
        self.warp_added.update(self.warp_pings(servers))
        return got

    def warp_pings(self, nodes: List[NodeConfig]) -> Dict[str, int]:
        """Ping from each server to the Cloudflare entry point its WARP uses (state warp_endpoint), ms, in parallel
        over SSH. Servers whose entry is not known yet or that get no ping reply are left out."""
        import shlex

        def one(node: NodeConfig) -> Optional[int]:
            ep = str((self.state.get(node.name, "warp_endpoint") or {}).get("endpoint") or "")
            ip = ep.rsplit(":", 1)[0] if ":" in ep else ep
            if not ip:
                return None
            try:
                ssh = self.ssh(node).connect()
                try:
                    # Cloudflare answers pings with spikes (1–30 ms on a 2 ms path): the median of ten, not the mean
                    out = ssh.run(f"ping -c 10 -i 0.2 -W 1 {shlex.quote(ip)} 2>/dev/null | grep -o 'time=[0-9.]*' | cut -d= -f2",
                                  check=False, timeout=25).out.split()
                finally:
                    ssh.close()
                import statistics
                times = [float(t) for t in out if t.replace(".", "", 1).isdigit()]
                return max(0, round(statistics.median(times))) if len(times) >= 3 else None
            except Exception:  # no SSH or no ping: the traffic difference stays
                return None
        got = dict(zip([n.name for n in nodes], parallel(nodes, one, 30, lambda _n: None))) if nodes else {}
        return {k: v for k, v in got.items() if v is not None}

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

    def backup_before(self, reason: str, max_age_hours: float = BEFORE_CHANGE_HOURS) -> None:
        """A restore point before a change to the master: always for deletions and resets (max_age_hours=0),
        otherwise unless one was made in the last 15 minutes (a series of edits shares one)."""
        if self.state.get(self.cfg.master.name, "api_token") and not self.cfg.master.api_url:
            self.auto_backup(max_age_hours=max_age_hours, reason=reason)

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
            gate_port = (self.state.get(self.cfg.master.name, "sub_gate") or {}).get("port")  # nginx's, behind the app gate
            for port in (settings["port"], int(sub_port) if sub_port.isdigit() else 2096, 80, *([gate_port] if gate_port else [])):
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

    def remove_node(self, name: str, purge: bool = False) -> Report:
        """Take a node out of the network. purge: also remove 3x-ui and everything xui-fleet put on it."""
        node = self.cfg.node(name)
        self.auto_backup(max_age_hours=0, reason=f"перед удалением сервера {name}")
        with self.master_panel(provision=False, wait=30) as panel:
            ports = self._server_ports(panel, name) if purge else []
            rec = Reconciler(panel, self.cfg, countries=self.countries(), neighbors=self.neighbors(), health=self.health(), log=self.log)
            rec.remove_node(name)
        if purge:
            self._purge(node, ports, rec.report)
        self.state.forget(name)
        return rec.report

    def master_probe(self, wait: int = 15) -> Dict[str, Any]:
        """Does the master panel answer? Asked before deleting the master: without it there is no backup."""
        latest = self.backups()
        out: Dict[str, Any] = {"alive": False, "error": "", "lastBackup": latest[0].created if latest else None}
        try:
            with self.master_panel(provision=False, wait=wait):
                out["alive"] = True
        except Exception as exc:
            out["error"] = _short(exc)
        return out

    def remove_master(self, purge: bool = False, purge_nodes: bool = False, sync_data: bool = False,
                      without_backup: bool = False) -> Report:
        """Take down the whole network of this profile. The master's backup is made first (it holds every
        user), so the network can be brought back with «Перенос главного сервера». When no backup can be
        made, nothing is touched unless the user agreed to go on without one (without_backup)."""
        report = Report()
        ports: Dict[str, List[int]] = {}
        alive = False
        try:  # a short check first: the panel may be long gone, and waiting 90 s for it twice looks like a hang
            self.log("проверяю, отвечает ли главная панель…")
            with self.master_panel(provision=False, wait=15) as panel:
                alive = True
                for server in [self.cfg.master] + (list(self.cfg.nodes) if purge_nodes else []):
                    ports[server.name] = self._server_ports(panel, server.name)
        except Exception as exc:
            problem = f"главная панель не отвечает ({_short(exc)})"
        else:
            problem = ""
            try:
                self.backup_master("перед удалением главного сервера")
            except Exception as exc:
                problem = f"бэкап не удался ({_short(exc)})"
        if problem:
            if not without_backup:
                raise FleetError(f"{problem} — без бэкапа ничего не удаляю. Подтвердите удаление без бэкапа или "
                                 "сначала верните панель.")
            self.log(f"⚠ {problem} — удаляю без бэкапа, как вы подтвердили"
                     + ("; на сервере — по SSH то, что известно из настроек" if purge else ""))
        if not purge:
            self.log("серверы не меняются: 3x-ui на них продолжает работать, пользователи — подключаться")
            return report
        if purge_nodes:
            for node in self.cfg.nodes:
                self._purge(node, ports.get(node.name) or [], report)
        self._purge(self.cfg.master, ports.get(self.cfg.master.name) or [], report, sync_data=sync_data)
        return report

    def _server_ports(self, panel: Panel, name: str) -> List[int]:
        """Ports opened for xui-fleet's connections hosted on this server, plus its panel port."""
        from .reconcile import _flatten_nodes
        if name == self.cfg.master.name:
            hosts: tuple = (None, 0)  # the master's own entries carry no nodeId (or 0)
        else:
            hosts = (next((n.get("id") for n in _flatten_nodes(panel.nodes()) if n.get("name") == name), -1),)
        ports = [int(i["port"]) for i in panel.inbounds()
                 if str(i.get("tag", "")).startswith("fleet-") and i.get("port") and i.get("nodeId") in hosts]
        panel_port = self.state.get(name, "panel_port")
        return ports + ([int(panel_port)] if panel_port else [])

    def _purge(self, server, ports: List[int], report: Report, sync_data: bool = False) -> None:
        """Uninstall on one server; a failure is reported, never stops the rest."""
        tls = self.cfg.tls_for(server)
        domains = ([tls.domain] if tls.domain else []) + list(tls.reserve_domains or [])
        if tls.mode == "ip" and pv._is_ip(server.host):
            domains.append(server.host)  # the installer's Let's Encrypt certificate for the IP, renewed by acme.sh
        try:
            ssh = self.ssh(server).connect()
        except Exception as exc:
            report.warnings.append(f"{server.name}: не удалось подключиться по SSH ({exc}) — 3x-ui на сервере остался")
            return
        try:
            self.log(f"{server.name}: удаляю 3x-ui и всё, что ставил xui-fleet")
            for line in pv.uninstall(ssh, ports, domains, self.cfg.ssh_for(server).port or 22, sync_data=sync_data):
                self.log(f"{server.name}: {line}")
            report.changes.append(f"{server.name}: 3x-ui удалён с сервера")
        except Exception as exc:
            report.warnings.append(f"{server.name}: удаление 3x-ui не завершено: {exc}")
        finally:
            ssh.close()

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
            service = self.cfg.clients.service_emails
            emails = [c["email"] for c in clients if c.get("email") and c["email"] not in service]

            def count(email: str) -> Optional[int]:
                try:
                    return len([d for d in panel.client_devices(email) if not stats.is_service_device(d)])
                except PanelError:
                    return None  # a panel without device tracking: shown as "—", never as 0
            devices = dict(zip(emails, parallel(emails, count, 30, lambda _e: None)))
            return stats.summarize(clients, names, online, ips, devices, hidden=service)

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
        self.backup_before(f"перед изменением лимитов {name}")
        with self.master_panel(provision=False) as panel:
            panel.update_client(name, fields)
        self.log(f"{name}: устройств — {fields['limitHwid'] or 'без ограничения'}, IP — {fields['limitIp'] or 'без ограничения'}")

    def edit_user(self, name: str, body: Dict[str, Any]) -> None:
        """Comment, traffic limit, expiry and on/off — in the panel, and in fleet.yaml when the user is listed there."""
        from . import stats
        from .config import update_user_entry
        fields = stats.user_fields(body)
        self.backup_before(f"перед изменением пользователя {name}")
        with self.master_panel(provision=False) as panel:
            panel.update_client(name, fields)
        update_user_entry(self.config_path, name, fields["comment"], round(fields["totalGB"] / 1024 ** 3, 3))

    def reset_user_traffic(self, name: str) -> None:
        self.backup_before(f"перед сбросом трафика {name}", max_age_hours=0)
        with self.master_panel(provision=False) as panel:
            panel.reset_traffic(name)

    def delete_user(self, name: str) -> Report:
        """Delete everywhere: the panel (every connection, the subscription) and fleet.yaml."""
        from .config import remove_user_entry
        if name in self.cfg.clients.service_emails:
            raise FleetError("это служебный клиент xui-fleet (каскады и проверка подключений) — его удалять нельзя")
        report = Report()
        self.auto_backup(max_age_hours=0, reason=f"перед удалением пользователя {name}")
        with self.master_panel(provision=False) as panel:
            if panel.client(name) is not None:
                panel.delete_client(name)
                report.changes.append(f"мастер: удалён пользователь {name} и его подписка")
        if remove_user_entry(self.config_path, name):
            report.changes.append(f"{name} убран из fleet.yaml")
        self.log(f"{name}: удалён")
        return report

    # ---- inbounds («Входящие» of the master panel) --------------------------
    def inbounds_view(self) -> List[Dict[str, Any]]:
        """Every inbound of the master panel, the ones it hosts on the nodes included."""
        from .reconcile import _flatten_nodes
        with self.master_panel(provision=False) as panel:
            hosts = {n.get("id"): n.get("name") for n in _flatten_nodes(panel.nodes())}
            rows = []
            for i in panel.inbounds():
                stream = i.get("streamSettings") or {}
                clients = (i.get("settings") or {}).get("clients") or []
                kind = xc.inbound_kind(i.get("tag", ""))
                rows.append({
                    "id": i.get("id"), "tag": i.get("tag", ""), "remark": i.get("remark", ""), "port": i.get("port"),
                    "protocol": i.get("protocol", ""), "network": stream.get("network", ""), "security": stream.get("security", ""),
                    "enabled": bool(i.get("enable", True)), "hidden": bool(i.get("excludeFromSub")),
                    "server": hosts.get(i.get("nodeId")) or self.cfg.master.name, "up": int(i.get("up") or 0),
                    "down": int(i.get("down") or 0), "clients": len(clients),
                    "managed": bool(kind), "kind": kind[0] if kind else "", "owner": kind[1] if kind else "",
                    "service": bool(kind) and kind[0] == "warpup",
                })
            return sorted(rows, key=lambda r: (r["server"] != self.cfg.master.name, r["server"], r["port"] or 0))

    def edit_inbound(self, inbound_id: int, body: Dict[str, Any]) -> Optional[str]:
        """Name, port, on/off of one inbound. Returns the server to re-apply (xui-fleet's own entries:
        the cascade and the firewall follow a new port), or None."""
        from .config import set_direct_port
        self.backup_before("перед изменением входящего")
        with self.master_panel(provision=False) as panel:
            cur = next((i for i in panel.inbounds() if i.get("id") == inbound_id), None)
            if cur is None:
                raise FleetError("такого входящего уже нет — обновите список")
            kind = xc.inbound_kind(cur.get("tag", ""))
            update = dict(cur)
            port = int(body.get("port") or cur["port"])
            if not 0 < port < 65536:
                raise FleetError("порт: от 1 до 65535")
            if port != cur["port"]:
                udp = cur.get("protocol") == "hysteria"
                clash = next((i for i in panel.inbounds() if i.get("id") != inbound_id and int(i.get("port") or 0) == port
                              and i.get("nodeId") == cur.get("nodeId") and (i.get("protocol") == "hysteria") == udp), None)
                if clash:
                    raise FleetError(f"порт {port} на этом сервере уже занят: «{clash.get('remark')}»")
            update["port"] = port
            if "enabled" in body:
                update["enable"] = bool(body["enabled"])
            remark = str(body.get("remark") if body.get("remark") is not None else cur.get("remark", "")).strip()
            if kind and remark != cur.get("remark", ""):
                raise FleetError("название подключений xui-fleet берётся из названия сервера — "
                                 "измените его в «Название и подключения» сервера")
            update["remark"] = remark
            panel.update_inbound(inbound_id, update)
            if bool(update.get("enable", True)) != bool(cur.get("enable", True)):
                panel.set_inbound_enable(inbound_id, bool(update["enable"]))
            self.log(f"«{remark}»: порт {port}, {'включено' if update.get('enable', True) else 'выключено'}")
        if kind and port != cur["port"]:
            if kind[0] == "direct":
                set_direct_port(self.config_path, kind[1], port)
            return kind[1]
        return None

    def delete_inbound(self, inbound_id: int) -> Optional[str]:
        """Delete one inbound. xui-fleet's own: that kind is turned off for its server in fleet.yaml and the
        server is returned to re-apply (otherwise `apply` would create it again)."""
        from .config import disable_connection
        self.auto_backup(max_age_hours=0, reason="перед удалением входящего")
        with self.master_panel(provision=False) as panel:
            cur = next((i for i in panel.inbounds() if i.get("id") == inbound_id), None)
            if cur is None:
                raise FleetError("такого входящего уже нет — обновите список")
            kind = xc.inbound_kind(cur.get("tag", ""))
            if not kind:
                panel.delete_inbound(inbound_id)
                self.log(f"удалено «{cur.get('remark')}» (порт {cur.get('port')})")
                return None
        disable_connection(self.config_path, kind[1], kind[0])
        self.log(f"«{cur.get('remark')}»: этот тип подключения выключен у сервера {kind[1]} в fleet.yaml"
                 + (" (прямое подключение скрывается: через него идут каскады)" if kind[0] == "direct" else ""))
        return kind[1]

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
            self.master_rtts = self._master_rtts()
            return nodes, inbounds

    def _master_rtts(self) -> Dict[str, int]:
        """RU master → each foreign server, ms, measured on the master over the SSH connection already open: ping
        (average of four), or the time to open a TCP connection for a server that ignores pings. The panel's own
        node latency is the time of an HTTP call to the node's panel, well above the network delay and jumpy."""
        if self._master_ssh is None:
            return {}
        import shlex
        lines = []
        for n in self.cfg.nodes:
            d = self.cfg.direct_for(n)
            port = d.port if d.enabled else (self.cfg.ssh_for(n).port or 22)
            name, host = shlex.quote(n.name), shlex.quote(n.host)
            lines.append(f"( p=$(ping -c 4 -i 0.2 -W 1 -q {host} 2>/dev/null | awk -F/ '/^(rtt|round-trip)/{{print $5}}'); "
                         f"if [ -n \"$p\" ]; then echo {name} ping $p; else echo {name} tcp $(for i in 1 2 3; do "
                         f"curl -s -o /dev/null -m 2 -w '%{{time_connect}} ' telnet://{host}:{int(port)} </dev/null; done); fi ) &")
        lines.append("wait")
        try:
            out = self._master_ssh.run("\n".join(lines), check=False, timeout=30).out
        except Exception:  # a figure for the map: never fail the status over it
            return {}
        return parse_master_rtts(out)

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
