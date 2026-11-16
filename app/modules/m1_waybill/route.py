"""联运路线解析：把一票拆成按顺序编号的运输段。

写法 ``承运商/服务/分区>承运商/服务/分区``，分隔符也接受 ``->`` 与 ``→``。
路线列留空表示整票只有一段，承运商与服务取自运单列，段号固定为 1。

本文件只做解析与结构校验，不查运价；查不到的段在采信与计价阶段分别给出
带段号的原因码，保证复核时能直接指出卡在第几段。
"""

from __future__ import annotations

import unicodedata

from app import config

LEG_SEPARATOR = ">"


def _clean(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").strip()


def _split(text: str) -> list[str]:
    body = _clean(text)
    for token in config.ROUTE_SEPARATORS:
        body = body.replace(token, LEG_SEPARATOR)
    return body.split(LEG_SEPARATOR)


def leg(carrier: str, service: str, zone: str, leg_no: int) -> dict:
    return {
        "leg_no": leg_no,
        "carrier": _clean(carrier).upper(),
        "service": _clean(service).lower(),
        "zone": _clean(zone).upper(),
    }


def route_text(legs: list[dict]) -> str:
    return LEG_SEPARATOR.join(f"{l['carrier']}/{l['service']}/{l['zone']}" for l in legs)


def parse(text: str, carrier: str, service: str, zone: str) -> tuple[list[dict], list[dict]]:
    """返回 (分段列表, 结构问题)。问题项带 ``leg_no``，未填路线时为单段。"""
    raw = _clean(text)
    fallback = leg(carrier, service, zone, 1)
    if not raw:
        return [fallback], []

    legs: list[dict] = []
    problems: list[dict] = []
    for index, segment in enumerate(_split(raw), start=1):
        if not segment.strip():
            problems.append({
                "leg_no": index,
                "code": "ROUTE_GAP",
                "detail": "路线缺少第 {0} 段".format(index),
            })
            continue
        parts = [_clean(part) for part in segment.split("/")]
        if len(parts) < 3:
            problems.append({
                "leg_no": index,
                "code": "ROUTE_GAP",
                "detail": "第 {0} 段缺少「承运商/服务/分区」中的字段：{1!r}".format(index, segment),
            })
            continue
        if len(parts) > 3 or not all(parts):
            problems.append({
                "leg_no": index,
                "code": "ROUTE_INVALID",
                "detail": "第 {0} 段应写成 承运商/服务/分区，实际为 {1!r}".format(index, segment),
            })
            continue
        current = leg(parts[0], parts[1], parts[2], index)
        if legs and (current["carrier"], current["service"], current["zone"]) == (
            legs[-1]["carrier"], legs[-1]["service"], legs[-1]["zone"]
        ):
            problems.append({
                "leg_no": index,
                "code": "ROUTE_DUP_LEG",
                "detail": "第 {0} 段与前一段完全相同".format(index),
            })
            continue
        legs.append(current)

    if not problems and len(legs) < 2:
        problems.append({
            "leg_no": 2,
            "code": "ROUTE_GAP",
            "detail": "联运路线至少两段，当前只解析出 1 段，缺第 2 段",
        })
    return legs, problems


def check_legs(legs: list[dict], keys: set[tuple[str, str, str]],
               known_carriers: set[str]) -> list[dict]:
    """逐段校验承运商与运价键；返回带断点段号的阻断项。"""
    issues: list[dict] = []
    for item in legs:
        carrier = item.get("carrier") or ""
        service = item.get("service") or ""
        zone = item.get("zone") or ""
        if not carrier or not service or not zone:
            continue
        if carrier not in known_carriers:
            issues.append({
                "leg_no": item["leg_no"],
                "code": "UNKNOWN_CARRIER",
                "detail": "第 {0} 段承运商 {1} 不在运价表".format(item["leg_no"], carrier),
            })
        elif (carrier, service, zone) not in keys:
            issues.append({
                "leg_no": item["leg_no"],
                "code": "RATE_MISSING",
                "detail": "第 {0} 段 {1}/{2}/{3} 没有运价".format(
                    item["leg_no"], carrier, service, zone
                ),
            })
    return issues


def is_interline(legs: list[dict]) -> bool:
    return len(legs) >= 2
