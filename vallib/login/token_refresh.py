"""Token 续期逻辑。"""
import logging
from typing import Any, Dict, Optional

from ..api.auth import call_login_by_qq
from ..db.repository import Repository

logger = logging.getLogger("astrbot")


async def try_refresh_credentials(
    repo: Repository,
    user_id: str,
    user_config: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """用保存的 openid/access_token 重调 login_by_qq 刷新 tid。

    成功返回新的 user_config dict，失败返回 None。
    """
    openid = user_config.get('openid', '')
    access_token = user_config.get('access_token', '')
    if not openid or not access_token:
        logger.info(f"用户 {user_id} 无 refresh 凭证，无法自动续期")
        return None

    logger.info(f"尝试自动续期: user_id={user_id}")
    login_result = await call_login_by_qq(openid, access_token)
    if not login_result or not login_result.get("userId") or not login_result.get("tid"):
        logger.warning(f"自动续期失败: user_id={user_id}")
        return None

    await repo.save_user_config(
        user_id,
        login_result["userId"],
        login_result["tid"],
        nickname=user_config.get("nickname"),
        openid=openid,
        access_token=access_token,
        login_type=user_config.get('login_type', 'qq'),
    )
    return await repo.get_user_config(user_id)
