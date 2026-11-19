# -*- coding: utf-8 -*-
"""
测试评测引擎与 API 对 max_tokens 与 ignore_sample_max_tokens 的控制
"""
import pytest
from backend.app.schemas.chat import ChatMessage, ModelConfig
from backend.app.schemas.benchmark import BenchmarkSample, ModelEvalConfig, BenchmarkRunRequest
from backend.app.adapters.mock_adapter import MockAdapter
from backend.app.engine.benchmark_runner import BenchmarkRunner


class InspectAdapter(MockAdapter):
    """用于捕获实际传入 chat 请求的 config.max_tokens"""
    def __init__(self):
        super().__init__()
        self.received_max_tokens = []

    def chat(self, request):
        self.received_max_tokens.append(request.config.max_tokens)
        return super().chat(request)


def test_benchmark_runner_token_controls():
    adapter = InspectAdapter()
    runner = BenchmarkRunner()

    sample_with_token_limit = BenchmarkSample(
        id="sample_1",
        category="测试分类",
        messages=[ChatMessage(role="user", content="请输出长文本")],
        max_new_tokens=64,
    )

    # 1. 默认情况: ignore_sample_max_tokens=False，样本自带 max_new_tokens=64 强行限制
    cfg_1 = ModelEvalConfig(
        model_id="mock_model_1",
        provider_type="mock",
        config=ModelConfig(max_tokens=4096, thinking_enabled=False),
    )
    res_1 = runner.evaluate_sample_single_model(
        sample=sample_with_token_limit,
        model_config=cfg_1,
        adapter=adapter,
        ignore_sample_max_tokens=False,
    )
    assert res_1.status in ("PASS", "FAIL")
    assert adapter.received_max_tokens[-1] == 64, "默认应使用样本自带的 64 max_new_tokens"

    # 2. 忽略样本限制: ignore_sample_max_tokens=True，应强制使用模型配置的 4096
    res_2 = runner.evaluate_sample_single_model(
        sample=sample_with_token_limit,
        model_config=cfg_1,
        adapter=adapter,
        ignore_sample_max_tokens=True,
    )
    assert res_2.status in ("PASS", "FAIL")
    assert adapter.received_max_tokens[-1] == 4096, "开启 ignore_sample_max_tokens 后应强制使用模型 4096"

    # 3. 自定义模型配置的 max_tokens=1024
    cfg_3 = ModelEvalConfig(
        model_id="mock_model_2",
        provider_type="mock",
        config=ModelConfig(max_tokens=1024, thinking_enabled=False),
    )
    res_3 = runner.evaluate_sample_single_model(
        sample=sample_with_token_limit,
        model_config=cfg_3,
        adapter=adapter,
        ignore_sample_max_tokens=True,
    )
    assert adapter.received_max_tokens[-1] == 1024, "应为自定义配置的 1024"


    # 4. 用户指定覆盖为 512 且忽略样本限制
    cfg_4 = ModelEvalConfig(
        model_id="mock_model_3",
        provider_type="mock",
        config=ModelConfig(max_tokens=512, thinking_enabled=False),
    )
    res_4 = runner.evaluate_sample_single_model(
        sample=sample_with_token_limit,
        model_config=cfg_4,
        adapter=adapter,
        ignore_sample_max_tokens=True,
    )
    assert res_4.status in ("PASS", "FAIL")
    assert adapter.received_max_tokens[-1] == 512, "开启 ignore_sample_max_tokens 且设定 512 后应为 512"


def test_benchmark_api_run_request_payload():
    """验证 BenchmarkRunRequest 支持 max_tokens 与 ignore_sample_max_tokens"""
    req = BenchmarkRunRequest(
        dataset_path="data/datasets/demo_benchmark.jsonl",
        model_ids=["test_model"],
        max_tokens=512,
        ignore_sample_max_tokens=True,
    )
    assert req.max_tokens == 512
    assert req.ignore_sample_max_tokens is True


def test_benchmark_task_execution_token_propagation(tmp_path):
    """验证 _execute_benchmark_task 全链路正确继承 512 max_tokens 与 ignore_sample_max_tokens"""
    import os
    import json
    import uuid
    from backend.app.models import SessionLocal, TaskRecord, RegisteredModel
    from backend.app.api.benchmarks import _execute_benchmark_task

    tmp_ds = tmp_path / "demo_tokens_bench.jsonl"
    tmp_ds.write_text(
        json.dumps(
            {
                "id": "tok_01",
                "category": "通用",
                "max_new_tokens": 64,
                "messages": [{"role": "user", "content": "请简短回答什么是 HTTP 404？"}],
                "reference": "资源未找到",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    db = SessionLocal()
    task_id = f"test_{uuid.uuid4().hex[:6]}"
    
    # 注册一个临时 mock 测试模型
    m_test = RegisteredModel(
        id=f"model_{task_id}",
        name="测试用Token模型",
        provider_type="mock",
        config_json=json.dumps({"max_tokens": 4096, "temperature": 0.3}),
    )
    db.add(m_test)

    rec = TaskRecord(
        id=task_id,
        task_type="benchmark",
        status="PENDING",
        result_json="{}",
    )
    db.add(rec)
    db.commit()

    payload = BenchmarkRunRequest(
        dataset_path=str(tmp_ds),
        model_configs=[
            ModelEvalConfig(
                model_id=f"model_{task_id}",
                config=ModelConfig(max_tokens=4096),
            )
        ],
        max_tokens=512,
        ignore_sample_max_tokens=True,
        workers=1,
    )

    try:
        _execute_benchmark_task(task_id, payload)

        task = db.query(TaskRecord).filter(TaskRecord.id == task_id).first()
        assert task is not None
        assert task.status == "COMPLETED"

        res = json.loads(task.result_json)
        assert res.get("max_tokens") == 512, "任务产物中的 max_tokens 应记录为 512"
        assert res.get("ignore_sample_max_tokens") is True, "任务产物中 ignore_sample_max_tokens 应为 True"
        assert len(res.get("model_configs", [])) > 0
        assert res["model_configs"][0]["config"]["max_tokens"] == 512, "被测模型配置中的 max_tokens 应被全局 512 覆盖"
    finally:
        # 清理测试数据
        db.query(TaskRecord).filter(TaskRecord.id == task_id).delete()
        db.query(RegisteredModel).filter(RegisteredModel.id == f"model_{task_id}").delete()
        db.commit()
        db.close()

