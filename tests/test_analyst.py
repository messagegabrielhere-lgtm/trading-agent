import unittest
from types import SimpleNamespace as NS

from agent import analyst


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def answer(tool, **inp):
    return NS(stop_reason="tool_use", content=[NS(type="tool_use", name=tool, input=inp)])


class Analyst(unittest.TestCase):
    def tearDown(self):
        analyst._client = None

    def test_thesis_check_uses_web_search_and_returns_verdict(self):
        analyst._client = FakeClient([answer("submit_thesis_check", thesis_broken=False, severity="none",
                                             summary="Nothing new.", evidence="")])
        v = analyst.thesis_check("KO", "moat", [])
        self.assertFalse(v["thesis_broken"])
        tools = [t["name"] for t in analyst._client.calls[0]["tools"]]
        self.assertEqual(tools, ["web_search", "submit_thesis_check"])

    def test_bear_kill_means_warren_never_sees_it(self):
        analyst._client = FakeClient([answer("submit_bear_case", kill=True, strongest_points=["fraud probe"],
                                             summary="SEC fraud probe.")])
        d = analyst.debate("XYZ", "XYZ Corp", {"buy_below": 10}, 8)
        self.assertFalse(d["buy"])
        self.assertIn("Bear killed it", d["reason"])
        self.assertEqual(len(analyst._client.calls), 1)

    def test_warren_decides_after_a_failed_bear(self):
        analyst._client = FakeClient([
            answer("submit_bear_case", kill=False, strongest_points=["slow growth"], summary="Meh."),
            answer("submit_decision", buy=True, reason="Durable brand, 35% under value.")])
        d = analyst.debate("KO", "Coca-Cola", {"buy_below": 70}, 60)
        self.assertTrue(d["buy"])
        self.assertEqual(d["bear"]["summary"], "Meh.")
        warren_call = analyst._client.calls[1]
        self.assertEqual([t["name"] for t in warren_call["tools"]], ["submit_decision"])
        self.assertIn("Meh.", warren_call["messages"][0]["content"])

    def test_resumes_after_pause_turn_and_raises_on_refusal(self):
        analyst._client = FakeClient([
            NS(stop_reason="pause_turn", content=[NS(type="server_tool_use", name="web_search")]),
            answer("submit_bear_case", kill=False, strongest_points=[], summary="ok")])
        self.assertFalse(analyst.bear_case("KO", "Coca-Cola", {}, 1)["kill"])
        analyst._client = FakeClient([NS(stop_reason="refusal", content=[])])
        with self.assertRaises(RuntimeError):
            analyst.bear_case("KO", "Coca-Cola", {}, 1)


if __name__ == "__main__":
    unittest.main()
