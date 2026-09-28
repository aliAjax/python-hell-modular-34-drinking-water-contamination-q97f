from . import domain, rules
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        rules.init_zone_progress(normalized)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        # 再次登记某区域污染来源时，该区域原恢复依据作废、已恢复回到已采样
        new_payload, new_status, invalidation = rules.apply_source(item, normalized, actor, role)
        source_type = normalized.pop("source_type")
        external_id = normalized.pop("external_id")
        observed_at = normalized.pop("observed_at")
        result = self.repository.add_source(
            item_id,
            source_type,
            external_id,
            normalized,
            observed_at,
            actor,
            role,
            new_payload=new_payload if invalidation else None,
            new_status=new_status if invalidation else None,
            invalidation=invalidation,
        )
        return result

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        ready, not_ready, summaries = rules.restore_readiness(item["payload"])
        item["zones"] = summaries
        item["restore_ready"] = ready
        item["zones_waiting"] = not_ready
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        summary = self.repository.state_summary()
        for item in summary.get("items", []):
            ready, not_ready, zone_summaries = rules.restore_readiness(item["payload"])
            item["zones"] = zone_summaries
            item["restore_ready"] = ready
            item["zones_waiting"] = not_ready
        return summary
