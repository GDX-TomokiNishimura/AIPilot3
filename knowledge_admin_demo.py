"""Fictional knowledge, criteria, and Teams messages for the local preview."""

from datetime import UTC, datetime, timedelta

from knowledge_db import KnowledgeStore


def seed_demo(store: KnowledgeStore) -> None:
    chat = "dummy-teams-chat"
    messages = (
        ("m1", "mentor", "田中（指導担当）", "発注を確定する前に、在庫表と入荷予定を確認してください。"),
        ("m2", "trainee", "佐藤（新入社員）", "在庫が不足している場合は、先に代替品の在庫も確認します。"),
        ("m3", "mentor", "田中（指導担当）", "納期が迫っている案件は、担当者に早めに相談してください。"),
        ("m4", "mentor", "田中（指導担当）", "顧客へ回答する前に、納期を仕入先とすり合わせてください。"),
        ("m5", "trainee", "佐藤（新入社員）", "発注メモを担当チームに共有しました。"),
    )
    start = datetime(2026, 8, 10, 0, 15, tzinfo=UTC)
    for index, (message_id, sender, name, body) in enumerate(messages):
        store.register_message(
            chat_id=chat, message_id=message_id, sender_id=sender, sender_display_name=name,
            sent_at=start + timedelta(minutes=15 * index), body=body,
            source="graph", source_ref=f"dummy-teams:{message_id}",
        )

    approved = store.submit_criterion(
        statement="顧客へ納期を回答する前に、仕入先と日程を確認する",
        reason="誤った日程で回答すると、手配のやり直しや顧客への再連絡が発生するため。",
        evidence=((chat, "m4"),),
    )
    store.approve_criterion(approved.criterion_id, expected_revision=1, actor="田中（指導担当）")
    rejected = store.submit_criterion(
        statement="すべての発注メモを全社に共有する",
        reason="確認漏れを減らすため。",
        evidence=((chat, "m5"),),
    )
    store.reject_criterion(rejected.criterion_id, expected_revision=1, actor="田中（指導担当）")
    store.submit_criterion(
        statement="納期が迫っている案件は担当者へ早めに相談する",
        evidence=((chat, "m3"),),
    )
    candidate = store.submit_criterion(
        statement="発注前に対象商品の在庫を確認する",
        reason="二重発注を防ぐため。",
        evidence=((chat, "m1"), (chat, "m2")),
    )
    store.revise_criterion(
        candidate.criterion_id, expected_revision=1,
        statement="発注を確定する前に、在庫数と入荷予定を確認する",
        reason="重複発注を防ぎ、入荷予定で不足分を補えるか判断するため。",
        actor="田中（指導担当）",
    )

    for kind, title, body in (
        ("business_knowledge", "在庫表の見方", "現在庫、引当済み数量、入荷予定日を確認します。\n在庫表の更新日時も確認してください。"),
        ("business_rule", "発注の締め時間", "当日手配の受付は15時までです。締め時間後に急ぎの依頼が入った場合は担当者に相談します。"),
        ("project", "商品入れ替えプロジェクト", "対象商品の旧在庫と新商品の入荷予定をまとめ、週次ミーティングで更新します。"),
        ("employee_guidance", "発注前の確認手順", "在庫確認、入荷予定の確認、仕入先との日程調整の順に進めます。"),
    ):
        store.add_knowledge(kind=kind, title=title, body=body)
