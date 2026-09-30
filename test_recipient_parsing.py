#!/usr/bin/env python3
"""Comprehensive test suite for CSV and Excel recipient parsing, dynamic column detection, and noise/duplicate cleaning."""

import io
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import openpyxl
import xlrd
from app import app, ALLOWED_LIST_EXTS, safe_child
from sender import (
    clean_and_validate_email,
    parse_recipients,
    parse_recipients_with_stats,
    process_table_records,
)

class TestEmailCleaner(unittest.TestCase):
    def test_clean_valid_emails(self):
        cases = [
            ("user@example.com", "user@example.com"),
            ("  JOHN.DOE@GMAIL.COM  ", "john.doe@gmail.com"),
            ("<alice@company.org>", "alice@company.org"),
            ("Jane Doe <jane.doe@work.co.uk>", "jane.doe@work.co.uk"),
            ("mailto:sales@shop.com", "sales@shop.com"),
            ("'client@test.io'", "client@test.io"),
            ('"support@service.net,"', "support@service.net"),
            ("lead@agency.com.", "lead@agency.com"),
            ("first+tag@domain.com", "first+tag@domain.com"),
            ("a@b.com; c@d.com", "a@b.com"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(clean_and_validate_email(raw), expected)

    def test_drop_invalid_emails_and_noise(self):
        invalids = [
            None,
            "",
            "   ",
            "not-an-email",
            "missing-domain@",
            "@missing-local.com",
            "two@@at.com",
            "user@domain",
            "user@domain..com",
            "user@domain.c",
            "email",
            "Email Address",
            "null",
            "undefined",
            "N/A",
            "none",
            12345,
            float("nan"),
        ]
        for raw in invalids:
            with self.subTest(raw=raw):
                self.assertIsNone(clean_and_validate_email(raw))


class TestRecipientParsing(unittest.TestCase):
    def setUp(self):
        self.test_dir = HERE / "scratch_test_files"
        self.test_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_csv_with_standard_header(self):
        csv_path = self.test_dir / "standard.csv"
        csv_path.write_text(
            "name,email,city\n"
            "Alice,alice@example.com,London\n"
            "Bob,bob@example.com,Paris\n",
            encoding="utf-8"
        )
        recipients, stats = parse_recipients_with_stats(csv_path)
        self.assertEqual(len(recipients), 2)
        self.assertEqual(stats["total_rows"], 2)
        self.assertEqual(stats["valid_count"], 2)
        self.assertEqual(stats["duplicate_count"], 0)
        self.assertEqual(stats["invalid_count"], 0)
        self.assertEqual(stats["detected_column"], "email")
        self.assertEqual(recipients[0]["email"], "alice@example.com")
        self.assertEqual(recipients[0]["name"], "Alice")
        self.assertEqual(recipients[0]["extra"]["city"], "London")

    def test_csv_with_dynamic_header_and_noise(self):
        csv_path = self.test_dir / "noisy.csv"
        csv_path.write_text(
            "# Exported from CRM\n"
            "ID,Full Name,Primary Electronic Mail,City,Notes\n"
            "1,John Doe,  john@domain.com  ,Dhaka,Customer\n"
            "2,Jane Doe,JOHN@DOMAIN.COM,Dhaka,Duplicate!\n"
            "3,Invalid User,not-an-email,Sylhet,Error email\n"
            "4,Empty Email,,Khulna,Missing\n"
            "5,Charlie, <charlie@sample.org> ,Rajshahi,Brackets\n",
            encoding="utf-8"
        )
        recipients, stats = parse_recipients_with_stats(csv_path)
        self.assertEqual(len(recipients), 2)
        self.assertEqual(stats["valid_count"], 2)
        self.assertEqual(stats["duplicate_count"], 1)  # JOHN@DOMAIN.COM
        self.assertEqual(stats["invalid_count"], 2)    # not-an-email, empty
        self.assertEqual(stats["detected_column"], "Primary Electronic Mail")
        self.assertEqual(recipients[0]["email"], "john@domain.com")
        self.assertEqual(recipients[0]["name"], "John Doe")
        self.assertEqual(recipients[1]["email"], "charlie@sample.org")

    def test_csv_no_header_dynamic_detection(self):
        csv_path = self.test_dir / "no_header.csv"
        csv_path.write_text(
            "101,Active,user1@test.com,Dhaka\n"
            "102,Active,user2@test.com,Chittagong\n"
            "103,Inactive,invalid_email,Khulna\n",
            encoding="utf-8"
        )
        recipients, stats = parse_recipients_with_stats(csv_path)
        self.assertEqual(len(recipients), 2)
        self.assertEqual(stats["detected_column"], "Column_3")
        self.assertEqual(stats["invalid_count"], 1)
        self.assertEqual(recipients[0]["email"], "user1@test.com")
        self.assertEqual(recipients[1]["email"], "user2@test.com")

    def test_xlsx_excel_parsing(self):
        xlsx_path = self.test_dir / "recipients.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Clients"
        ws.append(["Client Name", "Contact Mail", "Account ID", "Status"])
        ws.append(["Rahim Khan", "rahim@biz.com", 9001, "Active"])
        ws.append(["Karim Ahmed", "  karim@biz.com  ", 9002, "Active"])
        ws.append(["Duplicate Karim", "KARIM@BIZ.COM", 9003, "Pending"])
        ws.append(["No Mail", "", 9004, "Inactive"])
        ws.append(["Bad Mail", "broken-email", 9005, "Error"])
        wb.save(xlsx_path)

        recipients, stats = parse_recipients_with_stats(xlsx_path)
        self.assertEqual(len(recipients), 2)
        self.assertEqual(stats["total_rows"], 5)
        self.assertEqual(stats["valid_count"], 2)
        self.assertEqual(stats["duplicate_count"], 1)
        self.assertEqual(stats["invalid_count"], 2)
        self.assertEqual(stats["detected_column"], "Contact Mail")
        self.assertEqual(recipients[0]["email"], "rahim@biz.com")
        self.assertEqual(recipients[0]["name"], "Rahim Khan")
        self.assertEqual(recipients[0]["extra"]["Account ID"], "9001")
        self.assertEqual(recipients[1]["email"], "karim@biz.com")
        self.assertEqual(recipients[1]["name"], "Karim Ahmed")

    def test_xls_excel_parsing(self):
        # Test .xls using pandas/xlrd or synthetic rows if xlrd writer is not available
        # We can test process_table_records directly with realistic xlrd-extracted rows
        xls_simulated_rows = [
            ["Customer", "Email Address", "Discount"],
            ["Acme Corp", "orders@acme.com", 15.0],
            ["Beta LLC", "orders@acme.com", 20.0],  # duplicate
            ["Gamma Inc", "gamma@test.org", 10.0],
            ["Delta Bad", "not_valid", 5.0],
        ]
        recipients, stats = process_table_records(xls_simulated_rows)
        self.assertEqual(len(recipients), 2)
        self.assertEqual(stats["valid_count"], 2)
        self.assertEqual(stats["duplicate_count"], 1)
        self.assertEqual(stats["invalid_count"], 1)
        self.assertEqual(stats["detected_column"], "Email Address")
        self.assertEqual(recipients[0]["extra"]["Discount"], "15")


class TestAppUploadFlow(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_allowed_extensions(self):
        for ext in (".csv", ".xlsx", ".xls", ".txt"):
            self.assertIn(ext, ALLOWED_LIST_EXTS)

    def test_upload_csv(self):
        csv_content = b"name,email,city\nTester,tester@domain.com,Sylhet\n"
        data = {"file": (io.BytesIO(csv_content), "test_recipients.csv")}
        res = self.client.post("/upload", data=data, content_type="multipart/form-data")
        self.assertEqual(res.status_code, 302)
        self.assertIn("file_id=", res.location)

        # Follow redirect to index
        idx_res = self.client.get(res.location)
        self.assertEqual(idx_res.status_code, 200)
        self.assertIn(b"tester@domain.com", idx_res.data)
        self.assertIn(b"Valid emails:", idx_res.data)

    def test_upload_excel_xlsx(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Name", "Recipient Email", "City"])
        ws.append(["Excel User", "excel.user@company.com", "Barisal"])
        ws.append(["Excel Duplicate", "EXCEL.USER@COMPANY.COM", "Barisal"])
        ws.append(["Bad Email", "bad-format", "Dhaka"])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        data = {"file": (buf, "recipients_list.xlsx")}
        res = self.client.post("/upload", data=data, content_type="multipart/form-data")
        self.assertEqual(res.status_code, 302)
        self.assertIn("file_id=", res.location)

        # Check preview rendered
        idx_res = self.client.get(res.location)
        self.assertEqual(idx_res.status_code, 200)
        self.assertIn(b"excel.user@company.com", idx_res.data)
        self.assertIn(b"Recipient Email", idx_res.data)
        self.assertIn(b"Duplicates removed:", idx_res.data)
        self.assertIn(b"Invalid / noise dropped:", idx_res.data)

    def test_upload_invalid_extension_rejected(self):
        data = {"file": (io.BytesIO(b"malicious"), "test.exe")}
        res = self.client.post("/upload", data=data, content_type="multipart/form-data")
        self.assertEqual(res.status_code, 302)
        idx_res = self.client.get("/")
        self.assertIn(b"Invalid file extension", idx_res.data)


if __name__ == "__main__":
    unittest.main()
