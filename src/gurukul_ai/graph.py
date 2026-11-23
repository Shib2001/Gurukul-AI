from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from gurukul_ai.providers import LessonProvider, ProviderResult
from gurukul_ai.schemas import AIChatRunRequest


class LessonGraphState(TypedDict):
    request: AIChatRunRequest
    provider_result: ProviderResult | None


async def run_lesson_graph(
    request: AIChatRunRequest,
    provider: LessonProvider,
) -> ProviderResult:
    async def generate(state: LessonGraphState) -> dict[str, ProviderResult]:
        return {"provider_result": await provider.generate(state["request"])}

    graph = StateGraph(LessonGraphState)
    graph.add_node("generate_lesson", generate)
    graph.add_edge(START, "generate_lesson")
    graph.add_edge("generate_lesson", END)
    result = await graph.compile().ainvoke(
        {"request": request, "provider_result": None}
    )
    provider_result = result["provider_result"]
    if provider_result is None:
        raise RuntimeError("Lesson graph completed without a provider result")
    return provider_result
