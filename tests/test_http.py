import json, os, sys, tempfile, threading, unittest, urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from http.server import ThreadingHTTPServer
import app
from database import CollationDB


def req(port, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class EmendationHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        cls.db = CollationDB(cls.path); cls.db.seed_demo()
        app.Handler.db = cls.db
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.db.close(); os.unlink(cls.path)

    def test_full_emendation_review_cycle(self):
        # 审阅人(3)不能自审：演示校记由编辑(2)登记
        status, payload = req(self.port, "POST", "/api/emendations/approve", {"emendation_id": 1, "reviewer_id": 2})
        self.assertEqual(400, status); self.assertIn("自己", payload["error"])
        # 审阅人驳回必须写原因
        status, payload = req(self.port, "POST", "/api/emendations/reject", {"emendation_id": 1, "reviewer_id": 3})
        self.assertEqual(400, status); self.assertIn("驳回", payload["error"])
        # 通过后成为当前释文
        status, payload = req(self.port, "POST", "/api/emendations/approve", {"emendation_id": 1, "reviewer_id": 3})
        self.assertEqual(200, status); self.assertTrue(payload["ok"])
        status, payload = req(self.port, "GET", "/api/works/1/collation?user_id=3")
        reading = payload["passages"][0]["alignments"][1]["reading"]
        self.assertEqual("春水东流，故[不可辨]。", reading)
        self.assertEqual(0, payload["unresolved_count"])
        # 缺口字位（甲本不存在缺口；乙本仅8单位）越界与登记校验
        status, payload = req(self.port, "POST", "/api/emendations",
                              {"passage_id": 1, "witness_id": 2, "position": 99, "proposed_char": "也",
                               "basis": "越界补字", "user_id": 2, "expected_revision": 1})
        self.assertEqual(400, status); self.assertIn("超出范围", payload["error"])


if __name__ == "__main__": unittest.main()
