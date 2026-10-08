"""RAG 回答质量评测：LLM Judge 为主，词面算法仅作显式 smoke fallback。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


@dataclass
class EvalResult:
    groundedness: float = 0.0
    answer_relevancy: float = 0.0
    context_precision: float = 0.0
    context_recall: float = 0.0
    overall_score: float = 0.0

    def to_dict(self) -> Dict[str, float]:
        return {
            "groundedness": self.groundedness,
            "answer_relevancy": self.answer_relevancy,
            "context_precision": self.context_precision,
            "context_recall": self.context_recall,
            "overall_score": self.overall_score,
        }


class JudgeVerdict(BaseModel):
    groundedness: float = Field(ge=0, le=1)
    answer_relevancy: float = Field(ge=0, le=1)
    context_precision: float = Field(ge=0, le=1)
    context_recall: float = Field(ge=0, le=1)
    reasons: List[str] = Field(default_factory=list)


class EvaluationConfigurationError(RuntimeError):
    """未配置 judge 时不允许静默退化为词面重叠分数。"""


class LexicalSmokeJudge:
    """只用于离线 smoke test，不作为正式 RAG 质量指标。"""

    @staticmethod
    def _overlap(left: str, right: str) -> float:
        left_words = set(str(left or "").lower().split())
        right_words = set(str(right or "").lower().split())
        if not right_words:
            return 0.0
        return len(left_words & right_words) / len(right_words)

    def evaluate(self, question: str, ground_truth: str, context: str, answer: str) -> JudgeVerdict:
        return JudgeVerdict(
            groundedness=self._overlap(answer, context),
            answer_relevancy=self._overlap(answer, question),
            context_precision=self._overlap(question, context),
            context_recall=self._overlap(ground_truth, context),
            reasons=["lexical-smoke-only"],
        )


class RAGEvaluator:
    """按统一 rubric 调用 LLM Judge 评估 RAG 回答。"""

    def __init__(self, judge_llm=None, *, allow_lexical_fallback: bool = False):
        self.judge_llm = judge_llm
        self.allow_lexical_fallback = allow_lexical_fallback
        self.test_cases: List[Dict[str, str]] = []
        self.results: List[EvalResult] = []

    def add_test_case(self, question: str, ground_truth: str, context: str, answer: str) -> None:
        self.test_cases.append({
            "question": question,
            "ground_truth": ground_truth,
            "context": context,
            "answer": answer,
        })

    def _judge_one(self, case: Dict[str, str]) -> JudgeVerdict:
        if self.judge_llm is None:
            if not self.allow_lexical_fallback:
                raise EvaluationConfigurationError("正式 RAG 评测必须配置 LLM Judge")
            return LexicalSmokeJudge().evaluate(
                case["question"], case["ground_truth"], case["context"], case["answer"]
            )
        prompt = (
            "你是严格的 RAG 评测员。分别评估：\n"
            "1. groundedness：回答是否完全由上下文支持；\n"
            "2. answer_relevancy：回答是否直接回答问题；\n"
            "3. context_precision：上下文是否包含解决问题所需且不冗余的信息；\n"
            "4. context_recall：上下文是否覆盖标准答案中的关键事实。\n"
            "所有分数为 0 到 1。不得因为文笔流畅而提高 groundedness。"
        )
        chain = self.judge_llm.with_structured_output(JudgeVerdict)
        return chain.invoke({
            "system": prompt,
            **case,
        })

    def evaluate_all(self) -> EvalResult:
        if not self.test_cases:
            result = EvalResult()
            self.results.append(result)
            return result
        verdicts = [self._judge_one(case) for case in self.test_cases]
        count = len(verdicts)
        result = EvalResult(
            groundedness=sum(item.groundedness for item in verdicts) / count,
            answer_relevancy=sum(item.answer_relevancy for item in verdicts) / count,
            context_precision=sum(item.context_precision for item in verdicts) / count,
            context_recall=sum(item.context_recall for item in verdicts) / count,
        )
        result.overall_score = (
            result.groundedness
            + result.answer_relevancy
            + result.context_precision
            + result.context_recall
        ) / 4
        self.results.append(result)
        return result

    def compare_rerank(
        self,
        with_rerank: List[Dict[str, Any]],
        without_rerank: List[Dict[str, Any]],
        question: str,
    ) -> Dict[str, Any]:
        """只比较上下文精确度；正式离线排序指标使用 rerank_ab 的 Recall/MRR/NDCG。"""
        if self.judge_llm is None and not self.allow_lexical_fallback:
            raise EvaluationConfigurationError("rerank 对比必须配置 LLM Judge")
        judge = self._judge_one if self.judge_llm else LexicalSmokeJudge().evaluate
        with_context = " ".join(item.get("content", "") for item in with_rerank)
        without_context = " ".join(item.get("content", "") for item in without_rerank)
        with_score = judge(question, "", with_context, "").context_precision
        without_score = judge(question, "", without_context, "").context_precision
        return {
            "with_rerank_score": with_score,
            "without_rerank_score": without_score,
            "improvement": with_score - without_score,
        }
