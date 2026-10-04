"""
RAGAS 评测模块
"""
from typing import Dict, List, Any, Optional
from dataclasses import dataclass


@dataclass
class EvalResult:
    """评测结果"""
    faithfulness: float = 0.0
    answer_relevancy: float = 0.0
    context_precision: float = 0.0
    context_recall: float = 0.0
    overall_score: float = 0.0


class RAGASEvaluator:
    """RAGAS 评测器"""

    def __init__(self):
        self.test_cases: List[Dict[str, str]] = []
        self.results: List[EvalResult] = []

    def add_test_case(self, question: str, ground_truth: str, context: str, answer: str):
        """添加测试用例"""
        self.test_cases.append({
            "question": question,
            "ground_truth": ground_truth,
            "context": context,
            "answer": answer,
        })

    def evaluate_faithfulness(self, answer: str, context: str) -> float:
        """评估忠实度 - 答案是否基于给定上下文"""
        if not context or not answer:
            return 0.0

        # 简单的关键词匹配评估
        context_words = set(context.lower().split())
        answer_words = set(answer.lower().split())

        if not answer_words:
            return 0.0

        overlap = len(context_words & answer_words)
        return min(overlap / len(answer_words), 1.0)

    def evaluate_relevancy(self, question: str, answer: str) -> float:
        """评估答案相关性"""
        if not question or not answer:
            return 0.0

        q_words = set(question.lower().split())
        a_words = set(answer.lower().split())

        if not q_words:
            return 0.0

        overlap = len(q_words & a_words)
        return min(overlap / len(q_words), 1.0)

    def evaluate_context_precision(self, question: str, context: str) -> float:
        """评估上下文精确度"""
        if not context:
            return 0.0

        q_words = set(question.lower().split())
        c_words = set(context.lower().split())

        if not c_words:
            return 0.0

        overlap = len(q_words & c_words)
        return min(overlap / len(c_words), 1.0)

    def evaluate_all(self) -> EvalResult:
        """评估所有测试用例"""
        if not self.test_cases:
            return EvalResult()

        total_faith = 0.0
        total_relev = 0.0
        total_prec = 0.0

        for case in self.test_cases:
            total_faith += self.evaluate_faithfulness(case["answer"], case["context"])
            total_relev += self.evaluate_relevancy(case["question"], case["answer"])
            total_prec += self.evaluate_context_precision(case["question"], case["context"])

        n = len(self.test_cases)
        result = EvalResult(
            faithfulness=total_faith / n,
            answer_relevancy=total_relev / n,
            context_precision=total_prec / n,
            context_recall=0.0,  # 需要 ground_truth 对比
            overall_score=(total_faith / n + total_relev / n + total_prec / n) / 3,
        )

        self.results.append(result)
        return result

    def compare_rerank(
        self,
        with_rerank: List[Dict],
        without_rerank: List[Dict],
        question: str,
    ) -> Dict[str, Any]:
        """对比有无 Rerank 的效果"""
        with_score = self.evaluate_context_precision(
            question, " ".join(d.get("content", "") for d in with_rerank)
        )
        without_score = self.evaluate_context_precision(
            question, " ".join(d.get("content", "") for d in without_rerank)
        )

        return {
            "with_rerank_score": with_score,
            "without_rerank_score": without_score,
            "improvement": with_score - without_score,
            "improvement_pct": f"{(with_score - without_score) / max(without_score, 0.001) * 100:.1f}%",
        }
