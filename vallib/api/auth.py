"""认证接口：login_by_qq, login_by_wechat。"""
import logging
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger("astrbot")


async def call_login_by_qq(openid: str, access_token: str) -> Optional[Dict[str, Any]]:
    """用 QQ OAuth 凭证调用 login_by_qq 换取 userId/tid。"""
    login_url = "https://app.mval.qq.com/go/auth/login_by_qq?source_game_zone=agame&game_zone=agame"
    headers = {
        "Cookie": "clientType=9; openid=null; access_token=null;",
        "User-Agent": "mval/2.4.0.10053 Channel/10068 Manufacturer/Redmi  Mozilla/5.0 (Linux; Android 12; 23117RK66C Build/V417IR; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/101.0.4951.61 Mobile Safari/537.36",
        "Content-Type": "application/json",
        "Host": "app.mval.qq.com",
        "Connection": "Keep-Alive",
        "Accept-Encoding": "gzip",
    }
    data = {
        "clienttype": 9,
        "config_params": {"client_dev_name": "23117RK66C", "lang_type": 0},
        "login_info": {
            "appid": 102061775,
            "openid": openid,
            "qq_info_type": 5,
            "sig": access_token,
            "uin": 0,
        },
        "mappid": 10200,
        "mcode": "132f0a77d34402abc8463d60100011d19b0e",
        "source_game_zone": "agame",
        "game_zone": "agame",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(login_url, headers=headers, json=data) as response:
                response.raise_for_status()
                result = await response.json()

                if result.get("result") != 0:
                    logger.warning(f"login_by_qq 失败: {result.get('msg', '未知错误')}")
                    return None

                login_info = result.get("data", {}).get("login_info", {})
                return {
                    "userId": login_info.get("user_id", ""),
                    "tid": login_info.get("wt", ""),
                    "uin": login_info.get("uin", 0),
                    "openid": openid,
                }
    except Exception as e:
        logger.error(f"login_by_qq 请求异常: {e}")
        return None


async def get_final_cookies(login_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """用 QQ OAuth 凭证换取最终 userId/tid 并构造 Cookie。"""
    logger.info("正在获取最终 Cookie...")

    openid = login_data.get("openid", "")
    access_token = login_data.get("access_token", "")

    if not openid or not access_token:
        logger.error("缺少必要参数 openid 或 access_token")
        return None

    login_result = await call_login_by_qq(openid, access_token)
    if not login_result:
        logger.error("获取最终 Cookie 失败")
        return None

    uin = login_result["uin"]
    user_id = login_result["userId"]
    wt = login_result["tid"]

    final_cookie = (
        f"clientType=9; "
        f"uin=o{uin}; "
        f"appid=102061775; "
        f"acctype=pt;"
        f"openid={openid}; "
        f"access_token=null; "
        f"userId={user_id}; "
        f"accountType=5; "
        f"tid={wt};"
    )

    logger.info("成功获取最终 Cookie")
    return {
        "userId": user_id,
        "tid": wt,
        "openid": openid,
        "uin": uin,
        "final_cookie": final_cookie,
    }


async def call_login_by_wechat(wx_code: str) -> Optional[Dict[str, Any]]:
    """用微信 code 换取 login_info。"""
    login_url = "https://app.mval.qq.com/go/auth/login_by_wechat"
    payload = {
        "clienttype": 9,
        "config_params": {"client_dev_name": "22041216C", "lang_type": 0},
        "login_info": {
            "appid": "wxcbb49f1f39656c2a",
            "check_third_type": 1,
            "code": wx_code,
            "wx_info_type": 1,
        },
        "mappid": 10200,
        "mcode": "69028af6dca2c107f4f58290100011b1a303",
        "source_game_zone": "agame",
        "game_zone": "agame",
    }
    headers = {
        "user-agent": "mval/2.6.0.10062 Channel/3 Manufacturer/Redmi  Mozilla/5.0 (Linux; Android 12; 22041216C Build/V417IR; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/110.0.5481.154 Mobile Safari/537.36",
        "content-type": "application/json",
        "cookie": "clientType=9; openid=null; access_token=null;",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(login_url, headers=headers, json=payload) as resp:
                login_result = await resp.json(content_type=None)
                logger.info(f"login_by_wechat result: {login_result}")
                login_info = login_result.get("data", {}).get("login_info", {})
                if login_info and login_info.get("result") == 0:
                    return {
                        "userId": login_info.get("user_id"),
                        "tid": login_info.get("wt"),
                        "openid": login_info.get("openid"),
                        "access_token": login_info.get("access_token"),
                        "clienttype": 9,
                        "login_type": "wechat",
                    }
                logger.error(f"微信登录验证失败: {login_result}")
                return None
    except Exception as e:
        logger.error(f"login_by_wechat 请求异常: {e}")
        return None
