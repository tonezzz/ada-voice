import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend import bge_memory_shadow
from backend.memory_ops import memory_search
from tests.scenario_engine import FakeMddb, build_registry


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeClient:
    """Emulates the two-phase path: dense /vector-search (no content),
    then /get per candidate, then the reranker."""

    DOCS = {
        "collection-a": {
            "a": {"key": "a", "lang": "en",
                  "contentMd": "candidate a",
                  "meta": {"status": ["active"]}},
            "shared": {"key": "shared", "lang": "en",
                       "contentMd": "candidate shared",
                       "meta": {"status": ["active"]}},
        },
        "collection-b": {
            "b": {"key": "b", "lang": "en",
                  "contentMd": "candidate b",
                  "meta": {"status": ["active"]}},
        },
    }
    SCORES = {
        "collection-a": {"a": 0.8, "shared": 0.7},
        "collection-b": {"b": 0.9},
    }

    def __init__(self, **_kwargs):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, json, **_kwargs):
        self.calls.append((url, json))
        if url.endswith("/vector-search"):
            collection = json["collection"]
            return FakeResponse({
                "results": [
                    {"document": {k: v for k, v in doc.items()
                                  if k != "contentMd"},
                     "score": self.SCORES[collection][doc["key"]]}
                    for doc in self.DOCS[collection].values()
                ],
            })
        if url.endswith("/get"):
            doc = self.DOCS[json["collection"]][json["key"]]
            return FakeResponse(dict(doc))
        return FakeResponse({"scores": [0.2, 0.9, 0.1]})


class BgeMemoryShadowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "ADA_BGE_SHADOW_MDDB_URL": "http://mddb-embed/v1",
            "ADA_BGE_SHADOW_RERANK_URL": "http://reranker/rerank",
            "ADA_BGE_SHADOW_TIMEOUT_S": "2",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()

    async def test_shadow_merges_collections_and_returns_only_metrics(self):
        banks = [
            SimpleNamespace(name="bank-a", mddb_collection="collection-a",
                            writable=True, status_outcome=False, scope="shared"),
            SimpleNamespace(name="bank-b", mddb_collection="collection-b",
                            writable=True, status_outcome=False, scope="shared"),
        ]
        client = FakeClient()

        def client_factory(**kwargs):
            return client

        metrics = await bge_memory_shadow.run_shadow(
            query="private test query",
            banks=banks,
            baseline_ids=[("bank-b", "b"), ("bank-a", "a")],
            instance="tony",
            client_factory=client_factory,
        )

        self.assertEqual(metrics["status"], "ok")
        self.assertEqual(metrics["candidate_count"], 3)
        self.assertEqual(metrics["fetched_count"], 3)
        self.assertEqual(metrics["fetch_errors"], 0)
        self.assertEqual(metrics["reranked_count"], 3)
        self.assertEqual(metrics["overlap_at_5"], 2)
        self.assertIn("dense_ms", metrics)
        self.assertIn("fetch_ms", metrics)
        self.assertIn("rerank_ms", metrics)
        self.assertNotIn("private test query", repr(metrics))
        self.assertNotIn("candidate a", repr(metrics))
        # 2 dense + 3 content fetches + 1 rerank
        self.assertEqual(len(client.calls), 6)
        vector_calls = [c for c in client.calls
                        if c[0].endswith("/vector-search")]
        self.assertEqual(len(vector_calls), 2)
        for _url, payload in vector_calls:
            self.assertFalse(payload["includeContent"])
            self.assertEqual(payload["topK"], bge_memory_shadow.DENSE_TOP_K)
        get_calls = [c for c in client.calls if c[0].endswith("/get")]
        self.assertEqual(len(get_calls), 3)
        self.assertLessEqual(len(get_calls), bge_memory_shadow.FETCH_TOP_K)
        ranked_docs = client.calls[-1][1]["docs"]
        self.assertEqual([doc["text"] for doc in ranked_docs],
                         ["candidate b", "candidate a", "candidate shared"])

    def test_schedule_is_disabled_by_default(self):
        with patch.dict(os.environ, {"ADA_BGE_SHADOW_ENABLED": "0"}):
            accepted = bge_memory_shadow.schedule(
                "query", [SimpleNamespace(name="general")], [],
                instance="tony",
            )
        self.assertFalse(accepted)

    async def test_shadow_failure_does_not_change_gemini_result(self):
        mddb = FakeMddb()
        registry = build_registry("tony")
        bank = registry.bank("general")
        await mddb.add_document(
            bank.mddb_collection, "general/test", "en", "known result",
            {"status": ["active"], "kind": ["fact"]},
        )
        with patch.dict(os.environ, {
            "ADA_BGE_SHADOW_ENABLED": "1",
            "ADA_BGE_SHADOW_MDDB_URL": "http://mddb-embed/v1",
            "ADA_BGE_SHADOW_RERANK_URL": "http://reranker/rerank",
        }), patch(
            "backend.bge_memory_shadow.run_shadow",
            new_callable=AsyncMock,
            side_effect=RuntimeError("synthetic failure"),
        ) as shadow:
            result = await memory_search(
                mddb, registry, "general", "known result", limit=5)
            await asyncio.sleep(0)

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["hits"][0]["key"], "general/test")
        self.assertFalse(result["degraded"])
        shadow.assert_awaited_once()

    async def test_cancelled_baseline_still_schedules_shadow(self):
        """A tool-level timeout (paid-path outage) must still shadow —
        that is exactly when BGE-side health metrics matter."""
        mddb = FakeMddb()
        registry = build_registry("tony")
        scheduled = []

        async def hang(*_args, **_kwargs):
            await asyncio.sleep(60)

        with patch("backend.memory_ops._timed_bank_docs", hang), patch(
            "backend.memory_ops._schedule_bge_shadow",
            side_effect=lambda *a, **k: scheduled.append((a, k)),
        ):
            task = asyncio.create_task(
                memory_search(mddb, registry, "all", "where is the gate"))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(len(scheduled), 1)
        self.assertTrue(scheduled[0][1].get("degraded"))
        self.assertEqual(scheduled[0][0][2], [])


if __name__ == "__main__":
    unittest.main()
