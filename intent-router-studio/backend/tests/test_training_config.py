from app.router_core.training import TrainConfig, build_resource_plan, requested_training_pairs


def test_default_training_config_is_low_memory_friendly():
    cfg = TrainConfig()
    assert cfg.batch_size == 8
    assert cfg.max_length == 128
    assert cfg.num_iterations == 5
    assert cfg.fine_tune_embeddings is False


def test_resource_plan_caps_large_setfit_pair_expansion():
    cfg = TrainConfig(num_iterations=10, fine_tune_embeddings=True)
    assert requested_training_pairs(24_380, cfg) == 487_600
    report = build_resource_plan(24_380, cfg)
    assert report["requested_pair_samples"] == 487_600
    assert report["effective_pair_samples"] == 4_000
    assert report["embedding_max_steps"] == 500
    assert report["capped"] is True


def test_resource_plan_uses_quick_pair_cap_for_same_dataset():
    cfg = TrainConfig(
        num_epochs=1,
        num_iterations=3,
        batch_size=4,
        max_length=128,
        max_embedding_pairs=2_000,
        fine_tune_embeddings=True,
    )
    report = build_resource_plan(24_380, cfg)
    assert report["requested_pair_samples"] == 146_280
    assert report["effective_pair_samples"] == 2_000
    assert report["embedding_max_steps"] == 500
    assert report["status"] == "ok"


def test_frozen_encoder_plan_builds_no_contrastive_pairs():
    report = build_resource_plan(24_380, TrainConfig(fine_tune_embeddings=False))
    assert report["mode"] == "frozen_encoder"
    assert report["effective_pair_samples"] == 0
    assert report["embedding_max_steps"] == 0
    assert report["classifier_batch_size"] == 64
