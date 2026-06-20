"""商店图片生成服务。"""
import base64
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from ..api.shop import ShopAPI
from ..title_builder import TitleBuilder

logger = logging.getLogger("astrbot")


class ShopImageService:
    """商店图片生成：下载商品图片 + HTML 模板渲染。"""

    def __init__(self, plugin_dir: str, shop_api: ShopAPI = None):
        self.plugin_dir = plugin_dir
        self.shop_api = shop_api
        self._template = None

    # ── 路径安全 ─────────────────────────────────────────

    @staticmethod
    def _get_safe_dir(user_id: str) -> Path:
        import hashlib
        base_dir = Path("./temp/valo").resolve()
        raw_user_id = str(user_id or "").strip()
        if not raw_user_id:
            raise ValueError("user_id 为空")
        normalized = re.sub(r"[^0-9A-Za-z_-]", "_", raw_user_id).strip("_")
        digest = hashlib.sha256(raw_user_id.encode("utf-8")).hexdigest()[:10]
        safe_segment = f"{(normalized[:32] or 'user')}_{digest}"
        user_dir = (base_dir / safe_segment).resolve()
        if user_dir != base_dir and base_dir not in user_dir.parents:
            raise ValueError(f"检测到非法临时目录路径: {raw_user_id}")
        return user_dir

    @staticmethod
    def _safe_file_path(user_id: str, filename: str) -> Path:
        safe_filename = Path(str(filename or "")).name
        if not safe_filename:
            raise ValueError("文件名为空")
        user_dir = ShopImageService._get_safe_dir(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        file_path = (user_dir / safe_filename).resolve()
        if file_path.parent != user_dir:
            raise ValueError(f"检测到非法临时文件路径: {filename}")
        return file_path

    # ── 图片下载 ─────────────────────────────────────────

    @staticmethod
    async def download_image(url: str, user_id: str, filename: str) -> Optional[str]:
        try:
            filepath = ShopImageService._safe_file_path(user_id, filename)
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    response.raise_for_status()
                    content = await response.read()
                    with open(filepath, 'wb') as file:
                        file.write(content)
                    return str(filepath)
        except ValueError as e:
            logger.error(f"构建临时文件路径失败: {e}")
            return None
        except aiohttp.ClientError as e:
            logger.error(f"下载图片失败: {e}")
            return None

    # ── 主流程 ───────────────────────────────────────────

    async def generate(
        self,
        user_id: str,
        user_config: Dict[str, Any],
        html_render_func,
        keep_file: bool = False,
        goods_list: Optional[List[Dict]] = None,
        title_text: Optional[str] = None,
        date_str: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """生成每日商店图片。

        返回 (base64_data, error_msg)。
        """
        logger.info(f"开始获取商店数据，user_id: {user_id}")

        if goods_list is None and self.shop_api:
            goods_list = await self.shop_api.get_shop_items_raw(user_id, user_config)

        if not goods_list:
            return None, None

        # 下载商品图片并转为 base64 data URI
        goods_data = []
        for idx, goods in enumerate(goods_list):
            try:
                bg_img_path = await self.download_image(
                    goods.get('bg_image', ''), user_id, f'bg_{idx}.jpg')
                goods_img_path = await self.download_image(
                    goods.get('goods_pic', ''), user_id, f'goods_{idx}.jpg')
                if not bg_img_path or not goods_img_path:
                    continue

                with open(bg_img_path, 'rb') as f:
                    bg_b64 = base64.b64encode(f.read()).decode('utf-8')
                with open(goods_img_path, 'rb') as f:
                    img_b64 = base64.b64encode(f.read()).decode('utf-8')

                goods_data.append({
                    'bg_img': bg_b64,
                    'goods_img': img_b64,
                    'goods_name': goods.get('goods_name', ''),
                    'rmb_price': goods.get('rmb_price', '0'),
                    '_store_key': goods.get('_store_key', 'dailystore'),
                    '_store_title': goods.get('_store_title', '每日商店'),
                })

                for p in (bg_img_path, goods_img_path):
                    if p and os.path.exists(p):
                        os.remove(p)
            except Exception as e:
                logger.error(f"下载/处理商品图片失败: {e}")

        if not goods_data:
            logger.error("没有商品图片处理成功")
            return None, None

        # 构建标题
        title_full = TitleBuilder.build_shop_title(title_text or "", date_str)

        # 按商店类型分组并构建 HTML
        stores: Dict[str, dict] = {}
        for g in goods_data:
            key = g['_store_key']
            if key not in stores:
                stores[key] = {'title': g['_store_title'], 'items': []}
            stores[key]['items'].append(g)

        goods_html_parts = []
        for store_key in ("dailystore", "kingdomstore"):
            if store_key not in stores:
                continue
            store = stores[store_key]
            goods_html_parts.append(
                f'<div class="store-section">'
                f'<h2 class="store-title">{store["title"]}</h2>'
                f'<div class="store-grid">'
            )
            for g in store['items']:
                goods_html_parts.append(
                    '<div class="item-card">'
                    '<div class="item-image-wrap">'
                    f'<img class="item-bg" src="data:image/jpeg;base64,{g["bg_img"]}" alt="" />'
                    f'<img class="item-overlay" src="data:image/jpeg;base64,{g["goods_img"]}" alt="" />'
                    '</div>'
                    '<div class="item-info">'
                    f'<span class="item-name">{g["goods_name"]}</span>'
                    f'<span class="item-price">{g["rmb_price"]}</span>'
                    '</div>'
                    '</div>'
                )
            goods_html_parts.append('</div></div>')
        goods_html = "\n".join(goods_html_parts)

        # 加载模板
        if self._template is None:
            tmpl_path = os.path.join(self.plugin_dir, "shop_template.html")
            with open(tmpl_path, "r", encoding="utf-8") as f:
                self._template = f.read()

        html_str = self._template.replace("{{ title_text }}", title_full)
        html_str = re.sub(
            r"\{% if goods_list %\}.*?\{% endif %\}",
            goods_html if goods_html else '<div class="empty-state">今日商店暂无数据</div>',
            html_str,
            flags=re.DOTALL,
        )
        html_str = re.sub(r"\{[%{].*?[%}]\}", "", html_str)

        # T2I 渲染
        try:
            result_path = await html_render_func(
                html_str, {}, return_url=False, options={"type": "png"},
            )
        except Exception as e:
            logger.error(f"T2I 渲染失败: {e}")
            return None, None

        if not result_path or not os.path.exists(result_path):
            logger.error("T2I 渲染未返回有效图片路径")
            return None, None

        with open(result_path, 'rb') as f:
            img_bytes = f.read()
        base64_data = base64.b64encode(img_bytes).decode('utf-8')

        # 清理
        try:
            os.remove(result_path)
        except Exception:
            pass
        try:
            user_temp_dir = ShopImageService._get_safe_dir(user_id)
            if user_temp_dir.exists():
                shutil.rmtree(user_temp_dir)
        except Exception:
            pass

        logger.info("商店图片生成完成")
        return base64_data, None
