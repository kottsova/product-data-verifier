import json
import unittest

from core.category import detect_category
from core.extract import extract_attributes, split_value_unit


def fetched(*, html="", pdf_text="", document_type="html", source_type=None):
    return {
        "status": "success",
        "source_url": "https://example.com/product",
        "final_url": "https://example.com/product",
        "document_type": document_type,
        "html": html,
        "text": "",
        "pdf_text": pdf_text,
        "source_type": source_type,
    }


class ExtractTests(unittest.TestCase):
    def test_split_value_unit(self):
        self.assertEqual(split_value_unit("4600 W"), ("4600", "W"))
        self.assertEqual(split_value_unit("59.2 cm"), ("59.2", "cm"))
        self.assertEqual(split_value_unit("11,2 kg"), ("11,2", "kg"))
        self.assertEqual(split_value_unit("4"), ("4", None))

    def test_split_composite_units_when_unambiguous(self):
        self.assertEqual(split_value_unit("51x592x522 mm"), ("51x592x522", "mm"))
        self.assertEqual(split_value_unit("130x753x610 mm"), ("130x753x610", "mm"))
        self.assertEqual(split_value_unit("220-240 V"), ("220-240", "V"))
        self.assertEqual(split_value_unit("50; 60 Hz"), ("50; 60", "Hz"))

    def test_complex_value_is_not_split(self):
        value = "180 mm, 1.8 KW (max. power 3.1 KW)"
        self.assertEqual(split_value_unit(value), (value, None))

    def test_json_ld_product_and_properties(self):
        payload = {
            "@context": "https://schema.org", "@type": "Product",
            "sku": "PUE611BB5E", "gtin13": "4242005285082", "color": "Black",
            "additionalProperty": [
                {"@type": "PropertyValue", "name": "Power", "value": "4600", "unitText": "W"},
                {"@type": "PropertyValue", "name": "Burner diameter", "value": "180 mm"},
            ],
            "offers": {"@type": "Offer", "price": "499", "priceCurrency": "EUR"},
            "description": "Marketing copy must not become an attribute",
        }
        html = f"<script type='application/ld+json'>{json.dumps(payload)}</script>"
        attrs = extract_attributes(fetched(html=html, source_type="manufacturer"))
        by_name = {item.name: item for item in attrs}
        self.assertEqual(by_name["sku"].value, "PUE611BB5E")
        self.assertEqual(by_name["Power"].unit, "W")
        self.assertEqual(by_name["Power"].source_type, "manufacturer")
        self.assertNotIn("description", by_name)
        self.assertIn("price", by_name)

    def test_json_ld_product_preserves_provenance(self):
        payload = {
            "@context": "https://schema.org",
            "@type": "Product",
            "brand": {"@type": "Brand", "name": "Acme"},
            "model": "Phone 12",
        }
        attrs = extract_attributes(fetched(
            html=f"<script type='application/ld+json'>{json.dumps(payload)}</script>",
            source_type="manufacturer",
        ))
        model = next(item for item in attrs if item.name == "model")
        self.assertEqual(model.source_url, "https://example.com/product")
        self.assertEqual(model.source_type, "manufacturer")
        self.assertEqual(model.extraction_method, "json_ld")
        self.assertIn("Product:", model.evidence)

    def test_next_data_product_state_and_nested_specifications(self):
        payload = {
            "props": {
                "pageProps": {
                    "product": {
                        "brand": {"name": "Acme"},
                        "model": "Phone 12",
                        "category": "Smartphone",
                        "specifications": {
                            "Display": {
                                "Size": "6.7 inches",
                                "Resolution": "1080 x 2400",
                            },
                            "Battery": [
                                {"name": "Capacity", "value": "5000 mAh"},
                                {"name": "Wired Charging", "value": "45 W"},
                            ],
                        },
                    },
                },
            },
        }
        html = (
            "<script id='__NEXT_DATA__' type='application/json'>"
            f"{json.dumps(payload)}</script>"
        )
        attrs = extract_attributes(fetched(html=html, source_type="manufacturer"))
        by_pair = {(item.name, item.raw_value): item for item in attrs}

        self.assertIn(("brand", "Acme"), by_pair)
        self.assertIn(("model", "Phone 12"), by_pair)
        self.assertIn(("Size", "6.7 inches"), by_pair)
        self.assertIn(("Resolution", "1080 x 2400"), by_pair)
        self.assertIn(("Capacity", "5000 mAh"), by_pair)
        self.assertEqual(by_pair[("Size", "6.7 inches")].context, "Display")
        self.assertEqual(by_pair[("Capacity", "5000 mAh")].context, "Battery")
        self.assertTrue(all(
            item.extraction_method == "embedded_json" for item in by_pair.values()
        ))
        self.assertTrue(all(
            item.source_url == "https://example.com/product" for item in by_pair.values()
        ))

    def test_strict_nuxt_assignment_product_state(self):
        payload = {
            "data": [{
                "product": {
                    "model": "Phone 12",
                    "specs": {"Processor": {"CPU Model": "Example 9000"}},
                },
            }],
        }
        html = f"<script>window.__NUXT__ = {json.dumps(payload)};</script>"
        attrs = extract_attributes(fetched(html=html))
        pairs = {(item.name, item.raw_value, item.context) for item in attrs}
        self.assertIn(("model", "Phone 12", None), pairs)
        self.assertIn(("CPU Model", "Example 9000", "Processor"), pairs)

    def test_irrelevant_embedded_json_is_not_attributes(self):
        payload = {
            "analytics": {"page": "support", "memory": "session"},
            "user": {"name": "Visitor", "preferences": {"color": "blue"}},
            "navigation": {"items": ["Phones", "Laptops"]},
        }
        html = f"<script type='application/json'>{json.dumps(payload)}</script>"
        self.assertEqual(extract_attributes(fetched(html=html)), [])

    def test_data_value_specs_are_extracted_without_rendered_text(self):
        html = """<section class='product-specifications'>
          <h2>Display</h2>
          <div class='product-spec-item'>
            <h3>Size</h3>
            <div class='spec-description'>
              <div data-class='p2' data-value='6.77 inches'></div>
              <div data-class='p4' data-value='Measurement disclaimer'></div>
            </div>
          </div>
          <div class='product-spec-item'>
            <h3>Resolution</h3>
            <div data-value='1080 x 2392'></div>
          </div>
        </section>"""
        attrs = extract_attributes(fetched(html=html, source_type="manufacturer"))
        by_name = {item.name: item for item in attrs}
        self.assertEqual(set(by_name), {"Size", "Resolution"})
        self.assertEqual(by_name["Size"].raw_value, "6.77 inches")
        self.assertEqual(by_name["Size"].context, "Display")
        self.assertEqual(by_name["Size"].extraction_method, "structured_data")
        self.assertEqual(by_name["Size"].source_type, "manufacturer")
        self.assertIn("data-value:", by_name["Size"].evidence)

    def test_data_attribute_json_requires_spec_or_product_scope(self):
        specs = {"Battery": {"Capacity": "5000 mAh"}}
        product = {
            "model": "Phone 12",
            "specifications": {"Processor": {"CPU Model": "Example 9000"}},
        }
        irrelevant = {"preferences": {"Color": "Blue"}}
        html = (
            f"<div data-specifications='{json.dumps(specs)}'></div>"
            f"<div data-product='{json.dumps(product)}'></div>"
            f"<div data-config='{json.dumps(irrelevant)}'></div>"
        )
        attrs = extract_attributes(fetched(html=html))
        self.assertEqual(
            {(item.name, item.raw_value, item.context) for item in attrs},
            {
                ("Capacity", "5000 mAh", "Battery"),
                ("model", "Phone 12", None),
                ("CPU Model", "Example 9000", "Processor"),
            },
        )

    def test_structured_smartphone_facts_drive_existing_category_detector(self):
        payload = {
            "pageProps": {
                "product": {
                    "model": "Phone 12",
                    "specifications": {
                        "Processor": {
                            "CPU Model": "Example 9000",
                            "GPU": "Example G1",
                        },
                        "Camera": {"Rear Camera": "108 MP"},
                    },
                },
            },
        }
        attrs = extract_attributes(fetched(
            html=(
                "<script id='__NEXT_DATA__' type='application/json'>"
                f"{json.dumps(payload)}</script>"
            ),
        ))
        category = detect_category(attributes=attrs)
        self.assertEqual(category.category_id, "smartphone")
        self.assertEqual(category.source, "extracted_attributes")

    def test_html_table_keeps_distinct_dimensions_and_weights(self):
        html = """<table>
          <tr><th>Power</th><td>4600 W</td></tr><tr><td>Width</td><td>592 mm</td></tr>
          <tr><td>Package width</td><td>650 mm</td></tr>
          <tr><td>Net weight</td><td>11.2 kg</td></tr>
          <tr><td>Gross weight</td><td>12.8 kg</td></tr></table>"""
        attrs = extract_attributes(fetched(html=html))
        names = {item.name for item in attrs}
        self.assertTrue({"Power", "Width", "Package width", "Net weight", "Gross weight"} <= names)
        self.assertEqual(len([item for item in attrs if "weight" in item.name.lower()]), 2)
        self.assertTrue(all(item.extraction_method == "html_table" for item in attrs))

    def test_definition_list(self):
        attrs = extract_attributes(fetched(html="<dl><dt>Material</dt><dd>Ceramic glass</dd></dl>"))
        self.assertEqual((attrs[0].name, attrs[0].value, attrs[0].extraction_method),
                         ("Material", "Ceramic glass", "definition_list"))

    def test_generic_label_value_div(self):
        html = "<div class='spec'><span class='spec-label'>Width</span><span class='spec-value'>592 mm</span></div>"
        attrs = extract_attributes(fetched(html=html))
        self.assertTrue(any(item.name == "Width" and item.extraction_method == "label_value" for item in attrs))

    def test_repeated_sibling_pairs(self):
        html = """<section class='technical-data'>
          <div><span>Machine type</span><span>Electromechanical</span></div>
          <div><span>Shuttle type</span><span>Vertical</span></div>
          <div><span>Operations</span><span>12</span></div>
          <div><span>Lighting</span><span>LED</span></div>
        </section>"""
        attrs = extract_attributes(fetched(html=html))
        pairs = {(item.name, item.raw_value) for item in attrs}
        self.assertTrue({("Machine type", "Electromechanical"),
                         ("Shuttle type", "Vertical"), ("Operations", "12"),
                         ("Lighting", "LED")} <= pairs)

    def test_inline_bold_feature_rows_do_not_leak_into_weak_colon_pairs(self):
        html = """<div class='product-detail-text'>
          <p><b>Features</b> Suction power - 25 000 Pa. 4 operating modes:
            Self-cleaning with hot water at 90°C and drying with hot air for 30 min at 95°C.</p>
          <p><b>Self-cleaning mode</b> Yes</p>
          <p><b>Roller brush drying</b> Yes</p>
          <p><b>Maximum suction power, kPa</b> 25</p>
        </div>"""

        attrs = extract_attributes(fetched(html=html))
        pairs = {(item.name, item.raw_value) for item in attrs}

        self.assertIn(("Self-cleaning mode", "Yes"), pairs)
        self.assertIn(("Roller brush drying", "Yes"), pairs)
        self.assertIn(("Maximum suction power, kPa", "25"), pairs)
        self.assertIn(("drying", "95°C"), pairs)
        self.assertFalse(any(item.name.startswith("Suction power - 25 000") for item in attrs))
        self.assertFalse(any(
            item.extraction_method == "spec_block" and "Self-cleaning" in item.raw_value
            for item in attrs
        ))

    def test_repeated_pairs_preserve_semantic_section_context(self):
        html = """<section class='products-spec-component-level-1'>
          <div class='products-spec-component-title'>Dimensions and Weight</div>
          <div class='products-spec-component-transform-container'>
            <div class='products-spec-component-list-item-right'>
              <div><h3>Height</h3><p>162.9 mm</p></div>
              <div><h3>Width</h3><p>76.31 mm</p></div>
              <div><h3>Depth</h3><p>7.5 mm</p></div>
            </div>
          </div>
        </section>"""
        attrs = extract_attributes(fetched(html=html))
        dimensions = [item for item in attrs if item.name in {"Height", "Width", "Depth"}]
        self.assertEqual(len(dimensions), 3)
        self.assertTrue(all(item.context == "Dimensions and Weight" for item in dimensions))
        self.assertTrue(all("products-spec" not in item.context for item in dimensions))

    def test_nested_numbered_spec_pairs_do_not_create_weak_heading_pairs(self):
        html = """<section class='product-specifications'><div class='spec-item'>
          <h3>SIM Card</h3><div></div>
          <div class='sub-row'><p>SIM Card 1</p><div><p>Nano SIM card</p></div></div>
          <div class='sub-row'><p>SIM Card 2</p><div><p>Nano SIM card</p></div></div>
        </div><div class='spec-item'><h3>Power</h3><div>45 W</div></div>
        <div class='spec-item'><h3>Color</h3><div>Black</div></div></section>"""
        attrs = extract_attributes(fetched(html=html))
        pairs = {(item.name, item.raw_value) for item in attrs}
        self.assertIn(("SIM Card 1", "Nano SIM card"), pairs)
        self.assertIn(("SIM Card 2", "Nano SIM card"), pairs)
        self.assertNotIn(("SIM Card", "1"), pairs)
        self.assertNotIn(("SIM Card", "2"), pairs)

    def test_random_article_blocks_are_not_flat_pairs(self):
        html = """<article><div><p>First paragraph of an ordinary article.</p><p>More prose follows here.</p></div>
        <div><p>Second paragraph discusses a product.</p><p>It is not a specification value.</p></div></article>"""
        self.assertEqual(extract_attributes(fetched(html=html)), [])

    def test_embedded_json_html_table(self):
        payload = {"blocks": [{"html": "<table><tr><th>Power</th><td>2700 W</td></tr>"
                                       "<tr><th>Programs</th><td>9</td></tr>"
                                       "<tr><th>Gross weight</th><td>12.8 kg</td></tr>"
                                       "<tr><th>Package dimensions</th><td>650x590x120 mm</td></tr></table>"}]}
        html = f"<script type='application/json'>{json.dumps(payload)}</script>"
        attrs = extract_attributes(fetched(html=html))
        self.assertEqual({item.name for item in attrs},
                         {"Power", "Programs", "Gross weight", "Package dimensions"})
        self.assertTrue(all(item.extraction_method == "html_table" for item in attrs))

    def test_malformed_javascript_is_ignored(self):
        html = "<script>window.state = {broken: '<table><tr><td>x'};</script><p>Ordinary article.</p>"
        self.assertEqual(extract_attributes(fetched(html=html)), [])

    def test_seo_title_is_not_an_attribute(self):
        html = """<html><head><title>Buy Vacuum Model X | prices, reviews and credit</title></head>
        <body><h1>Buy Vacuum Model X</h1><p>Prices, reviews and credit</p></body></html>"""
        self.assertEqual(extract_attributes(fetched(html=html)), [])

    def test_attribute_kind_classification(self):
        payload = {"@type": "Product", "sku": "ABC-1", "offers": {"@type": "Offer", "price": "99"}}
        html = f"<script type='application/ld+json'>{json.dumps(payload)}</script>"
        html += "<table><tr><td>Power</td><td>4600 W</td></tr></table>"
        kinds = {item.name: item.attribute_kind for item in extract_attributes(fetched(html=html))}
        self.assertEqual(kinds["sku"], "identity")
        self.assertEqual(kinds["price"], "commerce")
        self.assertEqual(kinds["Power"], "product")

        generic = extract_attributes(fetched(html="<div>Power: 4600 W</div>"))
        self.assertEqual(generic[0].attribute_kind, "product")

    def test_generic_pairs_inside_ui_regions_are_ignored(self):
        html = """<nav>Width: 1 mm</nav><header>Height: 2 mm</header>
        <footer>Depth: 3 mm</footer><aside>Weight: 4 kg</aside>
        <div class='checkout-panel'>Color: Red</div><div id='delivery-options'>Power: 5 W</div>"""
        self.assertEqual(extract_attributes(fetched(html=html)), [])

    def test_high_link_density_menu_is_ignored(self):
        html = """<div><a href='/tv'>Smart TV's 4K TV's</a>
        <a href='/oled'>OLED TV</a><a href='/hangers'>TV hangers</a></div>"""
        self.assertEqual(extract_attributes(fetched(html=html)), [])

    def test_instruction_like_label_is_ignored(self):
        html = "<div>Fill in your contact information and delivery address.: 3. Payment:</div>"
        self.assertEqual(extract_attributes(fetched(html=html)), [])

    def test_short_specs_and_simple_values_are_retained(self):
        html = """<div>Color: Black</div><div>Timer: Yes</div><div>Home Connect: No</div>
        <div>Zones: 4</div><div>Power: 17 power levels</div><div>Material: Ceramic glass</div>"""
        attrs = extract_attributes(fetched(html=html))
        self.assertEqual({(item.name, item.raw_value) for item in attrs}, {
            ("Color", "Black"), ("Timer", "Yes"), ("Home Connect", "No"),
            ("Zones", "4"), ("Power", "17 power levels"), ("Material", "Ceramic glass"),
        })
        self.assertTrue(all(item.confidence == "low" for item in attrs))

    def test_pdf_structured_lines(self):
        text = """Connected load: 4600 W
Cooking zone diameter: 180 mm
Package dimensions: 650 x 590 x 120 mm
Gross weight: 12.8 kg"""
        attrs = extract_attributes(fetched(pdf_text=text, document_type="pdf"))
        self.assertEqual({item.name for item in attrs},
                         {"Connected load", "Cooking zone diameter", "Package dimensions", "Gross weight"})
        self.assertTrue(all(item.extraction_method == "pdf_spec" for item in attrs))

    def test_multiline_pdf_pair(self):
        text = "Net weight\n11.2 kg\nGross weight\n13.5 kg"
        attrs = extract_attributes(fetched(pdf_text=text, document_type="pdf"))
        by_name = {item.name: item for item in attrs}
        self.assertEqual((by_name["Net weight"].value, by_name["Net weight"].unit), ("11.2", "kg"))
        self.assertEqual((by_name["Gross weight"].value, by_name["Gross weight"].unit), ("13.5", "kg"))

    def test_safe_pdf_continuation_merges_interrupted_dimension(self):
        text = "Required niche size: 51 x 560 x (490 -\n500)\nConnected load: 4600 W"
        attrs = extract_attributes(fetched(pdf_text=text, document_type="pdf"))
        by_name = {item.name: item for item in attrs}
        self.assertEqual(by_name["Required niche size"].raw_value, "51 x 560 x (490 - 500)")
        self.assertEqual(by_name["Connected load"].value, "4600")

    def test_unrelated_pdf_neighbors_are_not_merged(self):
        text = "Net weight\nWidth: 592 mm\nTechnical details\nColor: Black"
        attrs = extract_attributes(fetched(pdf_text=text, document_type="pdf"))
        self.assertEqual({item.name for item in attrs}, {"Width", "Color"})

    def test_pdf_bullet_specification_list_extracted_with_provenance(self):
        text = (
            "25\n"
            "Disposal\n"
            "Technical specifications\n"
            "Do not dispose of the appliance with household waste.\n"
            "• Power: 2700 W\n"
            "• Voltage: 220-240 V, 50/60 Hz\n"
            "• Bowl 1 capacity: 4 l\n"
            "• Bowl 2 capacity: 4 l\n"
            "• Total capacity of two bowls: 8 l\n"
            "• Model: GAF-1825\n"
            "• Protection class: I\n"
            "EN\n"
        )
        attrs = extract_attributes(fetched(pdf_text=text, document_type="pdf"))
        by_name = {item.name: item for item in attrs}
        self.assertEqual(set(by_name), {
            "Power", "Voltage", "Bowl 1 capacity", "Bowl 2 capacity",
            "Total capacity of two bowls", "Model", "Protection class",
        })
        self.assertEqual((by_name["Power"].value, by_name["Power"].unit), ("2700", "W"))
        self.assertEqual(by_name["Model"].value, "GAF-1825")
        self.assertTrue(all(item.extraction_method == "pdf_spec" for item in attrs))
        self.assertIn("page 25", by_name["Power"].context)
        self.assertIn("Technical specifications", by_name["Power"].context)
        # Distinct bowl/total capacities must never collapse into one label.
        self.assertEqual(by_name["Bowl 1 capacity"].value, "4")
        self.assertEqual(by_name["Bowl 2 capacity"].value, "4")
        self.assertEqual(by_name["Total capacity of two bowls"].value, "8")

    def test_pdf_unit_terminated_lines_in_proven_spec_block(self):
        text = (
            "25\n"
            "Technical specifications\n"
            "Rated power 2700 W.\n"
            "Bowl capacity 4 l.\n"
            "Net weight 5 kg.\n"
        )
        attrs = extract_attributes(fetched(pdf_text=text, document_type="pdf"))
        by_name = {item.name: item for item in attrs}
        self.assertEqual(set(by_name), {"Rated power", "Bowl capacity", "Net weight"})
        self.assertEqual((by_name["Rated power"].value, by_name["Rated power"].unit), ("2700", "W"))

    def test_pdf_cyrillic_units_are_split(self):
        text = (
            "25\n"
            "Технические характеристики\n"
            "• Мощность: 2700 Вт\n"
            "• Объем чаши: 4 л\n"
            "• Вес: 5 кг\n"
        )
        attrs = extract_attributes(fetched(pdf_text=text, document_type="pdf"))
        by_name = {item.name: item for item in attrs}
        self.assertEqual((by_name["Мощность"].value, by_name["Мощность"].unit), ("2700", "Вт"))
        self.assertEqual((by_name["Объем чаши"].value, by_name["Объем чаши"].unit), ("4", "л"))
        self.assertEqual((by_name["Вес"].value, by_name["Вес"].unit), ("5", "кг"))

    def test_pdf_prose_with_incidental_colon_is_ignored(self):
        text = (
            "4\n"
            "General information\n"
            "• Please read this manual carefully before use.\n"
            "• Do not immerse the device in water or other liquids.\n"
            "• This appliance is intended for household use: homes, offices, "
            "apartments, hotels.\n"
            "• Keep the device away from children.\n"
        )
        attrs = extract_attributes(fetched(pdf_text=text, document_type="pdf"))
        self.assertEqual(attrs, [])

    def test_pdf_toc_and_page_numbers_are_ignored(self):
        text = (
            "3\n"
            "Contents\n"
            "4\n6\n7\n8\n"
            "Safety instructions .......................... 4\n"
            "Specifications ................................ 25\n"
        )
        attrs = extract_attributes(fetched(pdf_text=text, document_type="pdf"))
        self.assertEqual(attrs, [])

    def test_pdf_hotline_and_support_contacts_are_ignored(self):
        text = (
            "26\n"
            "Manufacturer: Example Group Co, Ltd.\n"
            "Hotline: +1 (555) 123-4567. E-mail: support@example.com\n"
            "Phone: +1 (555) 765-4321.\n"
            "Customer service in Region A: +1 (555) 123-4567.\n"
            "Customer service in Region B: +1 (555) 765-4321.\n"
        )
        attrs = extract_attributes(fetched(pdf_text=text, document_type="pdf"))
        self.assertEqual(attrs, [])

    def test_pdf_repeated_language_markers_do_not_merge_across_pages(self):
        text = "Some closing troubleshooting text ends here\n\n25\nEN\n"
        attrs = extract_attributes(fetched(pdf_text=text, document_type="pdf"))
        self.assertEqual(attrs, [])

    def test_duplicate_prefers_stronger_method(self):
        payload = {"@type": "Product", "additionalProperty": [
            {"@type": "PropertyValue", "name": "Power", "value": "4600 W"}
        ]}
        html = f"<script type='application/ld+json'>{json.dumps(payload)}</script><table><tr><td>Power</td><td>4600 W</td></tr></table>"
        attrs = [item for item in extract_attributes(fetched(html=html)) if item.name == "Power"]
        self.assertEqual(len(attrs), 1)
        self.assertEqual(attrs[0].extraction_method, "json_ld")

    def test_conflicting_values_are_preserved(self):
        html = "<table><tr><td>Weight</td><td>10 kg</td></tr><tr><td>Weight</td><td>11 kg</td></tr></table>"
        attrs = [item for item in extract_attributes(fetched(html=html)) if item.name == "Weight"]
        self.assertEqual({item.raw_value for item in attrs}, {"10 kg", "11 kg"})

    def test_marketing_paragraph_does_not_create_attributes(self):
        paragraph = "Discover exceptional cooking with elegant design and effortless control. " * 20
        attrs = extract_attributes(fetched(html=f"<p>{paragraph}</p>"))
        self.assertEqual(attrs, [])

    def test_non_successful_fetch_is_not_extracted(self):
        result = fetched(html="<table><tr><td>Power</td><td>10 W</td></tr></table>")
        result["status"] = "blocked"
        self.assertEqual(extract_attributes(result), [])


if __name__ == "__main__":
    unittest.main()
