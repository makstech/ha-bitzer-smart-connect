"""Per-product profile overrides."""
from custom_components.bitzer_smart_connect.profiles import (
    PRODUCT_PROFILES,
    fan_mode_labels,
    param_limit,
    product_model,
)

ENSY = "c51da380-ba51-45ca-8818-a4faf6179b77"


def test_ensy_profile_limits():
    assert product_model(ENSY) == "Ensy InoVent"
    vent = param_limit(ENSY, "Control.VentSet")
    assert (vent.min, vent.max) == (1, 3)
    temp = param_limit(ENSY, "Control.TempSet")
    assert (temp.min, temp.max, temp.step) == (10, 26, 1)


def test_ensy_fan_mode_labels():
    assert fan_mode_labels(ENSY) == {1: "low", 2: "medium", 3: "high"}


def test_unknown_product_falls_back():
    assert product_model("does-not-exist") is None
    assert param_limit("does-not-exist", "Control.TempSet") is None
    assert param_limit(None, "Control.VentSet") is None
    assert fan_mode_labels("does-not-exist") is None
