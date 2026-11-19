"""Turn a bare VPS into a ready 3x-ui panel over SSH. Every step is idempotent."""
from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import FleetConfig, ServerBase
from .ssh import SSH
from .state import State

XUI = "/usr/local/x-ui/x-ui"
CERT_DIR = "/etc/x-ui/fleet-cert"
MASTER_TOKEN = "xui-fleet"  # admin token the tool itself uses on the master
NODE_TOKEN = "xui-fleet-master"  # token the master uses to drive a node

# The x-ui CLI talks to the DB directly; PostgreSQL installs keep the DSN here.
_ENV = "set -a; [ -f /etc/default/x-ui ] && . /etc/default/x-ui; set +a\n"


@dataclass
class PanelInfo:
    scheme: str
    port: int
    base_path: str
    api_token: Optional[str]  # set only when freshly minted in this run
    stored_token: Optional[str]
    cert_pin: str = ""  # base64 SHA-256 of the self-signed leaf ("" for CA-issued certs)
    version: str = ""
    installed_now: bool = False

    @property
    def token(self) -> Optional[str]:
        return self.api_token or self.stored_token


def provision(cfg: FleetConfig, server: ServerBase, role: str, ssh: SSH, state: State,
              log: Callable[[str], None], master_ip: Optional[str] = None,
              open_ports: Optional[list] = None) -> PanelInfo:
    """role: "master" | "node"."""
    name = server.name
    facts = _facts(ssh)
    if facts.get("init") != "systemd":
        raise RuntimeError(f"{name}: нужен Linux с systemd (обнаружено: {facts.get('init') or 'неизвестно'})")
    tls = cfg.tls_for(server)
    hardening = cfg.hardening_for(server)

    installed_now = False
    if not facts.get("xui"):
        log(f"{name}: устанавливаю 3x-ui {cfg.panel.version} ({facts.get('os')}, {facts.get('arch')})")
        _install(ssh, cfg.panel.version, server, tls)
        installed_now = True
    elif facts["xui"].lstrip("v") != cfg.panel.version.lstrip("v"):
        log(f"{name}: уже установлен 3x-ui {facts['xui']} (в конфиге {cfg.panel.version}) — версию не трогаю, "
            f"обновить: `xui-fleet upgrade`")

    if hardening.bbr:
        _bbr(ssh)

    cert = _cert_path(ssh)
    pin = ""
    if not cert and tls.mode != "none":
        if tls.mode in ("ip", "domain") and installed_now:
            log(f"{name}: Let's Encrypt не выдал сертификат (нужен открытый порт 80) — ставлю самоподписанный")
        elif not installed_now:
            log(f"{name}: у панели нет сертификата — ставлю самоподписанный")
        _selfsigned(ssh, server.host)
        cert = _cert_path(ssh)
    if cert and cert.startswith(CERT_DIR):
        pin = ssh.run(f"openssl x509 -in {CERT_DIR}/panel.crt -outform der | openssl dgst -sha256 -binary | base64").out.strip()

    settings = _settings(ssh)
    scheme = "https" if cert else "http"

    stored = state.get(name, "api_token")
    fresh: Optional[str] = None
    if not stored or not _token_works(ssh, scheme, settings["port"], settings["base_path"], stored):
        if role == "master":
            log(f"{name}: выпускаю API-токен (admin)")
            fresh = _mint_token(ssh, MASTER_TOKEN, "admin")
        else:
            fresh = _mint_node_token(ssh, log)
        state.update(name, api_token=fresh)
        if not _wait_token(ssh, scheme, settings["port"], settings["base_path"], fresh):
            raise RuntimeError(f"{name}: панель не принимает новый API-токен — проверьте `systemctl status x-ui`")

    if hardening.firewall and role == "node":
        _firewall(ssh, cfg.ssh_for(server).port or 22, settings["port"], master_ip, open_ports or [])

    state.update(name, scheme=scheme, panel_port=settings["port"], base_path=settings["base_path"], cert_pin=pin)
    return PanelInfo(scheme=scheme, port=settings["port"], base_path=settings["base_path"], api_token=fresh,
                     stored_token=stored if not fresh else None, cert_pin=pin,
                     version=_facts(ssh).get("xui", ""), installed_now=installed_now)


# ---- steps --------------------------------------------------------------
def _facts(ssh: SSH) -> Dict[str, str]:
    out = ssh.run(
        '. /etc/os-release 2>/dev/null; echo "os=${ID:-unknown} ${VERSION_ID:-}"\n'
        'echo "arch=$(uname -m)"\n'
        'echo "init=$(ps -p 1 -o comm= 2>/dev/null | tr -d " ")"\n'
        f'[ -x {XUI} ] && echo "xui=$({XUI} -v 2>/dev/null | tail -1)"\n'
        "true"
    ).out
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def _install(ssh: SSH, version: str, server: ServerBase, tls) -> None:
    env = {"XUI_NONINTERACTIVE": "1", "XUI_SSL_MODE": "none", "XUI_VERSION": version,
           "DEBIAN_FRONTEND": "noninteractive"}
    if server.panel_port:
        env["XUI_PANEL_PORT"] = str(server.panel_port)
    if tls.mode == "ip":
        env["XUI_SSL_MODE"] = "ip"
        if not _is_ip(server.host):
            env["XUI_SSL_MODE"] = "none"  # host is a name, not an IP: fall back to self-signed
    elif tls.mode == "domain":
        env.update(XUI_SSL_MODE="domain", XUI_DOMAIN=tls.domain or "")
    if tls.email:
        env["XUI_ACME_EMAIL"] = tls.email
    ssh.run(
        "set -e\n"
        "if ! command -v curl >/dev/null || ! command -v openssl >/dev/null || ! command -v tar >/dev/null; then\n"
        "  if command -v apt-get >/dev/null; then apt-get update -qq && apt-get install -y -qq curl openssl tar ca-certificates >/dev/null\n"
        "  elif command -v dnf >/dev/null; then dnf install -y -q curl openssl tar ca-certificates\n"
        "  elif command -v yum >/dev/null; then yum install -y -q curl openssl tar ca-certificates\n"
        "  elif command -v apk >/dev/null; then apk add --no-cache curl openssl tar ca-certificates bash\n"
        "  fi\n"
        "fi\n"
        "tmp=$(mktemp)\n"
        'curl -fsSL --retry 3 --retry-delay 2 "https://raw.githubusercontent.com/MHSanaei/3x-ui/${XUI_VERSION}/install.sh" -o "$tmp"\n'
        'bash "$tmp" "${XUI_VERSION}" </dev/null\n'
        'rm -f "$tmp"\n'
        f"[ -x {XUI} ]\n"
        "systemctl enable x-ui >/dev/null 2>&1 || true\n"
        "systemctl is-active --quiet x-ui || systemctl restart x-ui\n",
        env=env, timeout=1800,
    )


def _bbr(ssh: SSH) -> None:
    ssh.run(
        'if [ "$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null)" != bbr ]; then\n'
        '  printf "net.core.default_qdisc=fq\\nnet.ipv4.tcp_congestion_control=bbr\\n" > /etc/sysctl.d/99-xui-fleet.conf\n'
        "  sysctl --system >/dev/null 2>&1 || true\n"
        "fi",
        check=False,
    )


def _cert_path(ssh: SSH) -> str:
    out = ssh.run(_ENV + f"{XUI} setting -getCert true 2>/dev/null | grep 'cert:' | head -1", check=False).out
    return out.split(":", 1)[1].strip() if ":" in out else ""


def _selfsigned(ssh: SSH, host: str) -> None:
    san = f"IP:{host}" if _is_ip(host) else f"DNS:{host}"
    ssh.run(
        "set -e\n"
        f"mkdir -p {CERT_DIR} && chmod 700 {CERT_DIR}\n"
        f"if [ ! -s {CERT_DIR}/panel.crt ]; then\n"
        "  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 "
        f'-subj "/CN=$FLEET_HOST" -addext "subjectAltName=$FLEET_SAN" '
        f"-keyout {CERT_DIR}/panel.key -out {CERT_DIR}/panel.crt 2>/dev/null\n"
        f"  chmod 600 {CERT_DIR}/panel.key\n"
        "fi\n"
        + _ENV +
        f"{XUI} cert -webCert {CERT_DIR}/panel.crt -webCertKey {CERT_DIR}/panel.key >/dev/null\n"
        "systemctl restart x-ui\n",
        env={"FLEET_HOST": host, "FLEET_SAN": san},
    )


def _settings(ssh: SSH) -> Dict[str, object]:
    out = ssh.run(_ENV + f"{XUI} setting -show true 2>/dev/null").out
    port = re.search(r"^port:\s*(\d+)", out, flags=re.M)
    base = re.search(r"^webBasePath:\s*(\S+)", out, flags=re.M)
    if not port:
        raise RuntimeError(f"{ssh.name}: не удалось прочитать настройки панели:\n{out[-400:]}")
    path = (base.group(1) if base else "/").strip()
    path = "/" + path.strip("/") + "/" if path.strip("/") else "/"
    return {"port": int(port.group(1)), "base_path": path}


def _mint_node_token(ssh: SSH, log: Callable[[str], None]) -> str:
    """node-admin (3x-ui >= 3.11) also lets the master update the node; older panels only know node-sync."""
    try:
        log(f"{ssh.name}: выпускаю API-токен (node-admin)")
        return _mint_token(ssh, NODE_TOKEN, "node-admin")
    except RuntimeError as exc:
        if "scope must be" not in str(exc):
            raise
    log(f"{ssh.name}: эта версия 3x-ui не знает node-admin — выпускаю токен node-sync. "
        f"Нода будет работать; обновить её: кнопка «Обновить 3x-ui»")
    return _mint_token(ssh, NODE_TOKEN, "node-sync")


def _mint_token(ssh: SSH, name: str, scope: str) -> str:
    out = ssh.run(_ENV + f"{XUI} setting -getApiToken -tokenName {name} -tokenScope {scope}").out
    match = re.search(r"apiToken:\s*(\S+)", out)
    if not match:
        raise RuntimeError(f"{ssh.name}: панель не выдала API-токен:\n{out[-400:]}")
    return match.group(1)


def _token_works(ssh: SSH, scheme: str, port: object, base_path: object, token: str) -> bool:
    # The token goes through stdin (curl -K -), never through the process list.
    script = (
        "curl -ksS -o /dev/null -w '%{http_code}' --max-time 10 -K - <<'XUIFLEET'\n"
        f'url = "{scheme}://127.0.0.1:{port}{base_path}panel/api/server/status"\n'
        f'header = "Authorization: Bearer {token}"\n'
        "XUIFLEET\n"
    )
    return ssh.run(script, check=False).out.strip() == "200"


def _wait_token(ssh: SSH, scheme: str, port: object, base_path: object, token: str) -> bool:
    for _ in range(20):
        if _token_works(ssh, scheme, port, base_path, token):
            return True
        ssh.run("sleep 2", check=False)
    return False


def _firewall(ssh: SSH, ssh_port: int, panel_port: object, master_ip: Optional[str], ports: list) -> None:
    rules = [f"ufw allow {int(ssh_port)}/tcp >/dev/null"]
    rules += [f"ufw allow {int(p)} >/dev/null" for p in ports]
    if master_ip and _is_ip(master_ip):
        rules.append(f"ufw allow from {master_ip} to any port {panel_port} proto tcp >/dev/null")
    else:
        rules.append(f"ufw allow {panel_port}/tcp >/dev/null")
    ssh.run(
        "set -e\n"
        "command -v ufw >/dev/null || { command -v apt-get >/dev/null && apt-get install -y -qq ufw >/dev/null; }\n"
        "command -v ufw >/dev/null || { echo 'ufw недоступен на этой ОС' >&2; exit 1; }\n"
        + "\n".join(rules) + "\n"
        "ufw --force enable >/dev/null\n"
    )


def upgrade(ssh: SSH, version: str) -> str:
    """Move an existing panel to the pinned release with the official updater."""
    ssh.run(
        "set -e\ntmp=$(mktemp)\n"
        'curl -fsSL --retry 3 "https://raw.githubusercontent.com/MHSanaei/3x-ui/${XUI_UPDATE_TAG}/update.sh" -o "$tmp"\n'
        'bash "$tmp" </dev/null\nrm -f "$tmp"\n'
        "systemctl is-active --quiet x-ui || systemctl restart x-ui\n",
        env={"XUI_NONINTERACTIVE": "1", "XUI_UPDATE_TAG": version}, timeout=1800,
    )
    return _facts(ssh).get("xui", "")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def pick_direct_port(ssh: SSH, wanted: int, keep: Optional[int] = None) -> int:
    """A port nothing listens on yet (TCP or UDP), so an existing server keeps all its inbounds."""
    if keep:
        return keep  # ours from an earlier run; xray listening on it is expected
    import random
    candidates = [wanted, 8443, 2083, 2087, 9443, 10443, 20443, 30443, 40443, 50443]
    candidates += random.sample(range(20000, 60000), 20)
    listing = ("{ ss -Htuln 2>/dev/null | awk '{print $5}'; netstat -tuln 2>/dev/null | awk 'NR>2{print $4}'; }"
               " | sed 's/.*://' | sort -un")
    # Connecting to the port catches listeners even where ss/netstat are missing.
    probe = " ".join(str(p) for p in candidates)
    out = ssh.run(listing + f"; for p in {probe}; do timeout 1 bash -c \"echo > /dev/tcp/127.0.0.1/$p\" 2>/dev/null && echo $p; done",
                  check=False).out
    busy = {int(p) for p in out.split() if p.isdigit()}
    for port in candidates:
        if port not in busy:
            return port
    raise RuntimeError(f"{ssh.name}: не нашёл свободного порта для подключения")


def open_port(ssh: SSH, port: int, proto: str = "tcp") -> bool:
    """Allow a port in ufw when ufw is active. Returns True if a rule was added."""
    out = ssh.run(
        f"if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q 'Status: active'; then "
        f"  ufw status | grep -qE '^{port}(/{proto})?[[:space:]]+ALLOW' && echo present || {{ ufw allow {port}/{proto} >/dev/null && echo added; }}; "
        f"else echo inactive; fi",
        check=False,
    ).out.strip()
    return out.endswith("added")


def reachable_from(ssh: SSH, host: str, port: int, tries: int = 6) -> int:
    """How many of `tries` TCP connects from this server to host:port succeed."""
    out = ssh.run(
        f"n=0; for i in $(seq {tries}); do timeout 3 bash -c 'echo > /dev/tcp/{host}/{port}' 2>/dev/null && n=$((n+1)); "
        f"sleep 0.3; done; echo $n",
        check=False,
    ).out.strip()
    return int(out) if out.isdigit() else 0


def resolve_public(ssh: SSH, domain: str) -> List[str]:
    """A records as the internet sees them (Cloudflare DoH), not the server's own cache."""
    out = ssh.run(
        f"curl -s -m 8 -H 'accept: application/dns-json' 'https://1.1.1.1/dns-query?name={domain}&type=A'",
        check=False).out
    try:
        answers = json.loads(out).get("Answer") or []
    except ValueError:
        return []
    return [a["data"] for a in answers if a.get("type") == 1]


def issue_domain_cert(ssh: SSH, domain: str, email: Optional[str] = None, extra: Optional[List[str]] = None) -> Dict[str, str]:
    """Let's Encrypt via acme.sh (standalone on :80); renewals restart x-ui by themselves.

    `extra` — more names in the same certificate (reserve domains); it is re-issued when the set changes."""
    d = f"/root/cert/{domain}"
    names = [domain] + [x for x in (extra or []) if x != domain]
    args = " ".join(f"-d {x}" for x in names)
    want = " ".join(sorted(names))
    ssh.run(
        "set -e\n"
        "[ -x ~/.acme.sh/acme.sh ] || curl -fsSL https://get.acme.sh | sh -s email=${FLEET_EMAIL:-admin@$FLEET_DOMAIN} >/dev/null\n"
        "command -v socat >/dev/null || { command -v apt-get >/dev/null && apt-get install -y -qq socat >/dev/null; }\n"
        "~/.acme.sh/acme.sh --set-default-ca --server letsencrypt >/dev/null\n"
        f"have=$(openssl x509 -in {d}/fullchain.pem -noout -ext subjectAltName 2>/dev/null | tr ',' '\\n' "
        "| sed -n 's/.*DNS:\\([^ ]*\\).*/\\1/p' | sort | tr '\\n' ' ' | sed 's/ $//')\n"
        # nginx serves the cover site on :80 once it is set up; then the challenge goes through it.
        f"if ss -Hltnp 'sport = :80' | grep -q nginx; then mkdir -p {ACME_WEBROOT}; MODE='-w {ACME_WEBROOT}'; "
        "else MODE='--standalone --httpport 80'; fi\n"
        "if ! ~/.acme.sh/acme.sh --list | awk 'NR>1{print $1}' | grep -qx \"$FLEET_DOMAIN\"; then\n"
        f"  ~/.acme.sh/acme.sh --issue {args} $MODE 2>&1 | tail -5\n"
        f"elif [ \"$have\" != \"{want}\" ]; then\n"
        f"  ~/.acme.sh/acme.sh --issue {args} $MODE --force 2>&1 | tail -5\n"
        "fi\n"
        f"mkdir -p {d}\n"
        f"~/.acme.sh/acme.sh --installcert -d \"$FLEET_DOMAIN\" --key-file {d}/privkey.pem --fullchain-file {d}/fullchain.pem "
        "--reloadcmd 'systemctl restart x-ui' >/dev/null\n"
        f"chmod 600 {d}/privkey.pem\n"
        f"openssl x509 -in {d}/fullchain.pem -noout -subject -enddate\n",
        env={"FLEET_DOMAIN": domain, "FLEET_EMAIL": email or ""}, timeout=300,
    )
    return {"cert": f"{d}/fullchain.pem", "key": f"{d}/privkey.pem"}


def panel_login(ssh: SSH, scheme: str, port: int, base_path: str) -> Dict[str, Any]:
    """Login saved by the installer, checked against the live panel (the owner may have changed it)."""
    base = f"{scheme}://127.0.0.1:{port}{base_path}"
    out = ssh.run(
        "f=/etc/x-ui/install-result.env; [ -f $f ] || { echo NOFILE; exit 0; }\n"
        ". $f\n"
        "printf 'U=%s\\nP=%s\\n' \"$XUI_USERNAME\" \"$XUI_PASSWORD\"\n"
        "jar=$(mktemp); trap 'rm -f $jar' EXIT\n"
        # 3x-ui v3 guards /login with a session CSRF token.
        f"tok=$(curl -ks -m 10 -c $jar -b $jar '{base}csrf-token' | sed -n 's/.*\"obj\":\"\\([^\"]*\\)\".*/\\1/p')\n"
        "enc() { python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.stdin.read(),safe=\"\"),end=\"\")'; }\n"
        # Credentials reach curl on stdin, never through the process list.
        "printf 'username=%s&password=%s' \"$(printf %s \"$XUI_USERNAME\" | enc)\" \"$(printf %s \"$XUI_PASSWORD\" | enc)\" | "
        f"curl -ks -m 10 -c $jar -b $jar -X POST --data-binary @- -H \"X-CSRF-Token: $tok\" "
        f"-H 'Content-Type: application/x-www-form-urlencoded' '{base}login' | grep -o '\"success\":[a-z]*' | sed 's/^/R=/'\n",
        check=False,
    ).out
    if "NOFILE" in out:
        return {"username": None, "password": None, "valid": False}
    kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line and line[:2] in ("U=", "P=", "R="))
    return {"username": kv.get("U") or None, "password": kv.get("P") or None, "valid": kv.get("R") == '"success":true'}


def set_panel_password(ssh: SSH, username: str) -> str:
    """New random password for the panel login; the installer's credentials file is kept in sync."""
    import secrets as _s
    import string
    password = "".join(_s.choice(string.ascii_letters + string.digits) for _ in range(16))
    ssh.run(
        _ENV +
        f"{XUI} setting -username \"$FLEET_U\" -password \"$FLEET_P\" >/dev/null\n"
        "f=/etc/x-ui/install-result.env; touch $f; chmod 600 $f\n"
        "grep -v -E '^XUI_(USERNAME|PASSWORD)=' $f > $f.tmp || true\n"
        "printf 'XUI_USERNAME=%q\\nXUI_PASSWORD=%q\\n' \"$FLEET_U\" \"$FLEET_P\" >> $f.tmp && mv $f.tmp $f && chmod 600 $f\n"
        "systemctl restart x-ui\n",
        env={"FLEET_U": username, "FLEET_P": password},
    )
    return password


DECOY_ROOT = "/var/www/xui-fleet"


ACME_WEBROOT = "/var/www/acme"


def ensure_decoy_site(ssh: SSH, domain: str, port: int, extra: Optional[List[str]] = None) -> bool:
    """The master's cover website: the domain opens an ordinary company site on 80/443, and the same site
    answers Reality probes on 127.0.0.1:port (self-steal). Certificates renew through it (webroot)."""
    return ensure_site(ssh, [domain] + [x for x in (extra or []) if x != domain], decoy_port=port, cert_domain=domain)


def ensure_site(ssh: SSH, domains: List[str], decoy_port: Optional[int] = None, cert_domain: Optional[str] = None,
                ip: str = "") -> bool:
    """nginx with a cover website. Every domain gets its own fictional brand (see site.py); a visit by IP
    gets one too. With `cert_domain` also HTTPS on 443 and the Reality target; without it — port 80 only
    (nodes, whose 443 belongs to Reality). Returns True if anything changed."""
    from . import site
    import base64 as b64
    roots = {d: f"{DECOY_ROOT}/{d}" for d in domains}
    pages = {f"{DECOY_ROOT}/_default": site.render(ip or (domains[0] if domains else "server"))}
    pages.update({roots[d]: site.render(d) for d in domains})
    root_map = "".join(f"    {d} {r};\n" for d, r in roots.items())
    tls_map = "".join(f"    {d} 1;\n" for d in domains)
    conf = (
        "map $host $xf_root {\n    hostnames;\n" f"    default {DECOY_ROOT}/_default;\n{root_map}" "}\n"
        "map $host $xf_tls {\n    default 0;\n" f"{tls_map if cert_domain else ''}" "}\n"
        "server {\n"
        "    listen 80 default_server;\n"
        "    server_name _;\n"
        "    server_tokens off;\n"
        f"    location ^~ /.well-known/acme-challenge/ {{ root {ACME_WEBROOT}; default_type text/plain; }}\n"
        "    location / {\n"
        "        if ($xf_tls) { return 301 https://$host$request_uri; }\n"
        "        root $xf_root;\n"
        "        try_files $uri /index.html;\n"
        "    }\n"
        "}\n"
    )
    if cert_domain:
        d = f"/root/cert/{cert_domain}"
        listens = "    listen 443 ssl http2 default_server;\n" + (f"    listen 127.0.0.1:{decoy_port} ssl http2;\n" if decoy_port else "")
        conf += (
            "server {\n" + listens +
            f"    server_name {' '.join(domains)};\n"
            f"    ssl_certificate {d}/fullchain.pem;\n"
            f"    ssl_certificate_key {d}/privkey.pem;\n"
            "    ssl_protocols TLSv1.2 TLSv1.3;\n"
            "    server_tokens off;\n"
            "    root $xf_root;\n"
            "    location / { try_files $uri /index.html; }\n"
            "}\n"
        )
    writes = "".join(
        f"mkdir -p {r}\necho {b64.b64encode(html.encode()).decode()} | base64 -d > {r}/index.html.new\n"
        for r, html in pages.items())
    files = " ".join(f"{r}/index.html" for r in pages) + " /etc/nginx/conf.d/xui-fleet-decoy.conf"
    renew = ""
    if cert_domain:
        d = f"/root/cert/{cert_domain}"
        renew = (
            # Renewals go through nginx (webroot) now that it owns :80, and reload it afterwards.
            "for c in ~/.acme.sh/*/*.conf; do case $c in *account.conf|*ca.conf|*.csr.conf) continue;; esac\n"
            f"  grep -q \"^Le_Webroot='no'\" $c && sed -i \"s|^Le_Webroot='no'|Le_Webroot='{ACME_WEBROOT}'|\" $c && changed=1\n"
            "done\n"
            f"if [ -x ~/.acme.sh/acme.sh ] && ~/.acme.sh/acme.sh --list | awk 'NR>1{{print $1}}' | grep -qx {cert_domain}; then\n"
            f"  ~/.acme.sh/acme.sh --installcert -d {cert_domain} --key-file {d}/privkey.pem --fullchain-file {d}/fullchain.pem "
            "--reloadcmd 'systemctl restart x-ui; systemctl reload nginx' >/dev/null\n"
            "fi\n"
        )
    out = ssh.run(
        "set -e\n"
        "if ! command -v nginx >/dev/null; then\n"
        # The stock package starts a default site on :80 and may report an error while doing so (Debian 13);
        # what matters is that nginx ends up installed.
        "  if command -v apt-get >/dev/null; then DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx-light >/dev/null 2>&1 "
        "|| DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx >/dev/null 2>&1 || dpkg --configure -a >/dev/null 2>&1 || true; "
        "  elif command -v dnf >/dev/null; then dnf install -y -q nginx || true; fi\n"
        "fi\n"
        "command -v nginx >/dev/null || { echo 'не удалось установить nginx' >&2; exit 1; }\n"
        "rm -f /etc/nginx/sites-enabled/default\n"
        f"mkdir -p {DECOY_ROOT} {ACME_WEBROOT} /etc/nginx/conf.d\n"
        f"rm -f {DECOY_ROOT}/index.html\n"  # the old single page
        + writes +
        f"cat > /etc/nginx/conf.d/xui-fleet-decoy.conf.new <<'XUIFLEETCONF'\n{conf}XUIFLEETCONF\n"
        "changed=0\n"
        f"for f in {files}; do\n"
        "  if ! cmp -s $f.new $f; then [ -f $f ] && cp -p $f $f.bak; mv $f.new $f; changed=1; else rm -f $f.new; fi\n"
        "done\n"
        # A config nginx rejects must never stay in place: the Reality entries depend on this nginx.
        "if ! nginx -t >/dev/null 2>&1; then nginx -t || true\n"
        f"  for f in {files}; do if [ -f $f.bak ]; then mv $f.bak $f; else rm -f $f; fi; done; exit 1; fi\n"
        f"for f in {files}; do rm -f $f.bak; done\n"
        "systemctl enable nginx >/dev/null 2>&1 || true\n"
        "if [ $changed = 1 ] || ! systemctl is-active --quiet nginx; then systemctl restart nginx; fi\n"
        + renew +
        "echo changed=$changed\n",
        timeout=600,
    ).out
    return "changed=1" in out


# ---- subscriptions only for chosen apps ------------------------------------
# nginx takes the public subscription port; 3x-ui moves to 127.0.0.1:<internal>. A request whose User-Agent is
# one of the allowed apps (or that comes from the master itself — the link check) goes on to 3x-ui as before:
# the same links, HWID limits, headers. Any other app gets a placeholder subscription that says it is not
# supported (two dummy entries and an announcement), a browser gets a short page.
SUBGATE_CONF = "/etc/nginx/conf.d/xui-fleet-subgate.conf"


def _app_names(apps: List[str]) -> str:
    from .config import APP_TITLES
    names = [APP_TITLES.get(a, a) for a in apps]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " или " + names[-1]


def unsupported_subscription(apps: List[str]) -> Tuple[str, Dict[str, str]]:
    """(body, headers) for an app that is not allowed: a valid subscription whose only entries are notices,
    so the app shows the reason in its server list instead of a bare error."""
    import base64 as b64
    from urllib.parse import quote
    names = _app_names(apps)
    remarks = ["⛔ Это приложение не поддерживается", f"Установите {names} и добавьте подписку заново"]
    lines = [f"vless://00000000-0000-0000-0000-000000000000@127.0.0.1:{k}?encryption=none&security=none&type=tcp#{quote(r)}"
             for k, r in enumerate(remarks, 1)]
    enc = lambda t: "base64:" + b64.b64encode(t.encode()).decode()
    headers = {
        "profile-title": enc(f"Только {names}"),
        "announce": enc(f"Эта подписка работает только в приложениях {names}. Установите одно из них и добавьте ссылку заново."),
        "profile-update-interval": "1",
    }
    return b64.b64encode("\n".join(lines).encode()).decode(), headers


def unsupported_page(apps: List[str], port: Optional[int] = None, title: str = "") -> str:
    """The page a person sees when they open their subscription link in a browser — usually the first thing they do
    with the link they were sent. Three steps: install the app, add the subscription (a button opens the app with
    this very address), switch it on; the address can be copied for an app on another device.
    It goes into nginx `return`: no double quotes, no $ and no backslashes; the page reads its own address from
    location in the browser, so nothing personal is put in by nginx. `port` is kept for older callers."""
    import html as _html
    sites = {"happ": ("Happ", "https://www.happ.su"), "incy": ("INCY", "https://incy.cc")}
    add = {"happ": ("Добавить в Happ", "happ://add/"), "incy": ("Добавить в INCY", "incy://import/")}
    names = _app_names(apps)
    brand = _html.escape(" ".join(str(title or "").split())[:60], quote=True).replace('"', "'").replace("$", "").replace("\\", "") or "VPN"
    install = "".join(f"<a class='b o' href='{sites[a][1]}' target='_blank' rel='noopener'>{sites[a][0]}</a>" for a in apps if a in sites)
    buttons = "".join(f"<a class='b' data-scheme='{add[a][1]}' href='#'>{add[a][0]}</a>" for a in apps if a in add)
    css = ("body{font:16px/1.55 system-ui,-apple-system,Segoe UI,sans-serif;max-width:560px;margin:0 auto;padding:28px 18px 40px;color:#14202f;background:#f3f6fa}"
           "h1{font-size:26px;margin:0 0 4px;letter-spacing:-.02em}.lead{color:#4a586d;margin:0 0 22px}"
           "ol{list-style:none;padding:0;margin:0;counter-reset:s}li{counter-increment:s;background:#fff;border:1px solid #dfe6ef;border-radius:14px;"
           "padding:16px 16px 16px 58px;margin:0 0 12px;position:relative}li:before{content:counter(s);position:absolute;left:16px;top:15px;width:28px;height:28px;"
           "border-radius:50%;background:#5f9600;color:#fff;font-weight:700;display:grid;place-items:center}li b{display:block;margin-bottom:4px}"
           ".b{display:inline-block;margin:8px 8px 0 0;padding:11px 16px;border-radius:10px;background:#5f9600;color:#fff;text-decoration:none;font-weight:600;"
           "border:0;font-size:15px;cursor:pointer}.b.o{background:#fff;color:#14202f;border:1px solid #c3cedd}.s{color:#687790;font-size:14px;margin-top:8px}"
           ".u{font:13px ui-monospace,monospace;word-break:break-all;color:#687790;background:#fff;border:1px dashed #c3cedd;border-radius:8px;padding:8px 10px;margin-top:10px}"
           "details{margin-top:18px;color:#4a586d;font-size:15px}summary{cursor:pointer;color:#14202f;font-weight:600}"
           "@media (prefers-color-scheme:dark){body{background:#0a101b;color:#e7eef8}.lead,details{color:#a7b4c8}summary{color:#e7eef8}"
           "li{background:#101929;border-color:#1b2840}li:before,.b{background:#c8f25a;color:#132003}.b.o{background:#101929;color:#e7eef8;border-color:#283a58}"
           ".u{background:#101929;border-color:#283a58;color:#a7b4c8}}")
    js = ("var u=location.href.split('#')[0];document.getElementById('u').textContent=u;"
          "var bs=document.querySelectorAll('[data-scheme]');for(var i=0;i<bs.length;i++){bs[i].href=bs[i].getAttribute('data-scheme')+u;}"
          "document.getElementById('c').onclick=function(){var b=this;function ok(){b.textContent='Скопировано';}"
          "if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(u).then(ok);}"
          "else{var t=document.createElement('textarea');t.value=u;document.body.appendChild(t);t.select();document.execCommand('copy');t.remove();ok();}};"
          "var a=navigator.userAgent,p=a.indexOf('iPhone')>=0||a.indexOf('iPad')>=0?'iPhone':a.indexOf('Android')>=0?'Android':"
          "a.indexOf('Windows')>=0?'Windows':a.indexOf('Mac')>=0?'Mac':'';if(p){document.getElementById('p').textContent='У вас '+p+' — приложение есть и для него.';}")
    page = ("<!doctype html><html lang='ru'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<meta name='robots' content='noindex'><title>{brand} — подключение</title><style>{css}</style></head><body>"
            f"<h1>{brand}</h1><p class='lead'>Это ваша личная ссылка для подключения. Настройка займёт минуту.</p><ol>"
            f"<li><b>Установите приложение</b>Подписка работает в {names}.<br>{install}<div class='s' id='p'>Есть для iPhone, Android, Windows и Mac.</div></li>"
            f"<li><b>Добавьте подписку</b>Нажмите кнопку на этом же устройстве — приложение откроется и добавит подписку само.<br>{buttons}"
            "<div class='s'>Приложение на другом устройстве или кнопка не сработала? Скопируйте ссылку и в приложении нажмите «+» → «Вставить из буфера».</div>"
            "<button class='b o' id='c' type='button'>Скопировать ссылку</button><div class='u' id='u'></div></li>"
            "<li><b>Включите VPN</b>Большой кнопкой в приложении. Подключения вверху списка — самые надёжные; если одно не открывает сайты, выберите "
            "следующее той же страны. Российские сайты и приложения банков работают напрямую, без VPN.</li></ol>"
            "<details><summary>Подписка в приложении перестала обновляться?</summary><p>Например, основной адрес заблокировали. Откройте эту "
            "страницу на телефоне и нажмите «Добавить» ещё раз — подписка добавится по этому адресу. Если в приложении после этого две подписки "
            "с одинаковым названием, ту, что не обновляется, можно удалить.</p></details>"
            f"<script>{js}</script></body></html>")
    assert '"' not in page and "$" not in page and "\\" not in page, "страница идёт в nginx return"
    return page


def sub_gate_conf(public_port: int, internal_port: int, cert: str, key: str, apps: List[str], local_ips: List[str],
                  ipv6: bool = True, domains: Optional[List[str]] = None, moved_from: str = "", title: str = "") -> str:
    """`domains`: the master's subscription domains, main first. With two or more, every subscription answer
    carries the user's own address on the other domain — `fallback-url` (Happ switches to it by itself when
    the main one stops answering) and `profile-web-page-url` (a button in the app, INCY included, that opens
    a page on the other domain with «add to the app» buttons).
    `moved_from`: the domain the master moved away from («switch to reserve»). Answers fetched through it carry
    `new-url` with the same address on the main domain: INCY then moves the saved subscription there itself
    (Happ honours it only with a Provider ID). Other domains never get it, so a copy added from the reserve
    page is never pulled back to a domain that may be blocked."""
    from .config import APPS
    import re as _re
    tls = bool(cert and key)
    # No backslashes in nginx regexes (\b is easy to break by quoting): a word boundary written out.
    ua = "".join(f"    ~*(^|[^a-z0-9]){_re.escape(a)}([^a-z0-9]|$) 1;\n" for a in apps if a in APPS)
    local = "".join(f"    {ip} 1;\n" for ip in dict.fromkeys(["127.0.0.1", "::1"] + list(local_ips)) if ip)
    body, headers = unsupported_subscription(apps)
    names = [d for d in dict.fromkeys(domains or []) if d]
    page = unsupported_page(apps, port=public_port if tls else None, title=title)
    scheme = "https" if tls else "http"
    # Each domain points at the next one (main → reserve → … → main).
    alt = "".join(f"    {d} {names[(i + 1) % len(names)]};\n" for i, d in enumerate(names)) if len(names) > 1 else ""
    # Only a plain path (letters, digits, / _ -) is echoed back: never a query or anything HTML could read.
    path_map = "map $request_uri $xf_sub_path {\n    default \"\";\n    \"~^(?<xfp>/[A-Za-z0-9/_-]{1,300})$\" $xfp;\n}\n"
    alt_maps = "" if not alt else (
        "map $host $xf_sub_alt {\n    hostnames;\n    default \"\";\n" + alt + "}\n"
        f"map $xf_sub_alt $xf_sub_alt_base {{\n    \"\" \"\";\n    default \"{scheme}://$xf_sub_alt:{public_port}\";\n}}\n"
        "map $xf_sub_alt_base$xf_sub_path $xf_sub_fallback {\n    default \"\";\n    \"~^https?://[^/]+/.\" $xf_sub_alt_base$xf_sub_path;\n}\n")
    fallback = ("        add_header fallback-url $xf_sub_fallback always;\n"
                "        add_header profile-web-page-url $xf_sub_fallback always;\n") if alt else ""
    moved = moved_from if names and moved_from and moved_from != names[0] and moved_from in names else ""
    if moved:
        alt_maps += (
            f"map $host $xf_sub_moved {{\n    hostnames;\n    default \"\";\n    {moved} \"{scheme}://{names[0]}:{public_port}\";\n}}\n"
            "map $xf_sub_moved$xf_sub_path $xf_sub_new_url {\n    default \"\";\n    \"~^https?://[^/]+/.\" $xf_sub_moved$xf_sub_path;\n}\n")
        fallback += "        add_header new-url $xf_sub_new_url always;\n"
    ssl = " ssl" if tls else ""
    listen = f"    listen {public_port}{ssl};\n" + (f"    listen [::]:{public_port}{ssl};\n" if ipv6 else "")
    hdrs = "".join(f"        add_header {k} \"{v}\" always;\n" for k, v in headers.items())
    upstream = f"{'https' if tls else 'http'}://127.0.0.1:{internal_port}"
    return (
        "# xui-fleet: subscriptions only for " + ", ".join(apps) + " (other apps get a notice)\n"
        "map $http_user_agent $xf_sub_app {\n    default 0;\n" + ua + "}\n"
        "map $remote_addr $xf_sub_local {\n    default 0;\n" + local + "}\n"
        "map $http_accept $xf_sub_page {\n    default 0;\n    ~*text/html 1;\n}\n"
        "map $xf_sub_app$xf_sub_local$xf_sub_page $xf_sub_deny {\n    default \"\";\n    000 app;\n    001 page;\n}\n"
        + path_map + alt_maps +
        "server {\n" + listen +
        "    server_name _;\n"
        "    server_tokens off;\n"
        + (f"    ssl_certificate {cert};\n    ssl_certificate_key {key};\n    ssl_protocols TLSv1.2 TLSv1.3;\n" if tls else "") +
        "    error_page 418 = @xf_sub_app_denied;\n"
        "    error_page 419 = @xf_sub_page_denied;\n"
        "    location / {\n"
        "        if ($xf_sub_deny = app) { return 418; }\n"
        "        if ($xf_sub_deny = page) { return 419; }\n"
        f"        proxy_pass {upstream};\n"
        + ("        proxy_ssl_server_name on;\n        proxy_ssl_name $host;\n        proxy_ssl_verify off;\n" if tls else "") +
        "        proxy_set_header Host $http_host;\n"
        "        proxy_set_header X-Real-IP $remote_addr;\n"
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        f"        proxy_set_header X-Forwarded-Proto {'https' if tls else 'http'};\n"
        "        proxy_set_header X-Forwarded-Host $http_host;\n"
        "        proxy_read_timeout 90s;\n"
        + fallback +
        "    }\n"
        "    location @xf_sub_app_denied {\n"
        "        default_type text/plain;\n" + hdrs +
        f"        return 200 \"{body}\";\n"
        "    }\n"
        "    location @xf_sub_page_denied {\n"
        "        default_type text/html;\n"
        "        charset utf-8;\n"
        f"        return 200 \"{page}\";\n"
        "    }\n"
        "}\n"
    )


def sub_gate_facts(ssh: SSH, start: int = 12096) -> Dict[str, Any]:
    """The master's own addresses (the link check fetches the subscription from there), whether it has IPv6,
    and a free local port for 3x-ui behind nginx."""
    out = ssh.run(
        "hostname -I 2>/dev/null | tr ' ' '\\n'; echo ---; [ -f /proc/net/if_inet6 ] && echo v6; echo ---; "
        "ss -Hltn | awk '{print $4}' | sed 's/.*://'", check=False).out
    parts = (out + "\n---\n---").split("---")
    ips = [x.strip() for x in parts[0].splitlines() if x.strip()]
    taken = {int(p) for p in parts[2].split() if p.strip().isdigit()}
    port = next(p for p in range(start, start + 2000) if p not in taken)
    return {"ips": ips, "ipv6": "v6" in parts[1], "free": port}


def write_sub_gate(ssh: SSH, conf: str) -> bool:
    """Put the gate config in place if it differs, checked by nginx -t (a rejected config is taken back).
    Does not reload: the caller frees the public port first. Returns True if the file changed."""
    out = ssh.run(
        "set -e\nmkdir -p /etc/nginx/conf.d\n"
        f"cat > {SUBGATE_CONF}.new <<'XUIFLEETCONF'\n{conf}XUIFLEETCONF\n"
        f"if cmp -s {SUBGATE_CONF}.new {SUBGATE_CONF}; then rm -f {SUBGATE_CONF}.new; echo same; exit 0; fi\n"
        f"[ -f {SUBGATE_CONF} ] && cp -p {SUBGATE_CONF} {SUBGATE_CONF}.bak || true\n"
        f"mv {SUBGATE_CONF}.new {SUBGATE_CONF}\n"
        f"if ! nginx -t >/dev/null 2>&1; then nginx -t || true\n"
        f"  if [ -f {SUBGATE_CONF}.bak ]; then mv {SUBGATE_CONF}.bak {SUBGATE_CONF}; else rm -f {SUBGATE_CONF}; fi; exit 1; fi\n"
        f"rm -f {SUBGATE_CONF}.bak\necho changed\n", timeout=60).out
    return "changed" in out


def reload_nginx_on(ssh: SSH, port: int) -> bool:
    """Reload nginx and tell whether it now listens on `port` (a reload that cannot bind only logs an error)."""
    out = ssh.run(
        "systemctl reload nginx 2>/dev/null || systemctl restart nginx; "
        f"for i in 1 2 3 4 5 6 7 8 9 10; do ss -Hltnp 'sport = :{port}' | grep -q nginx && {{ echo listening; exit 0; }}; sleep 1; done; "
        "echo missing", check=False, timeout=60).out
    return "listening" in out


def remove_sub_gate(ssh: SSH) -> bool:
    out = ssh.run(f"if [ -f {SUBGATE_CONF} ]; then rm -f {SUBGATE_CONF} {SUBGATE_CONF}.bak; "
                  "(systemctl reload nginx 2>/dev/null || systemctl restart nginx); echo removed; fi", check=False, timeout=60).out
    return "removed" in out


def node_facts(ssh: SSH) -> Dict[str, Any]:
    """Ports in use (TCP/UDP, any process) and the panel certificate, for the extra entries."""
    import base64
    import hashlib
    listing = ssh.run("ss -Htuln | awk '{print $1, $5}'", check=False).out
    busy: Dict[str, set] = {"tcp": set(), "udp": set()}
    for line in listing.splitlines():
        proto, _, addr = line.partition(" ")
        port = addr.rsplit(":", 1)[-1]
        if port.isdigit() and proto in busy and not addr.startswith(("127.", "[::1]")):
            busy[proto].add(int(port))
    paths = ssh.run(_ENV + f"{XUI} setting -getCert true 2>/dev/null | sed -n 's/^cert: //p;s/^key: //p'", check=False).out.split()
    cert = ssh.run(f"cat {paths[0]}", check=False).out if len(paths) == 2 else ""
    key = ssh.run(f"cat {paths[1]}", check=False).out if len(paths) == 2 else ""
    # The pin is over the leaf, i.e. the first PEM block of the chain.
    leaf = cert.split("-----END CERTIFICATE-----")[0]
    body = "".join(l for l in leaf.splitlines() if l and not l.startswith("-----"))
    pin = hashlib.sha256(base64.b64decode(body)).hexdigest() if body else ""
    return {"busy_tcp": busy["tcp"], "busy_udp": busy["udp"], "cert_pem": cert, "key_pem": key, "cert_pin_hex": pin}


# ---- Reality "neighbor": borrow the TLS handshake from a real site in the node's own /24 ----
# A Reality entry that claims www.microsoft.com on a random VPS IP is easy to spot (the SNI does not
# belong to that network). A site hosted next door — same provider, same subnet, its name resolving
# to that address — looks like ordinary traffic to that hosting. Field reports from Russia (autumn 2026)
# single this out, together with a firefox fingerprint, as what keeps Reality from being "frozen".
NEIGHBOR_SCRIPT = r"""
SELF=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')
[ -n "$SELF" ] || exit 0
probe() {
  ip=$1
  [ "$ip" = "$SELF" ] && return
  raw=$(echo | timeout 4 openssl s_client -connect "$ip:443" -tls1_3 -alpn h2 2>/dev/null | tr -d '\000') || true
  echo "$raw" | grep -q "BEGIN CERTIFICATE" || return
  names=$(echo "$raw" | openssl x509 -noout -ext subjectAltName 2>/dev/null | tr ',' '\n' | sed -n 's/.*DNS:\([^ ]*\).*/\1/p' | grep -v '^\*' | head -5)
  for n in $names; do
    getent ahostsv4 "$n" 2>/dev/null | awk '{print $1}' | grep -qx "$ip" || continue
    out=$(echo | timeout 4 openssl s_client -connect "$ip:443" -servername "$n" -tls1_3 -alpn h2 -verify_return_error 2>&1 | tr -d '\000') || continue
    echo "$out" | grep -q "ALPN protocol: h2" || continue
    echo "$out" | grep -q "X25519" || continue
    echo "$ip $n"; return
  done
}
export -f probe; export SELF
if [ -n "$CHECK_IP" ]; then probe "$CHECK_IP"; exit 0; fi
seq 1 254 | sed "s/^/${SELF%.*}./" | xargs -P 48 -I{} bash -c 'probe {}' 2>/dev/null
"""

# Names that would make the borrowed handshake worse than a generic one: big brands (an obvious
# mismatch with a VPS subnet, and some of them are throttled in Russia), wildcard-DNS services and
# names that look like someone else's proxy (those get blocked by name).
_NEIGHBOR_BAD = ("google", "cloudflare", "github", "microsoft", "apple", "amazon", "aws", "akamai", "facebook",
                 "instagram", "whatsapp", "telegram", "yandex", "vk.com", "sslip.io", "nip.io", "localhost",
                 "vpn", "proxy", "xray", "v2ray", "tg.", "tun", "warp", "party", "hysteria", "reality")


def _neighbor_rank(candidates: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Plain-looking names first; among them, the domain hosted most often in the subnet (a real
    hosting customer rather than a one-off box), then the lowest address, so the choice is stable."""
    ok = [(ip, n) for ip, n in candidates
          if not any(b in n.lower() for b in _NEIGHBOR_BAD) and not n.replace(".", "").replace("-", "").isdigit()]
    count: Dict[str, int] = {}
    for _ip, n in ok:
        count[".".join(n.split(".")[-2:])] = count.get(".".join(n.split(".")[-2:]), 0) + 1
    return sorted(ok, key=lambda c: (-count[".".join(c[1].split(".")[-2:])], tuple(int(x) for x in c[0].split("."))))


def find_reality_neighbor(ssh: SSH, exclude: Tuple[str, ...] = ()) -> Optional[Tuple[str, str]]:
    """(ip, server name) of the best neighbor site, or None when the subnet has none. `exclude`: IPs not to
    pick (changing the masking asks for a different site)."""
    out = ssh.run(NEIGHBOR_SCRIPT, check=False, timeout=240).out
    found = [tuple(line.split()[:2]) for line in out.splitlines() if len(line.split()) >= 2]
    ranked = [r for r in _neighbor_rank(found) if r[0] not in exclude]  # type: ignore[arg-type]
    return ranked[0] if ranked else None


def neighbor_alive(ssh: SSH, ip: str, name: str) -> bool:
    out = ssh.run(NEIGHBOR_SCRIPT, check=False, timeout=30, env={"CHECK_IP": ip}).out.split()
    return out[1:2] == [name]



# Cloudflare WARP entry points (same service, different routes). From some networks one of them is
# 50x slower than another, so each server measures them with its own WARP identity and keeps the best.
WARP_ENDPOINTS = ["188.114.96.1:2408", "162.159.192.1:2408", "162.159.192.1:500", "188.114.97.1:2408",
                  "162.159.195.1:2408", "162.159.192.1:1701"]


# Cloudflare points of presence in Russia. WARP traffic that leaves Cloudflare there is Russian traffic: YouTube
# answers it with a bot check (black video in the app), Telegram media stall — the same limits as without a VPN.
# From a server in Moscow the fastest entry point is usually DME, so the choice must skip these.
WARP_RU_COLOS = {"DME", "SVO", "VKO", "LED", "SVX", "KZN", "OVB", "KJA", "KHV", "VVO", "KUF", "ROV", "AER", "UFA", "KRR",
                 "CEK", "PEE", "IKT", "TJM", "OMS", "MRV", "GOJ", "VOZ", "SGC", "KGD", "MMK", "ARH", "NBC", "STW"}


def pick_warp_endpoint(ssh: SSH, warp: Dict[str, Any], candidates: Optional[List[str]] = None
                       ) -> Tuple[str, Dict[str, float], Dict[str, str]]:
    """Fastest WARP endpoint from this server whose traffic leaves Cloudflare outside Russia: median of a few real
    HTTPS requests through each, and Cloudflare's own answer where the traffic went (colo).
    Returns (endpoint or "", {endpoint: seconds}, {endpoint: colo}); a candidate that fails scores 99.
    Only when every working entry lands in Russia is one of them taken (better than no WARP at all)."""
    import copy
    import json as _json
    import statistics
    scores: Dict[str, float] = {}
    colos: Dict[str, str] = {}
    for ep in candidates or WARP_ENDPOINTS:
        o = copy.deepcopy(warp)
        o["tag"] = "t"
        o.setdefault("settings", {}).setdefault("peers", [{}])[0]["endpoint"] = ep
        cfg = {"log": {"loglevel": "none"}, "inbounds": [{"listen": "127.0.0.1", "port": 39996, "protocol": "socks"}], "outbounds": [o]}
        out = ssh.run(
            "cat > /tmp/xf-warp.json <<'XFEOF'\n" + _json.dumps(cfg) + "\nXFEOF\n"
            "cd /usr/local/x-ui/bin && (./xray-linux-amd64 run -c /tmp/xf-warp.json >/dev/null 2>&1 & echo $! > /tmp/xf-warp.pid); sleep 2\n"
            "for i in 1 2 3; do curl -s -m 6 -x socks5h://127.0.0.1:39996 -o /dev/null -w '%{http_code}:%{time_total} ' "
            "https://www.gstatic.com/generate_204; done\n"
            "curl -s -m 6 -x socks5h://127.0.0.1:39996 https://www.cloudflare.com/cdn-cgi/trace | grep '^colo='\n"
            "kill $(cat /tmp/xf-warp.pid) 2>/dev/null; rm -f /tmp/xf-warp.json /tmp/xf-warp.pid\n", check=False, timeout=60).out.split()
        times = [float(t) for code, _, t in (x.partition(":") for x in out if not x.startswith("colo=")) if code == "204"]
        scores[ep] = statistics.median(times) if len(times) >= 2 else 99.0
        colos[ep] = next((x[5:] for x in out if x.startswith("colo=")), "")
    working = [ep for ep in scores if scores[ep] < 99]
    abroad = [ep for ep in working if colos.get(ep) not in WARP_RU_COLOS]
    pool = abroad or working
    best = min(pool, key=scores.get) if pool else ""
    return best, scores, colos


def warp_added_ms(ssh: SSH, warp: Dict[str, Any], endpoint: str) -> Optional[int]:
    """How much WARP adds to every round trip from this server, in ms: an HTTPS handshake (TCP + TLS, two round
    trips) to the same site through WARP and directly, medians of three; half the difference. None if either
    side failed."""
    import copy
    import json as _json
    import statistics
    o = copy.deepcopy(warp)
    o["tag"] = "t"
    if endpoint:
        o.setdefault("settings", {}).setdefault("peers", [{}])[0]["endpoint"] = endpoint
    cfg = {"log": {"loglevel": "none"}, "inbounds": [{"listen": "127.0.0.1", "port": 39995, "protocol": "socks"}], "outbounds": [o]}
    probe = ("for i in 1 2 3 4; do curl -s -m 6 {via} -o /dev/null -w '{tag}:%{{http_code}}:%{{time_appconnect}} ' "
             "https://www.gstatic.com/generate_204; done\n")
    out = ssh.run(
        "cat > /tmp/xf-warp2.json <<'XFEOF'\n" + _json.dumps(cfg) + "\nXFEOF\n"
        "cd /usr/local/x-ui/bin && (./xray-linux-amd64 run -c /tmp/xf-warp2.json >/dev/null 2>&1 & echo $! > /tmp/xf-warp2.pid); sleep 2\n"
        + probe.format(via="-x socks5h://127.0.0.1:39995", tag="w") + probe.format(via="", tag="d") +
        "kill $(cat /tmp/xf-warp2.pid) 2>/dev/null; rm -f /tmp/xf-warp2.json /tmp/xf-warp2.pid\n", check=False, timeout=90).out.split()
    times: Dict[str, List[float]] = {"w": [], "d": []}
    for item in out:
        parts = item.split(":")
        if len(parts) == 3 and parts[1] == "204" and parts[0] in times:
            times[parts[0]].append(float(parts[2]))
    # the first request through a fresh tunnel pays for the WireGuard handshake: leave it out
    w, d = times["w"][1:], times["d"]
    if len(w) < 2 or len(d) < 2:
        return None
    return max(0, round((statistics.median(w) - statistics.median(d)) / 2 * 1000))


# ---- removal ------------------------------------------------------------
_DOMAIN = re.compile(r"^[A-Za-z0-9.-]{1,253}$")

UNINSTALL = r"""
set +o pipefail
say() { echo "$*"; }
# 3x-ui: the same steps as its own `x-ui uninstall`, without the question.
if systemctl list-unit-files x-ui.service >/dev/null 2>&1 || [ -e /etc/systemd/system/x-ui.service ]; then
  systemctl stop x-ui >/dev/null 2>&1; systemctl disable x-ui >/dev/null 2>&1
  rm -f /etc/systemd/system/x-ui.service; rm -rf /etc/systemd/system/x-ui.service.d
  systemctl daemon-reload; systemctl reset-failed >/dev/null 2>&1
  say "3x-ui остановлен и убран из автозапуска"
fi
for p in /etc/x-ui /usr/local/x-ui /etc/default/x-ui /usr/bin/x-ui /var/log/x-ui \
         /etc/fail2ban/filter.d/3x-ipl.conf /etc/fail2ban/action.d/3x-ipl.conf /etc/fail2ban/jail.d/3x-ipl.conf; do
  [ -e "$p" ] && rm -rf -- "$p" && say "удалено: $p"
done
command -v fail2ban-client >/dev/null && fail2ban-client reload >/dev/null 2>&1
# Cover site: only our own config and pages; nginx itself stays (other sites may use it).
if [ -e /etc/nginx/conf.d/xui-fleet-decoy.conf ] || [ -d /var/www/xui-fleet ]; then
  rm -f /etc/nginx/conf.d/xui-fleet-decoy.conf; rm -rf /var/www/xui-fleet /var/www/acme
  command -v nginx >/dev/null && nginx -t >/dev/null 2>&1 && systemctl reload nginx >/dev/null 2>&1
  say "сайт-витрина удалён (nginx оставлен)"
fi
# Certificates of this server's domains.
for d in $FLEET_DOMAINS; do
  [ -x ~/.acme.sh/acme.sh ] && ~/.acme.sh/acme.sh --remove -d "$d" >/dev/null 2>&1
  rm -rf ~/.acme.sh/"$d" ~/.acme.sh/"${d}_ecc" /root/cert/"$d"
  say "сертификат $d удалён"
done
# The certificate the 3x-ui installer issued for the server's IP ("ip" mode) lives here.
if [ -d /root/cert/ip ]; then rm -rf /root/cert/ip; say "сертификат 3x-ui для IP удалён"; fi
rmdir /root/cert 2>/dev/null
# Firewall: rules for the ports of the removed connections and the panel; SSH and everything else stay.
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q 'Status: active'; then
  for p in $FLEET_PORTS; do
    [ "$p" = "$FLEET_SSH_PORT" ] && continue
    while n=$(ufw status numbered | grep -E "^\[ *[0-9]+\] +${p}(/(tcp|udp))?( \(v6\))? +ALLOW" | head -1 | sed -E 's/^\[ *([0-9]+)\].*/\1/'); [ -n "$n" ]; do
      yes | ufw delete "$n" >/dev/null 2>&1 || break
    done
  done
  say "файрвол: закрыты порты $FLEET_PORTS"
fi
rm -f /etc/sysctl.d/99-xui-fleet.conf
if [ "$FLEET_SYNC" = 1 ] && [ -d /root/.xui-fleet-sync ]; then rm -rf /root/.xui-fleet-sync; say "копия синхронизации удалена"; fi
say "готово"
"""


def uninstall(ssh: SSH, ports: List[int], domains: List[str], ssh_port: int, sync_data: bool = False) -> List[str]:
    """Remove 3x-ui and what xui-fleet put on the server (cover site, its certificates, firewall rules of its
    ports, BBR file). SSH access, other sites, nginx itself and unrelated firewall rules stay. Returns the log."""
    clean_ports = sorted({int(p) for p in ports if 0 < int(p) < 65536 and int(p) != int(ssh_port)})
    clean_domains = sorted({d for d in domains if d and _DOMAIN.match(d)})
    out = ssh.run(UNINSTALL, check=False, timeout=300, env={
        "FLEET_PORTS": " ".join(map(str, clean_ports)), "FLEET_DOMAINS": " ".join(clean_domains),
        "FLEET_SSH_PORT": str(int(ssh_port)), "FLEET_SYNC": "1" if sync_data else "0"}).out
    return [line for line in out.splitlines() if line.strip()]
