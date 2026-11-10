"""Management only: never invokes a model, scheduler, transport or global sender."""
from pathlib import Path

from pydantic import ValidationError

from api.v1.schemas.research_delivery import DestinationCreate, DestinationUpdate, CredentialWrite, SubscriptionCreate, SubscriptionUpdate
from src.repositories.research_delivery_models import DeliveryDestination, DeliverySubscription
from src.repositories.research_delivery_repository import ManagementError
from src.services.research_delivery.credentials import CredentialError, credential_fingerprint
from src.services.screening.strategy import load_all_strategies


# 计划时间（新建订阅时的预填值；实际投递以订阅上保存的 planned_time 为准）。
#
# 语义已变更为「最早放行时间」而非「每群独占时段」：各群统一使用窗口起点，
# 工作进程到点后按 `automation.DELIVERY_ORDER` 的顺序**一个一个跑、跑完即推**，
# 数据未就绪则顺延（retry_wait 后重试），不设固定十分钟间隔。
# 值等于 `timing.PUBLICATION_WINDOW_START`，两侧必须一致，否则启用校验会拒绝。
DEFAULT_PLANNED_TIME = "15:30"


def _validated(schema, data):
    try:
        result = schema.model_validate(data).model_dump(exclude_unset=True)
    except ValidationError:
        raise ManagementError("invalid_request", "Invalid management fields") from None
    if any(value is None for value in result.values()):
        raise ManagementError("invalid_request", "Explicit null fields are not supported")
    return result


class ResearchDeliveryService:
    def __init__(self, repository, vault, strategies_dir=None):
        self.repository = repository
        self.vault = vault
        self.strategies_dir = strategies_dir or Path(__file__).resolve().parents[1] / "screening" / "strategies"

    def strategies(self):
        return [{"strategy_id": row.name, "display_name": row.display_name, "description": row.description,
                 "version": row.version, "market_scope": row.screening.market_scope}
                for row in load_all_strategies(self.strategies_dir).values() if row.screening.market_scope == ["cn"]]

    def _strategy(self, strategy_id):
        if strategy_id not in {row["strategy_id"] for row in self.strategies()}:
            raise ManagementError("invalid_strategy", "Unknown or unsupported A-share strategy")

    def categories(self):
        """Registration is separate from a working content producer; no fake readiness."""
        order = ("volume_breakout", "shrink_pullback", "momentum_quality", "capital_heat",
                 "oversold_reversal", "balanced_alpha", "dual_low", "quality_value",
                 "low_volatility_quality", "blue_chip_income")
        registry = {row["strategy_id"]: row for row in self.strategies()}
        items = [{"category_id": key, "display_name": registry[key]["display_name"],
                  "description": registry[key]["description"], "code": f"S{i + 1:02d}",
                  "destination_kind": "strategy", "preview_available": True,
                  "planned_time": DEFAULT_PLANNED_TIME,
                  "producer_status": "historical_draft_only"}
                 for i, key in enumerate(order) if key in registry]
        for key, code, name, kind in (
            ("etf_rotation", "S11", "ETF 轮动", "strategy"),
            ("market_radar", "R01", "市场雷达", "radar"),
            ("announcement_risk", "R02", "公告与风险", "radar"),
            ("system_status", "O01", "系统状态", "ops"),
        ):
            items.append({"category_id": key, "display_name": name, "description": "可独立绑定群；内容管道尚未接入多群发布。",
                          "code": code, "destination_kind": kind, "preview_available": False,
                          "planned_time": DEFAULT_PLANNED_TIME,
                          "producer_status": "not_connected"})
        if getattr(self, 'automation', None) is not None:
            for item in items:
                if item['category_id'] != 'system_status':
                    item['producer_status'] = 'live_on_demand'
                    if item['category_id'] in {'etf_rotation', 'market_radar', 'announcement_risk'}:
                        item['description'] = '独立群绑定与有明确范围的数据生产。'
                    item['description'] += ' 已接入当日按需数据生产；正式发布仍须通过数据、授权与时窗校验。'
        return items

    def _category(self, category_id):
        if category_id not in {row["category_id"] for row in self.categories()}:
            raise ManagementError("invalid_strategy", "Unknown research category")

    def create_destination(self, data):
        return self.repository.create(DeliveryDestination, _validated(DestinationCreate, data))

    def update_destination(self, record_id, data):
        values = _validated(DestinationUpdate, data)
        version = values.pop("version")
        return self.repository.update(DeliveryDestination, record_id, version, values)

    def create_subscription(self, data):
        values = _validated(SubscriptionCreate, data)
        self._category(values["strategy_id"])
        # Include schema defaults while preserving immutable disabled creation.
        values = SubscriptionCreate.model_validate(values).model_dump()
        return self.repository.create(DeliverySubscription, values)

    def update_subscription(self, record_id, data):
        values = _validated(SubscriptionUpdate, data)
        version = values.pop("version")
        if "strategy_id" in values:
            self._category(values["strategy_id"])
        return self.repository.update(DeliverySubscription, record_id, version, values)

    def set_credentials(self, record_id, data):
        values = _validated(CredentialWrite, data)
        current = self.repository.get(DeliveryDestination, record_id)
        if current["version"] != values["version"]:
            raise ManagementError("version_conflict", "Record changed; refresh before editing", 409)
        try:
            fingerprint = credential_fingerprint(values["webhook_url"])
        except CredentialError:
            raise ManagementError("invalid_webhook", "Invalid official Feishu webhook") from None
        secret = values.get("signing_secret", "")
        try:
            if not secret and current["signing_configured"]:
                binding = self.repository.credential_binding(record_id)
                previous = self.vault.resolve(binding["credential_ref"])
                if previous.webhook_url != values["webhook_url"]:
                    raise ManagementError("signing_secret_required", "Changing a signed webhook requires its new signing secret")
                secret = previous.signing_secret
            reference = self.vault.store(values["webhook_url"], secret)
        except CredentialError:
            raise ManagementError("credential_storage_unavailable", "Protected credential storage unavailable", 503) from None
        return self.repository.update(DeliveryDestination, record_id, values["version"], {
            "credential_ref": reference, "credential_fingerprint": fingerprint,
            "signing_configured": bool(secret), "keyword": values.get("keyword", current["keyword"])})
