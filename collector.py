# ── sing-box output (NEW, additive) ───────────────────────────────────────────
# Paste this whole block into collector.py (anywhere above main), then call
# save_singbox(...) where the other outputs are written. See the notes at the
# bottom of this block.
#
# Produces ONE sing-box JSON profile that the sing-box Android app (SFA) can
# load as a "Remote profile" URL and auto-update. It contains only the servers
# you pass in (the TCP-tested / Iran-confirmed ones), wired into a urltest
# group, so sing-box keeps picking the fastest live server by itself.
#
# Unlike the Clash-dict -> Xray path above, this parses the ORIGINAL URIs, so it
# keeps fields the Clash dict drops: VLESS `flow` (xtls-rprx-vision), uTLS
# fingerprint (`fp`), `alpn`, ws early-data, hysteria2 obfs, etc.

import uuid as _uuid
from urllib.parse import urlsplit, parse_qs, unquote

SINGBOX_OUTPUT_FILE = Path("output/singbox.json")
SINGBOX_IRAN_OUTPUT_FILE = Path("output/singbox_iran_working.json")
MAX_SINGBOX_SERVERS = 40            # cap on servers inside the urltest group
SINGBOX_TEST_URL = "https://www.gstatic.com/generate_204"
SINGBOX_TEST_INTERVAL = "1m"

# Ciphers sing-box's shadowsocks outbound accepts. Anything else would make the
# WHOLE profile fail to load, so unknown ones are skipped instead.
_SB_SS_METHODS = {
    "none", "aes-128-gcm", "aes-192-gcm", "aes-256-gcm",
    "chacha20-ietf-poly1305", "xchacha20-ietf-poly1305",
    "2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm",
    "2022-blake3-chacha20-poly1305",
    "aes-128-ctr", "aes-192-ctr", "aes-256-ctr",
    "aes-128-cfb", "aes-192-cfb", "aes-256-cfb", "chacha20-ietf",
}
_SB_VMESS_CIPHERS = {"auto", "none", "zero", "aes-128-gcm", "chacha20-poly1305"}
_HEX = set("0123456789abcdefABCDEF")


def _q(params: dict, key: str, default: str = "") -> str:
    v = params.get(key)
    return v[0] if v else default


def _sb_valid_uuid(s: str) -> bool:
    try:
        _uuid.UUID(s)
        return True
    except Exception:
        return False


def _sb_valid_reality(pbk: str, sid: str) -> bool:
    # public key: 32 bytes base64url (43 chars, no padding); short id: 0-16 hex, even length
    if len(pbk) != 43:
        return False
    if len(sid) > 16 or len(sid) % 2 or any(c not in _HEX for c in sid):
        return False
    return True


def _sb_tls(params: dict, server: str, force: bool = False) -> dict | None:
    security = _q(params, "security", "tls" if force else "none")
    if security not in ("tls", "reality"):
        return None
    sni = _q(params, "sni") or _q(params, "peer") or _q(params, "host") or server
    tls: dict = {"enabled": True, "server_name": sni}
    if _q(params, "allowInsecure") in ("1", "true") or _q(params, "insecure") in ("1", "true"):
        tls["insecure"] = True
    alpn = _q(params, "alpn")
    if alpn:
        tls["alpn"] = [a for a in unquote(alpn).split(",") if a]
    fp = _q(params, "fp")
    if security == "reality":
        pbk, sid = _q(params, "pbk"), _q(params, "sid")
        if not _sb_valid_reality(pbk, sid):
            return None
        tls["reality"] = {"enabled": True, "public_key": pbk, "short_id": sid}
        tls["utls"] = {"enabled": True, "fingerprint": fp or "chrome"}
    elif fp and fp != "randomized":
        tls["utls"] = {"enabled": True, "fingerprint": fp}
    return tls


def _sb_transport(net: str, params: dict, server: str) -> tuple[bool, dict | None]:
    """Return (supported, transport_dict_or_None). xhttp/kcp/quic aren't supported -> (False, None)."""
    host = _q(params, "host")
    path = unquote(_q(params, "path", "/")) or "/"
    if net in ("", "tcp"):
        if _q(params, "headerType") not in ("", "none"):
            return False, None          # tcp+http header obfuscation: skip
        return True, None
    if net == "ws":
        t: dict = {"type": "ws", "path": path}
        if "?ed=" in path or "&ed=" in path:
            base, _, query = path.partition("?")
            kv = parse_qs(query)
            if kv.get("ed", [""])[0].isdigit():
                t["path"] = base or "/"
                t["max_early_data"] = int(kv["ed"][0])
                t["early_data_header_name"] = "Sec-WebSocket-Protocol"
        if host:
            t["headers"] = {"Host": host}
        return True, t
    if net == "grpc":
        return True, {"type": "grpc", "service_name": _q(params, "serviceName") or _q(params, "path")}
    if net == "httpupgrade":
        t = {"type": "httpupgrade", "path": path}
        if host:
            t["host"] = host
        return True, t
    return False, None


def _sb_parse_hostport(hostport: str) -> tuple[str, int] | None:
    try:
        if hostport.startswith("["):                      # [ipv6]:port
            host, _, port = hostport[1:].partition("]:")
        else:
            host, _, port = hostport.rpartition(":")
        p = int(port)
        if not host or not 1 <= p <= 65535:
            return None
        return host, p
    except Exception:
        return None


def uri_to_singbox_outbound(cfg: str, tag: str) -> dict | None:
    """Convert one proxy URI to a sing-box outbound dict, or None if unsupported/invalid."""
    try:
        base = cfg.split("#")[0]

        if base.startswith("vmess://"):
            b64 = base[len("vmess://"):]
            raw = json.loads(base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode())
            server, port = str(raw.get("add", "")), int(raw.get("port", 0))
            uid = str(raw.get("id", ""))
            if not server or not 1 <= port <= 65535 or not _sb_valid_uuid(uid):
                return None
            params = {
                "host": [str(raw.get("host", ""))], "path": [str(raw.get("path", "/"))],
                "serviceName": [str(raw.get("path", ""))], "sni": [str(raw.get("sni", ""))],
                "alpn": [str(raw.get("alpn", ""))], "fp": [str(raw.get("fp", ""))],
                "security": ["tls" if str(raw.get("tls", "")) == "tls" else "none"],
                "headerType": [str(raw.get("type", "none"))],
            }
            ok, transport = _sb_transport(str(raw.get("net", "tcp")), params, server)
            if not ok:
                return None
            cipher = str(raw.get("scy") or raw.get("security") or "auto")
            ob: dict = {
                "type": "vmess", "tag": tag, "server": server, "server_port": port,
                "uuid": uid, "security": cipher if cipher in _SB_VMESS_CIPHERS else "auto",
                "alter_id": int(raw.get("aid", 0) or 0),
            }
            tls = _sb_tls(params, server)
            if tls:
                ob["tls"] = tls
            if transport:
                ob["transport"] = transport
            return ob

        if base.startswith("ss://"):
            body = base[len("ss://"):]
            if "?" in body:
                body, query = body.split("?", 1)
                if "plugin" in parse_qs(query):
                    return None                           # plugin-based ss: skip
            body = unquote(body)
            if "@" in body:
                userinfo, hostport = body.rsplit("@", 1)
                if ":" not in userinfo:
                    userinfo = base64.urlsafe_b64decode(userinfo + "=" * (-len(userinfo) % 4)).decode()
            else:
                decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
                userinfo, hostport = decoded.rsplit("@", 1)
            method, password = userinfo.split(":", 1)
            hp = _sb_parse_hostport(hostport)
            if not hp or method not in _SB_SS_METHODS:
                return None
            return {"type": "shadowsocks", "tag": tag, "server": hp[0], "server_port": hp[1],
                    "method": method, "password": password}

        # vless / trojan / hysteria2 share URL-style parsing
        for scheme in ("vless://", "trojan://", "hysteria2://", "hy2://"):
            if base.startswith(scheme):
                break
        else:
            return None
        u = urlsplit(base)
        params = parse_qs(u.query)
        user = unquote(u.username or "")
        server, port = u.hostname or "", u.port or 0
        if not server or not 1 <= port <= 65535 or not user:
            return None

        if scheme == "vless://":
            if not _sb_valid_uuid(user) or _q(params, "encryption", "none") != "none":
                return None
            ok, transport = _sb_transport(_q(params, "type", "tcp"), params, server)
            if not ok:
                return None
            ob = {"type": "vless", "tag": tag, "server": server, "server_port": port, "uuid": user}
            flow = _q(params, "flow")
            if flow:
                # sing-box only knows plain vision; Xray's "-udp443" variant maps to it
                if flow not in ("xtls-rprx-vision", "xtls-rprx-vision-udp443"):
                    return None
                ob["flow"] = "xtls-rprx-vision"
            tls = _sb_tls(params, server)
            if _q(params, "security") in ("tls", "reality") and not tls:
                return None                                # broken reality/tls params
            if tls:
                ob["tls"] = tls
            if transport:
                ob["transport"] = transport
            return ob

        if scheme == "trojan://":
            ok, transport = _sb_transport(_q(params, "type", "tcp"), params, server)
            if not ok:
                return None
            tls = _sb_tls(params, server, force=True)     # trojan is always TLS
            if not tls:
                return None
            ob = {"type": "trojan", "tag": tag, "server": server, "server_port": port,
                  "password": user, "tls": tls}
            if transport:
                ob["transport"] = transport
            return ob

        # hysteria2 / hy2
        if _q(params, "mport"):
            return None                                    # port-hopping: skip
        tls = _sb_tls({**params, "security": ["tls"]}, server) or {"enabled": True, "server_name": server}
        ob = {"type": "hysteria2", "tag": tag, "server": server, "server_port": port,
              "password": user, "tls": tls}
        if _q(params, "obfs") == "salamander":
            ob["obfs"] = {"type": "salamander", "password": _q(params, "obfs-password")}
        return ob
    except Exception:
        return None


def build_singbox_config(tested: list) -> dict | None:
    """
    tested: list of (config_uri, proxy_dict, latency_ms) fastest-first (what
    test_configs() / test_iran_reachability() return). Returns a full sing-box
    profile dict, or None if no config could be converted.
    """
    outbounds: list[dict] = []
    for cfg, _proxy, _lat in tested:
        if len(outbounds) >= MAX_SINGBOX_SERVERS:
            break
        ob = uri_to_singbox_outbound(cfg, tag=f"s{len(outbounds) + 1}")
        if ob:
            outbounds.append(ob)
    if not outbounds:
        return None

    tags = [o["tag"] for o in outbounds]
    return {
        "log": {"level": "warn"},
        "dns": {
            "servers": [
                {"type": "https", "tag": "dns-remote", "server": "1.1.1.1", "detour": "proxy"},
                {"type": "local", "tag": "dns-local"},
            ],
            "final": "dns-remote",
            "strategy": "ipv4_only",
        },
        "inbounds": [{
            "type": "tun", "tag": "tun-in",
            "address": ["172.19.0.1/30"],
            "auto_route": True, "strict_route": True, "stack": "mixed",
        }],
        "outbounds": [
            {"type": "selector", "tag": "proxy", "outbounds": ["auto", *tags], "default": "auto"},
            {"type": "urltest", "tag": "auto", "outbounds": tags,
             "url": SINGBOX_TEST_URL, "interval": SINGBOX_TEST_INTERVAL, "tolerance": 50},
            *outbounds,
            {"type": "direct", "tag": "direct"},
        ],
        "route": {
            "rules": [
                {"action": "sniff"},
                {"protocol": "dns", "action": "hijack-dns"},
                {"ip_is_private": True, "outbound": "direct"},
            ],
            "final": "proxy",
            "auto_detect_interface": True,
            "default_domain_resolver": "dns-local",
        },
    }


def save_singbox(tested: list, path: Path = SINGBOX_OUTPUT_FILE) -> None:
    """Write a sing-box profile for the given tested configs. Does nothing if none convert."""
    cfg = build_singbox_config(tested)
    if cfg is None:
        print(f"⚠️ sing-box: no convertible configs, {path} not written")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    n = len(cfg["outbounds"]) - 3          # minus selector, urltest, direct
    print(f"✅ Saved sing-box profile with {n} servers → {path}")

# ── How to hook it in ─────────────────────────────────────────────────────────
#   Right after the line(s) in main() that write LEASTPING_OUTPUT_FILE, add:
#       save_singbox(tested)                                   # whatever variable holds the TCP-tested list
#   and right after save_iran_working(...), add:
#       save_singbox(iran_ok, SINGBOX_IRAN_OUTPUT_FILE)        # whatever variable holds the Iran-confirmed list
