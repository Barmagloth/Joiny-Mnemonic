from __future__ import annotations

import importlib.util
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "plugins"
    / "nuextract-local"
    / "src"
    / "joiny_mnemonic_nuextract"
    / "__init__.py"
)
SPEC = importlib.util.spec_from_file_location("joiny_mnemonic_nuextract_test", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class FakeInputIds:
    shape = (1, 3)


class FakeInputs(dict):
    def __init__(self):
        super().__init__(input_ids=FakeInputIds())
        self.device = None

    def to(self, device):
        self.device = device
        return self


class FakeTokenizer:
    def __init__(self, completion):
        self.completion = completion
        self.prompt = None

    def __call__(self, prompt, *, return_tensors):
        self.prompt = prompt
        self.return_tensors = return_tensors
        return FakeInputs()

    def decode(self, tokens, *, skip_special_tokens):
        self.decoded_tokens = tokens
        self.skip_special_tokens = skip_special_tokens
        return self.completion


class FakeModel:
    device = "cpu"

    def __init__(self):
        self.arguments = None

    def generate(self, **arguments):
        self.arguments = arguments
        return [[0, 0, 0, 4, 5]]


class NuExtractPluginTest(unittest.TestCase):
    def event(self, content):
        return SimpleNamespace(content=content, role="user", kind="message")

    def test_extract_builds_bounded_prompt_and_parses_json(self):
        plugin = MODULE.NuExtractPlugin()
        tokenizer = FakeTokenizer(
            'prefix {"candidates":[{"memory_type":"decision",'
            '"normalized_content":"Use SQLite.",'
            '"evidence_quote":"Use SQLite.","confidence":0.95}]} suffix'
        )
        model = FakeModel()
        plugin._tokenizer = tokenizer
        plugin._model = model

        result = plugin.extract(
            self.event("Use SQLite."),
            context=(self.event("We compared databases."),),
            config={"inference_parameters": {"do_sample": False, "max_new_tokens": 12}},
        )

        self.assertEqual(result["candidates"][0]["memory_type"], "decision")
        self.assertIn("CONTEXT user: We compared databases.", tokenizer.prompt)
        self.assertIn("CURRENT EVENT:\nUse SQLite.", tokenizer.prompt)
        self.assertEqual(model.arguments["max_new_tokens"], 12)

    def test_extract_rejects_non_json_completion(self):
        plugin = MODULE.NuExtractPlugin()
        plugin._tokenizer = FakeTokenizer("not json")
        plugin._model = FakeModel()
        with self.assertRaisesRegex(ValueError, "no JSON object"):
            plugin.extract(self.event("Evidence."), context=(), config={})

    def test_lazy_load_pins_revision_and_uses_accelerate_device_map(self):
        plugin = MODULE.NuExtractPlugin()
        plugin.model_identity = "local/model"
        plugin.model_version = "revision-1"
        tokenizer = object()
        model = SimpleNamespace(device="cpu")
        tokenizer_loader = MagicMock(return_value=tokenizer)
        model_loader = MagicMock(return_value=model)
        fake_transformers = SimpleNamespace(
            AutoTokenizer=SimpleNamespace(from_pretrained=tokenizer_loader),
            AutoModelForCausalLM=SimpleNamespace(from_pretrained=model_loader),
        )
        with patch.dict("sys.modules", {"transformers": fake_transformers}):
            loaded = plugin._load()
        self.assertEqual(loaded, (tokenizer, model))
        tokenizer_loader.assert_called_once_with("local/model", revision="revision-1")
        model_loader.assert_called_once_with(
            "local/model", revision="revision-1", device_map="auto"
        )

        pyproject = tomllib.loads(
            (ROOT / "plugins" / "nuextract-local" / "pyproject.toml").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(
            any(item.startswith("accelerate") for item in pyproject["project"]["dependencies"])
        )


if __name__ == "__main__":
    unittest.main()
