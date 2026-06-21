"""消息通知服务。"""
import logging
from datetime import datetime

from astrbot.api.event import MessageChain

logger = logging.getLogger("astrbot")


async def send_notification(context, user_id: str, matched_items: list,
                           unified_msg_origin: str = None):
    """发送监控命中通知。"""
    try:
        current_date = datetime.now().strftime("%Y-%m-%d")
        items_text = "\n".join([f"  - {item['name']} ({item['price']})" for item in matched_items])
        matched_names = [item['name'] for item in matched_items]

        notification_text = (
            f"{current_date} 商店监控通知\n\n"
            f"以下监控商品已上架：\n"
            f"{items_text}\n\n"
            f"请使用 /瓦 商店 查看详情\n\n"
            f"匹配商品：{', '.join(matched_names)}"
        )

        session_id = unified_msg_origin or f"qq/{user_id}"
        message_chain = MessageChain().message(notification_text)
        await context.send_message(session_id, message_chain)
        logger.info(f"已发送通知给用户 {user_id}, 会话ID: {session_id}")

    except Exception as e:
        logger.error(f"发送通知失败: {e}")
