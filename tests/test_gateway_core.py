# -*- coding: utf-8 -*-
"""离线单测：不依赖任何外部服务/数据文件。
运行：python -m unittest discover -s tests"""
import asyncio
import json
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

    # ---------- 非流式参数映射（Bug-05 回归网） ----------

    def _openai_body(self, **overrides):
        body = {
            "model": "gpt-x",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 512,
            "temperature": 0.3,
            "top_p": 0.9,
            "stop": ["END"],
            "stream": False,
        }
        body.update(overrides)
        return body

    def test_anthropic_inbound_param_mapping(self):
        a_body = {
            "model": "claude-x",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 700,
            "temperature": 0.8,
            "top_p": 0.5,
            "stop_sequences": ["STOP"],
            "tools": [{"name": "t1"}],
            "tool_choice": {"type": "auto"},
        }
        out, _ = protocols.inbound_to_openai(a_body, "anthropic")
        self.assertEqual(out["max_tokens"], 700)
        self.assertEqual(out["temperature"], 0.8)
        self.assertEqual(out["top_p"], 0.5)
        self.assertEqual(out["stop"], ["STOP"])          # stop_sequences → stop
        self.assertEqual(out["tools"], a_body["tools"])
        self.assertEqual(out["tool_choice"], {"type": "auto"})

    def test_anthropic_inbound_system_and_default_max_tokens(self):
        out, _ = protocols.inbound_to_openai(
            {"model": "c", "system": "SYS",
             "messages": [{"role": "user", "content": "hi"}]}, "anthropic")
        self.assertEqual(out["messages"][0], {"role": "system", "content": "SYS"})
        # 未显式传 max_tokens 时注入默认上限，避免上游默认小值截断
        self.assertEqual(out["max_tokens"], protocols._DEFAULT_MAX_TOKENS)

    def test_openai_to_anthropic_param_mapping(self):
        out, _ = protocols.openai_to_outbound(self._openai_body(), "anthropic")
        self.assertEqual(out["max_tokens"], 512)
        self.assertEqual(out["temperature"], 0.3)
        self.assertEqual(out["top_p"], 0.9)
        self.assertEqual(out["stop_sequences"], ["END"])   # stop → stop_sequences
        self.assertNotIn("stop", out)
        self.assertNotIn("system", out)

    def test_anthropic_roundtrip_params_survive(self):
        toward, _ = protocols.openai_to_outbound(self._openai_body(), "anthropic")
        back, _ = protocols.inbound_to_openai(toward, "anthropic")
        self.assertEqual(back["max_tokens"], 512)
        self.assertEqual(back["temperature"], 0.3)
        self.assertEqual(back["top_p"], 0.9)
        self.assertEqual(back["stop"], ["END"])
        self.assertEqual(back["model"], "gpt-x")

    def test_temperature_zero_not_dropped(self):
        ob = self._openai_body(temperature=0, top_p=1)
        toward, _ = protocols.openai_to_outbound(ob, "anthropic")
        self.assertIn("temperature", toward)
        self.assertEqual(toward["temperature"], 0)
        back, _ = protocols.inbound_to_openai(toward, "anthropic")
        self.assertEqual(back["temperature"], 0)

    def test_gemini_inbound_param_mapping(self):
        g_body = {
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "systemInstruction": {"parts": [{"text": "SYS"}]},
            "generationConfig": {"maxOutputTokens": 888, "temperature": 0.4,
                                 "topP": 0.2, "stopSequences": ["STOP"]},
            "stream": False,
        }
        out, _ = protocols.inbound_to_openai(g_body, "gemini")
        self.assertEqual(out["messages"][0], {"role": "system", "content": "SYS"})
        self.assertEqual(out["max_tokens"], 888)
        self.assertEqual(out["temperature"], 0.4)
        self.assertEqual(out["top_p"], 0.2)
        self.assertEqual(out["stop"], ["STOP"])            # stopSequences → stop

    def test_openai_to_gemini_param_mapping(self):
        out, _ = protocols.openai_to_outbound(self._openai_body(), "gemini")
        gc = out["generationConfig"]
        self.assertEqual(gc["maxOutputTokens"], 512)
        self.assertEqual(gc["temperature"], 0.3)
        self.assertEqual(gc["topP"], 0.9)
        self.assertEqual(gc["stopSequences"], ["END"])
        self.assertEqual(out["stream"], False)

    def test_anthropic_resp_to_openai(self):
        a_resp = {
            "id": "msg_1", "model": "claude-x",
            "content": [{"type": "text", "text": "hello"},
                        {"type": "tool_use", "id": "tu1",
                         "name": "f", "input": {"a": 1}}],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        out = protocols.response_to_openai(a_resp, "anthropic", "claude-x")
        ch = out["choices"][0]["message"]
        self.assertIn("hello", ch["content"])
        self.assertEqual(ch["tool_calls"][0]["function"]["name"], "f")
        self.assertEqual(json.loads(ch["tool_calls"][0]["function"]["arguments"]), {"a": 1})
        self.assertEqual(out["usage"]["prompt_tokens"], 100)
        self.assertEqual(out["usage"]["completion_tokens"], 50)
        self.assertEqual(out["usage"]["total_tokens"], 150)

    def test_gemini_resp_to_openai(self):
        g_resp = {
            "candidates": [{
                "content": {"role": "model", "parts": [
                    {"text": "hi"},
                    {"functionCall": {"name": "f", "args": {"b": 2}, "id": "t2"}}]}}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
        }
        out = protocols.response_to_openai(g_resp, "gemini", "g-x")
        ch = out["choices"][0]["message"]
        self.assertEqual(ch["content"], "hi")
        self.assertEqual(ch["tool_calls"][0]["function"]["name"], "f")
        self.assertEqual(out["usage"]["prompt_tokens"], 10)
        self.assertEqual(out["usage"]["completion_tokens"], 5)

    def test_openai_resp_to_anthropic(self):
        o_resp = {"id": "c1", "model": "gpt",
                  "choices": [{"index": 0,
                               "message": {"role": "assistant", "content": "hi",
                                           "tool_calls": [{"id": "t1", "type": "function",
                                                           "function": {"name": "f",
                                                                       "arguments": '{"x":1}'}}]},
                               "finish_reason": "tool_calls"}],
                  "usage": {"prompt_tokens": 20, "completion_tokens": 7}}
        out = protocols.openai_resp_to_anthropic(o_resp, "gpt")
        self.assertEqual(out["type"], "message")
        self.assertEqual(out["stop_reason"], "tool_use")
        self.assertEqual(out["content"][1]["type"], "tool_use")
        self.assertEqual(out["content"][1]["name"], "f")
        self.assertEqual(out["content"][1]["input"], {"x": 1})
        self.assertEqual(out["usage"]["output_tokens"], 7)

    def test_openai_resp_to_gemini(self):
        o_resp = {"id": "c2", "model": "gpt",
                  "choices": [{"index": 0,
                               "message": {"role": "assistant", "content": "hi"},
                               "finish_reason": "length"}],
                  "usage": {"prompt_tokens": 3, "completion_tokens": 9}}
        out = protocols.openai_resp_to_gemini(o_resp, "gpt")
        self.assertEqual(out["candidates"][0]["finishReason"], "MAX_TOKENS")
        self.assertEqual(out["candidates"][0]["content"]["parts"][0]["text"], "hi")
        self.assertEqual(out["usageMetadata"]["candidatesTokenCount"], 9)


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
