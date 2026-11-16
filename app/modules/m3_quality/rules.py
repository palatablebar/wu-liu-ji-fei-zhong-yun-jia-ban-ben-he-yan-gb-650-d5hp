"""交叉行断言：承运商与分区引用、重复运单取值冲突、时区写法混用。

采信模块只做单行解析，跨行与跨表的判定集中在这里，保证原因码只有一处产出。
"""

from __future__ import annotations

from app.modules.m1_waybill import route

PAYLOAD_FIELDS = (
    "carrier", "service", "zone", "pickup_utc",
    "weight_kg", "length_cm", "width_cm", "height_cm", "declared_value",
    "route",
)


def payload(draft: dict) -> tuple:
    return tuple(str(draft.get(field)) for field in PAYLOAD_FIELDS)


def check_reference(draft: dict, keys: set[tuple[str, str, str]],
                    known_carriers: set[str]) -> list[str]:
    """承运商与分区必须能在运价表里找到，否则计费无依据。"""
    carrier = draft.get("carrier") or ""
    service = draft.get("service") or ""
    zone = draft.get("zone") or ""
    if not carrier or not service or not zone:
        return []
    if carrier not in known_carriers:
        return ["UNKNOWN_CARRIER"]
    if (carrier, service, zone) not in keys:
        return ["RATE_MISSING"]
    return []


def check_route(legs: list[dict], keys: set[tuple[str, str, str]],
                known_carriers: set[str]) -> list[dict]:
    """逐段查运价；返回带段号的阻断项，让复核一眼看到卡在第几段。

    单段运单沿用整票口径，与 ``check_reference`` 等价；联运逐段独立判定，
    任何一段缺承运商或运价都整票隔离，不会只算一半。
    """
    if not legs:
        return []
    return route.check_legs(legs, keys, known_carriers)


def dedupe(drafts: list[dict]) -> int:
    """就地追加重复判定；返回被判重复的行数。

    先出现的保留，后出现的按载荷是否一致分成 DUP_CONFLICT 与 DUPLICATE_WAYBILL。
    已被其它原因阻断的行不参与登记，避免一条坏行污染后续好行。
    """
    registry: dict[str, tuple] = {}
    hits = 0
    for draft in drafts:
        if draft["blocks"]:
            continue
        current = payload(draft)
        previous = registry.get(draft["waybill_no"])
        if previous is None:
            registry[draft["waybill_no"]] = current
            continue
        draft["blocks"].append("DUPLICATE_WAYBILL" if previous == current else "DUP_CONFLICT")
        hits += 1
    return hits


def apply_tz_styles(drafts: list[dict], styles: set[str]) -> int:
    """同一列里既有 Z 又有 +hh:mm 时统一标记，不改变已归一的 UTC 值。"""
    if not {"z", "offset"} <= styles:
        return 0
    marked = 0
    for draft in drafts:
        if draft.get("pickup") is not None:
            draft["codes"].append("TZ_MIXED")
            marked += 1
    return marked
