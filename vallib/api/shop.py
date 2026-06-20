"""商店接口：商品列表获取和商店 API 请求。"""
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from .client import APIClient

logger = logging.getLogger("astrbot")

STORE_URL = "https://app.mval.qq.com/go/mlol_store/agame/user_store"


class ShopAPI:
    """商店相关 API 操作。"""

    def __init__(self, api_client: APIClient):
        self.api = api_client

    @staticmethod
    def extract_goods_list(response_data: Dict[str, Any]) -> Tuple[Optional[List[Dict]], Optional[str]]:
        """从 API 响应中提取商品列表，合并每日商店和王国商店。"""
        if "data" not in response_data:
            logger.error("API 返回数据格式不正确，缺少'data'字段")
            return None, "商店接口返回格式异常，请稍后重试"

        if not response_data["data"]:
            logger.info("API 返回数据为空")
            return [], None

        data = response_data["data"]
        if not isinstance(data, list):
            logger.error("API 返回数据格式不正确，data 不是列表")
            return None, "商店接口返回格式异常，请稍后重试"

        all_goods = []
        for store in data:
            if not isinstance(store, dict):
                continue
            store_key = store.get("key", "")
            store_title = store.get("title", "")
            goods_list = store.get("list", [])
            for goods in goods_list:
                goods["_store_key"] = store_key
                goods["_store_title"] = store_title
            all_goods.extend(goods_list)

        if not all_goods:
            logger.info("所有商店均无商品")
            return [], None

        logger.info(f"获取到 {len(all_goods)} 个商品 (来自 {len(data)} 个商店)")
        return all_goods, None

    async def request_store_api(
        self,
        user_id: str,
        user_config: Dict[str, Any],
        max_retries: int = 3,
        timeout: int = 15,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str], bool]:
        """请求商店接口，返回 (response_data, err_msg, auth_invalid)。"""
        logger.info(f"开始请求商店接口，user_id: {user_id}, userId: {user_config.get('userId', '未知')}")

        if not all(k in user_config for k in ["userId", "tid"]):
            return None, "配置不完整，需要包含 userId 和 tid", False

        headers = APIClient.build_store_headers(user_config)

        for attempt in range(max_retries):
            timestamp = int(time.time())
            data = {"_t": timestamp}
            try:
                logger.info(f"发送 API 请求到 {STORE_URL} (尝试 {attempt + 1}/{max_retries})")
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        STORE_URL,
                        headers=headers,
                        json=data,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                    ) as response:
                        response.raise_for_status()
                        response_data = await response.json()
                        logger.info(f"API 响应: {json.dumps(response_data, indent=2, ensure_ascii=False)}")

                        result_code = response_data.get("result")
                        if result_code != 0:
                            err_msg = response_data.get("errMsg") or response_data.get("msg") or "未知错误"
                            auth_invalid = APIClient.is_auth_invalid(result_code, err_msg)
                            log_method = logger.warning if auth_invalid else logger.error
                            log_method(f"API 请求失败，错误码: {result_code}，错误信息: {err_msg}")
                            return None, err_msg, auth_invalid

                        return response_data, None, False

            except aiohttp.ClientError as e:
                logger.error(f"网络请求失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    continue
                return None, "请求商店接口失败，请稍后重试", False
            except Exception as e:
                logger.error(f"处理失败 (尝试 {attempt + 1}/{max_retries}): {e}", exc_info=True)
                if attempt < max_retries - 1:
                    continue
                return None, "处理商店数据时出错，请稍后重试", False

        return None, "请求商店接口失败，请稍后重试", False

    async def get_shop_items_raw(self, user_id: str, user_config: Dict[str, Any]) -> Optional[List[Dict]]:
        """获取商店原始商品列表，失败返回 None。"""
        response_data, err_msg, auth_invalid = await self.request_store_api(user_id, user_config)
        if not response_data:
            if auth_invalid:
                logger.warning(f"用户 {user_id} 登录凭证已失效: {err_msg}")
            elif err_msg:
                logger.error(f"获取商店原始数据失败: {err_msg}")
            return None

        goods_list, parse_err_msg = self.extract_goods_list(response_data)
        if parse_err_msg:
            logger.error(f"解析商店原始数据失败: {parse_err_msg}")
            return None
        return goods_list or None
