import csv
from dataclasses import replace
from io import StringIO
import json
import unittest
from unittest.mock import patch

from core.category import CategoryResult
from core.export import CSV_COLUMNS, export_profile_csv, export_profile_json, profile_rows, profile_to_dict
from core.identity import ProductIdentity
from core.mapping import CanonicalAttribute
from core.profile import ProfileInvariantError, build_final_profile, validate_final_profile
from core.schema import AttributeDefinition
from core.validation import CandidateFact, ValidatedProductProfile, validate_product_profile


IDENTITY = ProductIdentity(
    brand="Acme",
    raw_name="Acme X100",
    base_model="X100",
    commercial_model="X100",
    configuration={"storage": "128 GB", "ram": "8 GB"},
    confidence="high",
    evidence=["Synthetic identity evidence."],
)


def definition(
    name,
    *,
    expected=True,
    priority="high",
    value_type="text",
    scope="unknown",
    unit_family=None,
    schema_scope="category_specific",
):
    return AttributeDefinition(
        name,
        scope=schema_scope,
        expected=expected,
        priority=priority,
        value_type=value_type,
        attribute_scope=scope,
        unit_family=unit_family,
    )


def candidate(
    name,
    value,
    *,
    unit=None,
    source="https://official.example/X100",
    evidence=None,
    source_type="manufacturer",
    authority="verified",
    relation="exact_variant",
):
    attribute = CanonicalAttribute(
        canonical_name=name,
        value=value,
        unit=unit,
        raw_label=name.replace("_", " "),
        raw_value=f"{value} {unit or ''}".strip(),
        source_url=source,
        source_type=source_type,
        fact_evidence=evidence if evidence is not None else f"{name}: {value}",
        mapping_confidence="high",
        mapping_reason=f"exact_alias:{name}",
    )
    return CandidateFact(
        attribute,
        authority_status=authority,
        identity_relation=relation,
        source_type=source_type,
    )


def final_profile(schema, facts=(), *, category="cooktop", identity=IDENTITY):
    validated = validate_product_profile(
        identity,
        category,
        list(facts),
        schema=schema,
    )
    return build_final_profile(validated, schema=schema)


class FinalProfileContractTests(unittest.TestCase):
    def test_confirmed_fact_has_universal_row_contract(self):
        profile = final_profile([definition("power")], [candidate("power", "1000", unit="W")])
        row = profile_rows(profile)[0]
        self.assertEqual(
            {name: row[name] for name in ("Attribute", "Value", "Status", "Source", "Evidence")},
            {
                "Attribute": "power",
                "Value": "1000 W",
                "Status": "Confirmed",
                "Source": "https://official.example/X100",
                "Evidence": "power: 1000",
            },
        )

    def test_confirmed_without_source_is_rejected(self):
        profile = final_profile([definition("power")], [candidate("power", "1000")])
        broken = replace(profile, attributes=(replace(profile.attributes[0], source=None),))
        with self.assertRaisesRegex(ProfileInvariantError, "lacks a source"):
            validate_final_profile(broken)

    def test_confirmed_without_evidence_is_rejected(self):
        profile = final_profile([definition("power")], [candidate("power", "1000")])
        broken = replace(profile, attributes=(replace(profile.attributes[0], evidence=None),))
        with self.assertRaisesRegex(ProfileInvariantError, "lacks evidence"):
            export_profile_json(broken)

    def test_expected_unresolved_field_survives_json(self):
        profile = final_profile([definition("gross_weight", value_type="weight")])
        data = profile_to_dict(profile)
        item = data["attributes"][0]
        self.assertEqual(item["canonical_name"], "gross_weight")
        self.assertEqual(item["status"], "Unresolved")
        self.assertIsNone(item["value"])
        self.assertIn("gross_weight", data["unresolved"])

    def test_missing_expected_attribute_fails_contract_validation(self):
        profile = final_profile([definition("power")])
        broken = replace(profile, attributes=())
        with self.assertRaisesRegex(ProfileInvariantError, "expected attributes are missing"):
            validate_final_profile(broken, expected_names=["power"])

    def test_unknown_discovered_fact_survives_and_is_not_expected(self):
        profile = final_profile(
            [definition("power")],
            [candidate("peak_brightness", "1800 nit")],
        )
        item = profile.by_name["peak_brightness"]
        self.assertTrue(item.discovered)
        self.assertFalse(item.expected)
        data = profile_to_dict(profile)
        self.assertIn("peak_brightness", data["discovered"])

    def test_extended_schema_definition_remains_discovered(self):
        profile = final_profile(
            [definition(
                "custom_airflow_mode",
                expected=False,
                schema_scope="discovered",
            )],
            [candidate("custom_airflow_mode", "Quiet")],
        )
        item = profile.by_name["custom_airflow_mode"]
        self.assertTrue(item.discovered)
        self.assertFalse(item.expected)

    def test_product_and_package_dimensions_remain_separate(self):
        schema = [definition("product_dimensions"), definition("package_dimensions")]
        profile = final_profile(schema, [
            candidate("product_dimensions", "10 x 20 x 30", unit="cm"),
            candidate("package_dimensions", "15 x 25 x 35", unit="cm"),
        ])
        rows = profile_rows(profile)
        self.assertEqual([row["Attribute"] for row in rows], [
            "product_dimensions", "package_dimensions",
        ])

    def test_net_and_gross_weight_remain_separate(self):
        schema = [definition("net_weight"), definition("gross_weight")]
        profile = final_profile(schema, [
            candidate("net_weight", "4.7", unit="kg"),
            candidate("gross_weight", "6.1", unit="kg"),
        ])
        self.assertEqual([row["Attribute"] for row in profile_rows(profile)], [
            "net_weight", "gross_weight",
        ])

    def test_multiple_supporting_sources_survive_json(self):
        profile = final_profile([definition("power")], [
            candidate("power", "1000", unit="W", source="https://one.example/X100"),
            candidate("power", "1000.0", unit="W", source="https://two.example/X100"),
        ])
        item = profile_to_dict(profile)["attributes"][0]
        self.assertEqual(len(item["supporting_sources"]), 2)
        self.assertEqual(
            {source["source"] for source in item["supporting_sources"]},
            {"https://one.example/X100", "https://two.example/X100"},
        )

    def test_identity_is_separate_from_attributes(self):
        profile = final_profile([definition("power")])
        data = profile_to_dict(profile)
        self.assertEqual(data["identity"]["brand"], "Acme")
        self.assertEqual(data["identity"]["configuration"], {"ram": "8 GB", "storage": "128 GB"})
        self.assertNotIn("identity", data["attributes"][0])

    def test_unknown_category_is_valid(self):
        profile = final_profile([definition("model")], category="unknown")
        self.assertEqual(profile.category.category_id, "unknown")
        self.assertEqual(profile.category.category_name, "Unknown")

    def test_category_result_preserves_confidence_evidence_and_source(self):
        schema = [definition("power")]
        validated = validate_product_profile(
            IDENTITY, "cooktop", [candidate("power", "1000")], schema=schema,
        )
        category = CategoryResult(
            "cooktop",
            "Cooktop",
            "major_appliance",
            "high",
            ["Category evidence remains verbatim."],
            "mixed",
        )
        data = profile_to_dict(build_final_profile(
            validated, category_result=category, schema=schema,
        ))
        self.assertEqual(data["category"], {
            "category_id": "cooktop",
            "category_name": "Cooktop",
            "parent_category": "major_appliance",
            "confidence": "high",
            "evidence": ["Category evidence remains verbatim."],
            "source": "mixed",
        })

    def test_finalization_does_not_rerun_validation_or_targeted_search(self):
        schema = [definition("power")]
        validated = validate_product_profile(
            IDENTITY, "cooktop", [candidate("power", "1000")], schema=schema,
        )
        with (
            patch(
                "core.validation.validate_product_profile",
                side_effect=AssertionError("Stage 6 must not be rerun"),
            ),
            patch(
                "core.targeted_search.run_targeted_search",
                side_effect=AssertionError("targeted search must not run"),
            ),
        ):
            profile = build_final_profile(validated, schema=schema)
            self.assertEqual(profile.by_name["power"].status, "Confirmed")

    def test_public_status_strings_are_exact(self):
        conflict = final_profile([definition("power")], [
            candidate("power", "1000", source="https://one.example/X100"),
            candidate("power", "1100", source="https://two.example/X100"),
        ])
        unresolved = final_profile([definition("gross_weight")])
        confirmed = final_profile([definition("power")], [candidate("power", "1000")])
        self.assertEqual({
            confirmed.attributes[0].status,
            conflict.attributes[0].status,
            unresolved.attributes[0].status,
        }, {"Confirmed", "Conflict", "Unresolved"})

    def test_duplicate_canonical_attribute_is_rejected_not_overwritten(self):
        validated = validate_product_profile(
            IDENTITY, "cooktop", [candidate("power", "1000")], schema=[definition("power")],
        )
        duplicate = ValidatedProductProfile(
            validated.identity,
            validated.category,
            [validated.facts[0], validated.facts[0]],
        )
        with self.assertRaisesRegex(ProfileInvariantError, "duplicate canonical attribute"):
            build_final_profile(duplicate, schema=[definition("power")])

    def test_conflict_requires_distinct_value_groups(self):
        profile = final_profile([definition("power")], [
            candidate("power", "1000", source="https://one.example/X100"),
            candidate("power", "1100", source="https://two.example/X100"),
        ])
        item = profile.attributes[0]
        broken = replace(
            profile,
            attributes=(replace(
                item,
                conflicting_values=(replace(
                    item.conflicting_values[0],
                    value=item.supporting_sources[0].value,
                    unit=item.supporting_sources[0].unit,
                ),),
            ),),
        )
        with self.assertRaisesRegex(ProfileInvariantError, "lacks distinct alternatives"):
            validate_final_profile(broken)

    def test_metadata_counts_are_deterministic(self):
        profile = final_profile([definition("power"), definition("missing")], [
            candidate("power", "1000"),
        ])
        self.assertEqual(profile.metadata, {
            "contract_version": "1.0",
            "attribute_count": 2,
            "confirmed_count": 1,
            "conflict_count": 0,
            "unresolved_count": 1,
            "discovered_count": 0,
            "source_count": 1,
        })


class JsonExportTests(unittest.TestCase):
    def test_conflict_json_preserves_all_values_and_evidence(self):
        profile = final_profile([definition("power")], [
            candidate("power", "1000", unit="W", source="https://one.example/X100", evidence="Page says 1000 W"),
            candidate("power", "1100", unit="W", source="https://two.example/X100", evidence="Manual says 1100 W"),
        ])
        item = profile_to_dict(profile)["attributes"][0]
        alternatives = [*item["supporting_sources"], *item["conflicting_values"]]
        self.assertEqual({entry["value"] for entry in alternatives}, {"1000", "1100"})
        self.assertEqual(
            {entry["evidence"] for entry in alternatives},
            {"Page says 1000 W", "Manual says 1100 W"},
        )

    def test_unicode_survives_json_without_ascii_escaping(self):
        profile = final_profile([definition("lighting")], [candidate(
            "lighting",
            "Подсветка — Да",
            source="https://пример.example/товар",
            evidence="Освещение: Да",
        )])
        exported = export_profile_json(profile)
        self.assertIn("Подсветка", exported)
        self.assertIn("пример", exported)
        self.assertNotIn("\\u", exported)

    def test_json_contains_no_dataclass_repr(self):
        profile = final_profile([definition("power")], [candidate("power", "1000")])
        exported = export_profile_json(profile)
        for marker in ("CanonicalAttribute(", "CandidateFact(", "ProductIdentity("):
            self.assertNotIn(marker, exported)

    def test_json_round_trip_and_pretty_export_share_structure(self):
        profile = final_profile([definition("power")], [candidate("power", "1000")])
        compact = json.loads(export_profile_json(profile))
        pretty = json.loads(export_profile_json(profile, pretty=True))
        self.assertEqual(compact, pretty)
        self.assertEqual(compact, profile_to_dict(profile))

    def test_repeated_json_export_is_byte_stable(self):
        profile = final_profile([definition("power")], [candidate("power", "1000")])
        self.assertEqual(export_profile_json(profile), export_profile_json(profile))


class CsvExportTests(unittest.TestCase):
    def test_csv_starts_with_five_universal_columns(self):
        profile = final_profile([definition("power")])
        header = export_profile_csv(profile).splitlines()[0].split(",")
        self.assertEqual(tuple(header[:5]), CSV_COLUMNS[:5])

    def test_conflict_csv_emits_every_alternative(self):
        profile = final_profile([definition("power")], [
            candidate("power", "1000", unit="W", source="https://one.example/X100"),
            candidate("power", "1100", unit="W", source="https://two.example/X100"),
        ])
        rows = list(csv.DictReader(StringIO(export_profile_csv(profile))))
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["Value"] for row in rows}, {"1000 W", "1100 W"})
        self.assertEqual({row["EvidenceRole"] for row in rows}, {
            "supporting_candidate", "conflicting_candidate",
        })

    def test_csv_safely_quotes_commas_newlines_and_quotes(self):
        evidence = 'Line one, "quoted"\nLine two'
        profile = final_profile([definition("notes")], [candidate(
            "notes", "A, B", evidence=evidence,
        )])
        exported = export_profile_csv(profile)
        rows = list(csv.DictReader(StringIO(exported)))
        self.assertEqual(rows[0]["Value"], "A, B")
        self.assertEqual(rows[0]["Evidence"], evidence)
        self.assertIn('"Line one, ""quoted""', exported)

    def test_csv_row_order_is_schema_then_optional_then_discovered(self):
        schema = [
            definition("zeta"),
            definition("alpha"),
            definition("optional", expected=False),
        ]
        profile = final_profile(schema, [
            candidate("aaa_discovered", "A"),
            candidate("optional", "O"),
            candidate("alpha", "B"),
            candidate("zeta", "Z"),
        ])
        self.assertEqual([row["Attribute"] for row in profile_rows(profile)], [
            "zeta", "alpha", "optional", "aaa_discovered",
        ])

    def test_repeated_csv_export_is_byte_stable(self):
        profile = final_profile([definition("power")], [candidate("power", "1000")])
        self.assertEqual(export_profile_csv(profile), export_profile_csv(profile))


if __name__ == "__main__":
    unittest.main()
