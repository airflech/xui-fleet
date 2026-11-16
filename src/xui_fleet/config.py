"""Desired state of the fleet: one YAML file, validated with pydantic."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SSHConfig(Strict):
    user: Optional[str] = None
    port: Optional[int] = None
    key: Optional[str] = None  # path to a private key
    key_passphrase_env: Optional[str] = None
    password_env: Optional[str] = None  # name of the env var holding the password
    password: Optional[str] = None  # inline password (discouraged)

    def merged(self, base: "SSHConfig") -> "SSHConfig":
        data = base.model_dump()
        data.update({k: v for k, v in self.model_dump().items() if v is not None})
        return SSHConfig(**data)

    def resolve_password(self) -> Optional[str]:
        if self.password_env:
            value = os.environ.get(self.password_env)
            if not value:
                raise ValueError(f"переменная окружения {self.password_env} с SSH-паролем не задана")
            return value
        return self.password


class TLSConfig(Strict):
    # selfsigned: own cert, the master pins its SHA-256 (best for nodes)
    # ip / domain: Let's Encrypt via the 3x-ui installer (needs port 80)
    # none: plain HTTP (only for testing)
    mode: str = "selfsigned"
    domain: Optional[str] = None
    # Spare domains on the same master, in the same certificate: subscriptions open on them too, and if the
    # main domain is blocked the master switches to one of them (users keep a ready reserve link).
    reserve_domains: List[str] = Field(default_factory=list)
    # Which address the entries via the master carry in subscriptions (3x-ui "hosts"): main — the main domain,
    # both — two links per entry (main + reserve), reserve — only the reserve domain (main one blocked).
    address_mode: str = "main"
    email: Optional[str] = None

    @field_validator("address_mode")
    @classmethod
    def _am(cls, v: str) -> str:
        if v not in ("main", "both", "reserve"):
            raise ValueError("tls.address_mode: main | both | reserve")
        return v

    @field_validator("mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v not in ("selfsigned", "ip", "domain", "none"):
            raise ValueError("tls.mode: selfsigned | ip | domain | none")
        return v

    @model_validator(mode="after")
    def _domain(self) -> "TLSConfig":
        if self.mode == "domain" and not self.domain:
            raise ValueError("tls.mode=domain требует tls.domain")
        return self


class RealityConfig(Strict):
    target: Optional[str] = None  # host:port the handshake is borrowed from
    server_names: Optional[List[str]] = None
    fingerprint: Optional[str] = None

    def merged(self, base: "RealityConfig") -> "RealityConfig":
        data = base.model_dump()
        data.update({k: v for k, v in self.model_dump().items() if v is not None})
        return RealityConfig(**data)

    def resolved(self) -> Tuple[str, List[str], str]:
        target = self.target or "www.microsoft.com:443"
        if ":" not in target:
            target += ":443"
        names = self.server_names or [target.rsplit(":", 1)[0]]
        # firefox: in Russia (autumn 2026) the chrome fingerprint gets VLESS frozen noticeably more often.
        return target, names, self.fingerprint or "firefox"


class DirectConfig(Strict):
    """Inbound on the foreign node itself: client -> node.

    enabled=false hides it from users; it still exists as the cascade upstream.
    """

    enabled: Optional[bool] = None
    port: Optional[int] = None
    profile: Optional[str] = None
    reality: RealityConfig = RealityConfig()


class CascadeConfig(Strict):
    """Inbound on the RU master chained to the node: client -> RU -> node."""

    enabled: Optional[bool] = None
    port: Optional[int] = None  # port on the master; auto-allocated when empty
    profile: Optional[str] = None
    reality: RealityConfig = RealityConfig()


HIDEABLE = {"direct", "xhttp", "hy2", "cascade", "cascade_xhttp", "cascade_hy2", "warp", "cascade_warp"}


class SetConfig(Strict):
    """Which extra connections every node gets besides direct TCP and the cascade."""

    xhttp: bool = True  # VLESS Reality over XHTTP: survives networks that freeze long TCP sessions
    # XHTTP entries carry VLESS Encryption (ML-KEM-768 + X25519) + Vision + XMUX: the TLS payload no longer
    # looks like TLS-in-TLS, which is what the freezing heuristics key on. Tested from a Russian home
    # network on a frozen hosting IP: TCP Reality stalls after ~20 KB, this runs at 70–130 Mbit/s.
    xhttp_encryption: bool = True
    # The same protected XHTTP as an entry on the RU master (self-steal of its own domain), routed to the
    # node: the TCP alternative to the Hysteria2 cascade for networks that block or throttle UDP.
    cascade_xhttp: bool = True
    hysteria2: bool = True  # QUIC/UDP backup; some networks throttle UDP, others are faster with it
    # Hysteria2 entry on the RU master too: TSPU freezes VPN-over-TCP to some hosting ranges, UDP passes.
    cascade_hysteria2: bool = True
    # VLESS/TCP entry on the master; off keeps only the Hysteria2 entry (same exit to the node).
    cascade_tcp: bool = True
    # Exit through Cloudflare WARP: directly to the node, and RU -> node -> WARP (Hysteria2 entry on the master).
    warp: bool = True
    cascade_warp: bool = True
    warp_transport: str = "hy2"  # hy2 | xhttp — how the user reaches the node's WARP entry

    @field_validator("warp_transport")
    @classmethod
    def _wt(cls, v: str) -> str:
        if v not in ("hy2", "xhttp"):
            raise ValueError("warp_transport: hy2 | xhttp")
        return v


class HardeningConfig(Strict):
    bbr: bool = True
    firewall: bool = False  # ufw: ssh + proxy ports open, node panel only for the master
    # An ordinary company website on every server (fictional brand per domain/IP): a domain or IP that
    # opens nothing looks odd. Master: 80 + 443 (and the Reality target); nodes: 80 when it is free.
    cover_site: bool = True


class Defaults(Strict):
    ssh: SSHConfig = SSHConfig(user="root", port=22)
    direct: DirectConfig = DirectConfig(enabled=True, port=443, profile="vless-reality-tcp")
    cascade: CascadeConfig = CascadeConfig(enabled=True, profile="vless-reality-tcp")
    hardening: HardeningConfig = HardeningConfig()
    set: SetConfig = SetConfig()


class ServerBase(Strict):
    name: str
    host: str  # IP or DNS name used for SSH and in share links
    ssh: SSHConfig = SSHConfig()
    tls: Optional[TLSConfig] = None
    panel_port: Optional[int] = None  # only used for a fresh install
    hardening: Optional[HardeningConfig] = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("имя: латиница в нижнем регистре, цифры и дефис (до 31 символа)")
        return v


class MasterConfig(ServerBase):
    cascade_ports: Tuple[int, int] = (8443, 8543)
    # Local HTTPS site (nginx, h2, the domain's real cert) that Reality on the master impersonates.
    decoy_port: int = 8444
    label: str = "RU"
    flag: Optional[str] = None  # like a node's: None/"auto" = by IP, "none", or an ISO code
    # Roles. vpn: the master is a VPN server itself — users get its own connection set (the template),
    # traffic leaves from the master. cascades: entries on the master that lead to the foreign nodes.
    # A single-server setup is vpn: true with no nodes.
    vpn: bool = False
    cascades: bool = True
    hide: List[str] = Field(default_factory=list)  # own entries left out of subscriptions (like a node's)
    # Its own connection set when vpn (like a node's): only the fields stated here override defaults.set.
    set: Optional[SetConfig] = None
    direct: DirectConfig = DirectConfig()
    # Address the foreign nodes are reached at from the master (defaults to node.host).
    api_url: Optional[str] = None  # direct panel URL instead of the SSH tunnel (tests)

    @field_validator("cascade_ports")
    @classmethod
    def _range(cls, v: Tuple[int, int]) -> Tuple[int, int]:
        if not (0 < v[0] <= v[1] < 65536):
            raise ValueError("cascade_ports: [начало, конец]")
        return v


class NodeConfig(ServerBase):
    label: Optional[str] = None  # shown in client apps, e.g. "Germany"
    # Country flag in front of the label: None/"auto" = by server IP, "none", or an ISO code like "DE".
    flag: Optional[str] = None
    # Entries kept working but left out of subscriptions: direct, xhttp, hy2, cascade, cascade_hy2.
    hide: List[str] = Field(default_factory=list)

    @field_validator("hide")
    @classmethod
    def _hide(cls, v: List[str]) -> List[str]:
        bad = set(v) - HIDEABLE
        if bad:
            raise ValueError(f"hide: неизвестные подключения {sorted(bad)}; доступны {sorted(HIDEABLE)}")
        return v
    direct: DirectConfig = DirectConfig()
    cascade: CascadeConfig = CascadeConfig()
    enabled: bool = True
    set: Optional[SetConfig] = None
    # Overrides for how the master reaches the node panel (tests / NAT).
    panel_address: Optional[str] = None
    allow_private_address: bool = False


class UserConfig(Strict):
    name: str  # becomes the client e-mail in 3x-ui
    comment: str = ""
    total_gb: float = 0  # 0 = unlimited
    limit_ip: int = 0

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not re.match(r"^[\w.@-]{1,64}$", v):  # \w is Unicode: «Мама-phone» is a fine name
            raise ValueError("имя пользователя: буквы (любые), цифры, . _ @ -, без пробелов")
        return v


class ClientsConfig(Strict):
    # Attach every client that already lives on a fleet inbound to all fleet
    # inbounds, so users created in the panel UI get new nodes automatically.
    sync_all: bool = True
    bridge_email: str = "fleet-bridge"
    # Service user whose subscription the link check uses, so test traffic from the master never lands in a
    # real user's devices, IPs and "last connected".
    check_email: str = "fleet-check"
    # Apps allowed to fetch subscriptions (by User-Agent, through nginx on the master); empty — any app.
    # Other apps get a placeholder subscription that says the app is not supported, browsers a page.
    allowed_apps: List[str] = Field(default_factory=list)
    # The VPN's name in the apps (subscription title, 3x-ui subTitle → Profile-Title). Empty: the apps show
    # whatever they pick themselves (usually the address or the subscription id).
    vpn_name: str = ""

    @field_validator("vpn_name")
    @classmethod
    def _vpn_name(cls, v: str) -> str:
        v = " ".join(str(v or "").split())
        if len(v) > 40:
            raise ValueError("clients.vpn_name: не длиннее 40 символов")
        return v

    @field_validator("allowed_apps")
    @classmethod
    def _apps(cls, v: List[str]) -> List[str]:
        v = [a.strip().lower() for a in v if a.strip()]
        bad = [a for a in v if a not in APPS]
        if bad:
            raise ValueError(f"clients.allowed_apps: {', '.join(bad)} — допустимо {', '.join(APPS)}")
        return list(dict.fromkeys(v))

    @property
    def service_emails(self) -> Set[str]:
        return {self.bridge_email, self.check_email}


# Apps that can be the only ones allowed to fetch subscriptions: name → how its User-Agent is recognised
# (the same patterns 3x-ui itself uses to detect them).
APPS = {"happ": r"\bhapp\b", "incy": r"\bincy\b"}
APP_TITLES = {"happ": "Happ", "incy": "INCY"}


def set_vpn_name(path: Path, name: str) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    clients = doc.get("clients") or {}
    name = " ".join(str(name or "").split())
    if name:
        clients["vpn_name"] = name
    else:
        clients.pop("vpn_name", None)
    if clients:
        doc["clients"] = clients
    else:
        doc.pop("clients", None)
    FleetConfig.model_validate(doc)
    _save_doc(path, doc)


def set_allowed_apps(path: Path, apps: List[str]) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    clients = doc.get("clients") or {}
    if apps:
        clients["allowed_apps"] = list(apps)
    else:
        clients.pop("allowed_apps", None)
    if clients:
        doc["clients"] = clients
    else:
        doc.pop("clients", None)
    FleetConfig.model_validate(doc)
    _save_doc(path, doc)


class RoutingConfig(Strict):
    """Russian traffic never needs the VPN: banks, Gosuslugi, marketplaces work better (or only) from a
    Russian address, and it saves the foreign servers' bandwidth."""

    # Entries on the RU master: Russian sites leave from the master itself instead of going abroad.
    ru_direct_on_master: bool = True
    # Subscription pushes a routing profile to Happ (and INCY): Russian sites bypass the VPN on the device.
    client_profile: bool = True
    # Extra destinations to send directly from both, e.g. "domain:example.com", "geosite:apple", "geoip:by".
    extra_direct: List[str] = Field(default_factory=list)


class PanelConfig(Strict):
    version: str = "v3.11.0"  # pinned 3x-ui release, identical on every server

    @field_validator("version")
    @classmethod
    def _v(cls, v: str) -> str:
        if not re.match(r"^v\d+\.\d+\.\d+$", v):
            raise ValueError("panel.version в формате v3.11.0")
        return v


class FleetConfig(Strict):
    version: int = 1
    panel: PanelConfig = PanelConfig()
    defaults: Defaults = Defaults()
    master: MasterConfig
    nodes: List[NodeConfig] = Field(default_factory=list)
    users: List[UserConfig] = Field(default_factory=list)
    clients: ClientsConfig = ClientsConfig()
    routing: RoutingConfig = RoutingConfig()

    @field_validator("nodes", "users", mode="before")
    @classmethod
    def _empty_list(cls, v):
        return [] if v is None else v  # a bare `nodes:` key in YAML

    @model_validator(mode="after")
    def _unique(self) -> "FleetConfig":
        names = [self.master.name] + [n.name for n in self.nodes]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ValueError(f"повторяющиеся имена серверов: {sorted(dup)}")
        ports: Dict[int, str] = {}
        for n in self.nodes:
            p = n.cascade.port
            if p:
                if p in ports:
                    raise ValueError(f"cascade.port {p} задан и у {ports[p]}, и у {n.name}")
                ports[p] = n.name
        return self

    # ---- resolved views -------------------------------------------------
    def ssh_for(self, server: ServerBase) -> SSHConfig:
        return server.ssh.merged(self.defaults.ssh)

    def hardening_for(self, server: ServerBase) -> HardeningConfig:
        return server.hardening or self.defaults.hardening

    def tls_for(self, server: ServerBase) -> TLSConfig:
        if server.tls:
            return server.tls
        # The master serves subscriptions to client apps, so it needs a publicly
        # trusted cert; nodes only talk to the master, which pins their cert.
        return TLSConfig(mode="ip" if server is self.master else "selfsigned")

    def direct_for(self, node: NodeConfig, neighbor: Optional[Tuple[str, str]] = None) -> DirectConfig:
        """`neighbor` — (host:port, server name) of a real site in the node's subnet, used when the
        config does not name a Reality target itself."""
        d, b = node.direct, self.defaults.direct
        reality = d.reality.merged(b.reality)
        if neighbor and not reality.target and not reality.server_names:
            reality = RealityConfig(target=neighbor[0], server_names=[neighbor[1]], fingerprint=reality.fingerprint)
        return DirectConfig(
            enabled=b.enabled if d.enabled is None else d.enabled,
            port=d.port or b.port or 443,
            profile=d.profile or b.profile or "vless-reality-tcp",
            reality=reality,
        )

    def cascade_for(self, node: NodeConfig) -> CascadeConfig:
        if not self.master.cascades:
            return CascadeConfig(enabled=False, profile=self.defaults.cascade.profile)
        c, b = node.cascade, self.defaults.cascade
        reality = c.reality.merged(b.reality)
        if not reality.target and not reality.server_names:
            domain = self.tls_for(self.master).domain if self.tls_for(self.master).mode == "domain" else None
            if domain:
                # "Steal from yourself": the SNI is our own domain, which really resolves to this IP and
                # serves a real h2 site with a valid cert, so SNI/IP checks and active probes see nothing odd.
                reality = RealityConfig(target=f"127.0.0.1:{self.master.decoy_port}",
                                        server_names=[domain] + [d for d in self.tls_for(self.master).reserve_domains if d != domain],
                                        fingerprint=reality.fingerprint)
            else:
                reality = RealityConfig(target="ya.ru:443", server_names=["ya.ru"], fingerprint=reality.fingerprint)
        return CascadeConfig(
            enabled=b.enabled if c.enabled is None else c.enabled,
            port=c.port,
            profile=c.profile or b.profile or "vless-reality-tcp",
            reality=reality,
        )

    def local_node(self) -> NodeConfig:
        """The master as a server with its own connection set (master.vpn). Same name as the master."""
        m = self.master
        tls = self.tls_for(m)
        return NodeConfig(name=m.name, host=(tls.domain if tls.mode == "domain" and tls.domain else m.host),
                          label=m.label, flag=m.flag, hide=list(m.hide), ssh=m.ssh, set=m.set, direct=m.direct)

    def set_for(self, node: NodeConfig) -> SetConfig:
        """The connection set every node gets (`defaults.set` — the template), with only the fields a node
        states itself overridden. A new server therefore gets exactly what the others have."""
        if node.set is None:
            return self.defaults.set
        return self.defaults.set.model_copy(update={k: getattr(node.set, k) for k in node.set.model_fields_set})

    def node(self, name: str) -> NodeConfig:
        for n in self.nodes:
            if n.name == name:
                return n
        raise KeyError(f"нода {name!r} не найдена в конфиге")


def load_config(path: Path) -> FleetConfig:
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return FleetConfig.model_validate(raw)


def _save_doc(path: Path, doc: dict) -> None:
    FleetConfig.model_validate(doc)  # never write a config we cannot read back
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


def append_node(path: Path, node: dict) -> None:
    """Add a node entry. Works on the parsed document, so any YAML layout of the file is fine."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    nodes = doc.get("nodes") or []
    if any(n.get("name") == node.get("name") for n in nodes):
        raise ValueError(f"сервер с именем {node.get('name')} уже есть")
    doc["nodes"] = nodes + [node]
    _save_doc(path, doc)


def remove_node_entry(path: Path, name: str) -> bool:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    nodes = doc.get("nodes") or []
    kept = [n for n in nodes if n.get("name") != name]
    if len(kept) == len(nodes):
        return False
    doc["nodes"] = kept
    _save_doc(path, doc)
    return True


# What turning off one kind of xui-fleet connection means in fleet.yaml (see inbound_kind).
CONNECTION_SWITCH = {"xhttp": "xhttp", "hy2": "hysteria2", "warp": "warp", "rhy2": "cascade_hysteria2",
                     "rxh": "cascade_xhttp", "rwarp": "cascade_warp", "cascade": "cascade_tcp"}


def disable_connection(path: Path, server: str, kind: str) -> None:
    """Turn one kind of connection off for one server, so `apply` removes it and never brings it back.
    The direct entry is only hidden: the cascades of that server lead through it."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    master = doc.get("master") or {}
    entry = master if master.get("name") == server else next(
        (n for n in doc.get("nodes") or [] if (n or {}).get("name") == server), None)
    if entry is None:
        raise KeyError(f"сервер {server!r} не найден в конфиге")
    if kind == "direct":
        entry.setdefault("direct", {})["enabled"] = False
    elif kind in CONNECTION_SWITCH:
        entry.setdefault("set", {})[CONNECTION_SWITCH[kind]] = False
    else:
        raise ValueError("это служебное подключение — его нельзя удалить отдельно")
    FleetConfig.model_validate(doc)
    _save_doc(path, doc)


def set_direct_port(path: Path, server: str, port: int) -> bool:
    """A port written in fleet.yaml wins over the live one: keep it in step when the port is changed by hand."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entry = next((n for n in [doc.get("master") or {}] + list(doc.get("nodes") or []) if (n or {}).get("name") == server), None)
    if not entry or not (entry.get("direct") or {}).get("port"):
        return False
    entry["direct"]["port"] = int(port)
    _save_doc(path, doc)
    return True


def remove_user_entry(path: Path, name: str) -> bool:
    """Drop a user from fleet.yaml, so `apply` does not create it again."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    users = doc.get("users") or []
    kept = [u for u in users if (u or {}).get("name") != name]
    if len(kept) == len(users):
        return False
    doc["users"] = kept
    _save_doc(path, doc)
    return True


def update_user_entry(path: Path, name: str, comment: str, total_gb: float) -> bool:
    """Keep a user listed in fleet.yaml in step with the panel (it seeds the user if the panel loses it)."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entry = next((u for u in doc.get("users") or [] if (u or {}).get("name") == name), None)
    if entry is None:
        return False
    for key, value, empty in (("comment", comment, ""), ("total_gb", total_gb, 0)):
        if value == empty:
            entry.pop(key, None)
        else:
            entry[key] = value
    FleetConfig.model_validate(doc)
    _save_doc(path, doc)
    return True


def update_server_access(path: Path, name: str, host: str, ssh: dict) -> None:
    """Replace host and SSH settings of the master or a node (used by the web UI)."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    servers = [doc.get("master") or {}] + list(doc.get("nodes") or [])
    entry = next((s for s in servers if s.get("name") == name), None)
    if entry is None:
        raise KeyError(f"сервер {name!r} не найден в конфиге")
    entry["host"] = host
    if ssh:
        entry["ssh"] = ssh
    else:
        entry.pop("ssh", None)
    FleetConfig.model_validate(doc)
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")


def set_master_domain(path: Path, domain: str, reserves: Optional[List[str]] = None) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tls = (doc.setdefault("master", {}).get("tls") or {})
    old = tls.get("domain")
    tls.update(mode="domain", domain=domain)
    if reserves is not None:
        tls["reserve_domains"] = [d for d in dict.fromkeys(reserves) if d != domain]
        if not tls["reserve_domains"]:
            tls.pop("reserve_domains")
    # "Only the reserve domain" means the main one is blocked. Once the reserve became the main domain (the old,
    # blocked one is now the reserve) or no reserve is left, keeping that mode would send every link to the
    # blocked domain or to none: links go back to the main domain. "Both" without a reserve is just "main".
    mode = tls.get("address_mode")
    if (mode == "reserve" and old != domain) or (mode in ("reserve", "both") and not tls.get("reserve_domains")):
        tls.pop("address_mode")
    doc["master"]["tls"] = tls
    FleetConfig.model_validate(doc)
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")


def set_master_role(path: Path, vpn: bool, cascades: bool, label: Optional[str] = None,
                    flag: Optional[str] = None, hide: Optional[List[str]] = None) -> None:
    """Roles of the master (its own VPN entries / cascades to the nodes) and how its entries are named."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    m = doc.setdefault("master", {})
    m["vpn"], m["cascades"] = bool(vpn), bool(cascades)
    if label is not None:
        if label.strip():
            m["label"] = label.strip()
        else:
            m.pop("label", None)
    if flag is not None:
        if flag and flag != "auto":
            m["flag"] = flag
        else:
            m.pop("flag", None)
    if hide is not None:
        if hide:
            m["hide"] = hide
        else:
            m.pop("hide", None)
    FleetConfig.model_validate(doc)
    _save_doc(path, doc)


def set_address_mode(path: Path, mode: str) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tls = (doc.setdefault("master", {}).get("tls") or {})
    if mode == "main":
        tls.pop("address_mode", None)
    else:
        tls["address_mode"] = mode
    doc["master"]["tls"] = tls
    FleetConfig.model_validate(doc)
    _save_doc(path, doc)


def set_node_label(path: Path, name: str, label: Optional[str], flag: Optional[str],
                   hide: Optional[List[str]] = None) -> None:
    """Name and flag shown in client apps; empty values fall back to the detected country."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    node = next((n for n in doc.get("nodes") or [] if n.get("name") == name), None)
    if node is None:
        raise KeyError(f"нода {name!r} не найдена в конфиге")
    for key, value in (("label", (label or "").strip()), ("flag", (flag or "").strip())):
        if value and value.lower() != "auto":
            node[key] = value
        else:
            node.pop(key, None)
    if hide is not None:
        if hide:
            node["hide"] = sorted(set(hide))
        else:
            node.pop("hide", None)
    _save_doc(path, doc)


def set_master_host(path: Path, host: str, ssh: dict) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    doc["master"]["host"] = host
    if ssh:
        doc["master"]["ssh"] = ssh
    _save_doc(path, doc)
