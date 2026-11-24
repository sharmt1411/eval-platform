# -*- coding: utf-8 -*-
"""
模拟对弈与人工自由测试 API
专门承载双 Agent 模拟对弈（同步/SSE流式）、历史对弈会话查询/清空、以及人工自由对练白盒质检。
"""
import json
import uuid
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from backend.app.models import get_db, FlywheelSampleRecord
from backend.app.schemas import (
    ChatMessage,
    SimulationRunRequest,
    PersonaDefinition,
)
from backend.app.schemas.flywheel import InspectionReport, FlywheelRecord
from backend.app.api.deps import (
    get_adapter_and_config_by_model_id,
    load_rule_schema,
    messages_to_dicts,
    upsert_flywheel_sample,
)
from backend.app.services.simulation_service import SimulationService
from backend.app.services.flywheel_service import FlywheelPipelineService
from backend.app.engine.judge import UniversalJudgeEngine

router = APIRouter(prefix="/simulation", tags=["模拟对弈与自由测试"])


class EvaluateDialoguePayload(BaseModel):
    rule_id: str
    scenario_id: Optional[str] = "manual_playground"
    dialogue: List[ChatMessage]
    judge_model_id: str
    save_as_sample: bool = True


def _persist_flywheel_record(
    db: Session,
    task_id: str,
    rule_id: str,
    scenario: PersonaDefinition,
    raw_dialogue: List[ChatMessage],
    total_turns: int,
    record: FlywheelRecord,
    reviewer_comment: Optional[str] = None,
) -> Dict[str, Any]:
    """将 FlywheelPipelineService 产出的 FlywheelRecord 经 Upsert 持久化至 SQLite 并返回对弈展示载荷。"""
    final_score = (
        record.verified_eval.score
        if record.verified_eval
        else (record.initial_eval.score if record.initial_eval else 0)
    )
    remediated_dicts: Optional[List[Dict[str, Any]]] = (
        messages_to_dicts(record.remediated_messages)
        if record.remediated_messages
        else None
    )
    dialogue_dicts: List[Dict[str, Any]] = messages_to_dicts(raw_dialogue)

    db_row, _ = upsert_flywheel_sample(
        db,
        sample_id=record.id,
        task_id=task_id,
        rule_id=rule_id,
        scenario_id=scenario.id,
        status=record.status,
        score=final_score,
        raw_messages=record.raw_messages,
        remediated_messages=record.remediated_messages,
        applied_actions=record.applied_actions,
        benchmark_slices=[s.model_dump() for s in record.benchmark_slices],
        remediation_debug=record.remediation_debug or {},
        initial_eval=record.initial_eval.model_dump() if record.initial_eval else {},
        verified_eval=record.verified_eval.model_dump() if record.verified_eval else {},
        reviewer_comment=reviewer_comment,
        updated_at=record.updated_at,
        commit=True,
    )

    return {
        "sample_id": db_row.id,
        "scenario_id": scenario.id,
        "scenario_name": scenario.name,
        "status": record.status,
        "score": final_score,
        "total_turns": total_turns,
        "initial_eval": record.initial_eval.model_dump() if record.initial_eval else None,
        "verified_eval": record.verified_eval.model_dump() if record.verified_eval else None,
        "remediation_debug": record.remediation_debug,
        "dialogue": dialogue_dicts,
        "remediated": remediated_dicts,
    }


@router.post("/simulate")
def run_simulation_battle(
    payload: SimulationRunRequest,
    db: Session = Depends(get_db),
):
    """
    同步启动双 Agent 模拟对弈测试:
    1. 驱动虚拟客户与被测机器人对战;
    2. 对弈结束立即执行白盒拓扑质检与打分;
    3. 统一委托 FlywheelPipelineService 执行免修直通 / PENDING / 单次修复+二次质检+切题;
    4. 持久化至 SQLite FlywheelSampleRecord 表，供工作台复核。
    """
    rule = load_rule_schema(db, payload.rule_id)

    target_adapter, target_config = get_adapter_and_config_by_model_id(db, payload.target_model_id)
    client_adapter, client_config = get_adapter_and_config_by_model_id(db, payload.client_model_id)
    judge_adapter, judge_config = get_adapter_and_config_by_model_id(db, payload.judge_model_id)
    remediate_adapter, remediate_config = (
        get_adapter_and_config_by_model_id(db, payload.remediate_model_id)
        if payload.remediate_model_id
        else (None, None)
    )

    sim_service = SimulationService(
        target_adapter,
        client_adapter,
        judge_adapter,
        judge_config=judge_config,
        client_config=client_config,
    )
    pipeline = FlywheelPipelineService(
        judge_adapter=judge_adapter,
        remediate_adapter=remediate_adapter,
        judge_config=judge_config,
        remediate_config=remediate_config,
    )

    scenarios_to_run = rule.scenarios
    if payload.scenario_ids and payload.scenario_ids != ["all"]:
        scenarios_to_run = [s for s in rule.scenarios if s.id in payload.scenario_ids]

    if not scenarios_to_run:
        raise HTTPException(status_code=400, detail="未找到匹配的对弈场景")

    task_id = f"sim_{str(uuid.uuid4())[:8]}"
    battle_results: List[Dict[str, Any]] = []
    should_remediate = bool(payload.auto_remediate and remediate_adapter is not None)

    for sc in scenarios_to_run:
        battle = sim_service.run_battle(
            rule, sc, max_turns=payload.max_turns, target_config=target_config
        )
        eval_rep: InspectionReport = battle["eval_report"]
        dialogue: List[ChatMessage] = battle["dialogue"]
        sample_id = f"{task_id}_{sc.id}"

        record = pipeline.process_single_dialogue_record(
            rule=rule,
            scenario=sc,
            raw_messages=dialogue,
            sample_id=sample_id,
            precomputed_initial_eval=eval_rep,
            auto_remediate=should_remediate,
        )
        battle_results.append(
            _persist_flywheel_record(
                db=db,
                task_id=task_id,
                rule_id=rule.rule_id,
                scenario=sc,
                raw_dialogue=dialogue,
                total_turns=battle["total_turns"],
                record=record,
            )
        )

    return {
        "message": "模拟对弈完成",
        "task_id": task_id,
        "results": battle_results,
    }


@router.post("/simulate/stream")
def run_simulation_battle_stream(
    payload: SimulationRunRequest,
):
    """
    流式启动双 Agent 模拟对弈测试 (SSE Server-Sent Events):
    逐轮实时推送发言、SOP 连通性质检、统一委托飞轮服务自动修复与样本落库，支持多人设多样本批量执行。
    """
    def event_generator():
        from backend.app.models import SessionLocal
        bg_db = SessionLocal()
        try:
            rule = load_rule_schema(bg_db, payload.rule_id)
            target_adapter, target_config = get_adapter_and_config_by_model_id(
                bg_db, payload.target_model_id
            )
            client_adapter, client_config = get_adapter_and_config_by_model_id(
                bg_db, payload.client_model_id
            )
            judge_adapter, judge_config = get_adapter_and_config_by_model_id(
                bg_db, payload.judge_model_id
            )
            remediate_adapter, remediate_config = (
                get_adapter_and_config_by_model_id(bg_db, payload.remediate_model_id)
                if payload.remediate_model_id
                else (None, None)
            )

            sim_service = SimulationService(
                target_adapter,
                client_adapter,
                judge_adapter,
                judge_config=judge_config,
                client_config=client_config,
            )
            pipeline = FlywheelPipelineService(
                judge_adapter=judge_adapter,
                remediate_adapter=remediate_adapter,
                judge_config=judge_config,
                remediate_config=remediate_config,
            )

            scenarios_to_run = rule.scenarios
            if payload.scenario_ids and payload.scenario_ids != ["all"]:
                scenarios_to_run = [s for s in rule.scenarios if s.id in payload.scenario_ids]

            if not scenarios_to_run:
                yield f"data: {json.dumps({'event': 'error', 'data': {'message': '未找到匹配的对弈场景'}}, ensure_ascii=False)}\n\n"
                return

            task_id = f"sim_{str(uuid.uuid4())[:8]}"
            threshold = rule.inspection_rules.pass_score_threshold
            should_remediate = bool(payload.auto_remediate and remediate_adapter is not None)
            all_battle_results: List[Dict[str, Any]] = []

            for sc_idx, sc in enumerate(scenarios_to_run):
                yield f"data: {json.dumps({'event': 'scenario_start', 'data': {'scenario_id': sc.id, 'scenario_name': sc.name, 'scenario_index': sc_idx, 'total_scenarios': len(scenarios_to_run), 'task_id': task_id}}, ensure_ascii=False)}\n\n"

                battle_res: Optional[Dict[str, Any]] = None
                for ev_type, p_data, turn_count in sim_service.run_battle_iter(
                    rule, sc, max_turns=payload.max_turns, target_config=target_config
                ):
                    if ev_type == "turn":
                        turn_msg: ChatMessage = p_data
                        msg_dict = turn_msg.to_dict()
                        yield f"data: {json.dumps({'event': 'turn', 'data': {'scenario_id': sc.id, 'turn_index': turn_count, 'message': msg_dict}}, ensure_ascii=False)}\n\n"
                    elif ev_type == "judging":
                        yield f"data: {json.dumps({'event': 'judging', 'data': {'scenario_id': sc.id, 'turn_count': turn_count}}, ensure_ascii=False)}\n\n"
                    elif ev_type == "done":
                        battle_res = p_data

                if battle_res is None:
                    raise RuntimeError(f"场景 {sc.id} 对弈未返回完成结果")

                eval_rep: InspectionReport = battle_res["eval_report"]
                dialogue: List[ChatMessage] = battle_res["dialogue"]
                sample_id = f"{task_id}_{sc.id}"

                if not FlywheelPipelineService.is_gold_pass(eval_rep, threshold) and should_remediate:
                    yield f"data: {json.dumps({'event': 'remediating', 'data': {'scenario_id': sc.id, 'sample_id': sample_id}}, ensure_ascii=False)}\n\n"

                record = pipeline.process_single_dialogue_record(
                    rule=rule,
                    scenario=sc,
                    raw_messages=dialogue,
                    sample_id=sample_id,
                    precomputed_initial_eval=eval_rep,
                    auto_remediate=should_remediate,
                )
                sample_payload = _persist_flywheel_record(
                    db=bg_db,
                    task_id=task_id,
                    rule_id=rule.rule_id,
                    scenario=sc,
                    raw_dialogue=dialogue,
                    total_turns=battle_res["total_turns"],
                    record=record,
                )
                all_battle_results.append(sample_payload)

                yield f"data: {json.dumps({'event': 'scenario_done', 'data': sample_payload}, ensure_ascii=False)}\n\n"

            yield f"data: {json.dumps({'event': 'finish', 'data': {'task_id': task_id, 'total_scenarios': len(scenarios_to_run), 'results': all_battle_results}}, ensure_ascii=False)}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'event': 'error', 'data': {'message': str(e)}}, ensure_ascii=False)}\n\n"
        finally:
            bg_db.close()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/battles/recent")
def list_recent_battles(
    rule_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """查询最近执行的模拟对弈历史会话，供对弈台回放、切换与批量管理"""
    query = db.query(FlywheelSampleRecord)
    if rule_id and rule_id != "all":
        query = query.filter(FlywheelSampleRecord.rule_id == rule_id)
    if status and status != "all":
        query = query.filter(FlywheelSampleRecord.status == status)
    records = query.order_by(FlywheelSampleRecord.updated_at.desc()).limit(limit).all()

    results = []
    for r in records:
        raw_msgs = json.loads(r.raw_messages_json or "[]")
        remediated_msgs = json.loads(r.remediated_messages_json or "[]") if r.remediated_messages_json else None
        init_eval = json.loads(r.initial_eval_json or "{}")
        verified_eval = json.loads(r.verified_eval_json or "{}") if r.verified_eval_json else None
        remed_debug = (
            json.loads(r.remediation_debug_json or "{}")
            if getattr(r, "remediation_debug_json", None)
            else None
        )
        results.append({
            "sample_id": r.id,
            "task_id": r.task_id,
            "rule_id": r.rule_id,
            "scenario_id": r.scenario_id,
            "scenario_name": r.scenario_id,
            "status": r.status,
            "score": r.score,
            "total_turns": len(raw_msgs),
            "initial_eval": init_eval,
            "verified_eval": verified_eval,
            "remediation_debug": remed_debug,
            "dialogue": raw_msgs,
            "remediated": remediated_msgs,
            "updated_at": r.updated_at,
        })
    return results


@router.delete("/battles/all")
def clear_all_battles(
    rule_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """清空模拟对弈历史记录 (可根据 rule_id 过滤)"""
    query = db.query(FlywheelSampleRecord)
    if rule_id and rule_id != "all":
        query = query.filter(FlywheelSampleRecord.rule_id == rule_id)
    deleted_count = query.delete(synchronize_session=False)
    db.commit()
    return {"message": "历史对弈记录已清空", "deleted_count": deleted_count}


@router.post("/evaluate-dialogue")
def evaluate_dialogue_endpoint(
    payload: EvaluateDialoguePayload,
    db: Session = Depends(get_db),
):
    """
    支持人工自由对话结束后的即时白盒质检并可选入库:
    - 达标直通: 标定为 GOLD_PASS 并清洗生成标准 SFT 语料;
    - 未达标(仅初检未经过修复与复检): 统一标定为 PENDING (待复核)，补齐首轮 system，不直接淘汰为 DISCARDED。
    """
    rule = load_rule_schema(db, payload.rule_id)
    judge_adapter, judge_config = get_adapter_and_config_by_model_id(db, payload.judge_model_id)
    judge_engine = UniversalJudgeEngine(judge_adapter, default_config=judge_config)

    target_sc = next(
        (sc for sc in rule.scenarios if sc.id == payload.scenario_id),
        None,
    )
    if not target_sc:
        target_sc = PersonaDefinition(
            id=payload.scenario_id or "manual_playground",
            name="人工自由对练测试",
            category="人工实战测试",
            instruction="人工自由交互测试会话",
        )

    eval_report = judge_engine.evaluate_dialogue(rule, target_sc, payload.dialogue)

    sample_id: Optional[str] = None
    sample_status: Optional[str] = None
    if payload.save_as_sample:
        sample_id = f"manual_{uuid.uuid4().hex[:8]}_{target_sc.id}"
        pipeline = FlywheelPipelineService(
            judge_adapter=judge_adapter,
            remediate_adapter=None,
            judge_config=judge_config,
        )
        record = pipeline.process_single_dialogue_record(
            rule=rule,
            scenario=target_sc,
            raw_messages=payload.dialogue,
            sample_id=sample_id,
            precomputed_initial_eval=eval_report,
            auto_remediate=False,
        )
        sample_status = record.status
        persisted_payload = _persist_flywheel_record(
            db=db,
            task_id="manual_test",
            rule_id=rule.rule_id,
            scenario=target_sc,
            raw_dialogue=payload.dialogue,
            total_turns=len(payload.dialogue),
            record=record,
            reviewer_comment="人工自由对话实战质检测试",
        )
        sample_id = persisted_payload["sample_id"]

    return {
        "status": "SUCCESS",
        "sample_id": sample_id,
        "sample_status": sample_status,
        "eval_report": eval_report.model_dump(),
        "total_turns": len(payload.dialogue),
    }
