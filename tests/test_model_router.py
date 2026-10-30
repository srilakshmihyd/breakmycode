from breakmycode.model_router import ModelRouter


def test_model_router_uses_role_specific_environment(monkeypatch):
    monkeypatch.setenv("NEBIUS_MODEL", "fallback")
    monkeypatch.setenv("NEBIUS_MODEL_FAST", "nano")
    monkeypatch.setenv("NEBIUS_MODEL_STANDARD", "super")
    monkeypatch.setenv("NEBIUS_MODEL_REASONING", "ultra")

    router = ModelRouter.from_environment()

    assert router.for_task("classification") == "nano"
    assert router.for_task("patch") == "super"
    assert router.for_task("deep_diagnosis") == "ultra"
    assert router.for_task("classification", confidence=0.2) == "ultra"

