# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

这是一个 AstrBot 插件，为无畏契约（Valorant）提供 QQ/微信机器人集成。提供每日商店查询、商品监控、对局战绩查询和 AI 深度分析功能。

## 架构

```
main.py                          # 插件入口，ValorantShopPlugin(Star)，所有命令处理器
vallib/
  api/
    client.py                    # APIClient — HTTP 客户端，SSL忽略，Cookie/Header 构造，重试
    shop.py                      # ShopAPI — 商店接口 (request_store_api, extract_goods_list)
    match.py                     # MatchAPI — 对局列表、详情、赛季数据探测；normalize_match, aggregate_matches
    auth.py                      # QQ/微信 login_by_qq, login_by_wechat, get_final_cookies
    role.py                      # fetch_role_info — 获取游戏角色信息
  db/
    repository.py                # Repository — 所有 SQL 操作 (用户配置/监控列表/推送订阅)
    migrations.py                # run_migrations — 建表 + 兼容旧版字段新增
  services/
    scheduler.py                 # SchedulerService — APScheduler 定时任务 (每日监控+推送)
    shop_image.py                # ShopImageService — 下载商品图片 + HTML模板渲染 → T2I 图片
    analysis.py                  # AnalysisService — LLM prompt 构建 + 调用 + Markdown→图片渲染
    notification.py              # send_notification — 监控命中通知
  login/
    qq_login.py                  # QQLoginFlow — QQ 二维码生成、轮询、token 提取
    wechat_login.py              # WechatLoginFlow — 微信二维码获取、轮询
    token_refresh.py             # try_refresh_credentials — token 过期自动续期
  title_builder.py               # TitleBuilder — 显示名/标题构建工具
_conf_schema.json                # 插件配置 schema (监控时间/时区/bot_id/登录回调URL等)
shop_template.html               # 商店图片 HTML 模板 (Jinja2 风格占位符)
analysis_template.html           # AI 分析报告 HTML 模板 ({{ content }} 插值)
```

## 核心模式

### 命令处理器

所有命令定义在 `main.py` 的 `ValorantShopPlugin` 中，用 `@filter.command()` 装饰。处理器是 **async generator**，通过 `yield event.plain_result()` 返回文本或 `yield event.chain_result()` 返回图片。

```
/瓦、/瓦 qq、/瓦 wx、/瓦 清除    — 账号绑定
/每日商店、/每日商店 @某人       — 查询商店
/商店监控 [添加|删除|列表|查询|开启|关闭]  — 商品监控
/每日推送 [订阅|取消订阅|状态]   — 每日推送订阅
/瓦状态、/瓦同步、/瓦战绩 [N]、/瓦分析 [N]  — 数据查询
/瓦帮助                          — 帮助
```

### 数据流

用户凭证保存在 SQLite（通过 AstrBot 的 `context.get_db()` 访问，表 `valo_users`/`valo_watchlist`/`valo_daily_push`）。所有 SQL 通过 `Repository` 类集中管理，使用 SQLAlchemy 的 `text()` 执行。

API 请求都打到 `https://app.mval.qq.com`，Cookie 格式有「商店接口」和「通用接口」两种变体。凭证失效检测在 `APIClient.is_auth_invalid()` 中（错误码 1001/1003/999999 或 ticket expire）。

### 图片生成

商店和 AI 分析报告都通过「HTML 模板 → AstrBot 的 `html_render` T2I 引擎 → base64 PNG」流水线生成。商店图片需要先下载商品图片再内联 base64。

### 定时任务

`SchedulerService` 使用 APScheduler 的 `AsyncIOScheduler`，在 `initialize()` 时启动两个 cron job：
- `daily_auto_check` — 遍历开启监控的用户，查询商店并匹配监控项
- `daily_push_check` — 向所有订阅用户推送每日商店图片

### 登录流程

QQ 登录：直接 HTTP 调用 `xui.ptlogin2.qq.com` 生成二维码 → 轮询 `ptqrlogin` → 提取 `openid`/`access_token` → `login_by_qq` 换取 `userId`/`tid`。

微信登录：通过 `app.mval.qq.com` 获取 sdk_ticket → `open.weixin.qq.com` 生成二维码 → 长轮询 `long.open.weixin.qq.com` → `login_by_wechat` 换取凭证。

两种登录方式最终都保存 `userId`/`tid`/`openid`/`access_token`/`login_type` 到数据库。
