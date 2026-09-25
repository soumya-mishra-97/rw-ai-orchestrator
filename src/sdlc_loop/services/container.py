"""Composition root: the only place concrete implementations are chosen.

Everything else receives its collaborators through constructors, so tests and
the eval harness can swap the LLM backend (scripted, fault-injecting, batch)
without touching agent or graph code.
"""

from __future__ import annotations

from dataclasses import dataclass

from langgraph.checkpoint.sqlite import SqliteSaver

from sdlc_loop.agents import AgentDeps, EvaluatorAgent, build_personas
from sdlc_loop.config import Settings
from sdlc_loop.governance.audit import AuditLog
from sdlc_loop.governance.pii import Redactor, build_redactor
from sdlc_loop.graph.build_graph import CompiledGraph, compile_graph
from sdlc_loop.graph.checkpoint import open_checkpointer
from sdlc_loop.graph.nodes import GraphDeps
from sdlc_loop.graph.routing import GatePolicy
from sdlc_loop.llm.cache import CachePolicy
from sdlc_loop.llm.model_router import ModelRouter
from sdlc_loop.llm.types import LLMClient
from sdlc_loop.tools.code_verifier import CodeVerifier


@dataclass(slots=True)
class Container:
    settings: Settings
    llm: LLMClient
    redactor: Redactor
    audit: AuditLog
    router: ModelRouter
    agent_deps: AgentDeps
    checkpointer: SqliteSaver
    graph: CompiledGraph

    def close(self) -> None:
        self.audit.close()
        self.checkpointer.conn.close()


def build_container(settings: Settings, llm: LLMClient | None = None) -> Container:
    settings.ensure_dirs()
    if llm is None:
        from sdlc_loop.llm.anthropic_client import AnthropicLLMClient

        llm = AnthropicLLMClient(
            max_retries=settings.anthropic_max_retries, timeout_s=settings.anthropic_timeout_s
        )
    redactor = build_redactor(settings.pii_backend)
    audit = AuditLog(settings.audit_db, redactor)
    router = ModelRouter(settings)
    agent_deps = AgentDeps(
        llm=llm,
        router=router,
        cache=CachePolicy(enabled=settings.prompt_caching),
        settings=settings,
    )
    policy = GatePolicy.from_settings(settings)
    verifier = CodeVerifier(
        execute=settings.execute_generated_code, timeout_s=settings.code_exec_timeout_s
    )
    graph_deps = GraphDeps(
        personas=build_personas(agent_deps, verifier),
        evaluator=EvaluatorAgent(agent_deps, policy),
        audit=audit,
        router=router,
        settings=settings,
        policy=policy,
    )
    checkpointer = open_checkpointer(settings.checkpoint_db)
    return Container(
        settings=settings,
        llm=llm,
        redactor=redactor,
        audit=audit,
        router=router,
        agent_deps=agent_deps,
        checkpointer=checkpointer,
        graph=compile_graph(graph_deps, checkpointer),
    )
