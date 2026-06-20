"""QQ 扫码登录：生成二维码、HTTP 轮询、提取 token。"""
import asyncio
import json
import logging
import os
import random
import re
import time
import urllib.parse
from datetime import datetime
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger("astrbot")

# ── 常量 ────────────────────────────────────────────────
LOGIN_URL_TEMPLATE = (
    "https://xui.ptlogin2.qq.com/cgi-bin/xlogin?pt_enable_pwd=1&appid=716027609"
    "&pt_3rd_aid=102061775&daid=381&pt_skey_valid=0&style=35&force_qr=1&autorefresh=1"
    "&s_url=http%3A%2F%2Fconnect.qq.com&refer_cgi=m_authorize&ucheck=1&fall_to_wv=1"
    "&status_os=12&redirect_uri=auth%3A%2F%2Ftauth.qq.com%2F&client_id=102061775"
    "&pf=openmobile_android&response_type=token&scope=all&sdkp=a&sdkv=3.5.17.lite"
    "&sign=a6479455d3e49b597350f13f776a6288&status_machine=MjMxMTdSSzY2Qw%3D%3D"
    "&switch=1&time=1763280194&show_download_ui=true"
    "&h5sig=trobryxo8IPM0GaSQH12mowKG-CY65brFzkK7_-9EW4&loginty=6"
)
PTQR_SHOW_URL = "https://xui.ptlogin2.qq.com/ssl/ptqrshow"
PTQR_LOGIN_URL = "https://xui.ptlogin2.qq.com/ssl/ptqrlogin"
OPENMOBILE_REDIRECT_URL = "https://openmobile.qq.com/oauth2.0/m_get_redirect_url"
PTQR_AID = "716027609"
PTQR_DAID = "381"
PTQR_THIRD_AID = "102061775"
DEFAULT_CALLBACK_URL = "http://connect.qq.com"
DEFAULT_U1_URL = "http://connect.qq.com"


class QQLoginFlow:
    """QQ 扫码登录流程。

    可通过 config 字典传入 login_callback_url / login_u1_url（对应原插件的 _get_login_callback_url / _get_login_u1_url）。
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self._callback_url = ""
        self._u1_url = ""

    @property
    def callback_url(self) -> str:
        if not self._callback_url:
            value = str(self.config.get("login_callback_url", DEFAULT_CALLBACK_URL) or DEFAULT_CALLBACK_URL)
            self._callback_url = self._normalize_url(value)
        return self._callback_url

    @property
    def u1_url(self) -> str:
        if not self._u1_url:
            value = str(self.config.get("login_u1_url", DEFAULT_U1_URL) or DEFAULT_U1_URL)
            self._u1_url = self._normalize_url(value)
        return self._u1_url

    # ── 工具方法 ─────────────────────────────────────────

    @staticmethod
    def _normalize_url(value: str) -> str:
        url = (value or "").strip()
        if not url:
            return ""
        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = f"https://{url.lstrip('/')}"
        return url

    @staticmethod
    def _get_cookie_value(session: aiohttp.ClientSession, url: str, name: str) -> str:
        try:
            cookies = session.cookie_jar.filter_cookies(url)
            cookie = cookies.get(name)
            if cookie:
                return cookie.value
        except Exception:
            pass
        return ""

    @staticmethod
    def _calc_ptqrtoken(qrsig: str) -> int:
        token = 0
        for ch in qrsig:
            token += (token << 5) + ord(ch)
        return token & 2147483647

    @staticmethod
    def _parse_ptui_callback(text: str) -> Optional[Dict[str, str]]:
        match = re.search(r"ptuiCB\('([^']*)','([^']*)','([^']*)','([^']*)','([^']*)'", text)
        if not match:
            return None
        redirect_url = match.group(3).replace("\\/", "/").replace("\\x26", "&")
        return {"code": match.group(1), "redirect_url": redirect_url, "message": match.group(5)}

    @staticmethod
    def _extract_jsver_from_login_page(login_page: str) -> str:
        text = login_page or ""
        patterns = [
            r"/monorepo/([0-9A-Za-z]+)/ptlogin/js/login_10\.js",
            r"/monorepo/([0-9A-Za-z]+)/ptlogin/js/",
            r"https://qq-web\.cdn-go\.cn/monorepo/([0-9A-Za-z]+)",
        ]
        for pattern in patterns:
            m = re.search(pattern, text)
            if m:
                return m.group(1)
        return "28d22679"

    @staticmethod
    def _build_aegis_uid(session: aiohttp.ClientSession) -> str:
        aegis_uid = QQLoginFlow._get_cookie_value(session, "https://xui.ptlogin2.qq.com", "__aegis_uid")
        if aegis_uid:
            return aegis_uid
        server_ip = QQLoginFlow._get_cookie_value(session, "https://xui.ptlogin2.qq.com", "pt_serverip")
        client_ip = QQLoginFlow._get_cookie_value(session, "https://xui.ptlogin2.qq.com", "pt_clientip")
        if server_ip and client_ip:
            return f"{server_ip}-{client_ip}-4458"
        return ""

    @staticmethod
    def _build_pt_openlogin_data(login_url: str, session: aiohttp.ClientSession) -> str:
        parsed = urllib.parse.urlparse(login_url)
        query_map = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        def q(name: str, default: str = "") -> str:
            values = query_map.get(name, [])
            return values[0] if values else default

        tid = QQLoginFlow._get_cookie_value(session, "https://xui.ptlogin2.qq.com", "idt") or str(int(time.time()))
        auth_time = str(int(time.time() * 1000))
        items = [
            ("which", ""), ("refer_cgi", q("refer_cgi", "m_authorize")),
            ("response_type", q("response_type", "token")),
            ("client_id", q("client_id", PTQR_THIRD_AID)), ("state", ""),
            ("display", ""), ("openapi", "1011"),
            ("switch", q("switch", "1")), ("src", "1"),
            ("sdkv", q("sdkv", "3.5.17.lite")), ("sdkp", q("sdkp", "a")),
            ("tid", tid), ("pf", q("pf", "openmobile_android")),
            ("need_pay", "0"), ("browser", "0"),
            ("browser_error", ""), ("serial", ""), ("token_key", ""),
            ("redirect_uri", q("redirect_uri", "auth://tauth.qq.com/")),
            ("sign", q("sign", "")), ("time", q("time", "")),
            ("status_version", ""),
            ("status_os", q("status_os", "12")),
            ("status_machine", q("status_machine", "")),
            ("page_type", "1"), ("has_auth", "1"), ("update_auth", "1"),
            ("auth_time", auth_time), ("loginfrom", ""),
            ("h5sig", q("h5sig", "")), ("loginty", q("loginty", "6")),
        ]
        return urllib.parse.urlencode(items)

    @staticmethod
    def _extract_auth_url_from_callback_body(text: str) -> str:
        if not text:
            return ""
        callback_match = re.search(r"_Callback\s*\(\s*(\{.*?\})\s*\)\s*;?\s*$", text, re.DOTALL)
        if callback_match:
            try:
                payload = json.loads(callback_match.group(1))
                callback_url = str(payload.get("url", "") or "").strip()
                if callback_url.startswith("auth://"):
                    return callback_url
            except Exception:
                pass
        auth_match = re.search(r"(auth://tauth\.qq\.com/[^\s\"'<>]+)", text)
        if auth_match:
            return auth_match.group(1)
        return ""

    @staticmethod
    def _extract_url_from_body(body: str) -> str:
        text = (body or "").replace("\\/", "/").replace("\\x26", "&")
        patterns = [
            r"ptuiCB\('[^']*','[^']*','([^']+)'",
            r"ptui_auth_CB\('[^']*','[^']*','([^']+)'",
            r"location\.href\s*=\s*['\"]([^'\"]+)['\"]",
            r"location\.replace\(\s*['\"]([^'\"]+)['\"]\s*\)",
            r"window\.location\s*=\s*['\"]([^'\"]+)['\"]",
            r"(auth://tauth\.qq\.com/[^\s\"'<>]+)",
            r"(https?://imgcache\.qq\.com/[^\s\"'<>]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return ""

    # ── 主流程 ───────────────────────────────────────────

    def _build_login_url(self) -> str:
        encoded_callback = urllib.parse.quote(self.callback_url, safe="")
        if "s_url=" not in LOGIN_URL_TEMPLATE:
            return LOGIN_URL_TEMPLATE
        return re.sub(
            r"([?&])s_url=[^&]*",
            lambda m: f"{m.group(1)}s_url={encoded_callback}",
            LOGIN_URL_TEMPLATE, count=1,
        )

    async def generate_qr_code(self) -> Optional[Dict[str, Any]]:
        """生成登录二维码，返回包含 session / filename / ptqrtoken 等的上下文。"""
        logger.info("[HTTP登录] 开始生成二维码")

        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        login_url = self._build_login_url()
        logger.info(f"[HTTP登录] 使用回调参数: s_url={self.callback_url}, u1={self.u1_url}")

        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 12; 23117RK66C Build/V417IR; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/101.0.4951.61 Mobile Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://openmobile.qq.com/",
            "X-Requested-With": "com.tencent.apps.valorant",
            "Cookie": "accountType=5; clientType=9",
        }

        try:
            # 访问 xlogin 初始化会话
            async with session.get(login_url, headers=headers) as response:
                response.raise_for_status()
                login_page = await response.text(errors="ignore")
                logger.info(f"[HTTP登录] xlogin status={response.status}, len={len(login_page)}")

            login_sig = ""
            login_sig_match = re.search(r"g_login_sig=encodeURIComponent\(\"([^\"]+)\"\)", login_page)
            if login_sig_match:
                login_sig = login_sig_match.group(1)
            if not login_sig:
                login_sig = self._get_cookie_value(session, "https://xui.ptlogin2.qq.com", "pt_login_sig")
            if not login_sig:
                login_sig = self._get_cookie_value(session, "https://ssl.ptlogin2.qq.com", "pt_login_sig")

            parsed_login_url = urllib.parse.urlparse(login_url)
            login_query_map = urllib.parse.parse_qs(parsed_login_url.query, keep_blank_values=True)
            pt_uistyle = login_query_map.get("style", ["35"])[0] or "35"
            ptlang = login_query_map.get("ptlang", ["2052"])[0] or "2052"
            jsver = self._extract_jsver_from_login_page(login_page)
            pt_openlogin_data = self._build_pt_openlogin_data(login_url, session)
            aegis_uid = self._build_aegis_uid(session)

            # 请求二维码图片
            qr_params = {
                "s": "8", "e": "0", "appid": PTQR_AID, "type": "0",
                "t": str(random.random()), "u1": self.u1_url,
                "daid": PTQR_DAID, "pt_3rd_aid": PTQR_THIRD_AID,
            }
            qr_headers = {
                "User-Agent": headers["User-Agent"],
                "Referer": login_url,
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "X-Requested-With": "com.tencent.apps.valorant",
            }
            async with session.get(PTQR_SHOW_URL, params=qr_params, headers=qr_headers) as response:
                response.raise_for_status()
                qr_image_bytes = await response.read()

            if not qr_image_bytes:
                raise RuntimeError("二维码内容为空")

            qrsig = self._get_cookie_value(session, "https://xui.ptlogin2.qq.com", "qrsig")
            if not qrsig:
                qrsig = self._get_cookie_value(session, "https://ssl.ptlogin2.qq.com", "qrsig")
            if not qrsig:
                raise RuntimeError("未获取到 qrsig")

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"qr_code_http_{timestamp}.png"
            with open(filename, "wb") as f:
                f.write(qr_image_bytes)

            logger.info("[HTTP登录] 二维码生成成功")
            return {
                "session": session,
                "filename": filename,
                "ptqrtoken": self._calc_ptqrtoken(qrsig),
                "login_sig": login_sig,
                "login_url": login_url,
                "u1_url": self.u1_url,
                "callback_url": self.callback_url,
                "pt_openlogin_data": pt_openlogin_data,
                "aegis_uid": aegis_uid,
                "jsver": jsver,
                "pt_uistyle": pt_uistyle,
                "ptlang": ptlang,
            }

        except Exception as e:
            logger.warning(f"[HTTP登录] 生成二维码失败: {type(e).__name__}, {e}")
            await session.close()
            return None

    async def wait_for_login_result(
        self,
        session: aiohttp.ClientSession,
        ptqrtoken: int,
        login_sig: str,
        login_u1: str,
        referer_url: str,
        pt_openlogin_data: str = "",
        aegis_uid: str = "",
        jsver: str = "28d22679",
        pt_uistyle: str = "35",
        ptlang: str = "2052",
        timeout: int = 30,
    ) -> Optional[Dict[str, Any]]:
        """轮询二维码登录状态并提取 openid/access_token。"""
        logger.info(
            f"[HTTP登录] 开始轮询: ptqrtoken={ptqrtoken}, "
            f"login_sig={'有' if login_sig else '无'}, u1={login_u1}"
        )
        poll_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 12; 23117RK66C Build/V417IR; wv) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                "Chrome/101.0.4951.61 Mobile Safari/537.36 tencent_game_emulator"
            ),
            "Referer": referer_url, "Accept": "*/*",
            "X-Requested-With": "com.tencent.apps.valorant",
        }

        start_time = time.time()
        poll_index = 0
        while time.time() - start_time < timeout:
            poll_index += 1
            try:
                params = {
                    "u1": login_u1, "from_ui": "1", "type": "1",
                    "ptlang": str(ptlang or "2052"),
                    "ptqrtoken": str(ptqrtoken),
                    "daid": PTQR_DAID, "aid": PTQR_AID,
                    "pt_3rd_aid": PTQR_THIRD_AID,
                    "pt_openlogin_data": pt_openlogin_data,
                    "device": "2", "ptopt": "1",
                    "pt_uistyle": str(pt_uistyle or "35"),
                    "jsver": str(jsver or "28d22679"),
                    "r": str(random.random()),
                }
                if login_sig:
                    params["login_sig"] = login_sig
                if aegis_uid:
                    params["aegis_uid"] = aegis_uid

                async with session.get(PTQR_LOGIN_URL, params=params, headers=poll_headers) as response:
                    response.raise_for_status()
                    text = await response.text(errors="ignore")

                callback = self._parse_ptui_callback(text)
                if not callback:
                    await asyncio.sleep(2)
                    continue

                code = callback["code"]
                redirect_url = callback.get("redirect_url", "")

                if code == "0":
                    success_url = redirect_url
                    login_data = self._extract_login_data_from_success_url(success_url)

                    if not (login_data.get("openid") and login_data.get("access_token")):
                        # 尝试 check_sig 解析
                        resolved_url = await self._resolve_login_success_url(session, success_url, referer_url)
                        if resolved_url and resolved_url != success_url:
                            resolved_data = self._extract_login_data_from_success_url(resolved_url)
                            login_data = self._merge_login_data(login_data, resolved_data)

                        # 尝试 keystr 候选
                        candidate_url = resolved_url if resolved_url else success_url
                        key_candidates = self._collect_redirect_key_candidates(session, login_data, candidate_url)
                        for keystr, source in key_candidates:
                            auth_url = await self._fetch_auth_url_by_redirect_key(session, keystr)
                            if not auth_url:
                                continue
                            auth_data = self._extract_login_data_from_success_url(auth_url)
                            login_data = self._merge_login_data(login_data, auth_data)
                            if login_data.get("openid") and login_data.get("access_token"):
                                break

                    if login_data.get("openid") and login_data.get("access_token"):
                        logger.info("[HTTP登录] HTTP登录成功，已拿到 openid/access_token")
                        return login_data
                    return None

                if code == "65":
                    logger.warning(f"[HTTP登录] 二维码已失效")
                    return None
                if code in ("66", "67"):
                    await asyncio.sleep(2)
                    continue
                await asyncio.sleep(2)

            except Exception as e:
                logger.warning(f"[HTTP登录] 轮询异常: {type(e).__name__}, {e}")
                await asyncio.sleep(2)

        logger.warning("[HTTP登录] 轮询超时")
        return None

    # ── 内部辅助 ─────────────────────────────────────────

    @staticmethod
    def _extract_login_data_from_success_url(success_url: str) -> Dict[str, Any]:
        def normalize_url(url: str) -> str:
            return (url or "").replace("\\/", "/").replace("\\x26", "&").strip()

        def parse_param_str(raw: str) -> Dict[str, str]:
            parsed: Dict[str, str] = {}
            if not raw:
                return parsed
            part = raw.replace("#&", "&").lstrip("&")
            for key, value in urllib.parse.parse_qs(part, keep_blank_values=True).items():
                if value:
                    parsed[key] = value[0]
            return parsed

        nested_keys = {"u1", "url", "jump_url", "redirect_uri", "redirect_url",
                       "target_url", "s_url", "f_url", "qtarget", "jump", "ru"}
        merged_params: Dict[str, str] = {}
        queue = [normalize_url(success_url)]
        visited = set()

        while queue:
            candidate = queue.pop(0)
            if not candidate or candidate in visited:
                continue
            visited.add(candidate)
            decoded = candidate
            for _ in range(3):
                next_decoded = urllib.parse.unquote(decoded)
                if next_decoded == decoded:
                    break
                decoded = next_decoded
            parsed_url = urllib.parse.urlparse(decoded)
            candidate_params: Dict[str, str] = {}
            for raw_part in (parsed_url.query, parsed_url.fragment):
                candidate_params.update(parse_param_str(raw_part))
            if not candidate_params and ("openid=" in decoded or "access_token=" in decoded):
                candidate_params.update(parse_param_str(decoded))
            for key, value in candidate_params.items():
                if key not in merged_params:
                    merged_params[key] = value
            for nested_key in nested_keys:
                nested_value = candidate_params.get(nested_key, "")
                if nested_value and nested_value not in visited:
                    queue.append(normalize_url(nested_value))

        return {
            "openid": merged_params.get("openid", ""),
            "appid": merged_params.get("appid", ""),
            "access_token": merged_params.get("access_token", ""),
            "pay_token": merged_params.get("pay_token", ""),
            "key": merged_params.get("key", ""),
            "redirect_uri_key": merged_params.get("redirect_uri_key", ""),
            "expires_in": merged_params.get("expires_in", "7776000"),
            "pf": merged_params.get("pf", "openmobile_android"),
            "status_os": merged_params.get("status_os", "12"),
            "status_machine": merged_params.get("status_machine", ""),
            "full_params": merged_params,
        }

    @staticmethod
    def _merge_login_data(base_data: Dict[str, Any], extra_data: Dict[str, Any]) -> Dict[str, Any]:
        base = dict(base_data or {})
        extra = dict(extra_data or {})
        merged_params: Dict[str, str] = dict(base.get("full_params", {}) or {})
        merged_params.update(extra.get("full_params", {}) or {})
        for key in ("openid", "appid", "access_token", "pay_token", "key",
                     "redirect_uri_key", "expires_in", "pf", "status_os", "status_machine"):
            if not base.get(key) and extra.get(key):
                base[key] = extra[key]
        base["full_params"] = merged_params
        return base

    @staticmethod
    def _collect_redirect_key_candidates(
        session: aiohttp.ClientSession,
        login_data: Dict[str, Any],
        success_url: str,
    ) -> list:
        result = []
        seen = set()

        def add_key(value: str, source: str):
            keystr = (value or "").strip()
            if not keystr or keystr in seen:
                return
            seen.add(keystr)
            result.append((keystr, source))

        full_params = (login_data or {}).get("full_params", {}) or {}
        for key_name in ("redirect_uri_key", "keystr", "key", "uikey", "superkey", "supertoken"):
            add_key(str(full_params.get(key_name, "")), f"param:{key_name}")

        normalized_url = (success_url or "").replace("\\/", "/").replace("\\x26", "&")
        parsed = urllib.parse.urlparse(normalized_url)
        raw_parts = [parsed.query, parsed.fragment]
        if not parsed.query and not parsed.fragment:
            raw_parts.append(normalized_url)
        for raw in raw_parts:
            if not raw:
                continue
            raw_params = urllib.parse.parse_qs(raw.replace("#&", "&"), keep_blank_values=True)
            for key_name in ("redirect_uri_key", "keystr", "key", "uikey", "superkey", "supertoken"):
                values = raw_params.get(key_name, [])
                if values:
                    add_key(values[0], f"url:{key_name}")

        cookie_domains = [
            "https://xui.ptlogin2.qq.com", "https://ssl.ptlogin2.qq.com",
            "https://ptlogin4.openmobile.qq.com", "https://openmobile.qq.com",
            "https://connect.qq.com",
        ]
        for domain in cookie_domains:
            host = urllib.parse.urlparse(domain).netloc
            for key_name in ("redirect_uri_key", "keystr", "uikey", "superkey", "supertoken", "key"):
                add_key(QQLoginFlow._get_cookie_value(session, domain, key_name), f"cookie:{host}:{key_name}")
        return result

    @staticmethod
    async def _fetch_auth_url_by_redirect_key(
        session: aiohttp.ClientSession, redirect_uri_key: str,
    ) -> str:
        keystr = (redirect_uri_key or "").strip()
        if not keystr:
            return ""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 12; 23117RK66C Build/V417IR; wv) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                "Chrome/101.0.4951.61 Mobile Safari/537.36 tencent_game_emulator"
            ),
            "Accept": "*/*", "Referer": "https://imgcache.qq.com/",
        }
        try:
            async with session.get(
                OPENMOBILE_REDIRECT_URL, params={"keystr": keystr}, headers=headers,
                timeout=aiohttp.ClientTimeout(total=20, connect=10, sock_connect=10, sock_read=15),
            ) as response:
                body = await response.text(errors="ignore")
                if response.status != 200:
                    return ""
                auth_url = QQLoginFlow._extract_auth_url_from_callback_body(body)
                return auth_url
        except Exception:
            return ""

    @staticmethod
    async def _resolve_login_success_url(
        session: aiohttp.ClientSession, success_url: str, referer_url: str = "",
    ) -> str:
        current_url = (success_url or "").replace("\\/", "/").replace("\\x26", "&").strip()
        if not current_url or "check_sig" not in current_url:
            return current_url
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 12; 23117RK66C Build/V417IR; wv) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                "Chrome/101.0.4951.61 Mobile Safari/537.36 tencent_game_emulator"
            ),
            "Accept": "*/*",
            "Referer": referer_url or "https://openmobile.qq.com/",
        }
        try:
            async with session.get(
                current_url, headers=headers, allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=15, connect=8, sock_connect=8, sock_read=10),
            ) as response:
                body = await response.text(errors="ignore")
                location = (response.headers.get("Location", "") or "").strip()
                if location:
                    return urllib.parse.urljoin(str(response.url), location)
                body_url = QQLoginFlow._extract_url_from_body(body)
                if body_url:
                    return body_url
        except Exception:
            pass
        return current_url
