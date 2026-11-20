# -*- coding: utf-8 -*-
"""Сетевой уровень: управление прокси, VLESS (Xray / sing-box) и Amnezia.

Назначение модуля:
  * разбор и валидация HTTP / HTTPS / SOCKS5 и VLESS (`vless://...`) конфигураций;
  * локальный запуск клиентского туннеля Xray-core или sing-box для `vless://` ссылок
    (строго на 127.0.0.1:<ephemeral_port>, без открытия внешних портов);
  * диагностика и подключение локального клиента Amnezia (AmneziaWG / системный TUN
    или локальный SOCKS5/HTTP-порт Amnezia / Xray / sing-box / Tor);
  * выбор разрешённого маршрута при проверке доступности или сетевом сбое до отправки;
    лимиты площадок и возможные уже отправленные заявки не обходятся сменой IP;
  * безопасная загрузка пользовательских подписок (HTTPS, base64/список) с защитой от SSRF.
"""
from __future__ import annotations

import atexit
import base64
import contextlib
import hashlib
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuidlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

UA_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

# Локальные порты популярных клиентов (проверяются ТОЛЬКО на 127.0.0.1 по запросу)
LOCAL_PROXY_CANDIDATES: tuple[tuple[int, str, str], ...] = (
    (10808, "socks5", "Amnezia / Xray / v2rayN SOCKS5"),
    (10809, "http", "Amnezia / Xray / v2rayN HTTP"),
    (2080, "socks5", "Nekoray / Throne SOCKS5"),
    (12334, "socks5", "Hiddify SOCKS5"),
    (1080, "socks5", "Локальный SOCKS5 (Amnezia / SSH)"),
    (9050, "socks5", "Tor SOCKS5"),
    (9150, "socks5", "Tor Browser SOCKS5"),
)


@dataclass
class VlessSpec:
    """Разобранная конфигурация vless://UUID@host:port?...#remark."""
    raw: str
    uuid: str
    host: str
    port: int
    remark: str = ""
    network: str = "tcp"          # tcp | ws | grpc | httpupgrade | xhttp
    security: str = "none"        # none | tls | reality
    sni: str = ""
    fingerprint: str = "chrome"
    public_key: str = ""          # pbk (Reality)
    short_id: str = ""            # sid (Reality)
    spider_x: str = ""            # spx (Reality)
    flow: str = ""                # xtls-rprx-vision
    path: str = ""
    ws_host: str = ""
    service_name: str = ""        # gRPC serviceName
    alpn: list[str] = field(default_factory=list)
    allow_insecure: bool = False


@dataclass
class Endpoint:
    """Один сетевой маршрут (прямой выход, прокси, VLESS-туннель или локальный клиент)."""
    id: str
    kind: str                     # direct | http | socks5 | vless | amnezia_local | tor
    label: str
    proxy_url: str | None = None  # http://... или socks5://... (None = прямой выход)
    vless: VlessSpec | None = None
    ready: bool = True
    note: str = ""


def mask_proxy_url(url: str | None) -> str:
    """Скрывает логин/пароль или UUID в логах и отчётах."""
    if not url:
        return "direct"
    try:
        sp = urllib.parse.urlsplit(url)
        if sp.scheme.lower() == 'vless':
            return 'vless://***'
        if not sp.hostname:
            return 'proxy'
        port = f":{sp.port}" if sp.port else ""
        auth = "***@" if (sp.username or sp.password) else ""
        host = f'[{sp.hostname}]' if ':' in sp.hostname else sp.hostname
        return f"{sp.scheme}://{auth}{host}{port}"
    except Exception:
        return "proxy"


def parse_vless_uri(uri: str) -> VlessSpec:
    """Разбирает ссылку формата vless://uuid@host:port?params#remark."""
    s = (uri or "").strip()
    if not s.lower().startswith("vless://"):
        raise ValueError("Ожидается ссылка вида vless://uuid@host:port?...")
    sp = urllib.parse.urlsplit(s)
    uuid = urllib.parse.unquote(sp.username or "").strip()
    host = (sp.hostname or "").strip()
    port = sp.port or 443
    try:
        uuid = str(uuidlib.UUID(uuid))
    except (ValueError, AttributeError):
        raise ValueError("В VLESS-ссылке отсутствует корректный UUID")
    if not host:
        raise ValueError("В VLESS-ссылке отсутствует адрес сервера (host)")
    if not (1 <= int(port) <= 65535):
        raise ValueError("В VLESS-ссылке некорректный порт")

    qs = urllib.parse.parse_qs(sp.query, keep_blank_values=True)
    def q(name: str, default: str = "") -> str:
        vals = qs.get(name)
        return (vals[0] if vals else default).strip()

    network = (q("type", "tcp") or "tcp").lower()
    if network not in ("tcp", "ws", "grpc", "httpupgrade", "xhttp", "splithttp"):
        raise ValueError("Неизвестный транспорт VLESS")
    security = (q("security", "none") or "none").lower()
    if security not in ("none", "tls", "reality"):
        raise ValueError("Неизвестная настройка безопасности VLESS")
    sni = q("sni") or q("servername") or q("peer")
    fp = q("fp") or q("fingerprint") or "chrome"
    pbk = q("pbk") or q("publicKey")
    sid = q("sid") or q("shortId")
    spx = q("spx") or q("spiderX")
    flow = q("flow")
    path = q("path")
    ws_host = q("host")
    svc_name = q("serviceName")
    alpn_raw = q("alpn")
    alpn = [x.strip() for x in alpn_raw.split(",") if x.strip()] if alpn_raw else []
    insecure = q("allowInsecure") in ("1", "true", "yes")
    remark = urllib.parse.unquote(sp.fragment or "").strip()

    if security == "reality" and not pbk:
        raise ValueError("Для VLESS Reality необходим параметр pbk (publicKey)")

    return VlessSpec(
        raw=s,
        uuid=uuid,
        host=host,
        port=int(port),
        remark=remark,
        network=network,
        security=security,
        sni=sni,
        fingerprint=fp,
        public_key=pbk,
        short_id=sid,
        spider_x=spx,
        flow=flow,
        path=path,
        ws_host=ws_host,
        service_name=svc_name,
        alpn=alpn,
        allow_insecure=insecure,
    )


def build_xray_config(spec: VlessSpec, socks_port: int, http_port: int | None = None) -> dict[str, Any]:
    """Генерирует минимальный клиентский конфиг Xray-core для локального туннеля 127.0.0.1."""
    inbounds: list[dict[str, Any]] = [
        {
            "tag": "socks-in",
            "listen": "127.0.0.1",
            "port": int(socks_port),
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": False},
        }
    ]
    if http_port:
        inbounds.append(
            {
                "tag": "http-in",
                "listen": "127.0.0.1",
                "port": int(http_port),
                "protocol": "http",
                "settings": {},
            }
        )

    user: dict[str, Any] = {"id": spec.uuid, "encryption": "none"}
    if spec.flow:
        user["flow"] = spec.flow

    stream: dict[str, Any] = {"network": "xhttp" if spec.network == "splithttp" else spec.network}
    if spec.security == "reality":
        stream["security"] = "reality"
        stream["realitySettings"] = {
            "serverName": spec.sni or spec.host,
            "fingerprint": spec.fingerprint or "chrome",
            "publicKey": spec.public_key,
            "shortId": spec.short_id,
            "spiderX": spec.spider_x or "/",
        }
    elif spec.security == "tls":
        tls_cfg: dict[str, Any] = {
            "serverName": spec.sni or spec.ws_host or spec.host,
            "fingerprint": spec.fingerprint or "chrome",
            "allowInsecure": bool(spec.allow_insecure),
        }
        if spec.alpn:
            tls_cfg["alpn"] = spec.alpn
        stream["security"] = "tls"
        stream["tlsSettings"] = tls_cfg
    else:
        stream["security"] = "none"

    if spec.network == "ws":
        ws_cfg: dict[str, Any] = {"path": spec.path or "/"}
        if spec.ws_host:
            ws_cfg["headers"] = {"Host": spec.ws_host}
        stream["wsSettings"] = ws_cfg
    elif spec.network == "grpc":
        stream["grpcSettings"] = {"serviceName": spec.service_name}
    elif spec.network == "httpupgrade":
        stream["httpupgradeSettings"] = {"path": spec.path or "/", "host": spec.ws_host or spec.host}
    elif spec.network in ("xhttp", "splithttp"):
        stream["xhttpSettings"] = {"path": spec.path or "/", "host": spec.ws_host or spec.host}

    return {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": [
            {
                "tag": "proxy",
                "protocol": "vless",
                "settings": {"vnext": [{"address": spec.host, "port": spec.port, "users": [user]}]},
                "streamSettings": stream,
            },
            {"tag": "direct", "protocol": "freedom"},
        ],
    }


def build_singbox_config(spec: VlessSpec, mixed_port: int) -> dict[str, Any]:
    """Генерирует клиентский конфиг sing-box (mixed HTTP+SOCKS5 на 127.0.0.1)."""
    if spec.network in ('xhttp', 'splithttp'):
        raise ValueError('Для VLESS XHTTP требуется Xray; sing-box его здесь не поддерживает')
    outbound: dict[str, Any] = {
        "type": "vless",
        "tag": "proxy",
        "server": spec.host,
        "server_port": spec.port,
        "uuid": spec.uuid,
    }
    if spec.flow:
        outbound["flow"] = spec.flow
    if spec.security in ("tls", "reality"):
        tls_obj: dict[str, Any] = {
            "enabled": True,
            "server_name": spec.sni or spec.ws_host or spec.host,
            "insecure": bool(spec.allow_insecure),
            "utls": {"enabled": True, "fingerprint": spec.fingerprint or "chrome"},
        }
        if spec.alpn:
            tls_obj["alpn"] = spec.alpn
        if spec.security == "reality":
            tls_obj["reality"] = {
                "enabled": True,
                "public_key": spec.public_key,
                "short_id": spec.short_id,
            }
        outbound["tls"] = tls_obj
    if spec.network == "ws":
        tr: dict[str, Any] = {"type": "ws", "path": spec.path or "/"}
        if spec.ws_host:
            tr["headers"] = {"Host": spec.ws_host}
        outbound["transport"] = tr
    elif spec.network == "grpc":
        outbound["transport"] = {"type": "grpc", "service_name": spec.service_name}
    elif spec.network == "httpupgrade":
        outbound["transport"] = {"type": "httpupgrade", "path": spec.path or "/", "host": spec.ws_host or spec.host}

    return {
        "log": {"level": "warn"},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": int(mixed_port),
            }
        ],
        "outbounds": [outbound, {"type": "direct", "tag": "direct"}],
    }


def find_vless_runtime() -> tuple[str | None, str | None]:
    """Ищет установленный клиент для VLESS: ('xray'|'sing-box', путь) либо (None, None)."""
    env_xray = os.environ.get("VKFREE_XRAY_BIN", "").strip()
    if env_xray and Path(env_xray).is_file():
        return "xray", env_xray
    env_sb = os.environ.get("VKFREE_SINGBOX_BIN", "").strip()
    if env_sb and Path(env_sb).is_file():
        return "sing-box", env_sb
    for name in ("xray", "xray-core"):
        found = shutil.which(name)
        if found:
            return "xray", found
    found = shutil.which("sing-box")
    if found:
        return "sing-box", found
    return None, None


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _port_listening(port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


class VlessTunnelManager:
    """Управляет локальными процессами Xray / sing-box для VLESS-ссылок."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: dict[str, tuple[subprocess.Popen, int, Path]] = {}

    def ensure_tunnel(self, spec: VlessSpec) -> tuple[str | None, str]:
        """Поднимает локальный прокси для VLESS-спецификации. Возвращает (proxy_url, note)."""
        key = hashlib.sha256(spec.raw.encode("utf-8")).hexdigest()[:16]
        with self._lock:
            existing = self._procs.get(key)
            if existing:
                proc, port, _cfg = existing
                if proc.poll() is None and _port_listening(port):
                    return f"http://127.0.0.1:{port}", "VLESS-туннель активен"
                self._cleanup_one(key)

            engine_name, binary = find_vless_runtime()
            if not engine_name or not binary:
                return None, (
                    "Для прямого запуска vless:// нужен клиент xray или sing-box в PATH "
                    "(либо запустите VLESS в Amnezia / v2rayN / Nekoray — софт подхватит локальный порт 127.0.0.1:10808)"
                )

            # Конфиг содержит UUID и ключи. Создаём его с правами только владельца.
            tmp_dir = Path(tempfile.gettempdir()) / ('vkfree-vless-' + str(os.getuid()) if hasattr(os, 'getuid') else 'vkfree-vless')
            tmp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                tmp_dir.chmod(0o700)
            except OSError:
                pass
            fd, filename = tempfile.mkstemp(prefix='vless-', suffix='.json', dir=tmp_dir)
            os.close(fd)
            cfg_path = Path(filename)

            if engine_name == "xray":
                socks_port = _free_local_port()
                http_port = _free_local_port()
                cfg = build_xray_config(spec, socks_port=socks_port, http_port=http_port)
                cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
                cmd = [binary, "run", "-c", str(cfg_path)]
                listen_port = http_port
                proxy_url = f"http://127.0.0.1:{http_port}"
            else:
                mixed_port = _free_local_port()
                try:
                    cfg = build_singbox_config(spec, mixed_port=mixed_port)
                except ValueError as exc:
                    cfg_path.unlink(missing_ok=True)
                    return None, str(exc)
                cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
                cmd = [binary, "run", "-c", str(cfg_path)]
                listen_port = mixed_port
                proxy_url = f"http://127.0.0.1:{mixed_port}"

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                )
            except Exception as exc:
                cfg_path.unlink(missing_ok=True)
                return None, f"Не удалось запустить {engine_name}: {exc}"

            deadline = time.time() + 5.0
            while time.time() < deadline:
                if proc.poll() is not None:
                    err = ""
                    with contextlib.suppress(Exception):
                        err = (proc.stderr.read(400) or b"").decode("utf-8", errors="replace").strip()
                    cfg_path.unlink(missing_ok=True)
                    return None, f"{engine_name} завершился при старте: {err[:160] or 'ошибка конфига'}"
                if _port_listening(listen_port):
                    self._procs[key] = (proc, listen_port, cfg_path)
                    return proxy_url, f"VLESS ({engine_name}) на 127.0.0.1:{listen_port}"
                time.sleep(0.12)

            with contextlib.suppress(Exception):
                proc.terminate()
            cfg_path.unlink(missing_ok=True)
            return None, f"{engine_name} не открыл локальный порт за 5 с"

    def _cleanup_one(self, key: str) -> None:
        item = self._procs.pop(key, None)
        if not item:
            return
        proc, _port, cfg_path = item
        with contextlib.suppress(Exception):
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=2)
        with contextlib.suppress(Exception):
            cfg_path.unlink(missing_ok=True)

    def stop_all(self) -> None:
        with self._lock:
            for key in list(self._procs.keys()):
                self._cleanup_one(key)


VLESS_MANAGER = VlessTunnelManager()
atexit.register(VLESS_MANAGER.stop_all)


def detect_tunnel_interfaces() -> list[str]:
    """Проверяет наличие активных сетевых интерфейсов Amnezia / WireGuard / TUN в ОС."""
    names: list[str] = []
    try:
        for _idx, name in socket.if_nameindex():
            low = name.lower()
            if low.startswith(("amn", "awg", "wg", "tun", "tap", "utun", "ppp")):
                names.append(name)
    except Exception:
        net_sys = Path("/sys/class/net")
        if net_sys.exists():
            for p in net_sys.iterdir():
                low = p.name.lower()
                if low.startswith(("amn", "awg", "wg", "tun", "tap")):
                    names.append(p.name)
    return sorted(set(names))


def detect_local_proxies() -> list[Endpoint]:
    """Безопасная проверка локальных портов Amnezia / Xray / sing-box / Tor на 127.0.0.1."""
    found: list[Endpoint] = []
    seen_ports: set[int] = set()
    for port, proto, label in LOCAL_PROXY_CANDIDATES:
        if port in seen_ports:
            continue
        if _port_listening(port, timeout=0.18):
            seen_ports.add(port)
            kind = "tor" if port in (9050, 9150) else "amnezia_local"
            found.append(
                Endpoint(
                    id=f"local-{proto}-{port}",
                    kind=kind,
                    label=f"{label} (127.0.0.1:{port})",
                    proxy_url=f"{proto}://127.0.0.1:{port}",
                    ready=True,
                    note="обнаружен локальный прокси-клиент",
                )
            )
    return found


def parse_proxy_entries(raw: str) -> list[Endpoint]:
    """Разбирает пользовательский список прокси/VLESS (по строкам, запятым или base64-подписке)."""
    text = (raw or "").strip()
    if not text:
        return []

    # Если передан сплошной base64-блок подписки — декодируем
    if "://" not in text and re.fullmatch(r"[A-Za-z0-9+/=\s_-]{20,}", text):
        padded = text.strip().replace("-", "+").replace("_", "/")
        padded += "=" * ((4 - len(padded) % 4) % 4)
        with contextlib.suppress(Exception):
            decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
            if "://" in decoded:
                text = decoded

    # Запятая в параметре VLESS (например alpn=h2,http/1.1) — не разделитель.
    tokens = [t.strip() for t in re.split(r"[\r\n;]+|,(?=(?:https?|socks5h?|vless)://)", text, flags=re.I)
              if t.strip() and not t.strip().startswith("#")][:20]
    endpoints: list[Endpoint] = []
    seen: set[str] = set()

    for idx, tok in enumerate(tokens):
        if tok in seen:
            continue
        seen.add(tok)
        low = tok.lower()
        if low.startswith("vless://"):
            try:
                spec = parse_vless_uri(tok)
                proxy_url, note = VLESS_MANAGER.ensure_tunnel(spec)
                title = spec.remark or f"{spec.host}:{spec.port}"
                endpoints.append(
                    Endpoint(
                        id=f"vless-{idx}-{spec.host}:{spec.port}",
                        kind="vless",
                        label=f"VLESS {title}",
                        proxy_url=proxy_url,
                        vless=spec,
                        ready=bool(proxy_url),
                        note=note,
                    )
                )
            except ValueError as exc:
                endpoints.append(
                    Endpoint(
                        id=f"vless-bad-{idx}",
                        kind="vless",
                        label=f"VLESS #{idx + 1} (ошибка формата)",
                        proxy_url=None,
                        ready=False,
                        note=str(exc),
                    )
                )
            continue

        # Поддержка краткой записи host:port -> http://host:port
        candidate = tok if "://" in tok else f"http://{tok}"
        try:
            sp = urllib.parse.urlsplit(candidate)
            scheme = (sp.scheme or "").lower()
            if scheme not in ("http", "https", "socks5", "socks5h") or not sp.hostname or not sp.port:
                continue
            if sp.path not in ('', '/') or sp.query or sp.fragment:
                continue
        except ValueError:
            continue
        norm_scheme = "socks5" if scheme == "socks5h" else scheme
        kind = "socks5" if norm_scheme == "socks5" else "http"
        endpoints.append(
            Endpoint(
                id=f"{kind}-{idx}-{sp.hostname}:{sp.port}",
                kind=kind,
                label=f"{norm_scheme.upper()} {sp.hostname}:{sp.port}",
                proxy_url=candidate,
                ready=True,
                note="пользовательский прокси",
            )
        )
    return endpoints


def _public_addresses(hostname: str) -> list[str]:
    """Разрешает имя только если каждый DNS-ответ является публичным IP."""
    if not hostname or hostname.lower() in ('localhost', 'localhost.localdomain'):
        return []
    try:
        infos = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(info[4][0] for info in infos))
        def public(ip: str) -> bool:
            addr = ipaddress.ip_address(ip)
            # is_global у некоторых версий Python включает multicast.
            if (not addr.is_global or addr.is_multicast or addr.is_reserved or addr.is_unspecified
                    or addr.is_loopback or addr.is_link_local):
                return False
            mapped = getattr(addr, 'ipv4_mapped', None)
            return not mapped or (mapped.is_global and not mapped.is_multicast and not mapped.is_reserved)
        if not addresses or not all(public(ip) for ip in addresses):
            return []
        return addresses
    except (OSError, ValueError):
        return []


def _is_public_host(hostname: str) -> bool:
    return bool(_public_addresses(hostname))


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Соединяется с проверенным IP, сохраняя TLS-проверку исходного имени (защита от DNS rebinding)."""
    def __init__(self, hostname: str, address: str, timeout: float):
        super().__init__(hostname, 443, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self):
        raw = socket.create_connection((self._address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def fetch_subscription(url: str, timeout: float = 10.0) -> tuple[list[Endpoint], str]:
    """Пользовательская HTTPS-подписка: проверенный DNS, привязка IP, без редиректов."""
    u = (url or '').strip()
    if not u:
        return [], ''
    if len(u) > 2048 or any(ch.isspace() or ord(ch) < 32 for ch in u):
        return [], 'Некорректный адрес подписки'
    try:
        sp = urllib.parse.urlsplit(u)
        if sp.scheme.lower() != 'https' or not sp.hostname or sp.username or sp.password or sp.port not in (None, 443):
            return [], 'Подписка должна быть HTTPS-адресом на порту 443 без логина в URL'
        addresses = _public_addresses(sp.hostname)
    except ValueError:
        return [], 'Некорректный адрес подписки'
    if not addresses:
        return [], 'Адрес подписки отклонён (недоступен или указывает на локальную сеть)'
    conn = _PinnedHTTPSConnection(sp.hostname, addresses[0], timeout)
    try:
        path = urllib.parse.urlunsplit(('', '', sp.path or '/', sp.query, ''))
        conn.request('GET', path, headers={'User-Agent': 'VKFreeFarm/2.0', 'Accept': 'text/plain'})
        resp = conn.getresponse()
        if resp.status != 200:
            return [], f'Подписка вернула HTTP {resp.status}; перенаправления запрещены'
        raw = resp.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            return [], 'Подписка слишком большая'
        eps = parse_proxy_entries(raw.decode('utf-8', errors='replace'))
        return eps, f'Из подписки получено маршрутов: {len(eps)}'
    except Exception as exc:
        # В URL подписки часто есть токен; не помещаем его или подробности TLS в журнал.
        return [], f'Не удалось загрузить подписку ({type(exc).__name__})'
    finally:
        conn.close()


# ------------------------------------------------------------------ состояние и выбор маршрута

_BAD_LOCK = threading.Lock()
# (site_id, endpoint_id) -> (timestamp, reason)
_SITE_BAD_ENDPOINTS: dict[tuple[str, str], tuple[float, str]] = {}
BAD_TTL_SEC = 1800


def mark_endpoint_bad(site_id: str, endpoint_id: str, reason: str = "") -> None:
    if not site_id or not endpoint_id:
        return
    with _BAD_LOCK:
        _SITE_BAD_ENDPOINTS[(site_id, endpoint_id)] = (time.time(), reason[:160])


def clear_bad_endpoints(site_id: str | None = None) -> None:
    with _BAD_LOCK:
        if site_id is None:
            _SITE_BAD_ENDPOINTS.clear()
        else:
            for k in [k for k in _SITE_BAD_ENDPOINTS if k[0] == site_id]:
                _SITE_BAD_ENDPOINTS.pop(k, None)


def is_endpoint_bad(site_id: str, endpoint_id: str) -> bool:
    with _BAD_LOCK:
        rec = _SITE_BAD_ENDPOINTS.get((site_id, endpoint_id))
        if not rec:
            return False
        if time.time() - rec[0] > BAD_TTL_SEC:
            _SITE_BAD_ENDPOINTS.pop((site_id, endpoint_id), None)
            return False
        return True


def collect_endpoints(
    *,
    manual_raw: str = "",
    env_raw: str | None = None,
    autodetect_local: bool = True,
    include_direct: bool = True,
    prefer_proxy: bool = False,
    include_tor: bool = False,
) -> list[Endpoint]:
    """Собирает все доступные маршруты в порядке приоритета."""
    env_val = os.environ.get("VKFREE_PROXIES", "") if env_raw is None else env_raw
    combined = "\n".join(x for x in (manual_raw, env_val) if x)
    configured = [e for e in parse_proxy_entries(combined) if e.ready and e.proxy_url]
    local_eps = detect_local_proxies() if autodetect_local else []
    if not include_tor:
        local_eps = [e for e in local_eps if e.kind != 'tor']

    # Дедупликация по proxy_url
    seen_urls: set[str] = set()
    proxies: list[Endpoint] = []
    for ep in configured + local_eps:
        if ep.proxy_url and ep.proxy_url not in seen_urls:
            seen_urls.add(ep.proxy_url)
            proxies.append(ep)

    direct = Endpoint(id="direct", kind="direct", label="Прямое соединение", proxy_url=None, ready=True)
    if not include_direct:
        return proxies
    if prefer_proxy and proxies:
        return proxies + [direct]
    return [direct] + proxies


def endpoints_for_site(
    site_id: str,
    *,
    manual_raw: str = "",
    autodetect_local: bool = True,
    prefer_proxy: bool = False,
    include_tor: bool = False,
) -> list[Endpoint]:
    """Возвращает рабочие маршруты для конкретной площадки: сначала свежие, затем остальные."""
    all_eps = collect_endpoints(
        manual_raw=manual_raw,
        autodetect_local=autodetect_local,
        include_direct=True,
        prefer_proxy=prefer_proxy,
        include_tor=include_tor,
    )
    fresh = [e for e in all_eps if not is_endpoint_bad(site_id, e.id)]
    stale = [e for e in all_eps if is_endpoint_bad(site_id, e.id)]
    return fresh + stale


def diagnostics(manual_raw: str = "", autodetect_local: bool = True, *, include_env: bool = True) -> dict[str, Any]:
    """Диагностика; отключённая ротация не запускает VLESS из окружения."""
    vless_engine, vless_bin = find_vless_runtime()
    tunnels = detect_tunnel_interfaces()
    env_raw = os.environ.get("VKFREE_PROXIES", "") if include_env else ''
    parsed_manual = parse_proxy_entries("\n".join(x for x in (manual_raw, env_raw) if x))
    local_eps = detect_local_proxies() if autodetect_local else []
    return {
        "amnezia_or_vpn_interfaces": tunnels,
        "system_tunnel_active": bool(tunnels),
        "vless_runtime": vless_engine,
        "vless_binary": vless_bin,
        "configured_endpoints": [
            {
                "id": e.id,
                "kind": e.kind,
                "label": e.label,
                "proxy": mask_proxy_url(e.proxy_url),
                "ready": e.ready,
                "note": e.note,
            }
            for e in parsed_manual
        ],
        "local_detected": [
            {
                "id": e.id,
                "kind": e.kind,
                "label": e.label,
                "proxy": mask_proxy_url(e.proxy_url),
                "ready": e.ready,
            }
            for e in local_eps
        ],
        "recommendations": [
            "AmneziaVPN / AmneziaWG (режим системного туннеля или локальный SOCKS5 127.0.0.1:10808) — "
            "наиболее надёжный вариант с собственным VPS-IP без общих пулов.",
            "VLESS Reality (`vless://...`): вставьте ссылку в Настройках — при наличии `xray` или `sing-box` "
            "программа поднимет локальный туннель автоматически.",
            "Собственные или разрешённые HTTP/SOCKS5 прокси поддерживаются как альтернативные маршруты "
            "при сетевом сбое до отправки. Лимиты площадок сменой IP не обходятся.",
        ],
    }


# ------------------------------------------------------------------ HTTP-клиент с поддержкой прокси

def _curl_cffi_available() -> bool:
    try:
        import curl_cffi  # noqa: F401
        return True
    except Exception:
        return False


class _ForcedProxyHandler(urllib.request.ProxyHandler):
    """Явно выбранный прокси не обходится через NO_PROXY окружения."""
    def proxy_open(self, req, proxy, type):
        original = req.type
        proxy_type, user, password, hostport = urllib.request._parse_proxy(proxy)
        proxy_type = proxy_type or original
        if user is not None:
            raw = f'{urllib.parse.unquote(user)}:{urllib.parse.unquote(password or "")}'.encode()
            req.add_header('Proxy-authorization', 'Basic ' + base64.b64encode(raw).decode('ascii'))
        req.set_proxy(urllib.parse.unquote(hostport), proxy_type)
        if original == proxy_type or original == 'https':
            return None
        return self.parent.open(req, timeout=req.timeout)


def _socks_handlers(proxy: str) -> list[urllib.request.BaseHandler]:
    """Per-connection SOCKS5 для urllib без глобальной замены socket.socket."""
    import socks  # PySocks
    sp = urllib.parse.urlsplit(proxy)
    host, port = sp.hostname, sp.port or 1080
    user = urllib.parse.unquote(sp.username) if sp.username else None
    password = urllib.parse.unquote(sp.password) if sp.password else None

    def connect_via_socks(address, timeout):
        return socks.create_connection(address, timeout=timeout, proxy_type=socks.SOCKS5,
                                       proxy_addr=host, proxy_port=port, proxy_rdns=True,
                                       proxy_username=user, proxy_password=password)

    class HttpConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = connect_via_socks((self.host, self.port), self.timeout)

    class HttpsConnection(http.client.HTTPSConnection):
        def connect(self):
            raw = connect_via_socks((self.host, self.port), self.timeout)
            try:
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            except Exception:
                raw.close()
                raise

    class HttpHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(HttpConnection, req)

    class HttpsHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(HttpsConnection, req, context=self._context,
                                check_hostname=self._check_hostname)

    return [urllib.request.ProxyHandler({}), HttpHandler(), HttpsHandler()]


def http_request(
    url: str,
    *,
    method: str = "GET",
    data: dict[str, Any] | bytes | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    proxy: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Единый HTTP-запрос (curl_cffi с отпечатком Chrome -> фолбэк на urllib/PySocks).

    Возвращает словарь:
      {ok, status, text, url, cookies, backend, proxy, error}
    """
    hdrs = {
        "User-Agent": UA_CHROME,
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
    }
    if headers:
        hdrs.update(headers)

    if _curl_cffi_available():
        try:
            from curl_cffi import requests as creq  # type: ignore

            proxies = {"http": proxy, "https": proxy} if proxy else None
            # trust_env — настройка Session, а не отдельного get/post. Она не
            # допускает обход явного proxy через NO_PROXY и чужой proxy для direct.
            with creq.Session(trust_env=False) as session:
                if method.upper() == "POST":
                    r = session.post(url, data=data, headers=hdrs, cookies=cookies, proxies=proxies,
                                     impersonate="chrome", timeout=timeout, allow_redirects=True)
                else:
                    r = session.get(url, headers=hdrs, cookies=cookies, proxies=proxies,
                                    impersonate="chrome", timeout=timeout, allow_redirects=True)
                return {
                    "ok": 200 <= int(r.status_code) < 400,
                    "status": int(r.status_code),
                    "text": r.text,
                    "url": str(r.url),
                    "cookies": dict(r.cookies),
                    "backend": "curl_cffi",
                    "proxy": mask_proxy_url(proxy),
                    "error": "",
                }
        except Exception as exc:
            err_first = str(exc)[:220]
            if method.upper() != 'GET':
                # Запрос мог дойти до сервера: автоматический повтор POST опасен.
                return {'ok': False, 'status': None, 'text': '', 'url': url, 'cookies': {},
                        'backend': 'curl_cffi', 'proxy': mask_proxy_url(proxy),
                        'error': 'Нет ответа после отправки (' + type(exc).__name__ + ')'}
    else:
        err_first = ""

    # Фолбэк на стандартный urllib (+ PySocks для socks5://, если установлен)
    try:
        raw_body: bytes | None = None
        if isinstance(data, bytes):
            raw_body = data
        elif isinstance(data, dict):
            raw_body = urllib.parse.urlencode(data).encode("utf-8")
        if cookies:
            cookie_hdr = "; ".join(f"{k}={v}" for k, v in cookies.items())
            hdrs["Cookie"] = cookie_hdr
        req = urllib.request.Request(url, data=raw_body, headers=hdrs, method=method.upper())

        handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]
        if proxy:
            sp = urllib.parse.urlsplit(proxy)
            scheme = (sp.scheme or "").lower()
            if scheme in ("socks5", "socks5h"):
                handlers = _socks_handlers(proxy)
            elif scheme in ('http', 'https'):
                handlers = [_ForcedProxyHandler({'http': proxy, 'https': proxy})]
            else:
                raise ValueError('Неподдерживаемый протокол прокси')

        opener = urllib.request.build_opener(*handlers)
        with opener.open(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read(2 * 1024 * 1024)
            try:
                text = raw.decode(charset, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            return {
                "ok": 200 <= int(resp.status) < 400,
                "status": int(resp.status),
                "text": text,
                "url": str(resp.geturl()),
                "cookies": {},
                "backend": "urllib",
                "proxy": mask_proxy_url(proxy),
                "error": "",
            }
    except Exception as exc:
        err = err_first or str(exc)[:220]
        return {
            "ok": False,
            "status": None,
            "text": "",
            "url": url,
            "cookies": {},
            "backend": "curl_cffi" if err_first else "urllib",
            "proxy": mask_proxy_url(proxy),
            "error": err,
        }
