# -*- coding: utf-8 -*-
"""CLI 子命令组注册模块"""
from cli.commands import (
    benchmarks,
    datasets,
    flywheel,
    models,
    prompts,
    rules,
    simulation,
    system,
)

GROUP_REGISTRARS = {
    "system": system.register_commands,
    "model": models.register_commands,
    "rule": rules.register_commands,
    "prompt": prompts.register_commands,
    "dataset": datasets.register_commands,
    "benchmark": benchmarks.register_commands,
    "simulation": simulation.register_commands,
    "flywheel": flywheel.register_commands,
}
