"""Who uses the network and how: traffic, servers in use right now, devices and IP addresses per user.

Everything is read from the master's 3x-ui, which already merges what its nodes report:
- traffic and limits: /clients/list/paged (up/down are totals over all connections of the user —
  Xray counts per user, not per connection);
- who is online on which server: /clients/onlinesByGuid (the master and each node by its panel GUID);
- recent IPs per server: /clients/clientIpsByGuid;
- devices: /clients/hwids/<email> — registered when an app that sends a device ID (Happ, v2RayTun, INCY)
  fetches the subscription. Apps that do not send one never show up there; their IPs still do.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

# 3x-ui's own "subscription as a source" fetches carry no device data; they are not somebody's phone.
SERVICE_AGENTS = ("3x-ui-",)


def server_names(nodes: Iterable[Dict[str, Any]], master: str) -> Dict[str, str]:
    """Panel GUID -> server name (as in fleet.yaml). A GUID that is not a node is the master itself."""
    names = {str(n.get("guid")): str(n.get("name") or n.get("remark") or "") for n in nodes if n.get("guid")}
    return _MasterDefault(names, master)


class _MasterDefault(dict):
    def __init__(self, data: Dict[str, str], master: str):
        super().__init__(data)
        self.master = master

    def __missing__(self, key: str) -> str:
        return self.master


def is_service_device(device: Dict[str, Any]) -> bool:
    return str(device.get("userAgent") or "").startswith(SERVICE_AGENTS)


def summarize(clients: List[Dict[str, Any]], names: Dict[str, str], online_by: Dict[str, List[str]],
              ips_by: Dict[str, Dict[str, List[Dict[str, Any]]]], devices: Dict[str, Optional[int]],
              hidden: Iterable[str] = ()) -> Dict[str, Dict[str, Any]]:
    """One row per user for the list: traffic, limits, servers in use now, distinct IPs, devices."""
    skip = set(hidden)
    where: Dict[str, List[str]] = {}
    for guid, emails in (online_by or {}).items():
        for email in emails or []:
            server = names[guid]
            if server not in where.setdefault(email, []):
                where[email].append(server)
    ips: Dict[str, set] = {}
    for _guid, per_user in (ips_by or {}).items():
        for email, entries in (per_user or {}).items():
            for e in entries or []:
                if e.get("ip"):
                    ips.setdefault(email, set()).add(e["ip"])
    out: Dict[str, Dict[str, Any]] = {}
    for c in clients:
        email = c.get("email")
        if not email or email in skip:
            continue
        t = c.get("traffic") or {}
        up, down = int(t.get("up") or 0), int(t.get("down") or 0)
        limit_dev, limit_ip = int(c.get("limitHwid") or 0), int(c.get("limitIp") or 0)
        n_dev, n_ip = devices.get(email), len(ips.get(email, ()))
        out[email] = {
            "enabled": bool(c.get("enable", True)), "up": up, "down": down, "used": up + down,
            "quota": int(c.get("totalGB") or 0), "expiry": int(c.get("expiryTime") or 0),
            "lastOnline": int(t.get("lastOnline") or 0), "lastSubFetch": int(t.get("lastSubFetch") or 0),
            "limitDevices": limit_dev, "limitIp": limit_ip,
            "devices": n_dev, "ips": n_ip, "servers": where.get(email, []),
            # More devices or addresses than allowed: the subscription is shared further than intended.
            "over": bool((limit_dev and n_dev is not None and n_dev > limit_dev) or (limit_ip and n_ip > limit_ip)),
        }
    return out


def device_view(device: Dict[str, Any]) -> Dict[str, Any]:
    """A registered device as the page shows it."""
    agent = str(device.get("userAgent") or "")
    app = agent.split("/")[0] if agent else ""
    if len(agent.split("/")) > 1:
        app += " " + agent.split("/")[1]
    return {"id": device.get("id"), "os": device.get("deviceOs") or "", "osVersion": device.get("osVersion") or "",
            "model": device.get("deviceModel") or "", "app": app.strip(), "firstSeen": int(device.get("firstSeen") or 0),
            "lastSeen": int(device.get("lastSeen") or 0), "service": is_service_device(device)}


def validate_limit(value: Any, what: str) -> int:
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: введите число") from None
    if n < 0 or n > 1000:
        raise ValueError(f"{what}: от 0 (без ограничения) до 1000")
    return n
