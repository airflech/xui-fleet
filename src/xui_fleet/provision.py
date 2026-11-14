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


def find_reality_neighbor(ssh: SSH) -> Optional[Tuple[str, str]]:
    """(ip, server name) of the best neighbor site, or None when the subnet has none."""
    out = ssh.run(NEIGHBOR_SCRIPT, check=False, timeout=240).out
    found = [tuple(line.split()[:2]) for line in out.splitlines() if len(line.split()) >= 2]
    ranked = _neighbor_rank(found)  # type: ignore[arg-type]
    return ranked[0] if ranked else None


def neighbor_alive(ssh: SSH, ip: str, name: str) -> bool:
    out = ssh.run(NEIGHBOR_SCRIPT, check=False, timeout=30, env={"CHECK_IP": ip}).out.split()
    return out[1:2] == [name]



# Cloudflare WARP entry points (same service, different routes). From some networks one of them is
# 50x slower than another, so each server measures them with its own WARP identity and keeps the best.
WARP_ENDPOINTS = ["188.114.96.1:2408", "162.159.192.1:2408", "162.159.192.1:500", "188.114.97.1:2408",
                  "162.159.195.1:2408", "162.159.192.1:1701"]


def pick_warp_endpoint(ssh: SSH, warp: Dict[str, Any], candidates: Optional[List[str]] = None) -> Tuple[str, Dict[str, float]]:
    """Fastest WARP endpoint from this server: median of a few real HTTPS requests through each.
    Returns (endpoint or "", {endpoint: seconds}); a candidate that fails scores 99."""
    import copy
    import json as _json
    import statistics
    scores: Dict[str, float] = {}
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
            "kill $(cat /tmp/xf-warp.pid) 2>/dev/null; rm -f /tmp/xf-warp.json /tmp/xf-warp.pid\n", check=False, timeout=60).out.split()
        times = [float(t) for code, _, t in (x.partition(":") for x in out) if code == "204"]
        scores[ep] = statistics.median(times) if len(times) >= 2 else 99.0
    best = min(scores, key=scores.get) if scores else ""
    return (best if best and scores[best] < 99 else ""), scores
