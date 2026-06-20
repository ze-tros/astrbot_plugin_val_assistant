"""统一数据访问层，集中所有 SQL 操作。"""
import logging
from typing import Any, Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

logger = logging.getLogger("astrbot")


class Repository:
    """用户配置 / 监控列表 / 推送订阅 的数据库操作。"""

    def __init__(self, context):
        self.context = context

    def _db(self):
        return self.context.get_db()

    # ── 用户配置 ──────────────────────────────────────────

    async def get_user_config(self, user_id: str) -> Optional[Dict[str, Any]]:
        logger.info(f"查询用户配置，user_id: {user_id}")
        async with self._db().get_db() as session:
            session: AsyncSession
            result = await session.execute(
                text("""SELECT userId, tid, nickname, auto_check, openid, access_token, login_type,
                    uin, game_role_id, game_open_id, tier_text, role_name, role_level,
                    competitive_tier, avatar_url, season_kda, season_hs_rate, season_eval_score,
                    season_matches, season_win_rate, season_kast, season_round_wr,
                    season_total_time, season_rank_detail
                    FROM valo_users WHERE user_id = :user_id"""),
                {"user_id": user_id}
            )
            row = result.fetchone()
            if row:
                return {
                    'userId': row[0], 'tid': row[1], 'nickname': row[2],
                    'auto_check': row[3] if row[3] is not None else 0,
                    'openid': row[4] if len(row) > 4 else '',
                    'access_token': row[5] if len(row) > 5 else '',
                    'login_type': row[6] if len(row) > 6 else 'qq',
                    'uin': row[7] if len(row) > 7 else '',
                    'game_role_id': row[8] if len(row) > 8 else '',
                    'game_open_id': row[9] if len(row) > 9 else '',
                    'tier_text': row[10] if len(row) > 10 else '',
                    'role_name': row[11] if len(row) > 11 else '',
                    'role_level': row[12] if len(row) > 12 else '',
                    'competitive_tier': row[13] if len(row) > 13 else '',
                    'avatar_url': row[14] if len(row) > 14 else '',
                    'season_kda': row[15] if len(row) > 15 else '',
                    'season_hs_rate': row[16] if len(row) > 16 else '',
                    'season_eval_score': row[17] if len(row) > 17 else '',
                    'season_matches': row[18] if len(row) > 18 else '',
                    'season_win_rate': row[19] if len(row) > 19 else '',
                    'season_kast': row[20] if len(row) > 20 else '',
                    'season_round_wr': row[21] if len(row) > 21 else '',
                    'season_total_time': row[22] if len(row) > 22 else '',
                    'season_rank_detail': row[23] if len(row) > 23 else '',
                }
            logger.warning(f"未找到用户 {user_id} 的配置")
        return None

    async def save_user_config(self, user_id: str, userId: str, tid: str,
                               nickname: Optional[str] = None,
                               openid: str = "", access_token: str = "",
                               login_type: str = "qq", uin: str = ""):
        logger.info(f"保存用户配置: user_id={user_id}, userId={userId[:20]}..., login_type={login_type}")
        async with self._db().get_db() as session:
            session: AsyncSession
            async with session.begin():
                await session.execute(
                    text("""
                        INSERT OR REPLACE INTO valo_users
                        (user_id, userId, tid, nickname, openid, access_token, login_type, uin, updated_at)
                        VALUES (:user_id, :userId, :tid, :nickname, :openid, :access_token, :login_type, :uin, CURRENT_TIMESTAMP)
                    """),
                    {"user_id": user_id, "userId": userId, "tid": tid, "nickname": nickname,
                     "openid": openid or "", "access_token": access_token or "",
                     "login_type": login_type or "qq", "uin": uin or ""}
                )
                logger.info(f"用户配置保存成功: user_id={user_id}")

    async def save_game_role(self, user_id: str, data: Dict[str, Any]):
        """仅更新角色/赛季字段，不影响 userId/tid。"""
        fields = [
            "game_role_id", "game_open_id", "tier_text", "role_name", "role_level",
            "competitive_tier", "avatar_url", "season_kda", "season_hs_rate",
            "season_eval_score", "season_matches", "season_win_rate", "season_kast",
            "season_round_wr", "season_total_time", "season_rank_detail",
        ]
        set_clauses = [f"{f}=:{f}" for f in fields]
        params = {"user_id": user_id}
        for f in fields:
            params[f] = data.get(f, "")
        async with self._db().get_db() as session:
            session: AsyncSession
            async with session.begin():
                await session.execute(
                    text(f"UPDATE valo_users SET {', '.join(set_clauses)} WHERE user_id=:user_id"),
                    params,
                )
        logger.info(f"角色/赛季数据保存成功: user_id={user_id}")

    async def clear_user_config(self, user_id: str) -> bool:
        logger.info(f"清除用户配置: user_id={user_id}")
        async with self._db().get_db() as session:
            session: AsyncSession
            async with session.begin():
                result = await session.execute(
                    text("DELETE FROM valo_users WHERE user_id = :user_id"),
                    {"user_id": user_id}
                )
                return int(result.rowcount or 0) > 0

    # ── 监控列表 ──────────────────────────────────────────

    async def get_watchlist(self, user_id: str) -> List[Dict[str, Any]]:
        async with self._db().get_db() as session:
            session: AsyncSession
            result = await session.execute(
                text("SELECT item_name, created_at FROM valo_watchlist WHERE user_id = :user_id ORDER BY created_at"),
                {"user_id": user_id}
            )
            rows = result.fetchall()
            watchlist = [{'item_name': row[0], 'created_at': row[1]} for row in rows]
            logger.info(f"用户 {user_id} 监控项数量: {len(watchlist)}")
            return watchlist

    async def add_watch_item(self, user_id: str, item_name: str) -> bool:
        async with self._db().get_db() as session:
            session: AsyncSession
            async with session.begin():
                result = await session.execute(
                    text("SELECT COUNT(*) FROM valo_watchlist WHERE user_id = :user_id AND item_name = :item_name"),
                    {"user_id": user_id, "item_name": item_name}
                )
                if result.scalar() > 0:
                    return False
                await session.execute(
                    text("INSERT INTO valo_watchlist (user_id, item_name) VALUES (:user_id, :item_name)"),
                    {"user_id": user_id, "item_name": item_name}
                )
                logger.info(f"用户 {user_id} 添加监控项: {item_name}")
                return True

    async def remove_watch_item(self, user_id: str, item_name: str) -> bool:
        async with self._db().get_db() as session:
            session: AsyncSession
            async with session.begin():
                result = await session.execute(
                    text("DELETE FROM valo_watchlist WHERE user_id = :user_id AND item_name = :item_name"),
                    {"user_id": user_id, "item_name": item_name}
                )
                if result.rowcount > 0:
                    logger.info(f"用户 {user_id} 删除监控项: {item_name}")
                    return True
                return False

    async def update_auto_check(self, user_id: str, status: int):
        async with self._db().get_db() as session:
            session: AsyncSession
            async with session.begin():
                await session.execute(
                    text("UPDATE valo_users SET auto_check = :status, updated_at = CURRENT_TIMESTAMP WHERE user_id = :user_id"),
                    {"status": status, "user_id": user_id}
                )
                logger.info(f"用户 {user_id} 自动查询状态更新为: {status}")

    # ── 每日推送订阅 ──────────────────────────────────────

    async def add_daily_push_sub(self, user_id: str, session_id: str) -> bool:
        async with self._db().get_db() as session:
            session: AsyncSession
            async with session.begin():
                result = await session.execute(
                    text("SELECT COUNT(*) FROM valo_daily_push WHERE user_id = :user_id AND session_id = :session_id"),
                    {"user_id": user_id, "session_id": session_id}
                )
                if result.scalar() > 0:
                    return False
                await session.execute(
                    text("INSERT INTO valo_daily_push (user_id, session_id) VALUES (:user_id, :session_id)"),
                    {"user_id": user_id, "session_id": session_id}
                )
                logger.info(f"用户 {user_id} 订阅每日推送，会话: {session_id}")
                return True

    async def remove_daily_push_sub(self, user_id: str, session_id: str) -> bool:
        async with self._db().get_db() as session:
            session: AsyncSession
            async with session.begin():
                result = await session.execute(
                    text("DELETE FROM valo_daily_push WHERE user_id = :user_id AND session_id = :session_id"),
                    {"user_id": user_id, "session_id": session_id}
                )
                if result.rowcount > 0:
                    logger.info(f"用户 {user_id} 取消每日推送订阅，会话: {session_id}")
                    return True
                return False

    async def get_daily_push_subs(self) -> List[Dict[str, Any]]:
        async with self._db().get_db() as session:
            session: AsyncSession
            result = await session.execute(
                text("SELECT user_id, session_id, created_at FROM valo_daily_push ORDER BY created_at")
            )
            rows = result.fetchall()
            subs = [{'user_id': row[0], 'session_id': row[1], 'created_at': row[2]} for row in rows]
            logger.info(f"每日推送订阅数量: {len(subs)}")
            return subs

    async def get_user_daily_push_list(self, user_id: str) -> List[Dict[str, Any]]:
        """获取用户在的所有推送订阅。"""
        async with self._db().get_db() as session:
            session: AsyncSession
            result = await session.execute(
                text("SELECT session_id, created_at FROM valo_daily_push WHERE user_id = :user_id ORDER BY created_at"),
                {"user_id": user_id}
            )
            return [{'session_id': row[0], 'created_at': row[1]} for row in result.fetchall()]

    async def get_user_daily_push_status(self, user_id: str, session_id: str) -> bool:
        async with self._db().get_db() as session:
            session: AsyncSession
            result = await session.execute(
                text("SELECT COUNT(*) FROM valo_daily_push WHERE user_id = :user_id AND session_id = :session_id"),
                {"user_id": user_id, "session_id": session_id}
            )
            return result.scalar() > 0

    async def get_all_user_ids(self) -> List[str]:
        """获取所有已绑定的用户 ID 列表。"""
        async with self._db().get_db() as session:
            session: AsyncSession
            result = await session.execute(
                text("SELECT user_id FROM valo_users")
            )
            return [row[0] for row in result.fetchall()]

    async def get_auto_check_users(self) -> List[str]:
        """获取所有开启自动监控的用户 ID 列表。"""
        async with self._db().get_db() as session:
            session: AsyncSession
            result = await session.execute(
                text("SELECT user_id FROM valo_users WHERE auto_check = 1")
            )
            return [row[0] for row in result.fetchall()]
