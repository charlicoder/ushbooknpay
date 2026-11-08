"""Reclassification of an existing payment record (KNET POS vs stale MyFatoorah link)."""

from __future__ import annotations

from types import SimpleNamespace

from app.api.v1.payments import _apply_explicit_classification


def _payment(**kw):
    base = dict(
        status="pending",
        payment_provider="myfatoorah",
        payment_method=None,
        payment_gateway=None,
        payment_through="ushdesk",
        payment_provider_group="MyFatoorah",
        payment_source="myfatoorah",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_knet_pos_overrides_stale_myfatoorah_on_unsettled_payment():
    p = _payment()
    _apply_explicit_classification(
        p, payment_provider="knet_pos", payment_through="ushdesk",
        payment_gateway="KNET", payment_method="card",
    )
    assert p.payment_provider == "knet_pos"
    assert p.payment_method == "card"  # normalised to knet_card by the model listener
    assert p.payment_provider_group is None
    assert p.payment_source is None


def test_settled_payment_is_never_reclassified():
    p = _payment(status="success", payment_method="knet_card")
    _apply_explicit_classification(
        p, payment_provider="knet_pos", payment_through="ushdesk",
        payment_gateway="KNET", payment_method="card",
    )
    assert p.payment_provider == "myfatoorah"
    assert p.payment_method == "knet_card"


def test_other_provider_never_overrides():
    p = _payment()
    _apply_explicit_classification(
        p, payment_provider="other", payment_through=None,
        payment_gateway=None, payment_method=None,
    )
    assert p.payment_provider == "myfatoorah"

