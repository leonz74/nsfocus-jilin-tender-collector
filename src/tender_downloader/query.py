from __future__ import annotations

import copy
import re
from datetime import date
from pathlib import Path

from .classify import INDUSTRIES
from .config import AppConfig
from .geography import query_region


INDUSTRY_TERMS = {
    "金融": ["银行", "农信", "信用社", "证券", "保险公司", "金融"],
    "教育": ["学校", "大学", "学院", "中学", "小学", "教育"],
    "医疗卫生": ["医院", "卫生", "疾控", "医疗"],
    "党政": ["人民政府", "委员会", "管理局", "机关"],
    "政法公安": ["公安", "法院", "检察院", "司法", "监狱"],
    "交通物流": ["交通", "铁路", "公路", "机场", "物流"],
    "能源电力": ["电力", "供电", "能源", "燃气", "石油"],
    "通信广电": ["通信", "移动", "联通", "电信", "广播"],
    "科研": ["研究院", "研究所", "实验室"],
    "公共事业": ["水务", "供水", "供热", "市政"],
    "文旅": ["旅游", "博物馆", "图书馆", "景区"],
}
DEFAULT_QUERY_TERMS = ["网络安全", "信息安全", "数据安全", "等保", "密码", "防火墙", "安全服务", "信息化"]


def normalise_query(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("查询条件必须是对象")
    result = {}
    for key in ("start_date", "end_date", "city", "industry", "keyword", "notice_type", "province", "district"):
        if key in {"province", "district"} and key not in value:
            continue
        item = value.get(key, "")
        if not isinstance(item, str) or len(item) > 100 or "\x00" in item:
            raise ValueError(f"{key} 格式无效")
        result[key] = item.strip()
    start, end = date.fromisoformat(result["start_date"]), date.fromisoformat(result["end_date"])
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    if result["industry"] and result["industry"] not in INDUSTRIES:
        raise ValueError("请选择有效行业")
    if result["keyword"] and not re.fullmatch(r"[\w\s./()+\-]{1,100}", result["keyword"]):
        raise ValueError("关键词请使用文字、数字、空格或普通连接符")
    # Typing 金融 while also choosing 金融 means the same requirement,
    # not a requirement that every bank's name literally contain 金融.
    if result["keyword"] == result["industry"]:
        result["keyword"] = ""
    region = query_region(result)
    if "province" in result or "district" in result:
        result.update({key: region[key] for key in ("province", "city", "district")})
    mode = value.get("mode", "standard")
    if not isinstance(mode, str) or mode not in {"standard", "ai_recall"}:
        raise ValueError("查询模式必须是 standard 或 ai_recall")
    if mode == "ai_recall":
        limit = value.get("max_candidates", 100)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 10 <= limit <= 500:
            raise ValueError("AI 防漏检测每来源上限必须为 10 到 500 条")
        result.update(mode=mode, max_candidates=limit)
    return result


def query_config(config: AppConfig, criteria: dict, query_id: str) -> AppConfig:
    criteria = normalise_query(criteria)
    if not re.fullmatch(r"[a-f0-9]{32}", query_id):
        raise ValueError("查询标识无效")
    data = copy.deepcopy(config.data)
    data.update(start_date=criteria["start_date"], end_date=criteria["end_date"])
    terms = ([criteria["keyword"]] if criteria["keyword"] else
             INDUSTRY_TERMS.get(criteria["industry"], [""]))
    ai_recall = criteria.get("mode") == "ai_recall"
    baseline_terms = list(terms)
    if ai_recall:
        # Fetch by date and platform region, so title vocabulary cannot hide
        # candidates before the model sees their actual notice text.
        terms = [""]
    data["_query"] = {"id": query_id, "criteria": criteria, "terms": terms}
    if ai_recall:
        data["_query"]["baseline_terms"] = baseline_terms
    data.setdefault("delivery", {})["mode"] = "on_demand"
    data.setdefault("ai", {})["enabled"] = ai_recall
    data.setdefault("recall", {})["mode"] = "p0_complete"
    data.setdefault("http", {})["max_retries"] = min(1, int(data.get("http", {}).get("max_retries", 1)))
    region = query_region(criteria)
    for source in data["sources"]:
        source["_query_region"] = region
        source_type = source.get("type")
        source["_query_terms"] = terms
        if ai_recall:
            source["_baseline_terms"] = baseline_terms
        if source_type == "jilin_ggzy":
            source["_query_all_channels"] = True
            if region["province"] not in {"", "吉林省"}:
                source["_query_unsupported"] = "此平台仅覆盖吉林省，不适用于本次所选地区。"
        elif source_type == "ccgp_search":
            if any(terms):
                source["keywords"] = terms
            else:
                # Blank is a date-only query, not a saved keyword entry.
                source.pop("keywords", None)
            source["window_days"] = 366
        elif source_type == "national_ggzy":
            source["window_days"] = 366
        elif source_type == "ccgp_archive":
            # Archive pages have no server-side search. Scan the requested date
            # window and filter titles/buyers locally; make a depth cap explicit.
            source["max_pages_per_category"] = min(
                int(source.get("max_pages_per_category", 500)),
                int(source.get("max_query_pages_per_category", 20)))
        elif source_type == "custom_web" and source.get("adapter") == "okcis":
            source["_query_criteria"] = criteria
        else:
            source["_query_unsupported"] = (
                "该来源尚未配置按条件查询的接口，请在来源设置中配置后使用。"
            )
        if region["province_code"][:2] in {"71", "81", "82"}:
            source["_query_unsupported"] = "此来源暂未接入该地区的条件查询，请补充对应的官方来源。"
    return AppConfig(config.path, data)


def query_state_path(config: AppConfig) -> Path:
    return config.output_dir / "last_query.json"
