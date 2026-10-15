import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from meridian.config import settings
from meridian.agents import FinancialAgent, SynthesizerAgent
from meridian.documents import ExtractedBlock, extract_document
from meridian.fx import FXRates
from meridian.evaluation import run_fixture_evaluation
from meridian.pipeline import _has_cached_filing_coverage, _ingest_xbrl, run_research
from meridian.registry import Company, CompanyRegistry
from meridian.store import Store
from meridian.sec import SecClient
from meridian.xbrl_review import compare_dimensioned_periods, extract_inline_xbrl_review, reconcile_fact_versions


class RegistryTests(unittest.TestCase):
    def test_search_ticker_alias_and_exact_resolution(self):
        root = Path(__file__).resolve().parents[1]
        registry = CompanyRegistry(root / "data" / "fortune500.csv")
        self.assertEqual(registry.resolve("AMZN").company_name, "Amazon")
        self.assertEqual(registry.resolve("Amazon.com").cik, "1018724")
        self.assertEqual(len(registry.search("microsoft")), 1)
        with self.assertRaises(ValueError):
            registry.resolve("not in the registry")


class DocumentTests(unittest.TestCase):
    def test_html_tables_and_section_provenance(self):
        html = b"<h2>Consolidated Results</h2><p>Revenue increased in the period by a material amount.</p><table><tr><th>Metric</th><th>Value</th></tr><tr><td>Revenue</td><td>100</td></tr></table>"
        blocks = extract_document("filing.html", html, "text/html")
        self.assertTrue(any(x.content_type == "table" and "Revenue | 100" in x.content for x in blocks))
        table = next(x for x in blocks if x.content_type == "table")
        self.assertEqual(table.section, "Consolidated Results")
        self.assertEqual(table.table_id, "table-1")

    def test_scanned_pdf_uses_tesseract_and_preserves_page_number(self):
        tesseract = shutil.which(settings.tesseract_cmd) or settings.tesseract_cmd
        poppler = shutil.which(settings.pdftoppm_cmd) or settings.pdftoppm_cmd
        if not Path(tesseract).is_file() or not shutil.which(poppler):
            self.skipTest("Tesseract or Poppler is unavailable")

        from PIL import Image, ImageDraw, ImageFont
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas

        temp_root = Path(__file__).parent / ".tmp"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as tmp:
            root = Path(tmp)
            image_path = root / "scan.png"
            pdf_path = root / "scan.pdf"
            image = Image.new("RGB", (1600, 500), "white")
            draw = ImageDraw.Draw(image)
            font_path = Path("C:/Windows/Fonts/arial.ttf")
            font = ImageFont.truetype(str(font_path), 52) if font_path.is_file() else ImageFont.load_default(size=52)
            draw.text((70, 150), "Revenue from subscriptions increased to 4.2 billion dollars in 2025.",
                      fill="black", font=font)
            image.save(image_path)
            pdf = canvas.Canvas(str(pdf_path), pagesize=letter)
            pdf.drawImage(str(image_path), 35, 300, width=540, height=170)
            pdf.save()

            old_tempdir = tempfile.tempdir
            tempfile.tempdir = str(temp_root)
            try:
                blocks = extract_document("scan.pdf", pdf_path.read_bytes(), "application/pdf")
            finally:
                tempfile.tempdir = old_tempdir
            ocr_blocks = [block for block in blocks if block.extraction_method == "tesseract_ocr"]
            self.assertTrue(ocr_blocks)
            self.assertTrue(any(block.page_number == 1 for block in ocr_blocks))
            self.assertTrue(any("Revenue" in block.content for block in ocr_blocks))


class StoreTests(unittest.TestCase):
    def test_save_evaluation_persists_all_fields(self):
        temp_root = Path(__file__).parent / ".tmp"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as tmp:
            store = Store(Path(tmp) / "evaluation.sqlite3")
            result = {"context_precision": 0.75, "dataset_size": 2}
            store.save_evaluation(None, "fixture", result)
            saved = store.latest_evaluation()
            self.assertIsNotNone(saved)
            self.assertEqual(saved["dataset_name"], "fixture")
            self.assertEqual(saved["result"], result)

    def test_persistent_vector_retrieval_filters_company_period(self):
        temp_root = Path(__file__).parent / ".tmp"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as tmp:
            store = Store(Path(tmp) / "test.sqlite3")
            company = Company("Acme", "ACM", "123", rank=1, reporting_year=2026).as_dict()
            blocks = [ExtractedBlock("Cybersecurity incidents could disrupt customer information.", section="Risk Factors")]
            with patch("meridian.embeddings.EmbeddingClient._provider", return_value="hash"):
                doc_id, created = store.add_document(company, "10-K", "FY2025", "Acme 10-K", "https://example.test/10k", "hash-one", blocks)
                self.assertTrue(created)
                hits = store.search("cybersecurity incident customer information", ["123"], ["FY2025"], ["10-K"], 5)
                self.assertEqual(len(hits), 1)
                self.assertEqual(hits[0]["doc_id"], doc_id)
                self.assertEqual(store.search("cybersecurity", ["456"], ["FY2025"], ["10-K"], 5), [])
                self.assertEqual(store.search("cybersecurity", ["123"], ["FY2024"], ["10-K"], 5), [])


class FinancialTests(unittest.TestCase):
    def test_xbrl_fact_without_accession_uses_companyfacts_source(self):
        class FakeSec:
            @staticmethod
            def facts_for_year(_cik, _year, _raw_facts):
                return {"revenue": {"value": 10.0, "unit": "USD", "tag": "us-gaap:Revenues",
                    "accession": None, "end": "2025-12-31", "reported_value": 10.0,
                    "reported_unit": "USD"}}

        class FakeStore:
            source_url = None
            metadata = None

            @staticmethod
            def add_document(_company, _document_type, _period, _title, source_url, _digest, _blocks, **_kwargs):
                FakeStore.source_url = source_url
                return "doc-1", True

            @staticmethod
            def chunks_for(_cik, _document_type, _periods):
                return [{"doc_id": "doc-1", "table_id": "us-gaap:Revenues", "chunk_id": "chunk-1"}]

            @staticmethod
            def update_document_metadata(_doc_id, metadata):
                FakeStore.metadata = metadata

        company = Company("Acme", "ACM", "123", rank=1, reporting_year=2025)
        with patch("meridian.pipeline.reconcile_fact_versions", return_value=[]):
            result = _ingest_xbrl(FakeStore(), FakeSec(), company, 2025, {}, {})
        expected = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000123.json"
        self.assertEqual(result["facts"]["revenue"]["source_url"], expected)
        self.assertEqual(FakeStore.source_url, expected)

    def test_sec_recent_filings_tolerate_short_optional_field_arrays(self):
        client = SecClient("Meridian test test@example.com")
        client.submissions = lambda _cik: {"filings": {"recent": {
            "accessionNumber": ["0000320193-25-000001"],
            "form": ["10-K"],
            "filingDate": ["2025-02-01"],
            "reportDate": ["2024-12-31"],
            "primaryDocument": [],
            "fp": [],
        }}}
        filings = client.filings("320193", ("10-K",), 2024)
        self.assertEqual(len(filings), 1)
        self.assertEqual(filings[0]["period"], "2024-12-31")
        self.assertEqual(filings[0]["fiscal_period"], "")
        self.assertTrue(filings[0]["index_url"].endswith("/000032019325000001/index.json"))

    def test_margin_growth_and_fcf_formulas(self):
        companies = [{"company_name": "Acme", "cik": "123"}]
        facts = {"123": {
            "FY2024": {"revenue": {"value": 100.0, "evidence_ids": ["r24"]},
                       "gross_profit": {"value": 40.0, "evidence_ids": ["g24"]},
                       "operating_income": {"value": 20.0, "evidence_ids": ["o24"]},
                       "net_income": {"value": 10.0, "evidence_ids": ["n24"]}},
            "FY2025": {"revenue": {"value": 150.0, "evidence_ids": ["r25"]},
                       "gross_profit": {"value": 60.0, "evidence_ids": ["g25"]},
                       "operating_income": {"value": 30.0, "evidence_ids": ["o25"]},
                       "net_income": {"value": 15.0, "evidence_ids": ["n25"]},
                       "operating_cash_flow": {"value": 90.0, "evidence_ids": ["ocf"]},
                       "capital_expenditures": {"value": -20.0, "evidence_ids": ["capex"]}}}}
        result = FinancialAgent().run(companies, facts)["metrics"]["123"]["periods"]
        self.assertEqual(result["FY2025"]["revenue_growth"]["value"], 50.0)
        self.assertEqual(result["FY2025"]["gross_margin"]["value"], 40.0)
        self.assertEqual(result["FY2025"]["free_cash_flow"]["value"], 70.0)

    def test_sec_facts_choose_annual_filing_and_preserve_source_tag(self):
        raw = {"facts": {"us-gaap": {
            "Revenues": {"units": {"USD": [
                {"fy": 2024, "fp": "Q1", "form": "10-Q", "val": 25, "end": "2024-03-31", "filed": "2024-05-01", "acc": "0000000000-24-000001"},
                {"fy": 2024, "fp": "FY", "form": "10-K", "val": 100, "end": "2024-12-31", "filed": "2025-02-01", "acc": "0000000000-25-000002"},
                {"fy": 2023, "fp": "FY", "form": "10-K", "val": 90, "end": "2023-12-31", "filed": "2024-02-01", "acc": "0000000000-24-000003"}
            ]}},
            "EarningsPerShareDiluted": {"units": {"USD / shares": [
                {"fy": 2024, "fp": "FY", "form": "10-K", "val": 4.2, "end": "2024-12-31", "filed": "2025-02-01", "acc": "0000000000-25-000002"}
            ]}}
        }}}
        result = SecClient("Meridian test test@example.com").facts_for_year("123", 2024, raw)
        self.assertEqual(result["revenue"]["value"], 100)
        self.assertEqual(result["revenue"]["tag"], "Revenues")
        self.assertEqual(result["eps_diluted"]["value"], 4.2)
        self.assertEqual(result["eps_diluted"]["unit"], "USD / shares")

    def test_sec_facts_preserve_non_usd_reporting_unit(self):
        raw = {"facts": {"us-gaap": {"Revenues": {"units": {"EUR": [
            {"fy": 2025, "fp": "FY", "form": "10-K", "val": 90, "end": "2025-12-31", "filed": "2026-02-01", "acc": "0000000000-26-000001"}
        ]}}}}}
        fact = SecClient("Meridian test test@example.com").facts_for_year("123", 2025, raw)["revenue"]
        self.assertEqual(fact["value"], 90)
        self.assertEqual(fact["unit"], "EUR")


class AnalystReviewTests(unittest.TestCase):
    def test_inline_xbrl_review_finds_custom_and_dimensioned_facts(self):
        html = b'''<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:acme="https://example.com/taxonomy">
        <ix:header><xbrli:context id="segment2023"><xbrli:entity><xbrli:identifier>123</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:startDate>2023-01-01</xbrli:startDate><xbrli:endDate>2023-12-31</xbrli:endDate></xbrli:period>
        <xbrli:scenario><xbrldi:explicitMember dimension="us-gaap:StatementBusinessSegmentsAxis">acme:ConsumerProductsMember</xbrldi:explicitMember></xbrli:scenario></xbrli:context></ix:header>
        <ix:nonFraction name="acme:SubscriptionRevenue" contextRef="segment2023" unitRef="USD" scale="6">4.2</ix:nonFraction>
        <ix:nonFraction name="us-gaap:Revenues" contextRef="segment2023" unitRef="USD" scale="0">100</ix:nonFraction></html>'''
        result = extract_inline_xbrl_review(html)
        self.assertEqual(result["custom_tags"]["count"], 1)
        self.assertEqual(result["custom_tags"]["items"][0]["name"], "acme:SubscriptionRevenue")
        self.assertEqual(result["custom_tags"]["items"][0]["value"], 4_200_000)
        self.assertEqual(result["dimensioned_facts"]["count"], 2)
        custom_dims = next(item for item in result["dimensioned_facts"]["items"] if item["prefix"] == "acme")
        self.assertEqual(custom_dims["period"]["end"], "2023-12-31")
        self.assertEqual(custom_dims["dimensions"][0]["member"], "acme:ConsumerProductsMember")

    def test_restatement_candidates_compare_same_period_versions(self):
        raw = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
            {"fy": 2023, "fp": "FY", "form": "10-K", "val": 100, "start": "2023-01-01", "end": "2023-12-31", "filed": "2024-02-15", "acc": "0000000000-24-000001"},
            {"fy": 2024, "fp": "FY", "form": "10-K", "val": 95, "start": "2023-01-01", "end": "2023-12-31", "filed": "2025-02-15", "acc": "0000000000-25-000002"},
            {"fy": 2024, "fp": "FY", "form": "10-K", "val": 110, "start": "2024-01-01", "end": "2024-12-31", "filed": "2025-02-15", "acc": "0000000000-25-000002"}
        ]}}}}}
        selected = {"revenue": {"tag": "Revenues", "unit": "USD", "start": "2023-01-01", "end": "2023-12-31", "value": 100}}
        filings = {"0000000000-24-000001": {"url": "https://sec.example/old"},
                   "0000000000-25-000002": {"url": "https://sec.example/new"}}
        result = reconcile_fact_versions(raw, selected, 2023, filings)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["as_filed"]["value"], 100)
        self.assertEqual(result[0]["latest_comparative"]["value"], 95)
        self.assertEqual(result[0]["review_status"], "possible_restatement_or_reclassification")

    def test_dimensioned_series_are_candidates_not_approved_comparisons(self):
        facts = [{"name": "acme:SegmentRevenue", "unit_ref": "USD", "value": value,
                  "period": {"end": end, "start": f"{end[:4]}-01-01"},
                  "dimensions": [{"dimension": "acme:SegmentsAxis", "member": "acme:RetailMember"}],
                  "source_url": "https://sec.example/filing"}
                 for value, end in ((20, "2023-12-31"), (24, "2024-12-31"))]
        result = compare_dimensioned_periods(facts)
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["items"][0]["periods"]), 2)
        self.assertEqual(result["items"][0]["review_status"], "pending_analyst_review")

    def test_complete_cached_filing_coverage_avoids_repeat_discovery(self):
        class CachedStore:
            def list_documents(self, company_ciks):
                self.requested_ciks = company_ciks
                return [
                    {"document_type": "10-K", "fiscal_period": "10-K 2024"},
                    {"document_type": "10-K", "fiscal_period": "FY2025"},
                ]

        store = CachedStore()
        self.assertTrue(_has_cached_filing_coverage(store, "123", [2024, 2025], ("10-K",)))
        self.assertEqual(store.requested_ciks, ["123"])
        self.assertFalse(_has_cached_filing_coverage(store, "123", [2024, 2025], ("10-K", "10-Q")))

    def test_semantic_claim_judge_batches_findings_in_one_call(self):
        class FakeJudge:
            configured = True

            def __init__(self):
                self.calls = []

            def complete_json(self, prompt, payload, timeout=0):
                self.calls.append((prompt, payload, timeout))
                return {"assessments": [
                    {"id": "0", "assessment": "supported", "reason": "The passage states this."},
                    {"id": "1", "assessment": "supported", "reason": "The passage states this."},
                ]}, None

        judge = FakeJudge()
        risks = [{"company": "Acme", "findings": [
            {"description": "Revenue declined.", "category": "Financial and liquidity",
             "quote": "Revenue declined.", "evidence_ids": ["e1"]},
            {"description": "Costs increased.", "category": "Financial and liquidity",
             "quote": "Costs increased.", "evidence_ids": ["e2"]},
        ]}]
        evidence = {"e1": {"content": "Revenue declined.", "evidence_id": "e1"},
                    "e2": {"content": "Costs increased.", "evidence_id": "e2"}}
        with patch("meridian.agents.settings", replace(settings, claim_judge_enabled=True)), \
             patch("meridian.agents.LLMClient", return_value=judge):
            result = SynthesizerAgent().run(
                [{"company_name": "Acme"}, {"company_name": "Beta"}], risks, [],
                {"metrics": {}}, ["FY2024"], {}, evidence)
        self.assertEqual(len(judge.calls), 1)
        self.assertEqual(judge.calls[0][2], 20.0)
        self.assertEqual(result["validation"]["semantic_claims_judged"], 2)
        self.assertEqual(result["validation"]["claims_requiring_analyst_review"], 0)
        self.assertTrue(all(claim["support_status"] == "supported" for claim in result["claims"]))

    def test_exact_quote_without_judge_is_queued_for_human_entailment_review(self):
        risks = [{"company": "Acme", "findings": [{"description": "Revenue grew.",
            "category": "financial_performance", "quote": "Revenue grew.", "evidence_ids": ["e1"]}]}]
        with patch("meridian.agents.settings", replace(settings, claim_judge_enabled=False)):
            result = SynthesizerAgent().run(
                [{"company_name": "Acme"}, {"company_name": "Beta"}], risks, [],
                {"metrics": {}}, ["FY2024"], {},
                {"e1": {"content": "Revenue grew.", "evidence_id": "e1"}})
        self.assertEqual(result["validation"]["claims_supported"], 1)
        self.assertEqual(result["validation"]["claim_review_queue"]["count"], 1)
        self.assertEqual(result["claims"][0]["support_status"], "requires_analyst_review")


class FastRunTests(unittest.TestCase):
    def test_fast_mode_uses_cached_filings_without_calling_sec(self):
        class StubRegistry:
            companies = {
                "101": Company("Acme", ticker="ACME", cik="101"),
                "102": Company("Beta", ticker="BETA", cik="102"),
            }

            def by_cik(self, cik):
                return self.companies.get(cik)

        class SecMustNotBeCalled:
            def filings(self, *args, **kwargs):
                raise AssertionError("Fast mode called SEC filings")

            def companyfacts(self, *args, **kwargs):
                raise AssertionError("Fast mode called SEC Company Facts")

        class DisabledLLM:
            configured = False

        block = ExtractedBlock(
            "The company faces litigation and regulatory risks. Management plans capital expenditures and market expansion.",
            "paragraph", None, "Item 1A", "risk-strategy", "fixture")
        temp_root = Path(__file__).parent / ".tmp"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as tmp, \
             patch("meridian.embeddings.settings", replace(settings, embedding_provider="hash")), \
             patch("meridian.agents.LLMClient", return_value=DisabledLLM()):
            store = Store(Path(tmp) / "meridian.sqlite3")
            for company in StubRegistry.companies.values():
                store.add_document(company.as_dict(), "10-K", "FY2025",
                    f"{company.company_name} FY2025 filing", "https://sec.example/filing",
                    company.cik, [block])
            request = {"company_ciks": ["101", "102"], "years": [2025],
                "document_types": ["10-K"], "fetch_sec": True, "fast_mode": True}
            run_id = store.create_run(request)
            run_research(run_id, request, StubRegistry(), store, SecMustNotBeCalled())
            run = store.get_run(run_id)
            self.assertEqual(run["status"], "complete")
            self.assertEqual(run["request"]["fetch_sec"], False)
            self.assertEqual(run["result"]["execution_mode"], "fast_cached")
            self.assertTrue(run["result"]["memo"]["evidence"])

    def test_normal_run_reuses_complete_cached_filings_and_facts(self):
        class StubRegistry:
            companies = {
                "101": Company("Acme", ticker="ACME", cik="101"),
                "102": Company("Beta", ticker="BETA", cik="102"),
            }

            def by_cik(self, cik):
                return self.companies.get(cik)

        class SecMustNotBeCalled:
            def filings(self, *args, **kwargs):
                raise AssertionError("Cached filing coverage should skip SEC discovery")

            def companyfacts(self, *args, **kwargs):
                raise AssertionError("Cached XBRL facts should skip SEC retrieval")

        class DisabledLLM:
            configured = False

        filing_block = ExtractedBlock(
            "The company faces litigation and regulatory risks. Management plans capital expenditures and market expansion.",
            "paragraph", None, "Item 1A", "risk-strategy", "fixture")
        xbrl_block = ExtractedBlock(
            "revenue = reported 100 USD | fiscal period FY2025 | SEC tag Revenues",
            "table", None, "Financial statements", "Revenues", "fixture")
        temp_root = Path(__file__).parent / ".tmp"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as tmp, \
             patch("meridian.embeddings.settings", replace(settings, embedding_provider="hash")), \
             patch("meridian.agents.LLMClient", return_value=DisabledLLM()):
            store = Store(Path(tmp) / "meridian.sqlite3")
            for company in StubRegistry.companies.values():
                store.add_document(company.as_dict(), "10-K", "FY2025",
                    f"{company.company_name} FY2025 filing", "https://sec.example/filing",
                    f"filing-{company.cik}", [filing_block])
                store.add_document(company.as_dict(), "XBRL", "FY2025",
                    f"{company.company_name} FY2025 XBRL", "https://sec.example/companyfacts",
                    f"xbrl-{company.cik}", [xbrl_block], metadata={"facts": {
                        "revenue": {"tag": "Revenues", "value": 100.0, "unit": "USD",
                            "reported_value": 100.0, "reported_unit": "USD", "evidence_ids": []}}})
            request = {"company_ciks": ["101", "102"], "years": [2025],
                "document_types": ["10-K"], "fetch_sec": True, "fast_mode": False}
            run_id = store.create_run(request)
            run_research(run_id, request, StubRegistry(), store, SecMustNotBeCalled())
            run = store.get_run(run_id)
            traces = store.traces(run_id)
            self.assertEqual(run["status"], "complete")
            self.assertEqual(run["result"]["execution_mode"], "live_or_cached")
            self.assertEqual(sum(t["status"] == "reused" for t in traces if t["stage"] == "document_acquisition"), 2)
            self.assertEqual(sum(t["status"] == "reused" for t in traces if t["stage"] == "xbrl_ingestion"), 2)


class FXTests(unittest.TestCase):
    def test_annual_rates_normalize_and_preserve_reported_currency(self):
        temp_root = Path(__file__).parent / ".tmp"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as tmp:
            path = Path(tmp) / "fx.csv"
            path.write_text("year,from_currency,to_currency,rate,source,rate_date\n2025,EUR,USD,1.1,Official source,2025-12-31\n", encoding="utf-8")
            value = FXRates(path).normalize(100, "EUR", 2025)
            self.assertAlmostEqual(value["value"], 110)
            self.assertEqual(value["unit"], "USD")
            self.assertEqual(value["reported_value"], 100)
            self.assertEqual(value["fx_source"], "Official source")


class EvaluationTests(unittest.TestCase):
    def test_fixture_metrics_are_bounded_and_report_denominators(self):
        root = Path(__file__).resolve().parents[1]
        result = run_fixture_evaluation(root / "data" / "evaluation_cases.json")
        self.assertEqual(result["dataset_size"], 5)
        self.assertTrue(0 <= result["context_precision"] <= 1)
        self.assertTrue(0 <= result["context_recall"] <= 1)
        self.assertEqual(result["claims_evaluated"], 6)
        self.assertGreater(result["unsupported_claims"], 0)


if __name__ == "__main__":
    unittest.main()
