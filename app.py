from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import psycopg
import psycopg.errors
import os
import re
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback_secret")

DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME")

CONN_STR = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


def get_conn():
    return psycopg.connect(CONN_STR)


def login_required() -> bool:
    return "user_id" in session


PHONE_9_RE = re.compile(r"^\d{9}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")


def validate_phone_9(phone: str) -> bool:
    if not phone:
        return True
    return bool(PHONE_9_RE.match(phone))


def validate_email(email: str) -> bool:
    if not email:
        return True
    return bool(EMAIL_RE.match(email))


UPLOAD_FOLDER = os.path.join("static", "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def unique_filename(original_name: str) -> str:
    base, ext = os.path.splitext(original_name)
    candidate = original_name
    i = 1
    while os.path.exists(os.path.join(app.config["UPLOAD_FOLDER"], candidate)):
        candidate = f"{base}_{i}{ext}"
        i += 1
    return candidate


def get_user(username, password):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username FROM users WHERE username=%s AND password=%s",
                (username, password)
            )
            return cur.fetchone()


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


def list_suppliers():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM suppliers ORDER BY name ASC")
            return cur.fetchall()


def list_suppliers_full():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, phone, email, notes
                FROM suppliers
                ORDER BY name ASC
            """)
            return cur.fetchall()


def create_supplier(name: str, phone: str, email: str, notes: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO suppliers(name, phone, email, notes)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (name) DO NOTHING
            """, (name, phone or None, email or None, notes or None))
        conn.commit()


def get_supplier_by_id(supplier_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, phone, email, notes
                FROM suppliers
                WHERE id=%s
            """, (supplier_id,))
            return cur.fetchone()


def update_supplier(supplier_id: int, name: str, phone: str, email: str, notes: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE suppliers
                SET name=%s, phone=%s, email=%s, notes=%s
                WHERE id=%s
            """, (name, phone or None, email or None, notes or None, supplier_id))
        conn.commit()


def delete_supplier(supplier_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM suppliers WHERE id=%s", (supplier_id,))
        conn.commit()


def list_customers_full():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, phone, doc, email
                FROM customers
                ORDER BY name ASC
            """)
            return cur.fetchall()


def list_customers():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM customers ORDER BY name ASC")
            return cur.fetchall()


def create_customer(name: str, phone: str, doc: str, email: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO customers(name, phone, doc, email)
                VALUES (%s, %s, %s, %s)
            """, (name, phone or None, doc or None, email or None))
        conn.commit()


def get_customer_by_id(customer_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, name, phone, doc, email
                FROM customers
                WHERE id=%s
            """, (customer_id,))
            return cur.fetchone()


def update_customer(customer_id: int, name: str, phone: str, doc: str, email: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE customers
                SET name=%s, phone=%s, doc=%s, email=%s
                WHERE id=%s
            """, (name, phone or None, doc or None, email or None, customer_id))
        conn.commit()


def delete_customer(customer_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM customers WHERE id=%s", (customer_id,))
        conn.commit()


def count_products(search: str):
    q = "%" + search + "%"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*)
                FROM products p
                WHERE (%s = '' OR p.code ILIKE %s)
            """, (search, q))
            return cur.fetchone()[0]


def list_products(search: str, limit: int, offset: int):
    q = "%" + search + "%"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    p.id,
                    p.code,
                    p.name,
                    p.category,
                    p.image_url,
                    p.stock,
                    p.price,
                    p.min_stock,
                    COALESCE(s.name, '-') AS supplier_name
                FROM products p
                LEFT JOIN suppliers s ON s.id = p.supplier_id
                WHERE (%s = '' OR p.code ILIKE %s)
                ORDER BY p.id ASC
                LIMIT %s OFFSET %s
            """, (search, q, limit, offset))
            return cur.fetchall()


def create_product(code, name, category, category_id, supplier_id, stock, min_stock, price, image_filename):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO products (code, name, category, category_id, supplier_id, stock, min_stock, price, image_url)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (code, name, category, category_id, supplier_id, stock, min_stock, price, image_filename))
        conn.commit()


def get_product_by_id(product_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, code, name, category, category_id, supplier_id, image_url, stock, min_stock, price
                FROM products
                WHERE id=%s
            """, (product_id,))
            return cur.fetchone()


def update_product(product_id: int, code, name, category, category_id, supplier_id, stock, min_stock, price, image_filename):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE products
                SET code=%s, name=%s, category=%s, category_id=%s, supplier_id=%s,
                    stock=%s, min_stock=%s, price=%s, image_url=%s
                WHERE id=%s
            """, (code, name, category, category_id, supplier_id, stock, min_stock, price, image_filename, product_id))
        conn.commit()


def delete_product(product_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT image_url FROM products WHERE id=%s", (product_id,))
            row = cur.fetchone()
            image_filename = row[0] if row else None
            cur.execute("DELETE FROM products WHERE id=%s", (product_id,))
        conn.commit()
    return image_filename


def get_product_by_code(code: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, code, name, category, category_id, supplier_id, image_url, stock, min_stock, price
                FROM products
                WHERE code=%s
            """, (code,))
            return cur.fetchone()


def create_sale(product_id: int, qty: int, customer_id: int | None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT stock, price FROM products WHERE id=%s", (product_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError("Producto no existe")

            stock, unit_price = row
            if stock < qty:
                raise ValueError("Stock insuficiente")

            line_total = float(unit_price) * qty

            cur.execute("""
                INSERT INTO sales (total, customer_id)
                VALUES (%s, %s)
                RETURNING id
            """, (line_total, customer_id))
            sale_id = cur.fetchone()[0]

            cur.execute("""
                INSERT INTO sale_items (sale_id, product_id, qty, unit_price, line_total)
                VALUES (%s, %s, %s, %s, %s)
            """, (sale_id, product_id, qty, unit_price, line_total))

            cur.execute("""
                UPDATE products
                SET stock = stock - %s
                WHERE id=%s
                RETURNING stock
            """, (qty, product_id))
            new_stock = cur.fetchone()[0]

            cur.execute("""
                INSERT INTO kardex (product_id, movement, qty, stock_after, ref_table, ref_id, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (product_id, "SALIDA", qty, new_stock, "sales", sale_id, "Venta registrada"))

        conn.commit()
        return sale_id


def list_sales():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    s.id,
                    TO_CHAR(s.sold_at, 'DD-MM-YYYY HH24:MI:SS') as sold_at,
                    s.total,
                    COALESCE(c.name, '-') AS customer_name
                FROM sales s
                LEFT JOIN customers c ON c.id = s.customer_id
                ORDER BY s.sold_at DESC, s.id DESC
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
                    COALESCE(c.name, '-') AS customer_name
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
            return redirect(url_for("products_list"))

        flash("Usuario o contraseña incorrectos", "error")
        return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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
    except:
        per_page = 10

    try:
        page = int(request.args.get("page", "1"))
        if page < 1:
            page = 1
    except:
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
        name = request.form.get("name", "").strip()

        category_id_raw = request.form.get("category_id", "").strip()
        category_id = int(category_id_raw) if category_id_raw else None

        supplier_id_raw = request.form.get("supplier_id", "").strip()
        supplier_id = int(supplier_id_raw) if supplier_id_raw else None

        stock_raw = request.form.get("stock", "0").strip()
        min_stock_raw = request.form.get("min_stock", "0").strip()
        price_raw = request.form.get("price", "0").strip()

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
        except:
            flash("Stock inválido (0 o más).", "error")
            return redirect(url_for("products_new"))

        try:
            min_stock = int(min_stock_raw)
            if min_stock < 0:
                raise ValueError
        except:
            flash("Stock mínimo inválido (0 o más).", "error")
            return redirect(url_for("products_new"))

        try:
            price = float(price_raw.replace(",", "."))
            if price < 0:
                raise ValueError
        except:
            flash("Precio inválido (0 o más).", "error")
            return redirect(url_for("products_new"))

        image_filename = None
        file = request.files.get("image_file")
        if file and file.filename:
            if not allowed_file(file.filename):
                flash("Formato de imagen no permitido. Usa PNG/JPG/JPEG/WEBP.", "error")
                return redirect(url_for("products_new"))

            safe = secure_filename(file.filename)
            safe = unique_filename(safe)
            save_path = os.path.join(app.config["UPLOAD_FOLDER"], safe)
            file.save(save_path)
            image_filename = safe

        try:
            create_product(code, name, category, category_id, supplier_id, stock, min_stock, price, image_filename)
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
        name = request.form.get("name", "").strip()

        category_id_raw = request.form.get("category_id", "").strip()
        category_id = int(category_id_raw) if category_id_raw else None

        supplier_id_raw = request.form.get("supplier_id", "").strip()
        supplier_id = int(supplier_id_raw) if supplier_id_raw else None

        stock_raw = request.form.get("stock", "0").strip()
        min_stock_raw = request.form.get("min_stock", "0").strip()
        price_raw = request.form.get("price", "0").strip()

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
        except:
            flash("Stock inválido (0 o más).", "error")
            return redirect(url_for("products_edit", product_id=product_id))

        try:
            min_stock = int(min_stock_raw)
            if min_stock < 0:
                raise ValueError
        except:
            flash("Stock mínimo inválido (0 o más).", "error")
            return redirect(url_for("products_edit", product_id=product_id))

        try:
            price = float(price_raw.replace(",", "."))
            if price < 0:
                raise ValueError
        except:
            flash("Precio inválido (0 o más).", "error")
            return redirect(url_for("products_edit", product_id=product_id))

        image_filename = p[6]
        file = request.files.get("image_file")

        if file and file.filename:
            if not allowed_file(file.filename):
                flash("Formato de imagen no permitido. Usa PNG/JPG/JPEG/WEBP.", "error")
                return redirect(url_for("products_edit", product_id=product_id))

            safe = secure_filename(file.filename)
            safe = unique_filename(safe)
            save_path = os.path.join(app.config["UPLOAD_FOLDER"], safe)
            file.save(save_path)

            if image_filename:
                old_path = os.path.join(app.config["UPLOAD_FOLDER"], image_filename)
                if os.path.exists(old_path):
                    try:
                        os.remove(old_path)
                    except:
                        pass

            image_filename = safe

        try:
            update_product(
                product_id,
                code,
                name,
                category,
                category_id,
                supplier_id,
                stock,
                min_stock,
                price,
                image_filename
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
        image_filename = delete_product(product_id)

        if image_filename:
            path = os.path.join(app.config["UPLOAD_FOLDER"], image_filename)
            if os.path.exists(path):
                try:
                    os.remove(path)
                except:
                    pass

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
        notes = request.form.get("notes", "").strip()

        if not name:
            flash("El nombre del proveedor es obligatorio.", "error")
            return redirect(url_for("proveedores_nuevo"))

        if phone and not validate_phone_9(phone):
            flash("El teléfono debe tener exactamente 9 dígitos.", "error")
            return redirect(url_for("proveedores_nuevo"))

        if email and not validate_email(email):
            flash("El correo no es válido.", "error")
            return redirect(url_for("proveedores_nuevo"))

        try:
            create_supplier(name, phone, email, notes)
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
        notes = request.form.get("notes", "").strip()

        if not name:
            flash("El nombre del proveedor es obligatorio.", "error")
            return redirect(url_for("proveedores_edit", supplier_id=supplier_id))

        if phone and not validate_phone_9(phone):
            flash("El teléfono debe tener exactamente 9 dígitos.", "error")
            return redirect(url_for("proveedores_edit", supplier_id=supplier_id))

        if email and not validate_email(email):
            flash("El correo no es válido.", "error")
            return redirect(url_for("proveedores_edit", supplier_id=supplier_id))

        try:
            update_supplier(supplier_id, name, phone, email, notes)
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
        doc = request.form.get("doc", "").strip()
        email = request.form.get("email", "").strip()

        if not name:
            flash("El nombre del cliente es obligatorio.", "error")
            return redirect(url_for("cliente_nuevo"))

        if phone and not validate_phone_9(phone):
            flash("El teléfono debe tener exactamente 9 dígitos.", "error")
            return redirect(url_for("cliente_nuevo"))

        if email and not validate_email(email):
            flash("El correo no es válido.", "error")
            return redirect(url_for("cliente_nuevo"))

        try:
            create_customer(name, phone, doc, email)
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
        doc = request.form.get("doc", "").strip()
        email = request.form.get("email", "").strip()

        if not name:
            flash("El nombre del cliente es obligatorio.", "error")
            return redirect(url_for("cliente_edit", customer_id=customer_id))

        if phone and not validate_phone_9(phone):
            flash("El teléfono debe tener exactamente 9 dígitos.", "error")
            return redirect(url_for("cliente_edit", customer_id=customer_id))

        if email and not validate_email(email):
            flash("El correo no es válido.", "error")
            return redirect(url_for("cliente_edit", customer_id=customer_id))

        try:
            update_customer(customer_id, name, phone, doc, email)
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


@app.route("/ventas/nueva", methods=["GET", "POST"])
def venta_nueva():
    if not login_required():
        return redirect(url_for("login"))

    customers = list_customers()

    if request.method == "POST":
        code = request.form.get("code", "").strip()
        qty_raw = request.form.get("qty", "1").strip()
        customer_id_raw = request.form.get("customer_id", "").strip()
        customer_id = int(customer_id_raw) if customer_id_raw else None

        if not code:
            flash("Ingresa el código del producto.", "error")
            return redirect(url_for("venta_nueva"))

        try:
            qty = int(qty_raw)
            if qty <= 0:
                raise ValueError
        except:
            flash("Cantidad inválida.", "error")
            return redirect(url_for("venta_nueva"))

        product = get_product_by_code(code)
        if not product:
            flash("No existe un producto con ese código.", "error")
            return redirect(url_for("venta_nueva"))

        try:
            sale_id = create_sale(product_id=product[0], qty=qty, customer_id=customer_id)
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("venta_nueva"))
        except Exception as e:
            flash(f"Error registrando venta: {e}", "error")
            return redirect(url_for("venta_nueva"))

        flash(f"✅ Venta registrada (ID: {sale_id}).", "ok")
        return redirect(url_for("ventas"))

    return render_template("sale_new.html", username=session.get("username"), customers=customers)


@app.route("/ventas")
def ventas():
    if not login_required():
        return redirect(url_for("login"))

    sales = list_sales()
    return render_template("sales.html", username=session.get("username"), sales=sales)


@app.route("/ventas/<int:sale_id>")
def venta_detalle(sale_id):
    if not login_required():
        return redirect(url_for("login"))

    header = get_sale_header(sale_id)
    if not header:
        return jsonify({"ok": False, "message": "Venta no encontrada"}), 404

    items = get_sale_items(sale_id)

    return jsonify({
        "ok": True,
        "header": {
            "id": header[0],
            "sold_at": str(header[1]),
            "total": float(header[2]),
            "customer_name": header[3]
        },
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


if __name__ == "__main__":
    app.run(debug=True)