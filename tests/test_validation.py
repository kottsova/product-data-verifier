import unittest
from unittest.mock import patch

from core.discovery import DiscoveryIssue
from core.extract import RawAttribute
from core.gaps import analyze_gaps
from core.identity import ProductIdentity
from core.mapping import CanonicalAttribute, MappingResult, map_attributes
from core.schema import AttributeDefinition
from core.targeted_search import (
    TargetedFieldResult,
    TargetedQueryResult,
    TargetedSearchConfig,
    TargetedSearchPlan,
    TargetedSearchResult,
)
from core.validation import CandidateFact, validate_product_profile


IDENTITY = ProductIdentity(
    brand="Acme",
    raw_name="Acme X100",
    base_model="X100",
    commercial_model="X100",
    confidence="high",
)


def definition(name, *, value_type="text", unit_family=None, scope="unknown", expected=True):
    return AttributeDefinition(
        name,
        value_type=value_type,
        unit_family=unit_family,
        attribute_scope=scope,
        priority="high",
        expected=expected,
    )


def attribute(
    name,
    value,
    *,
    unit=None,
    source="https://source.example/product/X100",
    source_type="manufacturer",
    evidence=None,
    confidence="high",
):
    return CanonicalAttribute(
        canonical_name=name,
        value=value,
        unit=unit,
        raw_label=name.replace("_", " "),
        raw_value=f"{value} {unit or ''}".strip(),
        source_url=source,
        source_type=source_type,
        fact_evidence=evidence if evidence is not None else f"{name}: {value} {unit or ''}".strip(),
        mapping_confidence=confidence,
        mapping_reason=f"explicit_scope:{name}",
    )


def candidate(
    name,
    value,
    *,
    unit=None,
    source="https://source.example/product/X100",
    source_type="manufacturer",
    authority="verified",
    relation="exact_variant",
    evidence=None,
    confidence="high",
    source_status="success",
):
    item = attribute(
        name,
        value,
        unit=unit,
        source=source,
        source_type=source_type,
        evidence=evidence,
        confidence=confidence,
    )
    return CandidateFact(
        item,
        authority_status=authority,
        identity_relation=relation,
        source_status=source_status,
        source_type=source_type,
    )


def validate(name, facts, **definition_options):
    item_definition = definition(name, **definition_options)
    return validate_product_profile(
        IDENTITY,
        "cooktop",
        facts,
        schema=[item_definition],
    ).by_name[name]


def raw(name, value):
    return RawAttribute(
        name=name,
        value=value,
        unit=None,
        source_url="https://source.example/product/X100",
        source_type="manufacturer",
        evidence=f"{name}: {value}",
        extraction_method="html_table",
        confidence="high",
        raw_value=value,
        attribute_kind="product",
    )


class ConfirmationTests(unittest.TestCase):
    def test_one_official_exact_model_explicit_fact_is_confirmed(self):
        fact = validate("power", [candidate("power", "1000", unit="W")])
        self.assertEqual(fact.status, "Confirmed")
        self.assertEqual(fact.resolution_reason, "official_exact_model_explicit")
        self.assertTrue(fact.source)
        self.assertTrue(fact.evidence)

    def test_equivalent_official_facts_merge_with_support(self):
        facts = [
            candidate("battery_capacity", "5000", unit="mAh", source="https://one.example/X100"),
            candidate("battery_capacity", "5000.0", unit="mAh", source="https://two.example/X100"),
        ]
        result = validate("battery_capacity", facts)
        self.assertEqual(result.status, "Confirmed")
        self.assertEqual(result.resolution_reason, "equivalent_multi_source_evidence")
        self.assertEqual(len(result.supporting_facts), 2)

    def test_manufacturer_outweighs_retailer_disagreement(self):
        facts = [
            candidate("power", "1000", unit="W"),
            candidate(
                "power", "900", unit="W", source="https://shop.example/X100",
                source_type="retailer", authority="unknown",
            ),
        ]
        result = validate("power", facts)
        self.assertEqual(result.status, "Confirmed")
        self.assertEqual(result.value, "1000")
        self.assertEqual(result.resolution_reason, "higher_authority_source")
        self.assertEqual(len(result.conflicting_facts), 1)

    def test_three_retailers_do_not_outvote_manufacturer(self):
        facts = [candidate("power", "1000", unit="W")]
        facts.extend(
            candidate(
                "power", "900", unit="W", source=f"https://shop{index}.example/X100",
                source_type="retailer", authority="unknown",
            )
            for index in range(3)
        )
        result = validate("power", facts)
        self.assertEqual(result.status, "Confirmed")
        self.assertEqual(result.value, "1000")
        self.assertEqual(len(result.conflicting_facts), 3)

    def test_same_base_model_can_confirm_model_invariant_field(self):
        result = validate("processor", [candidate(
            "processor", "Chip X", relation="same_base_model",
        )])
        self.assertEqual(result.status, "Confirmed")

    def test_specialized_exact_source_can_confirm_without_higher_conflict(self):
        result = validate("power", [candidate(
            "power", "1000", unit="W", source_type="specialized_reference",
            authority="unknown",
        )])
        self.assertEqual(result.status, "Confirmed")
        self.assertEqual(result.confidence, "medium")
        self.assertEqual(result.resolution_reason, "specialized_exact_model_explicit")

    def test_targeted_exact_fact_participates_with_preserved_metadata(self):
        item_definition = definition("power")
        mapping = MappingResult()
        analysis = analyze_gaps(
            "cooktop", mapping, identity=IDENTITY, schema=[item_definition],
        )
        gap = analysis.by_name["power"]
        mapped = attribute("power", "1000", unit="W")
        source = {
            "source_url": mapped.source_url,
            "final_url": mapped.source_url,
            "status": "success",
            "source_type": "manufacturer",
            "authority_status": "verified",
            "identity_relation": "exact_variant",
            "discovery_metadata": {
                "source_type": "manufacturer",
                "authority_status": "verified",
                "identity_relation": "exact_variant",
            },
        }
        query_result = TargetedQueryResult(
            "power",
            "Acme X100 power",
            "power",
            fetched_sources=(source,),
            mapped_candidate_facts=(mapped,),
            useful_evidence_found=True,
        )
        targeted = TargetedSearchResult(
            TargetedSearchPlan("cooktop"),
            [TargetedFieldResult(
                gap, (query_result,), "success", True, "strong_official_evidence",
            )],
        )
        profile = validate_product_profile(
            IDENTITY,
            "cooktop",
            mapping,
            targeted_search=targeted,
            gap_analysis=analysis,
            schema=[item_definition],
        )
        fact = profile.by_name["power"]
        self.assertEqual(fact.status, "Confirmed")
        self.assertEqual(fact.supporting_facts[0].origin, "targeted_search")
        self.assertEqual(fact.authority_status, "verified")


class ConflictTests(unittest.TestCase):
    def test_equal_official_sources_disagree_is_conflict(self):
        result = validate("power", [
            candidate("power", "1000", unit="W", source="https://one.example/X100"),
            candidate("power", "1100", unit="W", source="https://two.example/X100"),
        ])
        self.assertEqual(result.status, "Conflict")
        self.assertEqual(result.resolution_reason, "conflicting_equal_authority")
        self.assertIsNone(result.value)

    def test_multiple_values_from_equal_strong_sources_conflict(self):
        result = validate("display_type", [
            candidate("display_type", "OLED", source="https://one.example/X100"),
            candidate("display_type", "LCD", source="https://two.example/X100"),
            candidate("display_type", "AMOLED", source="https://three.example/X100"),
        ])
        self.assertEqual(result.status, "Conflict")
        self.assertEqual(len(result.conflicting_facts), 2)

    def test_net_and_gross_weight_are_separate(self):
        schema = [
            definition("net_weight", value_type="weight", unit_family="mass"),
            definition("gross_weight", value_type="weight", unit_family="mass"),
        ]
        profile = validate_product_profile(IDENTITY, "cooktop", [
            candidate("net_weight", "4.7", unit="kg"),
            candidate("gross_weight", "6.1", unit="kg"),
        ], schema=schema)
        self.assertEqual(profile.by_name["net_weight"].status, "Confirmed")
        self.assertEqual(profile.by_name["gross_weight"].status, "Confirmed")
        self.assertEqual(profile.conflicts, [])

    def test_product_and_package_dimensions_are_separate(self):
        schema = [
            definition("product_dimensions", value_type="dimension", unit_family="length"),
            definition("package_dimensions", value_type="dimension", unit_family="length", scope="market_level"),
        ]
        profile = validate_product_profile(IDENTITY, "cooktop", [
            candidate("product_dimensions", "10 x 20 x 30", unit="cm"),
            candidate("package_dimensions", "15 x 25 x 35", unit="cm"),
        ], schema=schema)
        self.assertEqual(profile.by_name["product_dimensions"].status, "Confirmed")
        self.assertEqual(profile.by_name["package_dimensions"].status, "Confirmed")
        self.assertEqual(profile.conflicts, [])


class NormalizationTests(unittest.TestCase):
    def test_equivalent_weight_units_merge(self):
        result = validate(
            "net_weight",
            [
                candidate("net_weight", "4.7", unit="kg", source="https://one.example/X100"),
                candidate("net_weight", "4700", unit="g", source="https://two.example/X100"),
            ],
            value_type="weight",
            unit_family="mass",
        )
        self.assertEqual(result.status, "Confirmed")
        self.assertEqual(len(result.supporting_facts), 2)

    def test_small_tolerance_does_not_hide_weight_conflict(self):
        result = validate(
            "net_weight",
            [
                candidate("net_weight", "4.70", unit="kg", source="https://one.example/X100"),
                candidate("net_weight", "4.69", unit="kg", source="https://two.example/X100"),
            ],
            value_type="weight",
            unit_family="mass",
        )
        self.assertEqual(result.status, "Conflict")

    def test_equivalent_dimensions_convert_without_reordering(self):
        result = validate(
            "product_dimensions",
            [
                candidate("product_dimensions", "162.9 x 76.31 x 7.5", unit="mm", source="https://one.example/X100"),
                candidate("product_dimensions", "16.29 x 7.631 x 0.75", unit="cm", source="https://two.example/X100"),
            ],
            value_type="dimension",
            unit_family="length",
        )
        self.assertEqual(result.status, "Confirmed")
        self.assertEqual(len(result.supporting_facts), 2)

    def test_dimension_order_is_not_normalized_away(self):
        result = validate(
            "product_dimensions",
            [
                candidate("product_dimensions", "10 x 20 x 30", unit="cm", source="https://one.example/X100"),
                candidate("product_dimensions", "20 x 10 x 30", unit="cm", source="https://two.example/X100"),
            ],
            value_type="dimension",
            unit_family="length",
        )
        self.assertEqual(result.status, "Conflict")

    def test_boolean_equivalents_use_schema_type(self):
        result = validate(
            "self_cleaning",
            [
                candidate("self_cleaning", "Yes", source="https://one.example/X100"),
                candidate("self_cleaning", "true", source="https://two.example/X100"),
                candidate("self_cleaning", "Да", source="https://three.example/X100"),
            ],
            value_type="boolean",
        )
        self.assertEqual(result.status, "Confirmed")
        self.assertEqual(len(result.supporting_facts), 3)


class RejectionAndUnresolvedTests(unittest.TestCase):
    def test_validation_never_launches_stage5_search(self):
        with patch("core.targeted_search.run_targeted_search") as search:
            validate_product_profile(
                IDENTITY,
                "cooktop",
                MappingResult(),
                schema=[definition("power")],
            )
        search.assert_not_called()

    def test_wrong_model_official_source_is_rejected(self):
        profile = validate_product_profile(
            IDENTITY,
            "cooktop",
            [candidate("power", "1000", unit="W", relation="different_model")],
            schema=[definition("power")],
        )
        self.assertEqual(profile.by_name["power"].status, "Unresolved")
        self.assertTrue(any(item.code == "rejected_wrong_model" for item in profile.diagnostics))

    def test_same_base_model_does_not_confirm_variant_sensitive_field(self):
        result = validate(
            "ram",
            [candidate("ram", "8 GB", relation="same_base_model")],
            unit_family="digital_storage",
            scope="variant_level",
        )
        self.assertEqual(result.status, "Unresolved")
        self.assertEqual(result.resolution_reason, "insufficient_identity")

    def test_missing_source_cannot_confirm(self):
        result = validate("power", [candidate("power", "1000", unit="W", source="")])
        self.assertEqual(result.status, "Unresolved")
        self.assertEqual(result.resolution_reason, "missing_provenance")

    def test_missing_evidence_cannot_confirm(self):
        result = validate("power", [candidate("power", "1000", unit="W", evidence="")])
        self.assertEqual(result.status, "Unresolved")
        self.assertEqual(result.resolution_reason, "missing_provenance")

    def test_absent_value_is_not_contradictory_evidence(self):
        result = validate("power", [
            candidate("power", "1000", unit="W"),
            candidate(
                "power", "", unit="W", source="https://empty.example/X100",
                evidence="Power row was empty",
            ),
        ])
        self.assertEqual(result.status, "Confirmed")
        self.assertEqual(result.value, "1000")
        self.assertEqual(result.conflicting_facts, ())

    def test_retailer_only_evidence_remains_unresolved(self):
        result = validate("power", [candidate(
            "power", "1000", unit="W", source_type="retailer", authority="unknown",
        )])
        self.assertEqual(result.status, "Unresolved")
        self.assertEqual(result.resolution_reason, "insufficient_source_quality")

    def test_ambiguous_weight_does_not_confirm_net_weight(self):
        mapping = map_attributes([raw("Weight", "4.7 kg")], category="cooktop")
        profile = validate_product_profile(
            IDENTITY,
            "cooktop",
            mapping,
            schema=[definition("net_weight", value_type="weight", unit_family="mass")],
        )
        result = profile.by_name["net_weight"]
        self.assertEqual(result.status, "Unresolved")
        self.assertEqual(result.resolution_reason, "ambiguous_scope")

    def test_candidate_identifier_with_unknown_identity_does_not_confirm_model(self):
        result = validate("model", [candidate(
            "model", "GAF-1825", relation="unknown",
        )])
        self.assertEqual(result.status, "Unresolved")
        self.assertEqual(result.resolution_reason, "insufficient_identity")

    def test_unknown_discovered_canonical_fact_survives(self):
        profile = validate_product_profile(
            IDENTITY,
            "cooktop",
            [candidate("peak_brightness", "1800 nit")],
            schema=[definition("power")],
        )
        self.assertIn("peak_brightness", profile.by_name)
        self.assertEqual(profile.by_name["peak_brightness"].status, "Confirmed")

    def test_blocked_targeted_search_is_unresolved(self):
        mapping = MappingResult()
        item_definition = definition("net_weight", value_type="weight", unit_family="mass")
        analysis = analyze_gaps(
            "cooktop", mapping, identity=IDENTITY, schema=[item_definition],
        )
        gap = analysis.by_name["net_weight"]
        query_result = TargetedQueryResult(
            "net_weight",
            "Acme X100 net weight",
            "net weight",
            discovery_status="blocked",
            issues=(DiscoveryIssue("blocked", "Acme X100 net weight", "bot-check"),),
        )
        field_result = TargetedFieldResult(
            gap, (query_result,), "blocked", False, "discovery_blocked",
        )
        targeted = TargetedSearchResult(
            TargetedSearchPlan("cooktop", config=TargetedSearchConfig()),
            [field_result],
        )
        profile = validate_product_profile(
            IDENTITY,
            "cooktop",
            mapping,
            targeted_search=targeted,
            gap_analysis=analysis,
            schema=[item_definition],
        )
        result = profile.by_name["net_weight"]
        self.assertEqual(result.status, "Unresolved")
        self.assertEqual(result.resolution_reason, "blocked_search_only")

    def test_related_targeted_evidence_does_not_resolve_requested_field(self):
        mapping = MappingResult()
        item_definition = definition("net_weight", value_type="weight", unit_family="mass")
        analysis = analyze_gaps(
            "cooktop", mapping, identity=IDENTITY, schema=[item_definition],
        )
        gap = analysis.by_name["net_weight"]
        gross = attribute("gross_weight", "5", unit="kg")
        query_result = TargetedQueryResult(
            "net_weight",
            "Acme X100 net weight",
            "net weight",
            related_candidate_facts=(gross,),
        )
        targeted = TargetedSearchResult(
            TargetedSearchPlan("cooktop"),
            [TargetedFieldResult(gap, (query_result,), "unresolved", False, "query_limit")],
        )
        profile = validate_product_profile(
            IDENTITY,
            "cooktop",
            mapping,
            targeted_search=targeted,
            gap_analysis=analysis,
            schema=[item_definition],
        )
        result = profile.by_name["net_weight"]
        self.assertEqual(result.status, "Unresolved")
        self.assertEqual(result.resolution_reason, "related_evidence_only")


if __name__ == "__main__":
    unittest.main()
