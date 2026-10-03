import os
import re
import json
import random
import socket
import base64
import shutil
import asyncio
import tempfile
import uuid as _uuid
import httpx
from html import unescape
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qs, unquote, quote

try:                      # optional: pip install pyyaml  (script works without it)
    import yaml
except ImportError:
    yaml = None

# ── Config ────────────────────────────────────────────────────────────────────

CHANNELS = [
    "kurdconfig", "Configir98", "YamYamProxy", "FreeConfigForYou", "begoo_vpn_gp", "iranconnecting",
    "Zed_NetMeli", "on_proxy1", "Spotify_Porteghali", "oxnet_ir", "proxy_station", "bygfw", "ezaccess1",
    "appxa", "v2rayyngvpn", "sparrk_vpn", "amir_webstudio"
]

TELEGRAM_PAGES = 3   # t.me/s/<channel> only shows ~20 posts per page; fetch this many pages back

# ── External subscription URLs ────────────────────────────────────────────────
# Add any v2ray (plain or base64) or Clash (YAML, block or inline style) sub URLs.

EXTERNAL_SUB_URLS: list[str] = [
    "https://raw.githubusercontent.com/patterniha/Free-Configs/main/configs.txt",
    # "https://example.com/v2ray-sub",       # v2ray base64 / plain subscription
    # "https://example.com/clash-sub.yaml",  # Clash YAML subscription
]

PROTOCOLS = ("vmess://", "vless://", "trojan://", "ss://", "ssr://", "tuic://", "hysteria2://", "hy2://")

# ── Outputs ───────────────────────────────────────────────────────────────────
# configs.txt          tested configs only (plain text, one URI per line)
# configs_untested.txt every unique config collected, untested
# Every config is named  mn_<random emoji>_<number>  in each file.

OUTPUT_FILE                = Path("output/configs.txt")
UNTESTED_OUTPUT_FILE       = Path("output/configs_untested.txt")
CLASH_OUTPUT_FILE          = Path("output/clash.yaml")
CLASH_UNTESTED_OUTPUT_FILE = Path("output/clash_untested.yaml")

# ── Testing ───────────────────────────────────────────────────────────────────
# Each config is loaded into a real xray / sing-box process and a request is made
# THROUGH it. Only configs that really carry traffic are kept. No dedupe by
# server:port (many different working configs share one CDN ip:port).
# Where you run the script decides what "works" means: run it from inside Iran
# to get Iran-true results.

XRAY_BIN    = os.environ.get("XRAY_BIN")    or shutil.which("xray")
SINGBOX_BIN = os.environ.get("SINGBOX_BIN") or shutil.which("sing-box")   # only needed for hysteria2

REAL_TEST_URLS    = ("https://www.gstatic.com/generate_204", "https://cp.cloudflare.com/generate_204")
REAL_TEST_TIMEOUT = 8          # seconds per request through the proxy
REAL_TEST_BATCH   = 40         # configs loaded into one core process
CORE_PARALLEL     = 4          # core processes running at once
CORE_START_WAIT   = 10         # seconds to wait for a core to open its local ports

RETRY_FAILED  = True           # second, gentler pass over failures (smaller batches, longer timeout)
RETRY_BATCH   = 20
RETRY_TIMEOUT = 15

TESTED_MAX = None              # cap on the tested list, fastest first; None = keep every working config

# ── Balancer / sing-box profiles (built from the tested list) ─────────────────
LEASTPING_OUTPUT_FILE      = Path("output/xray_leastping.json")
MAX_BALANCER_SERVERS       = 40
OBSERVATORY_PROBE_INTERVAL = "1s"   # raise to e.g. "5s" if battery/data use is noticeable

SINGBOX_OUTPUT_FILE   = Path("output/singbox.json")
MAX_SINGBOX_SERVERS   = 40
SINGBOX_TEST_URL      = "https://www.gstatic.com/generate_204"
SINGBOX_TEST_INTERVAL = "1m"

# ─────────────────────────────────────────────────────────────────────────────

CONFIG_PATTERN = re.compile(
    r'(?<![A-Za-z0-9])(?:vmess|vless|trojan|ssr|ss|tuic|hysteria2|hy2)://[^\s<>"\'`]+'
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

# ── Small shared helpers ──────────────────────────────────────────────────────

def _b64d(s: str) -> str:
    """Decode standard OR urlsafe base64, with or without padding."""
    s = s.strip().replace("-", "+").replace("_", "/").rstrip("=")
    return base64.b64decode(s + "=" * (-len(s) % 4)).decode("utf-8", errors="ignore")


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _scheme_of(cfg: str) -> str:
    return cfg.split("://", 1)[0].lower()


def _find_uris(text: str) -> list[str]:
    """All proxy URIs found anywhere in text, order-preserving, exact-dupes removed."""
    seen: set[str] = set()
    out: list[str] = []
    for m in CONFIG_PATTERN.findall(text):
        c = m.strip().rstrip(".,;)")
        if c not in seen and any(c.startswith(p) for p in PROTOCOLS):
            seen.add(c)
            out.append(c)
    return out

# ── Telegram fetch (with pagination) ──────────────────────────────────────────

async def fetch_channel(client: httpx.AsyncClient, channel: str) -> list[str]:
    configs: list[str] = []
    before: int | None = None
    try:
        for _ in range(TELEGRAM_PAGES):
            url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
            r = await client.get(url, timeout=20)
            r.raise_for_status()
            configs.extend(_find_uris(unescape(r.text)))
            ids = [int(x) for x in re.findall(r'data-post="[^"/]+/(\d+)"', r.text)]
            if not ids or min(ids) <= 1 or min(ids) == before:
                break
            before = min(ids)
        print(f"  ✔ {channel}: {len(configs)} configs found")
    except Exception as e:
        print(f"  ✘ {channel}: {e}" + (f" (kept {len(configs)} from earlier pages)" if configs else ""))
    return configs

# ── External subscription fetch ───────────────────────────────────────────────

def _is_clash_yaml(text: str) -> bool:
    return bool(re.search(r'^\s*proxies\s*:', text, re.MULTILINE))


def _extract_configs_from_v2ray_sub(text: str) -> list[str]:
    """
    Plain-text list first (b64decode never fails on plain text — it just returns
    garbage — so base64 must be the FALLBACK, not the first attempt), then base64.
    """
    text = text.strip().lstrip("\ufeff")
    found = _find_uris(text)
    if found:
        return found
    compact = re.sub(r"\s+", "", text)      # base64 blobs are often wrapped across lines
    try:
        return _find_uris(_b64d(compact))
    except Exception:
        return []

# ── Clash YAML parsing (PyYAML if installed, otherwise built-in fallback) ─────

def _cast(value: str):
    """Best-effort cast a YAML scalar string to int / bool / str."""
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        return int(value)
    except ValueError:
        return value


def _split_top_level(s: str, sep: str = ",") -> list[str]:
    parts: list[str] = []
    cur: list[str] = []
    depth, quote_ch = 0, None
    for ch in s:
        if quote_ch:
            cur.append(ch)
            if ch == quote_ch:
                quote_ch = None
            continue
        if ch in "\"'":
            quote_ch = ch
            cur.append(ch)
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _parse_inline_value(v: str):
    v = v.strip()
    if v.startswith("{") and v.endswith("}"):
        return _parse_inline_dict(v)
    if v.startswith("[") and v.endswith("]"):
        return [_parse_inline_value(x) for x in _split_top_level(v[1:-1]) if x.strip()]
    return _cast(v.strip("\"'"))


def _parse_inline_dict(s: str) -> dict:
    """Parse a YAML flow mapping:  {name: x, type: vmess, ws-opts: {path: /}}"""
    d: dict = {}
    for part in _split_top_level(s.strip()[1:-1]):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        d[k.strip().strip("\"'")] = _parse_inline_value(v)
    return d


def _parse_clash_block_fallback(block: str) -> list[dict]:
    """
    Indentation-aware mini parser for the `proxies:` list. Handles block-style
    items, inline `- {a: b}` items, nested dicts (ws-opts, reality-opts, ...),
    and block lists (alpn). Works for any indent style, including list items
    at column 0 (`proxies:\\n- name: x`).
    """
    lines = [l.rstrip() for l in block.splitlines() if l.strip() and not l.strip().startswith("#")]
    dash_indent = None
    for l in lines:
        if l.lstrip().startswith("-"):
            dash_indent = len(l) - len(l.lstrip())
            break
    if dash_indent is None:
        return []

    proxies: list[dict] = []
    stack: list[tuple[int, dict]] = []                  # (exclusive_indent, dict)
    pending: tuple[dict, str, int] | None = None        # (container, key, key_indent) awaiting children

    def kv(content: str, ind: int) -> None:
        nonlocal pending
        m = re.match(r'^["\']?([\w.\-]+)["\']?\s*:(?:\s+(.*))?$', content)
        if not m or not stack:
            return
        key, val = m.group(1), (m.group(2) or "").strip()
        target = stack[-1][1]
        if val == "":
            target[key] = None
            pending = (target, key, ind)
        else:
            target[key] = _parse_inline_value(val)
            pending = None

    for l in lines:
        ind = len(l) - len(l.lstrip())
        s = l.strip()
        if s.startswith("-") and (len(s) == 1 or s[1] in " \t"):
            rest = s[1:].strip()
            if ind <= dash_indent:                       # new proxy item
                pending = None
                if rest.startswith("{"):
                    proxies.append(_parse_inline_dict(rest))
                    stack = []
                    continue
                cur: dict = {}
                proxies.append(cur)
                content_indent = ind + (len(s) - len(s[1:].lstrip()))
                stack = [(content_indent - 1, cur)]
                if rest:
                    kv(rest, content_indent)
            elif pending:                                # list item under a key
                cont, key, _ = pending
                if not isinstance(cont.get(key), list):
                    cont[key] = []
                cont[key].append(_parse_inline_value(rest))
            continue
        if not stack:
            continue
        while len(stack) > 1 and stack[-1][0] >= ind:
            stack.pop()
        if pending and ind > pending[2] and pending[0].get(pending[1]) is None:
            child: dict = {}
            pending[0][pending[1]] = child
            stack.append((pending[2], child))
        kv(s, ind)

    return proxies


def _parse_clash_yaml_proxies(text: str) -> list[dict]:
    if yaml is not None:
        try:
            data = yaml.safe_load(text)
            if isinstance(data, dict) and isinstance(data.get("proxies"), list):
                return [p for p in data["proxies"] if isinstance(p, dict)]
        except Exception:
            pass
    m = re.search(r'^proxies\s*:[ \t]*(?:#.*)?\n(.*?)(?=^[^\s#-]|\Z)', text, re.MULTILINE | re.DOTALL)
    if not m:
        return []
    return _parse_clash_block_fallback(m.group(1))

# ── URI building helpers (Clash -> URI) ───────────────────────────────────────

def _qs(pairs) -> str:
    return "&".join(f"{k}={quote(str(v), safe='')}" for k, v in pairs if v not in (None, ""))


def _hp(host: str, port) -> str:
    return f"[{host}]:{port}" if ":" in host and not host.startswith("[") else f"{host}:{port}"


def _transport_pairs(proxy: dict, host: str) -> list[tuple[str, str]]:
    net = str(proxy.get("network", "tcp"))
    pairs: list[tuple[str, str]] = [("type", net)]
    if net == "ws":
        ws = proxy.get("ws-opts") or {}
        pairs += [("path", ws.get("path", "/")), ("host", (ws.get("headers") or {}).get("Host", host))]
    elif net == "grpc":
        pairs.append(("serviceName", (proxy.get("grpc-opts") or {}).get("grpc-service-name", "")))
    elif net in ("xhttp", "httpupgrade"):
        o = proxy.get(f"{net}-opts") or {}
        pairs += [("path", o.get("path", "/")), ("host", o.get("host", host))]
    return pairs


def _clash_proxy_to_uri(proxy: dict) -> str | None:
    """Convert one Clash proxy dict back to a share URI. None if unsupported."""
    ptype = str(proxy.get("type", "")).lower()
    name  = str(proxy.get("name", "proxy"))
    host  = str(proxy.get("server", ""))
    port  = proxy.get("port", 443)
    if not host:
        return None
    frag = "#" + quote(name, safe="")

    try:
        if ptype == "vmess":
            raw = {
                "v": "2", "ps": name, "add": host, "port": str(port),
                "id": str(proxy.get("uuid", "")), "aid": str(proxy.get("alterId", 0)),
                "scy": str(proxy.get("cipher", "auto")), "net": "tcp", "type": "none",
                "tls": "tls" if proxy.get("tls") else "", "sni": str(proxy.get("servername", "")),
                "fp": str(proxy.get("client-fingerprint", "")),
            }
            net = str(proxy.get("network", "tcp"))
            if net == "ws":
                ws = proxy.get("ws-opts") or {}
                raw.update(net="ws", path=str(ws.get("path", "/")),
                           host=str((ws.get("headers") or {}).get("Host", host)))
            elif net == "grpc":
                raw.update(net="grpc", path=str((proxy.get("grpc-opts") or {}).get("grpc-service-name", "")))
            elif net in ("xhttp", "httpupgrade"):
                o = proxy.get(f"{net}-opts") or {}
                raw.update(net=net, path=str(o.get("path", "/")), host=str(o.get("host", host)))
            return "vmess://" + base64.b64encode(json.dumps(raw, ensure_ascii=False).encode()).decode()

        if ptype == "vless":
            reality = proxy.get("reality-opts") or {}
            pairs: list[tuple[str, str]] = [("encryption", "none")]
            if reality:
                pairs += [("security", "reality"), ("pbk", reality.get("public-key", "")),
                          ("sid", reality.get("short-id", ""))]
            elif proxy.get("tls"):
                pairs.append(("security", "tls"))
            alpn = proxy.get("alpn")
            pairs += [("sni", proxy.get("servername")), ("fp", proxy.get("client-fingerprint")),
                      ("flow", proxy.get("flow")),
                      ("alpn", ",".join(alpn) if isinstance(alpn, list) else alpn)]
            if proxy.get("skip-cert-verify"):
                pairs.append(("allowInsecure", "1"))
            pairs += _transport_pairs(proxy, host)
            return f"vless://{quote(str(proxy.get('uuid', '')), safe='')}@{_hp(host, port)}?{_qs(pairs)}{frag}"

        if ptype == "trojan":
            pairs = [("sni", proxy.get("sni") or proxy.get("servername"))]
            if proxy.get("skip-cert-verify"):
                pairs.append(("allowInsecure", "1"))
            pairs += _transport_pairs(proxy, "")
            return f"trojan://{quote(str(proxy.get('password', '')), safe='')}@{_hp(host, port)}?{_qs(pairs)}{frag}"

        if ptype == "ss":
            if proxy.get("plugin"):
                return None
            method   = str(proxy.get("cipher", "aes-256-gcm"))
            password = str(proxy.get("password", ""))
            userinfo = base64.urlsafe_b64encode(f"{method}:{password}".encode()).decode().rstrip("=")
            return f"ss://{userinfo}@{_hp(host, port)}{frag}"

        if ptype in ("hysteria2", "hy2"):
            pairs = [("sni", proxy.get("sni")), ("obfs", proxy.get("obfs")),
                     ("obfs-password", proxy.get("obfs-password"))]
            if proxy.get("skip-cert-verify"):
                pairs.append(("insecure", "1"))
            return f"hysteria2://{quote(str(proxy.get('password', '')), safe='')}@{_hp(host, port)}?{_qs(pairs)}{frag}"
    except Exception:
        pass
    return None


def _extract_configs_from_clash_sub(text: str) -> list[str]:
    configs: list[str] = []
    for proxy in _parse_clash_yaml_proxies(text):
        uri = _clash_proxy_to_uri(proxy)
        if uri:
            configs.append(uri)
    return configs


async def fetch_external_sub(client: httpx.AsyncClient, url: str) -> list[str]:
    """Fetch one external subscription URL (plain list, base64, or Clash YAML)."""
    try:
        r = await client.get(url, timeout=30, follow_redirects=True)
        r.raise_for_status()
        text = r.text.strip()

        if _is_clash_yaml(text):
            configs = _extract_configs_from_clash_sub(text) or _extract_configs_from_v2ray_sub(text)
            kind = "Clash sub"
        else:
            configs = _extract_configs_from_v2ray_sub(text) or _extract_configs_from_clash_sub(text)
            kind = "V2Ray sub"

        if configs:
            print(f"  ✔ [{kind}] {url}: {len(configs)} configs found")
        else:
            preview = text[:120].replace("\n", " ")
            print(f"  ⚠ [{kind}] {url}: 0 configs. Response starts with: {preview!r}")
        return configs
    except Exception as e:
        print(f"  ✘ [External sub] {url}: {e}")
        return []

# ── URI parsing (-> Clash-style dicts) ────────────────────────────────────────

def _decode_vmess(uri: str) -> dict | None:
    try:
        data = json.loads(_b64d(uri[len("vmess://"):]))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _parse_ss(uri: str) -> dict | None:
    """Parse ss:// in SIP002 (base64 userinfo or plain) and legacy (whole-body base64) forms."""
    try:
        body  = uri.split("#", 1)[0][len("ss://"):]
        query = ""
        if "?" in body:
            body, query = body.split("?", 1)
        body = unquote(body).rstrip("/")
        if "@" in body:
            userinfo, hostport = body.rsplit("@", 1)
            if ":" not in userinfo:
                userinfo = _b64d(userinfo)
        else:
            userinfo, hostport = _b64d(body).rsplit("@", 1)
        method, password = userinfo.split(":", 1)
        host, port_str   = hostport.rsplit(":", 1)
        return {"method": method, "password": password, "host": host.strip("[]"),
                "port": int(port_str), "plugin": "plugin=" in query}
    except Exception:
        return None


def _uri_parts(uri: str):
    """urlsplit-based parse for vless/trojan/hysteria2/tuic. -> (user, host, port, params) or None.
    Query values are percent-decoded (so path=%2Fws becomes /ws) but '+' is kept as-is."""
    try:
        u    = urlsplit(uri.split("#", 1)[0])
        host = u.hostname or ""
        try:
            port = u.port or 443
        except ValueError:                        # e.g. hysteria2 port-hopping "443,5000-6000"
            m    = re.search(r':(\d+)', u.netloc.rsplit("@", 1)[-1])
            port = int(m.group(1)) if m else 443
        user = unquote(u.username or "")
        if u.password is not None:
            user += ":" + unquote(u.password)
        params: dict[str, str] = {}
        for p in u.query.split("&"):
            k, _, v = p.partition("=")
            if k:
                params[k] = unquote(v)
        return user, host, port, params
    except Exception:
        return None


def _apply_transport(proxy: dict, net: str, params: dict, default_host: str) -> None:
    """Fill network/ws-opts/grpc-opts/... on a Clash proxy dict."""
    if net == "ws":
        proxy["network"] = "ws"
        opts: dict = {"path": params.get("path") or "/"}
        h = params.get("host") or default_host
        if h:
            opts["headers"] = {"Host": h}
        proxy["ws-opts"] = opts
    elif net == "grpc":
        proxy["network"]   = "grpc"
        proxy["grpc-opts"] = {"grpc-service-name": params.get("serviceName", "")}
    elif net in ("xhttp", "httpupgrade"):
        proxy["network"]       = net
        proxy[f"{net}-opts"]   = {"path": params.get("path") or "/",
                                  "host": params.get("host") or default_host}


def _config_to_clash_proxy(cfg: str, name: str) -> dict | None:
    if cfg.startswith("vmess://"):
        raw = _decode_vmess(cfg.split("#")[0])
        if not raw:
            return None
        server = str(raw.get("add", "")).strip()
        port   = _int(raw.get("port"), 0)
        if not server or not 1 <= port <= 65535:
            return None
        proxy: dict = {
            "name": name, "type": "vmess", "server": server, "port": port,
            "uuid": str(raw.get("id", "")), "alterId": _int(raw.get("aid"), 0),
            "cipher": str(raw.get("scy") or raw.get("security") or "auto"), "udp": True,
        }
        path = str(raw.get("path", "") or "")
        _apply_transport(proxy, str(raw.get("net", "tcp") or "tcp"),
                         {"path": path, "host": str(raw.get("host", "") or ""), "serviceName": path}, server)
        if str(raw.get("tls", "")) == "tls":
            proxy["tls"] = True
            sni = str(raw.get("sni") or raw.get("host") or "")
            if sni:
                proxy["servername"] = sni
            if raw.get("fp"):
                proxy["client-fingerprint"] = str(raw["fp"])
        return proxy

    if cfg.startswith("vless://"):
        parts = _uri_parts(cfg)
        if not parts or not parts[0] or not parts[1]:
            return None
        uuid, host, port, params = parts
        proxy = {"name": name, "type": "vless", "server": host, "port": port, "uuid": uuid, "udp": True}
        security = params.get("security", "")
        sni = params.get("sni") or params.get("peer")
        if security in ("tls", "reality"):
            proxy["tls"] = True
            if sni:
                proxy["servername"] = sni
        if security == "reality":
            proxy["reality-opts"] = {"public-key": params.get("pbk", ""), "short-id": params.get("sid", "")}
        if params.get("fp"):
            proxy["client-fingerprint"] = params["fp"]
        if params.get("alpn"):
            proxy["alpn"] = [a for a in params["alpn"].split(",") if a]
        if params.get("flow"):
            proxy["flow"] = params["flow"]
        if params.get("allowInsecure") in ("1", "true") or params.get("insecure") in ("1", "true"):
            proxy["skip-cert-verify"] = True
        _apply_transport(proxy, params.get("type", "tcp"), params, host)
        return proxy

    if cfg.startswith("trojan://"):
        parts = _uri_parts(cfg)
        if not parts or not parts[0] or not parts[1]:
            return None
        password, host, port, params = parts
        proxy = {"name": name, "type": "trojan", "server": host, "port": port, "password": password, "udp": True}
        sni = params.get("sni") or params.get("peer")
        if sni:
            proxy["sni"] = sni
        if params.get("alpn"):
            proxy["alpn"] = [a for a in params["alpn"].split(",") if a]
        if params.get("fp"):
            proxy["client-fingerprint"] = params["fp"]
        if params.get("allowInsecure") in ("1", "true") or params.get("insecure") in ("1", "true"):
            proxy["skip-cert-verify"] = True
        _apply_transport(proxy, params.get("type", "tcp"), params, "")
        return proxy

    if cfg.startswith("ss://"):
        ss = _parse_ss(cfg)
        if not ss or ss["plugin"]:          # plugin-based ss can't be expressed here
            return None
        return {"name": name, "type": "ss", "server": ss["host"], "port": ss["port"],
                "cipher": ss["method"], "password": ss["password"], "udp": True}

    if cfg.startswith("hysteria2://") or cfg.startswith("hy2://"):
        parts = _uri_parts(cfg)
        if not parts or not parts[0] or not parts[1]:
            return None
        password, host, port, params = parts
        proxy = {"name": name, "type": "hysteria2", "server": host, "port": port, "password": password, "udp": True}
        if params.get("sni"):
            proxy["sni"] = params["sni"]
        if params.get("insecure", "0") in ("1", "true"):
            proxy["skip-cert-verify"] = True
        if params.get("obfs"):
            proxy["obfs"] = params["obfs"]
            proxy["obfs-password"] = params.get("obfs-password", "")
        return proxy

    return None


def config_to_clash_proxy(cfg: str, name: str) -> dict | None:
    """Safe wrapper: a malformed config returns None instead of crashing the run."""
    try:
        return _config_to_clash_proxy(cfg, name)
    except Exception:
        return None

# ── Full dedup (canonical identity) ───────────────────────────────────────────
# Two configs are duplicates when they would behave identically on the wire.
# The old key compared raw query strings / raw vmess JSON, so cosmetic
# differences (param order, missing-vs-default fields, "hy2" vs "hysteria2",
# host case, empty sni vs sni==host, fp/alpn tweaks, ...) kept dupes alive.
# Here every config is reduced to its meaningful fields, with defaults filled in.

_NOISE_PARAMS = {"fp", "alpn", "allowinsecure", "insecure", "headertype", "encryption", "udp", "ed"}


def _norm_host(h: str) -> str:
    return str(h).strip().strip("[]").rstrip(".").lower()


def _canon_stream(net: str, host_hdr: str, path: str, service: str, server: str) -> tuple:
    net = (net or "tcp").lower()
    if net == "tcp":
        return ("tcp",)
    if net == "grpc":
        return ("grpc", service or path)
    return (net, (host_hdr or server).lower(), path or "/")


def _dedup_key(cfg: str) -> str:
    base = cfg.split("#", 1)[0].strip().rstrip("?")
    try:
        scheme = _scheme_of(base)
        if scheme == "hy2":
            scheme = "hysteria2"

        if scheme == "vmess":
            raw = _decode_vmess(base)
            if raw:
                def g(k: str, d: str = "") -> str:
                    v = raw.get(k)
                    return d if v is None or str(v).strip() == "" else str(v).strip()
                server = _norm_host(g("add"))
                tls    = g("tls").lower() == "tls"
                net    = g("net", "tcp").lower()
                path   = g("path")
                sni    = (g("sni") or g("host") or server).lower() if tls else ""
                htype  = g("type", "none").lower() if net == "tcp" else ""
                return repr(("vmess", server, _int(g("port")), g("id").lower(), _int(g("aid")),
                             g("scy", g("security", "auto")).lower(), tls, sni,
                             _canon_stream(net, g("host"), path, path, server), htype))

        elif scheme == "ss":
            ss = _parse_ss(base)
            if ss:
                return repr(("ss", ss["method"].lower(), ss["password"], _norm_host(ss["host"]),
                             ss["port"], ss["plugin"]))

        elif scheme == "ssr":
            return "ssr|" + _b64d(base[len("ssr://"):]).split("/?")[0]

        elif scheme in ("vless", "trojan"):
            parts = _uri_parts(base)
            if parts and parts[1]:
                user, host, port, raw = parts
                p = {k.lower(): v for k, v in raw.items() if v != ""}
                server = _norm_host(host)
                sec    = p.get("security", "tls" if scheme == "trojan" else "none").lower()
                net    = p.get("type", "tcp")
                sni    = (p.get("sni") or p.get("peer") or p.get("host") or server).lower() \
                         if sec in ("tls", "reality") else ""
                htype  = p.get("headertype", "none").lower() if net.lower() == "tcp" else ""
                return repr((scheme, user.lower() if scheme == "vless" else user, server, port, sec, sni,
                             _canon_stream(net, p.get("host", ""), p.get("path", ""),
                                           p.get("servicename", ""), server),
                             p.get("pbk", ""), p.get("sid", "").lower(), p.get("flow", ""), htype))

        else:   # hysteria2, tuic, ...
            parts = _uri_parts(base)
            if parts and parts[1]:
                user, host, port, raw = parts
                p = sorted((k.lower(), v) for k, v in raw.items()
                           if v != "" and k.lower() not in _NOISE_PARAMS)
                return repr((scheme, user, _norm_host(host), port, p))
    except Exception:
        pass
    return base

# ── Collect ───────────────────────────────────────────────────────────────────

async def collect_all() -> list[str]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; v2ray-collector/1.0)"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        tg_results  = await asyncio.gather(*(fetch_channel(client, ch) for ch in CHANNELS))
        sub_results = await asyncio.gather(*(fetch_external_sub(client, u) for u in EXTERNAL_SUB_URLS))

    seen: set[str] = set()
    all_configs: list[str] = []
    total = 0
    for batch in (*tg_results, *sub_results):
        for cfg in batch:
            total += 1
            key = _dedup_key(cfg)
            if key not in seen:
                seen.add(key)
                all_configs.append(cfg)
    print(f"   Collected {total} configs, {len(all_configs)} unique after full dedup")
    return all_configs

# ── Rename remarks ────────────────────────────────────────────────────────────

def _with_remark(cfg: str, remark: str) -> str:
    """Replace a config's remark. For vmess the remark is the JSON "ps" field, so set that too."""
    base = cfg.split("#", 1)[0].rstrip("?")
    if base.startswith("vmess://"):
        raw = _decode_vmess(base)
        if raw:
            raw["ps"] = remark
            base = "vmess://" + base64.b64encode(json.dumps(raw, ensure_ascii=False).encode()).decode()
    return f"{base}#{remark}"


_EMOJIS = list("🔥⚡🌙⭐🌈🍀🌸🍉🍋🍒🍓🍑🥝🍄🌻🌊🦊🐼🐧🦄🐬🦋🐢🚀🎯🎲🎧💎🧿🎈🎁🪐☀🍩🍕🥑🐙🦉🐝")


def name_configs(configs: list[str]) -> list[str]:
    """Name every config  mn_<random emoji>_<number>  (number = position in this output)."""
    return [_with_remark(cfg, f"mn_{random.choice(_EMOJIS)}_{i}") for i, cfg in enumerate(configs, start=1)]


def _remark(cfg: str) -> str:
    return cfg.split("#", 1)[1] if "#" in cfg else ""

# ── Clash YAML output ─────────────────────────────────────────────────────────

def _yaml_emit(lines: list[str], key: str, value, indent: int) -> None:
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return
        lines.append(f"{pad}{key}:")
        for k, v in value.items():
            _yaml_emit(lines, k, v, indent + 2)
    elif isinstance(value, bool):
        lines.append(f"{pad}{key}: {str(value).lower()}")
    elif isinstance(value, (int, float)):
        lines.append(f"{pad}{key}: {value}")
    elif isinstance(value, list):
        lines.append(f"{pad}{key}: {json.dumps(value, ensure_ascii=False)}")
    else:
        lines.append(f"{pad}{key}: {json.dumps(str(value), ensure_ascii=False)}")


def build_clash_yaml(configs: list[str]) -> str:
    proxies:     list[dict] = []
    proxy_names: list[str]  = []

    for cfg in configs:
        name  = cfg.split("#")[-1] if "#" in cfg else f"proxy_{len(proxies)+1}"
        proxy = config_to_clash_proxy(cfg, name)
        if proxy:
            proxies.append(proxy)
            proxy_names.append(name)

    if not proxies:
        return "# No parseable proxies found\nproxies: []\n"

    lines: list[str] = [
        "# Clash subscription — auto-generated",
        f"# Generated: {utc_now()}",
        f"# Total proxies: {len(proxies)}",
        "",
        "mixed-port: 7890",
        "allow-lan: false",
        "mode: rule",
        "log-level: info",
        "",
        "proxies:",
    ]
    for p in proxies:
        lines.append(f"  - name: {json.dumps(p['name'], ensure_ascii=False)}")
        for k, v in p.items():
            if k != "name":
                _yaml_emit(lines, k, v, 4)
        lines.append("")

    lines += ["proxy-groups:", '  - name: "AUTO"', "    type: url-test",
              "    url: http://www.gstatic.com/generate_204", "    interval: 300", "    proxies:"]
    lines += [f"      - {json.dumps(n)}" for n in proxy_names]
    lines += ["", '  - name: "PROXY"', "    type: select", "    proxies:", '      - "AUTO"']
    lines += [f"      - {json.dumps(n)}" for n in proxy_names]
    lines += ["", "rules:", "  - MATCH,AUTO", ""]
    return "\n".join(lines)

# ── Save outputs ──────────────────────────────────────────────────────────────

def save_untested(configs: list[str]) -> None:
    named = name_configs(configs)
    UNTESTED_OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    UNTESTED_OUTPUT_FILE.write_text("\n".join(named), encoding="utf-8")
    clash_yaml = build_clash_yaml(named)
    CLASH_UNTESTED_OUTPUT_FILE.write_text(clash_yaml, encoding="utf-8")
    print(f"\n✅ Saved {len(named)} unique untested configs → {UNTESTED_OUTPUT_FILE}")
    print(f"✅ Saved untested Clash subscription → {CLASH_UNTESTED_OUTPUT_FILE} "
          f"({clash_yaml.split('proxy-groups:')[0].count(chr(10) + '  - name:')} proxies)")


def save_tested(verified: list[tuple[str, dict, float]]) -> list[tuple[str, dict, float]]:
    """Write the tested outputs; returns the verified tuples with their final mn_ names applied."""
    if not verified:
        print("⚠️  No config passed the real test — leaving the tested outputs untouched.")
        return []
    top   = verified if TESTED_MAX is None else verified[:TESTED_MAX]
    named = name_configs([cfg for cfg, _p, _l in top])
    out   = [(n, p, lat) for n, (_c, p, lat) in zip(named, top)]
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text("\n".join(named), encoding="utf-8")
    clash_yaml = build_clash_yaml(named)
    CLASH_OUTPUT_FILE.write_text(clash_yaml, encoding="utf-8")
    print(f"✅ Saved {len(named)} tested configs → {OUTPUT_FILE}")
    print(f"✅ Saved tested Clash subscription → {CLASH_OUTPUT_FILE}")
    return out

# ── Xray outbound builder (Clash dict -> Xray) ────────────────────────────────

_XRAY_VMESS_SECURITY = {"auto", "aes-128-gcm", "chacha20-poly1305", "none", "zero"}


def clash_proxy_to_xray_outbound(proxy: dict, tag: str) -> dict | None:
    """Clash-style proxy dict -> raw Xray outbound (vmess / vless / trojan / shadowsocks)."""
    ptype   = str(proxy.get("type", "")).lower()
    host    = str(proxy.get("server", ""))
    port    = int(proxy.get("port", 443))
    network = str(proxy.get("network", "tcp"))

    stream: dict = {"network": network if network in ("tcp", "ws", "grpc", "xhttp", "httpupgrade") else "tcp"}
    if network == "ws":
        ws = proxy.get("ws-opts") or {}
        stream["wsSettings"] = {"path": ws.get("path", "/"), "headers": ws.get("headers") or {}}
    elif network == "grpc":
        stream["grpcSettings"] = {"serviceName": (proxy.get("grpc-opts") or {}).get("grpc-service-name", "")}
    elif network == "xhttp":
        xh = proxy.get("xhttp-opts") or {}
        stream["xhttpSettings"] = {"path": xh.get("path", "/"), "host": xh.get("host", "")}
    elif network == "httpupgrade":
        hu = proxy.get("httpupgrade-opts") or {}
        stream["httpupgradeSettings"] = {"path": hu.get("path", "/"), "host": hu.get("host", "")}

    fingerprint = proxy.get("client-fingerprint")
    reality     = proxy.get("reality-opts")
    if reality:
        stream["security"] = "reality"
        stream["realitySettings"] = {
            "serverName":  proxy.get("servername", ""),
            "publicKey":   reality.get("public-key", ""),
            "shortId":     reality.get("short-id", ""),
            "fingerprint": fingerprint or "chrome",
        }
    elif proxy.get("tls") or ptype == "trojan":      # trojan is TLS by design
        stream["security"] = "tls"
        tls: dict = {
            "serverName":    proxy.get("servername") or proxy.get("sni") or host,
            "allowInsecure": bool(proxy.get("skip-cert-verify", False)),
        }
        if proxy.get("alpn"):
            tls["alpn"] = proxy["alpn"]
        if fingerprint:
            tls["fingerprint"] = fingerprint
        stream["tlsSettings"] = tls
    else:
        stream["security"] = "none"

    try:
        if ptype == "vmess":
            cipher = str(proxy.get("cipher", "auto"))
            return {
                "tag": tag, "protocol": "vmess",
                "settings": {"vnext": [{"address": host, "port": port, "users": [{
                    "id": str(proxy.get("uuid", "")), "alterId": int(proxy.get("alterId", 0)),
                    "security": cipher if cipher in _XRAY_VMESS_SECURITY else "auto",
                }]}]},
                "streamSettings": stream,
            }
        if ptype == "vless":
            user = {"id": str(proxy.get("uuid", "")), "encryption": "none"}
            if proxy.get("flow"):                     # without this, Reality+Vision servers reject the connection
                user["flow"] = str(proxy["flow"])
            return {
                "tag": tag, "protocol": "vless",
                "settings": {"vnext": [{"address": host, "port": port, "users": [user]}]},
                "streamSettings": stream,
            }
        if ptype == "trojan":
            return {
                "tag": tag, "protocol": "trojan",
                "settings": {"servers": [{"address": host, "port": port,
                                          "password": str(proxy.get("password", ""))}]},
                "streamSettings": stream,
            }
        if ptype == "ss":
            return {
                "tag": tag, "protocol": "shadowsocks",
                "settings": {"servers": [{"address": host, "port": port,
                                          "method": str(proxy.get("cipher", "aes-256-gcm")),
                                          "password": str(proxy.get("password", ""))}]},
                "streamSettings": {"network": "tcp", "security": "none"},
            }
    except Exception:
        return None
    return None

# ── sing-box outbound builder (URI -> sing-box) ───────────────────────────────
# Parses the ORIGINAL URIs, so it keeps fields the Clash dict drops (VLESS flow,
# uTLS fingerprint, alpn, ws early-data, hysteria2 obfs, ...).

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
            return False, None
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
            ss = _parse_ss(base)
            if not ss or ss["plugin"] or ss["method"] not in _SB_SS_METHODS:
                return None
            if not 1 <= ss["port"] <= 65535:
                return None
            return {"type": "shadowsocks", "tag": tag, "server": ss["host"], "server_port": ss["port"],
                    "method": ss["method"], "password": ss["password"]}

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
                if flow not in ("xtls-rprx-vision", "xtls-rprx-vision-udp443"):
                    return None
                ob["flow"] = "xtls-rprx-vision"
            tls = _sb_tls(params, server)
            if _q(params, "security") in ("tls", "reality") and not tls:
                return None
            if tls:
                ob["tls"] = tls
            if transport:
                ob["transport"] = transport
            return ob

        if scheme == "trojan://":
            ok, transport = _sb_transport(_q(params, "type", "tcp"), params, server)
            if not ok:
                return None
            tls = _sb_tls(params, server, force=True)
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

# ── Testing: helpers ──────────────────────────────────────────────────────────

async def _tcp_ping(host: str, port: int, timeout: float = 5) -> float | None:
    """Raw TCP connect. Returns latency in ms, or None if dead."""
    loop  = asyncio.get_running_loop()
    start = loop.time()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        latency_ms = (loop.time() - start) * 1000
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return latency_ms
    except Exception:
        return None


def convertible(configs: list[str]) -> list[tuple[str, dict]]:
    """[(uri, clash_proxy_dict)] for every config we can turn into a testable proxy."""
    out = []
    for cfg in configs:
        proxy = config_to_clash_proxy(cfg, "t")
        if proxy and proxy.get("server") and proxy.get("port"):
            out.append((cfg, proxy))
    return out

# ── Testing: real end-to-end test through xray / sing-box ─────────────────────

def _free_ports(n: int) -> list[int]:
    socks, ports = [], []
    for _ in range(n):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        socks.append(s)
        ports.append(s.getsockname()[1])
    for s in socks:
        s.close()
    return ports


def _xray_test_config(items: list, ports: list[int]) -> dict:
    """One inbound per config; routing sends inbound i -> outbound i."""
    inbounds, outbounds, rules = [], [], []
    for i, ((_cfg, _proxy, ob), port) in enumerate(zip(items, ports)):
        outbounds.append({**ob, "tag": f"o{i}"})
        inbounds.append({"tag": f"i{i}", "listen": "127.0.0.1", "port": port,
                         "protocol": "http", "settings": {}})
        rules.append({"type": "field", "inboundTag": [f"i{i}"], "outboundTag": f"o{i}"})
    outbounds.append({"tag": "direct", "protocol": "freedom", "settings": {}})
    return {"log": {"loglevel": "none"}, "inbounds": inbounds, "outbounds": outbounds,
            "routing": {"domainStrategy": "AsIs", "rules": rules}}


def _singbox_test_config(items: list, ports: list[int]) -> dict:
    inbounds, outbounds, rules = [], [], []
    for i, ((_cfg, _proxy, ob), port) in enumerate(zip(items, ports)):
        outbounds.append({**ob, "tag": f"o{i}"})
        inbounds.append({"type": "http", "tag": f"i{i}", "listen": "127.0.0.1", "listen_port": port})
        rules.append({"inbound": [f"i{i}"], "outbound": f"o{i}"})
    outbounds.append({"type": "direct", "tag": "direct"})
    return {"log": {"level": "error"},
            "dns": {"servers": [{"type": "local", "tag": "dns-local"}]},
            "inbounds": inbounds, "outbounds": outbounds,
            "route": {"rules": rules, "final": "direct", "default_domain_resolver": "dns-local"}}


async def _probe(port: int, timeout: float | None = None) -> float | None:
    """Fetch a generate_204 page THROUGH the proxy. Returns latency (ms) or None.
    Must be exactly HTTP 204: this rejects dead tunnels AND hijacking/captive proxies."""
    loop = asyncio.get_running_loop()
    for url in REAL_TEST_URLS:
        try:
            async with httpx.AsyncClient(proxy=f"http://127.0.0.1:{port}", timeout=timeout or REAL_TEST_TIMEOUT) as c:
                t0 = loop.time()
                r = await c.get(url)
                if r.status_code != 204:
                    continue
                first = (loop.time() - t0) * 1000       # includes tunnel/TLS setup
                try:                                     # warm round trip = fairer latency
                    t1 = loop.time()
                    r2 = await c.get(url)
                    if r2.status_code == 204:
                        return (loop.time() - t1) * 1000
                except Exception:
                    pass
                return first
        except Exception:
            continue
    return None


async def _wait_ready(proc, ports: list[int]) -> bool:
    deadline = asyncio.get_running_loop().time() + CORE_START_WAIT
    while asyncio.get_running_loop().time() < deadline:
        if proc.returncode is not None:
            return False
        res = await asyncio.gather(*(_tcp_ping("127.0.0.1", p, timeout=0.5) for p in ports))
        if all(r is not None for r in res):
            return True
        await asyncio.sleep(0.3)
    return False


async def _stop(proc) -> None:
    if proc.returncode is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), 3)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


async def _run_batch(kind: str, items: list, timeout: float | None = None) -> list[tuple[str, dict, float]]:
    """
    Load `items` [(uri, proxy, outbound)] into ONE core process and probe each through its own port.
    If the core refuses to start (one bad outbound poisons the whole config), split the batch
    in half and retry, so a single broken config can't take the others down with it.
    """
    ports = _free_ports(len(items))
    build = _xray_test_config if kind == "xray" else _singbox_test_config
    binary = XRAY_BIN if kind == "xray" else SINGBOX_BIN
    fd, path = tempfile.mkstemp(suffix=".json", prefix=f"{kind}_test_")
    with os.fdopen(fd, "w") as f:
        json.dump(build(items, ports), f)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "run", "-c", path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        if not await _wait_ready(proc, ports):
            await _stop(proc)
            if len(items) == 1:
                return []
            mid = len(items) // 2
            return await _run_batch(kind, items[:mid], timeout) + await _run_batch(kind, items[mid:], timeout)
        lats = await asyncio.gather(*(_probe(p, timeout) for p in ports))
    finally:
        if proc is not None:
            await _stop(proc)
        try:
            os.unlink(path)
        except OSError:
            pass
    return [(cfg, proxy, lat) for (cfg, proxy, _ob), lat in zip(items, lats) if lat is not None]


async def run_tests(configs: list[str]) -> list[tuple[str, dict, float]]:
    """-> [(uri, clash_proxy_dict, latency_ms)] of configs that really work, fastest first."""
    if not XRAY_BIN:
        print("⚠️  xray not found (install xray-core or set XRAY_BIN) — skipping tests.")
        return []

    items = []
    for cfg, proxy in convertible(configs):
        if proxy.get("type") == "hysteria2":
            ob = uri_to_singbox_outbound(cfg, "x") if SINGBOX_BIN else None
        else:
            ob = clash_proxy_to_xray_outbound(proxy, "x")
        if ob:
            items.append((cfg, proxy, ob))
    print(f"   {len(items)}/{len(configs)} configs testable")

    async def run_pass(pool: list, batch: int, timeout: float | None) -> list:
        sem = asyncio.Semaphore(CORE_PARALLEL)

        async def _go(kind: str, b: list):
            async with sem:
                return await _run_batch(kind, b, timeout)

        jobs = []
        for kind, lst in (("xray",    [i for i in pool if i[1].get("type") != "hysteria2"]),
                          ("singbox", [i for i in pool if i[1].get("type") == "hysteria2"])):
            jobs += [_go(kind, lst[k:k + batch]) for k in range(0, len(lst), batch)]
        return [r for part in await asyncio.gather(*jobs) for r in part]

    results = await run_pass(items, REAL_TEST_BATCH, None)
    if RETRY_FAILED:
        ok = {r[0] for r in results}
        failed = [i for i in items if i[0] not in ok]
        if failed:
            results += await run_pass(failed, RETRY_BATCH, RETRY_TIMEOUT)

    results.sort(key=lambda r: r[2])
    print(f"   ✅ {len(results)} configs carried real traffic")
    return results

# ── Xray "leastPing" balancer config ──────────────────────────────────────────

def build_xray_leastping_config(tested: list[tuple[str, dict, float]]) -> dict:
    outbounds: list[dict] = []
    for cfg, proxy, _lat in tested:
        if len(outbounds) >= MAX_BALANCER_SERVERS:
            break
        ob = clash_proxy_to_xray_outbound(proxy, tag=_remark(cfg) or f"mn_{len(outbounds) + 1}")
        if ob:
            outbounds.append(ob)

    proxy_tags = [ob["tag"] for ob in outbounds]
    outbounds.append({"tag": "direct", "protocol": "freedom", "settings": {}})
    outbounds.append({"tag": "block", "protocol": "blackhole", "settings": {}})

    balancer: dict = {"tag": "auto", "selector": ["mn_"], "strategy": {"type": "leastPing"}}
    if proxy_tags:
        balancer["fallbackTag"] = proxy_tags[0]       # defined behaviour before the first probe finishes

    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {"tag": "socks-in", "listen": "127.0.0.1", "port": 10808, "protocol": "socks",
             "settings": {"auth": "noauth", "udp": True},
             "sniffing": {"enabled": True, "destOverride": ["http", "tls"]}},
            {"tag": "http-in", "listen": "127.0.0.1", "port": 10809, "protocol": "http"},
        ],
        "outbounds": outbounds,
        "routing": {
            "domainStrategy": "AsIs",
            "balancers": [balancer],
            "rules": [{"type": "field", "network": "tcp,udp", "balancerTag": "auto"}],
        },
        "observatory": {
            "subjectSelector":   ["mn_"],
            "probeURL":          "https://www.gstatic.com/generate_204",
            "probeInterval":     OBSERVATORY_PROBE_INTERVAL,
            "enableConcurrency": True,
        },
    }

# ── sing-box profile ──────────────────────────────────────────────────────────

def build_singbox_config(tested: list) -> dict | None:
    outbounds: list[dict] = []
    for cfg, _proxy, _lat in tested:
        if len(outbounds) >= MAX_SINGBOX_SERVERS:
            break
        ob = uri_to_singbox_outbound(cfg, tag=_remark(cfg) or f"mn_{len(outbounds) + 1}")
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
    cfg = build_singbox_config(tested)
    if cfg is None:
        print(f"⚠️ sing-box: no convertible configs, {path} not written")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    n = len(cfg["outbounds"]) - 3          # minus selector, urltest, direct
    print(f"✅ Saved sing-box profile with {n} servers → {path}")

# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    print(f"🔍 Collecting V2Ray configs [{utc_now()}]")
    print(f"   Channels      : {len(CHANNELS)}")
    print(f"   External subs : {len(EXTERNAL_SUB_URLS)}")
    print(f"   Test cores    : xray={XRAY_BIN or 'NOT FOUND'}  sing-box={SINGBOX_BIN or 'not found'}\n")

    configs = asyncio.run(collect_all())
    if not configs:
        print("⚠️  No configs found — leaving existing outputs untouched.")
        return

    save_untested(configs)

    print(f"\n🧪 Testing {len(configs)} configs...")
    tested = save_tested(asyncio.run(run_tests(configs)))

    if tested:
        leastping_cfg = build_xray_leastping_config(tested)
        LEASTPING_OUTPUT_FILE.write_text(json.dumps(leastping_cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        included = sum(1 for o in leastping_cfg["outbounds"] if o["tag"].startswith("mn_"))
        print(f"✅ Saved LeastPing config → {LEASTPING_OUTPUT_FILE} ({included} servers, auto-switching)")
        save_singbox(tested, SINGBOX_OUTPUT_FILE)


if __name__ == "__main__":
    main()
