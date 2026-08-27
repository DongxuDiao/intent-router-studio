import pytest

from app.router_core.training import TrainConfig, training_pair_samples, validate_resource_budget


def test_default_training_config_is_low_memory_friendly():
    cfg = TrainConfig()
    assert cfg.batch_size == 8
    assert cfg.max_length == 128
    assert cfg.num_iterations == 5


def test_resource_budget_rejects_large_setfit_pair_expansion():
    cfg = TrainConfig(num_iterations=10)
    assert training_pair_samples(24_380, cfg) == 243_800
    with pytest.raises(ValueError, match="训练资源预算超限"):
        validate_resource_budget(24_380, cfg)


def test_resource_budget_accepts_quick_profile_for_same_dataset():
    cfg = TrainConfig(num_epochs=1, num_iterations=3, batch_size=4, max_length=128)
    report = validate_resource_budget(24_380, cfg)
    assert report["pair_samples"] == 73_140
    assert report["status"] == "ok"
