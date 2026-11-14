"""Client for the 3x-ui v3 REST API (Bearer-token auth, /panel/api/*)."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

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
        self.post("/server/restartXrayService")

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
        """Real HTTP probe through the outbound, run by the panel's own Xray."""
        return self.post("/xray/testOutbound", form={"outbound": json.dumps(outbound)}) or {}

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

    def delete_inbound(self, inbound_id: int) -> None:
        self.post(f"/inbounds/del/{inbound_id}")

    # ---- clients --------------------------------------------------------
    def clients(self) -> List[Dict[str, Any]]:
        return self.get("/clients/list") or []

    def client(self, email: str) -> Optional[Dict[str, Any]]:
        try:
            return self.get(f"/clients/get/{email}")
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
