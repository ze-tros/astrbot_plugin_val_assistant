"""数据库迁移：建表和加列。"""
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

logger = logging.getLogger("astrbot")


async def run_migrations(context):
    """执行建表和兼容旧版数据库的列新增。"""
    db = context.get_db()

    # 创建用户配置表
    async with db.get_db() as session:
        session: AsyncSession
        async with session.begin():
            await session.execute(text("""
                CREATE TABLE IF NOT EXISTS valo_users (
                    user_id TEXT PRIMARY KEY,
                    userId TEXT NOT NULL,
                    tid TEXT NOT NULL,
                    nickname TEXT,
                    auto_check INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))

    # 创建监控列表表
    async with db.get_db() as session:
        session: AsyncSession
        async with session.begin():
            await session.execute(text("""
                CREATE TABLE IF NOT EXISTS valo_watchlist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    item_name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES valo_users(user_id),
                    UNIQUE(user_id, item_name)
                )
            """))

    # 创建每日推送订阅表
    async with db.get_db() as session:
        session: AsyncSession
        async with session.begin():
            await session.execute(text("""
                CREATE TABLE IF NOT EXISTS valo_daily_push (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, session_id)
                )
            """))

    # 兼容旧版数据库：新增字段
    async with db.get_db() as session:
        session: AsyncSession
        async with session.begin():
            for col, default_val in (
                ("openid", "TEXT DEFAULT ''"),
                ("access_token", "TEXT DEFAULT ''"),
                ("login_type", "TEXT DEFAULT 'qq'"),
                ("uin", "TEXT DEFAULT ''"),
                ("game_role_id", "TEXT DEFAULT ''"),
                ("game_open_id", "TEXT DEFAULT ''"),
                ("tier_text", "TEXT DEFAULT ''"),
                ("role_name", "TEXT DEFAULT ''"),
                ("role_level", "TEXT DEFAULT ''"),
                ("competitive_tier", "TEXT DEFAULT ''"),
                ("avatar_url", "TEXT DEFAULT ''"),
                ("season_kda", "TEXT DEFAULT ''"),
                ("season_hs_rate", "TEXT DEFAULT ''"),
                ("season_eval_score", "TEXT DEFAULT ''"),
                ("season_matches", "TEXT DEFAULT ''"),
                ("season_win_rate", "TEXT DEFAULT ''"),
                ("season_kast", "TEXT DEFAULT ''"),
                ("season_round_wr", "TEXT DEFAULT ''"),
                ("season_total_time", "TEXT DEFAULT ''"),
                ("season_rank_detail", "TEXT DEFAULT ''"),
            ):
                try:
                    await session.execute(
                        text(f"ALTER TABLE valo_users ADD COLUMN {col} {default_val}")
                    )
                except Exception:
                    pass  # 列已存在，忽略

    logger.info("数据库迁移完成")
