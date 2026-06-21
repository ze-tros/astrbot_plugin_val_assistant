"""定时任务服务：APScheduler 设置 + 每日自动监控 + 每日推送。"""
import asyncio
import base64
import logging
from datetime import datetime
from typing import Any, Dict, List

from astrbot.api.event import MessageChain
from astrbot.core.message.components import Plain, At, Image

from ..db.repository import Repository
from ..api.shop import ShopAPI
from ..api.role import fetch_role_info
from ..login.token_refresh import try_refresh_credentials
from ..title_builder import TitleBuilder
from .notification import send_notification
from .shop_image import ShopImageService

logger = logging.getLogger("astrbot")


class SchedulerService:
    """每日定时任务调度。"""

    def __init__(
        self,
        context,
        config: dict,
        repo: Repository,
        shop_api: ShopAPI,
        api_client,
        shop_image: ShopImageService,
        html_render_func,
    ):
        self.context = context
        self.config = config
        self.repo = repo
        self.shop_api = shop_api
        self.api_client = api_client
        self.shop_image = shop_image
        self.html_render_func = html_render_func
        self._scheduler = None

    def _cf(self, key: str, default=None):
        return self.config.get(key, default)

    async def setup(self):
        """初始化 APScheduler 定时任务。"""
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger

            if self._scheduler:
                logger.info("检测到已有调度器，正在关闭旧调度器")
                self._scheduler.shutdown()

            timezone = self._cf('timezone', 'Asia/Shanghai')
            self._scheduler = AsyncIOScheduler(timezone=timezone)

            monitor_time = self._cf('monitor_time', '08:01')
            hour, minute = map(int, monitor_time.split(':'))
            self._scheduler.add_job(
                self.daily_auto_check,
                CronTrigger(hour=hour, minute=minute, timezone=timezone),
                id='daily_shop_check',
                replace_existing=True,
            )

            push_time = self._cf('push_time', '08:01')
            push_hour, push_minute = map(int, push_time.split(':'))
            self._scheduler.add_job(
                self.daily_push_check,
                CronTrigger(hour=push_hour, minute=push_minute, timezone=timezone),
                id='daily_push_check',
                replace_existing=True,
            )

            self._scheduler.start()
            logger.info(f"自动监控定时任务已启动：每天 {monitor_time} ({timezone})")
            logger.info(f"每日推送定时任务已启动：每天 {push_time} ({timezone})")

        except Exception as e:
            logger.error(f"定时任务调度器启动失败: {e}")

    def shutdown(self):
        if self._scheduler:
            self._scheduler.shutdown()
            logger.info("定时任务调度器已关闭")

    # ── 每日自动监控 ─────────────────────────────────────

    async def daily_auto_check(self):
        logger.info("开始执行每日自动监控任务")
        try:
            users = await self.repo.get_auto_check_users()
            if not users:
                logger.info("当前没有开启自动监控的用户")
                return

            logger.info(f"自动监控用户数量: {len(users)}")
            for user_id in users:
                try:
                    bot_id = self._cf('bot_id', 'default')
                    unified_msg_origin = f"{bot_id}:FriendMessage:{user_id}"
                    await self.check_user_watchlist(user_id, unified_msg_origin)
                except Exception as e:
                    logger.error(f"检查用户 {user_id} 监控列表时出错: {e}")
                    continue
        except Exception as e:
            logger.error(f"每日自动监控任务执行失败: {e}")

    async def check_user_watchlist(self, user_id: str, unified_msg_origin: str = None):
        logger.info(f"开始检查用户 {user_id} 的监控列表")
        user_config = await self.repo.get_user_config(user_id)
        if not user_config:
            return

        watchlist = await self.repo.get_watchlist(user_id)
        if not watchlist:
            return

        goods_list = await self.shop_api.get_shop_items_raw(user_id, user_config)
        if not goods_list:
            new_config = await try_refresh_credentials(self.repo, user_id, user_config)
            if new_config:
                goods_list = await self.shop_api.get_shop_items_raw(user_id, new_config)
            if not goods_list:
                return

        matched_items = []
        watchlist_names = [item['item_name'] for item in watchlist]
        for goods in goods_list:
            goods_name = goods.get('goods_name', '')
            for watch_name in watchlist_names:
                if watch_name in goods_name or goods_name in watch_name:
                    matched_items.append({
                        'name': goods_name,
                        'price': goods.get('rmb_price', '0')
                    })
                    break

        if matched_items:
            logger.info(f"用户 {user_id} 命中 {len(matched_items)} 个监控商品")
            await send_notification(self.context, user_id, matched_items, unified_msg_origin)
        else:
            logger.info(f"用户 {user_id} 今日无监控商品上架")

    # ── 每日推送 ─────────────────────────────────────────

    async def daily_push_check(self):
        logger.info("开始执行每日商店推送任务")
        try:
            subs = await self.repo.get_daily_push_subs()
            if not subs:
                logger.info("当前没有每日推送订阅用户")
                return

            logger.info(f"每日推送订阅用户数量: {len(subs)}")
            sessions: Dict[str, list] = {}
            for sub in subs:
                sid = sub['session_id']
                if sid not in sessions:
                    sessions[sid] = []
                sessions[sid].append(sub['user_id'])

            success_count = 0
            fail_count = 0
            for session_id, user_ids in sessions.items():
                logger.info(f"推送会话 {session_id}，订阅用户: {user_ids}")
                for user_id in user_ids:
                    try:
                        ok = await self._push_to_user(user_id, session_id)
                        if ok:
                            success_count += 1
                        else:
                            fail_count += 1
                        await asyncio.sleep(2.0)
                    except Exception as e:
                        fail_count += 1
                        logger.error(f"推送每日商店给用户 {user_id} 时出错: {e}", exc_info=True)
                        continue
            logger.info(f"每日推送完成：成功 {success_count}，失败 {fail_count}")

        except Exception as e:
            logger.error(f"每日商店推送任务执行失败: {e}", exc_info=True)

    async def _push_to_user(self, user_id: str, session_id: str) -> bool:
        """向单用户推送每日商店，返回是否成功。"""
        user_config = await self.repo.get_user_config(user_id)
        if not user_config:
            logger.warning(f"推送跳过: 用户 {user_id} 未绑定")
            return False

        goods_list = await self.shop_api.get_shop_items_raw(user_id, user_config)
        if not goods_list:
            new_config = await try_refresh_credentials(self.repo, user_id, user_config)
            if new_config:
                goods_list = await self.shop_api.get_shop_items_raw(user_id, new_config)
                if goods_list:
                    user_config = new_config
            if not goods_list:
                logger.warning(f"推送跳过: 用户 {user_id} 商店数据为空")
                return False

        # 自动同步角色名
        if not user_config.get("role_name"):
            await self._auto_sync_role(user_id, user_config)
            user_config = await self.repo.get_user_config(user_id) or user_config

        title_name = TitleBuilder.get_display_name(user_config, user_id)
        push_date = datetime.now().strftime("%Y-%m-%d")
        shop_data, _ = await self.shop_image.generate(
            user_id, user_config,
            html_render_func=self.html_render_func,
            goods_list=goods_list,
            title_text=title_name,
            date_str=push_date,
        )

        if not shop_data:
            logger.warning(f"推送跳过: 用户 {user_id} 商店图片生成失败")
            return False

        message_chain = MessageChain()
        current_date = datetime.now().strftime("%Y-%m-%d")
        message_chain.chain.append(At(qq=user_id))
        message_chain.chain.append(Plain(f" {current_date} 每日商店"))
        await self.context.send_message(session_id, message_chain)

        image_bytes = base64.b64decode(shop_data)
        image_chain = MessageChain()
        image_chain.chain.append(Image.fromBytes(image_bytes))
        await self.context.send_message(session_id, image_chain)

        logger.info(f"已推送每日商店给用户 {user_id}，会话: {session_id}")
        return True

    async def _auto_sync_role(self, user_id: str, user_config: Dict[str, Any]):
        try:
            role_data, err = await fetch_role_info(self.api_client, user_config)
            if err or not role_data:
                return
            roles = role_data.get("list", [])
            if not roles:
                return
            role = roles[0]
            await self.repo.save_game_role(user_id, {
                "game_role_id": role.get("game_role_id", ""),
                "game_open_id": role.get("game_open_id", ""),
                "tier_text": role.get("tier_text", ""),
                "role_name": role.get("role_name", ""),
                "role_level": str(role.get("role_level", "")),
                "competitive_tier": str(role.get("competitive_tier", "")),
                "avatar_url": role.get("avatar") or role.get("head_icon") or role.get("icon_url", ""),
            })
        except Exception as e:
            logger.warning(f"自动同步角色信息失败: {e}")
