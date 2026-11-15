"""Bring the master panel to the state described by the config (idempotent)."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import geo
from . import xrayconf as xc
from .config import FleetConfig, NodeConfig
from .panel import Panel, PanelError


@dataclass
class NodeAccess:
    """How the master reaches a provisioned node panel."""

    scheme: str
    address: str
    port: int
    base_path: str
    api_token: Optional[str]
    token_fresh: bool = False  # minted in this run: must be pushed to the master
    tls_verify_mode: str = "verify"
    pinned_cert_sha256: str = ""
    allow_private_address: bool = False
    direct_port: Optional[int] = None  # free port picked on the node (443 may be taken)
    # Filled during provisioning, needed only to create the XHTTP/Hysteria2 entries.
    busy_tcp: Set[int] = field(default_factory=set)
    busy_udp: Set[int] = field(default_factory=set)
    cert_pem: str = ""
    key_pem: str = ""
    cert_pin_hex: str = ""


@dataclass
class Report:
    changes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    subscriptions: Dict[str, str] = field(default_factory=dict)
    failed: List[str] = field(default_factory=list)  # servers that could not be prepared
    cascade_ok: Dict[str, bool] = field(default_factory=dict)  # node -> RU -> node carried real traffic


LOCAL = 0  # node id of the master itself (master.vpn): its entries have no nodeId


def _nid(node_id: Any) -> Optional[int]:
    return None if node_id == LOCAL else node_id


def _on(inbound: Dict[str, Any], node_id: Any) -> bool:
    return (inbound.get("nodeId") or None) == _nid(node_id)


class Reconciler:
    def __init__(self, panel: Panel, cfg: FleetConfig, dry_run: bool = False,
                 log: Callable[[str], None] = lambda _m: None, countries: Optional[Dict[str, str]] = None,
                 neighbors: Optional[Dict[str, Tuple[str, str]]] = None,
                 health: Optional[Dict[str, float]] = None):
        self.panel, self.cfg, self.dry, self.log = panel, cfg, dry_run, log
        self.countries = countries or {}  # node name -> ISO code detected from its IP
        self.neighbors = neighbors or {}  # node name -> (ip:443, name) of a real site in its subnet
        self._enc_cache: Optional[Dict[str, str]] = None
        self.health = health or {}  # node name -> share of RU -> node connections that opened (1.0 = stable)
        self.report = Report()

    def _direct(self, node: NodeConfig):
        tls = self.cfg.tls_for(self.cfg.master)
        if node.name == self.cfg.master.name and tls.mode == "domain" and tls.domain:
            # The master's own entries "steal" its own domain: the nginx site behind it is real.
            return self.cfg.direct_for(node, (f"127.0.0.1:{self.cfg.master.decoy_port}", tls.domain))
        return self.cfg.direct_for(node, self.neighbors.get(node.name))

    def _vless_enc(self) -> Dict[str, str]:
        """A fresh ML-KEM-768 + X25519 pair for VLESS Encryption, from the panel's own generator."""
        if self.dry:
            return {"decryption": "<new>", "encryption": "<new>"}
        data = self.panel.get("/server/getNewVlessEnc")
        auths = (data or {}).get("auths") or []
        pick = next((a for a in auths if a.get("id") == "mlkem768"), None)
        if not pick:
            raise PanelError("панель не выдала ключи VLESS Encryption (нужен 3x-ui v3.10+)")
        return {"decryption": pick["decryption"], "encryption": pick["encryption"]}

    def _refresh_entry(self, node: NodeConfig, cur: Dict[str, Any], remark: str, sort_index: int,
                       hidden: bool, protect: bool) -> None:
        """Keep an existing node-hosted VLESS entry in line: name, order, SNI/fingerprint and, for XHTTP,
        VLESS Encryption + XMUX. Port, keys and path are kept."""
        if cur.get("protocol") != "vless":
            self._sync_remark(cur, remark, sort_index, hidden)
            return
        target, names, fp = self._direct(node).reality.resolved()
        update = xc.inbound_drift(cur, remark=remark, port=int(cur["port"]), target=target, server_names=names,
                                  exclude_from_sub=hidden, sort_index=sort_index, fingerprint=fp)
        base = update or cur
        hardened = xc.harden_xhttp(base, self._vless_enc()) if protect and (
            not xc.is_protected(base) or not ((base.get("streamSettings") or {}).get("xhttpSettings") or {}).get("xmux")) else None
        final = hardened or update
        if final is None:
            return
        what = [_what_changed(cur, update)] if update else []
        if hardened:
            what.append("включаю VLESS Encryption + XMUX")
        relink = hardened or (update and _what_changed(cur, update) != "порядок в подписке")
        self._do(f"{node.name}: обновляю «{remark}» ({'; '.join(what)})" + (" — пользователям нужно обновить подписку" if relink else ""),
                 lambda u=final, i=cur["id"]: self.panel.update_inbound(i, u))

    def _hy2_cert(self, node: NodeConfig, node_id: Any, acc: Optional["NodeAccess"]) -> Optional[Dict[str, Any]]:
        """Certificate for a Hysteria2 entry: a node's own cert pinned in the links; on the master with a domain,
        the domain's Let's Encrypt cert (no pin needed)."""
        tls = self.cfg.tls_for(self.cfg.master)
        if node_id == LOCAL and tls.mode == "domain" and tls.domain:
            d = f"/root/cert/{tls.domain}"
            return {"host": tls.domain, "cert_file": f"{d}/fullchain.pem", "key_file": f"{d}/privkey.pem", "share_addr": tls.domain}
        if acc is None or not acc.cert_pem or not acc.cert_pin_hex:
            return None
        return {"host": node.host, "cert_pem": acc.cert_pem, "key_pem": acc.key_pem, "pin_hex": acc.cert_pin_hex}

    def _local_taken(self, proto: str) -> Set[int]:
        """Ports already used on the master: its inbounds of that protocol, plus the service ports (TCP)."""
        hy = proto == "udp"
        used = {int(i["port"]) for i in self.panel.inbounds() if not i.get("nodeId") and (i["protocol"] == "hysteria") == hy}
        return used | (self._master_reserved() | {n.cascade.port for n in self.cfg.nodes if n.cascade.port} if not hy else set())

    def _drop_local_entries(self) -> None:
        """master.vpn switched off: remove the master's own entries (cascades are kept)."""
        name = self.cfg.master.name
        own = {xc.direct_tag(name), xc.xhttp_tag(name), xc.hy2_tag(name), xc.warp_tag(name)}
        for tag, inbound in self._inbounds_by_tag().items():
            if tag in own and not inbound.get("nodeId"):
                self._do(f"мастер больше не VPN-сервер: удаляю «{inbound.get('remark')}»",
                         lambda i=inbound["id"]: self.panel.delete_inbound(i))

    def _master_reserved(self) -> Set[int]:
        """TCP ports on the master that no inbound may take: Xray would fail to start and take every
        entry down with it (the nginx decoy, the panel, the subscription server, SSH, ACME)."""
        if getattr(self, "_reserved", None) is None:
            s = self.panel.settings() or {}
            self._reserved = {22, 80, self.cfg.master.decoy_port} | {int(s[k]) for k in ("webPort", "subPort") if s.get(k)}
            if self.cfg.defaults.hardening.cover_site and self.cfg.tls_for(self.cfg.master).mode == "domain":
                self._reserved.add(443)  # the cover website (nginx) owns 443 on a master with a domain
        return self._reserved

    def _sort(self, node: NodeConfig, kind: Any) -> int:
        """Subscription order: grouped by country (in config order), inside a country the servers with a
        stable link first, and for each server its best connections first (see KIND_RANK)."""
        if getattr(self, "_positions", None) is None:
            nodes = self._servers()
            labels = geo.display_labels(nodes, self.countries)
            country = {n.name: geo.iso_of_flag(labels[n.name]) or n.name for n in nodes}
            groups: List[str] = []
            for n in nodes:
                if country[n.name] not in groups:
                    groups.append(country[n.name])
            self._positions = {}
            for g, iso in enumerate(groups):
                members = [n for n in nodes if country[n.name] == iso]
                members.sort(key=lambda n: (self.health.get(n.name, 1.0) < 1.0, -self.health.get(n.name, 1.0), nodes.index(n)))
                for k, n in enumerate(members):
                    self._positions[n.name] = (g, k)
        g, k = self._positions.get(node.name, (99, 0))
        return 1 + g * 1000 + k * 100 + KIND_RANK[_kind_name(kind)]

    def _servers(self) -> List[NodeConfig]:
        """Every server that gives users connections: the master itself first (master.vpn), then the nodes."""
        return ([self.cfg.local_node()] if self.cfg.master.vpn else []) + list(self.cfg.nodes)

    def _label(self, node: NodeConfig) -> str:
        if getattr(self, "_labels", None) is None:
            self._labels = geo.display_labels(self._servers(), self.countries)
        return self._labels.get(node.name) or geo.display_label(node.name, node.label, node.flag, self.countries.get(node.name))

    # ---- helpers --------------------------------------------------------
    def _do(self, message: str, fn: Callable[[], Any]) -> Any:
        self.report.changes.append(message)
        self.log(("[dry-run] " if self.dry else "") + message)
        return None if self.dry else fn()

    def _warn(self, message: str) -> None:
        self.report.warnings.append(message)

    def _inbounds_by_tag(self) -> Dict[str, Dict[str, Any]]:
        return {i.get("tag", ""): i for i in self.panel.inbounds()}

    # ---- entry point ----------------------------------------------------
    def run(self, access: Dict[str, NodeAccess], only: Optional[Set[str]] = None) -> Report:
        """`access` holds the nodes provisioned in this run; `only` limits the scope."""
        nodes = [n for n in self.cfg.nodes if n.enabled and (only is None or n.name in only)]
        self._direct_ports = {name: a.direct_port for name, a in access.items() if a.direct_port}
        node_ids = self._ensure_nodes(nodes, access)
        # master.vpn: the master gets the same connection set as a node, hosted on itself.
        local = [self.cfg.local_node()] if self.cfg.master.vpn and (only is None or self.cfg.master.name in only) else []
        entry_ids = dict(node_ids, **{n.name: LOCAL for n in local})
        direct = self._ensure_direct_inbounds(local + nodes, entry_ids)
        self._ensure_extra_inbounds(local + nodes, entry_ids, access)
        self._ensure_warp_inbounds(local + nodes, entry_ids, access)
        if not self.cfg.master.vpn:
            self._drop_local_entries()
        direct = {k: v for k, v in direct.items() if k != self.cfg.master.name}
        bridge_id = self._ensure_bridge(nodes, direct)
        cascade = self._ensure_cascade_inbounds(nodes)
        self._ensure_cascade_hy2(nodes)
        self._ensure_cascade_xhttp(nodes)
        self._ensure_master_hosts()
        self._ensure_routes(direct, cascade, bridge_id)
        self._ensure_users(direct, cascade)
        self._ensure_subscription_settings()
        self._collect_subscriptions()
        self._sync_node_tags(node_ids)
        self._check_orphans()
        self._check_master_xray()
        self._verify_nodes(node_ids)
        self._verify_cascades(nodes)
        return self.report

    def _check_master_xray(self) -> None:
        """Xray on the master must be up after our changes. If one of our inbounds keeps it from
        starting (a port taken by another program), remove that inbound and say so loudly."""
        if self.dry or not self.report.changes:
            return
        import re
        for attempt in range(3):
            deadline = time.time() + 12
            state, error = "", ""
            while time.time() < deadline:
                try:
                    x = (self.panel.status() or {}).get("xray") or {}
                except Exception:  # status is best effort
                    return
                state, error = x.get("state", ""), x.get("errorMsg", "")
                if state == "running":
                    return
                time.sleep(2)
            ports = {int(p) for p in re.findall(r"listen (?:TCP|UDP) on (\d+)|:(\d+): bind", error) for p in p if p}
            culprit = [i for i in self.panel.inbounds() if not i.get("nodeId") and str(i.get("tag", "")).startswith(xc.PREFIX)
                       and int(i["port"]) in ports]
            if not culprit:
                break
            for i in culprit:
                self.log(f"⚠ мастер: Xray не запускается — порт {i['port']} занят другой программой; убираю «{i.get('remark')}»")
                self.panel.delete_inbound(i["id"])
                self._warn(f"мастер: подключение «{i.get('remark')}» убрано — его порт {i['port']} занят на сервере другой программой")
            self.panel.restart_xray()
        self._warn(f"мастер: Xray не запущен ({state or 'нет ответа'}): {error[:300]}")

    verify_wait = 60.0  # seconds a freshly added node gets to receive its clients

    def _verify_cascades(self, nodes: List[NodeConfig]) -> None:
        """Probe RU → node through each cascade outbound; a fresh node needs a few seconds to sync."""
        if self.dry:
            return
        wanted = {xc.out_tag(n.name): n.name for n in nodes if self.cfg.cascade_for(n).enabled}
        outbounds = {o["tag"]: o for o in self.panel.xray_template().get("outbounds") or [] if o.get("tag") in wanted}
        for tag, name in wanted.items():
            if tag not in outbounds:
                continue
            deadline, result = time.time() + self.verify_wait, {}
            while True:
                try:
                    result = self.panel.test_outbound(outbounds[tag])
                except PanelError as exc:
                    result = {"error": str(exc)}
                if result.get("success") or time.time() >= deadline:
                    break
                time.sleep(5)
            self.report.cascade_ok[name] = bool(result.get("success"))
            if result.get("success"):
                egress = (result.get("egress") or {})
                where = f", выход {egress.get('ipv4')} ({egress.get('country')})" if egress.get("ipv4") else ""
                self.log(f"{name}: каскад RU → {name} работает ({result.get('delay')} мс{where})")
            else:
                self._warn(f"{name}: каскад RU → {name} не прошёл проверку: {result.get('error') or 'нет ответа'}")

    def _verify_nodes(self, node_ids: Dict[str, int]) -> None:
        """Fail loudly when the master cannot actually reach a node it should manage."""
        if self.dry:
            return
        for node_id in node_ids.values():
            try:
                self.panel.probe_node(node_id)
            except PanelError:
                pass  # the status below carries the reason
        wanted = set(node_ids)
        for node in _flatten_nodes(self.panel.nodes()):
            if node.get("name") in wanted and not node.get("transitive") and node.get("status") != "online":
                self._warn(f"{node['name']}: мастер не достучался до панели ноды "
                           f"({node.get('lastError') or node.get('status')}) — проверьте порт {node.get('port')} и firewall")

    # ---- nodes ----------------------------------------------------------
    def _ensure_nodes(self, nodes: List[NodeConfig], access: Dict[str, NodeAccess]) -> Dict[str, int]:
        existing = {n["name"]: n for n in _flatten_nodes(self.panel.nodes()) if not n.get("transitive")}
        hosted: Dict[int, Set[str]] = {}
        for inbound in self.panel.inbounds():
            if inbound.get("nodeId"):
                hosted.setdefault(int(inbound["nodeId"]), set()).add(inbound["tag"])
        ids: Dict[str, int] = {}
        for node in nodes:
            acc = access.get(node.name)
            cur = existing.get(node.name)
            if cur is None:
                if acc is None or not acc.api_token:
                    self._warn(f"{node.name}: нода не зарегистрирована на мастере и не подготовлена в этом запуске")
                    continue
                created = self._do(f"мастер: регистрирую ноду {node.name} ({acc.address}:{acc.port})",
                                   lambda a=acc, n=node: self.panel.add_node(_node_payload(n, a, [])))
                if created:
                    ids[node.name] = created["id"]
                continue
            ids[node.name] = cur["id"]
            tags = sorted(hosted.get(int(cur["id"]), set()) | {xc.direct_tag(node.name)})
            if acc is None:
                if list(cur.get("inboundTags") or []) != tags:
                    # Inbounds placed on this node (e.g. imported from a backup) must stay in its sync set.
                    self._do(f"мастер: обновляю список синхронизируемых подключений ноды {node.name}",
                             lambda i=cur["id"], t=tags: self.panel.update_node(i, _sync_payload(self.panel, i, t)))
                continue
            drift = (
                cur.get("address") != acc.address or int(cur.get("port") or 0) != acc.port
                or (cur.get("basePath") or "/") != acc.base_path or cur.get("scheme") != acc.scheme
                or (cur.get("tlsVerifyMode") or "verify") != acc.tls_verify_mode
                or cur.get("inboundSyncMode") != "selected"
                or list(cur.get("inboundTags") or []) != tags
                or (cur.get("pinnedCertSha256") or "") != acc.pinned_cert_sha256
                or not cur.get("enable") or acc.token_fresh
            )
            if drift:
                self._do(f"мастер: обновляю параметры подключения к ноде {node.name}",
                         lambda a=acc, n=node, i=cur["id"], t=tags: self.panel.update_node(i, _node_payload(n, a, t)))
        return ids

    # ---- inbounds -------------------------------------------------------
    def _keys(self) -> Dict[str, str]:
        if self.dry:
            return {"privateKey": "<new>", "publicKey": "<new>"}
        return self.panel.new_x25519()

    def _ensure_direct_inbounds(self, nodes: List[NodeConfig], node_ids: Dict[str, int]) -> Dict[str, Dict[str, Any]]:
        """One Reality inbound on every node: the user-facing entry and the cascade upstream."""
        result: Dict[str, Dict[str, Any]] = {}
        by_tag = self._inbounds_by_tag()
        for index, node in enumerate(nodes):
            direct, cascade = self._direct(node), self.cfg.cascade_for(node)
            if not (direct.enabled or cascade.enabled):
                continue
            node_id = node_ids.get(node.name)
            if node_id is None:
                continue
            tag = xc.direct_tag(node.name)
            target, names, fp = direct.reality.resolved()
            cur = by_tag.get(tag)
            # An explicit port in the config wins; otherwise keep what already works, then the free one found.
            port = (node.direct.port or (int(cur["port"]) if cur else None)
                    or getattr(self, "_direct_ports", {}).get(node.name) or direct.port or 443)
            if node_id == LOCAL and cur is None:
                port = _first_free([443, 8443, 2053, 2083, 2087, 9443] + list(range(20443, 60000, 1000)), self._local_taken("tcp"))
            want = dict(remark=xc.remark_direct(self._label(node)), port=port, target=target,
                        server_names=names, exclude_from_sub=not direct.enabled or "direct" in node.hide,
                        sort_index=self._sort(node, "direct"))
            cur = by_tag.get(tag)
            if cur is None:
                keys = self._keys()
                payload = xc.build_inbound(tag=tag, profile=xc.get_profile(direct.profile or ""), fingerprint=fp,
                                           vless_enc=self._vless_enc() if xc.get_profile(direct.profile or "").encrypted else None,
                                           private_key=keys["privateKey"], public_key=keys["publicKey"],
                                           node_id=_nid(node_id), **want)
                created = self._do(f"{node.name}: создаю inbound {tag} (:{want['port']}, SNI {names[0]})",
                                   lambda p=payload: self.panel.add_inbound(p))
                if created:
                    result[node.name] = created
                continue
            if not _on(cur, node_id):
                self._warn(f"{tag}: inbound привязан к другой ноде (nodeId={cur.get('nodeId')}), пропускаю")
                continue
            update = xc.inbound_drift(cur, fingerprint=fp, **want)
            if update:
                self._do(f"{node.name}: обновляю «{want['remark']}» ({_what_changed(cur, update)})",
                         lambda u=update, i=cur["id"]: self.panel.update_inbound(i, u))
                cur = update
            result[node.name] = cur
        return result

    def _ensure_extra_inbounds(self, nodes: List[NodeConfig], node_ids: Dict[str, int],
                               access: Dict[str, "NodeAccess"]) -> None:
        """XHTTP and Hysteria2 on every node, so each node offers the same set of entries."""
        by_tag = self._inbounds_by_tag()
        for node in nodes:
            node_id = node_ids.get(node.name)
            if node_id is None:
                continue
            wanted = self.cfg.set_for(node)
            acc = access.get(node.name)
            on_node = [i for i in by_tag.values() if _on(i, node_id)]
            taken_tcp = {int(i["port"]) for i in on_node if i["protocol"] != "hysteria"} | (acc.busy_tcp if acc else set())
            taken_udp = {int(i["port"]) for i in on_node if i["protocol"] == "hysteria"} | (acc.busy_udp if acc else set())
            if node_id == LOCAL:
                taken_tcp |= self._local_taken("tcp")
                taken_udp |= self._local_taken("udp")
            label = self._label(node)

            tag = xc.xhttp_tag(node.name)
            cur = by_tag.get(tag)
            if not wanted.xhttp:
                if cur is not None:
                    self._do(f"{node.name}: XHTTP выключен в настройках, удаляю {tag}", lambda i=cur["id"]: self.panel.delete_inbound(i))
            elif cur is None:
                if acc is None:
                    self._warn(f"{node.name}: XHTTP будет создан при следующем «Применить» с доступом к серверу")
                else:
                    port = _first_free([8443, 2053, 2083, 2087, 9443, 10443] + list(range(20443, 60000, 1000)), taken_tcp)
                    target, names, fp = self._direct(node).reality.resolved()
                    keys = self._keys()
                    protect = wanted.xhttp_encryption
                    payload = xc.build_inbound(tag=tag, remark=xc.remark_xhttp(label), port=port,
                                               profile=xc.get_profile("vless-reality-xhttp-enc" if protect else "vless-reality-xhttp"),
                                               target=target, server_names=names, fingerprint=fp, private_key=keys["privateKey"],
                                               public_key=keys["publicKey"], node_id=_nid(node_id),
                                               sort_index=self._sort(node, "xhttp"),
                                               vless_enc=self._vless_enc() if protect else None)
                    payload["excludeFromSub"] = "xhttp" in node.hide
                    self._do(f"{node.name}: создаю XHTTP{' + VLESS Encryption' if protect else ''} ({port}/tcp, SNI {names[0]})",
                             lambda p=payload: self.panel.add_inbound(p))
                    taken_tcp.add(port)
            else:
                self._refresh_entry(node, cur, xc.remark_xhttp(label), self._sort(node, "xhttp"), "xhttp" in node.hide,
                                    wanted.xhttp_encryption)

            tag = xc.hy2_tag(node.name)
            cur = by_tag.get(tag)
            if not wanted.hysteria2:
                if cur is not None:
                    self._do(f"{node.name}: Hysteria2 выключен в настройках, удаляю {tag}", lambda i=cur["id"]: self.panel.delete_inbound(i))
            elif cur is None:
                cert = self._hy2_cert(node, node_id, acc)
                if cert is None:
                    self._warn(f"{node.name}: Hysteria2 будет создан при следующем «Применить» с доступом к серверу")
                else:
                    port = _first_free([443, 8443, 2053, 2083, 9443] + list(range(20443, 60000, 1000)), taken_udp)
                    payload = xc.build_hysteria_inbound(tag=tag, remark=xc.remark_hy2(label), port=port, node_id=_nid(node_id),
                                                        sort_index=self._sort(node, "hy2"), **cert)
                    payload["excludeFromSub"] = "hy2" in node.hide
                    self._do(f"{node.name}: создаю Hysteria2 ({port}/udp, сертификат ноды закреплён в ссылке)",
                             lambda p=payload: self.panel.add_inbound(p))
            else:
                self._sync_remark(cur, xc.remark_hy2(label), self._sort(node, "hy2"), "hy2" in node.hide)

    def _ensure_warp_inbounds(self, nodes: List[NodeConfig], node_ids: Dict[str, int],
                              access: Dict[str, "NodeAccess"]) -> None:
        """WARP entry for users plus a hidden upstream the master uses for RU -> node -> WARP."""
        by_tag = self._inbounds_by_tag()
        for node in nodes:
            node_id = node_ids.get(node.name)
            if node_id is None:
                continue
            want = self.cfg.set_for(node)
            acc = access.get(node.name)
            on_node = [i for i in by_tag.values() if _on(i, node_id)]
            taken_tcp = {int(i["port"]) for i in on_node if i["protocol"] != "hysteria"} | (acc.busy_tcp if acc else set())
            taken_udp = {int(i["port"]) for i in on_node if i["protocol"] == "hysteria"} | (acc.busy_udp if acc else set())
            if node_id == LOCAL:
                taken_tcp |= self._local_taken("tcp")
                taken_udp |= self._local_taken("udp")
            label = self._label(node)
            target, names, fp = self._direct(node).reality.resolved()
            ports = [8443, 2053, 2083, 2087, 9443, 10443] + list(range(20443, 60000, 1000))

            tag, cur = xc.warp_tag(node.name), by_tag.get(xc.warp_tag(node.name))
            if not want.warp:
                if cur is not None:
                    self._do(f"{node.name}: WARP выключен, удаляю {tag}", lambda i=cur["id"]: self.panel.delete_inbound(i))
            elif cur is not None:
                self._refresh_entry(node, cur, xc.remark_warp(label), self._sort(node, "warp"), "warp" in node.hide,
                                    want.xhttp_encryption)
            elif acc is None or (want.warp_transport == "hy2" and self._hy2_cert(node, node_id, acc) is None):
                self.log(f"{node.name}: WARP-подключение будет создано при «Применить» с доступом к серверу")
            else:
                if want.warp_transport == "hy2":
                    port = _first_free([443] + ports, taken_udp)
                    payload = xc.build_hysteria_inbound(tag=tag, remark=xc.remark_warp(label), port=port, node_id=_nid(node_id),
                                                        sort_index=self._sort(node, "warp"), **self._hy2_cert(node, node_id, acc))
                    taken_udp.add(port)
                    what = f"{port}/udp, Hysteria2"
                else:
                    port = _first_free(ports, taken_tcp)
                    keys = self._keys()
                    protect = want.xhttp_encryption
                    payload = xc.build_inbound(tag=tag, remark=xc.remark_warp(label), port=port,
                                               profile=xc.get_profile("vless-reality-xhttp-enc" if protect else "vless-reality-xhttp"),
                                               target=target, server_names=names, fingerprint=fp, private_key=keys["privateKey"],
                                               public_key=keys["publicKey"], node_id=_nid(node_id), sort_index=self._sort(node, "warp"),
                                               vless_enc=self._vless_enc() if protect else None)
                    taken_tcp.add(port)
                    what = f"{port}/tcp, XHTTP"
                payload["excludeFromSub"] = "warp" in node.hide
                self._do(f"{node.name}: создаю WARP-подключение ({what})", lambda p=payload: self.panel.add_inbound(p))

            tag, cur = xc.warpup_tag(node.name), by_tag.get(xc.warpup_tag(node.name))
            needed = want.cascade_warp and self.cfg.cascade_for(node).enabled and node_id != LOCAL
            if not needed:
                if cur is not None:
                    self._do(f"{node.name}: цепочка RU → WARP выключена, удаляю {tag}", lambda i=cur["id"]: self.panel.delete_inbound(i))
            elif cur is None:
                port = _first_free(ports, taken_tcp)
                keys = self._keys()
                payload = xc.build_inbound(tag=tag, remark=f"{label} · служебный вход RU → WARP", port=port,
                                           profile=xc.get_profile("vless-reality-tcp"), target=target, server_names=names,
                                           fingerprint=fp, private_key=keys["privateKey"], public_key=keys["publicKey"],
                                           node_id=_nid(node_id), exclude_from_sub=True, sort_index=9001)
                self._do(f"{node.name}: создаю служебный вход для цепочки RU → WARP ({port}/tcp)", lambda p=payload: self.panel.add_inbound(p))
            else:
                self._refresh_entry(node, cur, cur.get("remark") or "", 9001, True, False)

    def _sync_node_tags(self, node_ids: Dict[str, int]) -> None:
        """Keep each node's sync list equal to the inbounds it hosts, including ones created in this run."""
        if self.dry:
            return
        hosted: Dict[int, Set[str]] = {}
        for inbound in self.panel.inbounds():
            if inbound.get("nodeId"):
                hosted.setdefault(int(inbound["nodeId"]), set()).add(inbound["tag"])
        for node in _flatten_nodes(self.panel.nodes()):
            if node.get("transitive") or node.get("id") not in node_ids.values():
                continue
            tags = sorted(hosted.get(int(node["id"]), set()) | {xc.direct_tag(node["name"])})
            if list(node.get("inboundTags") or []) != tags:
                self._do(f"мастер: обновляю список синхронизируемых подключений ноды {node['name']}",
                         lambda i=node["id"], t=tags: self.panel.update_node(i, _sync_payload(self.panel, i, t)))

    def _sync_remark(self, cur: Dict[str, Any], remark: str, sort_index: int, hidden: bool = False) -> None:
        if (cur.get("remark") == remark and int(cur.get("subSortIndex") or 1) == sort_index
                and bool(cur.get("excludeFromSub")) == hidden):
            return
        updated = {k: v for k, v in cur.items() if k not in ("clientStats", "up", "down", "fallbackParent", "originNodeGuid")}
        updated.update(remark=remark, subSortIndex=sort_index, excludeFromSub=hidden)
        what = "скрываю из подписки" if hidden and not cur.get("excludeFromSub") else \
               "возвращаю в подписку" if not hidden and cur.get("excludeFromSub") else \
               "обновляю название" if cur.get("remark") != remark else "меняю порядок в подписке:"
        self._do(f"{what} «{remark}»", lambda: self.panel.update_inbound(cur["id"], updated))

    def _ensure_cascade_hy2(self, nodes: List[NodeConfig]) -> None:
        """Hysteria2 entry on the master for each node, sharing the node's cascade exit."""
        self._new_rhy2: Set[str] = set()
        tls = self.cfg.tls_for(self.cfg.master)
        by_tag = self._inbounds_by_tag()
        udp_taken = {int(i["port"]) for i in by_tag.values() if not i.get("nodeId") and i["protocol"] == "hysteria"}
        kinds = [(xc.rhy2_tag, xc.remark_cascade_hy2, "rhy2", "cascade_hy2", lambda n: self.cfg.set_for(n).cascade_hysteria2),
                 (xc.rwarp_tag, xc.remark_cascade_warp, "rwarp", "cascade_warp", lambda n: self.cfg.set_for(n).cascade_warp)]
        for node, (tag_of, remark_of, kind, hide_key, enabled) in [(n, k) for n in nodes for k in kinds]:
            tag = tag_of(node.name)
            cur = by_tag.get(tag)
            wanted = enabled(node) and self.cfg.cascade_for(node).enabled
            if not wanted:
                if cur is not None:
                    self._do(f"мастер: удаляю {tag}", lambda i=cur["id"]: self.panel.delete_inbound(i))
                continue
            if cur is not None:
                if not self._sync_master_domain(cur):
                    self._sync_remark(cur, remark_of(self._label(node)), self._sort(node, kind), hide_key in node.hide)
                continue
            if tls.mode != "domain" or not tls.domain:
                self.log(f"{node.name}: для каскада по Hysteria2 нужен домен главного сервера (кнопка «Домен подписки»)")
                continue
            port = _first_free([443, 8443, 2053, 2083, 9443] + list(range(20443, 60000, 1000)), udp_taken)
            udp_taken.add(port)
            d = f"/root/cert/{tls.domain}"
            payload = xc.build_hysteria_inbound(tag=tag, remark=remark_of(self._label(node)), port=port,
                                                node_id=None, host=tls.domain, cert_file=f"{d}/fullchain.pem",
                                                key_file=f"{d}/privkey.pem", sort_index=self._sort(node, kind),
                                                share_addr=tls.domain)
            payload["excludeFromSub"] = hide_key in node.hide
            self._do(f"мастер: создаю {'цепочку RU → ' + node.name + ' → WARP' if kind == 'rwarp' else 'каскад по Hysteria2'} {tag} ({port}/udp)",
                     lambda p=payload: self.panel.add_inbound(p))
            self._new_rhy2.add(tag)

    def _ensure_cascade_xhttp(self, nodes: List[NodeConfig]) -> None:
        """Protected XHTTP entry on the master for each node (TCP twin of the Hysteria2 cascade).

        Reality "steals" the master's own domain (nginx decoy on 127.0.0.1), the payload is wrapped in
        VLESS Encryption and multiplexed with XMUX; traffic leaves through the node's cascade exit."""
        tls = self.cfg.tls_for(self.cfg.master)
        by_tag = self._inbounds_by_tag()
        tcp_taken = {int(i["port"]) for i in by_tag.values() if not i.get("nodeId") and i["protocol"] != "hysteria"}
        tcp_taken |= {n.cascade.port for n in self.cfg.nodes if n.cascade.port} | self._master_reserved()
        new = getattr(self, "_new_rhy2", set())
        for node in nodes:
            tag = xc.rxh_tag(node.name)
            cur = by_tag.get(tag)
            if not (self.cfg.set_for(node).cascade_xhttp and self.cfg.cascade_for(node).enabled):
                if cur is not None:
                    self._do(f"мастер: каскад по XHTTP для {node.name} выключен, удаляю {tag}",
                             lambda i=cur["id"]: self.panel.delete_inbound(i))
                continue
            remark, sort = xc.remark_cascade_xhttp(self._label(node)), self._sort(node, "rxh")
            hidden = "cascade_xhttp" in node.hide
            if cur is not None:
                if not self._sync_master_domain(cur):
                    self._sync_remark(cur, remark, sort, hidden)
                continue
            if tls.mode != "domain" or not tls.domain:
                self.log(f"{node.name}: для каскада по XHTTP нужен домен главного сервера (кнопка «Домен подписки»)")
                continue
            port = _first_free([2053, 2083, 2087, 2096, 9443, 10443] + list(range(20443, 60000, 1000)), tcp_taken)
            tcp_taken.add(port)
            keys = self._keys()
            payload = xc.build_inbound(tag=tag, remark=remark, port=port, profile=xc.get_profile("vless-reality-xhttp-enc"),
                                       target=f"127.0.0.1:{self.cfg.master.decoy_port}", server_names=self._master_names(),
                                       fingerprint="firefox", private_key=keys["privateKey"], public_key=keys["publicKey"],
                                       exclude_from_sub=hidden, sniffing=False, sort_index=sort, share_addr=tls.domain,
                                       vless_enc=self._vless_enc())
            self._do(f"мастер: создаю каскад по XHTTP + VLESS Encryption {tag} ({port}/tcp → {node.name})",
                     lambda p=payload: self.panel.add_inbound(p))
            new.add(tag)
        self._new_rhy2 = new

    def _sync_master_domain(self, cur: Dict[str, Any]) -> bool:
        """A master entry made for another domain (after a switch to the reserve one): move its link address,
        Reality SNI or Hysteria2 certificate to the current domain. Keys stay, so a subscription refresh is enough."""
        tls = self.cfg.tls_for(self.cfg.master)
        if tls.mode != "domain" or not tls.domain:
            return False
        names = self._master_names()
        reality = ((cur.get("streamSettings") or {}).get("realitySettings") or {})
        if cur.get("shareAddr") == tls.domain and (not reality or list(reality.get("serverNames") or []) == names):
            return False
        d = tls.domain
        updated = xc._clean(cur)
        updated["shareAddrStrategy"], updated["shareAddr"] = "custom", d
        stream = updated.get("streamSettings") or {}
        if stream.get("security") == "reality":
            # Every domain of the master: links on the reserve domain (3x-ui hosts) carry its name as SNI.
            stream["realitySettings"]["serverNames"] = names
        elif stream.get("security") == "tls":
            t = stream.setdefault("tlsSettings", {})
            t["serverName"] = d
            for c in t.get("certificates") or []:
                if c.get("certificateFile"):
                    c["certificateFile"], c["keyFile"] = f"/root/cert/{d}/fullchain.pem", f"/root/cert/{d}/privkey.pem"
        self._do(f"мастер: «{cur.get('remark')}» переходит на домен {d}", lambda: self.panel.update_inbound(cur["id"], updated))
        return True

    def _master_names(self) -> List[str]:
        tls = self.cfg.tls_for(self.cfg.master)
        return [tls.domain] + [d for d in tls.reserve_domains if d != tls.domain] if tls.domain else []

    HOST_TAGS = {"main": "xui-fleet-main", "reserve": "xui-fleet-reserve"}

    def _ensure_master_hosts(self) -> None:
        """Addresses of the entries via the master in subscriptions, through 3x-ui hosts.

        Two host groups cover every such entry: the main domain and the reserve one. Which are on is
        tls.address_mode — so switching to the reserve domain is just turning hosts on, the entries and their
        keys stay as they are, and switching back is as quick."""
        tls = self.cfg.tls_for(self.cfg.master)
        try:
            rows = self.panel.get("/hosts/list") or []
        except PanelError:
            return  # an older 3x-ui without hosts
        ours: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            for kind, t in self.HOST_TAGS.items():
                if t in (r.get("tags") or []):
                    ours.setdefault(kind, []).append(r)
        prefixes = (xc.cascade_tag(""), xc.rxh_tag(""), xc.rhy2_tag(""), xc.rwarp_tag(""))
        ids = sorted(i["id"] for i in self.panel.inbounds() if not i.get("nodeId") and str(i.get("tag", "")).startswith(prefixes))
        reserve = next((d for d in tls.reserve_domains if d != tls.domain), "")
        want: Dict[str, Dict[str, Any]] = {}
        if tls.mode == "domain" and tls.domain and reserve and ids:
            mode = tls.address_mode
            want["main"] = {"address": tls.domain, "remark": " · основной", "disabled": mode != "both"}
            want["reserve"] = {"address": reserve, "remark": " · резерв", "disabled": mode == "main"}
        for kind in self.HOST_TAGS:
            have = ours.get(kind, [])
            gid = have[0]["groupId"] if have else None
            if kind not in want:
                if gid:
                    self._do(f"мастер: убираю хосты «{kind}»", lambda g=gid: self.panel.post(f"/hosts/del/{g}", json_body={}))
                continue
            w = want[kind]
            payload = {"inboundIds": ids, "hosts": [w["address"]], "remark": w["remark"], "isDisabled": w["disabled"],
                       "security": "same", "sni": w["address"], "tags": [self.HOST_TAGS[kind]], "sortOrder": 0 if kind == "main" else 1}
            # the panel lists one object per group (hosts[] + inboundIds[]); older builds listed a row per inbound and address
            have_ids = sorted({int(i) for r in have for i in (r.get("inboundIds") or ([r["inboundId"]] if "inboundId" in r else []))})
            have_addrs = {a for r in have for a in (r.get("hosts") or ([r["address"]] if r.get("address") else []))}
            same = (len({r["groupId"] for r in have}) == 1 and have_ids == ids and have_addrs == {w["address"]}
                    and all(bool(r.get("isDisabled")) == w["disabled"] and r.get("sni") == w["address"] and r.get("remark") == w["remark"] for r in have))
            for extra in sorted({r["groupId"] for r in have} - {gid}):
                self._do(f"мастер: убираю лишнюю копию хостов «{kind}»", lambda g=extra: self.panel.post(f"/hosts/del/{g}", json_body={}))
            if same:
                continue
            state = "выключены" if w["disabled"] else "включены"
            if gid:
                self._do(f"мастер: хосты {w['address']} для подключений через RU — {state}",
                         lambda g=gid, p=dict(payload, groupId=gid): self.panel.post(f"/hosts/update/{g}", json_body=p))
            else:
                self._do(f"мастер: создаю хосты {w['address']} для подключений через RU ({state})",
                         lambda p=payload: self.panel.post("/hosts/add", json_body=p))

    def _ensure_cascade_inbounds(self, nodes: List[NodeConfig]) -> Dict[str, Dict[str, Any]]:
        """One local inbound on the master per node; its traffic is chained to that node."""
        result: Dict[str, Dict[str, Any]] = {}
        by_tag = self._inbounds_by_tag()
        local_ports = {int(i["port"]) for i in by_tag.values() if not i.get("nodeId")}
        reserved = {n.cascade.port for n in self.cfg.nodes if n.cascade.port} | self._master_reserved()
        for node in nodes:
            cascade = self.cfg.cascade_for(node)
            tag = xc.cascade_tag(node.name)
            cur = by_tag.get(tag)
            if not cascade.enabled or not self.cfg.set_for(node).cascade_tcp:
                if cur is not None:
                    self._do(f"мастер: TCP-каскад для {node.name} выключен, удаляю inbound {tag}",
                             lambda i=cur["id"]: self.panel.delete_inbound(i))
                continue
            target, names, fp = cascade.reality.resolved()
            port = cascade.port or (int(cur["port"]) if cur else None)
            if port is None:
                port = _free_port(self.cfg.master.cascade_ports, local_ports | reserved)
            if cur is None and port in self._master_reserved():
                self._warn(f"{node.name}: порт {port} на мастере занят службой (панель, подписка или сайт-заглушка), пропускаю")
                continue
            if cur is None and port in local_ports:
                self._warn(f"{node.name}: порт каскада {port} на мастере уже занят другим inbound, пропускаю")
                continue
            local_ports.add(port)
            want = dict(remark=xc.remark_cascade(self._label(node)), port=port, target=target,
                        server_names=names, exclude_from_sub="cascade" in node.hide,
                        sort_index=self._sort(node, "cascade"),
                        share_addr=self.cfg.tls_for(self.cfg.master).domain or self.cfg.master.host)
            if cur is None:
                keys = self._keys()
                payload = xc.build_inbound(tag=tag, profile=xc.get_profile(cascade.profile or ""), fingerprint=fp,
                                           private_key=keys["privateKey"], public_key=keys["publicKey"],
                                           sniffing=False, **want)
                created = self._do(f"мастер: создаю каскадный inbound {tag} (:{port} → {node.name})",
                                   lambda p=payload: self.panel.add_inbound(p))
                if created:
                    result[node.name] = created
                continue
            update = xc.inbound_drift(cur, **want)
            if update:
                self._do(f"мастер: обновляю каскадный inbound {tag}",
                         lambda u=update, i=cur["id"]: self.panel.update_inbound(i, u))
                cur = update
            result[node.name] = cur
        return result

    # ---- cascade wiring -------------------------------------------------
    def _ensure_bridge(self, nodes: List[NodeConfig], direct: Dict[str, Dict[str, Any]]) -> Optional[str]:
        """Service client the master authenticates with on every cascade upstream."""
        wanted = [direct[n.name]["id"] for n in nodes if self.cfg.cascade_for(n).enabled and n.name in direct]
        live = self._inbounds_by_tag()
        wanted += [live[xc.warpup_tag(n.name)]["id"] for n in nodes if xc.warpup_tag(n.name) in live]
        email = self.cfg.clients.bridge_email
        if not wanted:
            return _client_uuid(self.panel.client(email))
        cur = self.panel.client(email)
        if cur is None:
            self._do(f"мастер: создаю служебного клиента {email} для каскада",
                     lambda: self.panel.add_client(
                         {"email": email, "flow": xc.VISION, "enable": True, "subId": secrets.token_hex(8),
                          "comment": "xui-fleet: мост RU → нода, не удалять"}, wanted))
            cur = None if self.dry else self.panel.client(email)
        else:
            missing = [i for i in wanted if i not in _client_inbounds(cur)]
            if missing:
                self._do(f"мастер: подключаю {email} к новым нодам", lambda: self.panel.attach_clients([email], missing))
        return _client_uuid(cur)

    def _ensure_routes(self, direct: Dict[str, Dict[str, Any]], cascade: Dict[str, Dict[str, Any]],
                       bridge_id: Optional[str]) -> None:
        template = self.panel.xray_template()
        # Start from what is already routed so a partial run (--only) keeps other nodes.
        routes: Dict[str, Dict[str, Any]] = {}
        existing_out = {o.get("tag"): o for o in template.get("outbounds") or []}
        known = {n.name: n for n in self.cfg.nodes if n.enabled}
        # Reading is safe in a dry run too; entries a dry run would create are in _new_rhy2.
        live_by_tag = self._inbounds_by_tag()
        live_tags = set(live_by_tag)
        for name, node in known.items():
            in_tag, o_tag = xc.cascade_tag(name), xc.out_tag(name)
            if not self.cfg.cascade_for(node).enabled:
                continue
            # One exit per node; every RU entry for that node (VLESS, Hysteria2) is routed to it.
            out = None
            if name in direct and bridge_id:
                upstream = direct[name]
                flow = xc.VISION if upstream["streamSettings"].get("network", "tcp") == "tcp" else ""
                out = xc.outbound_for_inbound(
                    upstream, tag=o_tag, address=node.panel_address or node.host, client_id=bridge_id, flow=flow)
            elif o_tag in existing_out:
                out = existing_out[o_tag]
            if out is None:
                continue
            new_hy2 = getattr(self, "_new_rhy2", set())
            for tag in (in_tag, xc.rhy2_tag(name), xc.rxh_tag(name)):
                if name in cascade and tag == in_tag or live_tags is None or tag in live_tags or tag in new_hy2:
                    routes[tag] = out
            # RU -> node -> WARP: its own exit, dialing the node's hidden WARP upstream.
            up = (live_by_tag or {}).get(xc.warpup_tag(name))
            w_out = None
            if up is not None and bridge_id:
                w_out = xc.outbound_for_inbound(up, tag=xc.out_warp_tag(name), address=node.panel_address or node.host,
                                                client_id=bridge_id, flow=xc.VISION)
            elif xc.out_warp_tag(name) in existing_out:
                w_out = existing_out[xc.out_warp_tag(name)]
            rw = xc.rwarp_tag(name)
            if w_out is not None and (live_tags is None or rw in live_tags or rw in new_hy2):
                routes[rw] = w_out
        merged = xc.merge_template(template, routes, self.cfg.routing.ru_direct_on_master, self.cfg.routing.extra_direct)
        if merged != template:
            def apply() -> None:
                self.panel.save_xray_template(merged)
                self.panel.restart_xray()
            ru = ", российские сайты — напрямую с мастера" if self.cfg.routing.ru_direct_on_master and routes else ""
            self._do(f"мастер{ru}: обновляю outbounds и маршруты каскада ({len(routes)} шт.), перезапускаю Xray", apply)

    # ---- clients --------------------------------------------------------
    def _ensure_users(self, direct: Dict[str, Dict[str, Any]], cascade: Dict[str, Dict[str, Any]]) -> None:
        by_tag = self._inbounds_by_tag()
        facing: List[int] = []
        for node in self._servers():
            if not node.enabled:
                continue
            if self._direct(node).enabled and xc.direct_tag(node.name) in by_tag:
                facing.append(by_tag[xc.direct_tag(node.name)]["id"])
            if self.cfg.cascade_for(node).enabled and xc.cascade_tag(node.name) in by_tag:
                facing.append(by_tag[xc.cascade_tag(node.name)]["id"])
            for extra in (xc.xhttp_tag(node.name), xc.hy2_tag(node.name), xc.rhy2_tag(node.name), xc.rxh_tag(node.name),
                          xc.warp_tag(node.name), xc.rwarp_tag(node.name)):
                if extra in by_tag:
                    facing.append(by_tag[extra]["id"])
        if not facing:
            return
        bridge = self.cfg.clients.bridge_email
        clients = {c.get("email"): c for c in map(_client_view, self.panel.clients())}
        for user in self.cfg.users:
            if user.name in clients:
                continue
            payload = {"email": user.name, "flow": xc.VISION, "enable": True, "subId": secrets.token_hex(8),
                       "comment": user.comment, "totalGB": int(user.total_gb * 1024 ** 3), "limitIp": user.limit_ip}
            self._do(f"мастер: создаю пользователя {user.name}",
                     lambda p=payload: self.panel.add_client(p, facing))
            clients[user.name] = {"email": user.name, "inboundIds": list(facing)}
        declared = {u.name for u in self.cfg.users}
        facing_set = set(facing)
        groups: Dict[tuple, List[str]] = {}
        for email, client in clients.items():
            if email == bridge:
                continue
            attached = set(client.get("inboundIds") or [])
            if email not in declared and not (self.cfg.clients.sync_all and attached & facing_set):
                continue
            missing = tuple(sorted(facing_set - attached))
            if missing:
                groups.setdefault(missing, []).append(email)
        for missing, emails in groups.items():
            self._do(f"мастер: подключаю {len(emails)} клиент(ов) к {len(missing)} новым inbound",
                     lambda e=emails, m=list(missing): self.panel.attach_clients(e, m))

    # ---- subscription ---------------------------------------------------
    def _ensure_subscription_settings(self) -> None:
        settings = self.panel.settings()
        changed = []
        if not settings.get("subEnable"):
            settings["subEnable"] = True
            changed.append("включаю сервер подписок")
        if settings.get("webCertFile") and not settings.get("subCertFile"):
            settings["subCertFile"], settings["subKeyFile"] = settings["webCertFile"], settings["webKeyFile"]
            changed.append("включаю HTTPS для подписок (сертификат панели)")
        tmpl = settings.get("remarkTemplate") or ""
        if self.cfg.tls_for(self.cfg.master).address_mode != "main" and tmpl and "{{HOST}}" not in tmpl:
            # Links of the reserve/main hosts must be told apart: put the host's name right after the entry's.
            settings["remarkTemplate"] = tmpl.replace("{{INBOUND}}", "{{INBOUND}}{{HOST}}", 1) if "{{INBOUND}}" in tmpl else tmpl + "{{HOST}}"
            changed.append("в именах подключений появится пометка основного или резервного адреса")
        r = self.cfg.routing
        if r.client_profile:
            link = xc.happ_routing_link(r.extra_direct)
            if not settings.get("subEnableRouting") or settings.get("subRoutingRules") != link:
                settings["subEnableRouting"], settings["subRoutingRules"] = True, link
                changed.append("подписка передаёт Happ профиль «RU напрямую»")
        elif settings.get("subRoutingRules", "").startswith("happ://routing/onadd/") and \
                "RU напрямую" in _b64_name(settings.get("subRoutingRules", "")):
            settings["subEnableRouting"], settings["subRoutingRules"] = False, ""
            changed.append("убираю профиль «RU напрямую» из подписки")
        if not changed:
            return

        def apply() -> None:
            self.panel.update_settings(settings)
            self.panel.restart_panel()
            time.sleep(4)
            self.panel.wait_ready(90)
        self._do("мастер: " + ", ".join(changed) + ", перезапускаю панель", apply)

    def subscription_base(self) -> str:
        s = self.panel.settings()
        if s.get("subURI"):
            return str(s["subURI"])
        scheme = "https" if s.get("subCertFile") else "http"
        host = s.get("subDomain") or self.cfg.tls_for(self.cfg.master).domain or self.cfg.master.host
        return f"{scheme}://{host}:{s.get('subPort', 2096)}{s.get('subPath', '/sub/')}"

    def _collect_subscriptions(self) -> None:
        base = self.subscription_base()
        if base.startswith("http://"):
            self._warn("подписки отдаются по HTTP без шифрования: задайте master.tls (domain или ip)")
        bridge = self.cfg.clients.bridge_email
        for client in map(_client_view, self.panel.clients()):
            if client.get("email") != bridge and client.get("subId"):
                self.report.subscriptions[client["email"]] = base + client["subId"]

    # ---- hygiene --------------------------------------------------------
    def _check_orphans(self) -> None:
        known = {n.name for n in self.cfg.nodes} | ({self.cfg.master.name} if self.cfg.master.vpn else set())
        for tag in self._inbounds_by_tag():
            for prefix in (xc.direct_tag(""), xc.cascade_tag(""), xc.xhttp_tag(""), xc.hy2_tag(""), xc.rhy2_tag(""),
                           xc.warp_tag(""), xc.warpup_tag(""), xc.rwarp_tag("")):
                if tag.startswith(prefix) and tag[len(prefix):] not in known:
                    self._warn(f"на мастере остался {tag}, но ноды нет в конфиге: `xui-fleet node remove {tag[len(prefix):]}`")

    def remove_node(self, name: str) -> None:
        """Tear down everything the fleet created for a node (works by tag)."""
        by_tag = self._inbounds_by_tag()
        template = self.panel.xray_template()
        routes = {r["inboundTag"][0]: o for o in template.get("outbounds") or []
                  for r in (template.get("routing") or {}).get("rules") or []
                  if r.get("outboundTag") == o.get("tag") and str(o.get("tag", "")).startswith(xc.out_tag(""))
                  and r.get("inboundTag")}
        routes.pop(xc.cascade_tag(name), None)
        routes.pop(xc.rhy2_tag(name), None)
        routes.pop(xc.rwarp_tag(name), None)
        routes.pop(xc.rxh_tag(name), None)
        merged = xc.merge_template(template, routes, self.cfg.routing.ru_direct_on_master, self.cfg.routing.extra_direct)
        if merged != template:
            def apply() -> None:
                self.panel.save_xray_template(merged)
                self.panel.restart_xray()
            self._do(f"мастер: убираю outbound и маршрут каскада для {name}", apply)
        tags = (xc.cascade_tag(name), xc.rhy2_tag(name), xc.rwarp_tag(name), xc.rxh_tag(name), xc.xhttp_tag(name), xc.hy2_tag(name),
                xc.warp_tag(name), xc.warpup_tag(name), xc.direct_tag(name))
        for tag in tags:
            if tag in by_tag:
                self._do(f"мастер: удаляю inbound {tag}", lambda i=by_tag[tag]["id"]: self.panel.delete_inbound(i))
        node_ids = [n["id"] for n in _flatten_nodes(self.panel.nodes()) if n.get("name") == name and not n.get("transitive")]
        for node_id in node_ids:
            self._do(f"мастер: удаляю ноду {name}", lambda i=node_id: self.panel.delete_node(i))
        if self.dry:
            return
        # A node still online can report its own inbounds back to the master between the two steps above,
        # and the master imports them again (seen on 3x-ui 3.11). Check again and remove what came back.
        for _ in range(3):
            back = [i for i in self.panel.inbounds() if i.get("tag") in tags or (node_ids and i.get("nodeId") in node_ids)]
            if not back:
                return
            for i in back:
                self._do(f"мастер: удаляю вернувшийся inbound {i.get('tag')}", lambda x=i["id"]: self.panel.delete_inbound(x))
            time.sleep(2)
        self._warn(f"мастер: подключения ноды {name} возвращаются после удаления — удалите их во вкладке «Входящие» панели")


# ---- module helpers -----------------------------------------------------
def _sync_payload(panel: Panel, node_id: int, tags: List[str]) -> Dict[str, Any]:
    """Current node record with a new tag list; apiToken omitted keeps the stored token."""
    cur = next(n for n in _flatten_nodes(panel.nodes()) if n.get("id") == node_id)
    keep = ("name", "remark", "scheme", "address", "port", "basePath", "enable", "allowPrivateAddress",
            "tlsVerifyMode", "pinnedCertSha256", "outboundTag")
    payload = {k: cur.get(k) for k in keep if cur.get(k) is not None}
    payload.update(inboundSyncMode="selected", inboundTags=tags)
    return payload


def _node_payload(node: NodeConfig, acc: NodeAccess, tags: List[str]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "name": node.name,
        "remark": node.label or "",
        "scheme": acc.scheme,
        "address": acc.address,
        "port": acc.port,
        "basePath": acc.base_path,
        "enable": True,
        "allowPrivateAddress": acc.allow_private_address,
        "tlsVerifyMode": acc.tls_verify_mode,
        "pinnedCertSha256": acc.pinned_cert_sha256,
        # Never adopt the node's own inbounds: on a live server they belong to its owner.
        "inboundSyncMode": "selected",
        "inboundTags": sorted(set(tags) | {xc.direct_tag(node.name)}),
    }
    if acc.api_token:
        payload["apiToken"] = acc.api_token
    return payload


def _flatten_nodes(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    flat: List[Dict[str, Any]] = []
    for node in nodes or []:
        flat.append(node)
        flat.extend(_flatten_nodes(node.get("children") or []))
    return flat


def _client_view(entry: Dict[str, Any]) -> Dict[str, Any]:
    """clients/* endpoints wrap the record as {client, inboundIds}; flatten it."""
    if isinstance(entry.get("client"), dict):
        view = dict(entry["client"])
        view["inboundIds"] = entry.get("inboundIds") or []
        return view
    return entry


def _client_uuid(entry: Optional[Dict[str, Any]]) -> Optional[str]:
    return _client_view(entry).get("uuid") if entry else None


def _client_inbounds(entry: Dict[str, Any]) -> List[int]:
    return list(_client_view(entry).get("inboundIds") or [])


def _free_port(port_range: Any, used: Set[Any]) -> int:
    for port in [443] + list(range(port_range[0], port_range[1] + 1)):  # 443 looks like any HTTPS site
        if port not in used:
            return port
    raise PanelError(f"в диапазоне master.cascade_ports {list(port_range)} не осталось свободных портов")


# Best first, from Wi-Fi measurements in Russia (autumn 2026): protected XHTTP survives the TCP "freeze",
# entering via RU works where foreign IPs are filtered, Hysteria2 needs UDP, plain TCP is the most fragile,
# WARP is for sites that block VPN/hosting addresses.
KIND_RANK = {"rxh": 0, "xhttp": 1, "rhy2": 2, "hy2": 3, "cascade": 4, "direct": 5, "rwarp": 6, "warp": 7}


def _b64_name(link: str) -> str:
    import base64
    try:
        return base64.b64decode(link.split("onadd/", 1)[-1] + "==").decode("utf-8", "replace")
    except Exception:
        return ""


def _what_changed(cur: Dict[str, Any], new: Dict[str, Any]) -> str:
    a = (cur.get("streamSettings") or {}).get("realitySettings") or {}
    b = (new.get("streamSettings") or {}).get("realitySettings") or {}
    parts = []
    if a.get("serverNames") != b.get("serverNames") or a.get("target") != b.get("target"):
        parts.append(f"SNI {(b.get('serverNames') or ['?'])[0]}")
    if (a.get("settings") or {}).get("fingerprint") != (b.get("settings") or {}).get("fingerprint"):
        parts.append(f"отпечаток {(b.get('settings') or {}).get('fingerprint')}")
    if cur.get("remark") != new.get("remark"):
        parts.append("название")
    if bool(cur.get("excludeFromSub")) != bool(new.get("excludeFromSub")):
        parts.append("видимость в подписке")
    if int(cur.get("port") or 0) != int(new.get("port") or 0):
        parts.append(f"порт {new.get('port')}")
    return ", ".join(parts) or "порядок в подписке"


def _kind_name(kind: Any) -> str:
    if kind is True:
        return "cascade"
    if kind is False:
        return "direct"
    return kind


def _first_free(candidates: List[int], taken: Set[int]) -> int:
    for port in candidates:
        if port not in taken:
            return port
    raise PanelError("на ноде не нашлось свободного порта")
