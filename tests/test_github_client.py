import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from src.github_client import GitHubClient


class GitHubClientTests(unittest.TestCase):
    def test_paginate_honors_smaller_page_size(self):
        client = GitHubClient(retries=0)
        requested = []
        pages = {
            "/items?per_page=2&page=1": {"items": [{"id": 1}, {"id": 2}]},
            "/items?per_page=2&page=2": {"items": [{"id": 3}]},
        }

        def get(path):
            requested.append(path)
            return pages[path]

        client.get = get
        self.assertEqual(
            [1, 2, 3],
            [item["id"] for item in client.paginate("/items", "items", per_page=2)],
        )
        self.assertEqual(list(pages), requested)

    def test_paginate_rejects_invalid_page_size(self):
        client = GitHubClient(retries=0)
        with self.assertRaisesRegex(ValueError, "between 1 and 100"):
            list(client.paginate("/items", per_page=0))

    def test_shared_client_serializes_network_requests(self):
        client = GitHubClient(token="token", retries=0)
        state = {"active": 0, "maximum": 0}
        state_lock = threading.Lock()

        class Response:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"{}"

        def urlopen(*args, **kwargs):
            with state_lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            time.sleep(0.02)
            with state_lock:
                state["active"] -= 1
            return Response()

        with patch("urllib.request.urlopen", side_effect=urlopen):
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(client.get, ["/one", "/two", "/three", "/four"]))

        self.assertEqual(1, state["maximum"])


if __name__ == "__main__":
    unittest.main()
