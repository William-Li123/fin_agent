from decimal import Decimal
from types import SimpleNamespace

import pytest

from finagent.models import BudgetAccountingError, BudgetExceeded, BudgetLedger, ModelClient, ModelResponseError
from finagent.models.client import normalize_messages, validate_base_url


MESSAGES = [{"role": "system", "content": "Return a JSON action."}, {"role": "user", "content": "计算收入同比。"}]


class FakeClient:
    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response if response is not None else good_response()
        self.error = error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def good_response(content='{"action":"finish"}', finish_reason="stop"):
    return SimpleNamespace(usage={"prompt_tokens": 100, "completion_tokens": 20,
                                  "total_tokens": 120, "prompt_cache_hit_tokens": 50,
                                  "prompt_cache_miss_tokens": 50},
                           choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)])


def setup(tmp_path, **kwargs):
    account = BudgetLedger(tmp_path / "budget.sqlite", ceiling_cny=1, carried_cny=0)
    fake = FakeClient(**kwargs)
    return account, fake, ModelClient(account, client=fake)


def test_real_request_shape_and_usage_accounting(tmp_path):
    account, fake, model = setup(tmp_path)
    assert model.complete(MESSAGES, stage="agent.turn") == '{"action":"finish"}'
    request = fake.calls[0]
    assert request["model"] == "deepseek-flash"
    assert request["temperature"] == 0
    assert request["stream"] is False
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert request["max_tokens"] == 2048
    assert model.last_usage["call_id"] == model.last_call_id
    assert Decimal(account.status()["new_known_cny"]) == Decimal("0.000262")


def test_transport_failure_never_retries_or_records_secret_exception(tmp_path):
    secret = "sk-sensitive-test-credential"
    account, fake, model = setup(tmp_path, error=RuntimeError("Authorization: " + secret))
    model._api_key = secret
    with pytest.raises(BudgetAccountingError) as caught:
        model.complete(MESSAGES, stage="agent.turn")
    assert len(fake.calls) == 1
    assert account.status()["halted"]
    assert Decimal(account.status()["new_reserved_cny"]) > 0
    assert secret not in str(caught.value)
    assert secret not in repr(model)
    assert secret not in account.path.read_bytes().decode("utf-8", errors="ignore")
    with pytest.raises(BudgetAccountingError):
        model.complete(MESSAGES, stage="agent.turn")
    assert len(fake.calls) == 1


def test_budget_refusal_does_not_make_network_request(tmp_path):
    account = BudgetLedger(tmp_path / "budget.sqlite", ceiling_cny=0, carried_cny=0)
    fake = FakeClient()
    model = ModelClient(account, client=fake)
    with pytest.raises(BudgetExceeded):
        model.complete(MESSAGES, stage="agent.turn")
    assert not fake.calls
    assert account.status()["calls_sent"] == 0


def test_missing_usage_is_not_zero_cost(tmp_path):
    response = good_response()
    response.usage = None
    account, fake, model = setup(tmp_path, response=response)
    with pytest.raises(BudgetAccountingError, match="usage"):
        model.complete(MESSAGES, stage="agent.turn")
    assert account.status()["halted"]
    assert account.calls()[0]["known_cny"] is None


@pytest.mark.parametrize("content,finish_reason", [("", "stop"), (None, "stop"), ('{"partial":', "length")])
def test_unusable_output_still_records_valid_usage(tmp_path, content, finish_reason):
    account, _, model = setup(tmp_path, response=good_response(content, finish_reason))
    with pytest.raises(ModelResponseError):
        model.complete(MESSAGES, stage="agent.turn")
    assert Decimal(account.status()["new_known_cny"]) > 0
    assert not account.status()["halted"]


@pytest.mark.parametrize("base_url", ["http://api.deepseek.com", "https://platform.deepseek.com/api_keys",
    "https://secret@api.deepseek.com", "https://api.deepseek.com?key=secret", "https://api.deepseek.com/#key",
    "file:///C:/key", "https://api.deepseek.com:invalid", "https:// api.deepseek.com"])
def test_invalid_or_console_base_url_rejected(base_url):
    with pytest.raises(ValueError):
        validate_base_url(base_url)


def test_local_test_server_and_api_v1_supported():
    assert validate_base_url("http://127.0.0.1:9876/v1/") == "http://127.0.0.1:9876/v1"
    assert validate_base_url("https://api.deepseek.com/v1/") == "https://api.deepseek.com/v1"


def test_multibyte_message_reservation_counts_utf8_bytes():
    normalized, upper = normalize_messages([{"role": "user", "content": "金融研究" * 100}])
    assert upper > len(normalized[0]["content"].encode("utf-8"))


@pytest.mark.parametrize("bad", [[{"role": "user", "content": "x", "images": ["hidden"]}],
    [{"role": "user", "content": []}], [{"role": "tool", "content": "native calls unsupported"}],
    [{"role": "user", "content": "x" * 130_000}], []])
def test_hidden_nontext_or_oversized_inputs_rejected_without_dispatch(tmp_path, bad):
    account, fake, model = setup(tmp_path)
    with pytest.raises(ValueError):
        model.complete(bad, stage="agent.turn")
    assert not fake.calls
    assert account.status()["calls_sent"] == 0


def test_no_key_does_not_create_reservation_or_attempt_network(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    account = BudgetLedger(tmp_path / "budget.sqlite", ceiling_cny=1, carried_cny=0)
    model = ModelClient(account)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        model.complete(MESSAGES, stage="agent.turn")
    assert account.status()["calls_sent"] == 0


def test_sdk_is_lazy_and_has_automatic_retries_disabled(tmp_path, monkeypatch):
    import openai
    calls = []
    fake = FakeClient()

    def sdk_factory(**kwargs):
        calls.append(kwargs)
        return fake

    monkeypatch.setattr(openai, "OpenAI", sdk_factory)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ephemeral-test-key")
    account = BudgetLedger(tmp_path / "budget.sqlite", ceiling_cny=1, carried_cny=0)
    model = ModelClient(account)
    assert calls == []
    model.complete(MESSAGES, stage="agent.turn")
    assert calls[0]["max_retries"] == 0
    assert calls[0]["base_url"] == "https://api.deepseek.com"
