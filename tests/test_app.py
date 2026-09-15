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

    def test_json_mode_can_include_quality_assessment_without_breaking_default_shape(self):
        profile = final_profile(
            [definition("power")],
            [candidate("power", "1000", unit="W")],
        )
        output = StringIO()
        with (
            patch(
                "app.run_product_workflow",
                return_value=SimpleNamespace(final_profile=profile),
            ),
            redirect_stdout(output),
        ):
            status = app.main([
                "Acme", "X100", "ABC-12345",
                "--format", "json",
                "--no-targeted-search",
                "--include-quality",
            ])
        self.assertEqual(status, 0)
        data = json.loads(output.getvalue())
        self.assertIn("quality", data)
        self.assertIn(data["quality"]["status"], (
            "verified", "partial", "insufficient", "conflicted",
        ))
        self.assertEqual(data["attributes"][0]["status"], "Confirmed")

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

    def test_invalid_brand_is_rejected_before_the_workflow_runs(self):
        """Stage 10: request validation happens in the service, not the pipeline."""
        stdout = StringIO()
        stderr = StringIO()
        with (
            patch("app.run_product_workflow") as run,
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            status = app.main(["", "X100"])
        self.assertEqual(status, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Workflow failed:", stderr.getvalue())
        run.assert_not_called()

    def test_table_mode_still_works_through_the_service_boundary(self):
        profile = final_profile(
            [definition("power")],
            [candidate("power", "1000", unit="W")],
        )
        output = StringIO()
        with (
            patch(
                "app.run_product_workflow",
                return_value=SimpleNamespace(
                    final_profile=profile,
                    discovery=SimpleNamespace(search_status="success"),
                ),
            ),
            redirect_stdout(output),
        ):
            status = app.main(["Acme", "X100"])
        self.assertEqual(status, 0)
        lines = output.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("Product: Acme"))
        self.assertTrue(any(line.startswith("Quality:") for line in lines))
        self.assertIn("power", output.getvalue())

    def test_csv_mode_still_works_through_the_service_boundary(self):
        profile = final_profile(
            [definition("power")],
            [candidate("power", "1000", unit="W")],
        )
        output = StringIO()
        with (
            patch(
                "app.run_product_workflow",
                return_value=SimpleNamespace(final_profile=profile),
            ),
            redirect_stdout(output),
        ):
            status = app.main(["Acme", "X100", "--format", "csv"])
        self.assertEqual(status, 0)
        self.assertIn("power", output.getvalue())
        self.assertIn("Confirmed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
