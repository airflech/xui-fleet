"""Client for the 3x-ui v3 REST API (Bearer-token auth, /panel/api/*)."""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx


class PanelError(RuntimeError):
    pass


class Panel:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        # TLS verification is off on purpose: we reach the panel through an SSH
        # tunnel (authenticated by the pinned host key) on 127.0.0.1.
        self._http = httpx.Client(
            base_url=self.base_url + "/panel/api",
            headers={"Authorization": f"Bearer {token}"},
            verify=False,
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    # ---- transport ------------------------------------------------------
    def _call(self, method: str, path: str, *, json_body: Any = None, form: Optional[Dict[str, str]] = None,
              retries: int = 3) -> Any:
        last: Optional[Exception] = None
        for attempt in range(retries):
            try:
                resp = self._http.request(method, path, json=json_body, data=form)
            except httpx.TransportError as exc:  # panel restarting, tunnel hiccup
                last = exc
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code in (401, 403):
                raise PanelError(f"{method} {path}: API-токен отклонён (HTTP {resp.status_code})")
            if resp.status_code == 404:
                raise PanelError(f"{method} {path}: HTTP 404 — неверный base path или токен не передан")
            if resp.status_code >= 500:
                last = PanelError(f"{method} {path}: HTTP {resp.status_code}")
                time.sleep(1.5 * (attempt + 1))
                continue
            try:
                body = resp.json()
            except ValueError as exc:
                raise PanelError(f"{method} {path}: не JSON (HTTP {resp.status_code})") from exc
            if isinstance(body, dict) and "success" in body:
                if not body["success"]:
                    raise PanelError(f"{method} {path}: {body.get('msg') or 'панель вернула ошибку'}")
                return body.get("obj")
            return body
        raise PanelError(f"{method} {path}: панель недоступна ({last})")

    def download(self, path: str, timeout: float = 120.0) -> bytes:
        """Raw file from the API (e.g. the database backup)."""
        resp = self._http.get(path, timeout=timeout)
        if resp.status_code != 200:
            raise PanelError(f"GET {path}: HTTP {resp.status_code}")
        return resp.content

    def get(self, path: str) -> Any:
        return self._call("GET", path)

    def post(self, path: str, json_body: Any = None, form: Optional[Dict[str, str]] = None) -> Any:
        return self._call("POST", path, json_body=json_body, form=form)

    def wait_ready(self, seconds: int = 60) -> Dict[str, Any]:
        deadline = time.time() + seconds
        err: Optional[Exception] = None
        while time.time() < deadline:
            try:
                return self._call("GET", "/server/status", retries=1)
            except PanelError as exc:
                err = exc
                if "токен" in str(exc):
                    raise
                time.sleep(2)
        raise PanelError(f"панель не ответила за {seconds}с: {err}")

    # ---- server ---------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        return self.get("/server/status")

    def new_x25519(self) -> Dict[str, str]:
        return self.get("/server/getNewX25519Cert")

    def restart_xray(self) -> None:
        """Restart Xray. The panel may drop the very connection that asked for it (an EOF instead of an answer) —
        the restart has happened then; it counts as done once the panel answers again."""
        try:
            self.post("/server/restartXrayService")
        except PanelError:
            time.sleep(3)
            self.wait_ready(60)

    def scan_reality_target(self, target: str, sni: str) -> Any:
        return self.post("/server/scanRealityTarget", form={"target": target, "sni": sni})

    # ---- settings -------------------------------------------------------
    def settings(self) -> Dict[str, Any]:
        return self.post("/setting/all")

    def update_settings(self, settings: Dict[str, Any]) -> None:
        self.post("/setting/update", json_body=settings)

    def restart_panel(self) -> None:
        self.post("/setting/restartPanel")

    # ---- xray template --------------------------------------------------
    def xray_template(self) -> Dict[str, Any]:
        obj = self.post("/xray/")
        if isinstance(obj, str):
            obj = json.loads(obj)
        tpl = obj["xraySetting"]
        return json.loads(tpl) if isinstance(tpl, str) else tpl

    def test_outbound(self, outbound: Dict[str, Any]) -> Dict[str, Any]:
        """Real HTTP probe through the outbound, run by the panel's own Xray. The panel runs one probe at a time
        (another copy of the app, or its own 5-minute check, may be probing right now): wait for the turn
        instead of reporting a working link as broken."""
        busy = "already running"
        for attempt in range(45):  # up to ~90 s: a probe through a dead way waits for its own timeout
            if attempt:
                time.sleep(2)
            try:
                got = self.post("/xray/testOutbound", form={"outbound": json.dumps(outbound)}) or {}
            except PanelError as exc:
                if busy in str(exc):
                    continue
                raise
            if busy in str(got.get("msg") or got.get("error") or ""):
                continue
            return got
        raise PanelError("панель занята другими проверками подключений — попробуйте через минуту")

    def save_xray_template(self, template: Dict[str, Any]) -> None:
        self.post("/xray/update", form={"xraySetting": json.dumps(template, ensure_ascii=False, indent=2)})

    # ---- nodes ----------------------------------------------------------
    def nodes(self) -> List[Dict[str, Any]]:
        return self.get("/nodes/list") or []

    def add_node(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.post("/nodes/add", json_body=payload)

    def update_node(self, node_id: int, payload: Dict[str, Any]) -> None:
        self.post(f"/nodes/update/{node_id}", json_body=payload)

    def delete_node(self, node_id: int) -> None:
        self.post(f"/nodes/del/{node_id}")

    def probe_node(self, node_id: int) -> Any:
        return self.post(f"/nodes/probe/{node_id}")

    # ---- inbounds -------------------------------------------------------
    def inbounds(self) -> List[Dict[str, Any]]:
        return [_decode_inbound(i) for i in (self.get("/inbounds/list") or [])]

    def add_inbound(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return _decode_inbound(self.post("/inbounds/add", json_body=payload))

    def update_inbound(self, inbound_id: int, payload: Dict[str, Any]) -> None:
        self.post(f"/inbounds/update/{inbound_id}", json_body=payload)

    def set_inbound_enable(self, inbound_id: int, enable: bool) -> None:
        """On/off has its own call: /inbounds/update ignores `enable` (3x-ui 3.11)."""
        self.post(f"/inbounds/setEnable/{inbound_id}", json_body={"enable": bool(enable)})

    def delete_inbound(self, inbound_id: int) -> None:
        self.post(f"/inbounds/del/{inbound_id}")

    # ---- clients --------------------------------------------------------
    def clients(self) -> List[Dict[str, Any]]:
        return self.get("/clients/list") or []

    def client(self, email: str) -> Optional[Dict[str, Any]]:
        try:
            return self.get(f"/clients/get/{_seg(email)}")
        except PanelError:
            return None

    def add_client(self, client: Dict[str, Any], inbound_ids: List[int]) -> None:
        self.post("/clients/add", json_body={"client": client, "inboundIds": inbound_ids})

    def attach_clients(self, emails: List[str], inbound_ids: List[int]) -> None:
        self.post("/clients/bulkAttach", json_body={"emails": emails, "inboundIds": inbound_ids})

    def delete_client(self, email: str) -> None:
        self.post(f"/clients/del/{email}")

    def sub_links(self, sub_id: str) -> List[str]:
        return self.get(f"/clients/subLinks/{sub_id}") or []

    # ---- client statistics ----------------------------------------------
    def clients_with_traffic(self) -> List[Dict[str, Any]]:
        """Every client with its limits and traffic counters (up/down, lastOnline, lastSubFetch)."""
        out: List[Dict[str, Any]] = []
        page = 1
        while True:
            res = self.get(f"/clients/list/paged?page={page}&pageSize=200") or {}
            items = res.get("items") or []
            out.extend(items)
            if not items or len(out) >= int(res.get("total") or 0):
                return out
            page += 1

    def onlines_by_server(self) -> Dict[str, List[str]]:
        """Who is connected right now, per server (keyed by the panel GUID of the master or a node)."""
        return self.post("/clients/onlinesByGuid") or {}

    def ips_by_server(self) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
        """Recent client IPs per server: {guid: {email: [{ip, timestamp}]}}."""
        return self.post("/clients/clientIpsByGuid") or {}

    def client_devices(self, email: str) -> List[Dict[str, Any]]:
        """Devices registered by the subscription (HWID): deviceOs, osVersion, deviceModel, userAgent, firstSeen, lastSeen."""
        return self.post(f"/clients/hwids/{_seg(email)}") or []

    def client_ips(self, email: str) -> List[Dict[str, Any]]:
        """Recent IPs of one client with the server each one was seen on: [{ip, time, node}]."""
        return self.post(f"/clients/ips/{_seg(email)}") or []

    def forget_device(self, email: str, device_id: int) -> None:
        self._call("DELETE", f"/clients/hwids/{_seg(email)}/{int(device_id)}")

    def reset_traffic(self, email: str) -> None:
        """Zero the counters; a user disabled by the quota is enabled again (one disabled by hand stays off)."""
        self.post(f"/clients/resetTraffic/{_seg(email)}")

    def update_client(self, email: str, fields: Dict[str, Any]) -> None:
        """Change only the given fields. 3x-ui 3.11 replaces the whole record with the body (a field left out becomes
        empty: the user is switched off, the comment, the term and the group are lost) and refuses a body without the
        email. So the current record is read and sent back whole, with `fields` on top."""
        cur = self.client(email)
        record = cur.get("client") if isinstance(cur, dict) and isinstance(cur.get("client"), dict) else cur
        if not isinstance(record, dict) or not record.get("email"):
            raise PanelError(f"клиент {email} не найден в панели — обновите список")
        # The record as the panel shows it is not quite the form it takes back: «id» is the database number there and
        # the client's key here, WireGuard lists come as strings… Send what is ours and the scalars; a field the
        # panel cannot read is dropped (named in its error) — the panel then keeps it as it is.
        body = {k: v for k, v in record.items() if k not in CLIENT_RECORD_ONLY and v is not None}
        if "uuid" in record:
            body["id"] = record.get("uuid") or ""
        body.update(fields)
        body["email"] = email
        for _ in range(12):
            try:
                self.post(f"/clients/update/{_seg(email)}", json_body=body)
                return
            except PanelError as exc:
                m = re.search(r"Go struct field \.?(\w+) of type", str(exc))
                if not m or m.group(1) not in body or m.group(1) in fields or m.group(1) in ("email", "id", "enable"):
                    raise
                body.pop(m.group(1))
        raise PanelError(f"панель не принимает изменения клиента {email}")


# Fields of the panel's client record that the update form does not take (or takes in another shape).
CLIENT_RECORD_ONLY = {"createdAt", "updatedAt", "uuid", "allowedIPs", "privateKey", "publicKey", "preSharedKey",
                      "keepAlive", "forwardedPorts", "reverse"}


def _seg(value: str) -> str:
    """One path segment: names may hold letters of any alphabet, never a slash or a query."""
    return quote(str(value), safe="@.-_")


def _decode_inbound(inbound: Dict[str, Any]) -> Dict[str, Any]:
    """Older panels return settings blocks as JSON strings; normalise to objects."""
    for key in ("settings", "streamSettings", "sniffing"):
        value = inbound.get(key)
        if isinstance(value, str) and value:
            try:
                inbound[key] = json.loads(value)
            except ValueError:
                pass
    return inbound
