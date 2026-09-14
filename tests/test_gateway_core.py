# -*- coding: utf-8 -*-
"""离线单测：不依赖任何外部服务/数据文件。
运行：python -m unittest discover -s tests"""
import asyncio
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core import breaker       # noqa: E402
from core import protocols     # noqa: E402


class TestBreaker(unittest.TestCase):
    def tearDown(self):
        # 清理测试产生的状态，避免影响其它用例
        try:
            breaker._states.clear()
        except Exception:
            pass

    def test_open_after_failures(self):
        breaker.configure("test-sup", threshold=2, reset=60)
        self.assertTrue(breaker.allow_request("test-sup"))
        breaker.on_failure("test-sup")
        breaker.on_failure("test-sup")
        st = breaker.get_state("test-sup")
        self.assertEqual(st["state"], "open")
        self.assertFalse(breaker.allow_request("test-sup"))

    def test_success_closes(self):
        breaker.configure("test-sup2", threshold=2, reset=60)
        breaker.on_failure("test-sup2")
        breaker.on_success("test-sup2")
        self.assertEqual(breaker.get_state("test-sup2")["state"], "closed")
        self.assertTrue(breaker.allow_request("test-sup2"))


class TestProtocols(unittest.TestCase):
    def test_stream_converter_registry(self):
        self.assertIsNone(protocols.make_stream_converter("openai", "m"))
        self.assertIsNotNone(protocols.make_stream_converter("anthropic", "m"))
        self.assertIsNotNone(protocols.make_stream_converter("gemini", "m"))

    def test_anthropic_roundtrip_stream(self):
        conv = protocols.make_stream_converter("anthropic", "m")
        ev = (b'event: content_block_start\n'
              b'data: {"type":"content_block_start","index":0,'
              b'"content_block":{"type":"text","text":""}}\n\n'
              b'event: content_block_delta\n'
              b'data: {"type":"content_block_delta","index":0,'
              b'"delta":{"type":"text_delta","text":"hi"}}\n\n')
        out = b"".join(conv.feed(ev))
        self.assertIn(b'"content": "hi"', out)
        tail = b"".join(conv.finalize())
        self.assertIn(b"[DONE]", tail)


class TestAsyncSSE(unittest.TestCase):
    def test_sse_event_split_across_chunks(self):
        # 验证跨网络块切开的事件能被重组（server._iter_sse_events 逻辑）
        import server as _srv
        async def _chunks():
            yield b'data: {"a":1'
            yield b"}\n\n"
            yield b"data: [DONE]\n\n"
        async def _run():
            got = []
            async for ev in _srv._iter_sse_events(_chunks()):
                got.append(ev)
            return got
        evs = asyncio.run(_run())
        self.assertEqual(len(evs), 2)
        self.assertTrue(any(b'"a":1}' in e for e in evs))


class TestShrinkForRetry(unittest.TestCase):
    def test_keeps_recent_and_drops_oldest(self):
        from core import token_compress as tc
        msgs = [{"role": "system", "content": "SYS"}] + [
            {"role": ("user" if i % 2 == 0 else "assistant"),
             "content": f"turn-{i}"} for i in range(20)]
        out = tc.shrink_for_retry(msgs)
        self.assertIsNotNone(out)
        self.assertEqual(out[0], {"role": "system", "content": "SYS"})
        self.assertLess(len(out), len(msgs))
        self.assertEqual(out[-1], msgs[-1])          # 最近一条保留
        self.assertNotIn({"role": "user", "content": "turn-0"}, out)

    def test_small_conv_returns_none(self):
        from core import token_compress as tc
        msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"}]
        self.assertIsNone(tc.shrink_for_retry(msgs))

    def test_context_error_detection(self):
        from core.proxy import UpstreamError
        import server as _srv
        self.assertTrue(_srv._is_context_error(
            UpstreamError(400, "This model's maximum context length is 8192 tokens")))
        self.assertFalse(_srv._is_context_error(UpstreamError(400, "model_unavailable")))
        self.assertFalse(_srv._is_context_error(UpstreamError(429, "rate limited")))

    def test_tool_result_truncated_before_dropping_turns(self):
        from core import token_compress as tc
        big = "x" * 20000
        msgs = [{"role": "system", "content": "SYS"}] + [
            {"role": "user", "content": f"q{i}"} for i in range(6)
        ] + [{"role": "tool", "content": big, "tool_call_id": "t0"}]
        out = tc.shrink_for_retry(msgs)
        self.assertIsNotNone(out)
        tool_msgs = [m for m in out if m.get("role") == "tool"]
        self.assertTrue(tool_msgs)
        self.assertLess(len(tool_msgs[0]["content"]), 5000)
        self.assertIn(tc._TRUNC_MARK, tool_msgs[0]["content"])
        self.assertEqual(out[-1], tool_msgs[0])      # 超大 tool 结果被原地截断保留

    def test_router_ctx_precheck_skips_small_window_when_alternatives_exist(self):
        from core import router
        def _sup(sid, mid, ctx):
            return {"id": sid, "enabled": True, "baseUrl": "http://x",
                    "keys": ["k"], "protocol": "openai",
                    "models": [{"id": mid, "enabled": True, "contextWindow": ctx}]}
        cfg = {"suppliers": [
            _sup("s-small", "m-small", 1000),
            _sup("s-big", "m-big", 100000),
        ], "pools": {}, "customRoutes": {}}
        plans, _ = router.select("auto", cfg, None, est_input_tokens=50000)
        mids = {(p["supplier_id"], p["model_id"]) for p in plans}
        self.assertNotIn(("s-small", "m-small"), mids)
        self.assertIn(("s-big", "m-big"), mids)

    def test_router_ctx_precheck_keeps_only_candidate(self):
        from core import router
        cfg = {"suppliers": [{
            "id": "solo", "enabled": True, "baseUrl": "http://x",
            "keys": ["k"], "protocol": "openai",
            "models": [{"id": "only-m", "enabled": True, "contextWindow": 1000}]}],
            "pools": {}, "customRoutes": {}}
        plans, _ = router.select("auto", cfg, None, est_input_tokens=50000)
        self.assertTrue(any(p["supplier_id"] == "solo" for p in plans))

    def test_plan_carries_all_keys_for_429_rotation(self):
        from core import router
        cfg = {"suppliers": [{
            "id": "multi", "enabled": True, "baseUrl": "http://x",
            "keys": ["k1", "k2", "k3"], "protocol": "openai",
            "models": [{"id": "m", "enabled": True}]}],
            "pools": {}, "customRoutes": {}}
        plans, _ = router.select("auto", cfg, None)
        p = plans[0]
        self.assertEqual(p["keys"], ["k1", "k2", "k3"])
        self.assertIn(p["key"], p["keys"])
        self.assertEqual(p["key_idx"], p["keys"].index(p["key"]))

    def test_cooldown_skips_429_key_in_round_robin(self):
        from core import router
        cfg = {"suppliers": [{
            "id": "cool", "enabled": True, "baseUrl": "http://x",
            "keys": ["b1", "b2", "b3"], "protocol": "openai",
            "models": [{"id": "m", "enabled": True}]}],
            "pools": {}, "customRoutes": {}}
        router.mark_key_429("cool", "b1", 600)   # 打入冷却
        picked = set()
        for _ in range(12):
            plans, _ = router.select("auto", cfg, None)
            picked.add(plans[0]["key"])
        self.assertNotIn("b1", picked)            # 冷却中的 key 不再被轮询选中
        self.assertEqual(picked, {"b2", "b3"})    # 其余两把正常轮换


if __name__ == "__main__":
    unittest.main()
