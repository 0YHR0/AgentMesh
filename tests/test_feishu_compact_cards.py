from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from agentmesh.integrations.feishu_notifications import (
    BusinessActivitySubject,
    ClaimedNotification,
    CollaborationSubject,
    FeishuNotificationWorker,
    build_card,
)


def visible(card):
    return str([e for e in card["elements"] if e["tag"] != "collapsible_panel"])


def test_business_ids_and_disclaimers_are_collapsed_not_deleted():
    subject = BusinessActivitySubject(
        uuid4(),
        3,
        "music-agent",
        "词作人",
        "RESULT",
        datetime.now(timezone.utc),
        "这句留住灯光意象就好。",
    )
    notice = ClaimedNotification(uuid4(), "t", "BUSINESS_ACTIVITY", uuid4(), "ACTIVITY_RESULT", 1)
    card = build_card(notice, subject, task_base_url=None, include_content=True)
    assert subject.summary in visible(card) and str(subject.object_id) not in visible(card)
    panel = card["elements"][-1]
    assert panel["expanded"] is False and str(subject.object_id) in str(panel)
    assert "不是模型内部推理" in str(panel)


def test_discussion_keeps_citations_out_of_prose_and_technical_ids_in_panel():
    subject = CollaborationSubject(
        uuid4(),
        uuid4(),
        "composer",
        "response",
        "编曲人",
        datetime.now(timezone.utc),
        "SUCCEEDED",
        (),
        "词作人，这句留着，尾奏再短点（audio-review）。",
        "尾奏",
        3,
        ("audio-review",),
    )
    notice = ClaimedNotification(uuid4(), "t", "TASK_RUN", subject.run_id, "COLLAB_RESULT", 1)
    card = build_card(notice, subject, task_base_url=None, include_content=True)
    assert "这句留着" in visible(card) and "audio-review" not in visible(card)
    assert str(subject.task_id) not in visible(card)
    assert "audio-review" in str(card["elements"][-1])
    hidden = build_card(notice, subject, task_base_url=None, include_content=False)
    assert "这句留着" not in str(hidden)


def test_discussion_reading_notice_is_suppressed_but_actual_reply_delivered():
    subject = CollaborationSubject(
        uuid4(),
        uuid4(),
        "composer",
        "response",
        "编曲人",
        datetime.now(timezone.utc),
        "SUCCEEDED",
        (),
        "保留这句。",
        "歌词",
        2,
    )
    notices = [
        ClaimedNotification(uuid4(), "t", "TASK_RUN", subject.run_id, kind, 1)
        for kind in ("COLLAB_STARTED", "COLLAB_RESULT")
    ]
    finishes, sends = [], []
    store = SimpleNamespace(
        claim=lambda **kwargs: notices,
        subject=lambda n: subject,
        finish=lambda n, **kwargs: finishes.append(kwargs["status"]),
    )
    worker = FeishuNotificationWorker(
        worker_id="test",
        store=store,
        client=SimpleNamespace(send=lambda **kw: sends.append(kw)),
        task_base_url=None,
        include_content=True,
        sync_collaboration=True,
    )
    assert worker.run_once() == 2
    assert finishes == ["SKIPPED", "DELIVERED"] and len(sends) == 1
