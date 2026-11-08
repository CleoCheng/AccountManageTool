from src.parser import parse_account_text, parse_domain_email_file, normalize_date_str


def test_parse_chatgpt_3_field():
    raw = "alice_demo01@example.com----DemoPass#2026!----JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    items = parse_account_text(raw)
    assert len(items) == 1
    item = items[0]
    assert item["account_type"] == "ChatGPT"
    assert item["account"] == "alice_demo01@example.com"
    assert item["password"] == "DemoPass#2026!"
    assert item["secret_key"] == "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    assert item["initial_secret_key"] == ""
    assert item["twofa_modified"] == 0
    assert item["recovery_email"] == ""


def test_parse_domain_email_credentials():
    items = parse_account_text("mailbox01@ggb6.com----MailboxPass#2026!")
    assert len(items) == 1
    item = items[0]
    assert item["account_type"] == "域名邮箱"
    assert item["account"] == "mailbox01@ggb6.com"
    assert item["password"] == "MailboxPass#2026!"


def test_parse_google_6_field_with_line_wrap():
    raw = (
        "bob_google99@gmail.com----GoogleDemo#888----recovery_demo@example.org\n"
        "----gezdgnbvgy3tqojqgezdgnbvgy3tqojq----2024----US"
    )
    items = parse_account_text(raw)
    assert len(items) == 1
    item = items[0]
    assert item["account_type"] == "Google"
    assert item["account"] == "bob_google99@gmail.com"
    assert item["password"] == "GoogleDemo#888"
    assert item["recovery_email"] == "recovery_demo@example.org"
    assert item["secret_key"] == "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert item["reg_year"] == "2024"
    assert item["reg_region"] == "US"


def test_parse_batch_mixed_records():
    raw = """
    alice_demo01@example.com----DemoPass#2026!----JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP
    bob_google99@gmail.com----GoogleDemo#888----recovery_demo@example.org----gezdgnbvgy3tqojqgezdgnbvgy3tqojq----2024----US
    """
    items = parse_account_text(raw)
    assert len(items) == 2
    assert items[0]["account_type"] == "ChatGPT"
    assert items[1]["account_type"] == "Google"


def test_parse_heuristic_4_fields_google():
    raw = "user123@gmail.com----pass123----backup@mail.com----GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    items = parse_account_text(raw)
    assert len(items) == 1
    assert items[0]["account_type"] == "Google"
    assert items[0]["recovery_email"] == "backup@mail.com"
    assert items[0]["secret_key"] == "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def test_parse_structured_chatgpt_blocks_with_dates_and_new_2fa():
    raw = """
1.
1021
user_one@example.com
----passOne123----JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP
新2FA----GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ
Workstation-A

2.
1019----1029
user_two@example.com
----passTwo456----JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP
新2FA----GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ
Workstation-B
----1029

3.
1020----1021
user_three@example.com----passThree789----JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP
"""
    items = parse_account_text(raw, default_year=2026)
    assert len(items) == 3

    # Item 1: Active, has new 2FA
    assert items[0]["account"] == "user_one@example.com"
    assert items[0]["purchase_date"] == "2026-10-21"
    assert items[0]["deactivated_date"] == ""
    assert items[0]["initial_secret_key"] == "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    assert items[0]["secret_key"] == "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert items[0]["twofa_modified"] == 1
    assert items[0]["note"] == "Workstation-A"

    # Item 2: Deactivated (1019 to 1029), has new 2FA
    assert items[1]["account"] == "user_two@example.com"
    assert items[1]["purchase_date"] == "2026-10-19"
    assert items[1]["deactivated_date"] == "2026-10-29"
    assert items[1]["twofa_modified"] == 1
    assert items[1]["note"] == "Workstation-B"

    # Item 3: Deactivated (1020 to 1021), initial 2FA only (not modified)
    assert items[2]["account"] == "user_three@example.com"
    assert items[2]["purchase_date"] == "2026-10-20"
    assert items[2]["deactivated_date"] == "2026-10-21"
    assert items[2]["secret_key"] == "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    assert items[2]["initial_secret_key"] == ""
    assert items[2]["twofa_modified"] == 0


def test_parse_pipe_delimiter_chatgpt_and_google():
    # ChatGPT 3-field with |
    raw_gpt = "alice_pipe@example.com|DemoPass#2026!|JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    items_gpt = parse_account_text(raw_gpt)
    assert len(items_gpt) == 1
    assert items_gpt[0]["account_type"] == "ChatGPT"
    assert items_gpt[0]["account"] == "alice_pipe@example.com"
    assert items_gpt[0]["password"] == "DemoPass#2026!"
    assert items_gpt[0]["secret_key"] == "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"

    # Google 6-field with | and optional spaces around |
    raw_google = "bob_pipe@gmail.com | GoogleDemo#888 | recovery_pipe@example.org | gezdgnbvgy3tqojqgezdgnbvgy3tqojq | 2024 | US"
    items_google = parse_account_text(raw_google)
    assert len(items_google) == 1
    assert items_google[0]["account_type"] == "Google"
    assert items_google[0]["account"] == "bob_pipe@gmail.com"
    assert items_google[0]["password"] == "GoogleDemo#888"
    assert items_google[0]["recovery_email"] == "recovery_pipe@example.org"
    assert items_google[0]["secret_key"] == "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert items_google[0]["reg_year"] == "2024"
    assert items_google[0]["reg_region"] == "US"


def test_parse_pipe_delimiter_with_line_wrap():
    raw = (
        "bob_pipe@gmail.com|GoogleDemo#888|recovery_pipe@example.org\n"
        "|gezdgnbvgy3tqojqgezdgnbvgy3tqojq|2024|US"
    )
    items = parse_account_text(raw)
    assert len(items) == 1
    assert items[0]["account"] == "bob_pipe@gmail.com"
    assert items[0]["recovery_email"] == "recovery_pipe@example.org"
    assert items[0]["secret_key"] == "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def test_parse_pipe_in_structured_blocks():
    raw = """
1019|1029
user_pipe@example.com|passTwo456|JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP
新2FA|GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ
Device-PC
|1029
"""
    items = parse_account_text(raw, default_year=2026)
    assert len(items) == 1
    item = items[0]
    assert item["account"] == "user_pipe@example.com"
    assert item["purchase_date"] == "2026-10-19"
    assert item["deactivated_date"] == "2026-10-29"
    assert item["initial_secret_key"] == "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    assert item["secret_key"] == "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert item["twofa_modified"] == 1
    assert item["note"] == "Device-PC"


def test_parse_ggb6_csv_and_xlsx_default_unfinished_status():
    import io
    import zipfile

    csv_raw = (
        "email,password,name,webmail\r\n"
        "liam.simpson@ggb6.com,Pass#2026!,Liam Simpson,https://mail.ggb6.com\r\n"
    ).encode("utf-8-sig")
    csv_items = parse_domain_email_file(csv_raw, filename="ggb6-mailboxes.csv")
    assert len(csv_items) == 1
    assert csv_items[0]["account_type"] == "域名邮箱"
    assert csv_items[0]["account"] == "liam.simpson@ggb6.com"
    assert csv_items[0]["password"] == "Pass#2026!"
    assert csv_items[0]["note"] == "Liam Simpson"
    assert csv_items[0]["custom_fields"]["wb_reg_status"] == "未完成"
    assert csv_items[0]["custom_fields"]["webmail"] == "https://mail.ggb6.com"

    # Build minimal valid .xlsx workbook in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<si><t>email</t></si>"
            "<si><t>password</t></si>"
            "<si><t>name</t></si>"
            "<si><t>webmail</t></si>"
            "<si><t>emma.stone@ggb6.com</t></si>"
            "<si><t>ExcelPass#99</t></si>"
            "<si><t>Emma Stone</t></si>"
            "<si><t>https://mail.ggb6.com</t></si>"
            "</sst>",
        )
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<sheetData>"
            '<row r="1">'
            '<c r="A1" t="s"><v>0</v></c>'
            '<c r="B1" t="s"><v>1</v></c>'
            '<c r="C1" t="s"><v>2</v></c>'
            '<c r="D1" t="s"><v>3</v></c>'
            "</row>"
            '<row r="2">'
            '<c r="A2" t="s"><v>4</v></c>'
            '<c r="B2" t="s"><v>5</v></c>'
            '<c r="C2" t="s"><v>6</v></c>'
            '<c r="D2" t="s"><v>7</v></c>'
            "</row>"
            "</sheetData></worksheet>",
        )
    xlsx_items = parse_domain_email_file(buf.getvalue(), filename="mailboxes.xlsx")
    assert len(xlsx_items) == 1
    assert xlsx_items[0]["account_type"] == "域名邮箱"
    assert xlsx_items[0]["account"] == "emma.stone@ggb6.com"
    assert xlsx_items[0]["password"] == "ExcelPass#99"
    assert xlsx_items[0]["note"] == "Emma Stone"
    assert xlsx_items[0]["custom_fields"]["wb_reg_status"] == "未完成"
