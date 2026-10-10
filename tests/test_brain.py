import unittest
from types import SimpleNamespace as NS

from agent import brain


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def verdict_block(**inp):
    return NS(type="tool_use", name="submit_verdict", input=inp)


class Review(unittest.TestCase):
    def tearDown(self):
        brain._client = None

    def test_returns_verdict_and_drops_unknown_symbols(self):
        brain._client = FakeClient([NS(stop_reason="tool_use", content=[
            NS(type="text", text="checking news"),
            verdict_block(risk="on", allow=["SOL/USD", "PEPE/USD"], note="clean trend")])])
        v = brain.review({"symbols": []}, ["SOL/USD", "BTC/USD"])
        self.assertEqual(v, {"risk": "on", "allow": ["SOL/USD"], "note": "clean trend"})
        self.assertEqual(brain._client.calls[0]["model"], "claude-opus-5-5")

    def test_resumes_after_pause_turn(self):
        brain._client = FakeClient([
            NS(stop_reason="pause_turn", content=[NS(type="server_tool_use", name="web_search")]),
            NS(stop_reason="tool_use", content=[verdict_block(risk="off", allow=[], note="Fed at 2pm")])])
        self.assertEqual(brain.review({}, ["BTC/USD"])["risk"], "off")
        self.assertEqual(len(brain._client.calls), 2)

    def test_refusal_raises(self):
        brain._client = FakeClient([NS(stop_reason="refusal", content=[])])
        with self.assertRaises(RuntimeError):
            brain.review({}, [])


if __name__ == "__main__":
    unittest.main()
