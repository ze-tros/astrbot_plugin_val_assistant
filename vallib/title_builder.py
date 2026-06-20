"""标题名构建和标题栏工具。"""
from typing import Any, Dict, Optional


class TitleBuilder:
    """构建图片标题的工具类。"""

    @staticmethod
    def get_display_name(user_config: Dict[str, Any], user_id: str) -> str:
        """从用户配置获取显示名称，优先角色名 > 昵称 > 用户ID。"""
        return user_config.get("role_name") or user_config.get("nickname") or user_id

    @staticmethod
    def build_shop_title(display_name: str, date_str: Optional[str] = None) -> str:
        """构建商店标题文本。"""
        text = f"{display_name}的每日商店"
        if date_str:
            text += f"  |  {date_str}"
        return text
