import unittest
from types import SimpleNamespace

from side_effects.github.errors import GitHubAdapterError
from side_effects.github.models import GITHUB_API_BASE
from side_effects.github.transport import GitHubHttpTransport, map_github_status


class RecordingClient:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or SimpleNamespace(
            status_code=200,
            headers={},
            json=lambda: [{"name": "bug"}],
        )

    async def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "json": json,
                "timeout": timeout,
            }
        )
        return self.response


class GitHubWriteTransportTests(unittest.IsolatedAsyncioTestCase):

    def _transport(self, client, token="ghs_secret_token_value"):
        return GitHubHttpTransport(token, timeout_seconds=12.5, client=client)

    async def test_a_auth_header_generated_internally(self):
        client = RecordingClient()
        transport = self._transport(client)
        await transport.get_issue_labels("octo", "hello", 1)
        headers = client.calls[0]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer ghs_secret_token_value")
        self.assertEqual(headers["Accept"], "application/vnd.github+json")

    async def test_b_token_never_exposed_in_error(self):
        client = RecordingClient(
            SimpleNamespace(status_code=401, headers={}, json=lambda: {"message": "bad"})
        )
        transport = self._transport(client)
        with self.assertRaises(GitHubAdapterError) as caught:
            await transport.get_issue_labels("octo", "hello", 1)
        self.assertNotIn("ghs_secret_token_value", str(caught.exception))
        self.assertNotIn("ghs_secret_token_value", repr(transport))
        self.assertEqual(caught.exception.error_code, "github_authentication_failed")

    async def test_c_fixed_api_github_com_base(self):
        client = RecordingClient()
        transport = self._transport(client)
        await transport.get_issue_labels("octo", "hello", 1)
        self.assertTrue(client.calls[0]["url"].startswith(GITHUB_API_BASE))
        self.assertNotIn("evil", client.calls[0]["url"])

    async def test_d_timeout_set(self):
        client = RecordingClient()
        transport = self._transport(client)
        await transport.get_issue_labels("octo", "hello", 1)
        self.assertEqual(client.calls[0]["timeout"], 12.5)

    async def test_e_get_labels_endpoint(self):
        client = RecordingClient()
        transport = self._transport(client)
        await transport.get_issue_labels("octo", "hello", 7)
        self.assertEqual(client.calls[0]["method"], "GET")
        self.assertEqual(
            client.calls[0]["url"],
            "https://api.github.com/repos/octo/hello/issues/7/labels",
        )

    async def test_f_add_label_endpoint(self):
        client = RecordingClient(
            SimpleNamespace(status_code=200, headers={}, json=lambda: [])
        )
        transport = self._transport(client)
        await transport.add_label("octo", "hello", 7, "bug")
        self.assertEqual(client.calls[0]["method"], "POST")
        self.assertEqual(
            client.calls[0]["url"],
            "https://api.github.com/repos/octo/hello/issues/7/labels",
        )
        self.assertEqual(client.calls[0]["json"], {"labels": ["bug"]})

    async def test_g_remove_label_endpoint(self):
        client = RecordingClient(
            SimpleNamespace(status_code=204, headers={}, json=lambda: None)
        )
        transport = self._transport(client)
        await transport.remove_label("octo", "hello", 7, "bug")
        self.assertEqual(client.calls[0]["method"], "DELETE")
        self.assertEqual(
            client.calls[0]["url"],
            "https://api.github.com/repos/octo/hello/issues/7/labels/bug",
        )

    def test_h_unexpected_endpoint_cannot_be_called(self):
        self.assertFalse(hasattr(GitHubHttpTransport, "request"))
        self.assertFalse(hasattr(GitHubHttpTransport, "patch_issue"))
        self.assertFalse(hasattr(GitHubHttpTransport, "update_issue"))
        transport = GitHubHttpTransport("token")
        with self.assertRaises(GitHubAdapterError):
            transport._url("https://evil.example/x")

    def test_status_mapping(self):
        self.assertEqual(map_github_status(403), "github_permission_denied")
        self.assertEqual(map_github_status(403, remaining="0"), "github_rate_limited")
        self.assertEqual(map_github_status(429), "github_rate_limited")
        self.assertEqual(map_github_status(409), "github_request_conflict")
        self.assertEqual(map_github_status(422), "github_validation_error")
        self.assertEqual(map_github_status(500), "github_temporary_error")
        self.assertEqual(map_github_status(404), "github_resource_not_found_or_inaccessible")
