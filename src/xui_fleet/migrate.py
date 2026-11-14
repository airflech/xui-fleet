"""Move clients and inbounds from a 3x-ui database backup onto the fleet.

Clients go to the master with the same UUID/subId/limits, so their keys and
subscription URLs keep working. Inbounds that ran on the old server itself are
recreated on a chosen node (same Reality keys); inbounds that lived on the old
server's own nodes (its "Russia to …" relays) are replaced by the fleet cascade.
Every step skips what is already there, so the import can be re-run safely.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from . import xrayconf as xc
from .panel import Panel


@dataclass
class Backup:
    inbounds: List[Dict[str, Any]]
    clients: List[Dict[str, Any]]
    links: Dict[int, List[int]]  # client id -> old inbound ids
    traffic: Dict[str, Dict[str, int]]  # email -> counters
    template: Dict[str, Any]
    settings: Dict[str, str]

    def local_inbounds(self) -> List[Dict[str, Any]]:
        """Inbounds that ran on the old server itself (not on one of its nodes)."""
        return [i for i in self.inbounds if not i.get("node_id")]

    def warp_tags(self) -> List[str]:
        local = {i["tag"] for i in self.local_inbounds()}
        tags: List[str] = []
        for rule in (self.template.get("routing") or {}).get("rules") or []:
            if rule.get("outboundTag") == "warp":
                tags += [t for t in rule.get("inboundTag") or [] if t in local]
        return tags

    def warp_outbound(self) -> Optional[Dict[str, Any]]:
        return next((o for o in self.template.get("outbounds") or [] if o.get("tag") == "warp"), None)


def read_backup(path: Path) -> Backup:
    """Read a 3x-ui SQLite backup. Works on a private copy; the original is never opened for writing."""
    with tempfile.TemporaryDirectory() as tmp:
        copy_path = Path(tmp) / "backup.db"
        shutil.copyfile(path, copy_path)
        con = sqlite3.connect(str(copy_path))
        con.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
            if not {"inbounds", "settings"} <= tables:
                raise ValueError("это не база 3x-ui: нет таблиц inbounds/settings")
            if "clients" not in tables or "client_inbounds" not in tables:
                raise ValueError("бэкап от 3x-ui старше v3 (нет таблицы clients) — такой формат не поддерживается")
            rows = lambda q: [dict(r) for r in con.execute(q)]  # noqa: E731
            inbounds = rows("select * from inbounds order by id")
            for inbound in inbounds:
                for key in ("settings", "stream_settings", "sniffing"):
                    inbound[key] = json.loads(inbound.get(key) or "{}")
            clients = rows("select * from clients order by id")
            links: Dict[int, List[int]] = {}
            for r in rows("select client_id, inbound_id from client_inbounds"):
                links.setdefault(r["client_id"], []).append(r["inbound_id"])
            traffic: Dict[str, Dict[str, int]] = {}
            for r in rows("select * from client_traffics"):
                t = traffic.setdefault(r["email"], {"up": 0, "down": 0, "lastOnline": 0, "resetCount": 0})
                t["up"] += int(r.get("up") or 0)
                t["down"] += int(r.get("down") or 0)
                t["lastOnline"] = max(t["lastOnline"], int(r.get("last_online") or 0))
                t["resetCount"] = max(t["resetCount"], int(r.get("reset_count") or 0))
            settings = {r["key"]: r["value"] for r in rows("select key, value from settings")}
            template = json.loads(settings.get("xrayTemplateConfig") or "{}")
        finally:
            con.close()
    return Backup(inbounds, clients, links, traffic, template, settings)


@dataclass
class NodeFacts:
    node_id: int
    host: str
    busy_tcp: Set[int]
    busy_udp: Set[int]
    cert_pem: str = ""
    key_pem: str = ""

    @property
    def pin_hex(self) -> str:
        """SHA-256 of the DER certificate, the form Hysteria's pinSHA256 expects."""
        import base64
        body = "".join(l for l in self.cert_pem.splitlines() if l and not l.startswith("-----"))
        return hashlib.sha256(base64.b64decode(body)).hexdigest() if body else ""


@dataclass
class ImportResult:
    changes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    port_moves: Dict[str, str] = field(default_factory=dict)


def _client_payload(c: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "email": c["email"], "id": c.get("uuid") or "", "subId": c.get("sub_id") or "",
        "password": c.get("password") or "", "auth": c.get("auth") or "", "flow": c.get("flow") or "",
        "limitIp": int(c.get("limit_ip") or 0), "totalGB": int(c.get("total_gb") or 0),
        "expiryTime": int(c.get("expiry_time") or 0), "enable": bool(c.get("enable")),
        "tgId": int(c.get("tg_id") or 0), "group": c.get("group_name") or "", "comment": c.get("comment") or "",
        "reset": int(c.get("reset") or 0), "resetDay": int(c.get("reset_day") or 0),
        "resetWeekday": int(c.get("reset_weekday") or 0), "resetMax": int(c.get("reset_max") or 0),
        "trafficReset": c.get("traffic_reset") or "never", "trafficResetDay": int(c.get("traffic_reset_day") or 1),
        "limitHwid": int(c.get("limit_hwid") or 0),
    }
    if c.get("secret"):
        payload["secret"] = c["secret"]
    return {k: v for k, v in payload.items() if v != "" or k in ("flow", "comment", "group")}


class Importer:
    def __init__(self, panel: Panel, backup: Backup, node_name: str, facts: NodeFacts,
                 label: Optional[str], dry_run: bool = False, log: Callable[[str], None] = print):
        self.panel, self.b, self.node_name, self.facts = panel, backup, node_name, facts
        self.label, self.dry, self.log = label, dry_run, log
        self.result = ImportResult()

    def _do(self, message: str, fn: Callable[[], Any]) -> Any:
        self.result.changes.append(message)
        self.log(("[план] " if self.dry else "") + message)
        return None if self.dry else fn()

    # ---- inbounds -------------------------------------------------------
    def _rename(self, remark: str) -> str:
        """Old remarks name the old country; swap the flag+country for the node label when one is set."""
        if not self.label:
            return remark
        return re.sub(r"^\S+\s+[A-Za-zА-Яа-яЁё]+", self.label, remark, count=1) if re.match(r"^\S+\s", remark) else remark

    def _port(self, old: Dict[str, Any], taken_tcp: Set[int], taken_udp: Set[int]) -> int:
        udp = old["protocol"] in ("hysteria", "tuic", "wireguard", "amneziawg")
        taken = taken_udp if udp else taken_tcp
        port = int(old["port"])
        if port not in taken:
            return port
        for candidate in [8443, 2053, 2083, 2087, 9443, 10443] + list(range(20443, 60000, 1000)):
            if candidate not in taken:
                return candidate
        raise RuntimeError("не нашёл свободного порта на ноде")

    def _inbound_payload(self, old: Dict[str, Any], port: int) -> Dict[str, Any]:
        settings = copy.deepcopy(old["settings"])
        settings["clients"] = []
        stream = copy.deepcopy(old["stream_settings"])
        if stream.get("security") == "tls":
            # The old cert belongs to the old server's domain; use the node's own cert and pin it in links.
            tls = stream.setdefault("tlsSettings", {})
            tls["certificates"] = [{"certificate": self.facts.cert_pem.splitlines(), "key": self.facts.key_pem.splitlines(),
                                    "ocspStapling": 3600, "oneTimeLoading": False, "usage": "encipherment",
                                    "buildChain": False}]
            tls["serverName"] = self.facts.host
            tls.setdefault("settings", {})["pinnedPeerCertSha256"] = [self.facts.pin_hex]
        return {
            "enable": bool(old.get("enable")), "remark": self._rename(old.get("remark") or old["tag"]),
            "listen": "", "port": port, "protocol": old["protocol"], "tag": old["tag"],
            "expiryTime": int(old.get("expiry_time") or 0), "total": int(old.get("total") or 0),
            "trafficReset": old.get("traffic_reset") or "never", "trafficResetDay": int(old.get("traffic_reset_day") or 1),
            "subSortIndex": int(old.get("sub_sort_index") or 1), "excludeFromSub": bool(old.get("exclude_from_sub")),
            "settings": settings, "streamSettings": stream, "sniffing": old.get("sniffing") or {},
            "nodeId": self.facts.node_id, "shareAddrStrategy": "node",
        }

    def ensure_inbounds(self) -> Dict[int, int]:
        """Old inbound id -> new master inbound id."""
        mapping: Dict[int, int] = {}
        live = {i["tag"]: i for i in self.panel.inbounds()}
        on_node = [i for i in live.values() if i.get("nodeId") == self.facts.node_id]
        taken_tcp = set(self.facts.busy_tcp) | {int(i["port"]) for i in on_node if i["protocol"] not in ("hysteria",)}
        taken_udp = set(self.facts.busy_udp) | {int(i["port"]) for i in on_node if i["protocol"] in ("hysteria",)}
        for old in self.b.local_inbounds():
            cur = live.get(old["tag"])
            if cur is not None:
                if cur.get("nodeId") != self.facts.node_id:
                    self.result.warnings.append(f"{old['tag']}: на мастере уже есть inbound с этим тегом на другом сервере — пропускаю")
                    continue
                mapping[old["id"]] = cur["id"]
                continue
            port = self._port(old, taken_tcp, taken_udp)
            (taken_udp if old["protocol"] == "hysteria" else taken_tcp).add(port)
            if port != int(old["port"]):
                self.result.port_moves[old["tag"]] = f"{old['port']} → {port}"
            payload = self._inbound_payload(old, port)
            note = f", порт {old['port']} занят — будет {port}" if port != int(old["port"]) else ""
            created = self._do(f"{self.node_name}: создаю «{payload['remark']}» ({old['protocol']}:{port}{note})",
                               lambda p=payload: self.panel.add_inbound(p))
            if created:
                mapping[old["id"]] = created["id"]
        return mapping

    # ---- clients --------------------------------------------------------
    def ensure_clients(self, mapping: Dict[int, int], cascade_ids: List[int]) -> None:
        relayed = {i["id"] for i in self.b.inbounds if i.get("node_id")}
        existing = {c["email"]: c for c in self.panel.clients()}
        items, attach = [], {}
        for c in self.b.clients:
            targets: List[int] = []
            for old_id in self.b.links.get(c["id"], []):
                if old_id in mapping:
                    targets.append(mapping[old_id])
                elif old_id in relayed:
                    targets += cascade_ids  # the old "Russia to …" relay is now the fleet cascade
            targets = sorted(set(targets))
            if not targets and not self.dry:
                self.result.warnings.append(f"{c['email']}: не был ни в одном переносимом подключении — пропускаю")
                continue
            cur = existing.get(c["email"])
            if cur is None:
                item: Dict[str, Any] = {"client": _client_payload(c), "inboundIds": targets}
                if c["email"] in self.b.traffic:
                    item["traffic"] = self.b.traffic[c["email"]]
                items.append(item)
            else:
                missing = [i for i in targets if i not in (cur.get("inboundIds") or [])]
                if missing:
                    attach[c["email"]] = missing
        if items:
            self._do(f"мастер: переношу {len(items)} клиент(ов) с теми же ключами, subId, лимитами и трафиком",
                     lambda: self.panel.post("/clients/import", json_body={"data": json.dumps(items, ensure_ascii=False)}))
        for email, ids in attach.items():
            self._do(f"мастер: подключаю {email} к {len(ids)} подключениям",
                     lambda e=email, i=ids: self.panel.attach_clients([e], i))

    # ---- subscription ---------------------------------------------------
    def ensure_sub_path(self) -> None:
        """Same path as the old panel, so old subscription URLs work once the domain points here."""
        old = self.b.settings.get("subPath")
        if not old:
            return
        settings = self.panel.settings()
        current = settings.get("subPath")
        if current == old:
            return
        settings["subPath"] = old

        def apply() -> None:
            self.panel.update_settings(settings)
            self.panel.restart_panel()
            import time
            time.sleep(4)
            self.panel.wait_ready(90)
        self._do(f"мастер: путь подписки {current} → {old}, как на старом сервере", apply)


WARP_ENDPOINT_V4 = "162.159.192.1"  # engage.cloudflareclient.com


def merge_warp(template: Dict[str, Any], warp: Dict[str, Any], tags: List[str], endpoint: str = "") -> Dict[str, Any]:
    """Put the old WARP outbound and its routing rule into a node's Xray template (idempotent)."""
    tpl = copy.deepcopy(template)
    outs = [o for o in tpl.get("outbounds") or [] if o.get("tag") != "warp"]
    warp = copy.deepcopy(warp)
    # The new host may lack IPv6 (the endpoint name resolves to v6 first) or behave
    # differently with a kernel TUN; IPv4 + userspace works on any VPS.
    warp.setdefault("settings", {})["domainStrategy"] = "ForceIPv4"
    warp["settings"]["noKernelTun"] = True
    # Xray may still resolve the endpoint name to IPv6 first and then fail with "network is unreachable" on a
    # server without IPv6; Cloudflare's anycast IPv4 address of the same endpoint avoids name resolution.
    for peer in warp["settings"].get("peers") or []:
        host, _, port = str(peer.get("endpoint") or "").rpartition(":")
        if endpoint:  # measured on this server: Cloudflare entry points differ a lot from one network to another
            peer["endpoint"] = endpoint
        elif host and not host.replace(".", "").isdigit():
            peer["endpoint"] = f"{WARP_ENDPOINT_V4}:{port or 2408}"
    tpl["outbounds"] = outs + [warp]
    routing = tpl.setdefault("routing", {})
    rules = [r for r in routing.get("rules") or [] if not (r.get("outboundTag") == "warp" and r.get("inboundTag"))]
    head = next((i + 1 for i, r in enumerate(rules) if r.get("outboundTag") == "api"), 0)
    rules.insert(head, {"type": "field", "inboundTag": sorted(tags), "outboundTag": "warp"})
    routing["rules"] = rules
    return tpl


def new_wireguard_keys() -> Dict[str, str]:
    import base64
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
    key = X25519PrivateKey.generate()
    raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return {"privateKey": base64.b64encode(raw).decode(), "publicKey": base64.b64encode(pub).decode()}


def warp_from_registration(old: Dict[str, Any], registration: Dict[str, Any]) -> Dict[str, Any]:
    """The old WARP outbound with this server's own WARP identity (keys, address, reserved).

    Two servers sharing one WARP device fight over the session, so each needs its own.
    Mirrors the panel's WarpModal.buildWarpOutbound.
    """
    import base64
    data, cfg = registration.get("data") or {}, registration.get("config") or {}
    cfg = cfg.get("config", cfg)  # the raw Cloudflare reply nests it under "config"
    peer = (cfg.get("peers") or [{}])[0]
    addrs = (cfg.get("interface") or {}).get("addresses") or {}
    client_id = cfg.get("client_id") or data.get("client_id") or ""
    out = copy.deepcopy(old)
    s = out.setdefault("settings", {})
    s["secretKey"] = data.get("private_key")
    s["address"] = [a for a in (f"{addrs['v4']}/32" if addrs.get("v4") else "", f"{addrs['v6']}/128" if addrs.get("v6") else "") if a]
    s["reserved"] = list(base64.b64decode(client_id)) if client_id else []
    peers = copy.deepcopy(s.get("peers") or [{}])
    peers[0]["publicKey"] = peer.get("public_key", peers[0].get("publicKey"))
    peers[0]["endpoint"] = (peer.get("endpoint") or {}).get("host") or peers[0].get("endpoint")
    s["peers"] = peers
    return out
