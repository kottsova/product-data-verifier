import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import app
from tests.test_profile_export import candidate, definition, final_profile


class ApplicationCliTests(unittest.TestCase):
    def test_no_arguments_keeps_smoke_test_output(self):
        output = StringIO()
        with redirect_stdout(output):
            status = app.main([])
        self.assertEqual(status, 0)
        self.assertEqual(output.getvalue(), "Product Data Verifier\n")

    def test_json_mode_serializes_the_workflow_final_profile(self):
        profile = final_profile(
            [definition("power")],
            [candidate("power", "1000", unit="W")],
        )
        output = StringIO()
        with (
            patch(
                "app.run_product_workflow",
                return_value=SimpleNamespace(final_profile=profile),
            ) as run,
            redirect_stdout(output),
        ):
            status = app.main([
                "Acme", "X100", "ABC-12345",
                "--format", "json",
                "--no-targeted-search",
            ])
        self.assertEqual(status, 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["attributes"][0]["status"], "Confirmed")
        request = run.call_args.args[0]
        self.assertEqual(request.identity_evidence[0].field, "manufacturer_article")
        self.assertFalse(request.targeted_search_enabled)

    def test_workflow_error_returns_nonzero_without_traceback(self):
        stdout = StringIO()
        stderr = StringIO()
        with (
            patch("app.run_product_workflow", side_effect=RuntimeError("offline")),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            status = app.main(["Acme", "X100"])
        self.assertEqual(status, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "Workflow failed: offline\n")


if __name__ == "__main__":
    unittest.main()
