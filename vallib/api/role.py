"""角色信息接口。"""
import time
from typing import Any, Dict, Tuple

from .client import APIClient

ROLE_ENDPOINT = "/go/account/get_main_role_raw"


async def fetch_role_info(api: APIClient, user: Dict) -> Tuple[Any, Any]:
    """获取用户游戏角色信息。"""
    return await api.post(ROLE_ENDPOINT, user, {"_t": int(time.time())}, cookie_variant="real_token")
