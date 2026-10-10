"""Offline finance boundary checks, requiring no API keys."""
import hashlib
import json
from decimal import Decimal, localcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from finagent.tools.store import FinanceStore
from finagent.tools.tools import build_finance_tools, decimal_text, decimal_value, normalize_value, safe_calculate


class FinanceToolsTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.document = {"id": "corp/2020-1", "question": "What was revenue growth?",
                         "pre_text": ["Revenue is stated in millions of USD."],
                         "post_text": ["A budget revision excluded an acquisition."],
                         "table": [["Revenue", "2019", "2020"], ["Net revenue", "100", "110"]]}
        self.path = self.inputs / "input.json"
        self.path.write_text(json.dumps(self.document), encoding="utf-8")
        self.store = FinanceStore(self.inputs, self.root / "outputs", selected_document_id=self.document["id"])
        self.tools = {x.name: x for x in build_finance_tools(store=self.store)}

    def invoke(self, name, **args):
        return self.tools["finance_" + name].invoke(args)

    def test_table_evidence_and_decimal_calculation(self):
        result = self.invoke("read_table", row_start=1)
        self.assertTrue(result.success)
        cells = result.structured_content["rows"][0]["cells"]
        self.assertEqual(cells[2]["locator"], "table:r1:c2")
        self.assertEqual(cells[2]["source_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        result = self.invoke("calculate", expression="(current-previous)/previous*100",
                             variables={"current": "110", "previous": "100"},
                             evidence_ids=[cells[1]["evidence_id"], cells[2]["evidence_id"]])
        self.assertEqual(result.structured_content["value"], "10")
        self.assertEqual(str(safe_calculate("0.1+0.2")), "0.3")
        self.assertEqual(str(safe_calculate("1.00000000000000001-1")), "1E-17")

    def test_search_preserves_split_year_heading_and_adjacent_evidence(self):
        self.document["post_text"] = ["2020 vs .", "2019 expense decreased.",
            "Special items total 8, including settlement losses 6.",
            "2019 vs .", "2018 expense increased.",
            "Special items total 20, including settlement losses 15."]
        self.path.write_text(json.dumps(self.document), encoding="utf-8")
        store = FinanceStore(self.inputs, self.root / "context_outputs")
        search = next(tool for tool in build_finance_tools(store=store) if tool.name == "finance_search_evidence")
        result = search.invoke({"query": "2020 settlement losses", "top_k": 5})
        self.assertTrue(result.success)
        hit = result.structured_content["hits"][0]
        self.assertEqual(hit["text"], self.document["post_text"][2])
        self.assertEqual(hit["context_heading"]["text"], "2020 vs .")
        for context in [*hit["context_before"], *hit["context_after"], hit["context_heading"]]:
            self.assertEqual(store.get_evidence([context["evidence_id"]])[0]["text"], context["text"])

    def test_search_matches_inflections_without_year_only_distractors(self):
        self.document["table"] = [["", "2020", "2019"], ["Pension expense continuing operations", "8", "6"]]
        self.document["post_text"] = ["Plan assets in 2020 and 2019 were 100 and 200."]
        self.path.write_text(json.dumps(self.document), encoding="utf-8")
        store = FinanceStore(self.inputs, self.root / "inflection_outputs")
        search = next(tool for tool in build_finance_tools(store=store) if tool.name == "finance_search_evidence")
        result = search.invoke({"query": "operating expenses 2020 2019", "top_k": 8})
        self.assertTrue(result.success)
        self.assertTrue(result.structured_content["hits"])
        self.assertTrue(all(hit["section"] == "table" and hit["row"] == 1 for hit in result.structured_content["hits"]))

    def test_rollforward_first_balance_is_not_treated_as_all_years_header(self):
        self.document["table"] = [["balance at December 31 2018", "$ 300"],
                                  ["tax positions current period", "20"],
                                  ["balance at December 31 2020", "$ 400"]]
        self.path.write_text(json.dumps(self.document), encoding="utf-8")
        store = FinanceStore(self.inputs, self.root / "rollforward_outputs")
        evidence = store.evidence_for_document()
        ending = next(item for item in evidence if item["locator"] == "table:r2:c1")
        self.assertEqual(ending["column_header"], "")
        self.assertNotIn("2018", ending["body"])
        search = next(tool for tool in build_finance_tools(store=store) if tool.name == "finance_search_evidence")
        result = search.invoke({"query": "balance December 31 2020 tax", "top_k": 2})
        self.assertTrue(all(hit["row"] == 2 for hit in result.structured_content["hits"]))

    def test_arithmetic_does_not_guess_financial_semantics_from_variable_names(self):
        result = self.invoke("calculate", expression="intangible_assets / total",
                             variables={"intangible_assets": 50, "total": 200})
        self.assertTrue(result.success)
        self.assertEqual(result.structured_content["value"], "0.25")
        # Domain reasoning belongs to validated methods and source selection,
        # rather than a benchmark-specific variable-name rule in arithmetic.

    def test_scope_gold_and_schema(self):
        self.assertFalse(self.invoke("read_table", document_id="../../gold/test.json").success)
        self.assertFalse(self.invoke("read_table", columns=[-1]).success)
        self.assertFalse(self.invoke("read_table", row_start=True).success)
        self.assertFalse(self.invoke("calculate", expression="1+2", extra="unexpected").success)
        with self.assertRaises(ValueError):
            FinanceStore(self.inputs, self.root / "other", selected_document_id="other")
        self.path.write_text(json.dumps(dict(self.document, answer="10")), encoding="utf-8")
        with self.assertRaises(ValueError):
            FinanceStore(self.inputs, self.root / "other")

    def test_no_execution_nonfinite_or_division_zero(self):
        for expression in ["__import__('os').system('unsafe')", "(1).__class__", "[1][0]", "1/0", "2**10000", "1e100*1e100", "True"]:
            self.assertFalse(self.invoke("calculate", expression=expression).success, expression)
        for value in ["NaN", "Infinity", "-Inf", "--", "1e-999999", "0e999999"]:
            self.assertFalse(self.invoke("normalize_value", value=value).success, value)
        self.assertFalse(self.invoke("calculate", expression="x", variables={"x": float("inf")}).success)

    def test_normalization_and_zero_growth(self):
        self.assertEqual(normalize_value("$1.25 million", target_unit="thousand")["value"], "1250")
        self.assertEqual(normalize_value("10%", target_unit="ratio")["value"], "0.1")
        self.assertEqual(normalize_value("(1,200)", unit="万元", target_unit="元")["value"], "-12000000")
        self.assertFalse(self.invoke("normalize_value", value="$10", currency="CNY").success)
        self.assertFalse(self.invoke("normalize_value", value="$10", target_unit="元").success)
        self.assertFalse(self.invoke("normalize_value", value="$10", target_unit="percent").success)
        self.assertFalse(self.invoke("normalize_value", value="$10 EUR").success)
        self.assertFalse(self.invoke("normalize_value", value="10", unit="million", target_unit="percent").success)
        result = self.invoke("compare_periods", current="10", previous="0")
        self.assertIsNone(result.structured_content["change_percent"])

    def test_thousands_grouping_rejects_ambiguous_financial_inputs(self):
        for value, expected in [("1,200", "1200"), ("(1,200.50)", "-1200.50"),
                                ("+12,345,678.90", "12345678.90"),
                                ("1,200e-3", "1.200"), ("1234.50", "1234.50")]:
            with self.subTest(value=value):
                self.assertEqual(decimal_value(value), Decimal(expected))
        self.assertEqual(normalize_value("($1,200)", target_unit="thousand")["value"], "-1.2")
        for value in ["1,2", "12,34", "1234,567", "1,,200", ",1200", "1,200,", "1.2,000"]:
            with self.subTest(value=value):
                self.assertFalse(self.invoke("normalize_value", value=value).success)
                self.assertFalse(self.invoke("calculate", expression="x", variables={"x": value}).success)

    def test_unit_aliases_agree_with_explicit_units_and_suffixes(self):
        for unit, suffix in [("percent", "%"), ("%", "percent"),
                             ("bps", "basis_points"), ("basis_points", "bps")]:
            with self.subTest(unit=unit, suffix=suffix):
                result = normalize_value("10" + suffix, unit=unit, target_unit="ratio")
                self.assertEqual(result["value"], "0.1" if unit in {"percent", "%"} else "0.001")
        self.assertEqual(normalize_value("0.1", unit="ratio", target_unit="%")["value"], "10")
        self.assertEqual(normalize_value("0.001", unit="ratio", target_unit="basis_points")["value"], "10")
        self.assertFalse(self.invoke("normalize_value", value="10%", unit="bps", target_unit="ratio").success)

    def test_decimal_range_checks_do_not_round_before_validation(self):
        # Unary Decimal abs applies the active context and can round a just-outside
        # value onto the accepted boundary. copy_abs must preserve every digit.
        inside = Decimal("9.999999999999999999999999999999999999999e99")
        outside = Decimal("1.000000000000000000000000000000000000001e100")
        with localcontext() as context:
            context.prec = 6
            for value in [inside, inside.copy_negate(), Decimal("1e100"), Decimal("-1e100")]:
                self.assertEqual(decimal_value(value), value)
                self.assertEqual(Decimal(decimal_text(value)), value)
            for value in [outside, outside.copy_negate()]:
                with self.assertRaises(ValueError):
                    decimal_value(value)
                with self.assertRaises(ValueError):
                    decimal_text(value)

    def test_search_and_verified_artifacts(self):
        hit = self.invoke("search_evidence", query="budget revision").structured_content["hits"][0]
        self.assertEqual(hit["locator"], "post_text:0")
        self.assertIn("budget", hit["body"])
        refs = [hit["evidence_id"]]
        self.assertFalse(self.invoke("write_report", title="Report", body="Body", evidence_ids=["fake"]).success)
        self.assertFalse(self.invoke("write_report", title="Report", body="Body", evidence_ids=refs, filename="../report.md").success)
        report = self.invoke("write_report", title="Report", body="Revision observed.", evidence_ids=refs, filename="report.md")
        self.assertTrue(report.success)
        self.assertIn(hit["source_sha256"], Path(report.structured_content["path"]).read_text(encoding="utf-8"))
        chart = self.invoke("render_chart", title="<script>", labels=["<bad>", "next"], values=["-1", "2"], evidence_ids=refs)
        self.assertTrue(chart.success)
        self.assertIn("&lt;script&gt;", Path(chart.structured_content["path"]).read_text(encoding="utf-8"))
        manifest = json.loads((self.root / "outputs" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["artifacts"]), 2)
        self.assertFalse(self.invoke("write_report", title="Again", body="Body", evidence_ids=refs, filename="report.md").success)
        reopened = FinanceStore(self.inputs, self.root / "outputs", selected_document_id=self.document["id"])
        self.assertEqual(reopened.known_evidence_ids, self.store.known_evidence_ids)
        manifest = json.loads((self.root / "outputs" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["artifacts"]), 2)
