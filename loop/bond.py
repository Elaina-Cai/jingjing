"""最近互动的只读呈现。消息数量不证明回应关系，不自动推导亲疏。"""


def observation(*, messages: int, typed: int, span_s: float = 0.0) -> str:
    """输入是同一会话后续的观察；渲染没有文件或状态写能力。"""
    if messages <= 0:
        span = int(span_s // 60) if span_s else 0
        return "此后暂未观察到新消息" + ("（%d 分钟了）。" % span if span else "。")
    return ("此后这个会话出现了 %d 条新消息；抽样中 %d 条是文字。"
            "这不代表它们在回应我，也不能据此判断关系亲疏。" % (messages, typed))
