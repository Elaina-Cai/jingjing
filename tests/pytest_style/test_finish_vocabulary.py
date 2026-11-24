"""P2-1 词表守卫：三种方言都必须能翻译，且新取值出现时会红。

跑法（与其它 pytest_style 一致）：`scripts/run_pytest_suite.py`。
"""

from __future__ import annotations

from core.contracts import finish as F
from core.jobs import runner as job_runner
from core.jobs import store as job_store
from core.tasks import store as task_store
from core.tasks import slot_fill


def test_five_codes_only():
    assert F.FINISH_CODES == ("pending", "done", "failed", "expired", "abandoned")
    assert F.FINISHED_CODES == ("done", "failed", "expired", "abandoned")


def test_every_job_stop_reason_is_translatable():
    """`STOP_REASONS` 新增一个而这里没跟上 → 立刻红（防方言再分叉）。"""
    for stop in job_runner.STOP_REASONS:
        code = F.from_job_stop(stop)
        assert code in F.FINISH_CODES, (stop, code)
    assert F.from_job_stop("done") == F.DONE
    assert F.from_job_stop("failed") == F.FAILED
    for waiting in ("need_approval", "awaiting", "budget", "turns", "state", "no_executor", "shadow"):
        assert F.from_job_stop(waiting) == F.PENDING, waiting


def test_every_receipt_outcome_is_translatable():
    for outcome in task_store.RUN_OUTCOMES:
        assert F.from_receipt(outcome) in F.FINISH_CODES
    assert F.from_receipt("sent") == F.DONE
    # 台账侧原话：只有 sent 算办好，silent / failed / blocked 都不是完成
    assert F.from_receipt("silent") == F.FAILED
    assert F.from_receipt("failed") == F.FAILED
    assert F.from_receipt("blocked") == F.PENDING
    assert set(F.from_receipt(o) for o in task_store.RUN_OUTCOMES) & {F.DONE} == {F.DONE}
    assert all(o != "sent" or F.from_receipt(o) == F.DONE for o in task_store.RUN_OUTCOMES)


def test_completed_outcomes_is_exactly_what_we_call_done():
    """`COMPLETED_OUTCOMES` 与词表的 `done` 必须一一对应——这是两条口径的唯一交点。"""
    done = tuple(o for o in task_store.RUN_OUTCOMES if F.from_receipt(o) == F.DONE)
    assert done == tuple(slot_fill.COMPLETED_OUTCOMES)


def test_every_job_state_is_translatable():
    for state in ("open", "awaiting", "done", "cancelled", "expired"):
        assert F.from_job_state(state) in F.FINISH_CODES
    assert F.from_job_state("done") == F.DONE
    assert F.from_job_state("cancelled") == F.ABANDONED
    assert F.from_job_state("expired") == F.EXPIRED
    assert F.from_job_state("awaiting") == F.PENDING


def test_every_work_item_status_is_translatable():
    for status in task_store.WORK_ITEM_STATUSES:
        assert F.from_work_item_status(status) in F.FINISH_CODES
    assert F.from_work_item_status("done") == F.DONE
    assert F.from_work_item_status("superseded") == F.ABANDONED
    assert F.from_work_item_status("waiting_user") == F.PENDING


def test_intention_translation_covers_real_results():
    assert F.from_intention("open") == F.PENDING
    assert F.from_intention("closed", "completed:水我已经喝过了") == F.DONE
    assert F.from_intention("closed", "matched:汪P5") == F.DONE
    assert F.from_intention("closed", "expired（超时未完成）") == F.EXPIRED
    assert F.from_intention("closed", "cleared_by_owner（2026-11-03 主人要求清空待办）") == F.ABANDONED
    # 关了账却说不清结论 → 宁可算失败，不要谎报办成
    assert F.from_intention("closed", "不知道什么结论") == F.FAILED
    assert F.from_intention("closed", "") == F.FAILED


def test_unknown_values_never_become_done():
    for junk in ("", "weird", "DONE!!", None, 123):
        for fn in (F.from_job_state, F.from_job_stop, F.from_receipt):
            assert fn(junk) == F.PENDING
        assert F.normalise(junk) == F.PENDING
    assert F.from_intention("closed", "weird") == F.FAILED


def test_case_and_space_tolerated():
    assert F.from_job_state("  DONE ") == F.DONE
    assert F.from_receipt("Sent") == F.DONE


def test_is_finished():
    assert F.is_finished("done") and F.is_finished("failed")
    assert F.is_finished("expired") and F.is_finished("abandoned")
    assert not F.is_finished("pending")
    assert F.is_finished("sent")          # 别名也认
    assert not F.is_finished("blocked")   # 被闸挡住是还没结，不是终态


def test_describe_is_chinese_and_total():
    for code in F.FINISH_CODES:
        assert F.describe(code)
        assert F.describe(code) != "未知"
    assert F.describe("nonsense") == "未知"


def test_count_by_finish_has_all_keys_and_counts_everything():
    counts = F.count_by_finish(["sent", "failed", "pending", "expired", "cleared_miss"])
    assert set(counts) == set(F.FINISH_CODES)
    assert counts[F.DONE] == 1
    assert counts[F.FAILED] == 1
    assert counts[F.PENDING] == 2   # pending + 不认识的那个
    assert counts[F.EXPIRED] == 1
    assert sum(counts.values()) == 5


def test_abandoned_rate_only_counts_finished():
    # 4 已结：1 放弃 + 1 过期 + 2 办成 → 2/4
    assert F.abandoned_rate({F.DONE: 2, F.ABANDONED: 1, F.EXPIRED: 1}) == 0.5
    # 还没结的不进分母
    assert F.abandoned_rate({F.PENDING: 99, F.DONE: 1}) == 0.0
    # 没有已结 → 0.0，不除零
    assert F.abandoned_rate({F.PENDING: 3}) == 0.0
    assert F.abandoned_rate({}) == 0.0


# ---------------------------------------------------------------- P2-2 补丁
#: 2026-11-03 P2-2：``abandoned`` 从"只在写入侧偶然出现"变成**状态机里的正式终态**。


def test_job_states_include_abandoned():
    """主人叫停（cancelled）与办不下去（abandoned）必须是两个词。"""
    from core.contracts import plan

    assert "abandoned" in plan.JOB_STATES
    assert "abandoned" in plan.JobState
    assert "cancelled" in plan.JOB_STATES


def test_abandoned_state_is_still_a_finish():
    assert F.from_job_state("abandoned") == F.ABANDONED
    assert F.is_finished(F.from_job_state("abandoned")) is True


def test_abandon_cause_splits_owner_from_stuck():
    assert F.abandon_cause("cancelled") == "owner"
    assert F.abandon_cause("abandoned") == "stuck"
    # updated_by 是唯一的证据：操作员叫停 = 正常收尾；别人收摊 = 办不下去
    assert F.abandon_cause("cancelled", updated_by="operator") == "owner"
    assert F.abandon_cause("cancelled", updated_by="judge") == "stuck"
    assert F.abandon_cause("done") == ""


def test_count_by_abandon_cause_accepts_pairs():
    out = F.count_by_abandon_cause(
        [("cancelled", "operator"), ("cancelled", "operator"), ("abandoned", ""), "done"]
    )
    assert out["owner"] == 2
    assert out["stuck"] == 1
    assert out["superseded"] == 0


def test_stuck_rate_ignores_owner_cancellations():
    counts = {F.DONE: 13, F.ABANDONED: 18}
    assert F.stuck_rate(counts, [("cancelled", "operator")] * 18) == 0.0
    assert F.stuck_rate(counts, [("abandoned", "")] * 3 + [("cancelled", "operator")] * 15) == round(3 / 31, 4)
    assert F.stuck_rate({}, []) == 0.0


def test_finish_report_script_uses_the_shared_vocabulary():
    """报表不许自己再定义一套词，必须走 core.contracts.finish。"""
    import io as _io
    import os as _os

    root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    src = _io.open(_os.path.join(root, "scripts", "finish_report.py"), encoding="utf-8").read()
    for needle in ("from core.contracts import finish as F", "count_by_finish", "stuck_rate", "updated_by"):
        assert needle in src, needle
