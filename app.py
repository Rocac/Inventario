from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import psycopg
import psycopg.errors
import os
import re
import json
from decimal import Decimal, ROUND_HALF_UP
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
import fitz  # PyMuPDF

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback_secret")

DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME")

CONN_STR = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

INVOICE_UPLOAD_FOLDER = os.path.join("static", "invoices")
ALLOWED_PDF_EXTENSIONS = {"pdf"}

app.config["INVOICE_UPLOAD_FOLDER"] = INVOICE_UPLOAD_FOLDER
os.makedirs(INVOICE_UPLOAD_FOLDER, exist_ok=True)


def get_conn():
    return psycopg.connect(CONN_STR)


def login_required() -> bool:
    return "user_id" in session


PHONE_RE = re.compile(r"^[\d\+\-\(\)\s]{7,20}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
DNI_RE = re.compile(r"^\d{8}$")
RUC_RE = re.compile(r"^\d{11}$")


def validate_phone(phone: str) -> bool:
    if not phone:
        return True
    return bool(PHONE_RE.match(phone))


def validate_email(email: str) -> bool:
    if not email:
        return True
    return bool(EMAIL_RE.match(email))


def validate_dni(dni: str) -> bool:
    if not dni:
        return True
    return bool(DNI_RE.match(dni))


def validate_ruc(ruc: str) -> bool:
    if not ruc:
        return True
    return bool(RUC_RE.match(ruc))


def money(x):
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# =========================
# HELPERS FACTURAS PDF
# =========================

def allowed_pdf_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_PDF_EXTENSIONS


def unique_invoice_filename(original_name: str) -> str:
    base, ext = os.path.splitext(original_name)
    candidate = original_name
    i = 1
    while os.path.exists(os.path.join(app.config["INVOICE_UPLOAD_FOLDER"], candidate)):
        candidate = f"{base}_{i}{ext}"
        i += 1
    return candidate


def extract_text_from_pdf(pdf_path: str) -> str:
    text_parts = []
    try:
        doc = fitz.open(pdf_path)
        for page in doc:
            text_parts.append(page.get_text("text"))
        doc.close()
    except Exception:
        return ""
    return "\n".join(text_parts).strip()


def clean_text_for_parse(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def parse_decimal_text(value: str):
    if not value:
        return 0.0

    value = value.strip()
    value = value.replace("S/", "").replace("$", "").replace("US$", "")
    value = value.replace(" ", "")

    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        value = value.replace(",", ".")

    try:
        return float(value)
    except Exception:
        return 0.0


def normalize_date_for_db(date_str: str) -> str | None:
    if not date_str:
        return None
    date_str = date_str.strip()
    if not date_str:
        return None

    # Ya viene ISO
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
        return date_str

    # dd/mm/yyyy o dd-mm-yyyy -> yyyy-mm-dd
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", date_str)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"

    return None


def auto_extract_invoice_data(raw_text: str):
    text = clean_text_for_parse(raw_text)
    upper_text = text.upper()

    result = {
        "supplier_name": "",
        "supplier_ruc": "",
        "invoice_type": "",
        "invoice_series": "",
        "invoice_number": "",
        "full_number": "",
        "issue_date": "",
        "due_date": "",
        "currency": "PEN",
        "subtotal": 0.0,
        "tax": 0.0,
        "total": 0.0,
        "customer_name": "",
        "customer_ruc": ""
    }

    if "FACTURA ELECTRONICA" in upper_text or "FACTURA ELECTRÓNICA" in upper_text or "FACTURA" in upper_text:
        result["invoice_type"] = "FACTURA"
    elif "BOLETA" in upper_text:
        result["invoice_type"] = "BOLETA"
    elif "RECIBO" in upper_text:
        result["invoice_type"] = "RECIBO"

    if "US$" in upper_text or "USD" in upper_text or "DÓLAR" in upper_text or "DOLAR" in upper_text:
        result["currency"] = "USD"
    else:
        result["currency"] = "PEN"

    supplier_ruc_match = re.search(r"RUC[:\s]*(\d{11})", upper_text)
    if supplier_ruc_match:
        result["supplier_ruc"] = supplier_ruc_match.group(1)

    full_number_match = re.search(r"\b([A-Z]\d{3})[-\s]?(\d+)\b", upper_text)
    if full_number_match:
        result["invoice_series"] = full_number_match.group(1)
        result["invoice_number"] = full_number_match.group(2)
        result["full_number"] = f"{full_number_match.group(1)}-{full_number_match.group(2)}"

    date_match = re.search(
        r"(?:FECHA\s+DE\s+EMISI[ÓO]N|FECHA|EMISI[ÓO]N)\s*:?\s*([0-3]?\d[\/\-][0-1]?\d[\/\-]\d{2,4})",
        upper_text
    )
    if date_match:
        result["issue_date"] = date_match.group(1)

    customer_name_match = re.search(r"SEÑOR\(ES\)\s*:?\s*(.+)", text, re.IGNORECASE)
    if customer_name_match:
        result["customer_name"] = customer_name_match.group(1).strip()

    customer_ruc_match = re.search(r"SEÑOR\(ES\).*?RUC\s*:?\s*(\d{11})", text, re.IGNORECASE | re.DOTALL)
    if customer_ruc_match:
        result["customer_ruc"] = customer_ruc_match.group(1)

    subtotal_match = re.search(r"VALOR VENTA\s*:?\s*S/\s*([\d,]+\.\d{2})", upper_text)
    if subtotal_match:
        result["subtotal"] = parse_decimal_text(subtotal_match.group(1))
    else:
        subtotal_match = re.search(r"SUB TOTAL VENTAS\s*:?\s*S/\s*([\d,]+\.\d{2})", upper_text)
        if subtotal_match:
            result["subtotal"] = parse_decimal_text(subtotal_match.group(1))

    tax_match = re.search(r"\bIGV\b\s*:?\s*S/\s*([\d,]+\.\d{2})", upper_text)
    if tax_match:
        result["tax"] = parse_decimal_text(tax_match.group(1))

    total_match = re.search(r"IMPORTE TOTAL\s*:?\s*S/\s*([\d,]+\.\d{2})", upper_text)
    if total_match:
        result["total"] = parse_decimal_text(total_match.group(1))
    else:
        total_match = re.search(r"\bTOTAL\b\s*:?\s*S/\s*([\d,]+\.\d{2})", upper_text)
        if total_match:
            result["total"] = parse_decimal_text(total_match.group(1))

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    ignored_words = {"FACTURA", "BOLETA", "RECIBO", "RUC", "FECHA", "TOTAL", "SUBTOTAL", "IGV"}

    for line in lines[:10]:
        line_upper = line.upper()
        if len(line) > 4 and not any(word in line_upper for word in ignored_words):
            if not re.fullmatch(r"[\d\W]+", line):
                result["supplier_name"] = line
                break

    return result


# =========================
# AUTENTICACIÓN
# =========================

def get_user(username, password):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username FROM users WHERE username=%s AND password=%s",
                (username, password)
            )
            return cur.fetchone()


# =========================
# CONFIG EMPRESA / DOCS
# =========================

def get_company_settings():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    id,
                    business_name,
                    trade_name,
                    ruc,
                    address,
                    phone,
                    email,
                    tax_rate,
                    currency,
                    logo_url
                FROM company_settings
                ORDER BY id ASC
                LIMIT 1
            """)
            return cur.fetchone()


def get_next_series_number(document_type: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, serie, current_number
                FROM document_series
                WHERE document_type = %s AND active = TRUE
                ORDER BY id ASC
                LIMIT 1
            """, (document_type,))
            row = cur.fetchone()

            if not row:
                raise ValueError(f"No existe una serie activa para {document_type}.")

            series_id, serie, current_number = row
            next_number = (current_number or 0) + 1

            cur.execute("""
                UPDATE document_series
                SET current_number = %s
                WHERE id = %s
            """, (next_number, series_id))

        conn.commit()
        return serie, next_number


def create_electronic_document_record(sale_id: int, document_type: str):
    serie, correlativo = get_next_series_number(document_type)
    full_number = f"{serie}-{str(correlativo).zfill(6)}"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO electronic_documents (
                    sale_id,
                    document_type,
                    serie,
                    correlativo,
                    full_number,
                    sunat_status
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, full_number
            """, (
                sale_id,
                document_type,
                serie,
                correlativo,
                full_number,
                "PENDIENTE"
            ))
            row = cur.fetchone()
            electronic_document_id = row[0]
            full_number = row[1]

            cur.execute("""
                INSERT INTO electronic_document_logs (
                    electronic_document_id,
                    event_type,
                    event_message
                )
                VALUES (%s, %s, %s)
            """, (
                electronic_document_id,
                "CREADO",
                f"Documento electrónico creado: {full_number}"
            ))

            cur.execute("""
                UPDATE sales
                SET document_number = %s
                WHERE id = %s
            """, (full_number, sale_id))

        conn.commit()

    return electronic_document_id, full_number


def get_electronic_document_by_sale_id(sale_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    id,
                    sale_id,
                    document_type,
                    serie,
                    correlativo,
                    full_number,
                    issue_date,
                    xml_path,
                    pdf_path,
                    cdr_path,
                    hash_value,
                    qr_text,
                    sunat_status,
                    sunat_message,
                    created_at
                FROM electronic_documents
                WHERE sale_id = %s
                ORDER BY id DESC
                LIMIT 1
            """, (sale_id,))
            return cur.fetchone()


# =========================
# CATEGORÍAS
# =========================

def list_categories(q: str = ""):
    q = (q or "").strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            if q:
                cur.execute("""
                    SELECT id, name, description
                    FROM categories
                    WHERE name ILIKE %s
                    ORDER BY id ASC
                """, ("%" + q + "%",))
            else:
                cur.execute("""
                    SELECT id, name, description
                    FROM categories
                    ORDER BY id ASC
                """)
            return cur.fetchall()


def get_category_by_id(category_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, description
                FROM categories
                WHERE id = %s
            """, (category_id,))
            return cur.fetchone()


def create_category(name: str, description: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO categories (name, description)
                VALUES (%s, %s)
            """, (name, description or None))
        conn.commit()


def update_category(category_id: int, name: str, description: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE categories
                SET name = %s,
                    description = %s
                WHERE id = %s
            """, (name, description or None, category_id))
        conn.commit()


def delete_category(category_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM products WHERE category_id=%s", (category_id,))
            used = cur.fetchone()[0]
            if used > 0:
                raise ValueError("No se puede eliminar: hay productos usando esta categoría.")

            cur.execute("DELETE FROM categories WHERE id = %s", (category_id,))
        conn.commit()


# =========================
# PROVEEDORES
# =========================

def list_suppliers():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM suppliers ORDER BY name ASC")
            return cur.fetchall()


def list_suppliers_full():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, phone, email, address, notes, ruc
                FROM suppliers
                ORDER BY name ASC
            """)
            return cur.fetchall()


def create_supplier(name: str, phone: str, email: str, address: str, notes: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO suppliers(name, phone, email, address, notes)
                VALUES (%s, %s, %s, %s, %s)
            """, (name, phone or None, email or None, address or None, notes or None))
        conn.commit()


def get_supplier_by_id(supplier_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, phone, email, address, notes, ruc
                FROM suppliers
                WHERE id=%s
            """, (supplier_id,))
            return cur.fetchone()


def update_supplier(supplier_id: int, name: str, phone: str, email: str, address: str, notes: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE suppliers
                SET name=%s, phone=%s, email=%s, address=%s, notes=%s
                WHERE id=%s
            """, (name, phone or None, email or None, address or None, notes or None, supplier_id))
        conn.commit()


def delete_supplier(supplier_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM suppliers WHERE id=%s", (supplier_id,))
        conn.commit()


# =========================
# CLIENTES
# =========================

def list_customers():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM customers ORDER BY name ASC")
            return cur.fetchall()


def list_customers_full():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, phone, dni, ruc, email, address
                FROM customers
                ORDER BY name ASC
            """)
            return cur.fetchall()


def create_customer(name: str, phone: str, dni: str, ruc: str, email: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO customers(name, phone, dni, ruc, email)
                VALUES (%s, %s, %s, %s, %s)
            """, (name, phone or None, dni or None, ruc or None, email or None))
        conn.commit()


def create_customer_quick(name: str, phone: str, dni: str, ruc: str, email: str, address: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO customers(name, phone, dni, ruc, email, address)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (name, phone or None, dni or None, ruc or None, email or None, address or None))
            customer_id = cur.fetchone()[0]
        conn.commit()
        return customer_id


def get_customer_by_id(customer_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, phone, dni, ruc, email, address
                FROM customers
                WHERE id=%s
            """, (customer_id,))
            return cur.fetchone()


def get_customer_full(customer_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, phone, dni, ruc, email, address
                FROM customers
                WHERE id=%s
            """, (customer_id,))
            return cur.fetchone()


def update_customer(customer_id: int, name: str, phone: str, dni: str, ruc: str, email: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE customers
                SET name=%s, phone=%s, dni=%s, ruc=%s, email=%s
                WHERE id=%s
            """, (name, phone or None, dni or None, ruc or None, email or None, customer_id))
        conn.commit()


def delete_customer(customer_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM customers WHERE id=%s", (customer_id,))
        conn.commit()


# =========================
# PRODUCTOS
# =========================

def count_products(search: str):
    q = "%" + search + "%"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*)
                FROM products p
                WHERE (
                    %s = ''
                    OR p.code ILIKE %s
                    OR COALESCE(p.alt_code, '') ILIKE %s
                )
            """, (search, q, q))
            return cur.fetchone()[0]


def list_products(search: str, limit: int, offset: int):
    q = "%" + search + "%"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    p.id,
                    p.code,
                    COALESCE(p.alt_code, ''),
                    p.name,
                    COALESCE(p.category, 'Sin categoría'),
                    COALESCE(s.name, '-'),
                    p.stock,
                    p.price,
                    COALESCE(p.price_usd, 0),
                    COALESCE(p.min_stock, 0)
                FROM products p
                LEFT JOIN suppliers s ON s.id = p.supplier_id
                WHERE (
                    %s = ''
                    OR p.code ILIKE %s
                    OR COALESCE(p.alt_code, '') ILIKE %s
                )
                ORDER BY p.id ASC
                LIMIT %s OFFSET %s
            """, (search, q, q, limit, offset))
            return cur.fetchall()


def get_products_for_sale():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, code, name, stock, price
                FROM products
                ORDER BY name ASC
            """)
            return cur.fetchall()


def create_product(code, alt_code, name, category, category_id, supplier_id, stock, min_stock, price, price_usd):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO products (
                    code, alt_code, name, category, category_id,
                    supplier_id, stock, min_stock, price, price_usd
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                code, alt_code or None, name, category, category_id,
                supplier_id, stock, min_stock, price, price_usd
            ))
        conn.commit()


def get_product_by_id(product_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    id, code, alt_code, name, category, category_id,
                    supplier_id, stock, min_stock, price, price_usd
                FROM products
                WHERE id=%s
            """, (product_id,))
            return cur.fetchone()


def update_product(product_id: int, code, alt_code, name, category, category_id, supplier_id, stock, min_stock, price, price_usd):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE products
                SET code=%s,
                    alt_code=%s,
                    name=%s,
                    category=%s,
                    category_id=%s,
                    supplier_id=%s,
                    stock=%s,
                    min_stock=%s,
                    price=%s,
                    price_usd=%s
                WHERE id=%s
            """, (
                code, alt_code or None, name, category, category_id,
                supplier_id, stock, min_stock, price, price_usd, product_id
            ))
        conn.commit()


def delete_product(product_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM products WHERE id=%s", (product_id,))
        conn.commit()


# =========================
# DASHBOARD / SALIDAS / VENTAS / KARDEX
# =========================

def get_dashboard_summary():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM products")
            total_products = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM categories")
            total_categories = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM suppliers")
            total_suppliers = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM customers")
            total_customers = cur.fetchone()[0]

            cur.execute("SELECT COALESCE(SUM(stock), 0) FROM products")
            total_stock_units = cur.fetchone()[0]

            cur.execute("""
                SELECT code, name, stock, min_stock
                FROM products
                WHERE stock <= min_stock
                ORDER BY stock ASC, name ASC
                LIMIT 10
            """)
            low_stock = cur.fetchall()

            cur.execute("""
                SELECT
                    p.code,
                    p.name,
                    COALESCE(SUM(k.qty), 0) AS total_out
                FROM kardex k
                JOIN products p ON p.id = k.product_id
                WHERE k.movement = 'SALIDA'
                GROUP BY p.code, p.name
                ORDER BY total_out DESC, p.name ASC
                LIMIT 10
            """)
            top_outputs = cur.fetchall()

            cur.execute("""
                SELECT
                    p.code,
                    p.name,
                    COALESCE(SUM(k.qty), 0) AS total_in
                FROM kardex k
                JOIN products p ON p.id = k.product_id
                WHERE k.movement = 'ENTRADA'
                GROUP BY p.code, p.name
                ORDER BY total_in DESC, p.name ASC
                LIMIT 10
            """)
            top_inputs = cur.fetchall()

            cur.execute("""
                SELECT
                    TO_CHAR(k.created_at, 'DD-MM-YYYY HH24:MI:SS') AS created_at,
                    p.code,
                    p.name,
                    k.movement,
                    k.qty,
                    k.stock_after,
                    COALESCE(k.note, '-') AS note
                FROM kardex k
                JOIN products p ON p.id = k.product_id
                ORDER BY k.created_at DESC, k.id DESC
                LIMIT 10
            """)
            recent_moves = cur.fetchall()

    return {
        "total_products": total_products,
        "total_categories": total_categories,
        "total_suppliers": total_suppliers,
        "total_customers": total_customers,
        "total_stock_units": total_stock_units,
        "low_stock": low_stock,
        "top_outputs": top_outputs,
        "top_inputs": top_inputs,
        "recent_moves": recent_moves,
    }


def register_stock_output(product_id: int, qty: int, reason: str, note: str):
    if qty <= 0:
        raise ValueError("La cantidad debe ser mayor a 0.")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, code, name, stock
                FROM products
                WHERE id = %s
            """, (product_id,))
            product = cur.fetchone()

            if not product:
                raise ValueError("El producto no existe.")

            db_product_id, code, name, stock = product

            if stock < qty:
                raise ValueError(f"Stock insuficiente para {name}. Stock actual: {stock}.")

            cur.execute("""
                UPDATE products
                SET stock = stock - %s
                WHERE id = %s
                RETURNING stock
            """, (qty, db_product_id))
            new_stock = cur.fetchone()[0]

            full_note = reason.strip()
            if note.strip():
                full_note = f"{reason.strip()} - {note.strip()}"

            cur.execute("""
                INSERT INTO kardex (product_id, movement, qty, stock_after, ref_table, ref_id, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                db_product_id,
                "SALIDA",
                qty,
                new_stock,
                "manual_output",
                0,
                full_note
            ))

        conn.commit()

    return {
        "product_id": db_product_id,
        "code": code,
        "name": name,
        "qty": qty,
        "stock_after": new_stock
    }


def create_sale_full(
    document_type: str,
    customer_id: int | None,
    customer_name: str,
    customer_doc: str,
    customer_email: str,
    customer_address: str,
    items: list
):
    if not items:
        raise ValueError("Debes agregar al menos un producto.")

    with get_conn() as conn:
        with conn.cursor() as cur:
            subtotal = Decimal("0.00")
            validated_items = []

            for item in items:
                product_id = int(item["product_id"])
                qty = int(item["qty"])

                if qty <= 0:
                    raise ValueError("La cantidad debe ser mayor a 0.")

                cur.execute("""
                    SELECT id, code, name, stock, price
                    FROM products
                    WHERE id=%s
                """, (product_id,))
                product = cur.fetchone()

                if not product:
                    raise ValueError("Uno de los productos no existe.")

                db_product_id, code, name, stock, unit_price = product

                if stock < qty:
                    raise ValueError(f"Stock insuficiente para {name}.")

                unit_price = money(unit_price)
                line_total = money(unit_price * qty)
                subtotal += line_total

                validated_items.append({
                    "product_id": db_product_id,
                    "code": code,
                    "name": name,
                    "qty": qty,
                    "unit_price": unit_price,
                    "line_total": line_total
                })

            company = get_company_settings()
            tax_rate = Decimal("18.00")
            if company and company[7] is not None:
                tax_rate = Decimal(str(company[7]))

            igv = money(subtotal * (tax_rate / Decimal("100")))
            total = money(subtotal + igv)

            cur.execute("""
                INSERT INTO sales (
                    total, customer_id, document_type,
                    subtotal, igv, customer_name, customer_doc,
                    customer_email, customer_address
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                total, customer_id, document_type,
                subtotal, igv, customer_name or None, customer_doc or None,
                customer_email or None, customer_address or None
            ))
            sale_id = cur.fetchone()[0]

            for item in validated_items:
                cur.execute("""
                    INSERT INTO sale_items (sale_id, product_id, qty, unit_price, line_total)
                    VALUES (%s, %s, %s, %s, %s)
                """, (
                    sale_id, item["product_id"], item["qty"],
                    item["unit_price"], item["line_total"]
                ))

                cur.execute("""
                    UPDATE products
                    SET stock = stock - %s
                    WHERE id = %s
                    RETURNING stock
                """, (item["qty"], item["product_id"]))
                new_stock = cur.fetchone()[0]

                cur.execute("""
                    INSERT INTO kardex (product_id, movement, qty, stock_after, ref_table, ref_id, note)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    item["product_id"], "SALIDA", item["qty"], new_stock,
                    "sales", sale_id, f"{document_type} generado"
                ))

        conn.commit()

    electronic_document_id, full_number = create_electronic_document_record(sale_id, document_type)
    return sale_id, electronic_document_id, full_number


def list_sales():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    s.id,
                    TO_CHAR(s.sold_at, 'DD-MM-YYYY HH24:MI:SS') as sold_at,
                    s.total,
                    COALESCE(s.customer_name, c.name, '-') AS customer_name,
                    COALESCE(s.document_type, 'VENTA') AS document_type,
                    COALESCE(s.document_number, '-') AS document_number
                FROM sales s
                LEFT JOIN customers c ON c.id = s.customer_id
                ORDER BY s.id DESC
            """)
            return cur.fetchall()


def get_sale_header(sale_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    s.id,
                    TO_CHAR(s.sold_at, 'DD-MM-YYYY HH24:MI:SS') as sold_at,
                    s.total,
                    COALESCE(s.customer_name, c.name, '-') AS customer_name,
                    COALESCE(s.document_type, 'VENTA') AS document_type,
                    COALESCE(s.document_number, '-') AS document_number,
                    COALESCE(s.subtotal, 0) AS subtotal,
                    COALESCE(s.igv, 0) AS igv,
                    COALESCE(s.customer_doc, '') AS customer_doc,
                    COALESCE(s.customer_email, '') AS customer_email,
                    COALESCE(s.customer_address, '') AS customer_address
                FROM sales s
                LEFT JOIN customers c ON c.id = s.customer_id
                WHERE s.id = %s
            """, (sale_id,))
            return cur.fetchone()


def get_sale_items(sale_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    si.id,
                    si.sale_id,
                    p.code,
                    p.name,
                    si.qty,
                    si.unit_price,
                    si.line_total
                FROM sale_items si
                JOIN products p ON p.id = si.product_id
                WHERE si.sale_id = %s
                ORDER BY si.id ASC
            """, (sale_id,))
            return cur.fetchall()


def list_kardex(product_code: str = "", limit: int = 200):
    with get_conn() as conn:
        with conn.cursor() as cur:
            if product_code:
                cur.execute("""
                    SELECT
                        k.id,
                        k.created_at,
                        p.code,
                        p.name,
                        k.movement,
                        k.qty,
                        k.stock_after,
                        COALESCE(k.ref_table,'-') AS ref_table,
                        COALESCE(k.ref_id, 0) AS ref_id,
                        COALESCE(k.note,'-') AS note
                    FROM kardex k
                    JOIN products p ON p.id = k.product_id
                    WHERE p.code = %s
                    ORDER BY k.created_at DESC, k.id DESC
                    LIMIT %s
                """, (product_code, limit))
            else:
                cur.execute("""
                    SELECT
                        k.id,
                        k.created_at,
                        p.code,
                        p.name,
                        k.movement,
                        k.qty,
                        k.stock_after,
                        COALESCE(k.ref_table,'-') AS ref_table,
                        COALESCE(k.ref_id, 0) AS ref_id,
                        COALESCE(k.note,'-') AS note
                    FROM kardex k
                    JOIN products p ON p.id = k.product_id
                    ORDER BY k.created_at DESC, k.id DESC
                    LIMIT %s
                """, (limit,))
            return cur.fetchall()


def delete_all_sales_history():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM electronic_document_logs")
            cur.execute("DELETE FROM electronic_documents")
            cur.execute("DELETE FROM sale_items")
            cur.execute("DELETE FROM kardex WHERE ref_table IN ('sales', 'manual_output')")
            cur.execute("DELETE FROM sales")
        conn.commit()


def delete_sale_by_id(sale_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM electronic_document_logs
                WHERE electronic_document_id IN (
                    SELECT id FROM electronic_documents WHERE sale_id = %s
                )
            """, (sale_id,))

            cur.execute("""
                DELETE FROM electronic_documents
                WHERE sale_id = %s
            """, (sale_id,))

            cur.execute("""
                DELETE FROM kardex
                WHERE ref_table = 'sales' AND ref_id = %s
            """, (sale_id,))

            cur.execute("""
                DELETE FROM sale_items
                WHERE sale_id = %s
            """, (sale_id,))

            cur.execute("""
                DELETE FROM sales
                WHERE id = %s
            """, (sale_id,))

        conn.commit()


def delete_kardex_move(kardex_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM kardex WHERE id = %s", (kardex_id,))
        conn.commit()


# =========================
# FACTURAS PDF
# =========================

def create_invoice_file_record(original_filename: str, stored_filename: str, file_path: str, file_size: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO invoice_files (
                    original_filename,
                    stored_filename,
                    file_path,
                    mime_type,
                    file_size,
                    status
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                original_filename,
                stored_filename,
                file_path,
                "application/pdf",
                file_size,
                "PENDIENTE"
            ))
            invoice_file_id = cur.fetchone()[0]
        conn.commit()
        return invoice_file_id


def update_invoice_file_status(invoice_file_id: int, status: str, error_message: str = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE invoice_files
                SET status = %s,
                    error_message = %s
                WHERE id = %s
            """, (status, error_message, invoice_file_id))
        conn.commit()


def create_invoice_log(invoice_file_id: int, event_type: str, event_message: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO invoice_logs (invoice_file_id, event_type, event_message)
                VALUES (%s, %s, %s)
            """, (invoice_file_id, event_type, event_message))
        conn.commit()


def create_invoice(
    invoice_file_id: int,
    supplier_id: int | None,
    supplier_name: str,
    supplier_ruc: str,
    invoice_type: str,
    invoice_series: str,
    invoice_number: str,
    full_number: str,
    issue_date,
    due_date,
    currency: str,
    subtotal,
    tax,
    total,
    raw_text: str,
    raw_json
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO invoices (
                    invoice_file_id,
                    supplier_id,
                    supplier_name,
                    supplier_ruc,
                    invoice_type,
                    invoice_series,
                    invoice_number,
                    full_number,
                    issue_date,
                    due_date,
                    currency,
                    subtotal,
                    tax,
                    total,
                    raw_text,
                    raw_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                invoice_file_id,
                supplier_id,
                supplier_name or None,
                supplier_ruc or None,
                invoice_type or None,
                invoice_series or None,
                invoice_number or None,
                full_number or None,
                issue_date or None,
                due_date or None,
                currency or "PEN",
                subtotal or 0,
                tax or 0,
                total or 0,
                raw_text or None,
                json.dumps(raw_json) if raw_json else None
            ))
            invoice_id = cur.fetchone()[0]
        conn.commit()
        return invoice_id


def create_invoice_item(
    invoice_id: int,
    item_order: int,
    description: str,
    qty,
    unit_measure: str,
    unit_price,
    line_total
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO invoice_items (
                    invoice_id,
                    item_order,
                    description,
                    qty,
                    unit_measure,
                    unit_price,
                    line_total
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                invoice_id,
                item_order,
                description,
                qty or 0,
                unit_measure or None,
                unit_price or 0,
                line_total or 0
            ))
        conn.commit()


def list_invoices(q="", ruc="", date_from="", date_to="", total_min="", total_max=""):
    with get_conn() as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT
                    i.id,
                    i.supplier_name,
                    i.supplier_ruc,
                    i.invoice_type,
                    i.full_number,
                    i.issue_date,
                    i.currency,
                    i.total,
                    f.original_filename,
                    f.status
                FROM invoices i
                JOIN invoice_files f ON f.id = i.invoice_file_id
                WHERE 1=1
            """
            params = []

            if q:
                sql += """
                    AND (
                        COALESCE(i.supplier_name, '') ILIKE %s
                        OR COALESCE(i.full_number, '') ILIKE %s
                        OR COALESCE(i.raw_text, '') ILIKE %s
                    )
                """
                like_q = f"%{q}%"
                params.extend([like_q, like_q, like_q])

            if ruc:
                sql += " AND COALESCE(i.supplier_ruc, '') ILIKE %s"
                params.append(f"%{ruc}%")

            if date_from:
                sql += " AND i.issue_date >= %s"
                params.append(date_from)

            if date_to:
                sql += " AND i.issue_date <= %s"
                params.append(date_to)

            if total_min:
                sql += " AND i.total >= %s"
                params.append(total_min)

            if total_max:
                sql += " AND i.total <= %s"
                params.append(total_max)

            sql += " ORDER BY i.id DESC"

            cur.execute(sql, params)
            return cur.fetchall()


def get_invoice_by_id(invoice_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    i.id,
                    i.invoice_file_id,
                    i.supplier_id,
                    i.supplier_name,
                    i.supplier_ruc,
                    i.invoice_type,
                    i.invoice_series,
                    i.invoice_number,
                    i.full_number,
                    i.issue_date,
                    i.due_date,
                    i.currency,
                    i.subtotal,
                    i.tax,
                    i.total,
                    i.raw_text,
                    i.raw_json,
                    i.created_at,
                    f.original_filename,
                    f.stored_filename,
                    f.file_path,
                    f.status
                FROM invoices i
                JOIN invoice_files f ON f.id = i.invoice_file_id
                WHERE i.id = %s
            """, (invoice_id,))
            return cur.fetchone()


def get_invoice_items(invoice_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    id,
                    item_order,
                    description,
                    qty,
                    unit_measure,
                    unit_price,
                    line_total
                FROM invoice_items
                WHERE invoice_id = %s
                ORDER BY item_order ASC, id ASC
            """, (invoice_id,))
            return cur.fetchall()


def update_invoice(
    invoice_id: int,
    supplier_id: int | None,
    supplier_name: str,
    supplier_ruc: str,
    invoice_type: str,
    invoice_series: str,
    invoice_number: str,
    full_number: str,
    issue_date,
    due_date,
    currency: str,
    subtotal,
    tax,
    total
):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE invoices
                SET supplier_id = %s,
                    supplier_name = %s,
                    supplier_ruc = %s,
                    invoice_type = %s,
                    invoice_series = %s,
                    invoice_number = %s,
                    full_number = %s,
                    issue_date = %s,
                    due_date = %s,
                    currency = %s,
                    subtotal = %s,
                    tax = %s,
                    total = %s,
                    updated_at = NOW()
                WHERE id = %s
            """, (
                supplier_id,
                supplier_name or None,
                supplier_ruc or None,
                invoice_type or None,
                invoice_series or None,
                invoice_number or None,
                full_number or None,
                issue_date or None,
                due_date or None,
                currency or "PEN",
                subtotal or 0,
                tax or 0,
                total or 0,
                invoice_id
            ))
        conn.commit()


def delete_invoice(invoice_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT invoice_file_id FROM invoices WHERE id = %s", (invoice_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError("Factura no encontrada.")

            invoice_file_id = row[0]

            cur.execute("""
                SELECT stored_filename, file_path
                FROM invoice_files
                WHERE id = %s
            """, (invoice_file_id,))
            file_row = cur.fetchone()

            cur.execute("DELETE FROM invoices WHERE id = %s", (invoice_id,))
        conn.commit()

    return file_row


# =========================
# RUTAS
# =========================

@app.route("/")
def home():
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        user = get_user(username, password)
        if user:
            session["user_id"] = user[0]
            session["username"] = user[1]
            return redirect(url_for("dashboard"))

        flash("Usuario o contraseña incorrectos", "error")
        return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/inicio")
def dashboard():
    if not login_required():
        return redirect(url_for("login"))

    data = get_dashboard_summary()
    return render_template(
        "dashboard.html",
        username=session.get("username"),
        data=data
    )


@app.route("/categorias")
def categories_list():
    if not login_required():
        return redirect(url_for("login"))

    q = request.args.get("q", "").strip()
    categories = list_categories(q)

    return render_template(
        "categories.html",
        username=session.get("username"),
        categories=categories,
        q=q
    )


@app.route("/categorias/nuevo", methods=["GET", "POST"])
def categories_new():
    if not login_required():
        return redirect(url_for("login"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()

        if not name:
            flash("El nombre es obligatorio.", "error")
            return redirect(url_for("categories_new"))

        try:
            create_category(name, description)
        except psycopg.errors.UniqueViolation:
            flash("Ya existe una categoría con ese nombre.", "error")
            return redirect(url_for("categories_new"))
        except Exception as e:
            flash(f"Error creando categoría: {e}", "error")
            return redirect(url_for("categories_new"))

        flash("✅ Categoría registrada.", "ok")
        return redirect(url_for("categories_list"))

    return render_template("category_new.html", username=session.get("username"))


@app.route("/categorias/<int:category_id>/editar", methods=["GET", "POST"])
def categories_edit(category_id):
    if not login_required():
        return redirect(url_for("login"))

    cat = get_category_by_id(category_id)
    if not cat:
        flash("Categoría no encontrada.", "error")
        return redirect(url_for("categories_list"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()

        if not name:
            flash("El nombre es obligatorio.", "error")
            return redirect(url_for("categories_edit", category_id=category_id))

        try:
            update_category(category_id, name, description)
        except psycopg.errors.UniqueViolation:
            flash("Ya existe otra categoría con ese nombre.", "error")
            return redirect(url_for("categories_edit", category_id=category_id))
        except Exception as e:
            flash(f"Error actualizando categoría: {e}", "error")
            return redirect(url_for("categories_edit", category_id=category_id))

        flash("✅ Categoría actualizada.", "ok")
        return redirect(url_for("categories_list"))

    return render_template("category_edit.html", username=session.get("username"), cat=cat)


@app.route("/categorias/<int:category_id>/eliminar", methods=["POST"])
def categories_delete(category_id):
    if not login_required():
        return redirect(url_for("login"))

    try:
        delete_category(category_id)
    except ValueError as ve:
        flash(str(ve), "error")
        return redirect(url_for("categories_list"))
    except Exception as e:
        flash(f"Error eliminando categoría: {e}", "error")
        return redirect(url_for("categories_list"))

    flash("🗑 Categoría eliminada.", "ok")
    return redirect(url_for("categories_list"))


@app.route("/productos")
def products_list():
    if not login_required():
        return redirect(url_for("login"))

    search = request.args.get("q", "").strip()

    try:
        per_page = int(request.args.get("per_page", "10"))
        if per_page not in (5, 10, 25, 50):
            per_page = 10
    except Exception:
        per_page = 10

    try:
        page = int(request.args.get("page", "1"))
        if page < 1:
            page = 1
    except Exception:
        page = 1

    total = count_products(search)
    total_pages = max(1, (total + per_page - 1) // per_page)

    if page > total_pages:
        page = total_pages

    offset = (page - 1) * per_page
    rows = list_products(search, per_page, offset)

    return render_template(
        "products.html",
        username=session.get("username"),
        products=rows,
        search=search,
        per_page=per_page,
        page=page,
        total=total,
        total_pages=total_pages
    )


@app.route("/productos/nuevo", methods=["GET", "POST"])
def products_new():
    if not login_required():
        return redirect(url_for("login"))

    categories = list_categories()
    suppliers = list_suppliers()

    if request.method == "POST":
        code = request.form.get("code", "").strip()
        alt_code = request.form.get("alt_code", "").strip()
        name = request.form.get("name", "").strip()

        category_id_raw = request.form.get("category_id", "").strip()
        category_id = int(category_id_raw) if category_id_raw else None

        supplier_id_raw = request.form.get("supplier_id", "").strip()
        supplier_id = int(supplier_id_raw) if supplier_id_raw else None

        stock_raw = request.form.get("stock", "0").strip()
        min_stock_raw = request.form.get("min_stock", "0").strip()
        price_raw = request.form.get("price", "0").strip()
        price_usd_raw = request.form.get("price_usd", "0").strip()

        if not code:
            flash("El código es obligatorio.", "error")
            return redirect(url_for("products_new"))

        if not name:
            flash("El nombre es obligatorio.", "error")
            return redirect(url_for("products_new"))

        if not category_id:
            flash("Debes seleccionar una categoría.", "error")
            return redirect(url_for("products_new"))

        category_row = get_category_by_id(category_id)
        if not category_row:
            flash("La categoría seleccionada no existe.", "error")
            return redirect(url_for("products_new"))

        category = category_row[1]

        try:
            stock = int(stock_raw)
            if stock < 0:
                raise ValueError
        except Exception:
            flash("Stock inválido (0 o más).", "error")
            return redirect(url_for("products_new"))

        try:
            min_stock = int(min_stock_raw)
            if min_stock < 0:
                raise ValueError
        except Exception:
            flash("Stock mínimo inválido (0 o más).", "error")
            return redirect(url_for("products_new"))

        try:
            price = float(price_raw.replace(",", "."))
            if price < 0:
                raise ValueError
        except Exception:
            flash("Precio inválido (0 o más).", "error")
            return redirect(url_for("products_new"))

        try:
            price_usd = float(price_usd_raw.replace(",", "."))
            if price_usd < 0:
                raise ValueError
        except Exception:
            flash("Precio en dólares inválido (0 o más).", "error")
            return redirect(url_for("products_new"))

        try:
            create_product(
                code, alt_code, name, category, category_id,
                supplier_id, stock, min_stock, price, price_usd
            )
        except psycopg.errors.UniqueViolation:
            flash("Ese código ya existe. Usa otro código.", "error")
            return redirect(url_for("products_new"))
        except Exception as e:
            flash(f"Error guardando producto: {e}", "error")
            return redirect(url_for("products_new"))

        flash("✅ Producto registrado.", "ok")
        return redirect(url_for("products_list"))

    return render_template(
        "product_new.html",
        username=session.get("username"),
        categories=categories,
        suppliers=suppliers
    )


@app.route("/productos/<int:product_id>/editar", methods=["GET", "POST"])
def products_edit(product_id):
    if not login_required():
        return redirect(url_for("login"))

    p = get_product_by_id(product_id)
    if not p:
        flash("Producto no encontrado.", "error")
        return redirect(url_for("products_list"))

    categories = list_categories()
    suppliers = list_suppliers()

    if request.method == "POST":
        code = request.form.get("code", "").strip()
        alt_code = request.form.get("alt_code", "").strip()
        name = request.form.get("name", "").strip()

        category_id_raw = request.form.get("category_id", "").strip()
        category_id = int(category_id_raw) if category_id_raw else None

        supplier_id_raw = request.form.get("supplier_id", "").strip()
        supplier_id = int(supplier_id_raw) if supplier_id_raw else None

        stock_raw = request.form.get("stock", "0").strip()
        min_stock_raw = request.form.get("min_stock", "0").strip()
        price_raw = request.form.get("price", "0").strip()
        price_usd_raw = request.form.get("price_usd", "0").strip()

        if not code:
            flash("El código es obligatorio.", "error")
            return redirect(url_for("products_edit", product_id=product_id))

        if not name:
            flash("El nombre es obligatorio.", "error")
            return redirect(url_for("products_edit", product_id=product_id))

        if not category_id:
            flash("Debes seleccionar una categoría.", "error")
            return redirect(url_for("products_edit", product_id=product_id))

        category_row = get_category_by_id(category_id)
        if not category_row:
            flash("La categoría seleccionada no existe.", "error")
            return redirect(url_for("products_edit", product_id=product_id))

        category = category_row[1]

        try:
            stock = int(stock_raw)
            if stock < 0:
                raise ValueError
        except Exception:
            flash("Stock inválido (0 o más).", "error")
            return redirect(url_for("products_edit", product_id=product_id))

        try:
            min_stock = int(min_stock_raw)
            if min_stock < 0:
                raise ValueError
        except Exception:
            flash("Stock mínimo inválido (0 o más).", "error")
            return redirect(url_for("products_edit", product_id=product_id))

        try:
            price = float(price_raw.replace(",", "."))
            if price < 0:
                raise ValueError
        except Exception:
            flash("Precio inválido (0 o más).", "error")
            return redirect(url_for("products_edit", product_id=product_id))

        try:
            price_usd = float(price_usd_raw.replace(",", "."))
            if price_usd < 0:
                raise ValueError
        except Exception:
            flash("Precio en dólares inválido (0 o más).", "error")
            return redirect(url_for("products_edit", product_id=product_id))

        try:
            update_product(
                product_id,
                code,
                alt_code,
                name,
                category,
                category_id,
                supplier_id,
                stock,
                min_stock,
                price,
                price_usd
            )
        except psycopg.errors.UniqueViolation:
            flash("Ese código ya existe. Usa otro código.", "error")
            return redirect(url_for("products_edit", product_id=product_id))
        except Exception as e:
            flash(f"Error actualizando producto: {e}", "error")
            return redirect(url_for("products_edit", product_id=product_id))

        flash("✅ Producto actualizado.", "ok")
        return redirect(url_for("products_list"))

    return render_template(
        "product_edit.html",
        username=session.get("username"),
        p=p,
        categories=categories,
        suppliers=suppliers
    )


@app.route("/productos/<int:product_id>/eliminar", methods=["POST"])
def products_delete(product_id):
    if not login_required():
        return redirect(url_for("login"))

    try:
        delete_product(product_id)
        flash("🗑 Producto eliminado.", "ok")
    except psycopg.errors.ForeignKeyViolation:
        flash("❌ No se puede eliminar este producto porque ya está registrado en una venta.", "error")
    except Exception as e:
        flash(f"Error eliminando producto: {e}", "error")

    return redirect(url_for("products_list"))


@app.route("/proveedores")
def proveedores():
    if not login_required():
        return redirect(url_for("login"))

    suppliers = list_suppliers_full()
    return render_template("suppliers.html", username=session.get("username"), suppliers=suppliers)


@app.route("/proveedores/nuevo", methods=["GET", "POST"])
def proveedores_nuevo():
    if not login_required():
        return redirect(url_for("login"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        address = request.form.get("address", "").strip()
        notes = request.form.get("notes", "").strip()

        if not name:
            flash("El nombre del proveedor es obligatorio.", "error")
            return redirect(url_for("proveedores_nuevo"))

        if phone and not validate_phone(phone):
            flash("El teléfono puede tener entre 7 y 20 caracteres y usar números, +, -, paréntesis y espacios.", "error")
            return redirect(url_for("proveedores_nuevo"))

        if email and not validate_email(email):
            flash("El correo no es válido.", "error")
            return redirect(url_for("proveedores_nuevo"))

        try:
            create_supplier(name, phone, email, address, notes)
        except Exception as e:
            flash(f"Error registrando proveedor: {e}", "error")
            return redirect(url_for("proveedores_nuevo"))

        flash("✅ Proveedor registrado.", "ok")
        return redirect(url_for("proveedores"))

    return render_template("supplier_new.html", username=session.get("username"))


@app.route("/proveedores/<int:supplier_id>/editar", methods=["GET", "POST"])
def proveedores_edit(supplier_id):
    if not login_required():
        return redirect(url_for("login"))

    supplier = get_supplier_by_id(supplier_id)
    if not supplier:
        flash("Proveedor no encontrado.", "error")
        return redirect(url_for("proveedores"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        address = request.form.get("address", "").strip()
        notes = request.form.get("notes", "").strip()

        if not name:
            flash("El nombre del proveedor es obligatorio.", "error")
            return redirect(url_for("proveedores_edit", supplier_id=supplier_id))

        if phone and not validate_phone(phone):
            flash("El teléfono puede tener entre 7 y 20 caracteres y usar números, +, -, paréntesis y espacios.", "error")
            return redirect(url_for("proveedores_edit", supplier_id=supplier_id))

        if email and not validate_email(email):
            flash("El correo no es válido.", "error")
            return redirect(url_for("proveedores_edit", supplier_id=supplier_id))

        try:
            update_supplier(supplier_id, name, phone, email, address, notes)
        except Exception as e:
            flash(f"Error actualizando proveedor: {e}", "error")
            return redirect(url_for("proveedores_edit", supplier_id=supplier_id))

        flash("✅ Proveedor actualizado.", "ok")
        return redirect(url_for("proveedores"))

    return render_template("supplier_edit.html", username=session.get("username"), supplier=supplier)


@app.route("/proveedores/<int:supplier_id>/eliminar", methods=["POST"])
def proveedores_delete(supplier_id):
    if not login_required():
        return redirect(url_for("login"))

    try:
        delete_supplier(supplier_id)
    except psycopg.errors.ForeignKeyViolation:
        flash("No se puede eliminar: este proveedor está asignado a productos.", "error")
        return redirect(url_for("proveedores"))
    except Exception as e:
        flash(f"Error eliminando proveedor: {e}", "error")
        return redirect(url_for("proveedores"))

    flash("🗑 Proveedor eliminado.", "ok")
    return redirect(url_for("proveedores"))


@app.route("/clientes")
def clientes():
    if not login_required():
        return redirect(url_for("login"))

    customers = list_customers_full()
    return render_template("customers.html", username=session.get("username"), customers=customers)


@app.route("/clientes/nuevo", methods=["GET", "POST"])
def cliente_nuevo():
    if not login_required():
        return redirect(url_for("login"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        dni = request.form.get("dni", "").strip()
        ruc = request.form.get("ruc", "").strip()
        email = request.form.get("email", "").strip()

        if not name:
            flash("El nombre del cliente es obligatorio.", "error")
            return redirect(url_for("cliente_nuevo"))

        if phone and not validate_phone(phone):
            flash("El teléfono puede tener entre 7 y 20 caracteres y usar números, +, -, paréntesis y espacios.", "error")
            return redirect(url_for("cliente_nuevo"))

        if dni and not validate_dni(dni):
            flash("El DNI debe tener exactamente 8 dígitos.", "error")
            return redirect(url_for("cliente_nuevo"))

        if ruc and not validate_ruc(ruc):
            flash("El RUC debe tener exactamente 11 dígitos.", "error")
            return redirect(url_for("cliente_nuevo"))

        if email and not validate_email(email):
            flash("El correo no es válido.", "error")
            return redirect(url_for("cliente_nuevo"))

        try:
            create_customer(name, phone, dni, ruc, email)
        except Exception as e:
            flash(f"Error registrando cliente: {e}", "error")
            return redirect(url_for("cliente_nuevo"))

        flash("✅ Cliente registrado.", "ok")
        return redirect(url_for("clientes"))

    return render_template("customer_new.html", username=session.get("username"))


@app.route("/clientes/<int:customer_id>/editar", methods=["GET", "POST"])
def cliente_edit(customer_id):
    if not login_required():
        return redirect(url_for("login"))

    customer = get_customer_by_id(customer_id)
    if not customer:
        flash("Cliente no encontrado.", "error")
        return redirect(url_for("clientes"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        dni = request.form.get("dni", "").strip()
        ruc = request.form.get("ruc", "").strip()
        email = request.form.get("email", "").strip()

        if not name:
            flash("El nombre del cliente es obligatorio.", "error")
            return redirect(url_for("cliente_edit", customer_id=customer_id))

        if phone and not validate_phone(phone):
            flash("El teléfono puede tener entre 7 y 20 caracteres y usar números, +, -, paréntesis y espacios.", "error")
            return redirect(url_for("cliente_edit", customer_id=customer_id))

        if dni and not validate_dni(dni):
            flash("El DNI debe tener exactamente 8 dígitos.", "error")
            return redirect(url_for("cliente_edit", customer_id=customer_id))

        if ruc and not validate_ruc(ruc):
            flash("El RUC debe tener exactamente 11 dígitos.", "error")
            return redirect(url_for("cliente_edit", customer_id=customer_id))

        if email and not validate_email(email):
            flash("El correo no es válido.", "error")
            return redirect(url_for("cliente_edit", customer_id=customer_id))

        try:
            update_customer(customer_id, name, phone, dni, ruc, email)
        except Exception as e:
            flash(f"Error actualizando cliente: {e}", "error")
            return redirect(url_for("cliente_edit", customer_id=customer_id))

        flash("✅ Cliente actualizado.", "ok")
        return redirect(url_for("clientes"))

    return render_template("customer_edit.html", username=session.get("username"), customer=customer)


@app.route("/clientes/<int:customer_id>/eliminar", methods=["POST"])
def cliente_delete(customer_id):
    if not login_required():
        return redirect(url_for("login"))

    try:
        delete_customer(customer_id)
    except psycopg.errors.ForeignKeyViolation:
        flash("No se puede eliminar: este cliente está asociado a ventas.", "error")
        return redirect(url_for("clientes"))
    except Exception as e:
        flash(f"Error eliminando cliente: {e}", "error")
        return redirect(url_for("clientes"))

    flash("🗑 Cliente eliminado.", "ok")
    return redirect(url_for("clientes"))


@app.route("/salidas", methods=["GET", "POST"])
def stock_out():
    if not login_required():
        return redirect(url_for("login"))

    products = get_products_for_sale()

    if request.method == "POST":
        product_id_raw = request.form.get("product_id", "").strip()
        qty_raw = request.form.get("qty", "").strip()
        reason = request.form.get("reason", "").strip()
        note = ""

        if not product_id_raw:
            flash("Debes seleccionar un producto.", "error")
            return redirect(url_for("stock_out"))

        if not reason:
            flash("Debes seleccionar un motivo.", "error")
            return redirect(url_for("stock_out"))

        try:
            product_id = int(product_id_raw)
        except Exception:
            flash("Producto inválido.", "error")
            return redirect(url_for("stock_out"))

        try:
            qty = int(qty_raw)
        except Exception:
            flash("Cantidad inválida.", "error")
            return redirect(url_for("stock_out"))

        try:
            result = register_stock_output(product_id, qty, reason, note)
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("stock_out"))
        except Exception as e:
            flash(f"Error registrando salida: {e}", "error")
            return redirect(url_for("stock_out"))

        flash(
            f"✅ Salida registrada: {result['qty']} unidad(es) de {result['name']}. Stock actual: {result['stock_after']}",
            "ok"
        )
        return redirect(url_for("stock_out"))

    return render_template(
        "stock_out.html",
        username=session.get("username"),
        products=products
    )


@app.route("/ventas/nueva", methods=["GET", "POST"])
def venta_nueva():
    if not login_required():
        return redirect(url_for("login"))

    customers = list_customers_full()
    products = get_products_for_sale()

    if request.method == "POST":
        document_type = request.form.get("document_type", "VENTA").strip().upper()

        customer_id_raw = request.form.get("customer_id", "").strip()
        customer_id = int(customer_id_raw) if customer_id_raw else None

        customer_name = request.form.get("customer_name", "").strip()
        customer_dni = request.form.get("customer_dni", "").strip()
        customer_ruc = request.form.get("customer_ruc", "").strip()
        customer_phone = request.form.get("customer_phone", "").strip()
        customer_email = request.form.get("customer_email", "").strip()
        customer_address = request.form.get("customer_address", "").strip()

        items_json = request.form.get("items_json", "[]").strip()

        try:
            items = json.loads(items_json)
        except Exception:
            items = []

        if customer_id:
            customer = get_customer_full(customer_id)
            if customer:
                customer_name = customer[1] or customer_name
                customer_dni = customer[3] or customer_dni
                customer_ruc = customer[4] or customer_ruc
                customer_email = customer[5] or customer_email
                customer_address = customer[6] or customer_address
        else:
            if not customer_name:
                flash("Debes ingresar el nombre o razón social del cliente.", "error")
                return redirect(url_for("venta_nueva"))

            if customer_phone and not validate_phone(customer_phone):
                flash("El teléfono del cliente puede tener entre 7 y 20 caracteres y usar números, +, -, paréntesis y espacios.", "error")
                return redirect(url_for("venta_nueva"))

            if customer_dni and not validate_dni(customer_dni):
                flash("El DNI del cliente debe tener 8 dígitos.", "error")
                return redirect(url_for("venta_nueva"))

            if customer_ruc and not validate_ruc(customer_ruc):
                flash("El RUC del cliente debe tener 11 dígitos.", "error")
                return redirect(url_for("venta_nueva"))

            if customer_email and not validate_email(customer_email):
                flash("El correo del cliente no es válido.", "error")
                return redirect(url_for("venta_nueva"))

            customer_id = create_customer_quick(
                customer_name,
                customer_phone,
                customer_dni,
                customer_ruc,
                customer_email,
                customer_address
            )

        if document_type == "FACTURA":
            customer_doc = customer_ruc
            if not customer_name:
                flash("Debes ingresar la razón social del cliente.", "error")
                return redirect(url_for("venta_nueva"))
            if not validate_ruc(customer_doc):
                flash("Para FACTURA debes ingresar un RUC válido de 11 dígitos.", "error")
                return redirect(url_for("venta_nueva"))

        elif document_type == "BOLETA":
            customer_doc = customer_dni
            if not customer_name:
                flash("Debes ingresar el nombre del cliente.", "error")
                return redirect(url_for("venta_nueva"))
            if not validate_dni(customer_doc):
                flash("Para BOLETA debes ingresar un DNI válido de 8 dígitos.", "error")
                return redirect(url_for("venta_nueva"))

        else:
            customer_doc = customer_ruc or customer_dni

        try:
            sale_id, electronic_document_id, full_number = create_sale_full(
                document_type=document_type,
                customer_id=customer_id,
                customer_name=customer_name,
                customer_doc=customer_doc,
                customer_email=customer_email,
                customer_address=customer_address,
                items=items
            )
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("venta_nueva"))
        except Exception as e:
            flash(f"Error registrando venta: {e}", "error")
            return redirect(url_for("venta_nueva"))

        flash(f"✅ {document_type} registrada correctamente. Nº {full_number}", "ok")
        return redirect(url_for("venta_comprobante", sale_id=sale_id))

    return render_template(
        "sale_new.html",
        username=session.get("username"),
        customers=customers,
        products=products
    )


@app.route("/ventas")
def ventas():
    if not login_required():
        return redirect(url_for("login"))

    sales = list_sales()
    return render_template("sales.html", username=session.get("username"), sales=sales)


@app.route("/ventas/eliminar_historial", methods=["POST"])
def ventas_delete_all():
    if not login_required():
        return redirect(url_for("login"))

    try:
        delete_all_sales_history()
        flash("🗑 Historial de ventas eliminado correctamente.", "ok")
    except Exception as e:
        flash(f"Error eliminando historial de ventas: {e}", "error")

    return redirect(url_for("ventas"))


@app.route("/ventas/<int:sale_id>/eliminar", methods=["POST"])
def ventas_delete_one(sale_id):
    if not login_required():
        return redirect(url_for("login"))

    try:
        delete_sale_by_id(sale_id)
        flash("🗑 Venta eliminada correctamente.", "ok")
    except Exception as e:
        flash(f"Error eliminando venta: {e}", "error")

    return redirect(url_for("ventas"))


@app.route("/ventas/<int:sale_id>")
def venta_detalle(sale_id):
    if not login_required():
        return redirect(url_for("login"))

    header = get_sale_header(sale_id)
    if not header:
        return jsonify({"ok": False, "message": "Venta no encontrada"}), 404

    items = get_sale_items(sale_id)
    edoc = get_electronic_document_by_sale_id(sale_id)

    return jsonify({
        "ok": True,
        "header": {
            "id": header[0],
            "sold_at": header[1],
            "total": float(header[2]),
            "customer_name": header[3],
            "document_type": header[4],
            "document_number": header[5],
            "subtotal": float(header[6]),
            "igv": float(header[7]),
            "customer_doc": header[8],
            "customer_email": header[9],
            "customer_address": header[10]
        },
        "electronic_document": {
            "id": edoc[0],
            "full_number": edoc[5],
            "sunat_status": edoc[12],
            "sunat_message": edoc[13]
        } if edoc else None,
        "items": [
            {
                "id": item[0],
                "sale_id": item[1],
                "code": item[2],
                "name": item[3],
                "qty": item[4],
                "unit_price": float(item[5]),
                "line_total": float(item[6])
            }
            for item in items
        ]
    })


@app.route("/ventas/<int:sale_id>/comprobante")
def venta_comprobante(sale_id):
    if not login_required():
        return redirect(url_for("login"))

    header = get_sale_header(sale_id)
    if not header:
        flash("Comprobante no encontrado.", "error")
        return redirect(url_for("ventas"))

    items = get_sale_items(sale_id)
    company = get_company_settings()
    edoc = get_electronic_document_by_sale_id(sale_id)

    return render_template(
        "sale_receipt.html",
        username=session.get("username"),
        header=header,
        items=items,
        company=company,
        edoc=edoc
    )


@app.route("/kardex")
def kardex():
    if not login_required():
        return redirect(url_for("login"))

    code = request.args.get("code", "").strip()
    rows = list_kardex(product_code=code, limit=200)

    return render_template(
        "kardex.html",
        username=session.get("username"),
        code=code,
        rows=rows
    )


@app.route("/kardex/<int:kardex_id>/eliminar", methods=["POST"])
def kardex_delete(kardex_id):
    if not login_required():
        return redirect(url_for("login"))

    try:
        delete_kardex_move(kardex_id)
        flash("🗑 Movimiento de kardex eliminado.", "ok")
    except Exception as e:
        flash(f"Error eliminando movimiento: {e}", "error")

    return redirect(url_for("kardex"))


# =========================
# FACTURAS PDF - RUTAS
# =========================

@app.route("/facturas")
def invoices_list():
    if not login_required():
        return redirect(url_for("login"))

    q = request.args.get("q", "").strip()
    ruc = request.args.get("ruc", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    total_min = request.args.get("total_min", "").strip()
    total_max = request.args.get("total_max", "").strip()

    rows = list_invoices(q, ruc, date_from, date_to, total_min, total_max)

    return render_template(
        "invoices.html",
        username=session.get("username"),
        invoices=rows,
        q=q,
        ruc=ruc,
        date_from=date_from,
        date_to=date_to,
        total_min=total_min,
        total_max=total_max
    )


@app.route("/facturas/nueva", methods=["GET", "POST"])
def invoice_new():
    if not login_required():
        return redirect(url_for("login"))

    suppliers = list_suppliers_full()

    if request.method == "POST":
        supplier_id_raw = request.form.get("supplier_id", "").strip()
        supplier_id = int(supplier_id_raw) if supplier_id_raw else None

        supplier_name = request.form.get("supplier_name", "").strip()
        supplier_ruc = request.form.get("supplier_ruc", "").strip()
        invoice_type = request.form.get("invoice_type", "").strip()
        invoice_series = request.form.get("invoice_series", "").strip()
        invoice_number = request.form.get("invoice_number", "").strip()
        issue_date = request.form.get("issue_date", "").strip()
        due_date = request.form.get("due_date", "").strip()
        currency = request.form.get("currency", "PEN").strip()
        subtotal = request.form.get("subtotal", "0").strip()
        tax = request.form.get("tax", "0").strip()
        total = request.form.get("total", "0").strip()

        file = request.files.get("pdf_file")

        if not file or not file.filename:
            flash("Debes subir un archivo PDF.", "error")
            return redirect(url_for("invoice_new"))

        if not allowed_pdf_file(file.filename):
            flash("Solo se permite subir archivos PDF.", "error")
            return redirect(url_for("invoice_new"))

        try:
            safe_name = secure_filename(file.filename)
            safe_name = unique_invoice_filename(safe_name)
            save_path = os.path.join(app.config["INVOICE_UPLOAD_FOLDER"], safe_name)
            file.save(save_path)

            file_size = os.path.getsize(save_path)

            invoice_file_id = create_invoice_file_record(
                original_filename=file.filename,
                stored_filename=safe_name,
                file_path=save_path,
                file_size=file_size
            )

            create_invoice_log(invoice_file_id, "SUBIDO", f"Archivo PDF subido: {file.filename}")

            raw_text = extract_text_from_pdf(save_path)
            auto_data = auto_extract_invoice_data(raw_text)

            if not supplier_name:
                supplier_name = auto_data["supplier_name"]

            if not supplier_ruc:
                supplier_ruc = auto_data["supplier_ruc"]

            if not invoice_type:
                invoice_type = auto_data["invoice_type"]

            if not invoice_series:
                invoice_series = auto_data["invoice_series"]

            if not invoice_number:
                invoice_number = auto_data["invoice_number"]

            if not issue_date:
                issue_date = auto_data["issue_date"]

            if not due_date:
                due_date = auto_data["due_date"]

            if not currency or currency == "PEN":
                currency = auto_data["currency"] or currency

            if float(subtotal or 0) == 0:
                subtotal = auto_data["subtotal"]

            if float(tax or 0) == 0:
                tax = auto_data["tax"]

            if float(total or 0) == 0:
                total = auto_data["total"]

            issue_date_db = normalize_date_for_db(str(issue_date)) if issue_date else None
            due_date_db = normalize_date_for_db(str(due_date)) if due_date else None

            full_number = f"{invoice_series}-{invoice_number}" if invoice_series and invoice_number else ""
            raw_json = auto_data

            invoice_id = create_invoice(
                invoice_file_id=invoice_file_id,
                supplier_id=supplier_id,
                supplier_name=supplier_name,
                supplier_ruc=supplier_ruc,
                invoice_type=invoice_type,
                invoice_series=invoice_series,
                invoice_number=invoice_number,
                full_number=full_number,
                issue_date=issue_date_db,
                due_date=due_date_db,
                currency=currency,
                subtotal=float(subtotal or 0),
                tax=float(tax or 0),
                total=float(total or 0),
                raw_text=raw_text,
                raw_json=raw_json
            )

            update_invoice_file_status(invoice_file_id, "PROCESADO")
            create_invoice_log(invoice_file_id, "PROCESADO", f"Factura registrada con ID {invoice_id}")

            flash("✅ Factura registrada correctamente.", "ok")
            return redirect(url_for("invoice_detail", invoice_id=invoice_id))

        except Exception as e:
            flash(f"Error registrando factura: {e}", "error")
            return redirect(url_for("invoice_new"))

    return render_template(
        "invoice_new.html",
        username=session.get("username"),
        suppliers=suppliers
    )


@app.route("/facturas/<int:invoice_id>")
def invoice_detail(invoice_id):
    if not login_required():
        return redirect(url_for("login"))

    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        flash("Factura no encontrada.", "error")
        return redirect(url_for("invoices_list"))

    items = get_invoice_items(invoice_id)

    return render_template(
        "invoice_detail.html",
        username=session.get("username"),
        invoice=invoice,
        items=items
    )


@app.route("/facturas/<int:invoice_id>/agregar_item", methods=["POST"])
def invoice_add_item(invoice_id):
    if not login_required():
        return redirect(url_for("login"))

    description = request.form.get("description", "").strip()
    qty = request.form.get("qty", "0").strip()
    unit_measure = request.form.get("unit_measure", "").strip()
    unit_price = request.form.get("unit_price", "0").strip()
    line_total = request.form.get("line_total", "0").strip()

    if not description:
        flash("La descripción del ítem es obligatoria.", "error")
        return redirect(url_for("invoice_detail", invoice_id=invoice_id))

    items = get_invoice_items(invoice_id)
    item_order = len(items) + 1

    try:
        create_invoice_item(
            invoice_id=invoice_id,
            item_order=item_order,
            description=description,
            qty=float(qty or 0),
            unit_measure=unit_measure,
            unit_price=float(unit_price or 0),
            line_total=float(line_total or 0)
        )
        flash("✅ Ítem agregado correctamente.", "ok")
    except Exception as e:
        flash(f"Error agregando ítem: {e}", "error")

    return redirect(url_for("invoice_detail", invoice_id=invoice_id))


@app.route("/facturas/<int:invoice_id>/editar", methods=["GET", "POST"])
def invoice_edit(invoice_id):
    if not login_required():
        return redirect(url_for("login"))

    invoice = get_invoice_by_id(invoice_id)
    if not invoice:
        flash("Factura no encontrada.", "error")
        return redirect(url_for("invoices_list"))

    suppliers = list_suppliers_full()

    if request.method == "POST":
        supplier_id_raw = request.form.get("supplier_id", "").strip()
        supplier_id = int(supplier_id_raw) if supplier_id_raw else None

        supplier_name = request.form.get("supplier_name", "").strip()
        supplier_ruc = request.form.get("supplier_ruc", "").strip()
        invoice_type = request.form.get("invoice_type", "").strip()
        invoice_series = request.form.get("invoice_series", "").strip()
        invoice_number = request.form.get("invoice_number", "").strip()
        issue_date = request.form.get("issue_date", "").strip()
        due_date = request.form.get("due_date", "").strip()
        currency = request.form.get("currency", "PEN").strip()
        subtotal = request.form.get("subtotal", "0").strip()
        tax = request.form.get("tax", "0").strip()
        total = request.form.get("total", "0").strip()

        full_number = f"{invoice_series}-{invoice_number}" if invoice_series and invoice_number else ""

        try:
            update_invoice(
                invoice_id=invoice_id,
                supplier_id=supplier_id,
                supplier_name=supplier_name,
                supplier_ruc=supplier_ruc,
                invoice_type=invoice_type,
                invoice_series=invoice_series,
                invoice_number=invoice_number,
                full_number=full_number,
                issue_date=normalize_date_for_db(issue_date),
                due_date=normalize_date_for_db(due_date),
                currency=currency,
                subtotal=float(subtotal or 0),
                tax=float(tax or 0),
                total=float(total or 0)
            )
            flash("✅ Factura actualizada correctamente.", "ok")
            return redirect(url_for("invoice_detail", invoice_id=invoice_id))
        except Exception as e:
            flash(f"Error actualizando factura: {e}", "error")
            return redirect(url_for("invoice_edit", invoice_id=invoice_id))

    return render_template(
        "invoice_edit.html",
        username=session.get("username"),
        invoice=invoice,
        suppliers=suppliers
    )


@app.route("/facturas/<int:invoice_id>/eliminar", methods=["POST"])
def invoice_delete(invoice_id):
    if not login_required():
        return redirect(url_for("login"))

    try:
        file_row = delete_invoice(invoice_id)

        if file_row:
            stored_filename, file_path = file_row
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    pass

        flash("🗑 Factura eliminada correctamente.", "ok")
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:
        flash(f"Error eliminando factura: {e}", "error")

    return redirect(url_for("invoices_list"))


@app.route("/facturas/leer_pdf", methods=["POST"])
def invoice_read_pdf():
    if not login_required():
        return jsonify({"ok": False, "message": "Sesión no válida"}), 401

    file = request.files.get("pdf_file")

    if not file or not file.filename:
        return jsonify({"ok": False, "message": "No se recibió ningún archivo PDF."}), 400

    if not allowed_pdf_file(file.filename):
        return jsonify({"ok": False, "message": "Solo se permiten archivos PDF."}), 400

    temp_filename = secure_filename(file.filename)
    temp_filename = f"temp_{temp_filename}"
    temp_path = os.path.join(app.config["INVOICE_UPLOAD_FOLDER"], temp_filename)

    try:
        file.save(temp_path)

        raw_text = extract_text_from_pdf(temp_path)
        auto_data = auto_extract_invoice_data(raw_text)

        if os.path.exists(temp_path):
            os.remove(temp_path)

        return jsonify({
            "ok": True,
            "data": auto_data,
            "raw_text": raw_text[:5000]
        })

    except Exception as e:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

        return jsonify({
            "ok": False,
            "message": f"Error leyendo PDF: {e}"
        }), 500


if __name__ == "__main__":
    app.run(debug=True)