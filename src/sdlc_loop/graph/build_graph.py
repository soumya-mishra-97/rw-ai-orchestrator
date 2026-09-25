"""Assembles the state machine from :data:`~sdlc_loop.graph.stages.STAGES`.

START → pm → gate_pm_to_ba ─pass→ ba → gate_ba_to_architect ─pass→ architect → …
                  │ retry ↺ pm        │ retry ↺ ba
                  └ escalate → human_review ─accept→ next persona
                                             ─retry → same persona (Tier 2 + guidance)
                                             ─abort → END
… dev → gate_dev_to_qa ─pass→ qa → finalize → END
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from sdlc_loop.graph.nodes import (
    GraphDeps,
    Node,
    make_finalize_node,
    make_gate_node,
    make_human_review_node,
    make_persona_node,
)
from sdlc_loop.graph.routing import (
    END_NODE,
    HUMAN_ABORT,
    HUMAN_RETRY,
    route_after_gate,
    route_after_review,
)
from sdlc_loop.graph.stages import (
    FINALIZE_NODE,
    HUMAN_REVIEW_NODE,
    STAGES,
    Stage,
    next_stage,
    stage_for_handoff,
)
from sdlc_loop.graph.state import SDLCState
from sdlc_loop.schemas.review import ReviewAction

type CompiledGraph = CompiledStateGraph[SDLCState, Any, SDLCState, SDLCState]


def _gate_router(stage: Stage) -> Any:
    def route(state: SDLCState) -> str:
        return route_after_gate(state.get("last_verdict"), stage)

    return route


def _review_router(state: SDLCState) -> str:
    handoff = state.get("pending_handoff")
    assert handoff is not None
    verdict = state.get("last_verdict")
    action: ReviewAction = (
        "abort" if verdict == HUMAN_ABORT else "retry" if verdict == HUMAN_RETRY else "accept_as_is"
    )
    return route_after_review(action, stage_for_handoff(handoff))


def _add(graph: StateGraph[SDLCState, Any, SDLCState, SDLCState], name: str, node: Node) -> None:
    # LangGraph's overloads cannot match a plain Callable[[TypedDict], dict] under mypy;
    # the runtime contract (state in, partial update out) is exactly what we pass.
    graph.add_node(name, node)  # type: ignore[call-overload]


def build_state_graph(deps: GraphDeps) -> StateGraph[SDLCState, Any, SDLCState, SDLCState]:
    graph: StateGraph[SDLCState, Any, SDLCState, SDLCState] = StateGraph(SDLCState)
    for stage in STAGES:
        _add(graph, stage.node, make_persona_node(stage, deps))
        if stage.handoff is not None:
            _add(graph, stage.gate_node, make_gate_node(stage, deps))
    _add(graph, HUMAN_REVIEW_NODE, make_human_review_node(deps))
    _add(graph, FINALIZE_NODE, make_finalize_node(deps))

    graph.add_edge(START, STAGES[0].node)
    for stage in STAGES:
        if stage.handoff is None:
            graph.add_edge(stage.node, FINALIZE_NODE)
            continue
        nxt = next_stage(stage)
        graph.add_edge(stage.node, stage.gate_node)
        graph.add_conditional_edges(
            stage.gate_node,
            _gate_router(stage),
            [stage.node, nxt.node if nxt else FINALIZE_NODE, HUMAN_REVIEW_NODE],
        )
    graph.add_conditional_edges(
        HUMAN_REVIEW_NODE,
        _review_router,
        [*(s.node for s in STAGES), FINALIZE_NODE, END_NODE],
    )
    graph.add_edge(FINALIZE_NODE, END)
    return graph


def compile_graph(deps: GraphDeps, checkpointer: BaseCheckpointSaver[Any]) -> CompiledGraph:
    return build_state_graph(deps).compile(checkpointer=checkpointer)
