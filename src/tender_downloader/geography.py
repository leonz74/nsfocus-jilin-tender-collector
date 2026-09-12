"""Shared, offline province/city/district catalogue and conservative location matching."""
from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def region_catalog() -> list[dict]:
    provinces = json.loads((Path(__file__).with_name("data") / "china-regions.json").read_text())
    for province in provinces:
        if province["code"] in {"11", "12", "31", "50"}:
            province["municipality"] = True
            province["children"] = [{"name": province["name"], "code": province["code"],
                                     "children": province["children"]}]
        if province["code"] == "22":
            province["children"].extend([
                {"name": "梅河口市", "code": "220581", "children": [], "operational": True},
                {"name": "长白山", "code": "", "children": [], "operational": True},
            ])
    provinces.extend({"code": code, "name": name, "children": [], "subdivisions_unavailable": True}
                     for code, name in [("71", "台湾省"), ("81", "香港特别行政区"), ("82", "澳门特别行政区")])
    return provinces


def province_alias(name: str) -> str:
    for suffix in ("壮族自治区", "回族自治区", "维吾尔自治区", "特别行政区", "自治区", "省", "市"):
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


def canonical_city(value: str, province: dict) -> str:
    if not value or value == province["name"]:
        return value if province.get("municipality") and value else ""
    aliases = {"延边": "延边朝鲜族自治州", "长白山管委会": "长白山"}
    value = aliases.get(value, value)
    for city in province["children"]:
        if value in {city["name"], city["name"].removesuffix("市")}:
            return city["name"]
    return value


def query_region(criteria: dict) -> dict:
    # Old saved queries without a province were constrained to Jilin.
    name = criteria.get("province", "吉林省")
    city = criteria.get("city", "")
    district = criteria.get("district", "")
    if not name:
        if city or district:
            raise ValueError("请先选择省份，再选择城市或区县")
        return {"province": "", "city": "", "district": "", "province_code": "", "city_code": ""}
    province = next((p for p in region_catalog() if name == p["name"]), None)
    if province is None:
        raise ValueError("请选择有效省份")
    city = canonical_city(city, province)
    selected = next((c for c in province["children"] if city == c["name"]), None)
    if city and selected is None:
        raise ValueError("城市不属于所选省份，请重新选择")
    if district and (selected is None or district not in [d["name"] for d in selected.get("children", [])]):
        raise ValueError("区县不属于所选城市，请重新选择")
    return {"province": name, "city": city, "district": district,
            "province_code": province["code"].ljust(6, "0"),
            "city_code": selected["code"].ljust(6, "0") if selected and selected["code"] else ""}


def infer_location(region: str = "", city: str = "", purchaser: str = "", title: str = "",
                   district: str = "") -> dict:
    """Use explicit locations and purchaser/title evidence, never supplier addresses."""
    provinces = region_catalog()
    province = None
    for text in (region, city, purchaser, title):
        matches = [p for p in provinces if p["name"] in text or (
            province_alias(p["name"]) in text.split("-"))]
        if len(matches) == 1:
            province = matches[0]
            break
    pool = [province] if province else provinces
    selected = None
    for text in (city, region, purchaser, title):
        matches = [(p, c) for p in pool for c in p["children"]
                   if c["name"] in text or text.strip() == c["name"].removesuffix("市")
                   or (text in (city, region) and text.split("-")[-1] == c["name"].removesuffix("市"))
                   or (c["name"] == "延边朝鲜族自治州" and "延边" in text)]
        if len(matches) == 1:
            province, selected = matches[0]
            break
    district_name = ""
    county_pool = [(p, c, d) for p in ([province] if province else provinces)
                   for c in ([selected] if selected else p["children"])
                   for d in c.get("children", [])]
    for text in (district, city, region, purchaser, title):
        matches = [(p, c, d) for p, c, d in county_pool if d["name"] in text]
        if len(matches) == 1:
            province, selected, county = matches[0]
            district_name = county["name"]
            break
    return {"province": province["name"] if province else "",
            "city": selected["name"] if selected else "", "district": district_name}


def source_region(config: dict) -> dict:
    return config.get("_query_region") or query_region({})


def region_payload() -> dict:
    return {"provinces": copy.deepcopy(region_catalog()), "data_version": "2025-12-31",
            "note": "港澳台暂提供省级选择；区县识别取决于公告中的地域信息。"}
