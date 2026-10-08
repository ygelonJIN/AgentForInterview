"""按信息需求选择证据源的确定性规划层。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple


VALID_SOURCES = ("sql", "rag", "tool", "memory")
VALID_STRATEGIES = ("none", "sql_only", "rag_only", "mixed")


@dataclass(frozen=True)
class EvidenceNeed:
    """回答问题所需的事实类型，而不是执行顺序。"""

    facts: Tuple[str, ...] = ()
    constraints: Mapping[str, Any] = None  # type: ignore[assignment]
    required_sources: Tuple[str, ...] = ()
    optional_sources: Tuple[str, ...] = ()
    allow_partial: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "facts", tuple(dict.fromkeys(self.facts)))
        object.__setattr__(self, "constraints", dict(self.constraints or {}))
        object.__setattr__(
            self,
            "required_sources",
            self._validate_sources(self.required_sources, "required_sources"),
        )
        object.__setattr__(
            self,
            "optional_sources",
            self._validate_sources(self.optional_sources, "optional_sources"),
        )
        overlap = set(self.required_sources) & set(self.optional_sources)
        if overlap:
            raise ValueError(f"required_sources 与 optional_sources 不能重叠: {sorted(overlap)}")

    @staticmethod
    def _validate_sources(values: Iterable[str], field: str) -> Tuple[str, ...]:
        normalized = tuple(dict.fromkeys(str(value).strip().lower() for value in values if str(value).strip()))
        invalid = sorted(set(normalized) - set(VALID_SOURCES))
        if invalid:
            raise ValueError(f"{field} 包含未知证据源: {invalid}")
        return normalized


@dataclass(frozen=True)
class RetrievalPlan:
    """确定性数据源选择结果。"""

    facts: Tuple[str, ...]
    required_sources: Tuple[str, ...]
    optional_sources: Tuple[str, ...]
    allow_partial: bool
    requested_strategy: str
    policy_version: str = "evidence-v1"

    @property
    def selected_sources(self) -> Tuple[str, ...]:
        return tuple(dict.fromkeys(self.required_sources + self.optional_sources))

    @property
    def derived_strategy(self) -> str:
        selected = set(self.required_sources + self.optional_sources)
        if selected == {"sql"}:
            return "sql_only"
        if selected == {"rag"}:
            return "rag_only"
        if {"sql", "rag"} <= selected:
            return "mixed"
        return "none"

    def to_dict(self) -> Dict[str, Any]:
        return {
            **asdict(self),
            "selected_sources": list(self.selected_sources),
            "derived_strategy": self.derived_strategy,
        }


class EvidencePlanner:
    """把显式证据需求或兼容策略编译成数据源计划。"""

    def plan(
        self,
        *,
        strategy: str = "mixed",
        needs: Optional[Mapping[str, Any]] = None,
        evidence_need: Optional[EvidenceNeed] = None,
    ) -> RetrievalPlan:
        if strategy not in VALID_STRATEGIES:
            raise ValueError(f"不支持的检索策略: {strategy}")
        needs = dict(needs or {})
        if evidence_need is None:
            evidence_need = self._need_from_mapping(needs)

        required = list(evidence_need.required_sources)
        optional = list(evidence_need.optional_sources)
        if not required and not optional:
            required, optional = self._from_compat_strategy(strategy)

        # 兼容字段只补充缺失选择，不覆盖显式证据需求。
        if needs.get("needs_product") and "sql" not in required + optional:
            required.append("sql")
        if needs.get("needs_review") and "rag" not in required + optional:
            required.append("rag")

        plan = RetrievalPlan(
            facts=evidence_need.facts,
            required_sources=tuple(required),
            optional_sources=tuple(optional),
            allow_partial=evidence_need.allow_partial,
            requested_strategy=strategy,
        )
        requested_sources = self._strategy_sources(strategy)
        selected = set(plan.selected_sources)
        if strategy != "none" and selected != set(requested_sources):
            raise ValueError(
                f"证据需求 {sorted(selected)} 与兼容策略 {strategy} "
                f"要求的 {sorted(requested_sources)} 不一致"
            )
        return plan

    @staticmethod
    def _need_from_mapping(needs: Mapping[str, Any]) -> EvidenceNeed:
        return EvidenceNeed(
            facts=tuple(needs.get("facts") or ()),
            constraints=needs.get("constraints") or {},
            required_sources=tuple(needs.get("required_sources") or ()),
            optional_sources=tuple(needs.get("optional_sources") or ()),
            allow_partial=bool(needs.get("allow_partial", True)),
        )

    @staticmethod
    def _strategy_sources(strategy: str) -> Sequence[str]:
        return {
            "none": (),
            "sql_only": ("sql",),
            "rag_only": ("rag",),
            "mixed": ("sql", "rag"),
        }[strategy]

    @classmethod
    def _from_compat_strategy(cls, strategy: str) -> Tuple[list[str], list[str]]:
        return list(cls._strategy_sources(strategy)), []


def build_retrieval_plan(
    strategy: str,
    needs: Optional[Mapping[str, Any]] = None,
) -> RetrievalPlan:
    return EvidencePlanner().plan(strategy=strategy, needs=needs)
