"""微信扫码登录流程。"""
import asyncio
import hashlib
import json
import logging
import random
import re
import string
import time
from typing import Any, Dict, Optional

import aiohttp

from ..api.auth import call_login_by_wechat

logger = logging.getLogger("astrbot")

WECHAT_APP_ID = "wxcbb49f1f39656c2a"
WECHAT_QRCONNECT_URL = "https://open.weixin.qq.com/connect/sdk/qrconnect"
WECHAT_LONG_POLL_URL = "https://long.open.weixin.qq.com/connect/l/qrconnect"


class WechatLoginFlow:
    """微信扫码登录流程。"""

    @staticmethod
    async def get_qr_code() -> Optional[Dict[str, Any]]:
        """获取微信登录二维码。返回包含 uuid, qrcode_base64 的字典。"""
        timestamp = str(int(time.time()))
        noncestr = ''.join(random.choices(string.ascii_letters + string.digits, k=6))

        async with aiohttp.ClientSession() as session:
            # 获取 sdk_ticket
            ticket_url = "https://app.mval.qq.com/go/auth/get_sdk_ticket"
            ticket_payload = {
                "clienttype": 9,
                "config_params": {"client_dev_name": "22041216C", "lang_type": 0},
                "mappid": 10200,
                "mcode": "69028af6dca2c107f4f58290100011b1a303",
                "sdk_appid": WECHAT_APP_ID,
                "source_game_zone": "agame",
                "game_zone": "agame",
            }
            ticket_headers = {
                "user-agent": "mval/2.10062 Channel/3 Manufacturer/Redmi  Mozilla/5.0 (Linux; Android 12; 22041216C Build/V417IR; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/110.0.5481.154 Mobile Safari/537.36",
                "content-type": "application/json",
            }
            async with session.post(ticket_url, headers=ticket_headers, json=ticket_payload) as resp:
                ticket_resp = await resp.json(content_type=None)

            sdk_ticket = ticket_resp.get("data", {}).get("ticket", "")
            if not sdk_ticket:
                return None

            # 签名
            raw_string = f"appid={WECHAT_APP_ID}&noncestr={noncestr}&sdk_ticket={sdk_ticket}&timestamp={timestamp}"
            signature = hashlib.sha1(raw_string.encode('utf-8')).hexdigest()

            params = {
                "appid": WECHAT_APP_ID,
                "noncestr": noncestr,
                "timestamp": timestamp,
                "scope": "snsapi_userinfo",
                "signature": signature,
            }
            headers = {
                "User-Agent": "mval/2.10053 Channel/10068 Manufacturer/Redmi Mozilla/5.0 (Linux; Android 12; 23117RK66C Build/V417IR; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/101.0.4951.61 Mobile Safari/537.36",
                "Content-Type": "application/json",
                "Accept": "*/*",
            }

            async with session.get(WECHAT_QRCONNECT_URL, params=params, headers=headers) as resp:
                resp_text = await resp.text()
                try:
                    result = await resp.json(content_type=None)
                except Exception:
                    result = json.loads(resp_text)

                if result.get("errcode") != 0:
                    return None

                qrcode_base64 = result.get("qrcode", {}).get("qrcodebase64", "")
                if not qrcode_base64:
                    return None

                return {
                    "uuid": result.get("uuid"),
                    "qrcode_base64": qrcode_base64,
                }

    @staticmethod
    async def poll_and_get_result(uuid: str, timeout: int = 60) -> Optional[Dict[str, Any]]:
        """轮询微信登录结果，成功返回 login_info。"""
        async with aiohttp.ClientSession() as session:
            headers = {
                "User-Agent": "mval/2.10053 Channel/10068 Manufacturer/Redmi Mozilla/5.0 (Linux; Android 12; 23117RK66C Build/V417IR; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/101.0.4951.61 Mobile Safari/537.36",
                "Content-Type": "application/json",
                "Accept": "*/*",
            }

            wx_code = None
            last_code = None
            for _ in range(timeout // 2):
                await asyncio.sleep(2)

                poll_url = f"{WECHAT_LONG_POLL_URL}?f=json&uuid={uuid}"
                if last_code is not None:
                    poll_url += f"&last={last_code}"

                async with session.get(poll_url, headers=headers) as resp:
                    if resp.status != 200:
                        continue
                    resp_text = await resp.text()
                    try:
                        result = await resp.json(content_type=None)
                    except Exception:
                        if "window.wx_errcode" in resp_text:
                            errcode_match = re.search(r"wx_errcode=(\d+)", resp_text)
                            code_match = re.search(r"wx_code='([^']+)'", resp_text)
                            result = {
                                "wx_errcode": int(errcode_match.group(1)) if errcode_match else 408,
                                "wx_code": code_match.group(1) if code_match else "",
                            }
                        else:
                            result = json.loads(resp_text)

                    wx_errcode = result.get("wx_errcode")
                    last_code = wx_errcode

                    if wx_errcode in [0, 405] and result.get("wx_code"):
                        wx_code = result.get("wx_code")
                        break
                    elif wx_errcode == 404:
                        continue  # 等待确认
                    elif wx_errcode == 408:
                        continue  # 等待扫码
                    elif wx_errcode == 0:
                        wx_code = result.get("wx_code")
                        break
                    else:
                        return None

            if not wx_code:
                return None

            return await call_login_by_wechat(wx_code)
