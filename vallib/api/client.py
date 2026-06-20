"""通用 API 客户端：HTTP、SSL、Cookie 构造、重试。"""
import asyncio
import logging
import ssl as _sslmod
import time
from typing import Any, Dict, Optional, Tuple

import aiohttp

logger = logging.getLogger("astrbot")

API_BASE = "https://app.mval.qq.com"
UA = (
    "mval/2.4.0.10053 Channel/10068 Manufacturer/Xiaomi "
    "Mozilla/5.0 (Linux; Android 14; 23117RK66C Build/V417IR; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/101.0.4951.61 Mobile Safari/537.36"
)

AUTH_INVALID_CODES = {1001, 1003, 999999}


class APIClient:
    """无畏契约 API 客户端。

    两种请求头构造模式：
    - 商店接口（store）：与战绩接口 Cookie 格式不同
    - 通用接口（default / real_token / scene）
    """

    @staticmethod
    def build_headers(user_config: Dict[str, Any], cookie_variant: str = "default") -> Dict[str, str]:
        """构造战绩等通用接口请求头。"""
        login_type = user_config.get("login_type", "qq")
        if login_type == "wx":
            acctype = "wx"
            access_token = user_config.get("access_token", "")
            uin_part = ""
        else:
            acctype = "pt"
            access_token = user_config.get("access_token", "") if cookie_variant == "real_token" else "null"
            uin_part = f"uin=o{user_config.get('uin', 0)}; "

        parts = [
            "clientType=9",
            f"{uin_part}appid=102061775",
            f"acctype={acctype}",
        ]
        if access_token and access_token != "null":
            parts.append(f"openid={user_config.get('openid', '')}")
            parts.append(f"access_token={access_token}")
        else:
            parts.append(f"openid={user_config.get('openid', '')}")
            parts.append("access_token=null")
        parts.extend([f"userId={user_config['userId']}", "accountType=5", f"tid={user_config['tid']}"])
        if cookie_variant == "scene":
            parts.append(f"scene={user_config.get('access_token', '')}")

        return {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/json",
            "User-Agent": UA,
            "Connection": "keep-alive",
            "Cookie": "; ".join(parts),
        }

    @staticmethod
    def build_store_headers(user_config: Dict[str, Any]) -> Dict[str, str]:
        """构造商店接口专用请求头。"""
        login_type = user_config.get('login_type', 'qq')
        if login_type == 'wx':
            acctype = 'wx'
            openid = user_config.get('openid', '')
            access_token = user_config.get('access_token', '')
            uin_part = ""
        else:
            acctype = 'pt'
            openid = user_config.get('openid', '')
            access_token = "null"
            uin_part = "uin=o105940478; "

        cookie = (
            "clientType=9; "
            f"{uin_part}"
            "appid=102061775; "
            f"acctype={acctype};"
            f"openid={openid}; "
            f"access_token={access_token}; "
            f"userId={user_config['userId']}; "
            "accountType=5; "
            f"tid={user_config['tid']}"
        )
        return {
            "Accept": "*/*",
            "Upload-Draft-Interop-Version": "5",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Content-Type": "application/json",
            "User-Agent": "mval/2.3.0.10050 Channel/5 Manufacturer/Xiaomi  Mozilla/5.0 (Linux; Android 14; 23078RKD5C Build/UP1A.230905.011; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/140.0.7339.207 Mobile Safari/537.36",
            "Connection": "keep-alive",
            "Upload-Complete": "?1",
            "GH-HEADER": "1-2-105-160-0",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Cookie": cookie,
        }

    @staticmethod
    def is_auth_invalid(result_code: Any, err_msg: str) -> bool:
        """判断是否登录凭证失效。"""
        err_msg_lower = (err_msg or "").lower()
        return (
            result_code in AUTH_INVALID_CODES
            or "ticket expire" in err_msg_lower
            or "auth web ticket fail" in err_msg_lower
        )

    async def post(self, path: str, user: Dict, body=None, timeout: int = 20,
                   retries: int = 2, cookie_variant: str = "default") -> Tuple[Optional[Dict], Optional[str]]:
        """POST 请求，返回 (data, err_msg)。"""
        url = f"{API_BASE}{path}"
        headers = self.build_headers(user, cookie_variant)
        body = body or {"_t": int(time.time())}

        ssl_ctx = _sslmod.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = _sslmod.CERT_NONE

        for attempt in range(retries):
            try:
                async with aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(ssl=ssl_ctx)
                ) as s:
                    async with s.post(url, headers=headers, json=body,
                                      timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                        resp.raise_for_status()
                        data = await resp.json()
                        code = data.get("result", -1)
                        if code != 0:
                            msg = data.get("errMsg") or data.get("msg") or f"错误码 {code}"
                            logger.info(f"[ValAPI] {code}: {msg}")
                            if self.is_auth_invalid(code, msg):
                                return None, f"登录过期（{msg}），请重新绑定"
                            return None, f"{msg} (code={code})"
                        return data.get("data"), None
            except aiohttp.ClientError as e:
                if attempt >= retries - 1:
                    return None, f"网络错误: {e}"
                await asyncio.sleep(1)
        return None, "已达最大重试次数"

    async def get(self, path: str, user: Dict, query: str = "", timeout: int = 20,
                  cookie_variant: str = "default") -> Tuple[Optional[Dict], Optional[str]]:
        """GET 请求，返回 (data, err_msg)。"""
        url = f"{API_BASE}{path}"
        if query:
            url = f"{url}?{query}" if "?" not in url else f"{url}&{query}"
        headers = self.build_headers(user, cookie_variant)

        ssl_ctx = _sslmod.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = _sslmod.CERT_NONE

        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=ssl_ctx)
            ) as s:
                async with s.get(url, headers=headers,
                                 timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status in (404, 405):
                        return None, f"{resp.status}"
                    resp.raise_for_status()
                    data = await resp.json()
                    code = data.get("result", -1)
                    if code != 0:
                        return None, data.get("errMsg") or data.get("msg") or f"错误码 {code}"
                    return data.get("data"), None
        except aiohttp.ClientError as e:
            return None, f"网络错误: {e}"
