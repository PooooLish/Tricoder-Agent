"""Build public user turns from the same edit-scope contract used by scoring."""
from .models import EvalCase, ScenarioStep


def build_case_steps(case: EvalCase) -> tuple[ScenarioStep, ...]:
    steps = case.steps or (ScenarioStep("user_turn", content=case.task),)
    if not case.expose_edit_scope:
        return steps
    scope = "\n\n公开修改范围（相对工作区路径）：\n" + "\n".join(
        f"- {path}" for path in case.allowed_changes
    )
    scope += (
        "\n仅允许修改上述范围内的文件或目录，生产源码不得超出列表。"
        "如需添加测试，只能在列出的 tests/ 范围内添加直接子 Python 文件；"
        "不要求添加测试，不允许根目录测试文件或 tests/ 下的嵌套目录。"
        "若列表未允许 tests 路径，则不得添加测试。"
    )
    if case.path_match_mode == "workspace_root":
        scope += "上述路径模式均从工作区根匹配；通配符只匹配单层名称，不包含任意父目录前缀。"
    return tuple(
        ScenarioStep("user_turn", content=step.content + scope)
        if step.kind == "user_turn" else step
        for step in steps
    )
