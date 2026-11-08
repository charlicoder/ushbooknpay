from app.voucher.domain.history_actor import (
    is_customer_channel,
    resolve_history_actor,
    sanitize_history,
)

SENDER = {"name": "Test1"}


def test_customer_channels_are_case_insensitive():
    for v in ("ushweb", "USHWEB", "ushspa", "web", "app"):
        assert is_customer_channel(v)
    for v in ("ushdesk", "desk", None, ""):
        assert not is_customer_channel(v)


def test_payment_gateway_actor_becomes_customer_on_ushweb():
    for label in ("Payment Gateway", "Payment Gateway / Online", "system", "Staff", None):
        name, role = resolve_history_actor(
            payment_through="ushweb", sender_data=SENDER, name=label, role="Staff"
        )
        assert (name, role) == ("Test1", "Customer"), label


def test_real_user_name_is_kept_on_ushweb():
    name, role = resolve_history_actor(
        payment_through="ushweb", sender_data=SENDER, name="Sara Admin", role="Admin"
    )
    assert (name, role) == ("Sara Admin", "Admin")
    name, role = resolve_history_actor(
        payment_through="ushweb", sender_data=SENDER, name="Sara Admin", role=None
    )
    assert (name, role) == ("Sara Admin", "Staff")


def test_desk_defaults_to_staff_not_customer():
    assert resolve_history_actor(
        payment_through="ushdesk", sender_data=SENDER, name=None, role=None
    ) == ("Staff", "Staff")


def test_sanitize_history_fixes_stored_entries_without_mutating():
    stored = [
        {"action": "created", "changed_by_name": "Test1", "changed_by_role": "Customer"},
        {"action": "details_updated", "changed_by_name": "Payment Gateway",
         "changed_by_role": "Staff", "user_data": {"name": "Payment Gateway", "role": "Staff"}},
        {"action": "status_updated", "performed_by_name": "Payment Gateway / Online",
         "performed_by_role": "Staff"},
    ]
    out = sanitize_history(stored, payment_through="ushweb", sender_data=SENDER)
    assert [(e.get("performed_by_name") or e["changed_by_name"]) for e in out] == ["Test1"] * 3
    assert out[1]["changed_by_role"] == "Customer" and out[1]["user_data"]["role"] == "Customer"
    assert stored[1]["changed_by_name"] == "Payment Gateway"  # untouched


def test_sanitize_history_leaves_desk_vouchers_alone():
    stored = [{"action": "status_updated", "changed_by_name": "Staff", "changed_by_role": "Staff"}]
    assert sanitize_history(stored, payment_through="ushdesk", sender_data=SENDER) == stored

