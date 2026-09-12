from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .models import Notice
from .regions import infer_notice_city


_SPACE_RE = re.compile(r"[\t\r\f\v ]+")
_AMOUNT_NUMBER = r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(万元|万|元)?"

_AWARD_VENDOR_TABLE_HEADERS = {
    "供应商名称",
    "中标供应商",
    "成交供应商",
    "中标成交供应商",
    "中标成交供应商名称",
}
_AWARD_AMOUNT_TABLE_HEADERS = {
    "中标金额",
    "成交金额",
    "中标成交金额",
}


@dataclass(frozen=True, slots=True)
class ExtractedFields:
    event_type: str = ""
    project_code: str = ""
    package_no: str = ""
    intention_amount_minor: int | None = None
    budget_amount_minor: int | None = None
    max_price_minor: int | None = None
    award_amount_minor: int | None = None
    contract_amount_minor: int | None = None
    winning_vendor: str = ""
    city: str = ""
    buyer_contact: str = ""
    buyer_phone: str = ""
    winning_vendor_contact: str = ""
    winning_vendor_phone: str = ""
    project_summary: str = ""
    published_at: str = ""
    bid_deadline: str = ""
    opening_at: str = ""
    award_at: str = ""
    expected_purchase_date: str = ""
    evidence: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _clean_text(text: str) -> str:
    lines = []
    for raw_line in text.replace("\u3000", " ").splitlines():
        line = _SPACE_RE.sub(" ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _amount_minor(number: str, unit: str | None) -> int | None:
    try:
        value = Decimal(number.replace(",", ""))
    except InvalidOperation:
        return None
    if not value.is_finite() or value < 0 or value > Decimal(10**16):
        return None
    multiplier = Decimal("1000000") if unit in {"万元", "万"} else Decimal("100")
    try:
        minor = (value * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        result = int(minor)
    except (InvalidOperation, OverflowError, ValueError):
        return None
    return result if result <= 10**18 else None


def _first_match(text: str, labels: tuple[str, ...]) -> tuple[int | None, str]:
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?:{label_pattern})\s*"
        rf"(?P<label_meta>（[^）]{{0,20}}）|\([^)]{{0,20}}\))?\s*[:：]?\s*"
        rf"(?P<number>[0-9][0-9,]*(?:\.[0-9]+)?)\s*"
        rf"(?P<value_unit>万元|万|元)?",
        text,
        flags=re.I,
    )
    if not match:
        return None, ""
    label_unit_match = re.search(
        r"万元|万|元", match.group("label_meta") or ""
    )
    unit = match.group("value_unit") or (
        label_unit_match.group(0) if label_unit_match else None
    )
    return _amount_minor(match.group("number"), unit), match.group(0)[:200]


def _first_value(text: str, labels: tuple[str, ...], *, limit: int = 160) -> tuple[str, str]:
    # Official templates use spaced labels such as "名 称" and "地 址".
    label_pattern = "|".join(r"\s*".join(map(re.escape, label)) for label in sorted(labels, key=len, reverse=True))
    match = re.search(
        rf"(?:{label_pattern})\s*[:：]?\s*([^\n]{{1,{limit}}})",
        text,
        flags=re.I,
    )
    if not match:
        return "", ""
    value = re.split(
        r"\s{2,}|地址\s*[:：]|联系人\s*[:：]|统一社会信用代码\s*[:：]",
        match.group(1),
    )[0].strip(" ：:，,;；")
    return value[:limit], match.group(0)[:240]


def clean_project_code(value: str) -> str:
    value = value.strip(" ：:，,;；")
    if value in {"", "-", "--", "—", "暂无", "无", "未公开", "未填写"}:
        return ""
    if re.match(r"^(?:更新时间|发布时间|发布日期|项目名称|所属地区|所属行业|采购单位|招标人|采购人|招标编号|项目编号|登录后查看|请登录)(?:\s|[:：]|$)", value):
        return ""
    return value


def _project_code(text: str) -> tuple[str, str]:
    labels = ("采购项目编号", "项目编号", "招标编号", "采购编号")
    pattern = "|".join(r"\s*".join(map(re.escape, label)) for label in labels)
    # A blank table cell can be followed by another field label. Skip it and
    # continue looking for a real code elsewhere in the document.
    for match in re.finditer(rf"(?:{pattern})\s*[:：]?\s*([^\n]{{1,100}})", text):
        value = clean_project_code(match.group(1))
        if value:
            return value, match.group(0)[:240]
    return "", ""


def _first_date(text: str, labels: tuple[str, ...]) -> tuple[str, str]:
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?:{label_pattern})\s*[:：]?\s*"
        r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})日?"
        r"(?:\s*(\d{1,2})\s*[:时]\s*(\d{1,2})(?:\s*分)?)?",
        text,
        flags=re.I,
    )
    if not match:
        return "", ""
    year, month, day = (int(match.group(i)) for i in range(1, 4))
    try:
        parsed = datetime(
            year,
            month,
            day,
            int(match.group(4) or 0),
            int(match.group(5) or 0),
        )
    except ValueError:
        return "", ""
    base = parsed.strftime("%Y-%m-%d")
    if match.group(4):
        base += parsed.strftime(" %H:%M")
    return base, match.group(0)[:200]


def _metadata_value(notice: Notice, *keys: str, limit: int = 200) -> str:
    """Return an explicitly labelled source value without inventing data."""

    for key in keys:
        value = notice.metadata.get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            cleaned = _SPACE_RE.sub(" ", str(value)).strip()
            if cleaned:
                return cleaned[:limit]
    return ""


def _city(notice: Notice) -> str:
    return infer_notice_city(notice, str(notice.metadata.get("buyer_address") or ""))


def enrich_purchaser(notice: Notice, text: str) -> None:
    """Use only explicitly labelled purchaser fields, never the agency address."""
    cleaned = _clean_text(text)
    section = _buyer_section(cleaned)
    if not notice.buyer:
        buyer, _ = _first_value(cleaned, (
            "采购人名称", "采购单位名称", "招标人名称",
            "采购人（甲方）", "采购人(甲方)",
        ), limit=150)
        if not buyer:
            labelled = re.search(r"(?:^|\n)(?:招\s*标\s*人|采\s*购\s*人|采\s*购\s*单\s*位)\s*[:：]\s*([^\n]{2,150})", cleaned)
            buyer = labelled.group(1).strip() if labelled else ""
        if not buyer and section:
            buyer, _ = _first_value(section, ("名称",), limit=150)
        if buyer and not re.search(r"[:：]|项目编号|联系电话", buyer):
            notice.buyer = buyer
    if section:
        address, _ = _first_value(section, ("地址",), limit=200)
        if address:
            notice.metadata["buyer_address"] = address


def _buyer_section(text: str) -> str:
    """Limit buyer contacts to an explicitly headed purchaser section."""

    match = re.search(
        r"(?:^|\n)\s*(?:\d+[.\u3001]\s*|[\u4e00-十]+[\u3001]\s*)?"
        r"(?:采购人|采购单位)信息\s*[:：]?\s*(?P<body>.*?)"
        r"(?=(?:\n\s*(?:\d+[.\u3001]|[\u4e00-十]+[\u3001])\s*)?"
        r"(?:采购代理机构|代理机构|项目联系人)信息|\Z)",
        text,
        flags=re.S,
    )
    return match.group("body")[:2500] if match else ""


def _first_phone(text: str) -> tuple[str, str]:
    match = re.search(
        r"(?:联系方式|联系电话|电话|手机)\s*[:：]?\s*"
        r"(?P<phone>(?:\+?86[- ]?)?(?:0\d{2,3}[- ]?)?"
        r"(?:1[3-9]\d{9}|\d{7,8})(?:[-转 ]\d{1,6})?)(?!\d)",
        text,
        flags=re.I,
    )
    if not match:
        return "", ""
    return match.group("phone").strip(), match.group(0)[:160]


def _search_key(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", value).casefold()


def _amount_from_table_cell(value: str) -> int | None:
    match = re.search(_AMOUNT_NUMBER, value)
    if not match:
        return None
    unit = match.group(2)
    if unit is None:
        parenthesized_unit = re.search(
            r"[（(]\s*(万元|万|元)\s*[）)]", value[match.end():]
        )
        if parenthesized_unit:
            unit = parenthesized_unit.group(1)
    return _amount_minor(match.group(1), unit)


def _award_table_values(
    text: str,
) -> tuple[str, str, int | None, str]:
    """Extract the first result row from a flattened procurement award table.

    Official procurement templates commonly put labels in ``thead`` cells and
    values in the following ``tbody`` row.  HTML-to-text conversion therefore
    yields one line per cell, rather than ``label: value`` pairs.  We only use
    this path when a compact header block contains sequence, supplier and award
    amount columns, so unrelated mentions of a supplier or a price do not
    become structured results.
    """

    lines = text.splitlines()
    keys = [_search_key(line) for line in lines]
    for header_start, key in enumerate(keys):
        if key != "序号":
            continue

        header_stop = min(len(lines), header_start + 12)
        vendor_index = next(
            (
                index
                for index in range(header_start + 1, header_stop)
                if keys[index] in _AWARD_VENDOR_TABLE_HEADERS
            ),
            None,
        )
        amount_index = next(
            (
                index
                for index in range(header_start + 1, header_stop)
                if keys[index] in _AWARD_AMOUNT_TABLE_HEADERS
            ),
            None,
        )
        if vendor_index is None or amount_index is None:
            continue

        last_header_index = max(vendor_index, amount_index)
        row_start = next(
            (
                index
                for index in range(last_header_index + 1, header_stop + 8)
                if index < len(lines) and re.fullmatch(r"\d{1,4}", lines[index])
            ),
            None,
        )
        if row_start is None:
            continue

        vendor_value_index = row_start + vendor_index - header_start
        amount_value_index = row_start + amount_index - header_start
        if max(vendor_value_index, amount_value_index) >= len(lines):
            continue

        vendor = lines[vendor_value_index].strip(" ：:，,;；")
        amount_cell = lines[amount_value_index].strip()
        if (
            len(vendor) < 2
            or len(vendor) > 160
            or _search_key(vendor) in _AWARD_VENDOR_TABLE_HEADERS
            or re.fullmatch(r"[0-9.,]+", vendor)
        ):
            continue
        amount = _amount_from_table_cell(amount_cell)
        if amount is None:
            continue

        vendor_evidence = (
            f"{lines[vendor_index]}：{vendor}（结果表第{lines[row_start]}行）"
        )
        amount_evidence = (
            f"{lines[amount_index]}：{amount_cell}（结果表第{lines[row_start]}行）"
        )
        return vendor, vendor_evidence[:240], amount, amount_evidence[:240]
    return "", "", None, ""


def scope_text_for_notice(notice: Notice, text: str, *, radius: int = 24) -> str:
    """在整批/列表型官方公告中定位当前项目，避免串取其他项目金额。

    无法可靠定位时返回完整正文；该函数只裁剪用于字段提取和 AI 分析的
    派生文本，不修改或替换保存的官方响应字节。
    """

    cleaned = _clean_text(text)
    lines = cleaned.splitlines()
    title_parts = [notice.title]
    title_parts.extend(re.split(r"[-—–丨|]", notice.title))
    candidates: list[str] = []
    for part in title_parts:
        part = re.sub(
            r"(?:政府采购意向|采购意向|招标公告|采购公告|中标公告|成交公告)$",
            "",
            part.strip(),
        )
        key = _search_key(part)
        if len(key) >= 6 and key not in candidates:
            candidates.append(key)
    candidates.sort(key=len, reverse=True)

    normalized_lines = [_search_key(line) for line in lines]
    for key in candidates:
        matches = [
            index
            for index, line in enumerate(normalized_lines)
            if key in line or (len(line) >= 6 and line in key)
        ]
        if len(matches) != 1:
            continue
        index = matches[0]
        row_start = None
        for candidate_index in range(index - 1, max(-1, index - 8), -1):
            if re.fullmatch(r"\d{1,4}", lines[candidate_index]):
                row_start = candidate_index
                break
        if row_start is not None:
            for candidate_index in range(index + 1, min(len(lines), index + 12)):
                if re.fullmatch(r"\d{1,4}", lines[candidate_index]):
                    return "\n".join(lines[row_start:candidate_index])
        start = max(0, index - 4)
        end = min(len(lines), index + radius)
        return "\n".join(lines[start:end])
    return cleaned


def _event_type(notice: Notice, text: str) -> str:
    sample = f"{notice.notice_type}\n{notice.title}\n{text[:2500]}"
    rules = (
        ("合同公告", ("合同公告", "合同公示")),
        ("中标公告", ("中标公告", "中标结果", "中标信息")),
        ("成交公告", ("成交公告", "成交结果", "成交信息")),
        ("终止公告", ("终止公告", "废标公告", "流标公告")),
        ("更正公告", ("更正公告", "变更公告")),
        ("采购意向", ("采购意向", "预计采购时间")),
        ("采购公告", ("采购公告", "竞争性磋商", "竞争性谈判", "询价公告")),
        ("招标公告", ("招标公告", "公开招标")),
    )
    for name, markers in rules:
        if any(marker in sample for marker in markers):
            return name
    return notice.notice_type or "其他公告"


def extract_structured_fields(notice: Notice, text: str) -> ExtractedFields:
    cleaned = _clean_text(text)
    enrich_purchaser(notice, cleaned)
    evidence: dict[str, str] = {}

    project_code, project_evidence = _project_code(cleaned)
    if project_evidence:
        evidence["project_code"] = project_evidence

    package_no, package_evidence = _first_value(
        cleaned,
        ("包号", "标包编号", "标项编号"),
        limit=60,
    )
    if package_evidence:
        evidence["package_no"] = package_evidence

    amounts: dict[str, int | None] = {}
    amount_rules = {
        "intention_amount_minor": ("意向金额",),
        "budget_amount_minor": (
            "预算金额", "采购预算", "项目预算", "招标金额", "预算价",
        ),
        "max_price_minor": ("最高限价", "最高投标限价"),
        "award_amount_minor": ("中标金额", "成交金额", "中标（成交）金额"),
        "contract_amount_minor": ("合同金额", "合同总金额"),
    }
    for field_name, labels in amount_rules.items():
        value, quote = _first_match(cleaned, labels)
        amounts[field_name] = value
        if quote:
            evidence[field_name] = quote

    (
        table_vendor,
        table_vendor_evidence,
        table_award_amount,
        table_amount_evidence,
    ) = _award_table_values(cleaned)
    if table_award_amount is not None:
        amounts["award_amount_minor"] = table_award_amount
        evidence["award_amount_minor"] = table_amount_evidence

    # 中国政府采购网“整批采购意向”表格在解析成纯文本后，表头与每一行分离。
    # 当前项目作用域内稳定顺序为：序号、采购单位、项目名、品目、概况、
    # 预算金额(万元)、预计采购月份、备注。这里仅在采购意向且金额尚未由
    # 明示标签提取时采用该结构，避免把相邻项目金额串进来。
    if (
        amounts["budget_amount_minor"] is None
        and _event_type(notice, cleaned) == "采购意向"
    ):
        lines = cleaned.splitlines()
        month_index = next(
            (
                index
                for index, line in enumerate(lines)
                if re.fullmatch(r"20\d{2}\s*年\s*\d{1,2}\s*月", line)
            ),
            None,
        )
        if month_index is not None:
            for amount_index in range(month_index - 1, max(-1, month_index - 4), -1):
                match = re.fullmatch(r"([0-9][0-9,]*(?:\.[0-9]+)?)", lines[amount_index])
                if not match:
                    continue
                value = _amount_minor(match.group(1), "万元")
                if value is not None:
                    amounts["budget_amount_minor"] = value
                    evidence["budget_amount_minor"] = (
                        f"采购意向表预算金额(万元): {lines[amount_index]}"
                    )
                break

    winning_vendor, vendor_evidence = _first_value(
        cleaned,
        (
            "中标（成交）供应商（乙方）",
            "中标（成交）供应商(乙方)",
            "中标人名称",
            "中标（成交）供应商",
            "中标供应商",
            "成交供应商",
            "供应商名称",
            "中标人",
        ),
        limit=160,
    )
    if vendor_evidence:
        evidence["winning_vendor"] = vendor_evidence
    if table_vendor:
        winning_vendor = table_vendor
        evidence["winning_vendor"] = table_vendor_evidence

    buyer_contact = _metadata_value(
        notice,
        "buyer_contact", "purchaser_contact", "customer_contact", "contact",
    )
    buyer_phone = _metadata_value(
        notice,
        "buyer_phone", "purchaser_phone", "customer_phone", "contact_phone",
        "phone",
    )
    buyer_section = _buyer_section(cleaned)
    if not buyer_contact:
        buyer_contact, quote = _first_value(
            cleaned,
            ("采购人联系人", "采购单位联系人", "客户联系人"),
            limit=60,
        )
        if quote:
            evidence["buyer_contact"] = quote
    if not buyer_phone:
        explicit_phone = re.search(
            r"(?:采购人|采购单位|客户)(?:联系电话|电话|联系方式)\s*[:：]?\s*"
            r"(?P<phone>(?:\+?86[- ]?)?(?:0\d{2,3}[- ]?)?"
            r"(?:\d{7,8}|1[3-9]\d{9})(?:[-转 ]\d{1,6})?)",
            cleaned,
            flags=re.I,
        )
        if explicit_phone:
            buyer_phone = explicit_phone.group("phone").strip()
            evidence["buyer_phone"] = explicit_phone.group(0)[:160]
    if buyer_section:
        if not buyer_contact:
            buyer_contact, quote = _first_value(
                buyer_section, ("联系人", "项目联系人"), limit=60
            )
            if quote:
                evidence["buyer_contact"] = quote
        if not buyer_phone:
            buyer_phone, quote = _first_phone(buyer_section)
            if quote:
                evidence["buyer_phone"] = quote

    winning_vendor_contact = _metadata_value(
        notice,
        "winning_vendor_contact", "supplier_contact", "vendor_contact",
    )
    winning_vendor_phone = _metadata_value(
        notice,
        "winning_vendor_phone", "supplier_phone", "vendor_phone",
    )
    project_summary = _metadata_value(
        notice,
        "project_summary", "summary", "description", "project_info",
        limit=500,
    )
    if not project_summary:
        project_summary, quote = _first_value(
            cleaned,
            ("项目概况", "采购需求概况", "采购需求", "采购内容"),
            limit=500,
        )
        if quote:
            evidence["project_summary"] = quote

    date_values: dict[str, str] = {}
    date_rules = {
        "bid_deadline": ("提交投标文件截止时间", "投标截止时间", "响应文件提交截止时间"),
        "opening_at": ("开标时间", "开启时间"),
        "award_at": ("中标日期", "成交日期"),
    }
    for field_name, labels in date_rules.items():
        value, quote = _first_date(cleaned, labels)
        date_values[field_name] = value
        if quote:
            evidence[field_name] = quote

    expected_match = re.search(
        r"预计采购时间\s*[:：]?\s*(20\d{2})\s*[-/.年]\s*(\d{1,2})月?",
        cleaned,
    )
    expected = ""
    if expected_match:
        expected_year = int(expected_match.group(1))
        expected_month = int(expected_match.group(2))
        try:
            datetime(expected_year, expected_month, 1)
        except ValueError:
            pass
        else:
            expected = f"{expected_year:04d}-{expected_month:02d}"
            evidence["expected_purchase_date"] = expected_match.group(0)[:160]
    elif _event_type(notice, cleaned) == "采购意向":
        standalone_month = re.search(r"(?m)^\s*(20\d{2})\s*年\s*(\d{1,2})\s*月\s*$", cleaned)
        if standalone_month:
            expected_year = int(standalone_month.group(1))
            expected_month = int(standalone_month.group(2))
            try:
                datetime(expected_year, expected_month, 1)
            except ValueError:
                pass
            else:
                expected = f"{expected_year:04d}-{expected_month:02d}"
                evidence["expected_purchase_date"] = standalone_month.group(0).strip()

    return ExtractedFields(
        event_type=_event_type(notice, cleaned),
        project_code=project_code,
        package_no=package_no,
        intention_amount_minor=amounts["intention_amount_minor"],
        budget_amount_minor=amounts["budget_amount_minor"],
        max_price_minor=amounts["max_price_minor"],
        award_amount_minor=amounts["award_amount_minor"],
        contract_amount_minor=amounts["contract_amount_minor"],
        winning_vendor=winning_vendor,
        city=_city(notice),
        buyer_contact=buyer_contact,
        buyer_phone=buyer_phone,
        winning_vendor_contact=winning_vendor_contact,
        winning_vendor_phone=winning_vendor_phone,
        project_summary=project_summary,
        published_at=notice.published_at,
        bid_deadline=date_values["bid_deadline"],
        opening_at=date_values["opening_at"],
        award_at=date_values["award_at"],
        expected_purchase_date=expected,
        evidence=evidence,
    )
