"""History of the network, kept on this computer: route health every 5 minutes, traffic counters every hour.

Samples are taken by the running app (see ui.serve), so the history has gaps while the app is closed —
the page says so. Health is measured by the main panel, i.e. from the Russian network: latency RU → node.

Files, one folder per profile (keyed by its fleet.yaml path):
    health.jsonl   {"t": epoch, "n": {node: latency_ms | null (down)}, "m": 1 | 0 (master Xray running)}
    traffic.jsonl  {"t": epoch, "u": {user: bytes total}, "r": {node: {"ru": bytes via RU, "d": bytes direct}}}
Counters are cumulative (as 3x-ui keeps them); per-day amounts are the differences between samples, and a
counter that went down (reset in the panel) counts from zero.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

HEALTH_EVERY = 300
TRAFFIC_EVERY = 3600
KEEP_HEALTH_DAYS = 14
KEEP_TRAFFIC_DAYS = 120
CASCADE_KINDS = {"cascade", "rhy2", "rxh", "rwarp"}  # on the master, owned by a node: traffic RU → node
DIRECT_KINDS = {"direct", "xhttp", "hy2", "warp"}  # on the node itself


def folder(config_path: Path) -> Path:
    from .service import APP_DIR
    key = hashlib.sha1(str(Path(config_path).resolve()).encode()).hexdigest()[:12]
    return APP_DIR / "history" / key


def _append(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _read(path: Path, since: float) -> List[Dict[str, Any]]:
    out = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # a line cut by a crash
                if row.get("t", 0) >= since:
                    out.append(row)
    except FileNotFoundError:
        pass
    return out


def _prune(path: Path, keep_days: int) -> None:
    """Drop old lines once in a while (cheap: the files are small)."""
    if not path.exists() or path.stat().st_size < 256 * 1024:
        return
    rows = _read(path, time.time() - keep_days * 86400)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- taking samples
def health_sample(nodes: Iterable[Dict[str, Any]], master_status: Dict[str, Any]) -> Dict[str, Any]:
    """From the panel's own node probes (what the page shows live)."""
    n = {}
    for node in nodes:
        if not node.get("enable", True):
            continue
        ok = node.get("status") == "online"
        n[node.get("name")] = int(node.get("latencyMs") or 0) if ok else None
    xray = (master_status.get("xray") or {}).get("state")
    return {"t": int(time.time()), "n": n, "m": 1 if xray == "running" else 0}


def traffic_sample(clients: Iterable[Dict[str, Any]], inbounds: Iterable[Dict[str, Any]], node_names: Dict[Any, str],
                   hidden: Iterable[str]) -> Dict[str, Any]:
    from . import xrayconf as xc
    skip = set(hidden)
    users = {}
    for c in clients:
        email = c.get("email")
        if email and email not in skip:
            t = c.get("traffic") or {}
            users[email] = int(t.get("up") or 0) + int(t.get("down") or 0)
    routes: Dict[str, Dict[str, int]] = {}
    for i in inbounds:
        kind = xc.inbound_kind(i.get("tag", ""))
        if not kind:
            continue
        total = int(i.get("up") or 0) + int(i.get("down") or 0)
        name = kind[1]
        if kind[0] in CASCADE_KINDS and not i.get("nodeId"):
            routes.setdefault(name, {"ru": 0, "d": 0})["ru"] += total
        elif kind[0] in DIRECT_KINDS and i.get("nodeId"):
            routes.setdefault(node_names.get(i.get("nodeId"), name), {"ru": 0, "d": 0})["d"] += total
    return {"t": int(time.time()), "u": users, "r": routes}


class History:
    def __init__(self, config_path: Path):
        self.dir = folder(config_path)

    def add_health(self, row: Dict[str, Any]) -> None:
        _append(self.dir / "health.jsonl", row)
        _prune(self.dir / "health.jsonl", KEEP_HEALTH_DAYS)

    def add_traffic(self, row: Dict[str, Any]) -> None:
        _append(self.dir / "traffic.jsonl", row)
        _prune(self.dir / "traffic.jsonl", KEEP_TRAFFIC_DAYS)

    def last(self, kind: str) -> float:
        rows = _read(self.dir / f"{kind}.jsonl", time.time() - 2 * 86400)
        return rows[-1]["t"] if rows else 0

    def forget(self) -> None:
        for p in self.dir.glob("*.jsonl"):
            p.unlink(missing_ok=True)

    # ------------------------------------------------------------ reading for the page
    def health(self, hours: int, points: int = 288) -> Dict[str, Any]:
        """Per node: latency series (null = route down), uptime % and median / 95th latency over the period.
        Long periods are thinned to about `points` per node, keeping the worst sample of each bucket."""
        since = time.time() - hours * 3600
        rows = _read(self.dir / "health.jsonl", since)
        names = sorted({n for r in rows for n in r.get("n", {})})
        step = max(1, len(rows) // points)
        out: Dict[str, Any] = {}
        for name in names:
            seen = [(r["t"], r["n"][name]) for r in rows if name in r.get("n", {})]
            if not seen:
                continue
            up = [v for _t, v in seen if v is not None]
            series = []
            for k in range(0, len(seen), step):
                chunk = seen[k:k + step]
                down = any(v is None for _t, v in chunk)
                series.append([chunk[-1][0], None if down else max(v for _t, v in chunk)])
            lat = sorted(up)
            out[name] = {"points": series, "uptime": round(100 * len(up) / len(seen), 2),
                         "median": lat[len(lat) // 2] if lat else None,
                         "p95": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None}
        master = [r.get("m", 1) for r in rows]
        return {"nodes": out, "master": round(100 * sum(master) / len(master), 2) if master else None,
                "from": rows[0]["t"] if rows else None, "samples": len(rows), "every": HEALTH_EVERY}

    def traffic(self, days: int) -> Dict[str, Any]:
        """Per day (local dates): bytes of every user and of every route (via RU / direct)."""
        since = time.time() - (days + 1) * 86400
        rows = _read(self.dir / "traffic.jsonl", since)
        day_keys: List[str] = []
        users: Dict[str, Dict[str, int]] = {}
        routes: Dict[str, Dict[str, Dict[str, int]]] = {}

        def add(bucket: Dict[str, int], day: str, amount: int) -> None:
            bucket[day] = bucket.get(day, 0) + amount
        for prev, cur in zip(rows, rows[1:]):
            day = time.strftime("%Y-%m-%d", time.localtime(cur["t"]))
            if day not in day_keys:
                day_keys.append(day)
            for email, total in cur.get("u", {}).items():
                before = prev.get("u", {}).get(email)
                if before is None:
                    continue  # appeared in this sample: its earlier traffic is not "today's"
                add(users.setdefault(email, {}), day, total - before if total >= before else total)
            for node, r in cur.get("r", {}).items():
                p = prev.get("r", {}).get(node)
                if p is None:
                    continue
                for k in ("ru", "d"):
                    add(routes.setdefault(node, {}).setdefault(k, {}), day, r[k] - p[k] if r[k] >= p[k] else r[k])
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - (days - 1) * 86400))
        day_keys = [d for d in day_keys if d >= cutoff]
        return {"days": day_keys,
                "users": {u: [v.get(d, 0) for d in day_keys] for u, v in users.items()},
                "routes": {n: {k: [v.get(k, {}).get(d, 0) for d in day_keys] for k in ("ru", "d")} for n, v in routes.items()},
                "from": rows[0]["t"] if rows else None, "every": TRAFFIC_EVERY}


def record(fleet: Any, hist: History, now: Optional[float] = None) -> List[str]:
    """Take whatever samples are due. Returns what was recorded (for tests and the log)."""
    from .reconcile import _flatten_nodes
    now = now or time.time()
    done = []
    want_health = now - hist.last("health") >= HEALTH_EVERY - 5
    want_traffic = now - hist.last("traffic") >= TRAFFIC_EVERY - 5
    if not (want_health or want_traffic):
        return done
    if want_health:
        nodes, _inb = fleet.status()
        hist.add_health(health_sample(nodes, getattr(fleet, "master_status", {}) or {}))
        done.append("health")
    if want_traffic:
        with fleet.master_panel(provision=False) as panel:
            names = {n.get("id"): n.get("name") for n in _flatten_nodes(panel.nodes())}
            hist.add_traffic(traffic_sample(panel.clients_with_traffic(), panel.inbounds(), names,
                                            fleet.cfg.clients.service_emails))
        done.append("traffic")
    return done
