"""AI 对局分析服务。"""
import base64
import logging
import os
from typing import Any, Dict, List, Optional

from ..api.match import normalize_match, aggregate_matches
from ..api.client import APIClient

logger = logging.getLogger("astrbot")

SYSTEM_PROMPT = """你是一位无畏契约（Valorant）顶级职业分析师，以毒舌、一针见血著称。请根据以下玩家近期对战数据，生成一份锐利深刻的分析报告。注意：在报告中你不需要介绍自己，直接生成报告内容即可

⚠️ **数据说明**："未获取"字段注明缺失，不要假设为0。

## 分析维度（必须全部覆盖，顺序固定）

### 1. 📊 综合战绩概览
- KDA、胜率、场均ACS、伤害、爆头率
- 与同段位平均对比

### 2. 🎯🦸 战术风格 + 英雄池（合并为1节）
- 基于首杀/首死/KDA结构判断打法类型（激进突破/稳健补枪/老六断后/灵活支援）
- 最常用英雄的表现对比，英雄池广度评估
- **给玩家起一个精准的绰号**（如"人形突破口"、"闪光弹发射器"、"五杀绝缘体"）

### 3. 🔫 枪法与瞄准
- 爆头率分析、伤害转化效率、段位对标

### 4. 🗺️ 地图理解
- 各地图胜率对比，最强/最弱地图分析

### 5. 👥 团队配合 + 开黑队友锐评
- 好友识别（is_friend=1），分析开黑配合效果
- 给每个队友起绰号和锐评（毒舌但有趣）

### 6. 📈 近期趋势
- 状态走势分析

### 7. 💡 改进建议
- 2条最关键的提升建议，简洁有力

### 8. ⭐ 综合评分
- 基于所有维度给出综合评价和评分（/100）
"""


class AnalysisService:
    """AI 分析：prompt 构建、LLM 调用、报告渲染。"""

    def __init__(self, plugin_dir: str, match_api=None):
        self.plugin_dir = plugin_dir
        self.match_api = match_api
        self._template = None

    # ── Prompt 构建 ──────────────────────────────────────

    @staticmethod
    def build_prompt(nickname: str, matches: List[Dict], agg: Dict,
                     friend_ranking: Optional[List[Dict]] = None,
                     account_info: Optional[Dict] = None) -> str:
        account_header = ""
        if account_info:
            parts = ["## 账号信息"]
            if account_info.get("role_name"):
                parts.append(f"- 角色名：**{account_info['role_name']}**")
            if account_info.get("tier_text"):
                rd = account_info.get("season_rank_detail", "")
                parts.append(f"- 当前段位：**{account_info['tier_text']}**{'（' + rd + '）' if rd else ''}")
            if account_info.get("role_level"):
                parts.append(f"- 账号等级：**{account_info['role_level']}**")
            if account_info.get("season_total_time"):
                parts.append(f"- 游戏总时长：**{account_info['season_total_time']}**")
            if account_info.get("season_kda"):
                parts.append(f"- 赛季KDA：**{account_info['season_kda']}**")
            if account_info.get("season_win_rate"):
                parts.append(f"- 赛季胜率：**{account_info['season_win_rate']}**")
            if account_info.get("season_eval_score"):
                parts.append(f"- 场均ACS：**{account_info['season_eval_score']}**")
            if account_info.get("season_kast"):
                parts.append(f"- KAST：**{account_info['season_kast']}**")
            if account_info.get("season_hs_rate"):
                hs = account_info["season_hs_rate"]
                parts.append(f"- 精准击败(爆头率)：**{hs}{'%' if '%' not in str(hs) else ''}**")
            if account_info.get("season_round_wr"):
                parts.append(f"- 回合胜率：**{account_info['season_round_wr']}**")
            if account_info.get("season_matches"):
                parts.append(f"- 赛季场次：**{account_info['season_matches']}**")
            account_header = "\n".join(parts) + "\n\n"

        agent_lines = []
        for a_info in agg.get("top_agents", [])[:6]:
            agent_lines.append(
                f"| {a_info['name']} | {a_info['cnt']}场 | {a_info['wr']}% | "
                f"KDA {a_info['avg_kda']} | ACS {a_info['avg_score']} |")
        agent_table = "\n".join(agent_lines) or "无数据"

        map_lines = []
        for mp, s in sorted(agg.get("map_stats", {}).items(), key=lambda x: x[1]["cnt"], reverse=True):
            map_lines.append(f"| {mp} | {s['cnt']}场 | {s['wr']}% |")
        map_table = "\n".join(map_lines) or "无数据"

        friend_lines = []
        if friend_ranking:
            for i, f in enumerate(friend_ranking[:10], 1):
                friend_lines.append(
                    f"{i}. {f['name']} | {f['games']}场 | "
                    f"KDA {f['avg_k']}/{f['avg_d']}/{f['avg_a']}({f['kda']}) | "
                    f"ACS {f['acs']} | 爆头率 {f['hs_pct']}% | "
                    f"常用 {f['top_agent']}({f['top_agent_cnt']}次) | "
                    f"段位 {f['tier']} | 综合分 {f['score']}"
                )
        fm_text = "\n".join(friend_lines) if friend_lines else "无数据"

        records = []
        for i, m in enumerate(matches, 1):
            icon = "✅" if m.get("is_win") else ("❌" if m.get("is_win") is False else "➖")
            records.append(
                f"{i}. {icon} {m.get('queue_type','?')} | {m.get('map_name','?')} | "
                f"英雄:{m.get('agent_name','?')} | "
                f"KDA:{m.get('kills',0)}/{m.get('deaths',0)}/{m.get('assists',0)}({m.get('kda',0)}) | "
                f"ACS:{m.get('score',0)} | 爆头率:{m.get('headshot_rate','?')}"
            )
        match_text = "\n".join(records)

        return f"""{account_header}## 综合汇总
- 场次: {agg.get('matches', 0)} | 胜率: {agg.get('win_rate', 0)}% ({agg.get('wins', 0)}W/{agg.get('losses', 0)}L)
- 场均KDA: {agg.get('avg_kda', 0)} | 场均ACS: {agg.get('avg_acs', 0)}

## 英雄池表现
| 英雄 | 场次 | 胜率 | KDA | ACS |
|------|------|------|-----|-----|
{agent_table}

## 地图数据
| 地图 | 场次 | 胜率 |
|------|------|------|
{map_table}

## 开黑队友排名
{fm_text}

## 逐场明细
{match_text}
"""

    # ── LLM 调用 ─────────────────────────────────────────

    async def call_llm(self, event, nickname: str, matches: List[Dict], agg: Dict,
                       friend_ranking: Optional[List[Dict]] = None,
                       account_info: Optional[Dict] = None) -> Optional[str]:
        try:
            umo = event.unified_msg_origin
            provider_id = await event.get_context().get_current_chat_provider_id(umo=umo)
            if not provider_id:
                return None
            data = self.build_prompt(nickname, matches, agg, friend_ranking, account_info)
            resp = await event.get_context().llm_generate(
                chat_provider_id=provider_id,
                prompt=f"{SYSTEM_PROMPT}\n\n{data}"
            )
            text = resp.completion_text
            return text if text and len(text.strip()) >= 50 else None
        except Exception as e:
            logger.error(f"[ValAnalysis] LLM 异常: {e}")
            return None

    # ── 报告渲染 ─────────────────────────────────────────

    async def render_report_image(self, html_render_func, markdown_text: str) -> Optional[str]:
        """将 Markdown 分析报告渲染为图片。"""
        try:
            import markdown as md_lib
            html_body = md_lib.markdown(markdown_text, extensions=['tables', 'fenced_code', 'codehilite'])
        except ImportError:
            logger.warning("[ValAnalysis] markdown 库未安装，跳过图片渲染")
            return None
        except Exception as e:
            logger.warning(f"[ValAnalysis] Markdown 渲染失败: {e}")
            return None

        try:
            if self._template is None:
                tmpl_path = os.path.join(self.plugin_dir, "analysis_template.html")
                with open(tmpl_path, "r", encoding="utf-8") as f:
                    self._template = f.read()
            html_str = self._template.replace("{{ content }}", html_body)

            result_path = await html_render_func(
                html_str, {}, return_url=False, options={"type": "png"},
            )
            if not result_path or not os.path.exists(result_path):
                return None

            with open(result_path, 'rb') as f:
                img_bytes = f.read()
            try:
                os.remove(result_path)
            except Exception:
                pass
            return base64.b64encode(img_bytes).decode('utf-8')
        except Exception as e:
            logger.warning(f"[ValAnalysis] T2I 渲染失败: {e}")
            return None
