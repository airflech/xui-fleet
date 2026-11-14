"""Pure builders for 3x-ui inbounds and Xray outbounds/routing. No I/O here.

To add a new transport, register one more Profile: everything else (cascade
outbound, drift detection) is derived from the inbound the profile produces.
"""
from __future__ import annotations

import copy
import secrets
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

PREFIX = "fleet-"
VISION = "xtls-rprx-vision"


def direct_tag(node: str) -> str:
    return f"{PREFIX}direct-{node}"


def cascade_tag(node: str) -> str:
    return f"{PREFIX}cascade-{node}"


def out_tag(node: str) -> str:
    return f"{PREFIX}out-{node}"


def xhttp_tag(node: str) -> str:
    return f"{PREFIX}xhttp-{node}"


def hy2_tag(node: str) -> str:
    return f"{PREFIX}hy2-{node}"


# One naming scheme for every node so users see the same set everywhere.
def rhy2_tag(node: str) -> str:
    """Hysteria2 entry on the RU master chained to the node (UDP survives TCP freezes)."""
    return f"{PREFIX}rhy2-{node}"


def warp_tag(node: str) -> str:
    """Entry on the node whose traffic leaves through Cloudflare WARP."""
    return f"{PREFIX}warp-{node}"


def warpup_tag(node: str) -> str:
    """Hidden node entry the master dials for RU -> node -> WARP."""
    return f"{PREFIX}warpup-{node}"


def rwarp_tag(node: str) -> str:
    """Hysteria2 entry on the master chained to the node's WARP exit."""
    return f"{PREFIX}rwarp-{node}"


def out_warp_tag(node: str) -> str:
    return f"{PREFIX}out-warp-{node}"


def remark_warp(label: str) -> str:
    return f"{label} · WARP"


def remark_cascade_warp(label: str) -> str:
    return f"{label} · через RU → WARP"


def rxh_tag(node: str) -> str:
    """Protected XHTTP entry on the RU master, routed to the node."""
    return f"{PREFIX}rxh-{node}"


def remark_cascade_xhttp(label: str) -> str:
    return f"{label} · через RU (XHTTP)"


def remark_cascade_hy2(label: str) -> str:
    return f"{label} · через RU (Hysteria2)"


def remark_direct(label: str) -> str:
    return label


def remark_cascade(label: str) -> str:
    return f"{label} · через RU"


def remark_xhttp(label: str) -> str:
    return f"{label} · XHTTP"


def remark_hy2(label: str) -> str:
    return f"{label} · Hysteria2"


def build_hysteria_inbound(*, tag: str, remark: str, port: int, node_id: Optional[int], host: str, cert_pem: str = "",
                           key_pem: str = "", pin_hex: str = "", sort_index: int, cert_file: str = "",
                           key_file: str = "", share_addr: str = "") -> Dict[str, Any]:
    """Hysteria2 over QUIC with salamander obfuscation.

    Either a self-signed cert inline + its pin in the links, or a real cert by file path (no pin needed).
    """
    if cert_file:
        cert = {"certificateFile": cert_file, "keyFile": key_file}
    else:
        cert = {"certificate": cert_pem.splitlines(), "key": key_pem.splitlines()}
    cert.update(usage="encipherment", ocspStapling=3600, oneTimeLoading=False, buildChain=False)
    inbound = {
        "enable": True, "remark": remark, "listen": "", "port": port, "protocol": "hysteria", "tag": tag,
        "expiryTime": 0, "total": 0, "subSortIndex": sort_index, "excludeFromSub": False,
        "settings": {"version": 2, "clients": []},
        "streamSettings": {
            "network": "hysteria",
            "hysteriaSettings": {"version": 2, "udpIdleTimeout": 60},
            "security": "tls",
            "tlsSettings": {
                "serverName": host, "minVersion": "1.3", "maxVersion": "1.3", "alpn": ["h3"],
                "certificates": [cert],
                "settings": {"fingerprint": "chrome", "pinnedPeerCertSha256": [pin_hex] if pin_hex else []},
            },
            "finalmask": {
                "udp": [{"type": "salamander", "settings": {"password": secrets.token_hex(8)}}],
                "quicParams": {"congestion": "bbr", "maxIdleTimeout": 30, "keepAlivePeriod": 10,
                               "maxIncomingStreams": 1024},
            },
        },
        "sniffing": {"enabled": False},
    }
    if node_id is not None:
        inbound["nodeId"] = node_id
    if share_addr:
        inbound["shareAddrStrategy"], inbound["shareAddr"] = "custom", share_addr
    return inbound


@dataclass(frozen=True)
class Profile:
    name: str
    flow: str  # client flow required by this transport ("" = none)
    network: Callable[[], Dict[str, Any]]  # transport part of streamSettings
    encrypted: bool = False  # VLESS Encryption on top of Reality


def _tcp() -> Dict[str, Any]:
    return {"network": "tcp", "tcpSettings": {"header": {"type": "none"}}}


def _xhttp() -> Dict[str, Any]:
    return {"network": "xhttp", "xhttpSettings": {"path": "/" + secrets.token_hex(6), "host": "", "mode": "auto"}}


# XMUX from the field guides: a few long-lived H2 connections, each retired after a random number of
# requests / seconds, so no single connection grows into the long fat flow that gets frozen.
XMUX: Dict[str, Any] = {"maxConcurrency": "16-32", "maxConnections": 0, "cMaxReuseTimes": 0,
                        "hMaxRequestTimes": "600-900", "hMaxReusableSecs": "1800-3000"}


def _xhttp_protected() -> Dict[str, Any]:
    stream = _xhttp()
    # mode auto: with Reality the client picks stream-one (one bidirectional H2 stream).
    stream["xhttpSettings"]["xmux"] = dict(XMUX)
    return stream


PROFILES: Dict[str, Profile] = {
    "vless-reality-tcp": Profile("vless-reality-tcp", VISION, _tcp),
    "vless-reality-xhttp": Profile("vless-reality-xhttp", "", _xhttp),
    # XHTTP + VLESS Encryption + Vision + XMUX; needs `vless_enc` (see build_inbound).
    "vless-reality-xhttp-enc": Profile("vless-reality-xhttp-enc", VISION, _xhttp_protected, encrypted=True),
}


def get_profile(name: str) -> Profile:
    if name not in PROFILES:
        raise ValueError(f"неизвестный профиль {name!r}; доступны: {', '.join(PROFILES)}")
    return PROFILES[name]


def build_inbound(
    *,
    tag: str,
    remark: str,
    port: int,
    profile: Profile,
    target: str,
    server_names: List[str],
    fingerprint: str,
    private_key: str,
    public_key: str,
    node_id: Optional[int] = None,
    exclude_from_sub: bool = False,
    sniffing: bool = True,
    sort_index: int = 1,
    share_addr: str = "",
    vless_enc: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """`vless_enc` — {"decryption", "encryption"} pair from the panel's getNewVlessEnc (mlkem768)."""
    if profile.encrypted and not vless_enc:
        raise ValueError(f"профилю {profile.name} нужны ключи VLESS Encryption")
    stream = profile.network()
    stream["security"] = "reality"
    stream["realitySettings"] = {
        "show": False,
        "xver": 0,
        "target": target,
        "serverNames": list(server_names),
        "privateKey": private_key,
        "minClientVer": "",
        "maxClientVer": "",
        "maxTimediff": 0,
        "shortIds": [secrets.token_hex(8)],
        "mldsa65Seed": "",
        "settings": {
            "publicKey": public_key,
            "fingerprint": fingerprint,
            "serverName": "",
            "spiderX": "/",
            "mldsa65Verify": "",
        },
    }
    inbound: Dict[str, Any] = {
        "enable": True,
        "remark": remark,
        "listen": "",
        "port": port,
        "protocol": "vless",
        "tag": tag,
        "expiryTime": 0,
        "total": 0,
        "subSortIndex": sort_index,
        "excludeFromSub": exclude_from_sub,
        "settings": vless_settings(vless_enc if profile.encrypted else None),
        "streamSettings": stream,
        "sniffing": {
            "enabled": sniffing,
            "destOverride": ["http", "tls", "quic", "fakedns"],
            "metadataOnly": False,
            "routeOnly": False,
        },
    }
    if node_id is not None:
        inbound["nodeId"] = node_id
    if share_addr:
        # Address written into share links, independent of how the sub was fetched.
        inbound["shareAddrStrategy"], inbound["shareAddr"] = "custom", share_addr
    return inbound


def vless_settings(vless_enc: Optional[Dict[str, str]]) -> Dict[str, Any]:
    if vless_enc:
        # Xray refuses to start when "decryption" is set together with a "fallbacks" key (even an empty one).
        return {"clients": [], "decryption": vless_enc["decryption"], "encryption": vless_enc["encryption"]}
    return {"clients": [], "decryption": "none", "encryption": "none", "fallbacks": []}


def is_protected(inbound: Dict[str, Any]) -> bool:
    settings = inbound.get("settings") or {}
    return (settings.get("decryption") or "none") != "none"


def harden_xhttp(current: Dict[str, Any], vless_enc: Optional[Dict[str, str]]) -> Optional[Dict[str, Any]]:
    """Upgrade an existing XHTTP inbound in place: VLESS Encryption + XMUX. Keys and path stay, so
    users only need to refresh the subscription. None when nothing is missing."""
    stream = current.get("streamSettings") or {}
    if stream.get("network") != "xhttp":
        return None
    need_enc = not is_protected(current) and vless_enc is not None
    need_mux = not (stream.get("xhttpSettings") or {}).get("xmux")
    if not (need_enc or need_mux):
        return None
    updated = _clean(current)
    if need_enc:
        clients = (updated.get("settings") or {}).get("clients") or []
        updated["settings"] = dict(vless_settings(vless_enc), clients=clients)
    updated["streamSettings"].setdefault("xhttpSettings", {})["xmux"] = dict(XMUX)
    return updated


def _clean(current: Dict[str, Any]) -> Dict[str, Any]:
    updated = copy.deepcopy(current)
    for key in ("clientStats", "up", "down", "fallbackParent", "originNodeGuid"):
        updated.pop(key, None)
    return updated


def inbound_drift(
    current: Dict[str, Any],
    *,
    remark: str,
    port: int,
    target: str,
    server_names: List[str],
    exclude_from_sub: bool,
    sort_index: int,
    share_addr: str = "",
    fingerprint: str = "",
) -> Optional[Dict[str, Any]]:
    """Return an update payload when the live inbound differs from the config.

    Keys, short ids and the transport are never rotated here: existing client
    links must keep working.
    """
    reality = (current.get("streamSettings") or {}).get("realitySettings") or {}
    same = (
        current.get("remark") == remark
        and int(current.get("port") or 0) == port
        and reality.get("target") == target
        and list(reality.get("serverNames") or []) == list(server_names)
        and bool(current.get("excludeFromSub")) == exclude_from_sub
        and int(current.get("subSortIndex") or 1) == sort_index
        and (not share_addr or (current.get("shareAddrStrategy") == "custom" and current.get("shareAddr") == share_addr))
        and (not fingerprint or (reality.get("settings") or {}).get("fingerprint") == fingerprint)
    )
    if same:
        return None
    updated = _clean(current)
    if fingerprint:  # only goes into share links and the cascade outbound; never breaks a client
        updated["streamSettings"]["realitySettings"].setdefault("settings", {})["fingerprint"] = fingerprint
    updated["remark"] = remark
    updated["port"] = port
    updated["excludeFromSub"] = exclude_from_sub
    updated["subSortIndex"] = sort_index
    if share_addr:
        updated["shareAddrStrategy"], updated["shareAddr"] = "custom", share_addr
    updated["streamSettings"]["realitySettings"]["target"] = target
    updated["streamSettings"]["realitySettings"]["serverNames"] = list(server_names)
    return updated


def outbound_for_inbound(inbound: Dict[str, Any], *, tag: str, address: str, client_id: str, flow: str) -> Dict[str, Any]:
    """VLESS outbound on the master that dials the given (node-hosted) inbound."""
    stream = inbound["streamSettings"]
    reality = stream["realitySettings"]
    out_stream: Dict[str, Any] = {"network": stream.get("network", "tcp"), "security": "reality"}
    network = out_stream["network"]
    if network == "tcp":
        out_stream["tcpSettings"] = {"header": {"type": "none"}}
    elif network == "xhttp":
        xh = stream.get("xhttpSettings") or {}
        out_stream["xhttpSettings"] = {"path": xh.get("path", "/"), "host": xh.get("host", ""), "mode": xh.get("mode", "auto")}
    else:
        raise ValueError(f"каскад не умеет транспорт {network!r}")
    names = reality.get("serverNames") or []
    out_stream["realitySettings"] = {
        "publicKey": (reality.get("settings") or {}).get("publicKey", ""),
        "fingerprint": (reality.get("settings") or {}).get("fingerprint") or "firefox",
        "serverName": names[0] if names else "",
        "shortId": (reality.get("shortIds") or [""])[0],
        "spiderX": "/",
    }
    return {
        "tag": tag,
        "protocol": "vless",
        "settings": {
            "address": address,
            "port": int(inbound["port"]),
            "id": client_id,
            "flow": flow,
            "encryption": (inbound.get("settings") or {}).get("encryption") or "none",
        },
        "streamSettings": out_stream,
    }


# What counts as "Russian" for routing. Domain rules only: the master never resolves names itself, because
# a Russian resolver may answer blocked sites with a stub address in Russia, which would then go "direct".
RU_DOMAINS = ["geosite:category-ru", "domain:ru", "domain:su", "domain:xn--p1ai", "domain:xn--d1acj3b",
              "domain:xn--80asehdb", "domain:xn--80aswg", "domain:xn--c1avg", "domain:moscow", "domain:tatar"]
RU_IPS = ["geoip:ru"]
RU_RULE_TAG = f"{PREFIX}ru-direct"


def ru_direct_rules(inbound_tags: List[str], extra: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    extra = extra or []
    domains = RU_DOMAINS + [e for e in extra if not e.startswith("geoip:") and "/" not in e]
    ips = RU_IPS + [e for e in extra if e.startswith("geoip:") or "/" in e]
    tags = sorted(inbound_tags)
    return [{"type": "field", "ruleTag": RU_RULE_TAG, "inboundTag": tags, "domain": domains, "outboundTag": "direct"},
            {"type": "field", "ruleTag": RU_RULE_TAG + "-ip", "inboundTag": tags, "ip": ips, "outboundTag": "direct"}]


def happ_routing_link(extra: Optional[List[str]] = None, name: str = "RU напрямую") -> str:
    """Happ routing profile (happ://routing/onadd/…): everything through the VPN except Russian sites,
    local networks and the user's extra entries. Uses Happ's own geo files (v2fly geosite, geoip with "ru")."""
    import base64
    import json
    extra = extra or []
    profile = {
        "Name": name,
        "GlobalProxy": "true",
        "RouteOrder": "block-proxy-direct",
        "DirectSites": ["geosite:private"] + RU_DOMAINS + [e for e in extra if not e.startswith("geoip:") and "/" not in e],
        "DirectIp": ["geoip:private", "geoip:ru", "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                     "169.254.0.0/16", "224.0.0.0/4", "255.255.255.255"] + [e for e in extra if e.startswith("geoip:") or "/" in e],
        "ProxySites": [], "ProxyIp": [], "BlockSites": [], "BlockIp": [],
        # Russian names are resolved by Yandex DNS, the rest remotely through the tunnel (no DNS leaks abroad).
        "DomesticDNSType": "DoH", "DomesticDNSDomain": "https://77.88.8.8/dns-query", "DomesticDNSIP": "77.88.8.8",
        "RemoteDNSType": "DoH", "RemoteDNSDomain": "https://1.1.1.1/dns-query", "RemoteDNSIP": "1.1.1.1",
        "DomainStrategy": "IPIfNonMatch",
        "FakeDNS": "false",
    }
    data = json.dumps(profile, ensure_ascii=False, separators=(",", ":")).encode()
    return "happ://routing/onadd/" + base64.b64encode(data).decode()


def merge_template(template: Dict[str, Any], routes: Dict[str, Dict[str, Any]],
                   ru_direct: bool = False, extra_direct: Optional[List[str]] = None) -> Dict[str, Any]:
    """Replace every fleet-managed outbound/rule in the Xray template.

    `routes` maps a master inbound tag to the outbound its traffic must leave
    through. Anything not prefixed with `fleet-out-` is left untouched.
    """
    tpl = copy.deepcopy(template)
    managed = f"{PREFIX}out-"
    outbounds = [o for o in tpl.get("outbounds") or [] if not str(o.get("tag", "")).startswith(managed)]
    routing = tpl.setdefault("routing", {})
    rules = [r for r in routing.get("rules") or [] if not str(r.get("outboundTag", "")).startswith(managed)
             and not str(r.get("ruleTag", "")).startswith(RU_RULE_TAG)]

    # Russian destinations of the RU entries leave from the master; must precede the cascade rules.
    new_rules = ru_direct_rules(list(routes), extra_direct) if ru_direct and routes else []
    added: Dict[str, bool] = {}
    for inbound_tag in sorted(routes):
        outbound = routes[inbound_tag]
        if outbound["tag"] not in added:  # several entries (VLESS, Hysteria2) may share one exit
            outbounds.append(outbound)
            added[outbound["tag"]] = True
        new_rules.append({"type": "field", "inboundTag": [inbound_tag], "outboundTag": outbound["tag"]})

    # Keep the internal API rule first; fleet rules must win over everything else
    # (otherwise e.g. a geoip/bittorrent rule could leak cascade traffic via RU).
    head = 0
    for i, rule in enumerate(rules):
        if rule.get("outboundTag") == "api" or rule.get("inboundTag") == ["api"]:
            head = i + 1
    tpl["outbounds"] = outbounds
    routing["rules"] = rules[:head] + new_rules + rules[head:]
    return tpl
