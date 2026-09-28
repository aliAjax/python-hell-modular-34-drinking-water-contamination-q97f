from datetime import datetime, timezone

from .domain import DomainError, parse_timestamp

ENTITY_TYPE = "water_contamination"
INITIAL_STATUS = "detected"
CREATE_ROLES = {"analyst", "dispatcher"}
SOURCE_ROLES = {"analyst", "dispatcher", "field_operator", "lab"}
ACTION_ROLES = {
    "verify": {"analyst", "dispatcher"},
    "advise": {"coordinator", "dispatcher"},
    "switch_source": {"coordinator"},
    "flush": {"field_operator"},
    "disinfect": {"field_operator"},
    "sample": {"lab", "field_operator"},
    "restore": {"coordinator", "regulator"},
    "cancel": {"coordinator"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {"advise", "switch_source", "flush", "disinfect", "sample", "restore", "cancel"}

# 区域复检状态：待冲洗 ->（冲洗）-> 待消毒 ->（消毒批次覆盖）-> 已消毒
#   ->（同批次消毒后合格样本）-> 已采样 ->（统一恢复）-> 已恢复
STAGE_PENDING_FLUSH = "pending_flush"
STAGE_FLUSHED = "flushed"
STAGE_DISINFECTED = "disinfected"
STAGE_SAMPLED = "sampled"
STAGE_RESTORED = "restored"

STAGE_LABELS = {
    STAGE_PENDING_FLUSH: "待冲洗",
    STAGE_FLUSHED: "待消毒",
    STAGE_DISINFECTED: "已消毒",
    STAGE_SAMPLED: "已采样",
    STAGE_RESTORED: "已恢复",
}
MISSING_LABELS = {
    "flush": "待冲洗",
    "disinfect": "待消毒批次覆盖",
    "sample": "待消毒后合格样本",
    "restore": "待整体恢复供水",
}


def assess(payload):
    concentration = float(payload.get("concentration", 0))
    limit = max(float(payload.get("limit", 0.000001)), 0.000001)
    ratio = concentration / limit
    population = int(payload.get("population", 0))
    score = min(100.0, ratio * 35.0 + min(population / 1000.0, 40.0))
    if score >= 80:
        level = "critical"
    elif score >= 50:
        level = "high"
    elif score >= 20:
        level = "medium"
    else:
        level = "low"
    return {"score": round(score, 2), "level": level, "ratio": round(ratio, 3)}


def initial_zones(zone_ids):
    return {
        zone_id: {"status": STAGE_PENDING_FLUSH, "basis": 0, "flushed": False}
        for zone_id in zone_ids
    }


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def _parse_dt(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _zone(current, zone_id):
    zones = current.get("zones") or {}
    if zone_id not in zones:
        raise DomainError("unknown_zone", "区域 %s 不属于本次受影响区域" % zone_id, 409)
    return zones[zone_id]


def _batch_zones(payload, current):
    raw = payload.get("zone_ids")
    if not isinstance(raw, list) or not raw:
        raise DomainError("field_required", "消毒批次必须写明覆盖区域")
    zones = current.get("zones") or {}
    zone_ids = []
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            raise DomainError("invalid_zones", "区域编号必须是字符串列表")
        zone_id = value.strip()
        if zone_id not in zones:
            raise DomainError("unknown_zone", "区域 %s 不属于本次受影响区域" % zone_id, 409)
        if zone_id not in zone_ids:
            zone_ids.append(zone_id)
    return zone_ids


def _find_batch(current, batch_id):
    for batch in current.get("disinfection_batches", []):
        if batch["batch_id"] == batch_id:
            return batch
    raise DomainError("batch_not_found", "消毒批次 %s 不存在" % batch_id, 409)


def current_qualified_sample(current, zone_id):
    """该区域当前依据（最近一次消毒批次）下、消毒后采集且合格的最新样本。"""
    zone = (current.get("zones") or {}).get(zone_id)
    if zone is None:
        return None
    basis = int(zone.get("basis", 0))
    limit = float(current.get("limit", 0) or 0)
    chosen = None
    for result in current.get("sample_results", []):
        if result.get("zone_id") != zone_id:
            continue
        if int(result.get("basis", -1)) != basis or not result.get("qualified"):
            continue
        if float(result["concentration"]) > limit:
            continue
        if chosen is None or _parse_dt(result["sampled_at"]) >= _parse_dt(chosen["sampled_at"]):
            chosen = result
    return chosen


def current_batch(current, zone_id):
    zone = (current.get("zones") or {}).get(zone_id)
    if zone is None:
        return None
    basis = int(zone.get("basis", 0))
    for batch in reversed(current.get("disinfection_batches", [])):
        if zone_id in batch.get("zone_ids", []) and int(batch.get("basis_by_zone", {}).get(zone_id, -1)) == basis:
            return batch
    return None


def zone_progress(current):
    """按区域汇总复检依据，列出每个区域还缺什么。"""
    zone_ids = current.get("zone_ids", [])
    zones = current.get("zones") or {}
    progress = []
    for zone_id in zone_ids:
        zone = zones.get(zone_id, {"status": STAGE_PENDING_FLUSH, "basis": 0, "flushed": False})
        flushed = bool(zone.get("flushed"))
        batch = current_batch(current, zone_id)
        sample = current_qualified_sample(current, zone_id)
        restored = zone.get("status") == STAGE_RESTORED
        missing = []
        if not flushed:
            missing.append("flush")
        if batch is None:
            missing.append("disinfect")
        if sample is None:
            missing.append("sample")
        if not restored:
            missing.append("restore")
        if restored:
            stage = STAGE_RESTORED
        elif sample is not None:
            stage = STAGE_SAMPLED
        elif batch is not None:
            stage = STAGE_DISINFECTED
        elif flushed:
            stage = STAGE_FLUSHED
        else:
            stage = STAGE_PENDING_FLUSH
        progress.append({
            "zone_id": zone_id,
            "stage": stage,
            "stage_label": STAGE_LABELS[stage],
            "basis": int(zone.get("basis", 0)),
            "flushed": flushed,
            "batch_id": batch["batch_id"] if batch else None,
            "qualified_sample": sample is not None,
            "sample_id": sample["sample_id"] if sample else None,
            "concentration": sample["concentration"] if sample else None,
            "restored": restored,
            "missing": missing,
            "missing_labels": [MISSING_LABELS[key] for key in missing],
        })
    return progress


def apply_source_registration(item, source, observed_at):
    """再次登记某区域污染来源时，该区域原有恢复依据作废。"""
    current = dict(item["payload"])
    zone_id = source.get("zone_id")
    invalidation = None
    new_status = item["status"]
    zones = current.get("zones") or {}
    if isinstance(zone_id, str) and zone_id.strip() in zones:
        zone_id = zone_id.strip()
        zone = zones[zone_id]
        if int(zone.get("basis", 0)) > 0:
            zone["basis"] = int(zone["basis"]) + 1
            if zone.get("status") == STAGE_RESTORED:
                zone["status"] = STAGE_SAMPLED
            invalidation = {
                "zone_id": zone_id,
                "reason": "source_reregistered",
                "at": observed_at,
            }
            current.setdefault("basis_invalidations", []).append(invalidation)
            if item["status"] == STAGE_RESTORED:
                new_status = STAGE_SAMPLED
    return new_status, current, invalidation


def apply_action(item, action, payload, actor, role):
    status = item["status"]
    current = dict(item["payload"])

    if action == "verify":
        _need_status(item, {"detected", "verified"})
        sample_count = int(payload.get("sample_count", 0) or 0)
        if sample_count < 1:
            raise DomainError("sample_required", "需要至少一份复检样本", 409)
        current["assessment"] = assess(current)
        current["verification"] = {"sample_count": sample_count, "note": payload.get("note", "")}
        return "verified", current, {"assessment": current["assessment"], "verification": current["verification"]}

    if action == "advise":
        _need_status(item, {"verified", "advisory"})
        notice_id = _text(payload, "notice_id")
        notice = {
            "notice_id": notice_id,
            "kind": _text(payload, "kind"),
            "message": _text(payload, "message"),
        }
        notices = current.setdefault("notifications", [])
        if any(existing.get("notice_id") == notice_id for existing in notices):
            raise DomainError("duplicate_notification", "同一通知编号不能重复发送", 409)
        notices.append(notice)
        return "advisory", current, {"notice": notice}

    if action == "switch_source":
        _need_status(item, {"verified", "advisory", "flushing", "disinfected", "sampled", "switched"})
        alternate = _text(payload, "alternate_source_id")
        current["alternate_source_id"] = alternate
        return "switched", current, {"alternate_source_id": alternate}

    if action == "flush":
        _need_status(item, {"advisory", "flushing", "switched", "disinfected", "sampled", "restored"})
        zone_id = _text(payload, "zone_id")
        zone = _zone(current, zone_id)
        zone["flushed"] = True
        if zone["status"] == STAGE_PENDING_FLUSH:
            zone["status"] = STAGE_FLUSHED
        record = {"type": "flush", "zone_id": zone_id}
        current.setdefault("response_actions", []).append(record)
        if status in ("advisory", "switched", "flushing"):
            status = "flushing"
        return status, current, record

    if action == "disinfect":
        _need_status(item, {"flushing", "disinfected", "sampled", "restored"})
        if not payload.get("completed"):
            raise DomainError("disinfection_incomplete", "消毒尚未完成", 409)
        batch_id = _text(payload, "batch_id")
        completed_at = parse_timestamp(payload, "completed_at")
        zone_ids = _batch_zones(payload, current)
        batches = current.setdefault("disinfection_batches", [])
        if any(batch["batch_id"] == batch_id for batch in batches):
            raise DomainError("duplicate_batch", "同一消毒批次编号不能重复登记", 409)
        for zone_id in zone_ids:
            if not current["zones"][zone_id].get("flushed"):
                raise DomainError("zone_not_flushed", "区域 %s 尚未冲洗，不能纳入消毒批次" % zone_id, 409)
        basis_by_zone = {}
        for zone_id in zone_ids:
            zone = current["zones"][zone_id]
            zone["basis"] = int(zone.get("basis", 0)) + 1
            zone["status"] = STAGE_DISINFECTED
            basis_by_zone[zone_id] = zone["basis"]
        batch = {
            "batch_id": batch_id,
            "zone_ids": zone_ids,
            "completed_at": completed_at,
            "basis_by_zone": basis_by_zone,
        }
        batches.append(batch)
        record = {
            "type": "disinfect",
            "batch_id": batch_id,
            "zone_ids": zone_ids,
            "completed_at": completed_at,
        }
        current.setdefault("response_actions", []).append(record)
        return "disinfected", current, record

    if action == "sample":
        _need_status(item, {"disinfected", "sampled", "restored"})
        sample_id = _text(payload, "sample_id")
        zone_id = _text(payload, "zone_id")
        _zone(current, zone_id)
        batch_id = _text(payload, "batch_id")
        sampled_at = parse_timestamp(payload, "sampled_at")
        concentration = float(payload.get("concentration", 0))
        if concentration < 0:
            raise DomainError("invalid_concentration", "浓度不能为负数")
        batch = _find_batch(current, batch_id)
        if zone_id not in batch["zone_ids"]:
            raise DomainError("batch_zone_mismatch", "样本区域不在消毒批次 %s 的覆盖范围内" % batch_id, 409)
        if _parse_dt(sampled_at) <= _parse_dt(batch["completed_at"]):
            raise DomainError("sample_before_disinfection", "样本必须在消毒批次 %s 完成之后采集" % batch_id, 409)
        limit = float(current.get("limit", 0) or 0)
        qualified = concentration <= limit
        basis = int(batch["basis_by_zone"][zone_id])
        zone = current["zones"][zone_id]
        invalidation = None
        if not qualified and basis == int(zone.get("basis", 0)):
            # 本次消毒后的复检超限：原恢复依据作废，需重新消毒采样
            zone["basis"] = basis + 1
            if zone.get("status") == STAGE_RESTORED:
                zone["status"] = STAGE_SAMPLED
            invalidation = {
                "zone_id": zone_id,
                "reason": "sample_exceeded",
                "batch_id": batch_id,
                "sample_id": sample_id,
                "concentration": concentration,
                "at": sampled_at,
            }
            current.setdefault("basis_invalidations", []).append(invalidation)
        elif qualified and basis == int(zone.get("basis", 0)) and zone.get("status") != STAGE_RESTORED:
            zone["status"] = STAGE_SAMPLED
        result = {
            "sample_id": sample_id,
            "zone_id": zone_id,
            "batch_id": batch_id,
            "concentration": concentration,
            "sampled_at": sampled_at,
            "basis": basis,
            "qualified": qualified,
            "current": basis == int(zone.get("basis", 0)),
        }
        current.setdefault("sample_results", []).append(result)
        record = {"sample_result": result, "invalidation": invalidation}
        new_status = "sampled"
        if invalidation and status == STAGE_RESTORED:
            new_status = STAGE_SAMPLED
        return new_status, current, record

    if action == "restore":
        _need_status(item, {"sampled"})
        progress = zone_progress(current)
        blocked = [entry for entry in progress if not entry["qualified_sample"]]
        if blocked:
            detail = "；".join(
                "%s 缺少 %s" % (entry["zone_id"], "、".join(
                    label for label in entry["missing_labels"] if label != MISSING_LABELS["restore"]
                ))
                for entry in blocked
            )
            raise DomainError("zones_not_cleared", "仍有区域未取得当前合格样本：%s" % detail, 409)
        basis_by_zone = {}
        sample_by_zone = {}
        for entry in progress:
            zone = current["zones"][entry["zone_id"]]
            zone["status"] = STAGE_RESTORED
            basis_by_zone[entry["zone_id"]] = entry["basis"]
            sample_by_zone[entry["zone_id"]] = entry["sample_id"]
        current["restoration"] = {
            "actor": actor,
            "note": payload.get("note", ""),
            "basis_by_zone": basis_by_zone,
            "sample_by_zone": sample_by_zone,
        }
        record = {"restoration": current["restoration"], "zone_ids": list(basis_by_zone)}
        return "restored", current, record

    if action == "cancel":
        _need_status(item, {"detected", "verified"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
