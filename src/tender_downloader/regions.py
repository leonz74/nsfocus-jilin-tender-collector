"""Normalize Jilin cities from explicit purchaser/project location evidence.

County aliases: https://www.jl.gov.cn/shengqing/xzqh/ (checked 2026-09-11).
Meihekou and Changbaishan remain separate operational filters used by the app.
"""
import re

CITY_DISTRICTS = {
    "长春": "南关区 宽城区 朝阳区 二道区 绿园区 双阳区 九台区 榆树市 德惠市 公主岭市 农安县",
    "吉林市": "船营区 昌邑区 龙潭区 丰满区 蛟河市 桦甸市 舒兰市 磐石市 永吉县",
    "四平": "铁西区 铁东区 双辽市 梨树县 伊通满族自治县",
    "辽源": "龙山区 西安区 东丰县 东辽县",
    "通化": "东昌区 二道江区 集安市 通化县 辉南县 柳河县",
    "白山": "浑江区 江源区 临江市 抚松县 靖宇县 长白朝鲜族自治县",
    "松原": "宁江区 扶余市 前郭尔罗斯蒙古族自治县 长岭县 乾安县",
    "白城": "洮北区 洮南市 大安市 镇赉县 通榆县",
    "延边": "延吉市 图们市 敦化市 珲春市 龙井市 和龙市 汪清县 安图县",
    "梅河口": "梅河口市",
    "长白山": "长白山管委会 长白山保护开发区 池北区 池西区 池南区",
}


def city_in_text(text: str) -> str:
    if text.strip() in CITY_DISTRICTS:
        return text.strip()
    # Require city/state names for ordinary cities, so a procuring agency with
    # "吉林省" or "白山" in its company name does not create a false location.
    exact = {city for city in CITY_DISTRICTS if
             (city if city == "吉林市" else city + "市") in text
             or (city == "延边" and "延边" in text)
             or (city == "长白山" and any(x in text for x in CITY_DISTRICTS[city].split()))}
    if len(exact) == 1:
        return exact.pop()
    if len(exact) > 1:
        return ""
    matches = {city for city, names in CITY_DISTRICTS.items() if any(n in text for n in names.split())}
    return matches.pop() if len(matches) == 1 else ""


def infer_notice_city(notice, purchaser_address: str = "") -> str:
    from .geography import infer_location
    location = infer_location(notice.region or "", str(notice.metadata.get("city") or ""),
                              purchaser_address or notice.buyer, notice.title)
    if location["province"] and location["province"] != "吉林省":
        return location["city"] or location["province"]
    for key in ("city", "area", "district"):
        value = str(notice.metadata.get(key) or "").strip()
        if value in CITY_DISTRICTS:
            return value
        found = city_in_text(value)
        if found:
            return found
    project_title = re.split(r"关于", notice.title, maxsplit=1)[-1]
    for text in (purchaser_address, notice.buyer, project_title, notice.region):
        found = city_in_text(text or "")
        if found:
            return found
    return "吉林省" if "吉林" in (notice.region or "") else ""
