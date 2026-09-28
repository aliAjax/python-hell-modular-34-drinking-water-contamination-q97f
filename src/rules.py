from .domain import DomainError
from datetime import datetime, timezone

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

# 每个受影响区域独立经历的复检阶段
ZONE_PENDING_FLUSH = "pending_flush"   # 待冲洗
ZONE_DISINFECTED = "disinfected"      # 已消毒
ZONE_SAMPLED = "sampled"              # 已采样（已有本批次样本）
ZONE_RESTORED = "restored"            # 已恢复（恢复供水）

ZONE_STAGE_LABELS = {
    ZONE_PENDING_FLUSH: "待冲洗",
    ZONE_DISINFECTED: "已消毒",
    ZONE_SAMPLED: "已采样",
    ZONE_RESTORED: "已恢复",
}

# 总体状态的推进次序，用于部分区域重做时回退总体状态
_STATUS_RANK = {
    "detected": 0,
    "verified": 1,
    "advisory": 2,
    "switched": 3,
    "flushing": 4,
    "disinfected": 5,
    "sampled": 6,
    "restored": 7,
    "cancelled": 8,
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


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 按区域维护的复检依据
# ---------------------------------------------------------------------------

def init_zone_progress(payload):
    """创建事件时为每个受影响区域建立独立的复检台账。"""
    zones = payload.get("zone_ids", [])
    payload["zone_progress"] = {
        zone: {
            "flushed": None,        # 最近一次冲洗 {at, actor, seq}
            "disinfected": None,    # 当前消毒批次依据 {at, actor, batch_id, seq}
            "sampled": None,        # 最近一次样本 {sample_id, concentration, batch_id, passed, at, actor, seq}
            "reopened": None,       # 再次登记来源后重新打开复检 {at, reason, detail, seq}
        }
        for zone in zones
    }
    payload.setdefault("disinfection_batches", [])  # 消毒批次（写明覆盖区域）
    payload.setdefault("restoration_history", [])   # 历次被作废的恢复依据
    payload.setdefault("invalidation_log", [])      # 历次依据作废记录
    payload["_seq"] = 0
    return payload


def _progress(current):
    return current.setdefault("zone_progress", {})


def _zone_record(current, zone_id):
    zones = _progress(current)
    if zone_id not in zones:
        raise DomainError("unknown_zone", "区域 %s 不在本次受影响区域清单内" % zone_id, 404)
    return zones[zone_id]


def _next_seq(current):
    current["_seq"] = int(current.get("_seq", 0)) + 1
    return current["_seq"]


def _void_restoration(current, zone_id, reason, detail, seq):
    """原恢复依据作废，留痕；已恢复事件因此回到已采样。"""
    restoration = current.get("restoration")
    if restoration:
        archived = dict(restoration)
        archived["voided_by"] = {"zone_id": zone_id, "reason": reason, "seq": seq}
        current.setdefault("restoration_history", []).append(archived)
        current.pop("restoration", None)


def _invalidate_zone(current, zone_id, reason, detail):
    """再次登记该区域污染来源：原恢复依据作废，旧样本不再代表当前水质，需重新采样。"""
    record = _zone_record(current, zone_id)
    seq = _next_seq(current)
    at = now_iso()
    entry = {"at": at, "reason": reason, "detail": detail, "seq": seq}
    record["reopened"] = entry
    current.setdefault("invalidation_log", []).append({"zone_id": zone_id, **entry})
    _void_restoration(current, zone_id, reason, detail, seq)
    return entry


def _fail_sample(current, zone_id, detail, seq):
    """复检超限：原恢复依据作废；超限样本本身保留为当前（不合格）样本。"""
    entry = {"at": now_iso(), "reason": "sample_exceeded", "detail": detail, "seq": seq}
    current.setdefault("invalidation_log", []).append({"zone_id": zone_id, **entry})
    _void_restoration(current, zone_id, "sample_exceeded", detail, seq)
    return entry


def _floor_status(status, new_floor):
    if status == "cancelled":
        return status
    if _STATUS_RANK.get(status, 0) < _STATUS_RANK.get(new_floor, 0):
        return new_floor
    return status


def _current_sample(record):
    """样本只对同一批次、在本次消毒之后、且晚于最近一次重新登记有效。"""
    sample = record.get("sampled")
    disinfected = record.get("disinfected")
    if not sample or not disinfected:
        return None
    if sample.get("batch_id") != disinfected.get("batch_id"):
        return None
    reopened = record.get("reopened")
    if reopened and int(sample.get("seq", 0)) <= int(reopened.get("seq", 0)):
        return None
    return sample


def zone_summaries(payload):
    """按区域计算当前复检阶段、缺少的步骤和依据是否有效。"""
    zones = payload.get("zone_progress", {})
    limit = float(payload.get("limit", 0) or 0)
    restoration = payload.get("restoration")
    summaries = []
    for zone_id in payload.get("zone_ids", []):
        record = zones.get(zone_id, {"flushed": None, "disinfected": None, "sampled": None, "reopened": None})
        flushed = record.get("flushed")
        disinfected = record.get("disinfected")
        sample = _current_sample(record)
        passed = bool(sample and float(sample["concentration"]) <= limit)
        reopened = record.get("reopened")
        ever_sampled = record.get("sampled") is not None

        missing = []
        if not flushed:
            missing.append("冲洗")
        if not disinfected:
            missing.append("消毒")
        if sample is None:
            if reopened and ever_sampled:
                missing.append("重新采样（再次登记来源之后）")
            else:
                missing.append("采样")
        elif not passed:
            missing.append("合格复检样本")
        ready = not missing

        if ready and restoration:
            stage = ZONE_RESTORED
        elif disinfected and (sample is not None or ever_sampled):
            # 已采样：样本可能不合格，或已被新来源登记作废，页面会列出缺口
            stage = ZONE_SAMPLED
        elif disinfected:
            stage = ZONE_DISINFECTED
        else:
            stage = ZONE_PENDING_FLUSH

        summaries.append({
            "zone_id": zone_id,
            "stage": stage,
            "stage_label": ZONE_STAGE_LABELS[stage],
            "flushed": flushed,
            "disinfected": disinfected,
            "sample": sample,
            "sample_passed": passed,
            "batch_id": disinfected.get("batch_id") if disinfected else None,
            "reopened": reopened,
            "ready_to_restore": ready,
            "missing": missing,
        })
    return summaries


def restore_readiness(payload):
    """所有区域都有当前合格样本后才能恢复供水。"""
    summaries = zone_summaries(payload)
    not_ready = [s["zone_id"] for s in summaries if not s["ready_to_restore"]]
    return not not_ready, not_ready, summaries


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

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
        _need_status(item, {"advisory", "flushing", "switched", "disinfected", "sampled"})
        zone_id = _text(payload, "zone_id")
        record = _zone_record(current, zone_id)
        # 已冲洗但尚未消毒时禁止重复冲洗；消毒/采样后允许重新冲洗重做该区域
        if record.get("flushed") and not record.get("disinfected"):
            raise DomainError("zone_already_flushed", "区域 %s 已冲洗，等待消毒" % zone_id, 409)
        seq = _next_seq(current)
        entry = {"at": now_iso(), "actor": actor, "seq": seq}
        record["flushed"] = entry
        current.setdefault("response_actions", []).append(
            {"type": "flush", "zone_id": zone_id, "at": entry["at"], "seq": seq}
        )
        new_status = _floor_status(status, "flushing")
        return new_status, current, {"zone_id": zone_id, "type": "flush", "seq": seq}

    if action == "disinfect":
        _need_status(item, {"flushing", "disinfected", "sampled"})
        if not payload.get("completed"):
            raise DomainError("disinfection_incomplete", "消毒尚未完成", 409)
        batch_id = _text(payload, "batch_id")
        covered = payload.get("zone_ids")
        if not isinstance(covered, list) or not covered or any(not isinstance(z, str) or not z.strip() for z in covered):
            raise DomainError("zones_required", "消毒批次必须写明覆盖区域 zone_ids")
        covered = [z.strip() for z in covered]

        batches = current.setdefault("disinfection_batches", [])
        if any(b.get("batch_id") == batch_id for b in batches):
            raise DomainError("duplicate_batch", "消毒批次 %s 已登记" % batch_id, 409)
        known = set(current.get("zone_ids", []))
        unknown = [z for z in covered if z not in known]
        if unknown:
            raise DomainError("unknown_zone", "区域 %s 不在受影响区域清单内" % ", ".join(unknown), 404)

        # 覆盖区域必须已冲洗，且不能已在另一个批次中消毒
        for covered_zone in covered:
            zone_record = _zone_record(current, covered_zone)
            if not zone_record.get("flushed"):
                raise DomainError("zone_not_flushed", "区域 %s 尚未冲洗，不能消毒" % covered_zone, 409)
            if zone_record.get("disinfected"):
                raise DomainError(
                    "zone_already_disinfected",
                    "区域 %s 已在批次 %s 中消毒" % (covered_zone, zone_record["disinfected"].get("batch_id")),
                    409,
                )

        seq = _next_seq(current)
        at = now_iso()
        batches.append({"batch_id": batch_id, "zone_ids": covered, "at": at, "actor": actor, "seq": seq})
        for covered_zone in covered:
            zone_record = _zone_record(current, covered_zone)
            zone_record["disinfected"] = {"at": at, "actor": actor, "batch_id": batch_id, "seq": seq}
            current.setdefault("response_actions", []).append(
                {"type": "disinfect", "zone_id": covered_zone, "batch_id": batch_id, "at": at, "seq": seq}
            )
        new_status = _floor_status(status, "disinfected")
        return new_status, current, {"type": "disinfect", "batch_id": batch_id, "zone_ids": covered, "seq": seq}

    if action == "sample":
        # 恢复后复检仍可能发现问题，故 restored 也允许继续采样
        _need_status(item, {"disinfected", "sampled", "restored"})
        zone_id = _text(payload, "zone_id")
        sample_id = _text(payload, "sample_id")
        concentration = float(payload.get("concentration", 0))
        if concentration < 0:
            raise DomainError("invalid_concentration", "浓度不能为负数")
        record = _zone_record(current, zone_id)

        existing_ids = {s["sample_id"] for s in current.get("sample_results", [])}
        if sample_id in existing_ids:
            raise DomainError("duplicate_sample", "样本 %s 已登记" % sample_id, 409)

        disinfected = record.get("disinfected")
        if not disinfected:
            raise DomainError("zone_not_disinfected", "区域 %s 尚未消毒，不能采样" % zone_id, 409)
        batch_id = disinfected["batch_id"]
        sampled_at = payload.get("sampled_at")
        if sampled_at is not None:
            if not isinstance(sampled_at, str) or not sampled_at.strip():
                raise DomainError("invalid_timestamp", "sampled_at 必须是 ISO 时间")
            try:
                datetime.fromisoformat(sampled_at.replace("Z", "+00:00"))
            except ValueError:
                raise DomainError("invalid_timestamp", "sampled_at 必须是 ISO 时间")
            if sampled_at < disinfected["at"]:
                raise DomainError(
                    "sample_before_disinfection",
                    "样本 %s 早于批次 %s 消毒时间，样本只对同一批次且在本次消毒之后有效" % (sample_id, batch_id),
                    409,
                )

        seq = _next_seq(current)
        at = sampled_at or now_iso()
        limit = float(current.get("limit", 0) or 0)
        passed = concentration <= limit
        result = {
            "sample_id": sample_id,
            "zone_id": zone_id,
            "concentration": concentration,
            "batch_id": batch_id,
            "passed": passed,
            "at": at,
            "actor": actor,
            "seq": seq,
        }
        current.setdefault("sample_results", []).append(result)
        record["sampled"] = result

        event_payload = {"sample_result": result}
        new_status = _floor_status(status, "sampled")

        if not passed:
            # 复检超限：原恢复依据作废，已恢复事件回到已采样，等待同批次合格样本
            invalidation = _fail_sample(
                current, zone_id,
                "样本 %s 浓度 %s 超过限值 %s" % (sample_id, concentration, limit),
                seq,
            )
            event_payload["invalidation"] = invalidation
            new_status = "sampled"
        return new_status, current, event_payload

    if action == "restore":
        _need_status(item, {"disinfected", "sampled", "restored"})
        ready, not_ready, summaries = restore_readiness(current)
        if not_ready:
            raise DomainError(
                "zones_not_cleared",
                "仍有区域未取得当前合格样本：%s" % "、".join(not_ready),
                409,
            )
        current["restoration"] = {
            "actor": actor,
            "note": payload.get("note", ""),
            "at": now_iso(),
            "basis": [
                {"zone_id": s["zone_id"], "batch_id": s["batch_id"], "sample_id": s["sample"]["sample_id"]}
                for s in summaries
            ],
        }
        return "restored", current, {"restoration": current["restoration"], "zone_count": len(summaries)}

    if action == "cancel":
        _need_status(item, {"detected", "verified"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")


def apply_source(item, normalized, actor, role):
    """登记污染来源；再次登记某区域来源时，该区域原恢复依据作废、需重新采样。"""
    current = dict(item["payload"])
    zone_id = normalized.get("zone_id")
    invalidation = None
    new_status = item["status"]
    if isinstance(zone_id, str) and zone_id.strip():
        zone_id = zone_id.strip()
        normalized["zone_id"] = zone_id
        # 仅当来源指向本次受影响区域、且该区域已开始处置时才推翻依据
        record = current.get("zone_progress", {}).get(zone_id)
        if record and (record.get("flushed") or record.get("disinfected") or record.get("sampled")):
            invalidation = _invalidate_zone(
                current, zone_id, "source_recorded",
                "再次登记污染来源 %s/%s" % (normalized.get("source_type"), normalized.get("external_id")),
            )
            if new_status == "restored":
                # 已恢复事件回到已采样
                new_status = "sampled"
    return current, new_status, invalidation
