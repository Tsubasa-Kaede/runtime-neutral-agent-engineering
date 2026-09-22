"""CU-CONTEXT W1 production wiring —— 接缝域铸造/编译投影。

Context 语义层（CU-CONTEXT-1/3）的唯一生产消费者：prompt 接缝在
调用时点经本模块读当前 run 事实 → 铸 ContextSnapshot →
compile_context 确定性投影 → 段 payload 交回接缝做模板组装。
本层无事实（输入只读 duck 读取）、零调用（不 invoke、不 emit）、
零持久化、零 route/TUI/引擎依赖；对语义层单向依赖（只 import
cockpit_context / cockpit_compile），绝不反向。

PRIOR 准入（单一规则，零双路径）：previous_result 存在、output 为
非空白 str、且 trace.invocation_id 为非空 str → 铸
PRIOR_STEP_OUTPUT 条目；否则快照仅携带 TASK。邻接旁证与条目铸造
由同一次读取派生：有 PRIOR 条目 → adjacent_invocation_ids=
(previous_invocation_id,)（恰一元组，与条目 source_id 同值，编译
选择阶段的成员判定结构性必中）；无 PRIOR →
adjacent_invocation_ids=None（唯一缺席拼写，首步同构）。调用方
不自行传旁证——旁证与铸造不可分叉。

W1 production contract（兼容边界）：
- 入座 cockpit 组合的 adapter，其 SUCCESS 结果必须携带 trace 与
  非空 invocation_id（shipped 八家已逐分支证明）；违例属
  out-of-contract 输入：防御性省略 PRIOR 条目（无节、无
  HANDOFF、不 raise、不失败）。
- whitespace-only prior（六家 adapter 的模型可控文本可达）同走
  防御性省略——DOCUMENTED SEMANTIC DELTA：legacy 接缝会原样嵌入，
  CU-CONTEXT-1 桥契约拒绝空白载荷，语义层权威。
- 修订 overlay 留在其冻结 owner（RevisionAdapter），位于编译产物
  下游；REVISION 条目本阶段不入编译。

Memory 保持 dormant（零 writer/零持久化/零桥）；Compiler 保持纯
函数（本层只供给事实与政策，零优化、零排序裁量）。
"""
try:  # flat-import mode（单一 module graph 纪律同构，先例见编译层）
    from cockpit_compile import CompilePolicy, compile_context
    from cockpit_context import (
        ContextSnapshot,
        derive_validity,
        item_from_invocation_result,
        task_item,
    )
except ImportError:  # embedded package context without the flat shim
    from .cockpit_compile import CompilePolicy, compile_context
    from .cockpit_context import (
        ContextSnapshot,
        derive_validity,
        item_from_invocation_result,
        task_item,
    )

__all__ = ("compile_invocation_context",)


def _prior_invocation_id(previous_result):
    """PRIOR 准入前置检查（单一规则）：可铸时返回 invocation_id。

    previous_result 存在、output 为非空白 str、trace.invocation_id
    为非空 str → 返回该 id（铸造与邻接旁证的同一读取值）；否则
    None。零 raise：检查逐条镜像 CU-CONTEXT-1 桥契约
    （item_from_invocation_result 的受理面）——契约拒收的输入在
    本层先行识别为 PRIOR 缺席，绝不把桥的构造期拒绝漏成运行期
    异常。"""
    if previous_result is None:
        return None
    output = getattr(previous_result, "output", None)
    if not isinstance(output, str) or not output.strip():
        return None
    trace = getattr(previous_result, "trace", None)
    invocation_id = getattr(trace, "invocation_id", None)
    if not isinstance(invocation_id, str) or not invocation_id.strip():
        return None
    return invocation_id


def compile_invocation_context(task_text, task_id, step_index,
                               previous_result, producer_role,
                               *, prior_char_limit):
    """唯一公共面：调用时点事实 → 编译投影（纯函数）。

    铸恰一快照（IDENTITY INVARIANT：每 (task_id, step_index) 至多
    一铸，快照身份即此二元组）：TASK 条目恒在（mandatory，载荷=
    任务原文，execution_version 诚实缺席）；PRIOR 条目 iff 前置
    检查过（载荷=输出原文全量——截断属编译域）。validity 经
    derive_validity 默认见证推导（现时事实无取代/陈旧见证 → 恒
    VALID）。政策 prior_output_char_limit 由调用方传入（接缝以
    _EMBED_LIMIT 为单真源派生，本层零常量复制）。返回
    CompiledInvocationContext——同输入逐字节同输出（compiler
    确定性律）。"""
    task = task_item(task_text, task_id=task_id)
    items = (task,)
    validity = (derive_validity(task),)
    adjacent = None
    invocation_id = _prior_invocation_id(previous_result)
    if invocation_id is not None:
        prior = item_from_invocation_result(
            previous_result, task_id=task_id,
            producer_role=producer_role)
        items = items + (prior,)
        validity = validity + (derive_validity(prior),)
        adjacent = (invocation_id,)
    snapshot = ContextSnapshot(
        task_id=task_id, step_index=step_index,
        items=items, validity=validity)
    return compile_context(
        snapshot,
        CompilePolicy(prior_output_char_limit=prior_char_limit),
        adjacent_invocation_ids=adjacent)
