"""헤츠너에 에스크로 서버를 새로 만들고 설치까지 (작업 환경에서 실행 — 서버로 SSH 가 막혀 있어도 됨).

  HCLOUD_TOKEN=... python deploy/provision.py create      # 서버 생성 + 부트 통로 열림 기다림 → 코드 올리고 설치 → 확인
  HCLOUD_TOKEN=... python deploy/provision.py status      # 서버·관리 페이지 상태

방식: 서버 생성 데이터(cloud-init)에 1회용 토큰을 넣은 작은 업로드 서버(boot.py)를 심는다.
  서버가 Caddy(HTTPS, <IP>.sslip.io)를 띄우면 → 여기서 코드 묶음(git archive)을 HTTPS 로 올림 → install.sh 실행 →
  설치 로그를 받아 봄 → finish 로 통로를 닫는다 (Caddy 에서 /_boot 경로 삭제 + boot 서비스 끔).
비밀값: 1회용 토큰은 deploy/.provision.json (gitignore) 에만. 서버 root 비밀번호는 헤츠너가 메일로 보냄 (SSH 키 안 붙임).
"""
from __future__ import annotations

import io
import json
import os
import secrets
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
STATE = Path(__file__).with_name(".provision.json")
API = "https://api.hetzner.cloud/v1"
NAME = os.environ.get("SERVER_NAME", "escrow")
TYPE = os.environ.get("SERVER_TYPE", "cx23")
LOCATIONS = os.environ.get("SERVER_LOCATIONS", "fsn1,nbg1,hel1").split(",")

BOOT_PY = r'''
import json, os, subprocess, tarfile, io, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
TOKEN = open("/root/boot.token").read().strip()
APP = "/opt/escrow"
LOG = "/var/log/escrow-install.log"
DOMAIN = open("/root/boot.domain").read().strip()
ENV_KEYS = {"ADMIN_IDS", "PAY_ADDRESS", "BOT_NAME", "SUPPORT", "BOT_TOKEN", "TRONGRID_KEY", "MENU_PHOTO", "BOT_USERNAME"}

def set_env(values):
    p = APP + "/.env"
    lines = open(p).read().splitlines() if os.path.exists(p) else []
    for k, v in values.items():
        if k not in ENV_KEYS or "\n" in str(v):
            continue
        line = f"{k}={v}"
        for i, l in enumerate(lines):
            if l.startswith(k + "="):
                lines[i] = line
                break
        else:
            lines.append(line)
    open(p, "w").write("\n".join(lines) + "\n")
    os.chmod(p, 0o600)
    subprocess.run(["chown", "escrow:", p])

def install():
    with open(LOG, "a") as f:
        r = subprocess.run(["bash", APP + "/deploy/install.sh", DOMAIN], stdout=f, stderr=subprocess.STDOUT,
                           env={**os.environ, "BOOT_ROUTE": "1", "DEBIAN_FRONTEND": "noninteractive"})
        f.write(f"\n__EXIT__ {r.returncode}\n")

class H(BaseHTTPRequestHandler):
    def _ok(self, code, body):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _auth(self):
        if self.headers.get("X-Boot-Token") != TOKEN:
            self._ok(403, "no"); return False
        return True
    def do_GET(self):
        if self.path == "/ping":
            return self._ok(200, "ok")
        if not self._auth(): return
        if self.path == "/log":
            data = open(LOG).read()[-6000:] if os.path.exists(LOG) else ""
            return self._ok(200, data)
        if self.path == "/svc":
            out = subprocess.run(["systemctl", "is-active", "escrow-bot", "escrow-watcher", "escrow-admin", "postgresql", "caddy"],
                                 capture_output=True, text=True).stdout
            return self._ok(200, out)
        self._ok(404, "?")
    def do_POST(self):
        if not self._auth(): return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/upload":
            os.makedirs(APP, exist_ok=True)
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as t:
                t.extractall(APP, filter="data")
            subprocess.run(["chmod", "+x", APP + "/deploy/install.sh", APP + "/deploy/update.sh"])
            return self._ok(200, "uploaded")
        if self.path == "/install":
            threading.Thread(target=install, daemon=True).start()
            return self._ok(200, "started")
        if self.path == "/env":
            set_env(json.loads(body or b"{}"))
            subprocess.run(["systemctl", "restart", "escrow-watcher", "escrow-admin"])
            if any(l.startswith("BOT_TOKEN=") and len(l) > 10 for l in open(APP + "/.env").read().splitlines()):
                subprocess.run(["systemctl", "restart", "escrow-bot"])
            return self._ok(200, "env set")
        if self.path == "/finish":
            subprocess.run(["sed", "-i", "/# boot/d", "/etc/caddy/Caddyfile"])
            subprocess.run(["systemctl", "reload", "caddy"])
            self._ok(200, "closed")
            threading.Timer(1.0, lambda: subprocess.run(["systemctl", "disable", "--now", "escrow-boot"])).start()
            return
        self._ok(404, "?")
    def log_message(self, *a): pass

ThreadingHTTPServer(("127.0.0.1", 8099), H).serve_forever()
'''


def user_data(token: str) -> str:
    boot_sh = r'''#!/bin/bash
set -e
IP=$(curl -s http://169.254.169.254/hetzner/v1/metadata/public-ipv4)
DOMAIN="${IP//./-}.sslip.io"
echo "$DOMAIN" > /root/boot.domain
apt-get -o DPkg::Lock::Timeout=600 update -q
DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -yq debian-keyring debian-archive-keyring apt-transport-https curl gpg python3
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
apt-get -o DPkg::Lock::Timeout=600 update -q && DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -yq caddy
cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
    handle_path /_boot/* {  # boot
        reverse_proxy 127.0.0.1:8099  # boot
    }  # boot
    handle {
        respond "준비 중" 503
    }
}
CADDY
systemctl restart caddy
systemctl enable --now escrow-boot
'''
    unit = """[Unit]
Description=escrow one-time bootstrap upload
After=network-online.target
[Service]
ExecStart=/usr/bin/python3 /root/boot.py
Restart=on-failure
[Install]
WantedBy=multi-user.target
"""

    def block(path, content, perm="0600"):
        ind = "\n".join("      " + line for line in content.splitlines())
        return f"  - path: {path}\n    permissions: '{perm}'\n    content: |\n{ind}\n"
    return ("#cloud-config\nwrite_files:\n" + block("/root/boot.token", token) + block("/root/boot.py", BOOT_PY)
            + block("/root/boot.sh", boot_sh, "0700") + block("/etc/systemd/system/escrow-boot.service", unit, "0644")
            + "runcmd:\n  - [bash, /root/boot.sh]\n")


def api(method: str, path: str, **kw):
    tok = os.environ["HCLOUD_TOKEN"]
    r = httpx.request(method, API + path, headers={"Authorization": f"Bearer {tok}"}, timeout=60, **kw)
    if r.status_code >= 400:
        raise SystemExit(f"헤츠너 API {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


def code_tarball() -> bytes:
    """커밋된 코드만 (작업 중 파일·비밀값 안 섞이게)."""
    raw = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", "HEAD"], capture_output=True, check=True).stdout
    buf = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(raw)) as src, tarfile.open(fileobj=buf, mode="w:gz") as dst:
        for m in src.getmembers():
            if m.name.startswith(("tests/", "docs/")):
                continue
            dst.addfile(m, src.extractfile(m) if m.isfile() else None)
    return buf.getvalue()


def boot(st: dict, method: str, path: str, **kw) -> httpx.Response:
    return httpx.request(method, f"https://{st['domain']}/_boot{path}", headers={"X-Boot-Token": st["token"]}, timeout=60, **kw)


def wait(fn, what: str, timeout: int = 900, every: int = 10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            v = fn()
            if v:
                return v
        except httpx.HTTPError:
            pass
        time.sleep(every)
    raise SystemExit(f"시간 초과: {what}")


def create():
    if STATE.exists():
        st = json.loads(STATE.read_text())
        print("이미 만든 서버 있음:", st["ip"], "→ 이어서 진행")
    else:
        names = [s["name"] for s in api("GET", "/servers")["servers"]]
        if NAME in names:
            raise SystemExit(f"'{NAME}' 서버가 이미 있어요 (상태 파일 없음). 헤츠너에서 확인해주세요.")
        token = secrets.token_hex(24)
        last = None
        for loc in LOCATIONS:
            try:
                res = api("POST", "/servers", json={"name": NAME, "server_type": TYPE, "image": "ubuntu-24.04", "location": loc,
                                                    "user_data": user_data(token), "labels": {"app": "escrow"},
                                                    "public_net": {"enable_ipv4": True, "enable_ipv6": True}})
                break
            except SystemExit as e:
                last = e
                print(f"{loc} 실패: {e}")
        else:
            raise last
        ip = res["server"]["public_net"]["ipv4"]["ip"]
        st = {"id": res["server"]["id"], "ip": ip, "domain": ip.replace(".", "-") + ".sslip.io", "token": token,
              "location": res["server"]["datacenter"]["location"]["name"]}
        STATE.write_text(json.dumps(st))
        STATE.chmod(0o600)
        print(f"서버 생성: {ip} ({st['location']}) — root 비밀번호는 헤츠너 계정 메일로 감")
    print("부트 통로 기다리는 중 (Caddy·인증서, 보통 2~5분)…")
    wait(lambda: boot(st, "GET", "/ping").text == "ok", "부트 통로")
    print("코드 올리는 중…")
    r = boot(st, "POST", "/upload", content=code_tarball())
    print(" ", r.status_code, r.text)
    print("설치 시작 (Postgres·파이썬·서비스, 5~10분)…")
    print(" ", boot(st, "POST", "/install").text)

    def done():
        log = boot(st, "GET", "/log").text
        return log if "__EXIT__" in log else None
    log = wait(done, "설치", timeout=1800, every=15)
    print(log[-2500:])
    print("서비스:", boot(st, "GET", "/svc").text.split())


def status():
    st = json.loads(STATE.read_text())
    s = api("GET", f"/servers/{st['id']}")["server"]
    print("서버:", s["name"], s["status"], st["ip"], st["domain"])
    try:
        print("관리 페이지:", httpx.get(f"https://{st['domain']}/", timeout=20).status_code,
              "· API(로그인 전 401 이 정상):", httpx.get(f"https://{st['domain']}/api/stats", timeout=20).status_code)
    except httpx.HTTPError as e:
        print("관리 페이지 접속 실패:", e)
    try:
        r = boot(st, "GET", "/svc")
        print("부트 통로:", r.status_code, r.text.split() if r.status_code == 200 else "")
    except httpx.HTTPError:
        print("부트 통로: 닫힘")


if __name__ == "__main__":
    {"create": create, "status": status}[sys.argv[1] if len(sys.argv) > 1 else "status"]()
