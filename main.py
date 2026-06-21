"""无畏契约助手 插件入口。

命令处理器在此文件中，业务逻辑在 vallib/ 各子模块。
"""
import asyncio
import base64
import logging
import os
import re
import sys
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple, List

import aiohttp

# 确保插件目录在 Python 路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.message.components import Plain, At, Image
from astrbot.api.event import MessageChain

from vallib.title_builder import TitleBuilder
from vallib.db.repository import Repository
from vallib.db.migrations import run_migrations
from vallib.api.client import APIClient
from vallib.api.shop import ShopAPI
from vallib.api.match import MatchAPI
from vallib.api.auth import call_login_by_qq, get_final_cookies
from vallib.api.role import fetch_role_info
from vallib.login.qq_login import QQLoginFlow
from vallib.login.wechat_login import WechatLoginFlow
from vallib.login.token_refresh import try_refresh_credentials
from vallib.services.notification import send_notification
from vallib.services.shop_image import ShopImageService
from vallib.services.analysis import AnalysisService
from vallib.services.scheduler import SchedulerService

logger = logging.getLogger("astrbot")


@register("astrbot_plugin_val_assistant", "", "无畏契约助手", "v1.0.0")
class ValorantShopPlugin(Star):

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))

        # ── 初始化各服务模块 ──
        self.api = APIClient()
        self.repo = Repository(context)
        self.shop_api = ShopAPI(self.api)
        self.match_api = MatchAPI(self.api)
        self.shop_image = ShopImageService(self.plugin_dir, self.shop_api)
        self.analysis = AnalysisService(self.plugin_dir, match_api=self.match_api)

        # 登录流程
        self.qq_login = QQLoginFlow({
            "login_callback_url": self._get_login_callback_url(),
            "login_u1_url": self._get_login_u1_url(""),
        })
        self.wechat_login_flow = WechatLoginFlow()

        # 调度器（延后到 initialize 时才设置，因为需要配置）
        self.scheduler = None
        self.wechat_login_tasks: Dict[str, list] = {}

        # API 常量
        self.MATCH_LIST_PATH = "/go/agame/career/record/list"
        self.MATCH_LIST_QS = "source_game_zone=agame&game_zone=agame"
        self.MCODE = "132f0a77d34402abc8463d60100011d19b0e"

    # ════════════════════════════════════════════════════════════════
    # 生命周期
    # ════════════════════════════════════════════════════════════════

    async def initialize(self):
        await run_migrations(self.context)

        self.scheduler = SchedulerService(
            context=self.context,
            config=self.config,
            repo=self.repo,
            shop_api=self.shop_api,
            api_client=self.api,
            shop_image=self.shop_image,
            html_render_func=self.html_render,
        )
        await self.scheduler.setup()
        logger.info("插件初始化完成")

    async def terminate(self):
        if self.scheduler:
            self.scheduler.shutdown()
            logger.info("定时任务调度器已关闭")

    # ════════════════════════════════════════════════════════════════
    # 配置辅助
    # ════════════════════════════════════════════════════════════════

    def _get_config_value(self, key: str, default=None):
        return self.config.get(key, default)

    def _normalize_login_mode(self, mode: str) -> str:
        value = str(mode or "").strip().lower()
        if value in {"qq", "q"}:
            return "qq"
        if value in {"wx", "wechat", "weixin", "微信"}:
            return "wx"
        return ""

    def _get_default_login_mode(self) -> str:
        raw_value = self._get_config_value("default_login_mode", "qq")
        mode = self._normalize_login_mode(raw_value)
        return mode if mode else "qq"

    def _normalize_url(self, value: str, default: str = "") -> str:
        url = (value or default or "").strip()
        if not url:
            return ""
        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = f"https://{url.lstrip('/')}"
        return url

    def _get_login_callback_url(self) -> str:
        value = str(self._get_config_value("login_callback_url", "http://connect.qq.com"))
        return self._normalize_url(value)

    def _get_login_u1_url(self, _callback_url: str) -> str:
        value = str(self._get_config_value("login_u1_url", "http://connect.qq.com"))
        return self._normalize_url(value)

    # ════════════════════════════════════════════════════════════════
    # 工具方法
    # ════════════════════════════════════════════════════════════════

    async def get_at_id(self, event: AstrMessageEvent) -> Optional[str]:
        for seg in event.get_messages():
            if isinstance(seg, At) and str(seg.qq) != event.get_self_id():
                return str(seg.qq)
        return None

    async def test_config_validity(self, user_id: str, user_config: Dict[str, Any]) -> bool:
        logger.info(f"测试用户配置有效性，user_id: {user_id}")
        response_data, err_msg, _ = await self.shop_api.request_store_api(
            user_id, user_config, max_retries=1, timeout=10,
        )
        return response_data is not None

    async def _auto_sync_role_after_login(self, user_id: str, user_config: Dict[str, Any]):
        try:
            role_data, err = await fetch_role_info(self.api, user_config)
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
            logger.info(f"登录后自动同步角色名成功: {role.get('role_name', '')}")
        except Exception as e:
            logger.warning(f"登录后自动同步角色信息失败: {e}")

    async def _check_config_and_try_refresh(
        self, user_id: str, user_config: Dict, target_user_id: str = None
    ) -> Tuple[Optional[list], Optional[str]]:
        """检测凭证并尝试续期，返回 (goods_list, error_msg_or_none)。"""
        response_data, err_msg, auth_invalid = await self.shop_api.request_store_api(
            user_id, user_config, max_retries=1, timeout=10,
        )
        if response_data:
            goods_list, parse_err = self.shop_api.extract_goods_list(response_data)
            return goods_list, parse_err

        if auth_invalid and not target_user_id:
            new_config = await try_refresh_credentials(self.repo, user_id, user_config)
            if new_config:
                response_data2, err_msg2, _ = await self.shop_api.request_store_api(
                    user_id, new_config, max_retries=1, timeout=10,
                )
                if response_data2:
                    goods_list, parse_err = self.shop_api.extract_goods_list(response_data2)
                    return goods_list, parse_err
                return None, f"凭据已刷新但仍无法获取商店: {err_msg2 or '请稍后重试'}"
            return None, "当前登录凭证已过期，请使用 /瓦 重新绑定后再试"

        return None, err_msg or "获取商店信息失败，请稍后重试"

    # ── 子命令：商店 ─────────────────────────────────────

    async def _cmd_shop(self, event: AstrMessageEvent):
        target_user_id = await self.get_at_id(event)
        user_id = target_user_id or event.get_sender_id()

        user_config = await self.repo.get_user_config(user_id)
        if not user_config:
            msg = f"用户 {target_user_id} 未绑定账号" if target_user_id else "您尚未绑定无畏契约账号，请先使用 /瓦 qq 或 /瓦 wx 进行绑定"
            yield event.plain_result(msg)
            return

        goods_list, err = await self._check_config_and_try_refresh(user_id, user_config, target_user_id)
        if err:
            yield event.plain_result(err if not target_user_id else f"获取用户 {target_user_id} 的商店信息失败: {err}")
            return

        if not goods_list:
            msg = f"用户 {target_user_id} 今日商店暂无可用数据" if target_user_id else "今日商店暂无可用数据，请稍后再试"
            yield event.plain_result(msg)
            return

        if not user_config.get("role_name") and not target_user_id:
            await self._auto_sync_role_after_login(user_id, user_config)
            user_config = await self.repo.get_user_config(user_id) or user_config

        title_name = TitleBuilder.get_display_name(user_config, user_id)
        query_date = datetime.now().strftime("%Y-%m-%d")
        shop_data, _ = await self.shop_image.generate(
            user_id, user_config,
            html_render_func=self.html_render,
            goods_list=goods_list,
            title_text=title_name,
            date_str=query_date,
        )

        if shop_data:
            image_bytes = base64.b64decode(shop_data)
            yield event.chain_result([Image.fromBytes(image_bytes)])
        else:
            yield event.plain_result("获取商店信息失败，请稍后重试")

    # ════════════════════════════════════════════════════════════════
    # 命令：/瓦（统一入口）
    # ════════════════════════════════════════════════════════════════

    @filter.command("\u74e6")
    async def cmd_val(self, event: AstrMessageEvent):
        """所有命令的统一入口。"""
        user_id = str(event.get_sender_id() or "").strip()
        if not user_id:
            yield event.plain_result("无法识别当前用户ID，请稍后重试")
            return

        message = (event.get_message_str() or "").strip()
        parts = message.split(maxsplit=1)
        raw_args = parts[1].strip() if len(parts) > 1 else ""

        if raw_args:
            arg_parts = raw_args.split(maxsplit=1)
            sub_cmd = arg_parts[0].strip()
            sub_args = arg_parts[1].strip() if len(arg_parts) > 1 else ""
        else:
            sub_cmd = ""
            sub_args = ""

        sub_lower = sub_cmd.lower()

        # ── 路由分发 ──
        if sub_cmd in ("", "帮助"):
            async for r in self._cmd_help(event):
                yield r

        elif sub_lower in ("qq", "q"):
            async for r in self._cmd_bind_qq(event, user_id):
                yield r

        elif sub_lower in ("wx", "wechat", "weixin", "微信"):
            async for r in self._cmd_bind_wx(event, user_id):
                yield r

        elif sub_cmd in ("清除", "清空", "解绑", "clear", "reset", "remove", "delete"):
            async for r in self._cmd_clear(event, user_id):
                yield r

        elif sub_cmd == "商店":
            async for r in self._cmd_shop(event):
                yield r

        elif sub_cmd == "监控":
            async for r in self._cmd_watchlist(event, sub_args):
                yield r

        elif sub_cmd == "推送":
            async for r in self._cmd_push(event, sub_args):
                yield r

        elif sub_cmd == "状态":
            async for r in self._cmd_status(event):
                yield r

        elif sub_cmd == "同步":
            async for r in self._cmd_sync(event):
                yield r

        elif sub_cmd == "战绩":
            async for r in self._cmd_matches(event, sub_args):
                yield r

        elif sub_cmd == "分析":
            async for r in self._cmd_analysis(event, sub_args):
                yield r

        elif sub_cmd == "用户":
            async for r in self._cmd_users(event):
                yield r

        else:
            yield event.plain_result(f"未知命令「{sub_cmd}」，输入 /瓦 查看帮助")

    # ── 子命令：账号绑定 ─────────────────────────────────

    async def _qq_bind_flow(self, event: AstrMessageEvent, user_id: str, check_existing: bool = True):
        """QQ 二维码绑定流程。"""
        if check_existing:
            user_config = await self.repo.get_user_config(user_id)
            if user_config:
                yield event.plain_result("检测到你已绑定账号，正在测试配置有效性...")
                if await self.test_config_validity(user_id, user_config):
                    yield event.plain_result(
                        f"账号已绑定且配置有效。\n"
                        f"用户ID: {user_config['userId']}\n"
                        f"可直接使用 /瓦 商店"
                    )
                    return
                yield event.plain_result("检测到当前配置已失效，需要重新登录。")

        yield event.plain_result("正在生成QQ登录二维码，请稍候...")

        try:
            http_ctx = await self.qq_login.generate_qr_code()
            if not http_ctx:
                yield event.plain_result("生成登录二维码失败，请稍后重试")
                return

            http_session: aiohttp.ClientSession = http_ctx["session"]
            qr_filename = http_ctx["filename"]

            try:
                with open(qr_filename, 'rb') as f:
                    qr_image_data = f.read()
                yield event.chain_result([
                    Image.fromBytes(qr_image_data),
                    Plain("请在30秒内扫码登录"),
                ])

                login_data = await self.qq_login.wait_for_login_result(
                    session=http_session,
                    ptqrtoken=http_ctx["ptqrtoken"],
                    login_sig=http_ctx.get("login_sig", ""),
                    login_u1=http_ctx.get("u1_url", ""),
                    referer_url=http_ctx.get("login_url", ""),
                    pt_openlogin_data=http_ctx.get("pt_openlogin_data", ""),
                    aegis_uid=http_ctx.get("aegis_uid", ""),
                    jsver=http_ctx.get("jsver", "28d22679"),
                    pt_uistyle=http_ctx.get("pt_uistyle", "35"),
                    ptlang=http_ctx.get("ptlang", "2052"),
                    timeout=30,
                )
                if not login_data:
                    yield event.plain_result("登录失败或超时，请重试")
                    return

                final_data = await get_final_cookies(login_data)
                if not final_data:
                    yield event.plain_result("获取最终登录信息失败，请重试")
                    return

                await self.repo.save_user_config(
                    user_id, final_data['userId'], final_data['tid'],
                    nickname=final_data.get('nickname'),
                    openid=login_data.get('openid', ''),
                    access_token=login_data.get('access_token', ''),
                    login_type="qq", uin=str(final_data.get('uin', '')),
                )
                await self._auto_sync_role_after_login(user_id, {
                    'userId': final_data['userId'], 'tid': final_data['tid'],
                    'openid': login_data.get('openid', ''),
                    'access_token': login_data.get('access_token', ''),
                    'login_type': 'qq', 'uin': str(final_data.get('uin', '')),
                })
                yield event.plain_result(f"登录成功！\n用户ID: {final_data['userId']}\n现在可以使用 /瓦 商店")
            finally:
                await http_session.close()
                if os.path.exists(qr_filename):
                    os.remove(qr_filename)

        except Exception as e:
            logger.error(f"[HTTP登录] 绑定流程异常: {type(e).__name__}, {e}")
            yield event.plain_result("登录过程出错，请稍后重试")

    async def _cmd_bind_qq(self, event: AstrMessageEvent, user_id: str):
        """QQ 绑定入口。"""
        async for result in self._qq_bind_flow(event, user_id, check_existing=True):
            yield result

    async def _cmd_bind_wx(self, event: AstrMessageEvent, user_id: str):
        """微信绑定入口。"""
        user_config = await self.repo.get_user_config(user_id)
        if user_config:
            yield event.plain_result("检测到你已绑定账号，正在测试配置有效性...")
            if await self.test_config_validity(user_id, user_config):
                yield event.plain_result(
                    f"账号已绑定且配置有效。\n用户ID: {user_config['userId']}\n可直接使用 /瓦 商店"
                )
                return
            yield event.plain_result("检测到当前配置已失效，需要重新登录。")

        # 清理旧任务
        for task in self.wechat_login_tasks.get(user_id, []):
            if not task.done():
                task.cancel()
        self.wechat_login_tasks.pop(user_id, None)

        try:
            qr_data = await self.wechat_login_flow.get_qr_code()
            if not qr_data:
                yield event.plain_result("获取微信登录二维码失败，请稍后重试")
                return

            qrcode_base64 = qr_data["qrcode_base64"]
            if "," in qrcode_base64:
                qrcode_base64 = qrcode_base64.split(",", 1)[1]
            qr_image_bytes = base64.b64decode(qrcode_base64)
            yield event.chain_result([
                Image.fromBytes(qr_image_bytes),
                Plain("请使用微信扫码登录（30秒内有效）")
            ])

            result = await self.wechat_login_flow.poll_and_get_result(qr_data["uuid"], timeout=60)

            if result and result.get("userId") and result.get("tid"):
                await self.repo.save_user_config(
                    user_id, result['userId'], result['tid'],
                    nickname=result.get('nickname'),
                    openid=result.get('openid', ''),
                    access_token=result.get('access_token', ''),
                    login_type="wx",
                )
                await self._auto_sync_role_after_login(user_id, {
                    'userId': result['userId'], 'tid': result['tid'],
                    'openid': result.get('openid', ''),
                    'access_token': result.get('access_token', ''),
                    'login_type': 'wx',
                })
                yield event.plain_result("登录成功！现在可以使用 /瓦 商店")
            else:
                yield event.plain_result("登录失败或已过期，请重新使用 /瓦 wx 获取二维码")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"微信登录异常: {e}")
            yield event.plain_result("登录过程中发生错误，请重试")

    async def _cmd_clear(self, event: AstrMessageEvent, user_id: str):
        """清除绑定。"""
        for task in self.wechat_login_tasks.get(user_id, []):
            if not task.done():
                task.cancel()
        self.wechat_login_tasks.pop(user_id, None)

        cleared = await self.repo.clear_user_config(user_id)
        if cleared:
            yield event.plain_result("已清除你的登录信息。\n如需重新绑定，可使用 /瓦 qq 或 /瓦 wx")
        else:
            yield event.plain_result("当前未检测到已绑定登录信息，无需清除")

    # ── 子命令：监控 ─────────────────────────────────────

    async def _cmd_watchlist(self, event: AstrMessageEvent, sub_args: str = ""):
        user_id = event.get_sender_id()

        if not sub_args:
            user_config = await self.repo.get_user_config(user_id)
            auto_status = "已开启" if user_config and user_config.get('auto_check') == 1 else "已关闭"
            yield event.plain_result(
                "商店监控功能\n\n"
                "可用子命令：\n"
                "/瓦 监控 添加 \"皮肤 武器\" - 添加监控项\n"
                "/瓦 监控 删除 \"皮肤 武器\" - 删除监控项\n"
                "/瓦 监控 列表 - 查看监控列表\n"
                "/瓦 监控 查询 - 立即执行一次监控查询\n"
                "/瓦 监控 开启 - 启用自动查询\n"
                "/瓦 监控 关闭 - 停用自动查询\n\n"
                f"当前自动查询状态：{auto_status}\n"
                f"监控时间：{self._get_config_value('monitor_time', '08:01')}\n"
                f"时区：{self._get_config_value('timezone', 'Asia/Shanghai')}"
            )
            return

        parts = sub_args.split(maxsplit=1)
        sub_command = parts[0].strip()
        item_name = parts[1].strip().strip('"') if len(parts) > 1 else ""

        if sub_command == "添加":
            if not item_name:
                yield event.plain_result("请提供商品名称，例如：/瓦 监控 添加 \"侦察力量 幻象\"")
                return
            success = await self.repo.add_watch_item(user_id, item_name)
            yield event.plain_result(f"已添加监控项 \"{item_name}\"" if success else f"监控项 \"{item_name}\" 已存在")

        elif sub_command == "删除":
            if not item_name:
                yield event.plain_result("请提供商品名称，例如：/瓦 监控 删除 \"侦察力量 幻象\"")
                return
            success = await self.repo.remove_watch_item(user_id, item_name)
            yield event.plain_result(
                f"已从监控列表删除 \"{item_name}\"" if success else f"监控列表中不存在 \"{item_name}\""
            )

        elif sub_command == "列表":
            watchlist = await self.repo.get_watchlist(user_id)
            if not watchlist:
                yield event.plain_result("您的监控列表为空\n使用 /瓦 监控 添加 \"商品名称\" 来添加监控项")
            else:
                items_text = "\n".join([f"  - {item['item_name']}" for item in watchlist])
                yield event.plain_result(f"您的监控列表（{len(watchlist)}项）：\n{items_text}")

        elif sub_command == "查询":
            yield event.plain_result("正在执行监控查询，请稍候...")
            try:
                await self.scheduler.check_user_watchlist(user_id, event.unified_msg_origin)
                yield event.plain_result("监控查询完成")
            except Exception as e:
                logger.error(f"手动监控查询失败: {e}")
                yield event.plain_result("监控查询失败，请稍后重试")

        elif sub_command == "开启":
            await self.repo.update_auto_check(user_id, 1)
            yield event.plain_result(
                f"已开启自动查询\n每天 {self._get_config_value('monitor_time', '08:01')} "
                f"({self._get_config_value('timezone', 'Asia/Shanghai')}) 执行\n"
                "监控到上架后会自动通知你"
            )

        elif sub_command == "关闭":
            await self.repo.update_auto_check(user_id, 0)
            yield event.plain_result("已关闭自动查询")

        else:
            yield event.plain_result(f"未知子命令「{sub_command}」，输入 /瓦 监控 查看帮助")

    # ── 子命令：推送 ─────────────────────────────────────

    async def _cmd_push(self, event: AstrMessageEvent, sub_args: str = ""):
        user_id = event.get_sender_id()
        session_id = event.unified_msg_origin
        push_time = self._get_config_value('push_time', '08:01')
        timezone = self._get_config_value('timezone', 'Asia/Shanghai')

        if not sub_args:
            is_sub = await self.repo.get_user_daily_push_status(user_id, session_id)
            yield event.plain_result(
                "每日商店推送功能\n\n"
                "订阅后每天定时自动推送你的每日商店\n"
                "可用子命令：\n"
                "/瓦 推送 订阅 - 在当前会话订阅每日推送\n"
                "/瓦 推送 取消订阅 - 取消当前会话的每日推送\n"
                "/瓦 推送 状态 - 查看当前订阅状态\n"
                "/瓦 推送 列表 - 查看全部订阅列表\n\n"
                f"当前订阅状态：{'已订阅' if is_sub else '未订阅'}\n"
                f"推送时间：{push_time} ({timezone})"
            )
            return

        sub_command = sub_args.strip()

        if sub_command == "订阅":
            user_config = await self.repo.get_user_config(user_id)
            if not user_config:
                yield event.plain_result("您尚未绑定无畏契约账号，请先使用 /瓦 qq 或 /瓦 wx 进行绑定")
                return
            success = await self.repo.add_daily_push_sub(user_id, session_id)
            if success:
                yield event.plain_result(f"已订阅每日商店推送\n每天 {push_time} ({timezone}) 自动推送你的每日商店")
            else:
                yield event.plain_result("您已在当前会话订阅每日商店推送，无需重复订阅")

        elif sub_command == "取消订阅":
            success = await self.repo.remove_daily_push_sub(user_id, session_id)
            yield event.plain_result("已取消每日商店推送订阅" if success else "当前会话未订阅每日商店推送")

        elif sub_command == "状态":
            is_sub = await self.repo.get_user_daily_push_status(user_id, session_id)
            yield event.plain_result(
                f"当前会话已订阅每日商店推送\n推送时间：{push_time} ({timezone})" if is_sub
                else f"当前会话未订阅每日商店推送\n使用 /瓦 推送 订阅 来订阅"
            )

        elif sub_command == "列表":
            if not event.is_admin():
                return
            subs = await self.repo.get_daily_push_subs()
            if not subs:
                yield event.plain_result("当前没有任何推送订阅")
            else:
                # 按用户分组
                by_user: dict[str, list] = {}
                for s in subs:
                    uid = s['user_id']
                    by_user.setdefault(uid, []).append(s)
                lines = [f"每日推送订阅列表（共 {len(subs)} 条，{len(by_user)} 人）：", ""]
                for i, (uid, entries) in enumerate(by_user.items(), 1):
                    user_config = await self.repo.get_user_config(uid)
                    name = TitleBuilder.get_display_name(user_config or {}, uid)
                    lines.append(f"  {i}. {name}（{uid}）")
                    for e in entries:
                        sid_short = e['session_id']
                        lines.append(f"     └ {sid_short}")
                yield event.plain_result("\n".join(lines))

        else:
            yield event.plain_result(f"未知子命令「{sub_command}」，输入 /瓦 推送 查看帮助")

    # ════════════════════════════════════════════════════════════════
    # ── 子命令：状态 ─────────────────────────────────────
    # ════════════════════════════════════════════════════════════════

    async def _cmd_status(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id() or "")
        target = await self.get_at_id(event)
        if target:
            uid = target

        user_config = await self.repo.get_user_config(uid)
        if not user_config:
            hint = f"用户 {target} " if target else "你"
            yield event.plain_result(f"{hint}尚未绑定账号，请使用 /瓦 qq 或 /瓦 wx 进行绑定")
            return

        label = "群友" if target else "你"
        lines = [
            f"📋 **{label}** 的无畏契约账号",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        role_name = user_config.get("role_name") or user_config.get("nickname") or "未设置"
        lines.append(f"👤 角色名: **{role_name}**")
        lines.append(f"🏅 段位: **{user_config.get('tier_text') or '未知'}**")
        rlevel = user_config.get("role_level", "")
        if rlevel:
            lines.append(f"📊 账号等级: {rlevel}")

        season_fields = {
            "season_kda": "赛季KDA", "season_win_rate": "赛季胜率",
            "season_eval_score": "场均ACS", "season_kast": "KAST",
            "season_hs_rate": "精准击败(爆头率)", "season_round_wr": "回合胜率",
            "season_matches": "统计场次", "season_total_time": "游戏总时长",
            "season_rank_detail": "段位详情",
        }
        has_season = any(user_config.get(k) for k in season_fields)
        if has_season:
            lines.append("")
            lines.append("📈 **赛季数据**")
            srd = user_config.get("season_rank_detail", "")
            if srd and srd != user_config.get("tier_text", ""):
                lines.append(f"  段位详情: {srd}")
            for key, label in season_fields.items():
                val = user_config.get(key, "")
                if val and key != "season_rank_detail":
                    lines.append(f"  {label}: {val}")

        lines.append("")
        lines.append("🔑 **绑定信息**")
        lines.append(f"  用户ID: {user_config.get('userId', '?')[:20]}…")
        game_role = user_config.get("game_role_id", "")
        if game_role:
            lines.append(f"  游戏角色ID: {game_role[:20]}…")
        lines.append("  ✅ 登录状态: 正常")
        if not has_season:
            lines.append("\n💡 赛季数据未获取，请使用 /瓦 同步 刷新")

        yield event.plain_result("\n".join(lines))

    # ════════════════════════════════════════════════════════════════
    # ── 子命令：同步 ─────────────────────────────────────
    # ════════════════════════════════════════════════════════════════

    async def _cmd_sync(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id() or "")
        user_config = await self.repo.get_user_config(uid)
        if not user_config:
            yield event.plain_result("请先使用 /瓦 qq 或 /瓦 wx 进行绑定")
            return

        yield event.plain_result("⏳ 正在同步角色信息…")
        data, err = await fetch_role_info(self.api, user_config)
        if err:
            yield event.plain_result(f"❌ 同步失败: {err}")
            return
        if not data:
            yield event.plain_result("❌ 未获取到角色信息")
            return

        roles = data.get("list", [])
        if not roles:
            yield event.plain_result("❌ 未找到游戏角色")
            return

        role = roles[0]
        scene_token = role.get("scene", "")
        role_name = role.get("role_name", "")
        tier_text = role.get("tier_text", "")
        role_level = str(role.get("role_level", ""))
        grid = role.get("game_role_id", "")
        avatar = role.get("avatar") or role.get("head_icon") or role.get("icon_url", "")

        season_stats = None
        if scene_token:
            yield event.plain_result("⏳ 正在探测赛季数据端点…")
            season_stats = await self.match_api.probe_season_endpoints(user_config, scene_token, grid)
            if not season_stats:
                yield event.plain_result("⏳ 赛季端点未命中，改用近期对局计算…")
                career_stats = await self.match_api.fetch_career_stats(user_config, scene_token, sample_n=20)
                if career_stats:
                    season_stats = {
                        "kda": career_stats["kda"], "avg_acs": career_stats["avg_acs"],
                        "hs_pct": career_stats["hs_pct"], "matches": str(career_stats["matches"]),
                        "win_rate": "", "kast": "", "round_win_rate": "", "total_time": "",
                    }

        save_data = {
            "game_role_id": grid, "game_open_id": role.get("game_open_id", ""),
            "tier_text": tier_text, "role_name": role_name,
            "role_level": role_level, "avatar_url": avatar,
            "competitive_tier": str(role.get("competitive_tier", "")),
        }
        if season_stats:
            for k in ("kda", "hs_pct", "avg_acs", "matches", "win_rate",
                       "kast", "round_win_rate", "total_time", "rank_detail"):
                save_data[f"season_{k}" if k not in ("rank_detail",) else k] = season_stats.get(k, "")
            save_data["season_kda"] = save_data.pop("season_kda", season_stats.get("kda", ""))
            # fix the rank_detail key
            if "rank_detail" in save_data:
                save_data["season_rank_detail"] = save_data.pop("rank_detail")

        await self.repo.save_game_role(uid, save_data)

        lines = ["✅ 同步成功！", "━━━━━━━━━━━━━━━━━━━━",
                  f"👤 角色: {role_name or '?'}"]
        if season_stats:
            rank_detail = season_stats.get("rank_detail", "")
            if rank_detail:
                lines.append(f"🏅 段位: {tier_text or '?'}（{rank_detail}）")
            else:
                lines.append(f"🏅 段位: {tier_text or '?'}")
        else:
            lines.append(f"🏅 段位: {tier_text or '?'}")
        lines.append(f"📊 等级: {role_level}")
        if season_stats:
            lines.append("\n📈 **赛季数据**")
            for k, label in [("total_time", "总时长"), ("kda", "赛季KDA"), ("win_rate", "赛季胜率"),
                             ("avg_acs", "场均ACS"), ("kast", "KAST"), ("hs_pct", "精准击败(爆头率)"),
                             ("round_win_rate", "回合胜率"), ("matches", "统计场次")]:
                if season_stats.get(k):
                    lines.append(f"  {label}: {season_stats[k]}")
        lines.append(f"🆔 游戏ID: {grid[:20]}…")
        yield event.plain_result("\n".join(lines))

    # ════════════════════════════════════════════════════════════════
    # ── 子命令：战绩 ─────────────────────────────────────
    # ════════════════════════════════════════════════════════════════

    async def _cmd_matches(self, event: AstrMessageEvent, sub_args: str = ""):
        uid = str(event.get_sender_id() or "")
        count = 10
        for p in (args or "").strip().split():
            if p.isdigit():
                count = max(1, min(int(p), 50))

        target = await self.get_at_id(event)
        if target:
            uid = target

        user_config = await self.repo.get_user_config(uid)
        if not user_config:
            yield event.plain_result("该群友尚未绑定账号" if target else "请先使用 /瓦 qq 或 /瓦 wx 进行绑定")
            return

        nickname = user_config.get("role_name") or user_config.get("nickname") or f"玩家_{uid[-6:]}"
        yield event.plain_result(f"⏳ 正在查询 **{nickname}** 最近 {count} 场对战…")

        role_data, _ = await fetch_role_info(self.api, user_config)
        scene_token = ""
        grid = user_config.get("game_role_id", "")
        if role_data:
            roles = role_data.get("list", [])
            if roles:
                role = roles[0]
                scene_token = role.get("scene", "")
                grid = role.get("game_role_id", grid)

        if not scene_token:
            yield event.plain_result("❌ 无法获取 scene token，请先 /瓦 同步")
            return

        records = []
        for attempt in range(2):
            records = await self.match_api.fetch_match_list(user_config, scene_token, grid, count)
            if records:
                break
            if attempt == 0:
                await asyncio.sleep(2)

        if not records:
            yield event.plain_result("📭 暂无匹配/排位对战记录（已过滤死斗/极速等非竞技模式）")
            return

        from vallib.api.match import normalize_match
        norm = [normalize_match(m) for m in records[:count]]
        wins = sum(1 for m in norm if m.get("is_win"))
        total_k = sum(m.get("kills", 0) for m in norm)
        total_d = sum(m.get("deaths", 0) for m in norm)
        total_a = sum(m.get("assists", 0) for m in norm)
        total_score = sum(m.get("score", 0) for m in norm)
        kda_ratio = round((total_k + total_a) / max(total_d, 1), 2)
        avg_acs = round(total_score / len(norm), 1)

        lines = [
            f"📊 **{nickname}** 最近 {len(norm)} 场对战",
            "━━━━━━━━━━━━━━━━━━━━",
            f"胜率: {wins}W/{len(norm)-wins}L ({round(wins/len(norm)*100,1)}%) | "
            f"KDA: {total_k}/{total_d}/{total_a} ({kda_ratio}) | 场均ACS: {avg_acs}",
            "",
        ]
        for i, m in enumerate(norm, 1):
            icon = "✅" if m.get("is_win") else ("❌" if m.get("is_win") is False else "➖")
            hs = m.get("headshot_rate", "")
            lines.append(
                f"{i}. {icon} {m.get('queue_type','?')} | {m.get('map_name','?')} | "
                f"{m.get('agent_name','?')} | {m.get('kills',0)}/{m.get('deaths',0)}/{m.get('assists',0)} | "
                f"ACS:{m.get('score',0)}" + (f" | HS:{hs}" if hs else "")
            )
        yield event.plain_result("\n".join(lines))

    # ════════════════════════════════════════════════════════════════
    # ── 子命令：分析 ─────────────────────────────────────
    # ════════════════════════════════════════════════════════════════

    async def _cmd_analysis(self, event: AstrMessageEvent, sub_args: str = ""):
        uid = str(event.get_sender_id() or "")
        count = 25
        for p in (args or "").strip().split():
            if p.isdigit():
                count = max(1, min(int(p), 50))

        target = await self.get_at_id(event)
        if target:
            uid = target

        user_config = await self.repo.get_user_config(uid)
        if not user_config:
            yield event.plain_result("该群友尚未绑定账号" if target else "请先使用 /瓦 qq 或 /瓦 wx 进行绑定")
            return

        nickname = user_config.get("role_name") or user_config.get("nickname") or f"玩家_{uid[-6:]}"
        yield event.plain_result(f"⏳ 正在查询 **{nickname}** 最近 {count} 场对战…")

        role_data, _ = await fetch_role_info(self.api, user_config)
        scene_token = ""
        grid = user_config.get("game_role_id", "")
        account_info = {
            "role_name": user_config.get("role_name", "") or user_config.get("nickname", ""),
            "tier_text": user_config.get("tier_text", ""),
            "role_level": user_config.get("role_level", ""),
            "game_role_id": grid,
            "season_kda": user_config.get("season_kda", ""),
            "season_hs_rate": user_config.get("season_hs_rate", ""),
            "season_eval_score": user_config.get("season_eval_score", ""),
            "season_matches": user_config.get("season_matches", ""),
            "season_win_rate": user_config.get("season_win_rate", ""),
            "season_kast": user_config.get("season_kast", ""),
            "season_round_wr": user_config.get("season_round_wr", ""),
            "season_total_time": user_config.get("season_total_time", ""),
            "season_rank_detail": user_config.get("season_rank_detail", ""),
        }
        if role_data:
            roles = role_data.get("list", [])
            if roles:
                role = roles[0]
                scene_token = role.get("scene", "")
                grid = role.get("game_role_id", grid)
                await self.repo.save_game_role(uid, {
                    "game_role_id": grid,
                    "game_open_id": role.get("game_open_id", ""),
                    "tier_text": role.get("tier_text", ""),
                    "role_name": role.get("role_name", ""),
                    "role_level": str(role.get("role_level", "")),
                })
                account_info.update({
                    "role_name": role.get("role_name", "") or account_info["role_name"],
                    "tier_text": role.get("tier_text", "") or account_info["tier_text"],
                    "role_level": str(role.get("role_level", "") or "") or account_info["role_level"],
                })
                if role.get("role_name"):
                    nickname = role["role_name"]

        if not scene_token:
            yield event.plain_result("❌ 无法获取 scene token，请先 /瓦 同步")
            return

        records = []
        for attempt in range(2):
            records = await self.match_api.fetch_match_list(user_config, scene_token, grid, count, max_pages=10)
            if records:
                break
            if attempt == 0:
                await asyncio.sleep(2)

        if not records:
            yield event.plain_result("📭 暂无匹配/排位对战记录")
            return

        from vallib.api.match import normalize_match, aggregate_matches
        norm = [normalize_match(m) for m in records[:count]]
        valid = [m for m in norm if m.get("kills", 0) > 0 or m.get("match_id")]
        if not valid:
            valid = norm

        # 获取详情
        for i, m in enumerate(valid):
            battle_id = records[i].get("battle_id", "") if i < len(records) else ""
            match_id_rec = records[i].get("match_id", "") if i < len(records) else ""
            if not battle_id and not match_id_rec:
                continue
            detail = await self.match_api.fetch_battle_detail(user_config, battle_id, match_id_rec, scene_token)
            if detail:
                m["_detail"] = detail
            await asyncio.sleep(0.3)

        agg = aggregate_matches(valid)
        friend_ranking = agg.get("friend_ranking", [])

        yield event.plain_result(
            f"🤖 正在调用 AI 分析 **{nickname}** 的 {len(valid)} 场对战数据…"
            f"\n⏳ 预计需要 30-90 秒，请耐心等待"
        )
        report = await self.analysis.call_llm(
            event, nickname, valid, agg,
            friend_ranking=friend_ranking, account_info=account_info,
        )
        if not report:
            yield event.plain_result(
                "❌ AI 分析失败，请检查 LLM 配置或稍后重试。\n"
                "💡 提示：确认 AstrBot 面板已配置 LLM 模型"
            )
            return

        img_base64 = await self.analysis.render_report_image(self.html_render, report)
        if img_base64:
            image_bytes = base64.b64decode(img_base64)
            yield event.chain_result([Image.fromBytes(image_bytes)])
            return

        for chunk in [report[i:i+4000] for i in range(0, len(report), 4000)]:
            yield event.plain_result(chunk)

    # ════════════════════════════════════════════════════════════════
    # ── 子命令：用户 ─────────────────────────────────────

    async def _cmd_users(self, event: AstrMessageEvent):
        """查看所有已绑定用户的登录态（管理员）。"""
        if not event.is_admin():
            return
        all_ids = await self.repo.get_all_user_ids()
        if not all_ids:
            yield event.plain_result("暂无已绑定用户")
            return

        yield event.plain_result(f"⏳ 正在检测 {len(all_ids)} 个用户的登录状态…")

        valid_users = []
        invalid_users = []
        for uid in all_ids:
            config = await self.repo.get_user_config(uid)
            if not config:
                continue
            name = TitleBuilder.get_display_name(config, uid)
            is_valid = await self.test_config_validity(uid, config)
            if is_valid:
                valid_users.append((name, uid, config))
            else:
                invalid_users.append((name, uid))

        lines = [f"📋 用户登录状态检测（共 {len(all_ids)} 人）",
                  "━━━━━━━━━━━━━━━━━━━━"]
        if valid_users:
            lines.append(f"\n✅ 正常（{len(valid_users)} 人）：")
            for name, uid, _ in valid_users:
                lines.append(f"  - {name}")
        if invalid_users:
            lines.append(f"\n❌ 已过期（{len(invalid_users)} 人）：")
            for name, uid in invalid_users:
                lines.append(f"  - {name}（{uid}）")
        yield event.plain_result("\n".join(lines))

    # ── 子命令：帮助 ─────────────────────────────────────
    # ════════════════════════════════════════════════════════════════

    async def _cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "无畏契约助手 命令帮助\n\n"
            "🔑 账号管理：\n"
            "/瓦 qq /瓦 wx   — QQ/微信扫码登录绑定\n"
            "/瓦 清除         — 清除已绑定的登录信息\n\n"
            "🛒 商店功能：\n"
            "/瓦 商店         — 查询今日商店\n"
            "/瓦 商店 @某人   — 查询他人商店\n"
            "/瓦 监控         — 查看监控子命令帮助\n"
            "/瓦 推送         — 每日商店定时推送订阅\n\n"
            "📊 战绩查询：\n"
            "/瓦 状态         — 查看绑定和赛季数据\n"
            "/瓦 状态 @某人   — 查看他人绑定数据\n"
            "/瓦 同步         — 同步角色和赛季数据\n"
            "/瓦 战绩 [N]     — 直接查看最近N场对战\n"
            "/瓦 战绩 N @某人 — 查看他人最近N场对战\n"
            "/瓦 分析 [N]     — AI深度分析最近N场对战\n"
            "/瓦 分析 N @某人 — AI分析他人最近N场\n"
            "/瓦 用户         — 查看所有用户登录状态\n"
        )
